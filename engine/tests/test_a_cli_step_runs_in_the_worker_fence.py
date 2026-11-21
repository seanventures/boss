"""BOSSAPP-1138: a CLI step runs WITHOUT a microVM, in the worker's own per-run fence.

`skill.sandboxed_command` refused on every `runtime: local` install, so the onboarding workflow
could not run `gwlint validate tenant.yaml`. The `local_fenced` provider runs the command as a
leased pool uid in its own directory behind the entrypoint's iptables fence (sandbox_identity) —
and says, on the run and in `strong_isolation()`, that this is NOT strong isolation.

Everywhere (macOS dev included), with the stand-in helper the F1 boundary tests use: the command
runs as `boss-sandbox-exec run <uid>` from its own workdir, a file input is visible to it, the
exit code comes back, its output is capped, the agent allow-list environment points everything
that is not loopback at the egress relay and withholds the control plane's secrets, the directory
is gone afterwards. The real iptables fence is proved on Linux by
test_identity_f1_sandbox_boundary (A51), whose rules this provider runs under unchanged.
"""
from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

import pytest

from boss_engine.services import a2a_server, local_fenced, sandbox_identity, skill_executors
from boss_engine.services.local_fenced import LocalFenceUnavailable, LocalFencedSandbox
from boss_engine.services.skill_executors import (
    NON_STRONG_PROVIDERS,
    STRONG_ISOLATION_PROVIDERS,
    _isolation_available,
    isolation_grade,
)

LOOPBACK = "127.0.0.1"


@pytest.fixture()
def fence(tmp_path, monkeypatch):
    """A stand-in helper (logs its argv, runs the command as this uid) and short roots; the
    network fence itself is Linux-only, so `BOSS_SANDBOX_NETWORK=off` says so here."""
    short = Path(tempfile.mkdtemp(prefix="sbx", dir="/tmp"))
    run_root, shim_root, log = short / "runs", short / "shim", short / "helper.log"
    run_root.mkdir()
    shim_root.mkdir()
    helper = short / "boss-sandbox-exec"
    helper.write_text("#!/bin/sh\n"
                      f"echo \"$@\" >> {log}\n"
                      "if [ \"$1\" = run ]; then shift 3; exec \"$@\"; fi\n"
                      "exit 0\n")
    helper.chmod(0o755)
    monkeypatch.setenv(sandbox_identity.HELPER_ENV, str(helper))
    monkeypatch.setenv(sandbox_identity.RUN_ROOT_ENV, str(run_root))
    monkeypatch.setenv(sandbox_identity.MODE_ENV, "required")
    # The iptables fence is Linux-only (proved by test_identity_f1_sandbox_boundary's A51 run):
    # here the entrypoint's "fenced" marker is stood in for, and BOSS_SANDBOX_NETWORK stays
    # `enforce` — `off` is refused by local_fenced.
    monkeypatch.setenv(sandbox_identity.NETWORK_ENV, "enforce")
    monkeypatch.setattr(sandbox_identity, "network_fenced", lambda: True)
    # The stand-in helper runs the command as THIS uid, so a produced file's owner is ours.
    monkeypatch.setattr(LocalFencedSandbox, "_expected_owner", lambda self: os.getuid())
    monkeypatch.setenv(sandbox_identity.EGRESS_CONNECT_ENV, "http://egress:8091")
    monkeypatch.setenv("DATABASE_URL", "postgresql://control-plane-secret")
    monkeypatch.setattr(sandbox_identity, "SHIM_ROOT", str(shim_root))
    yield {"log": log, "run_root": run_root}
    shutil.rmtree(short, ignore_errors=True)


def _settings(**over):
    base = {"governed_sandbox_provider": "local_fenced", "sandbox_agent_url": "",
            "deployment_trust": "trusted"}
    base.update(over)
    return SimpleNamespace(**base)


# ── The provider is accepted, and labelled for what it is ───────────────────────────────────

