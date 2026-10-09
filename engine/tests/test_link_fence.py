"""The sandbox fence's F5 rules (scripts/sandbox/entrypoint.sh), checked as the chain they build.

The fence is shell run as root at container start (the API drops NET_ADMIN for good, so no rule is
ever added at runtime). This renders the chain the script builds for a given environment and
checks what F5 needs of it: the link gateway's ports are reachable on the container networks,
previews are refused on EVERY address (loopback included) BEFORE loopback is accepted, and
nothing else changed. The real kernel walk is the compose end-to-end (tests/e2e/links).
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "sandbox" / "entrypoint.sh"


def _chain(env: dict[str, str]) -> list[str]:
    """Run fence_network with iptables/ip6tables stubbed to print their arguments."""
    src = SCRIPT.read_text()
    fence = src[src.index("fence_network() {"):src.index("\n}\n", src.index("fence_network() {")) + 3]
    head = "\n".join(l for l in src.splitlines() if re.match(r"^(UID_RANGE|PORTS|LINK_PORTS|PREVIEW_PORTS)=", l))
    stub = ("iptables() { echo \"iptables $*\"; }\nip6tables() { return 1; }\n"
            "command() { return 0; }\n"
            # the container's networks, as the script's /proc/net/route reader would print them
            "python3() { cat >/dev/null; echo 172.18.0.0/16; echo 10.9.0.0/24; }\n")
    script = f"set -eu\n{stub}{head}\n{fence}\nfence_network\n"
    out = subprocess.run(["sh", "-c", script], capture_output=True, text=True, env={**os.environ, **env},
                         check=True)
    return [l for l in out.stdout.splitlines() if l.startswith("iptables -A BOSS_SANDBOX")]


@pytest.mark.skipif(shutil.which("sh") is None, reason="needs a POSIX shell")
def test_previews_are_refused_before_loopback_and_link_ports_are_allowed():
    rules = _chain({})
    preview = rules.index("iptables -A BOSS_SANDBOX -p tcp --dport 19000:19999 -j REJECT")
    loopback = rules.index("iptables -A BOSS_SANDBOX -o lo -d 127.0.0.0/8 -j ACCEPT")
    assert preview < loopback, "a build could reach a preview on loopback, around the link gateway"
    assert rules[-1] == "iptables -A BOSS_SANDBOX -j REJECT"
    nets = [r for r in rules if "--dports" in r]
    assert nets == [f"iptables -A BOSS_SANDBOX -d {n} -p tcp -m multiport --dports 8000,8090,8091,18100:18199 -j ACCEPT"
                    for n in ("172.18.0.0/16", "10.9.0.0/24")]


def test_the_ranges_follow_the_settings():
    rules = _chain({"BOSS_LINK_PORTS": "18200-18210", "BOSS_PREVIEW_PORTS": "20000-20500"})
    assert "iptables -A BOSS_SANDBOX -p tcp --dport 20000:20500 -j REJECT" in rules
    nets = [r for r in rules if "--dports" in r]
    assert nets and all(r.endswith("--dports 8000,8090,8091,18200:18210 -j ACCEPT") for r in nets)


def test_the_defaults_match_the_engine():
    from boss_engine.app_links import service

    src = SCRIPT.read_text()
    assert f'BOSS_LINK_PORTS:-{service.LINK_PORTS_DEFAULT}' in src
    assert f'BOSS_PREVIEW_PORTS:-{service.PREVIEW_PORTS_DEFAULT}' in src
