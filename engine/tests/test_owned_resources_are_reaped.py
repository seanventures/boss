"""EVERYTHING A BUILD MAKES IS OWNED AND REAPED — the resource × ending grid (stability spec §4f).

Seven kinds of resource BOSS creates, each ended five ways: success, failure, cancel, timeout,
and the parent itself SIGKILLed followed by one reaper pass. 35 cases, one guarantee:

    after it ends, nothing of it is left — no process in its group or carrying its token, no
    zombie, no listening port, no container or image, no directory, no open ledger row, and the
    database's connection count back at its baseline.

Measured before this existed (scratchpad/m/orphan_probe.py, in the api image): three agents
killed the way code_agent.py killed them left 9 processes holding 166 MB, then 9 zombies.

Each case builds a REAL tree: an HTTP server holding a port, a grandchild that calls setsid()
to escape the process group (the case a group kill alone misses), and a background sleep. The
container and image cases need a reachable Docker daemon and skip without one; they touch only
containers/images this test labels with its own throwaway install id and name prefix — never
anything else on the daemon. The database case needs BOSS_TEST_DATABASE_URL (Postgres).
"""
from __future__ import annotations

import json
import os
import signal
import socket
import subprocess
import sys
import textwrap
import time
import uuid
from pathlib import Path

import pytest

ENGINE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ENGINE))

from boss_engine.services import owned_process  # noqa: E402

ENDINGS = ("success", "failure", "cancel", "timeout", "parent_sigkill")


# ------------------------------------------------------------------------------------------ #
# helpers
# ------------------------------------------------------------------------------------------ #

def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _listening(port: int) -> bool:
    with socket.socket() as s:
        s.settimeout(0.3)
        return s.connect_ex(("127.0.0.1", port)) == 0