def test_local_fenced_is_opt_in_and_not_strong():
    from boss_engine.config import Settings

    assert Settings.model_fields["governed_sandbox_provider"].default == "local", \
        "local_fenced is opt-in (PR #119 review decision)"
    assert "local_fenced" in NON_STRONG_PROVIDERS and "local_fenced" not in STRONG_ISOLATION_PROVIDERS
    assert STRONG_ISOLATION_PROVIDERS == {"microvm", "firecracker", "kata", "sandbox_host"}


def test_strong_isolation_stays_false_whatever_the_provider_says(fence, monkeypatch):
    """AP-D11: a pool uid behind iptables is not a separate kernel. `local_fenced` being ready
    changes nothing about whether a write-capable agent may be exposed."""
    grade = isolation_grade(_settings())
    assert grade["ready"] is True and grade["strong"] is False and grade["provider"] == "local_fenced"
    assert a2a_server.strong_isolation() is False
    assert "local_fenced" in (a2a_server.strong_isolation.__doc__ or "")


def test_plain_local_still_refuses_and_names_the_fenced_provider():
    ready, why = _isolation_available(_settings(governed_sandbox_provider="local"))
    assert ready is False and "control plane" in why and "local_fenced" in why


def test_it_is_refused_on_a_deployment_marked_untrusted(fence):
    ready, why = _isolation_available(_settings(deployment_trust="untrusted"))
    assert ready is False and "untrusted" in why and "microVM" in why
    with pytest.raises(LocalFenceUnavailable, match="untrusted"):
        LocalFencedSandbox(_settings(deployment_trust="untrusted"), task_id="t-untrusted")


def test_it_is_refused_when_the_network_fence_is_switched_off(fence, monkeypatch):
    """network_fenced() answers True for BOSS_SANDBOX_NETWORK=off; a skill's command must not."""
    monkeypatch.setenv(sandbox_identity.NETWORK_ENV, "off")
    ready, why = _isolation_available(_settings())
    assert ready is False and "BOSS_SANDBOX_NETWORK=off" in why
    with pytest.raises(LocalFenceUnavailable, match="BOSS_SANDBOX_NETWORK"):
        LocalFencedSandbox(_settings(), task_id="t-netoff")


def test_a_skill_gets_no_model_credentials(fence, monkeypatch, tmp_path):
    """No copied Claude/Codex/Grok login in its HOME, no model or OAuth variable in its env."""
    grok = tmp_path / "grok"
    grok.mkdir()
    (grok / "auth.json").write_text('{"token": "operator-grok-login"}')
    monkeypatch.setenv("GROK_HOME", str(grok))
    for name, value in (("CLAUDE_CODE_OAUTH_TOKEN", "sk-ant-oat-operator"),
                        ("ANTHROPIC_API_KEY", "sk-ant-key"), ("OPENAI_API_KEY", "sk-openai"),
                        ("CODEX_HOME", str(tmp_path / "codex")), ("XAI_API_KEY", "xai-key"),
                        ("GEMINI_API_KEY", "g-key")):
        monkeypatch.setenv(name, value)
    seeded: list = []
    monkeypatch.setattr(sandbox_identity, "_seed_cli_logins", lambda ident: seeded.append(ident))
    sbx = LocalFencedSandbox(_settings(), task_id="t-creds")
    try:
        result = sbx.run("env; ls -A \"$HOME\"", timeout=30)
        home = sbx.identity.home
        assert sbx.identity.credential_free is True
    finally:
        sbx.teardown()
    assert seeded == [], "no CLI login is copied into a skill's HOME"
    names = {line.split("=", 1)[0] for line in result.stdout.splitlines() if "=" in line}
    # owned_process's process-tree tag (its own random id, not a credential) is the one
    # TOKEN-named variable every owned process carries.
    names.discard("BOSS_OWNER_TOKENS")
    for marker in ("CLAUDE", "ANTHROPIC", "OPENAI", "CODEX", "GROK", "XAI", "GEMINI", "OAUTH", "TOKEN"):
        assert not [n for n in names if marker in n.upper()], (marker, sorted(names))
    assert "operator-grok-login" not in result.stdout and "sk-ant" not in result.stdout
    assert not {".claude", ".codex", ".grok"} & set(result.stdout.split()), home


