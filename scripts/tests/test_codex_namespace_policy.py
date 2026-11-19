import importlib.util
import json
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location("codex_seccomp", ROOT / "scripts/sandbox/codex_seccomp.py")
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


def test_changed_source_is_rejected():
    with pytest.raises(ValueError, match="checksum"):
        MODULE.profile(b'{}')


def test_extension_preserves_default_deny_and_existing_rules(monkeypatch):
    import hashlib

    original = {"defaultAction": "SCMP_ACT_ERRNO", "syscalls": [
        {"names": ["read"], "action": "SCMP_ACT_ALLOW"}]}
    source = json.dumps(original).encode()
    monkeypatch.setattr(MODULE, "SHA256", hashlib.sha256(source).hexdigest())
    policy = MODULE.profile(source)
    assert policy["defaultAction"] == "SCMP_ACT_ERRNO"
    assert policy["syscalls"][0] == original["syscalls"][0]
    clone = policy["syscalls"][-1]
    assert clone["names"] == ["clone"]
    assert clone["args"][0]["valueTwo"] == 0x10000000
    unshare = policy["syscalls"][-2]
    assert unshare["args"][0]["value"] & 0x400


def test_override_is_opt_in_and_never_privileged():
    overlay = (ROOT / "scripts/install/community-codex-sandbox.yml").read_text()
    assert "privileged" not in overlay
    assert "unconfined" not in overlay
    assert "SYS_ADMIN" not in overlay
    assert "BOSS_CODEX_SECCOMP_FILE:?" in overlay
    launcher = (ROOT / "scripts/install/boss.sh").read_text()
    assert 'local compose_files=(-f "$COMPOSE_FILE")' in launcher
    assert '"${compose_files[@]}"' in launcher


def test_apparmor_keeps_host_control_paths_denied():
    policy = (ROOT / "scripts/sandbox/boss-codex.apparmor").read_text()
    assert "userns," in policy
    assert "deny /sys/kernel/security/** rwklx," in policy
    assert "deny @{PROC}/sysrq-trigger rwklx," in policy
