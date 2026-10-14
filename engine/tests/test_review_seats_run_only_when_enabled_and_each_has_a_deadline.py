"""The review panel runs only the seats it did not skip, side by side, each under its own deadline.

Pre-mortem findings, each verified by calling the code directly:

- After `review_skipped_tests_failing` the panel still handed EVERY seat to a pool sized from the
  enabled checks (1), so the "skipped" seats ran one after another for up to 480 s
  (crun_decc93: skip 22:49:14, review_degraded 22:57:15).
- A fix round's re-review waited on its seats with no timeout at all.
- One 420 s deadline covered the WHOLE panel while review_code alone took 336-348 s on HelpDesk,
  so a seat queued behind it was cut off before it had its turn.
- A 270 KB package-lock.json was inlined into the reviewer's input.
"""
from __future__ import annotations

import inspect
import time

from boss_engine.services import governed_code_task_service as g
from boss_engine.services import owned_process, review_input
from boss_engine.services.review_seats import run_seats


def _stub(record, label, sleep=0.0, value=None):
    def fn():
        record.append(label)
        time.sleep(sleep)
        return value if value is not None else label
    return fn


def test_a_skipped_seat_is_never_run():
    ran: list[str] = []
    seats = g.panel_seats({"regression"}, [_stub(ran, "review0"), _stub(ran, "review1")],
                          security=_stub(ran, "security"), regression=_stub(ran, "regression"),
                          consistency=_stub(ran, "consistency"))
    assert [label for label, _ in seats] == ["regression"]
    out = run_seats(seats, max_workers=len(seats), seat_deadline_s=5, owner_id=str, poll_s=0.01)
    assert ran == ["regression"] and out.results == {"regression": "regression"}


def test_the_panel_builds_its_seats_from_the_enabled_set_and_sizes_its_pool_from_them():
    src = inspect.getsource(g._run_governed_code_task)
    assert "guarded = panel_seats(enabled_seats, review_checks" in src
    assert "effective_review_lanes(_lanes, panel_size=len(guarded))" in src
    assert "max_workers = max(1, min(max_workers, len(guarded) or 1))" in src


def test_enabled_seats_run_side_by_side():
    ran: list[str] = []
    seats = [(f"s{i}", _stub(ran, f"s{i}", sleep=0.3)) for i in range(4)]
    t0 = time.monotonic()
    out = run_seats(seats, max_workers=4, seat_deadline_s=5, owner_id=str, poll_s=0.01)
    assert time.monotonic() - t0 < 0.9
    assert sorted(out.results) == ["s0", "s1", "s2", "s3"] and not out.timed_out


def test_a_seat_deadline_starts_when_that_seat_starts():
    """Two 0.4 s seats one at a time under a 0.6 s per-seat deadline both finish: a seat queued
    behind another is not charged for the wait (a whole-panel 0.6 s budget would cut the second)."""
    ran: list[str] = []
    seats = [("a", _stub(ran, "a", sleep=0.4)), ("b", _stub(ran, "b", sleep=0.4))]
    out = run_seats(seats, max_workers=1, seat_deadline_s=0.6, owner_id=str, poll_s=0.01)
    assert sorted(out.results) == ["a", "b"] and not out.timed_out


def test_a_sleeping_seat_is_ended_at_its_deadline_and_the_queued_seat_still_runs(monkeypatch):
    ended: list[str] = []
    monkeypatch.setattr(owned_process, "terminate_owner",
                        lambda oid, reason="cancel", **_k: ended.append((oid, reason)) or
                        {"ended": 1})
    strays: list[int] = []
    ran: list[str] = []
    seats = [("stuck", _stub(ran, "stuck", sleep=30)), ("next", _stub(ran, "next", sleep=0.05))]
    t0 = time.monotonic()
    out = run_seats(seats, max_workers=1, seat_deadline_s=0.3,
                    owner_id=lambda label: f"crun_x:{label}",
                    kill_strays=lambda: strays.append(1) or 0, poll_s=0.01)
    assert time.monotonic() - t0 < 2.0, "the panel waited on a seat past its deadline"
    assert out.timed_out == ["stuck"] and "stuck" not in out.results
    assert out.results.get("next") == "next", "the seat queued behind the stuck one never ran"
    assert ended == [("crun_x:stuck", "deadline")] and out.killed == 1
    assert strays == [1], "the stray sweep runs once, after the panel, only on a timeout"


def test_no_stray_sweep_without_a_timeout():
    strays: list[int] = []
    out = run_seats([("a", lambda: 1)], max_workers=1, seat_deadline_s=5, owner_id=str,
                    kill_strays=lambda: strays.append(1) or 0, poll_s=0.01)
    assert out.results == {"a": 1} and strays == []


def test_a_failing_seat_is_an_error_not_a_crash():
    def boom():
        raise RuntimeError("provider 500")
    out = run_seats([("a", boom), ("b", lambda: 2)], max_workers=2, seat_deadline_s=5,
                    owner_id=str, poll_s=0.01)
    assert "a" in out.errors and "provider 500" in out.errors["a"] and out.results == {"b": 2}


def test_a_fix_round_re_review_has_the_same_deadline_and_a_late_seat_is_hollow():
    src = inspect.getsource(g)
    block = src[src.index("_convergeable = _CONVERGEABLE"):src.index("rounds_used = max(0")]
    assert "_futures_wait(" not in block, "a re-review waits with no deadline"
    assert "seat_deadline_s=_rs_deadline" in block
    assert "kill_strays=lambda: _kill_stray_review_processes(root, review_root)" in block
    # A seat that did not finish is recorded as hollow (did not review), never as a pass.
    assert '_reseat[stage_key] = ({"summary": "", "cost_usd_micros": 0}, _why)' in block


def test_a_lockfile_is_listed_but_never_inlined_for_review():
    lock = '{"lockfileVersion": 3, "packages": {}}' + "x" * 270_000
    change = review_input.build({"package-lock.json": lock, "src/app.ts": "export const a = 1;"},
                                ["apps/web/pnpm-lock.yaml"], read=lambda p: "lock: text")
    assert "package-lock.json" in change.paths and "apps/web/pnpm-lock.yaml" in change.paths
    text = review_input.render(change)
    assert "lockfileVersion" not in text and "lock: text" not in text
    assert "=== FILE: src/app.ts ===" in text
    assert "package-lock.json  (generated lockfile: not inlined)" in text