def test_it_is_refused_when_the_fence_is_off_or_missing(fence, monkeypatch):
    monkeypatch.setenv(sandbox_identity.MODE_ENV, "off")
    ready, why = _isolation_available(_settings())
    assert ready is False and "switched off" in why
    monkeypatch.setenv(sandbox_identity.MODE_ENV, "required")
    monkeypatch.setenv(sandbox_identity.HELPER_ENV, "/nonexistent/boss-sandbox-exec")
    ready, why = _isolation_available(_settings())
    assert ready is False and "boss-sandbox-exec" in why
    with pytest.raises(LocalFenceUnavailable):
        LocalFencedSandbox(_settings(), task_id="t-nohelper")


# ── A command runs in the fence ──────────────────────────────────────────────────────────────

def test_a_command_runs_as_the_leased_uid_from_its_own_workdir_and_sees_its_file(fence):
    sbx = LocalFencedSandbox(_settings(), task_id="t-run",
                             files={"tenant_yaml": "tenant: lumen-pay\ntier: standard\n"})
    try:
        uid = sbx.identity.uid
        assert sandbox_identity.UID_BASE <= uid < sandbox_identity.UID_BASE + sandbox_identity.UID_COUNT
        assert Path(sbx.inputs["tenant_yaml"]).read_text().startswith("tenant: lumen-pay")
        assert Path(sbx.inputs["tenant_yaml"]).parent == sbx.workdir
        result = sbx.run(f"pwd; cat {sbx.inputs['tenant_yaml']}; "
                         f"{sys.executable} -c 'import os,json; print(json.dumps({{k: os.environ.get(k) "
                         f"for k in (\"HTTPS_PROXY\",\"NO_PROXY\",\"DATABASE_URL\",\"HOME\")}}))'; exit 3",
                         timeout=30)
        lines = result.stdout.strip().splitlines()
        assert result.exit_code == 3 and not result.timed_out
        assert Path(lines[0]).resolve() == sbx.workdir.resolve(), "cwd is the per-run workdir"
        assert lines[1] == "tenant: lumen-pay" and lines[2] == "tier: standard"
        env = json.loads(lines[3])
        # The fence's shape: everything that is not loopback goes through the egress relay; the
        # control plane's DATABASE_URL is withheld; HOME is inside the run directory.
        assert env["HTTPS_PROXY"] == "http://egress:8091"
        assert LOOPBACK in env["NO_PROXY"] and "localhost" in env["NO_PROXY"]
        assert env["DATABASE_URL"] is None
        assert env["HOME"] == str(sbx.identity.home)
        run_dir = sbx.identity.run_dir
    finally:
        sbx.teardown()
    log = fence["log"].read_text().splitlines()
    runs = [line for line in log if line.startswith("run ")]
    assert runs and all(r.startswith(f"run {uid} -- /bin/sh -c") for r in runs)
    assert f"kill {uid}" in log and not run_dir.exists(), "nothing of the run outlives it"


def test_output_is_capped_and_a_timeout_is_an_exit_code(fence, monkeypatch):
    monkeypatch.setattr(local_fenced, "OUTPUT_CAP", 2000)
    sbx = LocalFencedSandbox(_settings(), task_id="t-cap")
    try:
        result = sbx.run("yes abcdefghij | head -c 50000", timeout=30)
        assert result.exit_code == 0 and result.truncated is True
        assert len(result.stdout.encode()) <= 2000
        slow = sbx.run("sleep 5", timeout=1)
        assert slow.timed_out is True and slow.exit_code == 124 and "timed out" in slow.stderr
    finally:
        sbx.teardown()


