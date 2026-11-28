"""A bound must key on something the remedy cannot mint a fresh copy of.

`shepherd_ledger` refuses to apply the same remedy to the same run twice:

    WHERE task_ref = :t AND status = 'used'

and `retry_code_task` creates a NEW run, with a NEW id, for every attempt. So the child is a
fresh `task_ref` with an empty ledger, the same remedy applies again, and the chain has no end.
Bounded per instance, unbounded per lineage (S13).

Unlike the preview loop, this one SPENDS: $4-15 a link, unattended. `MAX_ACTIONS_PER_TICK`
bounds the RATE, not the total. The only thing that stopped it in practice is that autopilot
defaults to off — a feature flag standing in for a limit.

There was already a circuit breaker. It reads:

    elif prev_infra and prev_substrate == "local_subprocess" and attempt >= 3 and not fix_note:

`not fix_note` is the escape hatch for a human saying "I know, proceed" — and the shepherd
passes `fix_note` on EVERY retry, so for automated repair the condition is always False and the
breaker under it can never fire. The escape built for a person was taken by the machine.
"""

import inspect
import pytest

from boss_engine.services import shepherd_service

from ._source import code_only
from boss_engine.services.code_task_service import retry_code_task


def test_the_bound_exists_and_is_small():
    assert 1 <= shepherd_service.SHEPHERD_MAX_LINEAGE_ATTEMPTS <= 5


def test_the_ceiling_can_be_imposed_by_the_caller():
    """An automated repair and an operator pressing retry are the SAME function. Only one of
    them should be stopped by a limit it cannot argue with, so the caller states the ceiling."""
    assert "attempt_ceiling" in inspect.signature(retry_code_task).parameters


def test_the_bound_is_on_lineage_not_on_the_run_id():
    """`attempt` is carried in finalize_ctx across the whole lineage and cannot be reset by
    minting a new run. A bound on `run_id` is exactly the defect."""
    src = inspect.getsource(retry_code_task)
    assert "attempt > attempt_ceiling" in src
    assert 'attempt = int(fctx_prev.get("attempt", 1)) + 1' in src


def test_the_ceiling_is_checked_before_the_fix_note_escape():
    """The pre-existing breaker is bypassed by any fix_note. The new ceiling must not sit behind
    the same escape, or it inherits the bug it exists to fix."""
    src = code_only(retry_code_task)
    assert src.index("attempt_ceiling is not None") < src.index("not fix_note")


def test_the_ceiling_is_not_itself_bypassable_by_a_fix_note():
    """The whole point: the shepherd supplies a fix_note every time, so any guard standing
    behind `not fix_note` is dead code for automated repair."""
    src = code_only(retry_code_task)
    block = src[src.index("attempt_ceiling is not None"):]
    block = block[:block.index("prev_infra =")]
    assert "fix_note" not in block, (
        "the ceiling must not consult fix_note; that escape is what the machine always takes")


def test_the_shepherd_passes_the_ceiling_explicitly():
    src = inspect.getsource(shepherd_service.act)
    assert "attempt_ceiling=SHEPHERD_MAX_LINEAGE_ATTEMPTS" in src


def test_hitting_the_ceiling_escalates_rather_than_going_quiet():
    """A bound that just stops is indistinguishable from a bound that never fired."""
    src = inspect.getsource(shepherd_service.act)
    assert "_escalate_exhausted_lineage" in src
    assert "except ValueError" in src


def test_the_escalation_is_what_makes_the_bound_stick():
    """`sweep` skips runs with an open escalation, so escalating removes the run from the
    candidate set instead of it being reconsidered on every 180s tick."""
    esc = inspect.getsource(shepherd_service._escalate_exhausted_lineage)
    assert "open_question(" in esc
    # The one door refuses a second open question about the same build.
    door = inspect.getsource(shepherd_service.open_question)
    assert "task_ref = :t" in door and "status = 'open'" in door, \
        "a second escalation per tick is the loop wearing a new hat"
    sweep = inspect.getsource(shepherd_service.sweep)
    assert "SWEEP_CANDIDATES_SQL" in sweep
    candidates = shepherd_service.SWEEP_CANDIDATES_SQL
    assert "shepherd_escalations" in candidates and "status = 'open'" in candidates


def test_the_refusal_says_what_was_tried_and_what_failed():
    """'Ceiling reached' with no evidence sends the operator back to the database."""
    src = inspect.getsource(retry_code_task)
    seg = src[src.index("retry ceiling reached"): src.index("retry ceiling reached") + 700]
    assert "root" in seg and "attempt" in seg
    assert "prev.get('error')" in seg or 'prev.get("error")' in seg


def test_no_automated_caller_retries_without_a_ceiling():
    """The shape guard: any shepherd-initiated retry must state a ceiling. A new automated
    caller that forgets one reintroduces the unbounded chain."""
    src = inspect.getsource(shepherd_service)
    for block in src.split("retry_code_task(")[1:]:
        head = block[:400]
        if "job_priority=JOB_PRIORITY_BACKGROUND" in head:
            assert "attempt_ceiling" in head, (
                "an automated (BACKGROUND) retry with no ceiling is the unbounded chain:\n"
                + " ".join(head.split())[:160])


# ── BOSSAPP-1300(b): bounded infra breaker bypass + step/recovery attempt_ceiling ─────────────

