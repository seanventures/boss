"""A model's usage limit parks the panel until it resets — never a 500, never abandoned.

THE CASE (tester, recipe-box-fd3ab4a3, 2026-11-18 18:51 UTC). A workflow's panel step hit the
Claude CLI's session limit: `api_error_status 429 "You've hit your session limit · resets 7:50pm
(UTC)"`. /v1/meetings/step answered an unhandled 500, the meeting.step job was dead-lettered at
its first attempt, the Shepherd cancelled the meeting 21 minutes later ("abandoned: no job remains
that could advance this run") and the card showed "HTTPStatusError: Server error '500 …' for url
'http://api:8000/v1/meetings/step' For more information check: https://developer.mozilla.org/…".

THE RULE: a usage or rate limit with a known reset is a wait, not a failure. The phase is kept,
the same step is queued for after the reset (so the run always has a job that advances it), the
run says why in plain words, and the next step resumes the panel. Scripted: no model is called.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest
from sqlalchemy import text

from boss_engine.config import get_settings
from boss_engine.services import meeting_service, shepherd_service
from boss_engine.services.jobs import JobLeaseService

from ._ambient_support import eng, project  # noqa: F401

#: The Claude CLI's own words for it, as the adapter raised them on the laptop.
SESSION_LIMIT = (
    'claude CLI exited 1: {"type":"result","subtype":"success","is_error":true,'
    '"api_error_status":429,"duration_ms":721,"duration_api_ms":0,"num_turns":1,'
    '"result":"You\'ve hit your session limit · resets 7:50pm (UTC)","stop_reason":"stop_sequence"}')


def _meeting(eng, pid):  # noqa: F811
    rid = f"mrun_{uuid4().hex[:20]}"
    with eng.begin() as c:
        c.execute(text("INSERT INTO meeting_runs (id, project_id, status, phase, directive, state) "
                       "VALUES (:i, :p, 'running', 'round1', 'Recipe box: plan it', "
                       "CAST(:s AS JSONB))"),
                  {"i": rid, "p": pid, "s": json.dumps({"research": "notes"})})
    return rid


@pytest.fixture()
def limited(monkeypatch):
    calls = {"n": 0}

    def phase(engine, settings, jobs, run, run_id, phase):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError(SESSION_LIMIT)
        return {"status": "running", "done": False, "phase": "round2",
                "state_seen": dict(run["state"])}

    monkeypatch.setattr(meeting_service, "_advance_phase", phase)
    return calls


def test_the_session_limit_is_read_as_a_wait_with_its_reset():
    now = datetime(2026, 11, 18, 18, 51, tzinfo=timezone.utc)
    wait = meeting_service.usage_limit_wait(RuntimeError(SESSION_LIMIT), now=now)
    assert wait is not None and wait["resets_at"].startswith("2026-11-18T19:50")
    assert wait["words"] == ("Waiting for the model's usage limit to reset at 7:50pm UTC; "
                             "BOSS resumes it by itself then.")
    # Not every error is a limit: a crash still raises, as before.
    assert meeting_service.usage_limit_wait(RuntimeError("KeyError: 'round1'"), now=now) is None


def test_a_panel_that_hits_the_limit_waits_and_resumes_by_itself(eng, project, limited):  # noqa: F811
    rid = _meeting(eng, project)
    jobs = JobLeaseService(get_settings())
    out = meeting_service.advance_meeting_run(eng, get_settings(), jobs, rid)
    # No crash, no 500: the step answers that it waits, in words, with no internal URL.
    assert out["status"] == "waiting_for_model" and not out["done"]
    assert "usage limit to reset at" in out["words"] and "http" not in out["words"]
    with eng.begin() as c:
        run = c.execute(text("SELECT status, phase, state FROM meeting_runs WHERE id = :r"),
                        {"r": rid}).mappings().one()
        queued = c.execute(text(
            "SELECT status, run_after FROM jobs WHERE kind = 'meeting.step' "
            "AND payload->>'run_id' = :r"), {"r": rid}).mappings().all()
    assert run["status"] == "running" and run["phase"] == "round1", "the phase is kept"
    assert run["state"]["waiting_for_model"]["words"] == out["words"]
    [job] = queued
    assert job["status"] == "queued"
    resets = datetime.fromisoformat(out["resets_at"])
    assert job["run_after"] >= resets, "it runs again only after the reset"

    # The Shepherd never calls it abandoned: a job that advances it is queued.
    with eng.begin() as c:
        c.execute(text("UPDATE meeting_runs SET updated_at = now() - interval '3 hours' "
                       "WHERE id = :r"), {"r": rid})
    shepherd_service.sweep_abandoned_runs(eng, get_settings(), project)
    with eng.begin() as c:
        assert c.execute(text("SELECT status FROM meeting_runs WHERE id = :r"),
                         {"r": rid}).scalar() == "running"

    # After the reset the same step runs again and the panel moves on.
    again = meeting_service.advance_meeting_run(eng, get_settings(), jobs, rid)
    assert again["phase"] == "round2" and limited["n"] == 2


def test_every_real_phase_runs_inside_the_limit_guard():
    """/v1/meetings/step returns advance_meeting_run's answer: the guard is around every phase."""
    import inspect

    src = inspect.getsource(meeting_service.advance_meeting_run)
    assert "_advance_phase(" in src and "usage_limit_wait(" in src


def test_a_step_reason_never_carries_the_http_clients_internal_urls():
    from boss_engine.services.workflow_capability_runner import _without_internal_urls

    raw = ("HTTPStatusError: Server error '500 Internal Server Error' for url "
           "'http://api:8000/v1/meetings/step'\nFor more information check: "
           "https://developer.mozilla.org/en-US/docs/Web/HTTP/Status/500")
    said = _without_internal_urls(raw)
    assert said == "HTTPStatusError: Server error '500 Internal Server Error'"
    assert "http" not in said.lower().replace("httpstatuserror", "")