def test_a_file_name_cannot_leave_the_workdir(fence):
    for bad in ("../etc/passwd", "/etc/passwd", "a/../../b", ""):  # noqa: B007
        with pytest.raises(ValueError):
            local_fenced.safe_relative_path(bad)
    assert local_fenced.safe_relative_path("tenants/lumen-pay.yaml") == "tenants/lumen-pay.yaml"
    with pytest.raises(ValueError):
        LocalFencedSandbox(_settings(), task_id="t-bad", files={"../x": "y"})
    assert not any(p.name.startswith("skill-") for p in fence["run_root"].glob("*/*")), \
        "a refused sandbox leaves no directory behind"


def test_a_symlinked_output_cannot_exfiltrate_the_workers_files(fence, monkeypatch, tmp_path):
    """The review's exploit: `ln -s /proc/self/environ tenant.yaml` made BOSS, reading as its own
    uid, copy the worker's environment into a project artifact. Every link shape is refused and
    NOTHING is stored."""
    from boss_engine.services import skill_definitions

    secret = tmp_path / "worker-secret.env"
    secret.write_text("DATABASE_URL=postgresql://control-plane\n")
    script = (f"ln -s /proc/self/environ tenant.yaml; ln -s {secret} copy.yaml; "
              f"ln {secret} hard.yaml 2>/dev/null || true; ln -s {tmp_path} up; mkfifo pipe.yaml; "
              "echo ok")
    sbx = LocalFencedSandbox(_settings(), task_id="t-symlink")
    try:
        assert sbx.run(script, timeout=30).exit_code == 0
        found = sbx.produced_files([{"name": n, "path": p} for n, p in (
            ("environ", "tenant.yaml"), ("copy", "copy.yaml"), ("hard", "hard.yaml"),
            ("through_dir", "up/worker-secret.env"), ("fifo", "pipe.yaml"))])
    finally:
        sbx.teardown()
    assert found == {}, f"a link was followed: {sorted(found)}"

    skill = {"skill_key": "gwlint.fix", "enabled": True, "inputs": [],
             "config": {"image": "ghcr.io/n/gwlint@sha256:" + "0" * 64,
                        "command": "ln -s /proc/self/environ tenant.yaml",
                        "timeout_seconds": 30, "outputs": [{"name": "fixed", "path": "tenant.yaml"}]}}
    monkeypatch.setattr(skill_definitions, "get_skill", lambda c, p, k: dict(skill))
    monkeypatch.setattr(skill_executors, "_as_held", lambda context, s: s)
    stored: list = []
    import boss_engine.services.core as core
    monkeypatch.setattr(core, "create_artifact", lambda *a, **kw: stored.append(a) or "art_x")
    out = skill_executors.execute_sandboxed_command(
        SimpleNamespace(settings=_settings(), connection=None, project_id="p", created_by_job_id=None),
        {"_skill_key": "gwlint.fix"})
    assert out["produced_artifacts"] == {} and stored == [], "nothing is stored"


def test_an_output_another_user_owns_is_not_kept(fence, monkeypatch):
    monkeypatch.setattr(LocalFencedSandbox, "_expected_owner", lambda self: os.getuid() + 1)
    sbx = LocalFencedSandbox(_settings(), task_id="t-owner")
    try:
        sbx.run("echo x > a.yaml", timeout=30)
        assert sbx.produced_files([{"name": "a", "path": "a.yaml"}]) == {}
    finally:
        sbx.teardown()


