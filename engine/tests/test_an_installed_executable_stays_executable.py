"""HelpDesk AI crun_247435e17c00478483b2 was blocked after 35 minutes with
`sh: 1: tsx: Permission denied` from the serve proof, although its install, its tests and the
connector's tests had all passed — and its error said "the template's tests did not pass".

Why: the run's uid installs under umask 007 (boss-sandbox-exec), so npm left
node_modules/tsx/dist/cli.mjs 0770. workspace_perms' readable pass made it `a+rw` (0776), and the
serve proof's copy of the tree is BOSS's: the run's uid, now "other", could read the file but
not execute it. Every place BOSS opens or copies a tree for another uid had its own `| 0o666`.

Pinned here, as a class: an executable a build installed stays executable — for "other", the
uid that runs a BOSS-made copy — through the readable pass and through every path that copies,
seeds or opens a workspace (serve proof, preview, security scan, toolchain cache); and a blocked
run's error names the gate that failed (app_serves) with its first error line.
"""
from __future__ import annotations

import inspect
import os
import re
import shutil
import stat
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from boss_engine.security_scan import service as scan_service
from boss_engine.services import governed_code_task_service as gov
from boss_engine.services import preview_service as ps
from boss_engine.services import toolchains
from boss_engine.services import workspace_perms
from boss_engine.templates import gate

ENGINE = Path(gov.__file__).resolve().parents[1]

# What crun_247435e1's serve proof recorded (quality_gates.evidence.app_serves), verbatim.
SERVE_RECORD = {
    "ok": False, "port": None, "proven": False, "source": "declared", "harness": False,
    "exit_code": 127,
    "error": ("the app exited (code 127) before serving HTTP: error: sh: 1: tsx: Permission denied "
              "log tail: up to date in 327ms > helpdesk-ai@0.1.0 migrate > tsx scripts/migrate.ts "
              "sh: 1: tsx: Permission denied"),
    "command": ("cd helpdesk-ai && npm install --include=dev --no-audit --no-fund --loglevel=error "
                "&& npm run migrate && npx tsx src/server/index.ts"),
}
# What the connector smoke check recorded for the same run (evidence.template.template_tests).
SMOKE_RECORD = {"kind": "connector", "check": "wiring smoke",
                "error": "the app did not start, so the connector's wiring was not checked",
                "routes": ["/auth/providers", "/api/me", "/api/admin"]}


def _installed_bin(ws: Path, mode: int = 0o770) -> Path:
    """What `npm install` leaves under umask 007: node_modules/tsx/dist/cli.mjs at `mode`, and
    node_modules/.bin/tsx a RELATIVE link to it."""
    target = ws / "helpdesk-ai" / "node_modules" / "tsx" / "dist" / "cli.mjs"
    target.parent.mkdir(parents=True)
    target.write_text("#!/bin/sh\necho tsx-ran\n")
    os.chmod(target, mode)
    bindir = ws / "helpdesk-ai" / "node_modules" / ".bin"
    bindir.mkdir()
    (bindir / "tsx").symlink_to("../tsx/dist/cli.mjs")
    (ws / "helpdesk-ai" / "package.json").write_text('{"scripts":{"migrate":"tsx"}}\n')
    return target


def _readable_pass(ws: Path) -> None:
    """workspace_perms' readable pass, run as the files' owner (as boss-sandbox-exec runs it)."""
    subprocess.run(["/bin/sh", "-c", workspace_perms._READABLE, "sh", str(ws)], check=True)


def _other_can_run(path: Path) -> bool:
    """The uid that runs a BOSS-owned copy is "other" to it: read + execute for other."""
    mode = stat.S_IMODE(os.stat(path).st_mode)
    return mode & stat.S_IXOTH and mode & stat.S_IROTH


def _runs(app_dir: Path) -> bool:
    out = subprocess.run(["/bin/sh", "-c", "PATH=node_modules/.bin:$PATH tsx"], cwd=app_dir,
                         capture_output=True, text=True)
    return out.returncode == 0 and "tsx-ran" in out.stdout


# ---- the readable pass never makes an executable unexecutable for "other" ------------------------

def test_the_readable_pass_keeps_execute_for_every_uid(tmp_path):
    target = _installed_bin(tmp_path)
    plain = tmp_path / "helpdesk-ai" / "README.md"
    plain.write_text("x")
    os.chmod(plain, 0o660)
    _readable_pass(tmp_path)
    assert _other_can_run(target), oct(os.stat(target).st_mode)
    # A file nobody could execute does not become executable.
    assert not stat.S_IMODE(os.stat(plain).st_mode) & 0o111
    # A tree an EARLIER pass left 0776 (no x for other) is repaired, not skipped as "rw already".
    os.chmod(target, 0o776)
    _readable_pass(tmp_path)
    assert _other_can_run(target)


