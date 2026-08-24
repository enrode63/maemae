from __future__ import annotations

import argparse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
from urllib.parse import urlparse

from fund_chat import ChatError
from .runtime import LocalRuntime, RuntimeConfig


LOCAL_ALLOWED_ORIGINS = frozenset({"http://localhost", "http://127.0.0.1", "http://localhost:3000", "http://127.0.0.1:3000"})
BIND_HOSTS = ("localhost", "127.0.0.1")
ENV_PREFIX = "LOCAL_RUNTIME_"


def parse_allowed_origins(value: str | None) -> frozenset[str]:
    """Return local defaults plus explicitly configured, exact browser origins."""
    origins = set(LOCAL_ALLOWED_ORIGINS)
    for item in (value or "").split(","):
        origin = item.strip()
        if not origin:
            continue
        parsed = urlparse(origin)
        if ("*" in origin or parsed.scheme not in ("http", "https") or not parsed.netloc
                or parsed.username is not None or parsed.password is not None
                or parsed.path not in ("",) or parsed.params or parsed.query or parsed.fragment
                or origin != f"{parsed.scheme}://{parsed.netloc}"):
            raise ValueError(f"invalid CORS origin: {origin!r}")
        origins.add(origin)
    return frozenset(origins)


def port_number(value: str) -> int:
    port = int(value)
    if not 1 <= port <= 65535:
        raise argparse.ArgumentTypeError("port must be between 1 and 65535")
    return port


def bind_host(value: str) -> str:
    if value not in BIND_HOSTS:
        raise argparse.ArgumentTypeError("host must be localhost or 127.0.0.1")
    return value


def make_handler(runtime: LocalRuntime, allowed_origins: frozenset[str] = LOCAL_ALLOWED_ORIGINS):
    class Handler(BaseHTTPRequestHandler):
        def _cors(self) -> None:
            origin = self.headers.get("Origin")
            if origin in allowed_origins:
                self.send_header("Access-Control-Allow-Origin", origin)
                self.send_header("Vary", "Origin")

        def _send(self, status: int, value: object) -> None:
            body = json.dumps(value, ensure_ascii=False, sort_keys=True).encode()
            self.send_response(status)
            self._cors()
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _body(self) -> dict:
            length = int(self.headers.get("Content-Length", "0"))
            if length > 1_000_000:
                raise ValueError("request body too large")
            value = json.loads(self.rfile.read(length) or b"{}")
            if not isinstance(value, dict):
                raise ValueError("JSON object required")
            return value

        def do_OPTIONS(self):
            self.send_response(204)
            self._cors()
            self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
            self.send_header("Access-Control-Allow-Headers", "Content-Type")
            self.end_headers()

        def do_GET(self):
            path = urlparse(self.path).path
            routes = {"/health": lambda: {"ok": True, "scope": "localhost-demo-only"},
                      "/runtime/status": runtime.status_json,
                      "/results": runtime.results,
                      "/events": runtime.event_list}
            if path not in routes:
                return self._send(404, {"error": "not_found"})
            self._send(200, routes[path]())

        def do_POST(self):
            try:
                body, path = self._body(), urlparse(self.path).path
                if path == "/chat/conversations":
                    value = runtime.chat.create_conversation(
                        body.get("conversation_id"), body.get("role"), body.get("team"),
                        body.get("metadata"))
                elif path == "/chat/send":
                    value = runtime.chat.send_message(
                        body["conversation_id"], body["request_id"], body["content"],
                        body.get("proposal_kind"), body.get("role"), body.get("team"),
                        body.get("metadata"))
                elif path in ("/proposals/approve", "/proposals/reject"):
                    value = runtime.chat.decide_proposal(body["conversation_id"], body["proposal_id"], body["request_id"], path.endswith("approve"), body["reason"], body.get("actor_role"))
                elif path in ("/runtime/start", "/runtime/pause", "/runtime/stop"):
                    value = getattr(runtime, path.rsplit("/", 1)[1])(body["request_id"])
                else:
                    return self._send(404, {"error": "not_found"})
                self._send(200, value)
            except (KeyError, ValueError, ChatError, json.JSONDecodeError) as exc:
                self._send(400, {"error": type(exc).__name__, "message": str(exc)})

        def log_message(self, format, *args):
            pass
    return Handler


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="localhost-only deterministic demo runtime")
    parser.add_argument("--host", choices=BIND_HOSTS, type=bind_host,
                        default=os.getenv(f"{ENV_PREFIX}HOST", "127.0.0.1"))
    parser.add_argument("--port", type=port_number, default=os.getenv(f"{ENV_PREFIX}PORT", "8765"))
    parser.add_argument("--state-dir", type=Path, default=os.getenv(f"{ENV_PREFIX}STATE_DIR", "local-state"))
    parser.add_argument("--interval-seconds", type=float, default=os.getenv(f"{ENV_PREFIX}INTERVAL_SECONDS", "1.0"))
    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    try:
        allowed_origins = parse_allowed_origins(os.getenv(f"{ENV_PREFIX}ALLOWED_ORIGINS"))
    except ValueError as exc:
        parser.error(str(exc))
    runtime = LocalRuntime(args.state_dir, RuntimeConfig(interval_seconds=args.interval_seconds))
    ThreadingHTTPServer((args.host, args.port), make_handler(runtime, allowed_origins)).serve_forever()


if __name__ == "__main__":
    main()