def test_produced_outputs_are_kept_as_artifacts_for_the_next_step(fence):
    """The fenced directory dies with the step; a deploy_config step needs the exact bytes."""
    sbx = LocalFencedSandbox(_settings(), task_id="t-prod")
    try:
        sbx.run("printf 'tenant: lumen-pay\\n' > fixed.yaml; mkdir -p out; printf '\\xff\\x00' > out/bin", timeout=30)
        found = sbx.produced_files([{"name": "fixed", "path": "fixed.yaml"},
                                    {"name": "blob", "path": "out/bin"}, {"name": "missing"}])
    finally:
        sbx.teardown()
    assert set(found) == {"fixed", "blob"}
    assert found["fixed"]["bytes"] == b"tenant: lumen-pay\n" and found["fixed"]["path"] == "fixed.yaml"
    assert len(found["fixed"]["sha256"]) == 64 and found["blob"]["bytes"] == b"\xff\x00"

    stored: list[dict] = []

    def fake_create_artifact(connection, settings, project_id, task_id, artifact_type, title, content, **kw):
        stored.append({"type": artifact_type, "title": title, "content": content})
        return f"art_{len(stored)}"

    import boss_engine.services.core as core
    original = core.create_artifact
    core.create_artifact = fake_create_artifact
    try:
        ids = local_fenced.keep_produced(None, None, project_id="p", skill_key="gwlint.fix", produced=found)
    finally:
        core.create_artifact = original
    assert ids == {"fixed": "art_1", "blob": "art_2"}
    assert stored[0]["type"] == "produced_file" and stored[0]["content"]["text"] == "tenant: lumen-pay\n"
    assert "base64" in stored[1]["content"] and stored[1]["content"]["sha256"] == found["blob"]["sha256"]


# ── The executor, end to end on the fenced path ──────────────────────────────────────────────

def test_the_executor_runs_a_declared_skill_in_the_fence_and_labels_the_run(fence, monkeypatch):
    from boss_engine.services import skill_definitions

    skill = {
        "skill_key": "gwlint.validate", "enabled": True,
        "inputs": [{"name": "tenant_yaml", "required": True}],
        "config": {"image": "ghcr.io/northgate/gwlint@sha256:" + "0" * 64,
                   "command": "cat {tenant_yaml} > report.txt; grep -q lumen {tenant_yaml}",
                   "timeout_seconds": 30, "outputs": [{"name": "report", "path": "report.txt"}]},
    }
    monkeypatch.setattr(skill_definitions, "get_skill", lambda c, p, k: dict(skill))
    monkeypatch.setattr(skill_executors, "_as_held", lambda context, s: s)
    kept: list = []
    monkeypatch.setattr(local_fenced, "keep_produced",
                        lambda *a, **kw: kept.append(kw["produced"]) or {"report": "art_report"})
    context = SimpleNamespace(settings=_settings(), connection=None, project_id="p_onboarding",
                              created_by_job_id=None)
    out = skill_executors.execute_sandboxed_command(
        context, {"_skill_key": "gwlint.validate", "files": {"tenant_yaml": "tenant: lumen-pay\n"}})
    # The declared result shape, unchanged, plus what this provider adds.
    assert out["exit_code"] == 0 and out["produced"] == {"report": "report.txt"}
    assert "log_tail" in out
    assert out["isolation"] == {"provider": "local_fenced", "strong": False}
    assert out["produced_artifacts"] == {"report": "art_report"}
    assert kept[0]["report"]["bytes"] == b"tenant: lumen-pay\n"

    # A failing command is an exit code, not an exception.
    out = skill_executors.execute_sandboxed_command(
        context, {"_skill_key": "gwlint.validate", "files": {"tenant_yaml": "tenant: other\n"}})
    assert out["exit_code"] == 1

    # Untrusted deployment: refused before anything runs, in words.
    context.settings = _settings(deployment_trust="untrusted")
    with pytest.raises(skill_executors.SandboxIsolationUnavailable, match="untrusted"):
        skill_executors.execute_sandboxed_command(
            context, {"_skill_key": "gwlint.validate", "files": {"tenant_yaml": "x"}})


# ── BOSSAPP-1170: the pinned image is not what runs; declared local CLIs are ──────

def _gwlint_tree(tmp_path):
    """A stand-in CLI tree, as the read-only /opt/northgate/gwlint mount would hold it."""
    root = tmp_path / "opt" / "northgate"
    bin_dir = root / "gwlint" / "bin"
    bin_dir.mkdir(parents=True)
    cli = bin_dir / "gwlint"
    cli.write_text("#!/bin/sh\necho \"gwlint ok $*\"\n")
    cli.chmod(0o755)
    return root, root / "gwlint"


