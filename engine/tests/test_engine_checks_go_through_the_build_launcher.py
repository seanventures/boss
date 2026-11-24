"""The engine inner-sandbox check starts Codex THE WAY A BUILD DOES: through boss-sandbox-exec.

THE GAP (RCA row 12, BOSSAPP-1433 R5). Codex's sandbox failed under AppArmor on a customer host:
the build launcher (boss-sandbox-exec) sets PR_SET_NO_NEW_PRIVS before it execs, so the exec
transition into bubblewrap was refused. The fix's proof (#269) ran `codex sandbox` DIRECTLY, and
`boss doctor`'s probe did the same (subprocess.run), so both passed on a host where every build
failed.

Here the launcher and Codex are local fakes on disk — a boss-sandbox-exec that records its argv
and execs the rest, and a `codex` on PATH that records the argv and environment it was started
with — and nothing of BOSS is patched: the check, the build's spawn (owned_process.
workload_spawn) and the sandbox identity are the real ones. The proof is the argv the launcher
received and the environment Codex ran with, for the doctor's probe, the Setup engine-check
job's probe, and a build's own Codex spawn alike.
"""
from __future__ import annotations

import json
import shutil
import tempfile
from pathlib import Path

import pytest

from boss_engine.config import get_settings
from boss_engine.services import engine_inner_sandbox as eis
from boss_engine.services import engine_policy as ep
from boss_engine.services import owned_process, sandbox_identity

BWRAP = ("bwrap: No permissions to create a new namespace, likely because the kernel does not "
         "allow non-privileged user namespaces.")
PROBE_ARGV = ["sandbox", "-c", 'sandbox_mode="workspace-write"', "--", "true"]


@pytest.fixture()
def launcher(monkeypatch):
    # Short and under /tmp: the run directories hold sockets on Linux.
    root = Path(tempfile.mkdtemp(prefix="blch", dir="/tmp"))
    run_root, shim, home, bin_dir = (root / "runs", root / "shim", root / "home", root / "bin")
    for d in (run_root, shim, home, bin_dir):
        d.mkdir()
    helper_log, codex_log = root / "helper.log", root / "codex.jsonl"
    helper = root / "boss-sandbox-exec"
    helper.write_text("#!/bin/sh\n"
                      f"printf '%s\\n' \"$*\" >> {helper_log}\n"
                      "if [ \"$1\" = run ]; then shift 3; exec \"$@\"; fi\n"
                      "exit 0\n")
    codex = bin_dir / "codex"
    codex.write_text("#!/usr/bin/env python3\n"
                     "import json, os, sys\n"
                     f"open({str(codex_log)!r}, 'a').write(json.dumps({{'argv': sys.argv[1:], "
                     "'home': os.environ.get('HOME'), 'uid': os.environ.get('BOSS_SANDBOX_UID'), "
                     "'db': os.environ.get('DATABASE_URL')}) + '\\n')\n"
                     f"if os.path.exists({str(root / 'refuse')!r}):\n"
                     f"    sys.stderr.write({BWRAP!r} + '\\n'); sys.exit(1)\n")
    for f in (helper, codex):
        f.chmod(0o755)
    monkeypatch.setenv(sandbox_identity.HELPER_ENV, str(helper))
    monkeypatch.setenv(sandbox_identity.RUN_ROOT_ENV, str(run_root))
    monkeypatch.setenv(sandbox_identity.MODE_ENV, "required")
    monkeypatch.setenv(sandbox_identity.NETWORK_ENV, "off")
    monkeypatch.setattr(sandbox_identity, "SHIM_ROOT", str(shim))   # the helper's compiled path
    # No real CLI login is seeded into the throwaway HOME.
    for name in ("HOME", "CODEX_HOME", "GROK_HOME", "CLAUDE_CONFIG_DIR"):
        monkeypatch.setenv(name, str(home))
    monkeypatch.setenv("PATH", f"{bin_dir}:{__import__('os').environ.get('PATH', '')}")
    monkeypatch.setenv("DATABASE_URL", "postgresql://boss:hunter2@db/boss")
    monkeypatch.setenv(eis.ENV, "seccomp")
    yield {"root": root, "helper_log": helper_log, "codex_log": codex_log, "run_root": run_root,
           "refuse": root / "refuse"}
    shutil.rmtree(root, ignore_errors=True)


