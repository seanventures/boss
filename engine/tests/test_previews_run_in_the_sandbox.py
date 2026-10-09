"""A preview runs as its own sandbox uid, never as the control plane (IDENTITY.md §7.6).

A preview runs an app an agent, a template or a deliberately insecure demo wrote. It ran as the
API's uid, so it could read the identity master key, /proc/<api>/environ (DATABASE_URL, provider
keys), every other run's directory, and reach the database. Now `preview_service` leases a pool
uid per preview (`sandbox_identity.acquire`), held until `_teardown`, and every process of the
preview is spawned through boss-sandbox-exec under the network fence.

Here, everywhere: a stand-in helper records how it was called (the uid switch, the fence and the
kernel's refusals are proven in the Linux container by tests/e2e/preview_sandbox: master.key,
/proc of the API, the database and another preview's port, all refused).
"""
from __future__ import annotations

import ast
import inspect
import os
import shutil
import sys
import tempfile
import textwrap
import time
from pathlib import Path

import pytest

from boss_engine.services import owned_process, preview_service, sandbox_identity


@pytest.fixture()
def fake_sandbox(tmp_path, monkeypatch):
    short = Path(tempfile.mkdtemp(prefix="psbx", dir="/tmp"))
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
    monkeypatch.setenv(sandbox_identity.NETWORK_ENV, "off")
    monkeypatch.setattr(sandbox_identity, "SHIM_ROOT", str(shim_root))
    monkeypatch.setattr(preview_service, "PREVIEW_ROOT", short / "previews")
    monkeypatch.setenv("DATABASE_URL", "postgresql://boss:hunter2@db/boss")
    monkeypatch.setenv("BOSS_IDENTITY_SERVER_KEY", "server-key-must-not-leak")
    yield {"log": log, "run_root": run_root, "root": short}
    shutil.rmtree(short, ignore_errors=True)


def _lines(log: Path) -> list[str]:
    return log.read_text().splitlines() if log.exists() else []


PROBE = ("import json, os, sys; print(json.dumps({'cwd': os.getcwd(), 'home': os.environ.get('HOME'), "
         "'db': os.environ.get('DATABASE_URL'), 'server_key': os.environ.get('BOSS_IDENTITY_SERVER_KEY'), "
         "'port': os.environ.get('PORT'), 'uid_env': os.environ.get('BOSS_SANDBOX_UID')}), flush=True)")


def test_a_preview_runs_as_its_own_uid_in_its_own_directory_and_ends_with_it(fake_sandbox):
    ident = preview_service._preview_sandbox("prev_sbx1")
    assert ident is not None and sandbox_identity.held("prev_sbx1") is ident
    workdir = preview_service._preview_workdir("prev_sbx1", ident)
    assert workdir.parent == ident.run_dir                         # its tree is in its 2770 directory
    log_path = preview_service.preview_log_path("prev_sbx1")
    assert not Path(log_path).resolve().is_relative_to(ident.run_dir.resolve())   # its log is not
    proc, port = preview_service.launch_app(
        workdir, [sys.executable, "-c", PROBE], log_path,
        owner=owned_process.Owner("preview", "prev_sbx1", "proj_1"),
        port=preview_service._sandboxed_preview_port(ident))
    proc.wait(timeout=30)
    import json

    seen = json.loads(Path(log_path).read_text().strip().splitlines()[-1])
    assert Path(seen["cwd"]).resolve() == workdir.resolve()
    assert Path(seen["home"]).resolve() == ident.home.resolve()
    assert seen["db"] is None and seen["server_key"] is None      # nothing of the control plane's env
    assert seen["uid_env"] == str(ident.uid)
    assert port == sandbox_identity.preview_port_for_uid(ident.uid)  # the one port the fence opens to it
    assert seen["port"] == str(port)
    runs = [l for l in _lines(fake_sandbox["log"]) if l.startswith("run ")]
    assert runs and all(l.startswith(f"run {ident.uid} -- ") for l in runs)
    assert oct(os.stat(log_path).st_mode & 0o777) == "0o600"

    run_dir = ident.run_dir
    preview_service._teardown({"id": "prev_sbx1", "pid": proc.pid, "mode": "process"})
    assert sandbox_identity.held("prev_sbx1") is None
    assert _lines(fake_sandbox["log"]).count(f"kill {ident.uid}") >= 2     # at lease and at release
    assert not run_dir.exists()


def test_two_previews_never_share_a_uid_a_directory_or_a_port(fake_sandbox):
    a = preview_service._preview_sandbox("prev_a")
    b = preview_service._preview_sandbox("prev_b")
    try:
        assert a.uid != b.uid and a.run_dir != b.run_dir
        assert sandbox_identity.preview_port_for_uid(a.uid) != sandbox_identity.preview_port_for_uid(b.uid)
        assert preview_service._preview_sandbox("prev_a") is a          # idempotent while held
    finally:
        preview_service._release_preview_sandbox("prev_a")
        preview_service._release_preview_sandbox("prev_b")