def test_the_lazy_chmod_keeps_execute_too():
    assert "a+rwX" in workspace_perms._CHMOD_PATHS
    assert not re.search(r"chmod a\+rw[^xX]", workspace_perms._READABLE + workspace_perms._CHMOD_PATHS)


@pytest.mark.parametrize("before,after", [(0o770, 0o777), (0o776, 0o777), (0o700, 0o777),
                                          (0o644, 0o666), (0o600, 0o666), (0o444, 0o666)])
def test_usable_mode_opens_execute_when_anyone_had_it(before, after):
    assert workspace_perms.usable_mode(before, is_dir=False) == after


# ---- every path that copies, seeds or opens a tree, then runs it ---------------------------------

@pytest.mark.parametrize("mode", [0o770, 0o776])  # as npm left it; after the old a+rw pass
def test_the_serve_proof_copy_keeps_the_bin_executable(tmp_path, mode):
    ws = tmp_path / "ws"
    target = _installed_bin(ws, mode)
    proof = ps._proof_tree(ws)
    try:
        copied = proof / target.relative_to(ws)
        assert (proof / "helpdesk-ai/node_modules/.bin/tsx").is_symlink()
        assert _other_can_run(copied), oct(os.stat(copied).st_mode)
        assert _runs(proof / "helpdesk-ai")
    finally:
        shutil.rmtree(proof, ignore_errors=True)


def test_workspace_copy_tree_keeps_executables(tmp_path):
    target = _installed_bin(tmp_path / "src")
    dst = workspace_perms.copy_tree(tmp_path / "src", tmp_path / "dst")
    assert _other_can_run(dst / target.relative_to(tmp_path / "src"))
    assert _runs(dst / "helpdesk-ai")
    # Without a sandbox uid the modes are carried over as they were, opened to nobody.
    plain = workspace_perms.copy_tree(tmp_path / "src", tmp_path / "plain", open_to_run=False)
    assert stat.S_IMODE(os.stat(plain / target.relative_to(tmp_path / "src")).st_mode) == 0o770


def test_the_preview_open_keeps_executables(tmp_path):
    target = _installed_bin(tmp_path)
    ps._open_to_sandbox(tmp_path)
    assert _other_can_run(target)
    assert _runs(tmp_path / "helpdesk-ai")


def test_the_security_scan_open_keeps_executables(tmp_path):
    target = _installed_bin(tmp_path)
    scan_service._open_tree(tmp_path)
    assert _other_can_run(target)
    assert _runs(tmp_path / "helpdesk-ai")


@pytest.mark.parametrize("open_modes", [True, False])
def test_a_seeded_or_saved_toolchain_cache_keeps_executables(tmp_path, open_modes):
    """A Gradle wrapper's bin/gradle in a project's cache: seeded into a run (open) and saved
    back (owner only) — executable both ways."""
    src = tmp_path / "src" / "wrapper" / "bin"
    src.mkdir(parents=True)
    (src / "gradle").write_text("#!/bin/sh\necho ok\n")
    os.chmod(src / "gradle", 0o755)
    (src / "notes.txt").write_text("x")
    toolchains._copy_tree(tmp_path / "src", tmp_path / "dst", open_modes=open_modes)
    mode = stat.S_IMODE(os.stat(tmp_path / "dst/wrapper/bin/gradle").st_mode)
    assert mode == (0o777 if open_modes else 0o700)
    assert not stat.S_IMODE(os.stat(tmp_path / "dst/wrapper/bin/notes.txt").st_mode) & 0o111


@pytest.mark.skipif(not (shutil.which("npm") and shutil.which("node")), reason="needs npm")
def test_a_real_npm_bin_survives_the_readable_pass_and_the_proof_copy(tmp_path):
    """A REAL install: a local package with a `bin`, packed and installed under umask 007 (the
    sandbox's), then the readable pass and the serve proof's copy, then run from the copy."""
    pkg = tmp_path / "pkg"
    (pkg / "dist").mkdir(parents=True)
    (pkg / "package.json").write_text(
        '{"name":"fake-tsx","version":"1.0.0","bin":{"fake-tsx":"dist/cli.js"}}\n')
    (pkg / "dist" / "cli.js").write_text("#!/usr/bin/env node\nconsole.log('tsx-ran')\n")
    env = {**os.environ, "npm_config_cache": str(tmp_path / ".npm"), "npm_config_offline": "true",
           "npm_config_audit": "false", "npm_config_fund": "false", "npm_config_update_notifier": "false"}
    tgz = subprocess.run(["npm", "pack", "--silent", str(pkg)], cwd=tmp_path, env=env,
                         capture_output=True, text=True, check=True).stdout.strip().splitlines()[-1]
    app = tmp_path / "ws" / "app"
    app.mkdir(parents=True)
    (app / "package.json").write_text('{"name":"app","version":"0.0.0"}\n')
    inst = subprocess.run(["/bin/sh", "-c", f"umask 007 && npm install --no-save {tmp_path / tgz}"],
                          cwd=app, env=env, capture_output=True, text=True)
    if inst.returncode != 0:
        pytest.skip(f"npm could not install a local tarball here: {inst.stderr[-300:]}")
    cli = app / "node_modules" / "fake-tsx" / "dist" / "cli.js"
    assert stat.S_IMODE(os.stat(cli).st_mode) & 0o001 == 0  # umask 007: nothing for other
    _readable_pass(tmp_path / "ws")
    proof = ps._proof_tree(tmp_path / "ws")
    try:
        copied = proof / "app" / "node_modules" / "fake-tsx" / "dist" / "cli.js"
        assert _other_can_run(copied), oct(os.stat(copied).st_mode)
        out = subprocess.run(["/bin/sh", "-c", "node_modules/.bin/fake-tsx"], cwd=proof / "app",
                             capture_output=True, text=True, env=env)
        assert out.returncode == 0 and "tsx-ran" in out.stdout, out.stderr
    finally:
        shutil.rmtree(proof, ignore_errors=True)