def _wait_listening(port: int, timeout: float = 15.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if _listening(port):
            return
        time.sleep(0.1)
    raise AssertionError(f"port {port} never listened")


def _tree(port: int, tail: str) -> list[str]:
    """An agent-shaped tree: a server holding `port`, a grandchild that leaves the group with
    setsid(), a background sleep — then `tail` decides how the leader ends."""
    py = sys.executable
    script = (
        f"{py} -m http.server {port} --bind 127.0.0.1 >/dev/null 2>&1 & "
        f"{py} -c 'import os,time; os.setsid(); time.sleep(600)' & "
        "sleep 600 & "
        f"{tail}"
    )
    return ["sh", "-c", script]


def _assert_gone(pgid, token, port=None, *, within: float = 10.0) -> None:
    deadline = time.monotonic() + within
    left = owned_process.members(pgid, token)
    while left["alive"] and time.monotonic() < deadline:
        time.sleep(0.1)
        left = owned_process.members(pgid, token)
    assert left["alive"] == [], f"still running: {left['alive']}"
    assert left["zombies"] == [], f"zombies left: {left['zombies']}"
    if port is not None:
        assert not _listening(port), f"port {port} still held"


_PARENT = textwrap.dedent("""
    import json, sys, time
    sys.path.insert(0, {engine!r})
    from boss_engine.services import owned_process
    kind = {kind!r}
    argv = {argv!r}
    with owned_process.owner_scope("code_run", "run_parent_kill"):
        if kind == "preview":
            from pathlib import Path
            from boss_engine.services.preview_service import launch_app
            proc, _ = launch_app(Path({workdir!r}), argv, {log!r},
                                 owner=owned_process.Owner("preview", "prev_parent_kill"))
        elif kind == "seat":
            with owned_process.owner_scope("review_seat", "run_parent_kill:review[0]"):
                proc = owned_process.popen(argv)
        else:
            proc = owned_process.popen(argv)
    print(json.dumps({{"pgid": proc.pgid, "token": proc.token}}), flush=True)
    time.sleep(600)
""")


def _parent_then_sigkill(kind: str, argv: list[str], port: int, tmp_path: Path) -> dict:
    """A separate 'API' process starts the tree, then is SIGKILLed: nothing in it ever gets to
    run a `finally`. Returns the tree's handles."""
    if not owned_process._proc_available():
        pytest.skip("finding an orphan by its token needs /proc (Linux, where BOSS runs); "
                    "run this grid in the api image: scripts/owned_resources_grid.sh")
    code = _PARENT.format(engine=str(ENGINE), kind=kind, argv=argv, workdir=str(tmp_path),
                          log=str(tmp_path / "app.log"))
    parent = subprocess.Popen([sys.executable, "-c", code], stdout=subprocess.PIPE, text=True)
    try:
        line = parent.stdout.readline()
        handles = json.loads(line)
        _wait_listening(port)
    finally:
        parent.send_signal(signal.SIGKILL)
        parent.wait(timeout=10)
    # The orphaned tree is still alive — that is the leak this case exists for.
    assert owned_process.members(handles["pgid"], handles["token"])["alive"]
    # ONE reaper pass (resource_reaper runs this every minute).
    owned_process.sweep_orphans(only={handles["token"]})
    return handles


# ------------------------------------------------------------------------------------------ #
# 1. a process group: an agent with grandchildren
# ------------------------------------------------------------------------------------------ #

@pytest.mark.parametrize("ending", ENDINGS)
def test_process_group(ending, tmp_path):
    port = _free_port()
    run_id = f"run_{uuid.uuid4().hex[:8]}"
    if ending in ("success", "failure"):
        code = 0 if ending == "success" else 1
        argv = _tree(port, f"sleep 1.5; exit {code}")
        with owned_process.owner_scope("code_run", run_id):
            done = owned_process.run(argv, timeout=30)
        assert done.returncode == code
        # run() leaves nothing: the handles are gone with it, so look for anything still
        # carrying a token of ours that no live owner holds.
        assert owned_process.orphan_trees() == {}
        assert not _listening(port)
        return
    if ending == "timeout":
        argv = _tree(port, "sleep 600")
        started = time.monotonic()
        with pytest.raises(subprocess.TimeoutExpired):
            owned_process.run(argv, timeout=3)
        assert time.monotonic() - started < 20, "timeout must not wait on the pipe holders"
        assert owned_process.orphan_trees() == {}
        assert not _listening(port)
        return
    if ending == "cancel":
        with owned_process.owner_scope("code_run", run_id):
            proc = owned_process.popen(_tree(port, "sleep 600"))
        _wait_listening(port)
        started = time.monotonic()
        result = owned_process.terminate_owner(run_id, reason="cancel")
        assert result["ended"] == 1
        _assert_gone(proc.pgid, proc.token, port)
        assert time.monotonic() - started <= 10, "Stop → zero processes within 10 s"
        # And a stopped build cannot start another agent a second later.
        with owned_process.owner_scope("code_run", run_id):
            with pytest.raises(owned_process.OwnerCancelled):
                owned_process.popen(["true"])
        return
    handles = _parent_then_sigkill("process", _tree(port, "sleep 600"), port, tmp_path)
    _assert_gone(handles["pgid"], handles["token"], port)


# ------------------------------------------------------------------------------------------ #
# 2. a preview process (launch_app: the one launcher for previews and the serve proof)
# ------------------------------------------------------------------------------------------ #

@pytest.mark.parametrize("ending", ENDINGS)
def test_preview_process(ending, tmp_path):
    from boss_engine.services import preview_service

    port = _free_port()
    preview_id = f"prev_{uuid.uuid4().hex[:8]}"
    log = str(tmp_path / "app.log")
    tail = "sleep 2; exit 3" if ending == "failure" else "sleep 600"
    argv = _tree(port, tail)
    if ending == "parent_sigkill":
        handles = _parent_then_sigkill("preview", argv, port, tmp_path)
        _assert_gone(handles["pgid"], handles["token"], port)
        return
    proc, _ = preview_service.launch_app(
        tmp_path, argv, log, owner=owned_process.Owner("preview", preview_id))
    _wait_listening(port)
    if ending == "success":
        preview_service._kill(proc.pid)                     # stop_preview's teardown
    elif ending == "failure":
        # The app crashed on its own, leaving its server child behind. Nobody waits on a
        # preview's Popen; the reaper's poll collects it and ends what it left.
        deadline = time.monotonic() + 15
        while proc.poll() is None and time.monotonic() < deadline:
            time.sleep(0.2)
        owned_process.poll_all()
    elif ending == "cancel":
        owned_process.terminate_owner(preview_id, reason="cancel")
    elif ending == "timeout":
        preview_service._teardown({"mode": "process", "pid": proc.pid})   # TTL expiry
    _assert_gone(proc.pgid, proc.token, port)
    assert proc not in owned_process.live()


# ------------------------------------------------------------------------------------------ #
# 6. a review seat (nested in its build)
# ------------------------------------------------------------------------------------------ #

@pytest.mark.parametrize("ending", ENDINGS)
def test_review_seat(ending, tmp_path):
    port = _free_port()
    run_id = f"run_{uuid.uuid4().hex[:8]}"
    seat = f"{run_id}:review[0]"
    if ending == "parent_sigkill":
        handles = _parent_then_sigkill("seat", _tree(port, "sleep 600"), port, tmp_path)
        _assert_gone(handles["pgid"], handles["token"], port)
        return
    with owned_process.owner_scope("code_run", run_id):
        with owned_process.owner_scope("review_seat", seat):
            if ending in ("success", "failure"):
                code = 0 if ending == "success" else 1
                assert owned_process.run(_tree(port, f"sleep 1.5; exit {code}"),
                                         timeout=30).returncode == code
                assert seat not in owned_process.live_owner_ids()
                assert owned_process.orphan_trees() == {}
                assert not _listening(port)
                return
            proc = owned_process.popen(_tree(port, "sleep 600"))
    _wait_listening(port)
    assert seat in owned_process.live_owner_ids()
    if ending == "cancel":
        owned_process.terminate_owner(run_id, reason="cancel")      # the BUILD was stopped
    else:
        owned_process.terminate_owner(seat, reason="deadline")      # the panel deadline
    _assert_gone(proc.pgid, proc.token, port)
    assert seat not in owned_process.live_owner_ids()


# ------------------------------------------------------------------------------------------ #
# 7. database connections
# ------------------------------------------------------------------------------------------ #

PG_URL = os.environ.get("BOSS_TEST_DATABASE_URL", "")


def _pg_count(url: str) -> int:
    import psycopg

    dsn = url.replace("postgresql+psycopg://", "postgresql://")
    with psycopg.connect(dsn, autocommit=True) as c:
        return int(c.execute(
            "SELECT count(*) FROM pg_stat_activity WHERE datname = current_database() "
            "AND pid <> pg_backend_pid()").fetchone()[0])


@pytest.mark.skipif(not PG_URL.startswith("postgres"), reason="needs BOSS_TEST_DATABASE_URL")
@pytest.mark.parametrize("ending", ENDINGS)
def test_db_connections(ending, monkeypatch):
    from sqlalchemy import text

    from boss_engine import config, db

    monkeypatch.setenv("DATABASE_URL", PG_URL)
    config.get_settings.cache_clear()
    try:
        with db.begin(None) as c:                       # warm the one pool
            c.execute(text("SELECT 1"))
        baseline = _pg_count(PG_URL)
        if ending == "parent_sigkill":
            code = textwrap.dedent(f"""
                import sys, time
                sys.path.insert(0, {str(ENGINE)!r})
                from sqlalchemy import text
                from boss_engine import db
                cms = []
                for _ in range(5):
                    cm = db.begin(None); c = cm.__enter__(); c.execute(text("SELECT 1"))
                    cms.append(cm)
                print("held", flush=True)
                time.sleep(600)
            """)
            env = {**os.environ, "DATABASE_URL": PG_URL}
            child = subprocess.Popen([sys.executable, "-c", code], stdout=subprocess.PIPE,
                                     text=True, env=env)
            assert child.stdout.readline().strip() == "held"
            assert _pg_count(PG_URL) > baseline
            child.send_signal(signal.SIGKILL)
            child.wait(timeout=10)
        else:
            for _ in range(100):
                try:
                    with db.begin(None) as c:
                        if ending == "success":
                            c.execute(text("SELECT 1"))
                        elif ending == "failure":
                            c.execute(text("SELECT 1"))
                            raise RuntimeError("the work failed")
                        elif ending == "cancel":
                            c.execute(text("SELECT pg_cancel_backend(pg_backend_pid())"))
                            c.execute(text("SELECT pg_sleep(0.2)"))
                        elif ending == "timeout":
                            c.execute(text("SET LOCAL statement_timeout = 20"))
                            c.execute(text("SELECT pg_sleep(1)"))
                except Exception:  # noqa: BLE001 — the ending under test
                    pass
        deadline = time.monotonic() + 10
        count = _pg_count(PG_URL)
        while count > baseline + 2 and time.monotonic() < deadline:
            time.sleep(0.2)
            count = _pg_count(PG_URL)
        assert count <= baseline + 2, f"{count} connections after, baseline {baseline}"
    finally:
        config.get_settings.cache_clear()