def test_infra_failure_stamps_boss_commit_and_bounds_breaker_bypass(monkeypatch):
    from unittest.mock import MagicMock
    from boss_engine.config import Settings
    from boss_engine.services import code_task_service as cts, step_recovery

    monkeypatch.setenv("BOSS_SOURCE_SHA", "1111111111111111111111111111111111111111")
    monkeypatch.setattr(step_recovery, "_BUILD_ID", None)
    v1 = step_recovery.boss_build()

    # 1. _fail(..., failure_class="infra") records infra_boss_commit.
    saved = {}

    class _MemConn:
        def get_run(self, rid):
            return {"finalize_ctx": {"attempt": 2}}

        def save_run(self, rid, data):
            saved.update(data)

        def execute(self, *a, **k):
            return MagicMock()

    cts._fail(_MemConn(), "crun_1", "p1", "infra boom", failure_class="infra", reason_code="infra")
    assert saved["finalize_ctx"]["infra_boss_commit"] == v1

    # 2. Breaker bypass is allowed ONCE per lineage per BOSS version, then refused until version changes.
    runs = {
        "crun_a2": {
            "id": "crun_a2", "project_id": "p1", "task_id": None, "title": "App",
            "status": "failed", "phase": "done", "error": "infra boom",
            "pull_request_url": None, "spec": {"instructions": "build", "owner": "o", "repo": "r"},
            "state": {},
            "finalize_ctx": {
                "attempt": 2, "root_run_id": "crun_a1",
                "failure_class": "infra", "execution_substrate": "local_subprocess",
                "infra_boss_commit": v1,
            },
        }
    }

    class _Tx:
        def __init__(self, c):
            self.c = c

        def __enter__(self):
            return self.c

        def __exit__(self, *_a):
            return False

    class _Conn:
        def execute(self, *a, **k):
            return MagicMock()

    created_fctx = []
    monkeypatch.setattr(cts, "_transaction", lambda _e: _Tx(_Conn()))
    monkeypatch.setattr(cts, "load_code_task_run", lambda _c, rid: runs.get(rid))
    monkeypatch.setattr(cts, "_recorded_failed_gates", lambda _c, _rid: [])
    monkeypatch.setattr(cts, "_failed_at", lambda _c, _rid: None)
    monkeypatch.setattr(cts, "lineage_branch_owner", lambda *_a, **_k: None)

    def _fake_start(_c, **kw):
        nid = f"crun_a{kw['finalize_ctx']['attempt']}"
        created_fctx.append(kw["finalize_ctx"])
        runs[nid] = {
            **runs["crun_a2"], "id": nid,
            "finalize_ctx": {**kw["finalize_ctx"], "failure_class": "infra",
                             "execution_substrate": "local_subprocess", "infra_boss_commit": v1},
        }
        return nid

    monkeypatch.setattr(cts, "start_code_task", _fake_start)
    monkeypatch.setattr(cts, "emit_event", lambda *_a, **_k: None)

    s = Settings(_env_file=None)
    # First bypass on v1 succeeds (records infra_breaker_bypasses={v1: 1}).
    r3 = cts.retry_code_task(object(), s, MagicMock(), "crun_a2", fix_note="restarted")
    assert r3["run_id"] == "crun_a3"
    assert created_fctx[-1]["infra_breaker_bypasses"] == {v1: 1}

    # Second bypass on the SAME BOSS build v1 is refused (both fix_note and by_person).
    with pytest.raises(ValueError, match="already bypassed once on BOSS build"):
        cts.retry_code_task(object(), s, MagicMock(), "crun_a3", fix_note="try again")
    with pytest.raises(ValueError, match="already bypassed once on BOSS build"):
        cts.retry_code_task(object(), s, MagicMock(), "crun_a3", by_person=True)

    # Bumping BOSS version to v2 allows ONE bypass on v2.
    monkeypatch.setenv("BOSS_SOURCE_SHA", "2222222222222222222222222222222222222222")
    monkeypatch.setattr(step_recovery, "_BUILD_ID", None)
    v2 = step_recovery.boss_build()
    assert v2 != v1
    r4 = cts.retry_code_task(object(), s, MagicMock(), "crun_a3", fix_note="upgraded boss")
    assert r4["run_id"] == "crun_a4"
    assert created_fctx[-1]["infra_breaker_bypasses"] == {v1: 1, v2: 1}

    # A second bypass on v2 without another BOSS change is refused.
    runs["crun_a4"]["finalize_ctx"]["infra_boss_commit"] = v2
    with pytest.raises(ValueError, match="already bypassed once on BOSS build"):
        cts.retry_code_task(object(), s, MagicMock(), "crun_a4", fix_note="still stuck")


def test_workflow_capability_runner_passes_attempt_ceiling_to_retry_code_task():
    from boss_engine.services import workflow_capability_runner as wcr

    src_retried = inspect.getsource(wcr._retried_build)
    src_recovery = inspect.getsource(wcr._start_recovery_build)
    assert "attempt_ceiling=" in src_retried
    assert "attempt_ceiling=" in src_recovery


def test_breaker_bypass_state_handles_malformed_infra_breaker_bypasses():
    from boss_engine.services import code_task_service as cts

    for bad in (["abc111"], "abc111", {"abc111": None}, {"abc111": "not_an_int"}, {"abc111": True},
                {"abc111": float("inf")}, {"abc111": float("-inf")}, {"abc111": float("nan")}):
        allowed, bypasses = cts._breaker_bypass_state(
            {"infra_breaker_bypasses": bad, "infra_boss_commit": "abc111"}, "abc111"
        )
        assert allowed is True
        assert isinstance(bypasses, dict)
        assert bypasses.get("abc111", 0) == 0

    allowed_none, bypasses_none = cts._breaker_bypass_state(None, "abc111")  # type: ignore[arg-type]
    assert allowed_none is True and bypasses_none == {}

    blocked, valid = cts._breaker_bypass_state(
        {"infra_breaker_bypasses": {"abc111": 1, "bad": "x"}, "infra_boss_commit": "abc111"},
        "abc111",
    )
    assert blocked is False and valid == {"abc111": 1, "bad": 0}
