from __future__ import annotations

import json
from typing import Any, Optional

import httpx

from ..config import Settings


class MCPClient:
    """Minimal synchronous MCP client (JSON-RPC 2.0 over HTTP).

    Speaks the MCP methods needed to use a server as a governed tool backend:
    initialize, tools/list, tools/call. Designed so a richer transport (the
    official MCP SDK / stdio / SSE) can back it later without changing callers.
    """

    def __init__(self, settings: Settings, timeout: float = 20.0):
        self.settings = settings
        self.timeout = timeout

    def _rpc(self, url: str, method: str, params: Optional[dict] = None, rid: int = 1,
             headers: Optional[dict] = None) -> Any:
        resp = httpx.post(
            url,
            json={"jsonrpc": "2.0", "id": rid, "method": method, "params": params or {}},
            headers={"Accept": "application/json", **(headers or {})},
            timeout=self.timeout,
        )
        resp.raise_for_status()
        data = resp.json()
        if isinstance(data, dict) and data.get("error"):
            raise RuntimeError(data["error"].get("message") or "MCP error")
        return data.get("result") if isinstance(data, dict) else None

    def list_tools(self, url: str, headers: Optional[dict] = None) -> list[dict]:
        # Best-effort handshake; some servers require it before tools/list.
        try:
            self._rpc(
                url,
                "initialize",
                {
                    "protocolVersion": "2024-11-05",
                    "capabilities": {},
                    "clientInfo": {"name": "boss", "version": "0.1.0"},
                },
                headers=headers,
            )
        except Exception:
            pass
        result = self._rpc(url, "tools/list", headers=headers) or {}
        return result.get("tools", []) if isinstance(result, dict) else []

    def call_tool(self, url: str, name: str, args: dict,
                  headers: Optional[dict] = None) -> Any:
        result = self._rpc(url, "tools/call", {"name": name, "arguments": args or {}},
                           headers=headers) or {}
        content = result.get("content") if isinstance(result, dict) else None
        texts = [
            c.get("text", "")
            for c in (content or [])
            if isinstance(c, dict) and c.get("type") == "text"
        ]
        text = "\n".join(texts) if texts else json.dumps(result)
        try:
            return json.loads(text)
        except Exception:
            return {"text": text}
