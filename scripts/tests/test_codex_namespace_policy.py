import hashlib
import importlib.util
import json
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location("engine_seccomp", ROOT / "scripts/sandbox/engine_seccomp.py")
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


def test_changed_source_is_rejected():
    with pytest.raises(ValueError, match="checksum"):
        MODULE.profile(b'{}')


def test_extension_preserves_default_deny_and_existing_rules(monkeypatch):
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


# ── vendored, never fetched ─────────────────────────────────────────────────────────────────

def test_the_vendored_moby_profile_is_the_pinned_one():
    """The source is in the repository and matches the pinned upstream commit's hash."""
    data = (ROOT / "scripts/sandbox/vendor/moby-seccomp-default.json").read_bytes()
    assert hashlib.sha256(data).hexdigest() == MODULE.SHA256
    assert (ROOT / "scripts/sandbox/vendor/moby-profiles.LICENSE").read_text().lstrip() \
        .startswith("Apache License")
    notice = (ROOT / "scripts/sandbox/vendor/NOTICE").read_text()
    assert MODULE.SHA256 in notice and MODULE.SOURCE_COMMIT in notice


def test_the_committed_profile_is_what_the_generator_makes():
    assert (ROOT / "scripts/sandbox/engine-seccomp.json").read_text() == MODULE.render()


def test_nothing_fetches_the_profile_at_install_time():
    source = (ROOT / "scripts/sandbox/engine_seccomp.py").read_text()
    assert "urllib" not in source and "http" not in source.replace("https://github.com", "")


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
    policy = (ROOT / "scripts/sandbox/boss-engine.apparmor").read_text()
    assert "userns," in policy
    assert "deny /sys/kernel/security/** rwklx," in policy
    assert "deny @{PROC}/sysrq-trigger rwklx," in policy


# ── AppArmor: docker-default for the container, namespaces only through the engine's helper ──

def _apparmor_parts():
    text = (ROOT / "scripts/sandbox/boss-engine.apparmor").read_text()
    rules = "\n".join(line.split("#", 1)[0] if not line.lstrip().startswith("#include") else line
                      for line in text.splitlines())
    start = rules.index("profile engine_sandbox")
    child = rules[start:rules.rindex("}")]
    return rules[:start], child


def _rule_lines(block):
    return {line.strip() for line in block.splitlines() if line.strip()}


def test_the_container_profile_is_docker_default():
    base, _ = _apparmor_parts()
    lines = _rule_lines(base)
    assert "deny mount," in lines
    assert not lines & {"mount,", "pivot_root,", "userns,"}
    for kept in ("deny /sys/kernel/security/** rwklx,", "deny @{PROC}/sysrq-trigger rwklx,",
                 "deny @{PROC}/kcore rwklx,", "deny /sys/firmware/** rwklx,"):
        assert kept in lines


def test_namespaces_are_granted_only_in_the_helper_child_profile():
    base, child = _apparmor_parts()
    assert _rule_lines(child) >= {"userns,", "mount,", "pivot_root,"}
    assert "deny /sys/kernel/security/** rwklx," in _rule_lines(child)
    transitions = [line.strip() for line in base.splitlines() if "Cx -> engine_sandbox" in line]
    # Exact paths only: a glob would conflict with `file,` (apparmor_parser: "conflicting x
    # modifiers") and widen the transition.
    assert transitions and all("*" not in t for t in transitions)


def test_the_transition_covers_every_codex_platform_package_bundled():
    lock = json.loads((ROOT / "scripts/install/cli-lock/package-lock.json").read_text())
    triples = {"arm64": "aarch64-unknown-linux-musl", "x64": "x86_64-unknown-linux-musl"}
    base, _ = _apparmor_parts()
    for key in lock["packages"]:
        if key.startswith("node_modules/@openai/codex-linux-"):
            arch = key.rsplit("-", 1)[1]
            path = (f"/opt/boss-clis/{key}/vendor/{triples[arch]}/codex-resources/bwrap")
            assert f"{path} Cx -> engine_sandbox," in base


def test_the_loader_drops_userns_on_apparmor_3_instead_of_failing():
    loader = (ROOT / "scripts/sandbox/load_apparmor.sh").read_text()
    assert "grep -v '^[[:space:]]*userns,[[:space:]]*$'" in loader
    assert "features/namespaces/userns_create" in loader      # AA4 kernel + AA3 parser: say so
    assert "AppArmor is not enabled on this host: nothing to load" in loader


@pytest.mark.skipif(not __import__("shutil").which("apparmor_parser"),
                    reason="apparmor_parser is not installed here")
def test_the_profile_compiles(tmp_path):
    import subprocess

    src = ROOT / "scripts/sandbox/boss-engine.apparmor"
    stripped = tmp_path / "boss-engine"
    stripped.write_text("".join(l for l in src.read_text().splitlines(True)
                                if l.strip() != "userns,"))
    full = subprocess.run(["apparmor_parser", "-Q", "-K", str(src)], capture_output=True)
    assert full.returncode == 0 or subprocess.run(
        ["apparmor_parser", "-Q", "-K", str(stripped)], capture_output=True).returncode == 0


# ── the seccomp additions are as narrow as seccomp allows ────────────────────────────────────

def _added():
    base = json.loads((ROOT / "scripts/sandbox/vendor/moby-seccomp-default.json").read_bytes())
    full = json.loads((ROOT / "scripts/sandbox/engine-seccomp.json").read_text())
    assert full["syscalls"][:len(base["syscalls"])] == base["syscalls"]   # Docker's rules unchanged
    assert {k: v for k, v in full.items() if k != "syscalls"} == \
        {k: v for k, v in base.items() if k != "syscalls"}                # default-deny unchanged
    return full["syscalls"][len(base["syscalls"]):]


def test_only_the_namespace_calls_bubblewrap_needs_are_added():
    names = {n for rule in _added() for n in rule["names"]}
    assert names == {"mount", "umount2", "pivot_root", "unshare", "clone"}
    # Codex 0.158's bubblewrap passes neither --unshare-uts nor --hostname.
    assert not names & {"sethostname", "setdomainname", "setns", "clone3", "keyctl", "bpf",
                        "ptrace", "umount"}


def test_unshare_and_clone_need_a_new_user_namespace():
    rules = {tuple(rule["names"]): rule for rule in _added()}
    for name in ("unshare", "clone"):
        (arg,) = rules[(name,)]["args"]
        assert arg == {"index": 0, "value": 0x10000000, "valueTwo": 0x10000000,
                       "op": "SCMP_CMP_MASKED_EQ"}


def test_no_added_rule_grants_or_depends_on_a_capability():
    for rule in _added():
        assert rule["action"] == "SCMP_ACT_ALLOW"
        assert "caps" not in json.dumps(rule.get("includes", {}))
        assert "caps" not in json.dumps(rule.get("excludes", {}))
