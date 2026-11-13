"""A scripted Agent Client Protocol agent (BOSSAPP-853 conformance fake). No model.

Speaks ACP v1 over stdio as Gemini CLI / OpenCode would, enough to exercise BOSS's client:

    initialize      -> protocolVersion FAKE_ACP_VERSION (default 1); loadSession: true;
                       mcpCapabilities.http unless FAKE_ACP_NO_HTTP=1
    session/new     -> records params to FAKE_ACP_RECORD (JSON lines); sessionId "sess-1"
    session/load    -> replays one agent message (must NOT reach BOSS's summary)
    session/prompt  -> a read tool call (completed), then an edit that asks permission;
                       allowed: writes hello.txt through fs/write_text_file and reads it back
                       through fs/read_text_file; rejected: says so. Then, if BOSS sent
                       session/cancel, answers stopReason "cancelled", else "end_turn".
                       FAKE_ACP_SCENARIO=execute asks permission for an `execute` call instead;
                       FAKE_ACP_SCENARIO=auth answers session/new with auth_required;
                       FAKE_ACP_SCENARIO=escape tries to write outside the tree.
"""
import json
import os
import sys

RECORD = os.environ.get("FAKE_ACP_RECORD", "")
SCENARIO = os.environ.get("FAKE_ACP_SCENARIO", "edit")
_next = [1000]
_pending = []          # messages read while waiting for a specific response
cancelled = [False]


def send(message):
    sys.stdout.write(json.dumps(message) + "\n")
    sys.stdout.flush()


def record(kind, params):
    if RECORD:
        with open(RECORD, "a") as handle:
            handle.write(json.dumps({"kind": kind, "params": params}) + "\n")


def read():
    if _pending:
        return _pending.pop(0)
    line = sys.stdin.readline()
    if not line:
        sys.exit(0)
    return json.loads(line)


def ask(method, params):
    """A request to the client; returns its response, queueing anything else that arrives."""
    rid = _next[0]
    _next[0] += 1
    send({"jsonrpc": "2.0", "id": rid, "method": method, "params": params})
    while True:
        line = sys.stdin.readline()
        if not line:
            sys.exit(0)
        message = json.loads(line)
        if message.get("id") == rid and "method" not in message:
            return message
        if message.get("method") == "session/cancel":
            cancelled[0] = True
            continue
        _pending.append(message)


def update(session, body):
    send({"jsonrpc": "2.0", "method": "session/update",
          "params": {"sessionId": session, "update": body}})


def prompt_turn(rid, params):
    session = params["sessionId"]
    cwd = os.getcwd()
    update(session, {"sessionUpdate": "tool_call", "toolCallId": "t1", "title": "Read README",
                     "kind": "read", "status": "completed",
                     "locations": [{"path": os.path.join(cwd, "README.md")}],
                     "rawInput": {"path": "README.md"},
                     "content": [{"type": "content", "content": {"type": "text",
                                                                 "text": "readme"}}]})
    if SCENARIO == "execute":
        call = {"toolCallId": "t2", "title": "Run tests", "kind": "execute",
                "status": "pending", "rawInput": {"command": "pytest -q"}}
    else:
        target = "/etc/boss-escape.txt" if SCENARIO == "escape" else os.path.join(cwd,
                                                                                  "hello.txt")
        call = {"toolCallId": "t2", "title": "Write hello.txt", "kind": "edit",
                "status": "pending", "locations": [{"path": target}],
                "rawInput": {"path": target, "content": "hello"}}
    update(session, {"sessionUpdate": "tool_call", **call})
    answer = ask("session/request_permission", {
        "sessionId": session, "toolCall": {"toolCallId": "t2"},
        "options": [{"optionId": "yes-always", "name": "Always", "kind": "allow_always"},
                    {"optionId": "yes", "name": "Allow", "kind": "allow_once"},
                    {"optionId": "no", "name": "Reject", "kind": "reject_once"}]})
    outcome = (answer.get("result") or {}).get("outcome") or {}
    allowed = outcome.get("outcome") == "selected" and outcome.get("optionId") == "yes"
    record("permission", {"outcome": outcome})
    if allowed and call["kind"] == "edit":
        wrote = ask("fs/write_text_file", {"sessionId": session, "path": call["rawInput"]["path"],
                                           "content": "hello"})
        record("write", wrote)
        back = ask("fs/read_text_file", {"sessionId": session, "path": "hello.txt"})
        record("read", back)
    update(session, {"sessionUpdate": "tool_call_update", "toolCallId": "t2",
                     "status": "completed" if allowed else "failed",
                     "content": [{"type": "content", "content": {
                         "type": "text", "text": "ok" if allowed else "refused"}}]})
    update(session, {"sessionUpdate": "agent_message_chunk",
                     "content": {"type": "text",
                                 "text": "Done." if allowed else "The change was refused."}})
    stop = "cancelled" if cancelled[0] else "end_turn"
    send({"jsonrpc": "2.0", "id": rid, "result": {"stopReason": stop}})


def main():
    while True:
        message = read()
        method, rid, params = message.get("method"), message.get("id"), message.get("params")
        if method == "initialize":
            record("initialize", params)
            send({"jsonrpc": "2.0", "id": rid, "result": {
                "protocolVersion": int(os.environ.get("FAKE_ACP_VERSION", "1")),
                "agentCapabilities": {"loadSession": True,
                                      "mcpCapabilities": {"http": os.environ.get(
                                          "FAKE_ACP_NO_HTTP") != "1", "sse": False}},
                "authMethods": []}})
        elif method == "session/new":
            record("session/new", params)
            if SCENARIO == "auth":
                send({"jsonrpc": "2.0", "id": rid,
                      "error": {"code": -32000, "message": "Authentication required"}})
                continue
            send({"jsonrpc": "2.0", "id": rid, "result": {"sessionId": "sess-1"}})
        elif method == "session/load":
            record("session/load", params)
            update(params["sessionId"], {"sessionUpdate": "agent_message_chunk",
                                         "content": {"type": "text", "text": "REPLAYED "}})
            send({"jsonrpc": "2.0", "id": rid, "result": None})
        elif method == "session/prompt":
            prompt_turn(rid, params)
        elif method == "session/cancel":
            cancelled[0] = True
        elif rid is not None:
            send({"jsonrpc": "2.0", "id": rid,
                  "error": {"code": -32601, "message": f"no {method}"}})


if __name__ == "__main__":
    main()
