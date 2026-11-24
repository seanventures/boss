"""The image build's CLIENT goes through BOSS_UPSTREAM_PROXY (enterprise lane, BOSSAPP-1430).

BuildKit fetches registry tokens (the `# syntax=` frontend, base images) through the docker
CLIENT's session. On a box whose only way out is the company proxy, the daemon's own proxy
settings were not enough: the install stopped at "failed to fetch anonymous token ... connection
refused". The serial build block now gives the build command HTTPS_PROXY and NO_PROXY from .env,
after sudo when there is one (sudo resets the environment).
"""
from __future__ import annotations

import json
import os
import shlex
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def _block(name: str) -> str:
    source = (ROOT / "install.sh").read_text()
    return source[source.index(f"# ---- {name} "):source.index(f"# ---- end {name} ")]


def _run(tmp_path: Path, env_lines: str, compose: str = "fake") -> list[dict]:
    fake = tmp_path / "fake"
    fake.write_text("""#!/usr/bin/env python3
import json, os, sys
if "config" in sys.argv:
    print(json.dumps({"services": {"api": {"build": {}}}}))
    sys.exit(0)
with open(os.environ["TRACE"], "a") as f:
    f.write(json.dumps({"argv": sys.argv[1:], "https": os.environ.get("HTTPS_PROXY", ""),
                        "no": os.environ.get("NO_PROXY", "")}) + "\\n")
""")
    fake.chmod(0o700)
    sudo = tmp_path / "sudo"     # a sudo that resets the environment, as the real one does
    sudo.write_text('#!/bin/sh\nexec env -u HTTPS_PROXY -u NO_PROXY "$@"\n')
    sudo.chmod(0o700)
    envf = tmp_path / ".env"
    envf.write_text(env_lines)
    compose_cmd = shlex.quote(str(fake)) if compose == "fake" else f"sudo {shlex.quote(str(fake))}"
    script = tmp_path / "build.sh"
    script.write_text(
        "set -euo pipefail\nui_info() { :; }\ndie() { echo \"$*\" >&2; exit 1; }\n"
        f'PATH={shlex.quote(str(tmp_path))}:"$PATH"\n'
        f"ENV_FILE={shlex.quote(str(envf))}\n"
        'env_value() { grep -m 1 "^$1=" "$ENV_FILE" | cut -d= -f2-; }\n'
        f"COMPOSE=({compose_cmd}); COMPOSE_PROFILES=''\n"
        "BOSS_SOURCE_SHA=unknown BOSS_BUILT_AT=unknown\n"
        + _block("serial image builds"))
    trace = tmp_path / "trace"
    env = {k: v for k, v in os.environ.items() if k.upper() not in ("HTTPS_PROXY", "NO_PROXY")}
    done = subprocess.run(["bash", str(script)], text=True, capture_output=True, timeout=20,
                          cwd=ROOT, env={**env, "TRACE": str(trace)})
    assert done.returncode == 0, done.stdout + done.stderr
    return [json.loads(line) for line in trace.read_text().splitlines()]


def test_a_configured_proxy_reaches_the_build_client(tmp_path):
    rows = _run(tmp_path, "BOSS_UPSTREAM_PROXY=http://proxy.example.com:8080\n"
                          "BOSS_UPSTREAM_NO_PROXY=.example.com\n")
    assert rows and all(r["https"] == "http://proxy.example.com:8080" for r in rows)
    assert all(r["no"] == "localhost,127.0.0.1,.example.com" for r in rows)
    assert rows[0]["argv"][-1] == "api"


def test_it_survives_sudo(tmp_path):
    rows = _run(tmp_path, "BOSS_UPSTREAM_PROXY=http://proxy.example.com:8080\n"
                          "BOSS_UPSTREAM_NO_PROXY=\n", compose="sudo")
    assert rows and rows[0]["https"] == "http://proxy.example.com:8080"
    assert rows[0]["no"] == "localhost,127.0.0.1"


def test_no_proxy_configured_changes_nothing(tmp_path):
    rows = _run(tmp_path, "BOSS_UPSTREAM_PROXY=\n")
    assert rows and rows[0]["https"] == "" and rows[0]["no"] == ""