def test_required_isolation_refuses_the_preview_rather_than_run_it_as_the_api(fake_sandbox, monkeypatch):
    monkeypatch.setenv(sandbox_identity.HELPER_ENV, "/nonexistent/boss-sandbox-exec")
    with pytest.raises(sandbox_identity.SandboxUnavailable):
        preview_service._preview_sandbox("prev_no")


def test_a_lease_this_process_lost_is_still_cleaned_up(fake_sandbox):
    """After an API restart the lease is gone from memory; stopping the preview still kills its
    uid and removes its directory, found by the directory's owner."""
    ident = preview_service._preview_sandbox("prev_lost")
    run_dir = ident.run_dir
    with sandbox_identity._ACTIVE_LOCK:                                   # noqa: SLF001
        sandbox_identity._ACTIVE.pop("prev_lost")                         # noqa: SLF001
    owner = os.lstat(run_dir).st_uid
    if not (sandbox_identity.UID_BASE <= owner < sandbox_identity.UID_BASE + sandbox_identity.UID_COUNT):
        pytest.skip("the stand-in helper cannot chown to a pool uid here")
    preview_service._release_preview_sandbox("prev_lost")
    assert not run_dir.exists()


def test_the_discovered_port_must_be_one_the_previews_uid_listens_on(tmp_path, monkeypatch):
    """The log is the app's to write: 'listening on 8000' must not make BOSS's API the preview."""
    log = tmp_path / "p.log"
    log.write_text("Server listening on http://127.0.0.1:8000\n")
    monkeypatch.setattr(preview_service, "_http_ok", lambda port, path="/": True)
    monkeypatch.setattr(preview_service, "ports_listened_by", lambda uid: {19007})
    assert preview_service._discover_working_port(str(log), 19007, timeout=2, listener_uid=61007) == 19007
    monkeypatch.setattr(preview_service, "ports_listened_by", lambda uid: set())
    assert preview_service._discover_working_port(str(log), 19007, timeout=1, listener_uid=61007) is None
    # unsandboxed (a developer's Mac): unchanged
    assert preview_service._discover_working_port(str(log), 19007, timeout=1) == 8000


def test_ports_listened_by_reads_the_kernels_socket_table(tmp_path):
    table = tmp_path / "tcp"
    table.write_text(
        "  sl  local_address rem_address   st tx_queue rx_queue tr tm->when retrnsmt   uid  timeout inode\n"
        "   0: 00000000:4A3F 00000000:0000 0A 00000000:00000000 00:00000000 00000000 61007        0 1 1\n"
        "   1: 0100007F:1F40 00000000:0000 0A 00000000:00000000 00:00000000 00000000  1000        0 2 1\n"
        "   2: 0100007F:4A40 0100007F:9C40 01 00000000:00000000 00:00000000 00000000 61007        0 3 1\n")
    assert preview_service.ports_listened_by(61007, (str(table),)) == {0x4A3F}
    assert preview_service.ports_listened_by(61007, (str(tmp_path / "missing"),)) is None


def _calls(fn, name: str) -> list[ast.Call]:
    tree = ast.parse(textwrap.dedent(inspect.getsource(fn)))
    return [n for n in ast.walk(tree) if isinstance(n, ast.Call)
            and getattr(n.func, "id", getattr(n.func, "attr", None)) == name]


@pytest.mark.parametrize("fn", [preview_service.start_preview, preview_service._respawn_process_preview])  # noqa: SLF001
def test_every_process_preview_start_takes_its_sandbox_first(fn):
    """THE CLASS: each way a process preview starts holds its sandbox before it spawns, gives the
    app the preview's own owner (so owned_process wraps it as that uid), and asks discovery for a
    port that uid listens on."""
    src = textwrap.dedent(inspect.getsource(fn))
    assert src.index("_preview_sandbox(") < src.index("launch_app(")
    for call in _calls(fn, "launch_app"):
        owner = next(k for k in call.keywords if k.arg == "owner")
        assert "Owner('preview'" in ast.unparse(owner.value)
    for call in _calls(fn, "_discover_working_port"):
        assert any(k.arg == "listener_uid" for k in call.keywords)


def test_a_library_previews_tests_run_as_the_preview_too():
    src = inspect.getsource(preview_service.start_preview)
    verify = src[src.index("def _verify("):src.index("return {", src.index("def _verify("))]
    assert 'owner=owned_process.Owner("preview", preview_id' in verify
    assert "env=env" in verify and "app_environment(" in verify


def test_teardown_of_a_process_preview_releases_its_uid():
    src = inspect.getsource(preview_service._teardown)  # noqa: SLF001
    assert "_release_preview_sandbox(" in src
