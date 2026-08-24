import http.client
import json
import os
from decimal import Decimal
from pathlib import Path
import tempfile
import threading
import unittest
from unittest import mock

from local_runtime.api import (LOCAL_ALLOWED_ORIGINS, build_parser, make_handler,
                               parse_allowed_origins, public_auth_token)
from local_runtime.runtime import LocalRuntime, RuntimeConfig
from local_runtime.ticks import DeterministicTickSource
from http.server import ThreadingHTTPServer


class TickAndWorkerTests(unittest.TestCase):
    def test_cli_rejects_non_ipv4_loopback_bind(self):
        parser = build_parser()
        with self.assertRaises(SystemExit):
            parser.parse_args(["--host", "::1"])
        self.assertEqual(parser.parse_args(["--host", "localhost"]).host, "localhost")
        self.assertEqual(parser.parse_args(["--host", "127.0.0.1"]).host, "127.0.0.1")

    def test_public_bind_requires_explicit_mode_and_token(self):
        for environ in ({}, {"LOCAL_RUNTIME_PUBLIC_MODE": "1"},
                        {"LOCAL_RUNTIME_AUTH_TOKEN": "secret"},
                        {"LOCAL_RUNTIME_PUBLIC_MODE": "true", "LOCAL_RUNTIME_AUTH_TOKEN": "secret"}):
            with self.subTest(environ=environ), mock.patch.dict(os.environ, environ, clear=True):
                with self.assertRaises(SystemExit):
                    build_parser().parse_args(["--host", "0.0.0.0"])
        with mock.patch.dict(os.environ, {"LOCAL_RUNTIME_PUBLIC_MODE": "1",
                                          "LOCAL_RUNTIME_AUTH_TOKEN": "secret"}, clear=True):
            self.assertEqual(build_parser().parse_args(["--host", "0.0.0.0"]).host, "0.0.0.0")

    def test_public_mode_itself_fails_closed_without_token(self):
        with mock.patch.dict(os.environ, {"LOCAL_RUNTIME_PUBLIC_MODE": "1"}, clear=True):
            with self.assertRaisesRegex(ValueError, "non-empty"):
                public_auth_token()
        with mock.patch.dict(os.environ, {"LOCAL_RUNTIME_PUBLIC_MODE": "1",
                                          "LOCAL_RUNTIME_AUTH_TOKEN": "secret"}, clear=True):
            self.assertEqual(public_auth_token(), "secret")
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertIsNone(public_auth_token())

    def test_render_port_is_used_without_local_override(self):
        with mock.patch.dict(os.environ, {"PORT": "10000"}, clear=True):
            self.assertEqual(build_parser().parse_args([]).port, 10000)
        with mock.patch.dict(os.environ, {"PORT": "10000",
                                          "LOCAL_RUNTIME_PORT": "9876"}, clear=True):
            self.assertEqual(build_parser().parse_args([]).port, 9876)

    def test_environment_defaults_keep_loopback_and_cli_can_override(self):
        environ = {"LOCAL_RUNTIME_HOST": "localhost", "LOCAL_RUNTIME_PORT": "9876",
                   "LOCAL_RUNTIME_STATE_DIR": "env-state"}
        with mock.patch.dict(os.environ, environ, clear=False):
            parser = build_parser()
            args = parser.parse_args([])
            self.assertEqual((args.host, args.port, args.state_dir),
                             ("localhost", 9876, Path("env-state")))
            self.assertEqual(parser.parse_args(["--port", "8765"]).port, 8765)
            with self.assertRaises(SystemExit):
                parser.parse_args(["--port", "0"])
            with self.assertRaises(SystemExit):
                parser.parse_args(["--allowed-origins", "https://unexpected.example"])
        with mock.patch.dict(os.environ, {"LOCAL_RUNTIME_HOST": "0.0.0.0"}, clear=False):
            with self.assertRaises(SystemExit):
                build_parser().parse_args([])

    def test_cors_origins_are_exact_and_wildcards_are_rejected(self):
        configured = parse_allowed_origins("https://app.example.com, https://preview.example.com:8443")
        self.assertTrue(LOCAL_ALLOWED_ORIGINS.issubset(configured))
        self.assertIn("https://app.example.com", configured)
        for unsafe in ("*", "https://*.example.com", "https://app.example.com/path",
                       "https://user@app.example.com", "https://app.example.com?x=1"):
            with self.subTest(unsafe=unsafe), self.assertRaises(ValueError):
                parse_allowed_origins(unsafe)

    def test_seeded_ticks_are_reproducible(self):
        a = DeterministicTickSource(42)
        b = DeterministicTickSource(42)
        self.assertEqual([a.next().json() for _ in range(20)], [b.next().json() for _ in range(20)])

    def test_scalping_signal_has_explicit_demo_pm_approval(self):
        with tempfile.TemporaryDirectory() as tmp:
            runtime = LocalRuntime(Path(tmp), RuntimeConfig(seed=1, interval_seconds=.01))
            runtime.step()
            result = runtime.step()
            # Seed 1 produces an upward second tick.
            signal = result["signal"]
            self.assertEqual(signal["side"], "BUY")
            self.assertTrue(signal["pm_approved"])
            self.assertIn("demo-only", signal["pm_reason"])
            event_types = [e["event_type"] for e in result["engine_events"]]
            self.assertLess(event_types.index("risk_approved"), event_types.index("pm_approved"))
            self.assertIn("order_filled", event_types)

    def test_stop_and_error_are_safe_and_preserved(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp)
            runtime = LocalRuntime(path, RuntimeConfig(interval_seconds=.01))
            runtime.start("start")
            runtime.stop("stop")
            self.assertEqual(runtime.status, "stopped")
            sequence = runtime.ticks.sequence
            threading.Event().wait(.03)
            self.assertEqual(runtime.ticks.sequence, sequence)
            self.assertIn("worker_stopped", [e["event_type"] for e in runtime.event_list()])

            broken = LocalRuntime(path / "broken", RuntimeConfig(interval_seconds=.01))
            broken.ticks.next = lambda: (_ for _ in ()).throw(RuntimeError("synthetic failure"))
            broken.start("start-error")
            broken._thread.join(1)
            self.assertEqual(broken.status, "error_stopped")
            self.assertIn("synthetic failure", broken.error)
            self.assertEqual(broken.event_list()[-1]["event_type"], "worker_error_stopped")

    def test_restart_continues_ticks_and_restores_portfolio(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp)
            runtime = LocalRuntime(path, RuntimeConfig(seed=1))
            runtime.step()
            runtime.step()  # deterministic BUY fill
            cash = runtime.engine.cash
            restored = LocalRuntime(path, RuntimeConfig(seed=1))
            self.assertEqual(restored.ticks.sequence, 2)
            self.assertEqual(restored.engine.cash, cash.quantize(Decimal("0.00000001")))
            self.assertEqual(restored.step()["tick"]["sequence"], 3)

    def test_state_reads_wait_for_worker_snapshot_lock(self):
        with tempfile.TemporaryDirectory() as tmp:
            runtime = LocalRuntime(Path(tmp))
            completed = []

            def read_state():
                completed.append((runtime.results(), runtime.event_list()))

            with runtime._lock:
                reader = threading.Thread(target=read_state)
                reader.start()
                threading.Event().wait(.03)
                self.assertEqual(completed, [])
            reader.join(1)
            self.assertFalse(reader.is_alive())
            self.assertEqual(len(completed), 1)


class ApiTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.runtime = LocalRuntime(Path(self.temp.name))
        origins = parse_allowed_origins("https://trading-ui.vercel.app")
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(self.runtime, origins))
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.temp.cleanup()

    def request(self, method, path, body=None, origin=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.server.server_port)
        headers = {"Content-Type": "application/json"}
        if origin:
            headers["Origin"] = origin
        conn.request(method, path, json.dumps(body).encode() if body is not None else None, headers)
        response = conn.getresponse()
        value = json.loads(response.read() or b"{}")
        headers = dict(response.getheaders())
        conn.close()
        return response.status, value, headers

    def test_health_chat_and_approval_api(self):
        status, health, headers = self.request("GET", "/health", origin="http://localhost:3000")
        self.assertEqual((status, health["ok"]), (200, True))
        self.assertEqual(headers["Access-Control-Allow-Origin"], "http://localhost:3000")
        _, _, denied_headers = self.request("GET", "/health", origin="https://example.com")
        self.assertNotIn("Access-Control-Allow-Origin", denied_headers)
        _, _, vercel_headers = self.request("GET", "/health", origin="https://trading-ui.vercel.app")
        self.assertEqual(vercel_headers["Access-Control-Allow-Origin"], "https://trading-ui.vercel.app")

        _, conv, _ = self.request("POST", "/chat/conversations", {"conversation_id": "api"})
        _, chat, _ = self.request("POST", "/chat/send", {"conversation_id": conv["conversation_id"], "request_id": "send", "content": "test", "proposal_kind": "strategy"})
        proposal = chat["proposals"][0]
        status, decision, _ = self.request("POST", "/proposals/approve", {"conversation_id": "api", "proposal_id": proposal["proposal_id"], "request_id": "approve", "reason": "demo", "actor_role": "PM"})
        self.assertEqual((status, decision["status"], decision["applied"]), (200, "approved", True))

    def test_reports_reject_invalid_team_with_json_400(self):
        status, value, headers = self.request("GET", "/automation/reports?team=bogus")
        self.assertEqual(status, 400)
        self.assertEqual(value, {"error": "ValueError", "message": "invalid team"})
        self.assertEqual(headers["Content-Type"], "application/json; charset=utf-8")

    def test_chat_metadata_roundtrip_and_content_contract(self):
        context = {"role": "Risk", "team": "alpha",
                   "metadata": {"channel": "desk-chat", "team_label": "Alpha Desk"}}
        status, created, _ = self.request(
            "POST", "/chat/conversations", {"conversation_id": "metadata-api", **context})
        self.assertEqual(status, 200)
        self.assertEqual(created, {"conversation_id": "metadata-api", **context})
        status, sent, _ = self.request(
            "POST", "/chat/send", {"conversation_id": "metadata-api",
             "request_id": "content-send", "content": "protect downside"})
        self.assertEqual(status, 200)
        self.assertEqual({key: sent[key] for key in context}, context)
        self.assertEqual(sent["messages"][0]["content"], "protect downside")
        completed = next(e for e in self.runtime.chat.audit.read()
                         if e["event_type"] == "chat_completed")
        self.assertEqual(completed["payload"]["metadata"], context["metadata"])


class PublicApiTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.runtime = LocalRuntime(Path(self.temp.name))
        origins = parse_allowed_origins("https://trading-ui.vercel.app")
        self.server = ThreadingHTTPServer(
            ("127.0.0.1", 0), make_handler(self.runtime, origins, "public-secret"))
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.temp.cleanup()

    def request(self, method, path, body=None, origin=None, token=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.server.server_port)
        headers = {"Content-Type": "application/json"}
        if origin:
            headers["Origin"] = origin
        if token is not None:
            headers["Authorization"] = f"Bearer {token}"
        conn.request(method, path, json.dumps(body).encode() if body is not None else None, headers)
        response = conn.getresponse()
        value = json.loads(response.read() or b"{}")
        response_headers = dict(response.getheaders())
        conn.close()
        return response.status, value, response_headers

    def test_public_health_is_open_but_chat_requires_bearer_token(self):
        self.assertEqual(self.request("GET", "/health")[0], 200)
        self.assertEqual(self.request("POST", "/chat/conversations", {})[0], 401)
        self.assertEqual(self.request("POST", "/chat/conversations", {}, token="wrong")[0], 401)
        status, value, _ = self.request("POST", "/chat/conversations",
                                        {"conversation_id": "authorized"}, token="public-secret")
        self.assertEqual((status, value["conversation_id"]), (200, "authorized"))

    def test_public_cors_remains_exact_and_options_is_open(self):
        status, _, headers = self.request("OPTIONS", "/chat/send",
                                          origin="https://trading-ui.vercel.app")
        self.assertEqual(status, 204)
        self.assertEqual(headers["Access-Control-Allow-Origin"], "https://trading-ui.vercel.app")
        self.assertIn("Authorization", headers["Access-Control-Allow-Headers"])
        _, _, denied = self.request("GET", "/health", origin="https://evil.example")
        self.assertNotIn("Access-Control-Allow-Origin", denied)


if __name__ == "__main__":
    unittest.main()
