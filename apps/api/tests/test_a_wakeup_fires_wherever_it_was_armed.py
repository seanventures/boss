"""P1: a monitor armed outside the worker's default project must still fire.

wake_26186a08c079429e9e6d (boss-platform) sat active since 20:36, cycle 0, because the sweep
that reaches this code (apps/worker/boss_worker/main.py::_tick_agent_wakeups) only ever asked
`agent_wakeups.run_due` for the worker's single default project. `run_due(..., project_id="")`
is what must sweep every project in one call; this proves that path, end to end, against the
real `due()`/`advance()` SQL with a stubbed model turn (bound against the real `chat` signature,
the same convention `test_work_that_starts_itself.py` uses).
"""
from __future__ import annotations

import inspect

import pytest
from sqlalchemy import text

from apps.api.app.config import get_settings
from boss_engine.services import agent_wakeups as wake
from boss_engine.services.agent_service import AgentService


OTHER_PROJECT = "northpeak-bank-b"


@pytest.fixture(autouse=True)
def _clean(engine):
    with engine.begin() as c:
        c.execute(text("DELETE FROM agent_wakeups"))
    yield
    with engine.begin() as c:
        c.execute(text("DELETE FROM agent_wakeups"))


def _default_project():
    return get_settings().default_project_id


def _arm_due(engine, *, project_id, thread_id):
    with engine.begin() as c:
        wake.arm_monitor(
            c, project_id=project_id, actor="ceo", thread_id=thread_id,
            prompt="check whether the build is unstuck",
            exit_condition="the build has a result", interval_seconds=60,
        )
        c.execute(text("UPDATE agent_wakeups SET next_wake_at = now() - interval '1 min' "
                        "WHERE thread_id=:t"), {"t": thread_id})


def _bound_stub(monkeypatch, record):
    """A fake AgentService.chat bound against the real signature — see
    test_work_that_starts_itself.py::_stub_agent_service for why an unbound stub is not a test."""
    real = inspect.signature(AgentService.chat)

    class Bound:
        def __init__(self, *a, **k):
            pass

        def chat(self, *args, **kwargs):
            real.bind(self, *args, **kwargs)
            record(*args, **kwargs)
            return {"reply": "ok"}

    monkeypatch.setattr("boss_engine.services.agent_service.AgentService", Bound)


def test_a_monitor_outside_the_default_project_fires_within_one_sweep(engine, monkeypatch):
    seen = []
    _arm_due(engine, project_id=_default_project(), thread_id="athr_wake_default")
    _arm_due(engine, project_id=OTHER_PROJECT, thread_id="athr_wake_other")

    _bound_stub(monkeypatch, lambda *a, **k: seen.append(k.get("project_id")))

    out = wake.run_due(get_settings(), engine, project_id="", limit=10)

    assert set(seen) == {_default_project(), OTHER_PROJECT}, (
        f"both projects' due monitors must run in the same sweep, saw {seen!r}")
    assert len(out["ran"]) == 2
    assert all(not entry.get("error") for entry in out["ran"]), out["ran"]
    with engine.begin() as c:
        cycles = c.execute(text(
            "SELECT project_id, cycle FROM agent_wakeups WHERE thread_id IN "
            "('athr_wake_default','athr_wake_other')")).mappings().all()
    assert all(row["cycle"] == 1 for row in cycles), cycles


def test_the_wakeups_own_id_never_leaks_into_the_chat_turn_or_the_sweep_result(engine, monkeypatch):
    """Tenant isolation / neutral-names regression: no raw `wake_...` id in anything a
    cross-project sweep touches."""
    seen_messages = []
    with engine.begin() as c:
        armed = wake.arm_monitor(
            c, project_id=OTHER_PROJECT, actor="ceo", thread_id="athr_wake_neutral",
            prompt="check whether the build is unstuck",
            exit_condition="the build has a result", interval_seconds=60,
        )
        c.execute(text("UPDATE agent_wakeups SET next_wake_at = now() - interval '1 min' "
                        "WHERE id=:i"), {"i": armed["id"]})

    _bound_stub(monkeypatch, lambda *a, **k: seen_messages.append(k.get("message")))

    out = wake.run_due(get_settings(), engine, project_id="", limit=10)

    assert seen_messages and armed["id"] not in seen_messages[0]
    assert armed["id"] not in repr(out["ran"])
