"""THE HEAVY-BUILD SLOT IS HELD FOR HEAVY WORK, NOT FOR THINKING — on a real database.

Enterprise validation, 4 CPU / 8 GiB, one heavy slot: one build held the slot ~1.5 h, its
design, review panel and model turns included, while Hello World and License Shelf waited
12,361 s and 12,876 s before their first lease (ENT-REC-39, ENT-WAIT-03). A build now gives its
slot back while a read-only session thinks and takes it back, in order, before heavy work.

These hold the guarantees around it: capacity is respected (CONC-04), a running build waiting
for its slot goes before a new lease, lowering capacity drains and never kills (CONC-05), the
wait is queue time for every repair clock (REC-39), the wait is said on the build (WAIT-03), and
a stopped build stops waiting.
"""
from __future__ import annotations

import json
import threading
import time
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest
from sqlalchemy import text

from boss_engine.services import admission, heavy_slot, owned_process, step_recovery, stuck_work

from ._ambient_support import eng, make_project, project, purge  # noqa: F401

HOST = admission.Capacity(total_mb=8192, available_mb=6500, cores=4, disk_free_gb=80.0)


@pytest.fixture(autouse=True)
def fast(monkeypatch, eng):  # noqa: F811
    monkeypatch.setattr(heavy_slot, "RELEASE_AFTER_SECONDS", 0.05)
    monkeypatch.setattr(heavy_slot, "POLL_SECONDS", 0.05)
    monkeypatch.setattr(admission, "sample", lambda: HOST)
    with eng.begin() as c:
        c.execute(text("DELETE FROM heavy_slot_releases"))
        c.execute(text("UPDATE jobs SET status = 'succeeded' WHERE status IN ('leased', 'running', "
                       "'queued')"))
        admission.set_state(c, "builds_at_once", {"value": 1})
        admission.set_state(c, "capacity_boost", {"boost": 0})
    yield
    with eng.begin() as c:
        admission.set_state(c, "builds_at_once", {"value": None})
        c.execute(text("DELETE FROM heavy_slot_releases"))


def _build(eng, project, *, status="leased", title="Invoice desk"):  # noqa: F811
    rid, jid = f"crun_hs_{uuid4().hex[:12]}", f"job_hs_{uuid4().hex[:12]}"
    with eng.begin() as c:
        c.execute(text(
            "INSERT INTO code_task_runs (id, project_id, status, phase, title, spec) VALUES "
            "(:id, :p, 'running', 'implement', :t, '{}'::jsonb)"), {"id": rid, "p": project, "t": title})
        c.execute(text(
            "INSERT INTO jobs (id, project_id, kind, status, payload, on_behalf_of, chain, "
            "on_behalf_system, attempt, max_attempts, fence_epoch, lease_owner, lease_token_hash, "
            "lease_expires_at, heartbeat_at) VALUES (:j, :p, 'code.task', :s, CAST(:pl AS jsonb), "
            "'sys_legacy_system', '[]'::jsonb, false, 1, 3, 1, 'w1', :h, "
            "now() + interval '10 minutes', now())"),
            {"j": jid, "p": project, "s": status, "pl": json.dumps({"run_id": rid}),
             "h": heavy_slot._hash(f"tok_{jid}")})
    return rid, jid


def _finish(eng, jid):
    with eng.begin() as c:
        c.execute(text("UPDATE jobs SET status = 'succeeded' WHERE id = :j"), {"j": jid})


def _row(eng, jid):
    with eng.begin() as c:
        return c.execute(text("SELECT state, fence_epoch FROM heavy_slot_releases WHERE job_id = :j"),
                         {"j": jid}).mappings().first()