def _run_gwlint(monkeypatch, settings, declared, command="gwlint validate *.yaml"):
    from boss_engine.services import skill_definitions

    skill = {"skill_key": "gwlint_validate", "enabled": True, "inputs": [],
             "config": {"image": "ghcr.io/northgate/gwlint@sha256:" + "0" * 64,
                        "command": command, "timeout_seconds": 30,
                        "outputs": [], **({"local_binaries": declared} if declared else {})}}
    monkeypatch.setattr(skill_definitions, "get_skill", lambda c, p, k: dict(skill))
    monkeypatch.setattr(skill_executors, "_as_held", lambda context, s: s)
    monkeypatch.setattr(local_fenced, "keep_produced", lambda *a, **kw: {})
    context = SimpleNamespace(settings=settings, connection=None, project_id="p_onboarding",
                              created_by_job_id=None)
    return skill_executors.execute_sandboxed_command(
        context, {"_skill_key": "gwlint_validate", "files": {"lumen-pay.yaml": "id: lumen\n"}})


def test_a_declared_cli_under_an_allowed_root_runs_and_is_recorded(fence, monkeypatch, tmp_path):
    root, tree = _gwlint_tree(tmp_path)
    out = _run_gwlint(monkeypatch, _settings(local_fenced_binaries=str(root)), [str(tree)])
    assert out["exit_code"] == 0 and "gwlint ok validate lumen-pay.yaml" in out["log_tail"]
    assert out["image_used"] is False
    [recorded] = out["local_binaries"]
    assert recorded["path"] == str(tree) and recorded["kind"] == "directory"
    assert len(recorded["sha256"]) == 64, "a directory is hashed over every file"
    cli = tree / "bin" / "gwlint"
    out = _run_gwlint(monkeypatch, _settings(local_fenced_binaries=str(root)), [str(cli)])
    import hashlib
    assert out["local_binaries"][0]["sha256"] == hashlib.sha256(cli.read_bytes()).hexdigest()


@pytest.mark.parametrize("allowed,why", [("", "BOSS_LOCAL_FENCED_BINARIES"),
                                         ("/somewhere/else", "not under a root")])
def test_a_cli_outside_the_allow_list_is_refused_before_anything_runs(
        fence, monkeypatch, tmp_path, allowed, why):
    _, tree = _gwlint_tree(tmp_path)
    with pytest.raises(skill_executors.SandboxIsolationUnavailable, match=why):
        _run_gwlint(monkeypatch, _settings(local_fenced_binaries=allowed), [str(tree)])


def test_a_missing_or_world_writable_cli_is_refused(fence, monkeypatch, tmp_path):
    root, tree = _gwlint_tree(tmp_path)
    with pytest.raises(skill_executors.SandboxIsolationUnavailable, match="does not exist"):
        _run_gwlint(monkeypatch, _settings(local_fenced_binaries=str(root)),
                    [str(root / "missing")])
    (tree / "bin" / "gwlint").chmod(0o777)
    with pytest.raises(skill_executors.SandboxIsolationUnavailable, match="writable"):
        _run_gwlint(monkeypatch, _settings(local_fenced_binaries=str(root)),
                    [str(tree / "bin" / "gwlint")])
    (tree / "bin" / "gwlint").chmod(0o755)
    # A symlink cannot smuggle a path out of the allowed root.
    outside = tmp_path / "outside"
    outside.mkdir()
    (root / "escape").symlink_to(outside)
    with pytest.raises(skill_executors.SandboxIsolationUnavailable, match="not under a root"):
        _run_gwlint(monkeypatch, _settings(local_fenced_binaries=str(root)),
                    [str(root / "escape")])


