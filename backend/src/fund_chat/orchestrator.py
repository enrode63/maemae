from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any, Callable
from uuid import uuid4

from .audit import AuditLog
from .models import (ROLES, PROPOSAL_KINDS, AuthorizationError, Conversation,
                     DuplicateRequest, InvalidTransition, Proposal,
                     ValidationError, optional_context, require_text)


ApplyCallback = Callable[[dict[str, Any]], Any]


class ChatOrchestrator:
    """Deterministic fund-team chat. It performs no network or LLM calls."""

    def __init__(self, audit_path: Path, apply_callback: ApplyCallback | None = None):
        self.audit = AuditLog(audit_path)
        self.apply_callback = apply_callback
        self.conversations: dict[str, Conversation] = {}
        self.request_results: dict[str, dict[str, Any]] = {}
        self._restore()

    def _restore(self) -> None:
        for event in self.audit.read():
            p = event["payload"]
            kind = event["event_type"]
            if kind == "conversation_created":
                self.conversations[p["conversation_id"]] = Conversation(
                    p["conversation_id"], p.get("role"), p.get("team"), p.get("metadata", {}))
            elif kind == "chat_completed":
                conversation = self.conversations.setdefault(p["conversation_id"], Conversation(p["conversation_id"]))
                conversation.role = p.get("role", conversation.role)
                conversation.team = p.get("team", conversation.team)
                conversation.metadata.update(p.get("metadata", {}))
                conversation.messages.extend(p["messages"])
                for raw in p["proposals"]:
                    proposal = Proposal(**raw)
                    conversation.proposals[proposal.proposal_id] = proposal
                self.request_results[p["request_id"]] = p["result"]
            elif kind == "proposal_decided":
                proposal = self._proposal(p["conversation_id"], p["proposal_id"])
                proposal.status = p["status"]
                proposal.decision_reason = p["reason"]
                proposal.applied = p["applied"]
                self.request_results[p["request_id"]] = p["result"]

    @staticmethod
    def _new_id(prefix: str) -> str:
        return f"{prefix}_{uuid4().hex}"

    def create_conversation(self, conversation_id: str | None = None, role: str | None = None,
                            team: str | None = None,
                            metadata: dict[str, Any] | None = None) -> dict[str, Any]:
        cid = require_text(conversation_id, "conversation_id", 128) if conversation_id is not None else self._new_id("conv")
        role, team, metadata = optional_context(role, team, metadata)
        if cid in self.conversations:
            raise ValidationError("conversation_id already exists")
        self.conversations[cid] = Conversation(cid, role, team, metadata)
        result = {"conversation_id": cid, "role": role, "team": team, "metadata": metadata}
        self.audit.append("conversation_created", result)
        return result

    def send_message(self, conversation_id: str, request_id: str, content: str,
                     proposal_kind: str | None = None, role: str | None = None,
                     team: str | None = None,
                     metadata: dict[str, Any] | None = None) -> dict[str, Any]:
        cid = require_text(conversation_id, "conversation_id", 128)
        rid = require_text(request_id, "request_id", 128)
        text = require_text(content, "content")
        if rid in self.request_results:
            raise DuplicateRequest("request_id already processed")
        conversation = self.conversations.get(cid)
        if conversation is None:
            raise ValidationError("conversation not found")
        role, team, metadata = optional_context(role, team, metadata)
        if proposal_kind is not None and proposal_kind not in PROPOSAL_KINDS:
            raise ValidationError("proposal_kind must be strategy or code")

        if role is not None:
            conversation.role = role
        if team is not None:
            conversation.team = team
        conversation.metadata.update(metadata)

        user_id = self._new_id("msg")
        messages = [{"message_id": user_id, "role": "User", "content": text}]
        for role in ROLES:
            messages.append({"message_id": self._new_id("msg"), "role": role,
                             "content": self._response(role, text)})
        proposals: list[Proposal] = []
        if proposal_kind:
            digest = hashlib.sha256(text.encode("utf-8")).hexdigest()[:12]
            changes = ({"strategy_note": text, "deterministic_ref": digest}
                       if proposal_kind == "strategy" else
                       {"patch_intent": text, "deterministic_ref": digest,
                        "execution": "disabled_until_pm_approval"})
            proposals.append(Proposal(self._new_id("prop"), cid, user_id, proposal_kind,
                                      f"{proposal_kind.title()} modification {digest}", changes))
        result = {"conversation_id": cid, "request_id": rid, "messages": messages,
                  "proposals": [p.json() for p in proposals], "role": conversation.role,
                  "team": conversation.team, "metadata": dict(conversation.metadata)}
        # One event is the atomic durable unit used for idempotency restoration.
        self.audit.append("chat_completed", {"conversation_id": cid, "request_id": rid,
                          "messages": messages, "proposals": result["proposals"],
                          "role": conversation.role, "team": conversation.team,
                          "metadata": dict(conversation.metadata), "result": result})
        conversation.messages.extend(messages)
        for proposal in proposals:
            conversation.proposals[proposal.proposal_id] = proposal
        self.request_results[rid] = result
        return result

    @staticmethod
    def _response(role: str, content: str) -> str:
        topic = content[:120]
        templates = {
            "PM": "요청을 팀 검토에 회부했습니다. 제안은 명시적 승인 전 반영되지 않습니다.",
            "Bull": f"상승 관점: '{topic}'의 기대효과와 유리한 조건을 검토합니다.",
            "Bear": f"하락 관점: '{topic}'의 실패 조건과 반대 시나리오를 검토합니다.",
            "Risk": "위험 관점: 포지션·손실 한도와 승인 게이트를 우선 적용해야 합니다.",
            "Research": f"리서치 관점: '{topic}'을 검증할 데이터와 재현 가능한 가정을 기록합니다.",
        }
        return templates[role]

    def decide_proposal(self, conversation_id: str, proposal_id: str, request_id: str,
                        approve: bool, reason: str,
                        actor_role: str | None = None) -> dict[str, Any]:
        cid = require_text(conversation_id, "conversation_id", 128)
        pid = require_text(proposal_id, "proposal_id", 128)
        rid = require_text(request_id, "request_id", 128)
        why = require_text(reason, "reason", 1000)
        if not isinstance(approve, bool):
            raise ValidationError("approve must be a boolean")
        if actor_role != "PM":
            self.audit.append("proposal_decision_denied", {
                "conversation_id": cid,
                "proposal_id": pid,
                "request_id": rid,
                "actor_role": actor_role,
                "decision": "approve" if approve else "reject",
                "reason": "PM role required",
            })
            raise AuthorizationError("only the PM role may approve or reject proposals")
        if rid in self.request_results:
            raise DuplicateRequest("request_id already processed")
        proposal = self._proposal(cid, pid)
        if proposal.status != "pending":
            raise InvalidTransition(f"proposal is already {proposal.status}")
        applied = False
        application_result = None
        if approve and self.apply_callback is not None:
            application_result = self.apply_callback(proposal.json())
            applied = True
        status = "approved" if approve else "rejected"
        result = {"conversation_id": cid, "proposal_id": pid, "request_id": rid,
                  "status": status, "applied": applied, "application_result": application_result}
        self.audit.append("proposal_decided", {"conversation_id": cid, "proposal_id": pid,
                          "request_id": rid, "status": status, "reason": why,
                          "applied": applied, "application_result": application_result,
                          "result": result})
        proposal.status, proposal.decision_reason, proposal.applied = status, why, applied
        self.request_results[rid] = result
        return result

    def _proposal(self, conversation_id: str, proposal_id: str) -> Proposal:
        conversation = self.conversations.get(conversation_id)
        if conversation is None:
            raise ValidationError("conversation not found")
        proposal = conversation.proposals.get(proposal_id)
        if proposal is None:
            raise ValidationError("proposal not found")
        return proposal
