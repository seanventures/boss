"""A deterministic model for the end-to-end harness: OpenAI-compatible, and the brain behind
the fake `claude` CLI next to it.

WHY THIS EXISTS. Nothing in BOSS could run a workflow without a real model, so "every template
runs end to end" (R5) was a claim nobody could check without spending money, and the only runs
anyone had looked at were on boss-1, where a research step sat for seventeen hours retrying a
401 before going green as `failed_continued`. A harness that cannot reach the model path the
product really uses proves nothing, so this is a SERVER the real `OpenAIAdapter` and the real
`ClaudeCodeAgent` talk to over their own protocols — not a monkeypatch inside the process.

How a reply is chosen: the rules in `model_script.json` are tried in order and the first whose
`when` markers ALL appear in the prompt wins. A prompt no rule matches gets the `default` rule
and is flagged `unmatched` in the log, so an operator reading the report can see exactly which
prompt needs a script instead of guessing why a step produced nonsense.

Endpoints:
  POST /v1/chat/completions   OpenAI chat (tools ignored; content is the scripted reply)
  POST /v1/responses          OpenAI Responses API
  POST /v1/embeddings         deterministic vectors
  GET  /v1/models             one model, "e2e-stub"
  POST /_cli                  the fake `claude` CLI asks here: {prompt, argv, cwd} -> {result, files}
  GET  /_log                  every request, with the rule that answered it
  POST /_override             prepend rules for one test (e.g. make routing return garbage)
  POST /_reset                drop overrides and the log
  GET  /health

Stdlib only, so it starts on any machine the harness runs on, including boss-1's host.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent))
import model_probes  # noqa: E402  (Repo and models' key modes: sk-stub-401/402/404/429)
from typing import Any, Optional

HERE = Path(__file__).resolve().parent
DEFAULT_SCRIPT = HERE / "model_script.json"
MODEL_NAME = "e2e-stub"


class Script:
    """Ordered rules plus per-test overrides. Thread-safe: the server is threaded."""

    def __init__(self, path: Path = DEFAULT_SCRIPT):
        self.path = path
        self._lock = threading.Lock()
        self._overrides: list[dict[str, Any]] = []
        self.log: list[dict[str, Any]] = []
        self.reload()

    def reload(self) -> None:
        data = json.loads(self.path.read_text(encoding="utf-8"))
        rules = data.get("rules") or []
        for rule in rules:
            validate_rule(rule)
        default = data.get("default")
        if not isinstance(default, dict):
            raise ValueError(f"{self.path}: a 'default' rule is required")
        with self._lock:
            self.rules = rules
            self.default = default

    def override(self, rules: list[dict[str, Any]]) -> None:
        for rule in rules:
            validate_rule(rule)
        with self._lock:
            self._overrides = list(rules) + self._overrides

    def reset(self) -> None:
        with self._lock:
            self._overrides = []
            self.log = []

    def choose(self, prompt: str, channel: str) -> tuple[dict[str, Any], bool]:
        """(rule, matched). `channel` is 'chat' or 'cli'."""
        with self._lock:
            candidates = list(self._overrides) + list(self.rules)
        for rule in candidates:
            if rule_matches(rule, prompt, channel):
                return rule, True
        return self.default, False

    def record(self, entry: dict[str, Any]) -> None:
        with self._lock:
            self.log.append(entry)


def validate_rule(rule: dict[str, Any]) -> None:
    if not isinstance(rule, dict) or not rule.get("id"):
        raise ValueError(f"every rule needs an id: {rule!r}")
    when = rule.get("when")
    if not isinstance(when, list) or not when or not all(isinstance(w, str) and w for w in when):
        raise ValueError(f"rule {rule['id']}: 'when' must be a non-empty list of markers")
    if rule.get("channel", "any") not in ("any", "chat", "cli"):
        raise ValueError(f"rule {rule['id']}: channel must be any, chat or cli")
    if "reply" not in rule:
        raise ValueError(f"rule {rule['id']}: a reply is required")


def rule_matches(rule: dict[str, Any], prompt: str, channel: str) -> bool:
    wanted = rule.get("channel", "any")
    if wanted != "any" and wanted != channel:
        return False
    for marker in rule["when"]:
        if marker.startswith("re:"):
            if not re.search(marker[3:], prompt, re.IGNORECASE | re.DOTALL):
                return False
        elif marker.lower() not in prompt.lower():
            return False
    return True


def render_reply(rule: dict[str, Any], prompt: str) -> str:
    """The reply as text. A JSON reply is serialised; `{{title}}`-style holes are filled from
    the prompt so a scripted PRD names the work it was asked about, which is what lets a test
    tell one run's output from another's."""
    reply = rule["reply"]
    text = reply if isinstance(reply, str) else json.dumps(reply)
    title = _guess_title(prompt)
    text = text.replace("{{title}}", title).replace("{{prompt_sha}}", _sha(prompt)[:12])
    # `captures`: {"name": "regex with one group"} — a hole filled from the prompt itself, so a
    # routing rule can answer with an agent that is really among the candidates it was shown.
    for name, pattern in (rule.get("captures") or {}).items():
        found = re.findall(pattern, prompt, re.DOTALL)
        text = text.replace("{{" + name + "}}", found[0] if found else "")
    return text


def _guess_title(prompt: str) -> str:
    for pattern in (r"TASK:\s*(.+)", r"[Tt]itle:\s*(.+)", r"[Dd]irective:\s*(.+)"):
        found = re.search(pattern, prompt)
        if found:
            return found.group(1).strip()[:120]
    return "the requested work"


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8", "ignore")).hexdigest()


def prompt_of_messages(messages: Any) -> str:
    parts: list[str] = []
    for message in messages or []:
        if not isinstance(message, dict):
            continue
        content = message.get("content")
        if isinstance(content, str):
            parts.append(content)
        elif isinstance(content, list):
            for block in content:
                if isinstance(block, dict) and isinstance(block.get("text"), str):
                    parts.append(block["text"])
    return "\n".join(parts)


def prompt_of_responses_input(body: dict[str, Any]) -> str:
    parts = [str(body.get("instructions") or "")]
    value = body.get("input")
    if isinstance(value, str):
        parts.append(value)
    elif isinstance(value, list):
        for item in value:
            if isinstance(item, dict):
                content = item.get("content")
                if isinstance(content, str):
                    parts.append(content)
                elif isinstance(content, list):
                    for block in content:
                        if isinstance(block, dict) and isinstance(block.get("text"), str):
                            parts.append(block["text"])
    return "\n".join(p for p in parts if p)


def tool_calls_for(rule: dict[str, Any], body: dict[str, Any]) -> list[dict[str, Any]]:
    """The native code agent's half of a `files` rule.

    BOSS's native lane (code_agent.NativeCodeAgent) edits through OpenAI function calls —
    write_file, edit_file, bash — in a loop that ends when a turn returns no calls. So a rule
    that writes files answers the FIRST turn with one write_file call per file and, once the
    tool results come back, the scripted text. The same rule drives the `claude` CLI lane,
    which writes the files itself; both lanes then produce the same diff.
    """
    offered = {str(((t or {}).get("function") or {}).get("name") or "")
               for t in body.get("tools") or [] if isinstance(t, dict)}
    messages = body.get("messages") or []
    if isinstance(rule.get("turns"), list):
        return _scripted_turn(rule, messages, offered)
    files = rule.get("files") if isinstance(rule.get("files"), dict) else {}
    commands = rule.get("commands") if isinstance(rule.get("commands"), list) else []
    if not (files or commands):
        return []
    if any(isinstance(m, dict) and m.get("role") == "tool" for m in messages):
        return []  # the tools already ran this session; now say what was done
    calls: list[dict[str, Any]] = []
    if "write_file" in offered:
        for path, content in files.items():
            calls.append({"id": f"call_{uuid.uuid4().hex[:10]}", "type": "function",
                          "function": {"name": "write_file",
                                       "arguments": json.dumps({"path": path,
                                                                "content": str(content)})}})
    if "bash" in offered:
        for command in commands:
            calls.append({"id": f"call_{uuid.uuid4().hex[:10]}", "type": "function",
                          "function": {"name": "bash",
                                       "arguments": json.dumps({"command": str(command)})}})
    return calls


def _scripted_turn(rule: dict[str, Any], messages: list[Any],
                   offered: set[str]) -> list[dict[str, Any]]:
    """An AGENT's session, one tool call per turn: `turns` = [{"call": [{"tool", "arguments"}]}].

    The turn is how many assistant turns with tool calls the conversation already holds, so a
    retried request gets the same answer. `tool` matches an offered tool name exactly or by
    suffix (skills are offered as `skill_<slug>`). Argument strings take `{{name}}` holes filled
    by the rule's `captures`, searched across the WHOLE conversation (and so does `tool`) — which is how turn two uses
    the artifact id turn one's tool returned. Past the last turn, the rule's `reply` ends it.
    """
    done = sum(1 for m in messages if isinstance(m, dict) and m.get("role") == "assistant"
               and m.get("tool_calls"))
    turns = rule["turns"]
    if done >= len(turns):
        return []
    conversation = "\n".join(
        m.get("content") if isinstance(m.get("content"), str) else json.dumps(m.get("content"))
        for m in messages if isinstance(m, dict))
    holes: dict[str, str] = {}
    for name, pattern in (rule.get("captures") or {}).items():
        found = re.findall(pattern, conversation, re.DOTALL)
        holes[name] = found[-1] if found else ""
    calls: list[dict[str, Any]] = []
    for call in turns[done].get("call") or []:
        # The tool NAME takes holes too: a skill made during the run (the R2 journey's
        # `e2e_skill_<stamp>`, offered as `skill_e2e_skill_<stamp>`) cannot be named in a script
        # written before it existed, so the rule captures it from the instruction instead.
        wanted = _fill(str(call.get("tool") or ""), holes)
        name = next((t for t in sorted(offered) if t == wanted or t.endswith(wanted)), "")
        if not name:
            continue  # not offered: the session ends with the reply, which says so in the log
        arguments = json.loads(_fill(json.dumps(call.get("arguments") or {}), holes))
        calls.append({"id": f"call_{uuid.uuid4().hex[:10]}", "type": "function",
                      "function": {"name": name, "arguments": json.dumps(arguments)}})
    return calls


def _fill(text: str, holes: dict[str, str]) -> str:
    for name, value in holes.items():
        text = text.replace("{{" + name + "}}", json.dumps(value)[1:-1])
    return text


def embedding_for(text: str, dims: int = 64) -> list[float]:
    digest = hashlib.sha256(text.encode("utf-8", "ignore")).digest()
    return [((digest[i % len(digest)] / 255.0) * 2 - 1) for i in range(dims)]


def _usage(prompt: str, reply: str) -> dict[str, int]:
    p, c = max(1, len(prompt) // 4), max(1, len(reply) // 4)
    return {"prompt_tokens": p, "completion_tokens": c, "total_tokens": p + c}


class Handler(BaseHTTPRequestHandler):
    script: Script  # set on the class by make_server
    server_version = "boss-e2e-model-stub/1"

    def log_message(self, fmt: str, *args: Any) -> None:  # quiet: the /_log is the record
        return

    # ------------------------------------------------------------------ plumbing
    def _body(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b""
        try:
            value = json.loads(raw or b"{}")
        except json.JSONDecodeError:
            return {}
        return value if isinstance(value, dict) else {}

    def _send(self, status: int, payload: Any) -> None:
        data = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _answer(self, prompt: str, channel: str, endpoint: str,
                extra: Optional[dict[str, Any]] = None) -> tuple[dict[str, Any], str]:
        rule, matched = self.script.choose(prompt, channel)
        reply = render_reply(rule, prompt)
        self.script.record({
            "at": time.time(), "endpoint": endpoint, "channel": channel,
            "rule": rule.get("id"), "matched": matched,
            "prompt_head": prompt[:600], "prompt_sha": _sha(prompt)[:12],
            "prompt_chars": len(prompt), **(extra or {}),
            # The whole prompt when nothing was scripted for it: that is the one an operator
            # has to read to write the missing rule.
            **({} if matched else {"prompt": prompt[:20000]}),
        })
        return rule, reply

    # ------------------------------------------------------------------ routes
    def do_GET(self) -> None:  # noqa: N802 — http.server's naming
        path = self.path.split("?", 1)[0]
        if path.endswith("/models") and self._key_mode("GET", path):
            return
        if path in ("/health", "/v1/health"):
            self._send(200, {"ok": True})
        elif path == "/v1/models":
            self._send(200, {"object": "list", "data": [
                {"id": MODEL_NAME, "object": "model", "owned_by": "boss-e2e"}]})
        elif path == "/_log":
            self._send(200, {"entries": list(self.script.log)})
        else:
            self._send(404, {"error": {"message": f"no route {path}"}})

    def _key_mode(self, method: str, path: str) -> bool:
        """Repo and models' key modes (model_probes.py): sk-stub-401/402/404/429 answer as a
        provider would. True when one answered."""
        key = (self.headers.get("Authorization") or "").removeprefix("Bearer ").strip() \
            or (self.headers.get("x-api-key") or "")
        found = model_probes.answer(key, method, path)
        if found is None or key == "sk-stub-ok" and not path.endswith("/models"):
            return False
        status, headers, payload = found
        data = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        for name, value in headers.items():
            self.send_header(name, value)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)
        return True

    def do_POST(self) -> None:  # noqa: N802
        path = self.path.split("?", 1)[0]
        if path.endswith("/chat/completions") and self._key_mode("POST", path):
            return
        body = self._body()
        if path.endswith("/chat/completions"):
            self._chat(body)
        elif path.endswith("/responses"):
            self._responses(body)
        elif path.endswith("/embeddings"):
            inputs = body.get("input")
            items = inputs if isinstance(inputs, list) else [inputs]
            self._send(200, {"object": "list", "model": MODEL_NAME, "data": [
                {"object": "embedding", "index": i, "embedding": embedding_for(str(t))}
                for i, t in enumerate(items)], "usage": {"prompt_tokens": 1, "total_tokens": 1}})
        elif path == "/_cli":
            self._cli(body)
        elif path == "/_override":
            try:
                self.script.override(list(body.get("rules") or []))
            except ValueError as exc:
                self._send(400, {"error": str(exc)})
                return
            self._send(200, {"ok": True})
        elif path == "/_reset":
            self.script.reset()
            self._send(200, {"ok": True})
        else:
            self._send(404, {"error": {"message": f"no route {path}"}})

    def _chat(self, body: dict[str, Any]) -> None:
        prompt = prompt_of_messages(body.get("messages"))
        rule, reply = self._answer(prompt, "chat", "chat.completions",
                                   {"model": body.get("model")})
        if rule.get("http_status"):
            self._send(int(rule["http_status"]), {"error": {"message": reply}})
            return
        usage = _usage(prompt, reply)
        message: dict[str, Any] = {"role": "assistant", "content": reply}
        finish = "stop"
        calls = tool_calls_for(rule, body)
        if calls:
            message, finish = {"role": "assistant", "content": None, "tool_calls": calls}, "tool_calls"
        completion = {
            "id": f"chatcmpl-{uuid.uuid4().hex[:16]}", "object": "chat.completion",
            "created": int(time.time()), "model": body.get("model") or MODEL_NAME,
            "choices": [{"index": 0, "finish_reason": finish, "message": message}],
            "usage": usage,
        }
        if body.get("stream"):
            self._stream_chat(completion, reply)
            return
        self._send(200, completion)

    def _stream_chat(self, completion: dict[str, Any], reply: str) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.end_headers()
        chunk = {"id": completion["id"], "object": "chat.completion.chunk",
                 "created": completion["created"], "model": completion["model"],
                 "choices": [{"index": 0, "delta": {"role": "assistant", "content": reply},
                              "finish_reason": None}]}
        done = {**chunk, "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
                "usage": completion["usage"]}
        for event in (chunk, done):
            self.wfile.write(f"data: {json.dumps(event)}\n\n".encode("utf-8"))
        self.wfile.write(b"data: [DONE]\n\n")

    def _responses(self, body: dict[str, Any]) -> None:
        prompt = prompt_of_responses_input(body)
        rule, reply = self._answer(prompt, "chat", "responses", {"model": body.get("model")})
        if rule.get("http_status"):
            self._send(int(rule["http_status"]), {"error": {"message": reply}})
            return
        usage = _usage(prompt, reply)
        self._send(200, {
            "id": f"resp_{uuid.uuid4().hex[:16]}", "object": "response",
            "created_at": int(time.time()), "model": body.get("model") or MODEL_NAME,
            "status": "completed", "output_text": reply,
            "output": [{"type": "message", "id": f"msg_{uuid.uuid4().hex[:12]}",
                        "role": "assistant", "status": "completed",
                        "content": [{"type": "output_text", "text": reply,
                                     "annotations": []}]}],
            "usage": {"input_tokens": usage["prompt_tokens"],
                      "output_tokens": usage["completion_tokens"],
                      "total_tokens": usage["total_tokens"]},
        })

    def _cli(self, body: dict[str, Any]) -> None:
        prompt = str(body.get("prompt") or "")
        rule, reply = self._answer(prompt, "cli", "cli", {
            "argv": [a for a in (body.get("argv") or []) if len(str(a)) < 200][:24],
            "cwd": body.get("cwd"),
        })
        files = rule.get("files") if isinstance(rule.get("files"), dict) else {}
        self._send(200, {
            "result": reply, "files": files, "rule": rule.get("id"),
            "exit_code": int(rule.get("exit_code") or 0),
            "stderr": str(rule.get("stderr") or ""),
        })


def make_server(host: str, port: int, script_path: Path = DEFAULT_SCRIPT) -> ThreadingHTTPServer:
    handler = type("BoundHandler", (Handler,), {"script": Script(script_path)})
    server = ThreadingHTTPServer((host, port), handler)
    server.daemon_threads = True
    return server


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=0)
    parser.add_argument("--script", type=Path, default=DEFAULT_SCRIPT)
    args = parser.parse_args()
    server = make_server(args.host, args.port, args.script)
    print(f"model-stub listening on http://{args.host}:{server.server_address[1]}", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
