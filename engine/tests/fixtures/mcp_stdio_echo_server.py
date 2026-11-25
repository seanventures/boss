"""A tiny, REAL stdio MCP server for the BOSSAPP-850/851 tests. Newline-delimited JSON-RPC on
stdin/stdout, nothing else. Tools:

  echo        {"text"} -> the text back
  whoami      -> the value of ECHO_TOKEN in its environment (what the supervisor handed it)
  stats       -> how many times it was initialized, its pid
  write_note  {"text"} -> pretends to write (a tool the tests mark tier 2)
  die         -> exits without answering (a crash)
  sleep       {"seconds"} -> answers after that long (a slow tool)
  cancelled   -> the request ids this server was told to cancel (notifications/cancelled)
"""
import json
import os
import sys
import time

TOOLS = [
    {"name": "echo", "description": "Echo back the text you send.",
     "inputSchema": {"type": "object", "properties": {"text": {"type": "string"}}}},
    {"name": "whoami", "description": "Say which token this server was started with.",
     "inputSchema": {"type": "object"}},
    {"name": "stats", "description": "Initialize count and process id.",
     "inputSchema": {"type": "object"}},
    {"name": "write_note", "description": "Write a note somewhere.",
     "inputSchema": {"type": "object", "properties": {"text": {"type": "string"}}}},
    {"name": "die", "description": "Exit at once.", "inputSchema": {"type": "object"}},
    {"name": "sleep", "description": "Answer after `seconds`.",
     "inputSchema": {"type": "object", "properties": {"seconds": {"type": "number"}}}},
    {"name": "cancelled", "description": "Ids this server was told to cancel.",
     "inputSchema": {"type": "object"}},
]
cancelled = []

initialized = 0


def text(value):
    return {"content": [{"type": "text", "text": json.dumps(value)}]}


for line in sys.stdin:
    line = line.strip()
    if not line:
        continue
    msg = json.loads(line)
    if "id" not in msg:
        if msg.get("method") == "notifications/cancelled":
            cancelled.append((msg.get("params") or {}).get("requestId"))
        continue
    method = msg.get("method")
    params = msg.get("params") or {}
    if method == "initialize":
        initialized += 1
        result = {"protocolVersion": params.get("protocolVersion", "2025-06-18"),
                  "capabilities": {"tools": {}},
                  "serverInfo": {"name": "echo-fixture", "version": "1.0.0"}}
    elif method == "ping":
        result = {}
    elif method == "tools/list":
        result = {"tools": TOOLS}
    elif method == "tools/call":
        name = params.get("name")
        args = params.get("arguments") or {}
        if name == "echo":
            result = text({"echo": args.get("text")})
        elif name == "whoami":
            result = text({"token": os.environ.get("ECHO_TOKEN"),
                           "greeting": os.environ.get("ECHO_GREETING")})
        elif name == "stats":
            result = text({"initialized": initialized, "pid": os.getpid()})
        elif name == "write_note":
            result = text({"written": args.get("text")})
        elif name == "sleep":
            time.sleep(float(args.get("seconds") or 1))
            result = text({"slept": True})
        elif name == "cancelled":
            result = text({"ids": cancelled})
        elif name == "die":
            sys.exit(3)
        else:
            sys.stdout.write(json.dumps({"jsonrpc": "2.0", "id": msg["id"], "error": {
                "code": -32602, "message": f"unknown tool {name}"}}) + "\n")
            sys.stdout.flush()
            continue
    else:
        sys.stdout.write(json.dumps({"jsonrpc": "2.0", "id": msg["id"], "error": {
            "code": -32601, "message": f"no method {method}"}}) + "\n")
        sys.stdout.flush()
        continue
    sys.stdout.write(json.dumps({"jsonrpc": "2.0", "id": msg["id"], "result": result}) + "\n")
    sys.stdout.flush()
