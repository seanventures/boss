"""The step said what happened and never why, and the why was two tables away.

An operator's panel step read:

    Convene a panel — failed — "the meeting it started finished as 'cancelled'"

That sentence contains no cause. Tracing it by hand took four queries:

    1. POST /v1/meetings/step raised IntegrationConfigurationError: OPENAI_API_KEY is not
       configured, and returned 500.
    2. The meeting.step job retried and DEADLETTERED, keeping that message in jobs.last_error.
    3. The shepherd saw a run with no job in ('queued','running','leased') and closed it as
       cancelled — "abandoned: no job remains that could advance this run".
    4. The workflow step reported the STATUS of that run and nothing else.

The real cause — a missing credential the operator can fix in a minute — survived step 2 and
was discarded by steps 3 and 4. The console then offered "Try Convene a panel again": a button
guaranteed to fail identically forever, with nothing on screen explaining why.

This is the walk back down.
"""
from __future__ import annotations

import json

from sqlalchemy import text

from apps.api.app.config import get_settings
from boss_engine.services.workflow_capability_runner import _why_it_ended

REAL_CAUSE = "OPENAI_API_KEY is not configured."
SWEEPER_TAUTOLOGY = "abandoned: no job remains that could advance this run"


def _meeting(engine, project_id, run_id, error):
    with engine.begin() as c:
        c.execute(text(
            "INSERT INTO meeting_runs (id, project_id, status, phase, error, directive) "
            "VALUES (:i, :p, 'cancelled', 'round1', :e, 'build a thing') "
            "ON CONFLICT (id) DO UPDATE SET error = EXCLUDED.error"),
            {"i": run_id, "p": project_id, "e": error})


def _job(engine, project_id, run_id, last_error):
    with engine.begin() as c:
        c.execute(text(
            "INSERT INTO jobs (id, project_id, kind, status, payload, last_error) "
            "VALUES (:i, :p, 'meeting.step', 'deadlettered', CAST(:pl AS JSONB), "
            "        CAST(:e AS JSONB)) ON CONFLICT (id) DO NOTHING"),
            {"i": f"job_{run_id}", "p": project_id,
             "pl": json.dumps({"run_id": run_id}), "e": json.dumps(last_error)})


def test_the_real_cause_is_recovered_from_the_job(engine):
    """The case that was reported. The run's error is the sweeper's tautology."""
    pid = get_settings().default_project_id
    _meeting(engine, pid, "mrun_why1", SWEEPER_TAUTOLOGY)
    _job(engine, pid, "mrun_why1", {"type": "HTTPStatusError", "message": REAL_CAUSE})

    with engine.begin() as c:
        why = _why_it_ended(c, "meeting_runs", "mrun_why1")
    assert REAL_CAUSE in why
    # And it is no longer the sentence that explains nothing.
    assert SWEEPER_TAUTOLOGY not in why


def test_the_runs_own_error_is_preferred_when_it_is_a_real_one(engine):
    """Only the tautology triggers the extra hop — a genuine error is the better answer."""
    pid = get_settings().default_project_id
    _meeting(engine, pid, "mrun_why2", "the panel could not reach a quorum")
    _job(engine, pid, "mrun_why2", {"message": "something less specific"})

    with engine.begin() as c:
        why = _why_it_ended(c, "meeting_runs", "mrun_why2")
    assert why == "the panel could not reach a quorum"


def test_an_empty_error_still_reaches_for_the_job(engine):
    """Three of the four cancelled meetings on the reported instance had no error at all."""
    pid = get_settings().default_project_id
    _meeting(engine, pid, "mrun_why3", None)
    _job(engine, pid, "mrun_why3", {"type": "HTTPStatusError", "message": REAL_CAUSE})

    with engine.begin() as c:
        why = _why_it_ended(c, "meeting_runs", "mrun_why3")
    assert REAL_CAUSE in why


def test_it_reports_the_exception_type_when_there_is_one(engine):
    """"HTTPStatusError: 500" tells an operator where to look; a bare message may not."""
    pid = get_settings().default_project_id
    _meeting(engine, pid, "mrun_why4", SWEEPER_TAUTOLOGY)
    _job(engine, pid, "mrun_why4", {"type": "IntegrationConfigurationError", "message": REAL_CAUSE})

    with engine.begin() as c:
        why = _why_it_ended(c, "meeting_runs", "mrun_why4")
    assert why.startswith("IntegrationConfigurationError:")


def test_no_job_and_no_error_degrades_quietly(engine):
    """The status is still reportable without a reason; this must never raise."""
    pid = get_settings().default_project_id
    _meeting(engine, pid, "mrun_why5", None)
    with engine.begin() as c:
        assert _why_it_ended(c, "meeting_runs", "mrun_why5") == ""


def test_a_missing_table_does_not_raise(engine):
    """Optional tables differ by edition; a diagnostic must not become the failure."""
    with engine.begin() as c:
        assert _why_it_ended(c, "table_that_does_not_exist", "x") == ""