def test_no_workspace_copy_or_open_bypasses_workspace_perms():
    """The class, enforced: a tree copied or opened for another uid goes through
    workspace_perms (copy_tree / open_tree / usable_mode), never a bare copytree or `| 0o666`."""
    offenders = []
    for path in (ENGINE / "services" / "preview_service.py", ENGINE / "security_scan" / "service.py",
                 ENGINE / "services" / "toolchains.py"):
        src = path.read_text()
        if "shutil.copytree(" in src:
            offenders.append(f"{path.name}: shutil.copytree")
        if re.search(r"\|\s*0o666\b|0o666\s*\|", src) or re.search(r"chmod\([^)]*0o666 if", src):
            offenders.append(f"{path.name}: | 0o666")
    assert not offenders, offenders
    assert "workspace_perms.copy_tree(" in inspect.getsource(ps._proof_tree)


# ---- a blocked run names the gate that failed ---------------------------------------------------

def test_the_headline_is_the_gate_and_its_first_error_line():
    assert ps.app_serves_headline(SERVE_RECORD) == "the app did not start: tsx: Permission denied"
    assert ps.app_serves_headline(
        {"ok": False, "command": "x", "error": "the app started and kept running but did not "
                                               "answer HTTP within 180s. log tail: listening"}
    ).startswith("the app did not serve HTTP: ")
    assert ps.app_serves_headline({"ok": False, "command": None}) == \
        "BOSS found no command that runs this app"


def test_a_template_hold_names_app_serves_not_the_template_tests():
    """crun_247435e1: "Blocked: the template's tests did not pass" — the template tests passed;
    the connector smoke check failed only because the app did not start."""
    held = gate.hold_outcome(["app_serves", "template_tests", "code_review_passed"],
                             app_serves=SERVE_RECORD, template_tests=SMOKE_RECORD)
    assert held["error"].startswith("Blocked: the app did not start: tsx: Permission denied. ")
    assert "template's tests did not pass" not in held["error"]
    assert held["next_step"] == gate.APP_SERVES_NEXT
    # A template test failure of its OWN still shows beside the app's.
    own = gate.hold_outcome(["app_serves", "template_tests"], app_serves=SERVE_RECORD,
                            template_tests={"error": "3 of 12 tests failed"})
    assert "tsx: Permission denied" in own["error"] and "template's tests did not pass" in own["error"]
    # No app failure: the words are as before.
    assert "the template's tests did not pass" in gate.hold_outcome(["template_tests"])["error"]


def test_held_run_reads_the_serve_record_from_the_gate():
    row = {"gates_failed": ["app_serves", "template_tests"], "fctx_failing": None,
           "install_failure": None, "app_serves": SERVE_RECORD, "template_tests": SMOKE_RECORD}
    conn = SimpleNamespace(execute=lambda *a, **k: SimpleNamespace(
        mappings=lambda: SimpleNamespace(first=lambda: row)))
    error = gate.held_run(conn, "crun_247435e17c00478483b2")["error"]
    assert "the app did not start: tsx: Permission denied" in error
    assert "template's tests did not pass" not in error
    src = inspect.getsource(gate.held_run)
    assert "evidence->'app_serves'" in src and "evidence->'template'->'template_tests'" in src
    # The pipeline passes both records where it writes the blocked error.
    assert "app_serves=app_serves_record" in inspect.getsource(gov._run_governed_code_task)


def test_a_blocked_run_without_a_template_leads_with_the_app_too():
    error, reason = gov.blocked_gate_messages("qg_1", ["app_serves", "template_tests"],
                                              SERVE_RECORD, SMOKE_RECORD)
    assert error.startswith("Blocked: the app did not start: tsx: Permission denied. ")
    assert "Template tests:" not in error
    assert "does not serve" in reason
