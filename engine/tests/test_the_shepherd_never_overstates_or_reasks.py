"""The Shepherd never reports a refused remedy as a repair, and never asks a person about a
preview a newer one already replaced.

Contributed from an enterprise install (part of 04af2e792, adopted on its own):

  * `act()` returns {"acted": False, "refused_because": …} when a bound refuses; `autopilot()`
    still listed the run under `applied`, so its summary claimed repairs that never happened;
  * an old failed preview kept the run a candidate after a newer preview was running. Past
    PREVIEW_MAX_ATTEMPTS it asked a person "This app failed to preview N times …" about an app
    that was up — and that loop runs before the autopilot switch, so every install saw it.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest
from sqlalchemy import text

from boss_engine.services import shepherd_service

from ._ambient_support import eng, make_project, purge  # noqa: F401


def _quiet(monkeypatch):
    """Only the candidate and question logic: the sensors and sweeps around it are stubbed."""
    for name in ("sweep_starved_work", "sweep_abandoned_runs", "sweep_stale_previews"):
        monkeypatch.setattr(shepherd_service, name, lambda *a, **k: [])
    monkeypatch.setattr(shepherd_service, "sweep", lambda *a, **k: [])
    from boss_engine.services import preview_service

    monkeypatch.setattr(preview_service, "sweep_preview_workdirs",
                        lambda *a, **k: {"reclaimed": 0, "bytes": 0})


def _ago(**kw):
    return datetime.now(timezone.utc) - timedelta(**kw)


@pytest.fixture()
def project(eng):  # noqa: F811
    pid = make_project(eng)
    yield pid
    with eng.begin() as c:
        for table in ("shepherd_escalations", "app_previews", "code_task_runs"):
            c.execute(text(f"DELETE FROM {table} WHERE project_id = :p"), {"p": pid})
    purge(eng, pid)


def _run_with_previews(eng, pid, statuses):  # noqa: F811
    rid = f"crun_{uuid4().hex[:20]}"
    with eng.begin() as c:
        c.execute(text("INSERT INTO code_task_runs (id, project_id, title, status, "
                       "pull_request_url, finalize_ctx, created_at, updated_at) VALUES (:i, :p, "
                       "'Recipe box', 'completed', 'https://github.example.com/acme/app/pull/1', "
                       "'{}'::jsonb, :at, :at)"), {"i": rid, "p": pid, "at": _ago(hours=2)})
        for n, status in enumerate(statuses):
            c.execute(text("INSERT INTO app_previews (id, project_id, run_id, status, error, "
                           "created_at) VALUES (:i, :p, :r, :s, :e, :at)"),
                      {"i": f"prv_{uuid4().hex[:16]}", "p": pid, "r": rid, "s": status,
                       "e": "the app exited before serving HTTP" if status == "failed" else None,
                       "at": _ago(minutes=60 - n * 10)})
    return rid


def _questions(eng, rid):  # noqa: F811
    with eng.begin() as c:
        return c.execute(text("SELECT question FROM shepherd_escalations WHERE task_ref = :r "
                              "AND status = 'open'"), {"r": rid}).scalars().all()


def _autopilot(eng, pid):  # noqa: F811
    from boss_engine.config import get_settings

    return shepherd_service.autopilot(eng, get_settings(), None, pid)


def test_failed_previews_with_nothing_newer_still_ask_once(eng, project, monkeypatch):  # noqa: F811
    """The control: the bound still closes the loop when the app really does not boot."""
    _quiet(monkeypatch)
    rid = _run_with_previews(eng, project, ["failed"] * shepherd_service.PREVIEW_MAX_ATTEMPTS)
    _autopilot(eng, project)
    assert any("failed to preview" in q for q in _questions(eng, rid))


@pytest.mark.parametrize("newest", ["running", "starting", "stopped"])
def test_a_failed_preview_a_newer_one_replaced_asks_nobody(eng, project, monkeypatch, newest):  # noqa: F811
    _quiet(monkeypatch)
    rid = _run_with_previews(
        eng, project, ["failed"] * shepherd_service.PREVIEW_MAX_ATTEMPTS + [newest])
    out = _autopilot(eng, project)
    assert _questions(eng, rid) == [], out


def test_a_refused_remedy_is_deferred_never_reported_as_applied(monkeypatch):
    """Behaviour, not source text: act() refuses, so the run is deferred with its reason."""
    from boss_engine.config import get_settings

    _quiet(monkeypatch)
    monkeypatch.setattr(shepherd_service, "sweep", lambda *a, **k: [{"run_id": "crun_refused"}])
    monkeypatch.setattr(shepherd_service, "propose", lambda *a, **k: {
        "class": 1, "remedy": "retry", "remedy_action": "retry_code_task",
        "needs_confirmation": False})
    monkeypatch.setattr(shepherd_service, "act", lambda *a, **k: {
        "acted": False, "refused_because": "the check did not corroborate the diagnosis"})

    class _Conn:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def execute(self, *a, **k):
            class _R:
                def mappings(self):
                    return self

                def all(self):
                    return []

                def __iter__(self):
                    return iter(())

                def scalar(self):
                    return 0
            return _R()

    monkeypatch.setattr("boss_engine.db.begin", lambda engine: _Conn())
    settings = get_settings()
    monkeypatch.setattr(settings, "shepherd_autopilot_enabled", True)
    out = shepherd_service.autopilot(object(), settings, None, "p_x")
    assert out["applied"] == [], out
    assert out["deferred"] == [{"run_id": "crun_refused", "remedy": "retry",
                                "reason": "the check did not corroborate the diagnosis"}], out
