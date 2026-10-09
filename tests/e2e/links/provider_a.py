"""App A for the F5 proof: a tiny API with a login. It seeds BOSS_TEST_USERS (as the banking
template does when not in production) and serves on $PORT. Standard library only."""
import json
import os
import secrets
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

USERS = {u["username"]: u for u in json.loads(os.environ.get("BOSS_TEST_USERS") or "[]")}
TOKENS: dict[str, str] = {}


class H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        return

    def _reply(self, status, obj):
        body = json.dumps(obj).encode()
        self.send_response(status)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path == "/health":
            return self._reply(200, {"ok": True, "seeded": len(USERS)})
        if self.path == "/me":
            who = TOKENS.get((self.headers.get("authorization") or "").removeprefix("Bearer "))
            return self._reply(200 if who else 401, {"user": who, "role": USERS.get(who, {}).get("role")})
        return self._reply(404, {})

    def do_POST(self):
        data = json.loads(self.rfile.read(int(self.headers.get("content-length") or 0)) or b"{}")
        if self.path == "/login":
            u = USERS.get(data.get("username"))
            if not u or u["password"] != data.get("password"):
                return self._reply(401, {"error": "bad login"})
            tok = secrets.token_hex(16)
            TOKENS[tok] = u["username"]
            return self._reply(200, {"token": tok})
        return self._reply(404, {})


ThreadingHTTPServer(("0.0.0.0", int(os.environ["PORT"])), H).serve_forever()