def _until(check, timeout=5.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if check():
            return True
        time.sleep(0.02)
    return False


def _admit_new(eng):
    with eng.begin() as c:
        return admission.admit_job(c, "code.task", cap=HOST)


def test_a_thinking_build_gives_its_slot_to_the_next_and_takes_it_back_in_order(eng, project):  # noqa: F811
    a_run, a_job = _build(eng, project, title="Invoice desk")
    assert not _admit_new(eng).admitted, "one heavy slot, held by the leased build"
    with heavy_slot.tracking(eng, a_run, lease_token=f"tok_{a_job}"):
        exited = threading.Event()

        def design():
            with heavy_slot.light(a_run):
                assert _until(lambda: _row(eng, a_job) is not None)
                # The next build may lease now: the slot is free while Invoice desk thinks.
                assert _admit_new(eng).admitted
                _build(eng, project, title="Hello World")   # ... and it holds the slot now
            exited.set()

        t = threading.Thread(target=design)
        t.start()
        # Invoice desk's design ended: it waits for its slot back, said on the build.
        assert _until(lambda: (_row(eng, a_job) or {}).get("state") == "waiting")
        assert not exited.is_set()
        with eng.begin() as c:
            run = dict(c.execute(text("SELECT * FROM code_task_runs WHERE id = :r"),
                                 {"r": a_run}).mappings().first())
            said = stuck_work.wait_reason(c, run)
        assert said["code"] == "heavy_slot" and "Hello World" in said["words"]
        # A running build waiting for its slot goes before any new lease.
        assert not _admit_new(eng).admitted
        with eng.begin() as c:
            hello_job = c.execute(text(
                "SELECT j.id FROM jobs j JOIN code_task_runs r ON r.id = j.payload->>'run_id' "
                "WHERE r.title = 'Hello World' AND r.project_id = :p"), {"p": project}).scalar()
        time.sleep(0.2)
        _finish(eng, hello_job)
        t.join(5)
        assert exited.is_set() and _row(eng, a_job) is None
    with eng.begin() as c:
        waited = c.execute(text(
            "SELECT (data->>'waited_seconds')::float FROM events WHERE entity_id = :r AND "
            "event_type = 'code_task.heavy_slot_acquired'"), {"r": a_run}).scalar()
        assert waited and waited >= 0.2
        # QUEUE TIME IS NOT WORK TIME (REC-39): every repair clock subtracts the wait.
        assert step_recovery.heavy_slot_waited_seconds(c, [a_run]) >= 0.2


def test_a_short_verdict_never_gives_the_slot_away(eng, project, monkeypatch):  # noqa: F811
    monkeypatch.setattr(heavy_slot, "RELEASE_AFTER_SECONDS", 0.5)
    run, job = _build(eng, project)
    with heavy_slot.tracking(eng, run, lease_token=f"tok_{job}"):
        with heavy_slot.light(run):
            time.sleep(0.1)
        time.sleep(0.6)
        assert _row(eng, job) is None
        assert not _admit_new(eng).admitted


def test_a_light_session_opened_beside_heavy_work_never_gives_the_slot_away(eng, project):  # noqa: F811
    run, job = _build(eng, project)
    with heavy_slot.tracking(eng, run, lease_token=f"tok_{job}"):
        verify_done = threading.Event()
        light_in = threading.Event()

        def seat():
            with heavy_slot.light(run):
                light_in.set()
                verify_done.wait(5)
                time.sleep(0.3)

        with heavy_slot.heavy(run):
            t = threading.Thread(target=seat)
            t.start()
            light_in.wait(5)
            time.sleep(0.2)
            assert _row(eng, job) is None, "never released while the verify runs"
        verify_done.set()
        t.join(5)
        assert _row(eng, job) is None, "a heavy step ending never gives the slot away"


def test_an_abandoned_seat_never_gives_the_slot_away_between_stages(eng, project):  # noqa: F811
    """Review PR #367: a seat past its deadline, still inside light(), made the build release
    its slot after the NEXT heavy step, while it went on with un-instrumented work."""
    run, job = _build(eng, project)
    with heavy_slot.tracking(eng, run, lease_token=f"tok_{job}"):
        stuck = threading.Event()

        def zombie_seat():
            with heavy_slot.light(run):
                stuck.wait(5)

        t = threading.Thread(target=zombie_seat)
        t.start()
        time.sleep(0.02)
        with heavy_slot.heavy(run):       # implement / verify
            time.sleep(0.1)
        released = _until(lambda: _row(eng, job) is not None, timeout=1.0)
        stuck.set()
        t.join(5)
    assert not released, "the build holds between stages, whatever seat is still open"


def test_the_opening_scope_ending_takes_the_slot_back_whatever_else_is_open(eng, project):  # noqa: F811
    run, job = _build(eng, project)
    with heavy_slot.tracking(eng, run, lease_token=f"tok_{job}"):
        hold = threading.Event()
        opened = threading.Event()

        def stray():
            opened.wait(5)
            with heavy_slot.light(run):
                hold.wait(5)

        t = threading.Thread(target=stray)
        t.start()
        with heavy_slot.light(run):                     # the design session
            assert _until(lambda: _row(eng, job) is not None)
            opened.set()
            time.sleep(0.1)
        assert _row(eng, job) is None, "design ended: the build holds again"
        hold.set()
        t.join(5)


def test_lowering_capacity_drains_and_a_stale_row_never_counts(eng, project):  # noqa: F811
    with eng.begin() as c:
        admission.set_state(c, "builds_at_once", {"value": 2})
    a_run, a_job = _build(eng, project)
    b_run, b_job = _build(eng, project)
    with eng.begin() as c:
        assert admission.heavy_counts(c)["holding"] == 2
        # A row left by an older lease of this job (a process that died) does not count.
        c.execute(text("INSERT INTO heavy_slot_releases (job_id, fence_epoch, state) "
                       "VALUES (:j, 0, 'light')"), {"j": a_job})
        assert admission.heavy_counts(c)["holding"] == 2
        admission.set_state(c, "builds_at_once", {"value": 1})
    # Lowered to one: both running builds keep going (nothing is stopped) ...
    with eng.begin() as c:
        assert c.execute(text("SELECT count(*) FROM jobs WHERE id IN (:a, :b) AND status = "
                              "'leased'"), {"a": a_job, "b": b_job}).scalar() == 2
    # ... and a third waits until they drain below the new number.
    c_run, c_job = _build(eng, project)
    with heavy_slot.tracking(eng, c_run, lease_token=f"tok_{c_job}"):
        got = threading.Event()

        def review():
            with heavy_slot.light(c_run):
                assert _until(lambda: _row(eng, c_job) is not None)
            got.set()

        t = threading.Thread(target=review)
        t.start()
        assert _until(lambda: (_row(eng, c_job) or {}).get("state") == "waiting")
        _finish(eng, a_job)
        time.sleep(0.3)
        assert not got.is_set(), "one still holds; capacity is one"
        _finish(eng, b_job)
        t.join(5)
        assert got.is_set()


def test_a_stopped_build_stops_waiting(eng, project):  # noqa: F811
    _other_run, _other_job = _build(eng, project)
    run, job = _build(eng, project)
    with heavy_slot.tracking(eng, run, lease_token=f"tok_{job}"):
        errors: list = []

        def review():
            try:
                with heavy_slot.light(run):
                    assert _until(lambda: _row(eng, job) is not None)
            except owned_process.OwnerCancelled as exc:
                errors.append(exc)

        # Two leased, one slot: `run` gives its slot back and cannot take it again.
        t = threading.Thread(target=review)
        t.start()
        assert _until(lambda: (_row(eng, job) or {}).get("state") == "waiting")
        with eng.begin() as c:
            c.execute(text("UPDATE code_task_runs SET status = 'cancelled' WHERE id = :r"),
                      {"r": run})
        t.join(5)
        assert errors and "cancelled" in str(errors[0])


def test_an_untracked_run_is_untouched():
    # A preview, a discovery or a direct call: no leased build in this process, no slot moves.
    with heavy_slot.light("crun_nobody"), heavy_slot.heavy("crun_nobody"):
        pass


def test_read_only_sessions_think_and_writing_sessions_work(monkeypatch, tmp_path):
    from boss_engine.services import engine_contract

    seen: list[str] = []

    @contextmanager
    def mark(kind):
        seen.append(kind)
        yield

    monkeypatch.setattr(heavy_slot, "light", lambda *_a: mark("light"))
    monkeypatch.setattr(heavy_slot, "heavy", lambda *_a: mark("heavy"))

    class Inner:
        engine = "echo"
        adds_working_rules = True

        def run_role(self, prompt, cwd, **_kw):
            return {"summary": "ok", "stop_reason": "end_turn"}

        def run(self, spec, repo, sandbox):
            return "built"

    from boss_engine.services.llm_telemetry import set_llm_context

    agent = engine_contract.ContractedAgent(Inner())
    monkeypatch.setattr(engine_contract, "session_tree_problem", lambda _w: None)
    def _copy(_w):
        # The contract removes the copy's parent after the turn: never tmp_path's own parent.
        tree = tmp_path / "copies" / f"c{len(seen)}" / "tree"
        tree.mkdir(parents=True, exist_ok=True)
        return tree

    monkeypatch.setattr(engine_contract, "_isolated_copy", _copy)
    monkeypatch.setattr(engine_contract.readonly_guard, "is_repo", lambda _w: False)
    set_llm_context(stage="design")
    agent.run_role("plan it", tmp_path, read_only=True)          # thinks: may give it back
    # A review seat is read-only but holds a shell that can run the suite (review PR #367).
    for stage in ("review_code", "check_tests", "security_check", "acceptance_check"):
        set_llm_context(stage=stage)
        agent.run_role("review it", tmp_path, read_only=True)
    set_llm_context(stage="implement")
    agent.run_role("write it", tmp_path, read_only=False)
    agent.run(None, None, None)
    set_llm_context(stage=None)
    assert seen == ["light", "heavy", "heavy", "heavy", "heavy", "heavy", "heavy"]


def test_every_sandbox_command_holds_the_slot(monkeypatch):
    from boss_engine.services import sandbox as sandbox_mod

    entered: list[str] = []

    @contextmanager
    def held(*_a):
        entered.append("heavy")
        yield

    monkeypatch.setattr(heavy_slot, "heavy", held)
    box = sandbox_mod.EphemeralSandbox.__new__(sandbox_mod.EphemeralSandbox)
    monkeypatch.setattr(sandbox_mod.EphemeralSandbox, "_run",
                        lambda self, command, **_k: f"ran {command}")
    monkeypatch.setattr("boss_engine.services.workspace_perms.tidies_after_command",
                        lambda fn: fn)
    assert box.run("npm test") == "ran npm test"
    assert entered == ["heavy"]


def test_the_wait_still_going_counts_as_queue_time(eng, project):  # noqa: F811
    run, job = _build(eng, project)
    since = datetime.now(timezone.utc) - timedelta(minutes=30)
    with eng.begin() as c:
        c.execute(text("INSERT INTO heavy_slot_releases (job_id, fence_epoch, run_id, state, "
                       "waiting_since) VALUES (:j, 1, :r, 'waiting', :s)"),
                  {"j": job, "r": run, "s": since})
        waited = step_recovery.runs_queued_seconds(c, [run], since=since.isoformat())
    assert waited >= 29 * 60


def test_a_build_whose_run_ended_elsewhere_stands_down_quietly(eng, project):  # noqa: F811
    """HeavySlotLost is a RunFenced: the pipeline exits like a fenced duplicate (no failed
    status, no partial PR), and the wait never touches the status the run already has."""
    from boss_engine.services.run_fencing import RunFenced

    assert issubclass(heavy_slot.HeavySlotLost, RunFenced)
    _other_run, _other_job = _build(eng, project)
    run, job = _build(eng, project)
    with heavy_slot.tracking(eng, run, lease_token=f"tok_{job}"):
        errors: list = []

        def design():
            try:
                with heavy_slot.light(run):
                    assert _until(lambda: _row(eng, job) is not None)
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)

        t = threading.Thread(target=design)
        t.start()
        assert _until(lambda: (_row(eng, job) or {}).get("state") == "waiting")
        with eng.begin() as c:
            c.execute(text("UPDATE code_task_runs SET status = 'completed' WHERE id = :r"),
                      {"r": run})
        t.join(5)
    assert errors and isinstance(errors[0], heavy_slot.HeavySlotLost)
    with eng.begin() as c:
        assert c.execute(text("SELECT status FROM code_task_runs WHERE id = :r"),
                         {"r": run}).scalar() == "completed"
    # A reclaimed job (its epoch moved on) stands down the same way.
    run2, job2 = _build(eng, project)
    with heavy_slot.tracking(eng, run2, lease_token=f"tok_{job2}"):
        lost: list = []

        def design2():
            try:
                with heavy_slot.light(run2):
                    assert _until(lambda: _row(eng, job2) is not None)
            except Exception as exc:  # noqa: BLE001
                lost.append(exc)

        t = threading.Thread(target=design2)
        t.start()
        assert _until(lambda: (_row(eng, job2) or {}).get("state") == "waiting")
        with eng.begin() as c:
            c.execute(text("UPDATE jobs SET fence_epoch = fence_epoch + 1 WHERE id = :j"),
                      {"j": job2})
        t.join(5)
    assert lost and isinstance(lost[0], heavy_slot.HeavySlotLost)


