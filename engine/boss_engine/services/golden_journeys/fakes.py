"""The stand-ins a journey starts for itself. Stdlib only; each keeps what it was sent.

  mcp_http  An MCP server over streamable HTTP (JSON-RPC 2.0 on POST /mcp) that refuses every
            request without `Authorization: Bearer <its key>` (401). The key is made fresh per
            run and handed to the journey as `{{secret.mcp_http.key}}`, which the journey stores
            in BOSS's vault: the fake proves BOSS sent the vault's key, while the key itself
            never reaches a recorded call, a log or a result row. One tool, `ledger_lookup`,
            answers a fixed balance for an account and records the call.

WHERE A FAKE LISTENS. BOSS must reach it: from an install in Docker that is the host
(BOSS_JOURNEY_FAKE_HOST, e.g. host.docker.internal); from the release gate or a process on
the same machine, 127.0.0.1 (the default).
"""
from __future__ import annotations

import json
import os
import secrets
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

LEDGER_TOOL = {
    "name": "ledger_lookup",
    "description": "The balance of one account in the preview ledger.",
    "inputSchema": {"type": "object", "properties": {"account": {"type": "string"}},
                    "required": ["account"]},
}


class FakeMcpHttp:
    """`url` (…/mcp), `held` ({"key": …}: never recorded), `log()` (what it was sent), `stop()`."""

    def __init__(self, *, bind: str = "", advertise: str = ""):
        self.key = secrets.token_urlsafe(24)
        self.held = {"key": self.key}
        self.calls: list[dict[str, Any]] = []
        self.refused = 0
        self._lock = threading.Lock()
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_: Any) -> None:
                pass

            def _send(self, status: int, payload: Any) -> None:
                raw = json.dumps(payload).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def do_GET(self) -> None:  # noqa: N802
                self._send(405, {"message": "POST a JSON-RPC request to /mcp"})

            def do_POST(self) -> None:  # noqa: N802
                size = int(self.headers.get("Content-Length") or 0)
                try:
                    body = json.loads(self.rfile.read(size) or b"{}")
                except ValueError:
                    body = {}
                if self.headers.get("Authorization", "") != f"Bearer {fake.key}":
                    with fake._lock:
                        fake.refused += 1
                    self._send(401, {"error": "a bearer key is required"})
                    return
                self._send(200, fake.answer(body if isinstance(body, dict) else {}))

        host = bind or os.environ.get("BOSS_JOURNEY_FAKE_BIND") or "127.0.0.1"
        self._server = ThreadingHTTPServer((host, 0), Handler)
        port = self._server.server_address[1]
        shown = advertise or os.environ.get("BOSS_JOURNEY_FAKE_HOST") or "127.0.0.1"
        self.url = f"http://{shown}:{port}/mcp"
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()

    def answer(self, body: dict[str, Any]) -> dict[str, Any]:
        rid, rpc = body.get("id"), str(body.get("method") or "")
        params = body.get("params") if isinstance(body.get("params"), dict) else {}
        if rpc == "initialize":
            result: Any = {"protocolVersion": "2024-11-05", "capabilities": {"tools": {}},
                           "serverInfo": {"name": "journey-ledger", "version": "1"}}
        elif rpc in ("notifications/initialized", "ping"):
            result = {}
        elif rpc == "tools/list":
            result = {"tools": [LEDGER_TOOL]}
        elif rpc == "tools/call" and params.get("name") == LEDGER_TOOL["name"]:
            args = params.get("arguments") if isinstance(params.get("arguments"), dict) else {}
            with self._lock:
                self.calls.append({"tool": LEDGER_TOOL["name"], "arguments": args,
                                   "authorized": True, "at": time.time()})
                n = len(self.calls)
            result = {"content": [{"type": "text", "text": json.dumps(
                {"account": args.get("account"), "balance": "1250.00", "call": n})}]}
        else:
            return {"jsonrpc": "2.0", "id": rid,
                    "error": {"code": -32601, "message": f"no method {rpc!r}"}}
        return {"jsonrpc": "2.0", "id": rid, "result": result}

    def log(self) -> dict[str, Any]:
        with self._lock:
            return {"calls": list(self.calls), "refused": self.refused}

    def stop(self) -> None:
        self._server.shutdown()
        self._server.server_close()


FAKES = {"mcp_http": FakeMcpHttp}


def start(name: str) -> Any:
    """Start the fake a journey names (definitions: needs.fakes)."""
    if name not in FAKES:
        raise KeyError(f"no fake named {name!r}; known: {sorted(FAKES)}")
    return FAKES[name]()
