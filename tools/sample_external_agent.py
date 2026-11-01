"""KG P6b sample outside agent: the reference "bank's agent" HTTP server.

Stdlib-only, as any bank implementing `docs/community/OUTSIDE_AGENTS.md` would run it: answers
`{"ping": true}` with 2xx always; on a real step call, reads evidence via BOSS's KG MCP with its
own `kg.token` (`kg_find`/`kg_get_node`), asks a model over an OpenAI-chat-completions-compatible
endpoint configured by its own env vars, then writes the contract's declared output back through
the same token (`kg_write_agent_decision`/`kg_write_coverage_gap`). With no model configured it
refuses plainly rather than inventing a verdict — the documented safe default.
"""
from __future__ import annotations

import json
import os
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


def _post_json(url: str, payload: dict, *, headers: dict) -> dict:
    data = json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(
        url, data=data, method="POST",
        # A NAMED CLIENT. Some model endpoints' edge protection refuses Python's default
        # "Python-urllib" agent with 403 (Groq did, 2026-10-31).
        headers={"Content-Type": "application/json", "User-Agent": "boss-sample-bank-agent/1",
                 **headers})
    with urllib.request.urlopen(request, timeout=30) as response:
        if response.status >= 300:
            raise RuntimeError(f"{url} answered {response.status}")
        return json.loads(response.read().decode("utf-8") or "{}")


def _ask_model(base_url: str, api_key: str, input_data: dict, contract: dict,
               evidence: list | None = None) -> str:
    body = {
        "model": os.environ.get("MODEL_NAME", "gpt-4o-mini"),
        "messages": [
            {"role": "system", "content": "You are the bank's own regulatory compliance reviewer. "
                                           "BOSS's agents have assessed a regulation against "
                                           "the bank's code; their decisions and the proposed "
                                           "remediation are in the evidence. Judge them as the "
                                           "bank: say whether you agree with BOSS's finding and "
                                           "remediation and why, in two sentences, citing the "
                                           "clause."},
            {"role": "user", "content": json.dumps({"input": input_data, "contract": contract,
                                                    "evidence": evidence or []})},
        ],
    }
    result = _post_json(f"{base_url.rstrip('/')}/chat/completions", body,
                         headers={"Authorization": f"Bearer {api_key}"})
    choices = result.get("choices") or []
    if not choices:
        raise RuntimeError("model answered no choices")
    statement = str((choices[0].get("message") or {}).get("content") or "").strip()
    if not statement:
        raise RuntimeError("model answered an empty statement")
    return statement


def _mcp_call(mcp_url: str, token: str, name: str, arguments: dict) -> dict:
    result = _post_json(mcp_url, {
        "jsonrpc": "2.0", "id": 1, "method": "tools/call",
        "params": {"name": name, "arguments": arguments},
    }, headers={"Authorization": f"Bearer {token}"})
    if "error" in result:
        raise RuntimeError(f"{name} refused: {result['error']}")
    content = (result.get("result") or {}).get("content") or []
    if content and content[0].get("isError"):
        raise RuntimeError(f"{name} refused: {content[0].get('text')}")
    return result.get("result") or {}


#: How many nodes of one kind the agent takes into its judgement: a project has many rules.
MAX_PER_KIND = 8


def _node_text(node: dict) -> str:
    """What a node says: its body, else the text, statement or change brief its attrs carry
    (a clause's text and a decision's statement live in attrs, not the body)."""
    attrs = node.get("attrs") or {}
    for value in (node.get("body"), attrs.get("text"), attrs.get("statement"),
                  attrs.get("change_brief")):
        if value:
            return str(value)[:800]
    return ""


def _read_evidence(mcp_url: str, token: str, contract: dict | None, run_id: str = "") -> list:
    """Read what the contract lets this agent read (`reads` kinds) through its own token, so the
    review stands on BOSS's evidence and the run shows what the agent read. This run's own nodes
    first (decisions and packages are keyed by run), then the rest, at most MAX_PER_KIND each."""
    evidence: list = []
    for read in (contract or {}).get("reads") or []:
        kind = str((read or {}).get("kind") or "").strip() if isinstance(read, dict) else ""
        if not kind:
            continue
        result = _mcp_call(mcp_url, token, "kg_find", {"kind": kind})
        nodes: list = []
        for item in result.get("content") or []:
            try:
                nodes.extend(json.loads(item.get("text") or "{}").get("nodes") or [])
            except (TypeError, ValueError):
                continue
        mine = [n for n in nodes if run_id and (run_id in str(n.get("node_key") or "")
                                                 or (n.get("attrs") or {}).get("run_id") == run_id)]
        chosen = mine or nodes
        for node in chosen[:MAX_PER_KIND]:
            text_ = _node_text(node)
            if text_:
                evidence.append({"kind": kind, "name": node.get("name"), "text": text_})
    return evidence


def _apply_contract_writes(mcp_url: str, token: str, contract: dict | None, statement: str) -> None:
    writes = (contract or {}).get("writes") or []
    for write in writes:
        if not isinstance(write, dict):
            continue
        if write.get("kind") == "agent_decision":
            _mcp_call(mcp_url, token, "kg_write_agent_decision", {
                "decision_type": str(write.get("decision_type") or "assessment"),
                "statement": statement,
            })
        else:
            _mcp_call(mcp_url, token, "kg_write_coverage_gap", {"statement": statement})


def _handle_ping() -> tuple[int, dict]:
    return 200, {"pong": True}


def _handle_step(body: dict) -> tuple[int, dict]:
    base_url = os.environ.get("MODEL_BASE_URL")
    if not base_url:
        return 503, {"error": "no model configured"}
    kg = body.get("kg")
    if not isinstance(kg, dict) or not kg.get("mcp_url") or not kg.get("token"):
        return 400, {"error": "kg.mcp_url and kg.token are required"}
    try:
        evidence = _read_evidence(str(kg["mcp_url"]), str(kg["token"]), body.get("contract"),
                                  str(body.get("run_id") or ""))
        statement = _ask_model(
            base_url, os.environ.get("MODEL_API_KEY", ""),
            body.get("input") or {}, body.get("contract") or {}, evidence)
        _apply_contract_writes(str(kg["mcp_url"]), str(kg["token"]), body.get("contract"), statement)
    except Exception as exc:  # noqa: BLE001 — refused plainly, never a fabricated verdict
        return 502, {"error": str(exc)}
    return 200, {"ok": True, "decision": statement, "read": len(evidence)}


class _Handler(BaseHTTPRequestHandler):
    def do_POST(self) -> None:  # noqa: N802 — BaseHTTPRequestHandler's naming
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b""
        try:
            body = json.loads(raw.decode("utf-8")) if raw else {}
            if not isinstance(body, dict):
                raise ValueError("body must be a JSON object")
        except (json.JSONDecodeError, UnicodeDecodeError, ValueError):
            self._reply(400, {"error": "invalid request body"})
            return
        if body.get("ping"):
            self._reply(*_handle_ping())
            return
        self._reply(*_handle_step(body))

    def _reply(self, status: int, payload: dict) -> None:
        data = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *args) -> None:  # noqa: D401 — quiet by default
        pass


def main() -> None:
    host = os.environ.get("HOST", "0.0.0.0")
    port = int(os.environ.get("PORT", "8765"))
    server = ThreadingHTTPServer((host, port), _Handler)
    server.serve_forever()


if __name__ == "__main__":
    main()
