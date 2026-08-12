#!/usr/bin/env bash
# layer3-control — ops-agent skeleton + control timer stubs
set -euo pipefail
KIT="${GOLDENEYE_KIT_ROOT:-/opt/goldeneye}"
# shellcheck source=/dev/null
source "$KIT/lib/common.sh"
ge_require_root
ge_step "layer3-control"

RUN=/opt/goldeneye/run/control
mkdir -p "$RUN/ops-agent" /etc/goldeneye/timers

# Minimal ops-agent: token-scoped secret get/set + handshake (stdlib HTTP)
cat >"$RUN/ops-agent/server.py" <<'PY'
#!/usr/bin/env python3
"""Minimal Goldeneye ops-agent (kit skeleton)."""
from __future__ import annotations

import json
import os
import secrets
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

ROOT = Path(os.environ.get("OPS_STATE", "/var/lib/goldeneye/ops"))
ROOT.mkdir(parents=True, exist_ok=True)
TOKEN_FILE = ROOT / "ops-token"
SECRETS = ROOT / "secrets"
SECRETS.mkdir(exist_ok=True)
if not TOKEN_FILE.exists():
    TOKEN_FILE.write_text(secrets.token_urlsafe(32) + "\n")
    TOKEN_FILE.chmod(0o600)
OPS_TOKEN = TOKEN_FILE.read_text().strip()


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):  # quieter
        pass

    def _auth(self) -> bool:
        return self.headers.get("X-Ops-Token", "") == OPS_TOKEN

    def _json(self, code: int, body: dict):
        data = json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        path = urlparse(self.path).path
        if path == "/health":
            return self._json(200, {"ok": True, "service": "ops-agent"})
        if path == "/handshake":
            if not self._auth():
                return self._json(401, {"ok": False, "error": "unauthorized"})
            return self._json(
                200,
                {
                    "ok": True,
                    "kit": "goldeneye",
                    "capabilities": ["secret.get", "secret.set", "handshake"],
                    "version": "0.1.0",
                },
            )
        if path == "/secret":
            if not self._auth():
                return self._json(401, {"ok": False, "error": "unauthorized"})
            qs = parse_qs(urlparse(self.path).query)
            key = (qs.get("key") or [""])[0]
            if not key or "/" in key or ".." in key:
                return self._json(400, {"ok": False, "error": "bad key"})
            fp = SECRETS / key
            if not fp.exists():
                return self._json(404, {"ok": False, "error": "not found"})
            return self._json(200, {"ok": True, "key": key, "value": fp.read_text()})
        return self._json(404, {"ok": False, "error": "not found"})

    def do_POST(self):
        path = urlparse(self.path).path
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b"{}"
        try:
            body = json.loads(raw.decode() or "{}")
        except Exception:
            return self._json(400, {"ok": False, "error": "invalid json"})
        if path == "/secret":
            if not self._auth():
                return self._json(401, {"ok": False, "error": "unauthorized"})
            key = str(body.get("key") or "")
            value = str(body.get("value") or "")
            if not key or "/" in key or ".." in key:
                return self._json(400, {"ok": False, "error": "bad key"})
            fp = SECRETS / key
            fp.write_text(value)
            fp.chmod(0o600)
            return self._json(200, {"ok": True, "key": key})
        return self._json(404, {"ok": False, "error": "not found"})


def main():
    host = os.environ.get("OPS_BIND", "127.0.0.1")
    port = int(os.environ.get("OPS_PORT", "9091"))
    print(f"ops-agent listening on {host}:{port}", flush=True)
    ThreadingHTTPServer((host, port), Handler).serve_forever()


if __name__ == "__main__":
    main()
PY
chmod 0755 "$RUN/ops-agent/server.py"

# systemd unit if systemd is available
if command -v systemctl >/dev/null 2>&1 && [ -d /etc/systemd/system ]; then
  cat >/etc/systemd/system/goldeneye-ops-agent.service <<'UNIT'
[Unit]
Description=Goldeneye ops-agent (kit)
After=network.target

[Service]
Type=simple
Environment=OPS_STATE=/var/lib/goldeneye/ops
Environment=OPS_BIND=127.0.0.1
Environment=OPS_PORT=9091
ExecStart=/usr/bin/python3 /opt/goldeneye/run/control/ops-agent/server.py
Restart=on-failure

[Install]
WantedBy=multi-user.target
UNIT
  systemctl daemon-reload
  systemctl enable --now goldeneye-ops-agent.service || true
  ge_info "ops-agent systemd unit enabled (127.0.0.1:9091)"
else
  ge_info "systemd unavailable — start ops-agent manually: python3 $RUN/ops-agent/server.py"
fi

# Timer stub scripts (operator wires cadence)
cat >/etc/goldeneye/timers/exposure-audit.sh <<'SH'
#!/usr/bin/env bash
# Stub: list docker published ports; extend to fail on unexpected public binds.
docker ps --format '{{.Names}} {{.Ports}}' 2>/dev/null || true
SH
chmod 0755 /etc/goldeneye/timers/exposure-audit.sh

ge_info "layer3-control complete"
if [ -f /var/lib/goldeneye/ops/ops-token ]; then
  ge_info "ops token file: /var/lib/goldeneye/ops/ops-token"
fi