def test_conc04_three_released_builds_race_back_into_one_slot(eng, project):  # noqa: F811
    """Reviewer's adversarial check (PR #367): three builds that gave the slot back race to take
    it again; never more than one holds it."""
    builds = [_build(eng, project, title=f"B{i}") for i in range(3)]
    inside = {"now": 0, "max": 0}
    lock = threading.Lock()
    go = threading.Barrier(3)
    errors = []

    def one(run, job):
        try:
            with heavy_slot.tracking(eng, run, lease_token=f"tok_{job}"):
                with heavy_slot.light(run):
                    assert _until(lambda: _row(eng, job) is not None)
                    go.wait(5)
                with heavy_slot.heavy(run):
                    with lock:
                        inside["now"] += 1
                        inside["max"] = max(inside["max"], inside["now"])
                    time.sleep(0.15)
                    with lock:
                        inside["now"] -= 1
            _finish(eng, job)
        except Exception as e:  # noqa: BLE001
            errors.append(e)

    ts = [threading.Thread(target=one, args=b) for b in builds]
    [t.start() for t in ts]
    [t.join(20) for t in ts]
    assert not errors, errors
    assert inside["max"] == 1, inside


def test_a_thinking_stage_gets_no_shell_and_a_review_seat_keeps_one():
    from boss_engine.services.llm_telemetry import set_llm_context

    set_llm_context(stage="plan")
    assert heavy_slot.read_only_tools() == "Read,Grep,Glob"
    set_llm_context(stage="review_code")
    assert heavy_slot.read_only_tools() == "Read,Grep,Glob,Bash"
    set_llm_context(stage=None)
