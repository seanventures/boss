"""A previewed app that tries to reach what it must not (tests/e2e/preview_sandbox). Run by BOSS as
a preview: it probes, prints one JSON line of results, then serves HTTP on $PORT.

argv: <api pid> <db host:port> <victim port> <victim dir> <api address>
"""
import http.server
import json
import os
import socket
import sys
import threading
import urllib.request

api_pid, db, victim_port, victim_dir, api_addr = sys.argv[1:6]
PORT = int(os.environ["PORT"])


def read(path):
    try:
        with open(path, "rb") as fh:
            fh.read(64)
        return "READ"
    except Exception as exc:  # noqa: BLE001
        return type(exc).__name__


def listdir(path):
    try:
        os.listdir(path)
        return "LISTED"
    except Exception as exc:  # noqa: BLE001
        return type(exc).__name__


def connect(host, port):
    try:
        with socket.create_connection((host, int(port)), timeout=3):
            return "open"
    except Exception as exc:  # noqa: BLE001
        return type(exc).__name__


identity_files = {}
for name in ("master.key", "server.key", ""):
    identity_files[name or "<dir>"] = (read(f"/var/lib/boss/identity/{name}") if name
                                       else listdir("/var/lib/boss/identity"))

server = http.server.ThreadingHTTPServer(("0.0.0.0", PORT), type("H", (http.server.BaseHTTPRequestHandler,), {
    "do_GET": lambda self: (self.send_response(200), self.end_headers(), self.wfile.write(b"evil app")),
    "log_message": lambda self, *a: None}))
threading.Thread(target=server.serve_forever, daemon=True).start()

db_host, db_port = db.rsplit(":", 1)
result = {
    "uid": os.getuid(), "gid": os.getgid(), "groups": os.getgroups(),
    "identity": identity_files,
    "api_environ": read(f"/proc/{api_pid}/environ"),
    "pid1_environ": read("/proc/1/environ"),
    "api_mem": read(f"/proc/{api_pid}/mem"),
    "victim_dir": listdir(victim_dir),
    "sandboxes_root": listdir("/var/lib/boss/sandboxes"),
    "env_has_secrets": sorted(k for k in os.environ if any(
        s in k for s in ("DATABASE", "SECRET", "TOKEN", "PASSWORD", "MASTER", "SERVER_KEY", "ANTHROPIC", "OPENAI"))),
    "db": connect(db_host, db_port),
    "victim_loopback": connect("127.0.0.1", victim_port),
    "victim_api_addr": connect(api_addr, victim_port),
    "own_port_loopback": connect("127.0.0.1", PORT),
    "egress_proxy": connect("egress", 8091),
}
print(json.dumps({"probe": result}), flush=True)
threading.Event().wait()