def _launched(fx) -> list[list[str]]:
    lines = fx["helper_log"].read_text().splitlines() if fx["helper_log"].exists() else []
    # Codex's launches (the helper also runs BOSS's own cleanup of the run directory).
    return [line.split(" ") for line in lines if line.startswith("run ")
            and line.split(" ")[3:4] == ["codex"]]


def _codex_runs(fx) -> list[dict]:
    if not fx["codex_log"].exists():
        return []
    return [json.loads(line) for line in fx["codex_log"].read_text().splitlines()]


def test_the_doctor_probe_starts_codex_through_boss_sandbox_exec(launcher):
    found = eis.check(expected="seccomp", label=None)
    assert found["ok"] and found["launcher"] == "boss-sandbox-exec", found
    [launched] = _launched(launcher)
    assert launched[0] == "run" and launched[2] == "--", launched
    uid = launched[1]
    assert launched[3:] == ["codex", *PROBE_ARGV]
    [ran] = _codex_runs(launcher)
    # Codex ran with the build sandbox's environment: its HOME in the run directory, its uid,
    # and nothing of the control plane's.
    assert ran["argv"] == PROBE_ARGV and ran["uid"] == uid
    assert Path(ran["home"]).resolve().is_relative_to(launcher["run_root"].resolve())
    assert ran["db"] is None
    # The probe's sandbox ends with it.
    assert not any(launcher["run_root"].iterdir())


def test_a_refusal_inside_the_launcher_is_named_by_layer(launcher):
    launcher["refuse"].write_text("1")
    found = eis.check(expected="seccomp", label=None)
    assert not found["ok"] and found["layer"] == "kernel", found
    assert found["launcher"] == "boss-sandbox-exec" and _launched(launcher)


def test_doctor_prints_one_line_and_exits_by_verdict(launcher, capsys):
    launcher["refuse"].write_text("1")
    assert eis.main(["--expect", "seccomp"]) == 1
    line = capsys.readouterr().out.strip()
    assert line.startswith("bad [") and "Fix:" in line and "boss-sandbox-exec" in line
    launcher["refuse"].unlink()
    assert eis.main(["--expect", "seccomp"]) == 0
    assert capsys.readouterr().out.startswith("ok ")


def test_a_launcher_that_cannot_hold_a_sandbox_fails_the_check(launcher, monkeypatch):
    monkeypatch.setenv(sandbox_identity.HELPER_ENV, str(launcher["root"] / "missing-helper"))
    found = eis.check(expected="seccomp", label=None)
    assert not found["ok"] and found["layer"] == "launcher", found
    assert not _codex_runs(launcher)          # Codex is never started outside the launcher


def test_the_setup_job_probe_and_a_builds_codex_get_the_same_launch(launcher):
    """The engine-check job binds a build sandbox (engine_policy.run_check); its probe reuses it.
    A build's Codex (codex_code_agent: owned_process.popen under the build's owner and identity)
    is launched the same way: `run <uid> --` then codex, with the same sandbox environment."""
    with owned_process.owner_scope(ep.OWNER_KIND, "engchk_launch", fresh=True), \
            sandbox_identity.bind("engchk_launch") as ident:
        found = ep.inner_sandbox_check(get_settings())
        assert found["ok"] and found["launcher"] == "boss-sandbox-exec", found
    with owned_process.owner_scope("code_run", "crun_launch", fresh=True), \
            sandbox_identity.bind("crun_launch") as build:
        child = owned_process.popen(["codex", "exec", "--sandbox", "danger-full-access", "-"],
                                    stdin=None, cwd=str(build.home), env=None)
        assert child.wait(timeout=30) == 0
    probe_launch, build_launch = _launched(launcher)
    assert probe_launch[1] == str(ident.uid) and build_launch[1] == str(build.uid)
    assert probe_launch[2:4] == build_launch[2:4] == ["--", "codex"]
    probe_ran, build_ran = _codex_runs(launcher)
    for ran, who in ((probe_ran, ident), (build_ran, build)):
        assert ran["uid"] == str(who.uid) and ran["db"] is None
        assert Path(ran["home"]).resolve() == who.home.resolve()
