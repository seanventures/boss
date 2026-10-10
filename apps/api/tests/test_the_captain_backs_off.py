"""The Captain does not start build after build on an idle install.

D1 (2026-10-09): "Captain: QUALITY IMPROVE PASS" started 17 builds in 55 minutes on an idle test
stack, 10 of them blocked. A tick started a new pass whenever nothing was `running`, and a pass
that ended blocked or failed is not running, so the next tick started it again. Each one is real
model spend on a real install.
"""
from __future__ import annotations

import pytest
from sqlalchemy import text

from apps.api.tests import _factory_support as fx


@pytest.fixture(autouse=True)
def _no_captain_history(engine):
    def wipe():
        with engine.begin() as c:
            ids = [r[0] for r in c.execute(text(
                "SELECT id FROM code_task_runs WHERE project_id = :p AND title ILIKE 'Captain:%'"),
                {"p": fx.PROJECT}).all()]
        for run_id in ids:
            try:
                with engine.begin() as c:
                    c.execute(text("DELETE FROM code_task_runs WHERE id = :i"), {"i": run_id})
            except Exception:  # noqa: BLE001 - a row other tables still hold is re-titled
                with engine.begin() as c:
                    c.execute(text("UPDATE code_task_runs SET title = 'old pass', "
                                   "finalize_ctx = '{}'::jsonb WHERE id = :i"), {"i": run_id})
    wipe()
    yield
    wipe()


def _gate(engine):
    from boss_engine.services.metatron_captain import captain_launch_gate

    with engine.begin() as c:
        return captain_launch_gate(c, fx.PROJECT)


def _pass(engine, **kw):
    return fx.new_run(engine, title="Captain: QUALITY IMPROVE PASS",
                      fctx={"source": "metatron_captain"}, **kw)


def test_no_history_may_start(engine):
    assert _gate(engine) is None


def test_a_pass_blocked_for_consent_holds_every_later_one(engine):
    _pass(engine, status="blocked", phase="awaiting_plan_approval", minutes_ago=24 * 60)
    held = _gate(engine)
    assert held and held["reason"] == "captain_build_needs_a_person", held
    assert "approval" in held["message"]


def test_a_pass_refused_a_permission_holds_until_it_changes(engine):
    run_id = _pass(engine, status="blocked", phase="done", minutes_ago=24 * 60,
                   error="Not allowed to start preview: it needs “Manage previews” "
                         "(preview:manage), which whoever is running it does not hold.")
    assert _gate(engine)["reason"] == "captain_build_needs_a_person"
    with engine.begin() as c:   # a person cancels it: that is the change
        c.execute(text("UPDATE code_task_runs SET status = 'cancelled' WHERE id = :i"),
                  {"i": run_id})
    assert _gate(engine) is None


def test_the_old_raw_refusal_is_recognised_too(engine):
    _pass(engine, status="blocked", phase="done", minutes_ago=24 * 60,
          error="'start_preview' needs preview:manage.")
    assert _gate(engine)["reason"] == "captain_build_needs_a_person"


def test_failures_back_off_exponentially_with_a_cap(engine):
    from boss_engine.services import metatron_captain as mc

    for _ in range(3):
        _pass(engine, status="failed", minutes_ago=30)
    held = _gate(engine)
    # 3 in a row: 10 * 2**2 = 40 minutes, and the last stopped 30 minutes ago.
    assert held and held["reason"] == "captain_backoff" and held["wait_minutes"] == 40, held
    for _ in range(12):
        _pass(engine, status="failed", minutes_ago=30)
    assert _gate(engine)["wait_minutes"] == mc.LAUNCH_BACKOFF_CAP_MINUTES


def test_a_failure_long_enough_ago_may_retry(engine):
    _pass(engine, status="failed", minutes_ago=90)
    assert _gate(engine) is None


def test_a_held_launch_starts_no_build(engine, monkeypatch):
    from boss_engine.config import get_settings
    from boss_engine.services import code_task_service
    from boss_engine.services.metatron_captain import MetatronCaptain

    _pass(engine, status="blocked", phase="awaiting_plan_approval")
    started = []
    monkeypatch.setattr(code_task_service, "start_code_task",
                        lambda *a, **k: started.append(k) or "crun_x")
    with engine.begin() as c:
        out = MetatronCaptain(get_settings())._apply_validated_move(
            c, project_id=fx.PROJECT, actor="ceo",
            move={"kind": "launch_factory_job", "detail": "QUALITY IMPROVE PASS"},
            verify_run=None, directive=None)
    assert out["status"] == "rejected" and not started, out
    assert "{" not in out["error"] and "blocked" in out["error"], out