def test_local_binaries_are_validated_when_the_skill_is_authored():
    from boss_engine.services import skill_definitions as sd

    image = "ghcr.io/northgate/gwlint@sha256:" + "a" * 64
    cfg = sd.validate_config("sandboxed_command", {"image": image, "command": "gwlint x",
                                                   "local_binaries": ["/opt/northgate/gwlint/"]},
                             [])
    assert cfg["local_binaries"] == ["/opt/northgate/gwlint"]
    assert "local_binaries" not in sd.validate_config(
        "sandboxed_command", {"image": image, "command": "x"}, [])
    for bad in (["relative/gwlint"], ["/opt/../etc"], ["/"], ["/x"] * 17):
        with pytest.raises(sd.SkillDefinitionError, match="local_binaries"):
            sd.validate_config("sandboxed_command",
                               {"image": image, "command": "x", "local_binaries": bad}, [])


def test_nothing_a_sandbox_user_could_change_stands_in_the_way(fence, monkeypatch, tmp_path):
    """Review of #125: not only the file's own write bit. A file deep in the tree, a parent
    directory, or anything owned by a pool uid would let one run swap the tool another trusts."""
    root, tree = _gwlint_tree(tmp_path)
    settings = _settings(local_fenced_binaries=str(root))
    (tree / "lib").mkdir()
    (tree / "lib" / "rules.js").write_text("module.exports = {}\n")
    (tree / "lib" / "rules.js").chmod(0o664)
    with pytest.raises(skill_executors.SandboxIsolationUnavailable, match="group"):
        _run_gwlint(monkeypatch, settings, [str(tree)])
    (tree / "lib" / "rules.js").chmod(0o644)
    (tree / "bin").chmod(0o777)
    with pytest.raises(skill_executors.SandboxIsolationUnavailable, match="every user"):
        _run_gwlint(monkeypatch, settings, [str(tree / "bin" / "gwlint")])
    (tree / "bin").chmod(0o755)
    monkeypatch.setattr(local_fenced, "_is_pool_uid", lambda uid: uid == os.getuid())
    with pytest.raises(skill_executors.SandboxIsolationUnavailable, match="sandbox user"):
        _run_gwlint(monkeypatch, settings, [str(tree)])


def test_a_cli_changed_while_it_ran_is_refused(fence, monkeypatch, tmp_path):
    root, tree = _gwlint_tree(tmp_path)
    cli = tree / "bin" / "gwlint"
    with pytest.raises(skill_executors.SandboxIsolationUnavailable, match="changed while"):
        _run_gwlint(monkeypatch, _settings(local_fenced_binaries=str(root)), [str(tree)],
                    command=f"echo '# swapped' >> {cli}")


def test_symlinks_must_stay_in_the_tree_and_only_hashed_dirs_go_on_path(fence, monkeypatch,
                                                                       tmp_path):
    root, tree = _gwlint_tree(tmp_path)
    settings = _settings(local_fenced_binaries=str(root))
    # npm's .bin links stay inside the tree: allowed, and their targets are hashed.
    (tree / "node_modules" / ".bin").mkdir(parents=True)
    (tree / "node_modules" / ".bin" / "gw").symlink_to(tree / "bin" / "gwlint")
    assert _run_gwlint(monkeypatch, settings, [str(tree)])["exit_code"] == 0
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    (outside / "evil").write_text("#!/bin/sh\necho evil\n")
    (tree / "node_modules" / ".bin" / "evil").symlink_to(outside / "evil")
    with pytest.raises(skill_executors.SandboxIsolationUnavailable, match="links outside"):
        _run_gwlint(monkeypatch, settings, [str(tree)])
    (tree / "node_modules" / ".bin" / "evil").unlink()
    # A declared FILE's directory is not put on PATH: only the file itself was checked.
    (tree / "bin" / "other").write_text("#!/bin/sh\necho other\n")
    (tree / "bin" / "other").chmod(0o755)
    out = _run_gwlint(monkeypatch, settings, [str(tree / "bin" / "gwlint")],
                      command="other 2>&1; echo rc=$?")
    assert "rc=127" in out["log_tail"], out["log_tail"]
