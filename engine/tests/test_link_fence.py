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


def _clean_env() -> dict[str, str]:
    """This process's environment without the fence's own settings: a test that leaves
    BOSS_PREVIEW_PORTS / BOSS_LINK_PORTS / BOSS_SANDBOX_ALLOWED_PORTS behind must not change what
    the defaults render (each test passes the settings it means)."""
    return {k: v for k, v in os.environ.items()
            if k not in ("BOSS_PREVIEW_PORTS", "BOSS_LINK_PORTS", "BOSS_SANDBOX_ALLOWED_PORTS")}


def _restore_stub_path() -> str:
    """A directory holding an `iptables-restore` that prints its input (a shell function cannot
    carry a dash in its name)."""
    import tempfile

    d = tempfile.mkdtemp(prefix="fence-stub-")
    stub = Path(d) / "iptables-restore"
    stub.write_text("#!/bin/sh\nsed 's/^/restore /'\n")
    stub.chmod(0o755)
    return d


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
    path = f"{_restore_stub_path()}:{os.environ.get('PATH', '')}"
    out = subprocess.run(["sh", "-c", script], capture_output=True, text=True,
                         env={**_clean_env(), "PATH": path, **env}, check=True)
    return [l for l in out.stdout.splitlines() if l.startswith("iptables -A BOSS_SANDBOX ")]


def _restored(env: dict[str, str]) -> list[str]:
    """What fence_network feeds iptables-restore (the BOSS_PREVIEW_SELF chain)."""
    src = SCRIPT.read_text()
    fence = src[src.index("fence_network() {"):src.index("\n}\n", src.index("fence_network() {")) + 3]
    head = "\n".join(l for l in src.splitlines() if re.match(r"^(UID_RANGE|PORTS|LINK_PORTS|PREVIEW_PORTS)=", l))
    stub = ("iptables() { :; }\nip6tables() { return 1; }\ncommand() { return 0; }\n"

            "python3() { cat >/dev/null; echo 172.18.0.0/16; }\n")
    path = f"{_restore_stub_path()}:{os.environ.get('PATH', '')}"
    out = subprocess.run(["sh", "-c", f"set -eu\n{stub}{head}\n{fence}\nfence_network\n"],
                         capture_output=True, text=True, env={**_clean_env(), "PATH": path, **env}, check=True)
    return [l[len("restore "):] for l in out.stdout.splitlines() if l.startswith("restore ")]


@pytest.mark.skipif(shutil.which("sh") is None, reason="needs a POSIX shell")
def test_previews_are_refused_before_loopback_and_link_ports_are_allowed():
    rules = _chain({})
    own = rules.index("iptables -A BOSS_SANDBOX -p tcp --dport 19000:19999 -j BOSS_PREVIEW_SELF")
    preview = rules.index("iptables -A BOSS_SANDBOX -p tcp --dport 19000:19999 -j REJECT")
    loopback = rules.index("iptables -A BOSS_SANDBOX -o lo -d 127.0.0.0/8 -j ACCEPT")
    assert own + 1 == preview < loopback, "a build could reach a preview on loopback, around the link gateway"
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


def test_a_preview_uid_reaches_only_its_own_port_on_loopback():
    """Previews run as pool uids (preview_service): uid 61000+i holds port 19000+i, and only that
    port of the preview range is open to it, only on loopback; every other preview port falls
    through to the REJECT."""
    restored = _restored({})
    assert restored[0] == "*filter" and restored[1] == ":BOSS_PREVIEW_SELF - [0:0]" and restored[-1] == "COMMIT"
    rules = restored[2:-1]
    assert len(rules) == 1000
    assert rules[0] == "-A BOSS_PREVIEW_SELF -o lo -p tcp -m owner --uid-owner 61000 --dport 19000 -j ACCEPT"
    assert rules[-1] == "-A BOSS_PREVIEW_SELF -o lo -p tcp -m owner --uid-owner 61999 --dport 19999 -j ACCEPT"
    assert all(" -o lo " in r and r.endswith(" -j ACCEPT") for r in rules)


def test_a_smaller_preview_range_maps_only_the_uids_it_has_ports_for():
    rules = _restored({"BOSS_PREVIEW_PORTS": "20000-20009"})[2:-1]
    assert len(rules) == 10
    assert rules[-1].endswith("--uid-owner 61009 --dport 20009 -j ACCEPT")


def test_the_engine_maps_uids_to_ports_the_way_the_fence_does(monkeypatch):
    from boss_engine.services import sandbox_identity as si

    monkeypatch.delenv("BOSS_PREVIEW_PORTS", raising=False)
    assert si.preview_port_for_uid(61000) == 19000
    assert si.preview_port_for_uid(61999) == 19999
    monkeypatch.setenv("BOSS_PREVIEW_PORTS", "20000-20009")
    assert si.preview_port_for_uid(61009) == 20009
    assert si.preview_port_for_uid(61010) is None
