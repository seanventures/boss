"""A panel whose model is the person's problem says so, waits on them, and never blocks the budget.

STRANGER TEST r8 (2026-11-19, fresh install, a plain-http "Other" model; Easy Mode on "CSV quality
desk"):
  1. The panel step died on the plain-http refusal as an unhandled 500 in /v1/meetings/step; the
     owner card read "Background job · Panel step: Server error '500 Internal Server Error' for
     url 'http://api:8000/v1/meetings/step'".
  2. "Done — retry" on it re-queued the step job, but the panel had been cancelled as abandoned:
     the step answered "cancelled", the job succeeded doing nothing, the item read Resolved.
  4. The failed panel kept its $6 "Multi-agent PRD meeting" hold: the build sat in budget_wait
     with $0 spent.

THE RULES: a person-side refusal fails the panel in the person's words (build's own classifier)
with a 200 and its hold released; "Done — retry" reopens the panel it re-runs, or says why it
cannot and keeps the item open; a hold for an ended panel never blocks. Scripted: no model.
(3 and 5 are in apps/api/tests/test_easy_mode_specs_before_it_builds.py and the consent tests.)
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest
from sqlalchemy import text

from boss_engine.services import build_failure, failure_owner as fo, meeting_service
from boss_engine.services.budget import BudgetGovernor
from boss_engine.services.jobs import JobLeaseService

from ._ambient_support import eng  # noqa: F401
from .test_every_failure_has_an_owner import _dead_job, _owner, _settings, project  # noqa: F401

#: env_refs.CredentialMisdirected's words for a plain-http model address (#227).
PLAIN_HTTP = ("Your model at http://10.0.0.7:8080 is plain http; BOSS only sends keys over https. "
              "Use an https address (Setup › Models).")


def _hold(c, pid, amount=6_000_000):
    rid = f"bres_{uuid4().hex[:16]}"
    c.execute(text(
        "INSERT INTO budget_reservations (id, project_id, category, amount_usd_micros, status, "
        "reason, expires_at) VALUES (:i, :p, 'model', :a, 'active', 'Multi-agent PRD meeting.', "
        ":x)"), {"i": rid, "p": pid, "a": amount,
                 "x": datetime.now(timezone.utc) + timedelta(hours=2)})
    return rid


def _meeting(c, pid, *, status="running", error=None, reservation=None):
    rid = f"mrun_{uuid4().hex[:20]}"
    c.execute(text(
        "INSERT INTO meeting_runs (id, project_id, status, phase, directive, error, state, "
        "finalize_ctx) VALUES (:i, :p, :s, 'round1', 'CSV quality desk', :e, "
        "CAST('{}' AS JSONB), CAST(:f AS JSONB))"),
        {"i": rid, "p": pid, "s": status, "e": error,
         "f": json.dumps({"reservation_id": reservation})})
    return rid


def _status(c, table, rid):
    return c.execute(text(f"SELECT status FROM {table} WHERE id = :r"), {"r": rid}).scalar()  # noqa: S608


def test_a_plain_http_model_fails_the_panel_in_words_with_no_500(eng, project, monkeypatch):  # noqa: F811
    def phase(engine, settings, jobs, run, run_id, phase):
        raise RuntimeError(f"the seat's turn ended failed: {PLAIN_HTTP}")

    monkeypatch.setattr(meeting_service, "_advance_phase", phase)
    with eng.begin() as c:
        hold = _hold(c, project)
        rid = _meeting(c, project, reservation=hold)
    out = meeting_service.advance_meeting_run(eng, _settings(), JobLeaseService(_settings()), rid)

    assert out["status"] == "failed" and out["done"] is True
    assert out["failure_class"] == "needs_human" and out["reason_code"] == "credential_plain_http"
    with eng.begin() as c:
        run = c.execute(text("SELECT status, error, state FROM meeting_runs WHERE id = :r"),
                        {"r": rid}).mappings().one()
        assert _status(c, "budget_reservations", hold) == "released", "its $6 hold is let go"
        assert not c.execute(text("SELECT 1 FROM jobs WHERE kind = 'meeting.step' AND "
                                  "payload->>'run_id' = :r"), {"r": rid}).first()
    assert run["status"] == "failed" and run["state"]["failure_class"] == "needs_human"
    assert run["error"].startswith("Your model at http://10.0.0.7:8080 is plain http")
    assert "seat's turn" not in run["error"]

    # Its owner is the person: one card, in plain words, pointing at Setup › Models.
    fo.sweep(eng, _settings(), project_id=project)
    with eng.begin() as c:
        row = _owner(c, "meeting", rid)
        card = c.execute(text("SELECT question FROM decisions WHERE id = :d"),
                         {"d": row["decision_id"]}).scalar()
    assert row["state"] == "needs_person" and row["cause_code"] == "model_login"
    assert "Setup › Models" in row["action"]
    assert card.startswith("Panel · CSV quality desk: Your model at http://10.0.0.7:8080 is plain")
    assert "api:8000" not in card and "500" not in card


def test_other_errors_still_raise_and_a_server_error_is_said_in_words(eng, project, monkeypatch):  # noqa: F811
    monkeypatch.setattr(meeting_service, "_advance_phase",
                        lambda *a: (_ for _ in ()).throw(KeyError("round1")))
    with eng.begin() as c:
        rid = _meeting(c, project)
    with pytest.raises(KeyError):
        meeting_service.advance_meeting_run(eng, _settings(), None, rid)
    raw = ("HTTPStatusError: Server error '500 Internal Server Error' for url "
           "'http://api:8000/v1/meetings/step'\nFor more information check: "
           "https://developer.mozilla.org/en-US/docs/Web/HTTP/Status/500")
    said = build_failure.failure_words(raw)
    assert said == build_failure.SERVER_ERROR_WORDS
    assert "api:8000" not in fo._words(raw) and "500" not in fo._words(raw)  # noqa: SLF001
    assert "for url" not in build_failure.failure_words(
        "Client error '409 Conflict' for url 'http://api:8000/v1/knowledge/x'")


def test_done_retry_on_a_dead_panel_step_reopens_its_panel(eng, project):  # noqa: F811
    """r8's item: the step job dead-lettered, the shepherd cancelled the panel as abandoned. The
    person's retry reopens the panel where it stopped and re-queues the step: something runs."""
    with eng.begin() as c:
        rid = _meeting(c, project, status="cancelled",
                       error="abandoned: no job remains that could advance this run")
        _, dl = _dead_job(c, project, "meeting.step", "Server error '500 Internal Server Error'",
                          payload={"run_id": rid}, principal="usr_ceo")
    did = fo.SURFACES["job"].person_retry(eng, _settings(), None,
                                          {"id": dl, "project_id": project}, "user:ceo")
    assert "reopened its panel" in did
    with eng.begin() as c:
        run = c.execute(text("SELECT status, error, state FROM meeting_runs WHERE id = :r"),
                        {"r": rid}).mappings().one()
        new = c.execute(text("SELECT j.status FROM deadletters d JOIN jobs j ON "
                             "j.id = d.requeued_job_id WHERE d.id = :d"), {"d": dl}).scalar()
    assert run["status"] == "running" and run["error"] is None and run["state"]["resumed_at"]
    assert new == "queued"
    # And the re-queued step really advances it (it is no longer "cancelled, done").
    with eng.connect() as c:
        assert fo.SURFACES["job"].still_failed(c, {"id": dl}) is None


