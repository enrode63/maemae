import json
from pathlib import Path
import tempfile
import unittest

from fund_chat import (AuthorizationError, ChatOrchestrator, DuplicateRequest,
                       InvalidTransition, ValidationError)


class ChatTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.log = Path(self.temp.name) / "chat-audit.jsonl"
        self.applied = []
        self.chat = ChatOrchestrator(self.log, self.applied.append)
        self.cid = self.chat.create_conversation("conv-test")["conversation_id"]

    def tearDown(self):
        self.temp.cleanup()

    def proposal(self, request="req-1", kind="strategy"):
        return self.chat.send_message(self.cid, request, "DEMO 전략을 보수적으로 변경", kind)["proposals"][0]

    def test_message_routes_to_all_roles_and_assigns_ids(self):
        result = self.chat.send_message(self.cid, "req-chat", "시장 검토")
        self.assertEqual([m["role"] for m in result["messages"]],
                         ["User", "PM", "Bull", "Bear", "Risk", "Research"])
        self.assertEqual(len({m["message_id"] for m in result["messages"]}), 6)
        self.assertTrue(all(m["content"] for m in result["messages"]))

    def test_conversation_metadata_round_trips_through_audit_and_restart(self):
        path = Path(self.temp.name) / "metadata.jsonl"
        chat = ChatOrchestrator(path)
        created = chat.create_conversation(
            "metadata-conv", role="Research", team="macro",
            metadata={"channel": "fund-room", "team_label": "Macro Team"})
        result = chat.send_message("metadata-conv", "metadata-send", "inflation outlook")
        self.assertEqual(result["role"], "Research")
        self.assertEqual(result["team"], "macro")
        self.assertEqual(result["metadata"], created["metadata"])

        restored = ChatOrchestrator(path)
        conversation = restored.conversations["metadata-conv"]
        self.assertEqual((conversation.role, conversation.team), ("Research", "macro"))
        self.assertEqual(conversation.metadata["team_label"], "Macro Team")
        events = restored.audit.read()
        self.assertEqual(events[0]["payload"]["metadata"], created["metadata"])
        self.assertEqual(events[1]["payload"]["result"]["metadata"], created["metadata"])

    def test_context_validation_keeps_allowed_roles_and_metadata_fields(self):
        bad_contexts = [
            {"role": "Admin"}, {"team": " "}, {"metadata": []},
            {"metadata": {"channel": ""}}, {"metadata": {"arbitrary": "value"}},
        ]
        for index, kwargs in enumerate(bad_contexts):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValidationError):
                self.chat.send_message(self.cid, f"bad-{index}", "hello", **kwargs)

    def test_proposal_is_not_applied_before_approval(self):
        proposal = self.proposal()
        self.assertEqual(proposal["status"], "pending")
        self.assertEqual(self.applied, [])
        decision = self.chat.decide_proposal(self.cid, proposal["proposal_id"], "approve-1", True, "PM 승인", "PM")
        self.assertTrue(decision["applied"])
        self.assertEqual(len(self.applied), 1)

    def test_rejection_never_applies_and_terminal_state_is_safe(self):
        proposal = self.proposal()
        result = self.chat.decide_proposal(self.cid, proposal["proposal_id"], "reject-1", False, "위험 과다", "PM")
        self.assertEqual(result["status"], "rejected")
        self.assertFalse(result["applied"])
        with self.assertRaises(InvalidTransition):
            self.chat.decide_proposal(self.cid, proposal["proposal_id"], "approve-late", True, "번복", "PM")
        self.assertEqual(self.applied, [])

    def test_code_proposal_has_disabled_execution_marker(self):
        proposal = self.proposal(kind="code")
        self.assertEqual(proposal["kind"], "code")
        self.assertEqual(proposal["changes"]["execution"], "disabled_until_pm_approval")

    def test_duplicate_request_is_blocked_across_restart(self):
        self.chat.send_message(self.cid, "same", "hello")
        restored = ChatOrchestrator(self.log)
        with self.assertRaises(DuplicateRequest):
            restored.send_message(self.cid, "same", "changed")

    def test_state_and_ids_restore_from_append_only_log(self):
        proposal = self.proposal()
        before = self.log.read_bytes()
        self.chat.decide_proposal(self.cid, proposal["proposal_id"], "decision", True, "ok", "PM")
        after = self.log.read_bytes()
        self.assertTrue(after.startswith(before))
        restored = ChatOrchestrator(self.log)
        item = restored.conversations[self.cid].proposals[proposal["proposal_id"]]
        self.assertEqual(item.status, "approved")
        self.assertTrue(item.applied)
        with self.assertRaises(DuplicateRequest):
            restored.send_message(self.cid, "decision", "재사용")
        events = [json.loads(line) for line in self.log.read_text(encoding="utf-8").splitlines()]
        self.assertEqual([e["sequence"] for e in events], list(range(1, len(events) + 1)))

    def test_validation_rejects_bad_inputs_without_logging_chat(self):
        initial_lines = len(self.log.read_text(encoding="utf-8").splitlines())
        bad_calls = [
            lambda: self.chat.send_message(self.cid, "", "hello"),
            lambda: self.chat.send_message(self.cid, "x", " "),
            lambda: self.chat.send_message("missing", "x", "hello"),
            lambda: self.chat.send_message(self.cid, "x", "hello", "shell"),
        ]
        for call in bad_calls:
            with self.assertRaises(ValidationError):
                call()
        self.assertEqual(len(self.log.read_text(encoding="utf-8").splitlines()), initial_lines)

    def test_deterministic_content_for_same_input(self):
        first = self.chat.send_message(self.cid, "a", "동일 입력", "strategy")
        second = self.chat.send_message(self.cid, "b", "동일 입력", "strategy")
        self.assertEqual([m["content"] for m in first["messages"]],
                         [m["content"] for m in second["messages"]])
        self.assertEqual(first["proposals"][0]["changes"], second["proposals"][0]["changes"])

    def test_non_pm_cannot_approve_or_reject_and_denials_are_audited(self):
        for approve, actor_role, request_id in [
            (True, "Risk", "unauthorized-approve"),
            (False, None, "unauthorized-reject"),
        ]:
            proposal = self.proposal(request=f"proposal-{request_id}")
            with self.assertRaisesRegex(AuthorizationError, "only the PM role"):
                self.chat.decide_proposal(
                    self.cid, proposal["proposal_id"], request_id, approve, "attempt", actor_role
                )
            item = self.chat.conversations[self.cid].proposals[proposal["proposal_id"]]
            self.assertEqual(item.status, "pending")

        self.assertEqual(self.applied, [])
        events = [json.loads(line) for line in self.log.read_text(encoding="utf-8").splitlines()]
        denied = [e for e in events if e["event_type"] == "proposal_decision_denied"]
        self.assertEqual(len(denied), 2)
        self.assertEqual([e["payload"]["decision"] for e in denied], ["approve", "reject"])

    def test_pm_approval_invokes_callback(self):
        proposal = self.proposal()
        result = self.chat.decide_proposal(
            self.cid, proposal["proposal_id"], "pm-approve", True, "approved", actor_role="PM"
        )
        self.assertTrue(result["applied"])
        self.assertEqual([item["proposal_id"] for item in self.applied], [proposal["proposal_id"]])


if __name__ == "__main__":
    unittest.main()