@pytest.mark.parametrize("status, error, said", [
    ("completed", None, "already finished"),
    ("cancelled", "Stopped: x. BOSS built from the brief instead.", "went on without this panel"),
])
def test_done_retry_that_cannot_rerun_says_so_and_keeps_the_item_open(
        eng, project, status, error, said):  # noqa: F811
    with eng.begin() as c:
        rid = _meeting(c, project, status=status, error=error)
        _, dl = _dead_job(c, project, "meeting.step", "Server error '500 Internal Server Error'",
                          payload={"run_id": rid}, principal="usr_ceo")
    fo.sweep(eng, _settings(), project_id=project)
    key = f"meeting.step:{rid}"
    later: list = []
    with eng.begin() as c:
        row = _owner(c, "job", key)
        assert row is not None and row["state"] == "needs_person"
        card = c.execute(text("SELECT * FROM decisions WHERE id = :d"),
                         {"d": row["decision_id"]}).mappings().first()
        fo.on_answered(c, _settings(), None, dict(card), verdict="approve", actor="user:ceo",
                       spawn=later.append)
    later[0]()
    with eng.begin() as c:
        row = _owner(c, "job", key)
        assert _status(c, "deadletters", dl) == "open", "nothing was re-queued"
        assert _status(c, "meeting_runs", rid) == status
    assert row["state"] == "needs_person", "never Resolved when nothing ran"
    assert said in row["reason"] and row["decision_id"] != card["id"]


@pytest.mark.parametrize("ended", ["failed", "cancelled", "completed"])
def test_a_hold_for_a_panel_that_ended_never_blocks(eng, project, ended):  # noqa: F811
    with eng.begin() as c:
        hold = _hold(c, project)
        live = _hold(c, project, amount=1_000_000)
        _meeting(c, project, status=ended, reservation=hold)
        _meeting(c, project, status="running", reservation=live)
        before = c.execute(text("SELECT count(*) FROM budget_reservations WHERE project_id = :p "
                                "AND status = 'active'"), {"p": project}).scalar()
        assert before == 2
        BudgetGovernor(_settings()).status_for_project(c, project)
        assert _status(c, "budget_reservations", hold) == "released"
        assert _status(c, "budget_reservations", live) == "active", "a running panel keeps its"
        said = c.execute(text("SELECT data->>'reason' FROM events WHERE entity_id = :r AND "
                              "event_type = 'budget.reservation_released'"), {"r": hold}).scalar()
    assert f"has ended ({ended})" in said
