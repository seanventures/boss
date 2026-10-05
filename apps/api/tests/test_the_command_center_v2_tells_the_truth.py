"""Command Center v2: what goldeneye's landing page got wrong, fixed in the engine and pinned here.

Seen on goldeneye (2026-10): "8 running" for runs nobody had touched in a month; "1 needs you"
while two builds sat at "Needs you · plan" in the Factory (their plan question had expired after
24h); the one approval card read "Missing active reservation for OpenAI call" over a raw
`missing_reservation`; and eleven Shepherd cards about builds stuck since August. Each test here
goes through the endpoints the console reads.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import text

from apps.api.tests import command_center_view_seed as seed
from boss_engine.services import command_center_view as ccv

PID = seed.PROJECT
NOW = datetime.now(timezone.utc).replace(microsecond=0)


@pytest.fixture(autouse=True)
def _fresh_memo(engine):
    ccv._PULSE_MEMO.clear()
    ccv._DELIVERY_MEMO.clear()
    yield
    ccv._PULSE_MEMO.clear()
    # The suite's reset does not clear Shepherd's questions; an open one is a card on every
    # later test's Command Center.
    with engine.begin() as c:
        c.execute(text("DELETE FROM shepherd_escalations WHERE project_id = :p"), {"p": PID})


def _view(client) -> dict:
    got = client.get("/v1/command-center/view", params={"project_id": PID, "tz": "UTC"})
    assert got.status_code == 200, got.text
    return got.json()


def _build(c, *, title: str, status: str = "completed", phase: str | None = None,
           decision_id: str | None = None, state: dict | None = None,
           created: datetime | None = None, updated: datetime | None = None) -> str:
    rid = seed.uid("crun")
    at = created or NOW - timedelta(days=30)
    c.execute(text(
        "INSERT INTO code_task_runs (id, project_id, title, status, phase, decision_id, state, "
        "created_at, updated_at) VALUES (:i, :p, :t, :s, COALESCE(:ph, :s), :d, CAST(:st AS jsonb), :c, :u)"),
        {"i": rid, "p": PID, "t": title, "s": status, "ph": phase, "d": decision_id,
         "st": json.dumps(state or {}), "c": at, "u": updated or at})
    return rid


def _stage(c, *, run_id: str, role: str, step: str, ended: datetime, kind: str = "stage") -> str:
    aid = seed.uid("act")
    c.execute(text(
        "INSERT INTO agent_acts (act_id, project_id, role_key, kind, status, root_act_id, "
        "handed_by_kind, how, accountable, code_task_run_id, step_id, label, started_at, ended_at) "
        "VALUES (:i, :p, :r, :k, 'complete', :i, 'boss', 'next_stage', 'ceo', :run, :s, 'Stage', "
        ":a, :b)"),
        {"i": aid, "p": PID, "r": role, "k": kind, "run": run_id, "s": step,
         "a": ended - timedelta(minutes=5), "b": ended})
    return aid


# ── (a) a run with no progress for a week is stalled, not running ────────────────────────────


def test_a_run_with_no_progress_for_a_week_is_stalled_and_not_counted_running(engine, client):
    with engine.begin() as c:
        wf = seed.workflow(c, steps=3)
        stale = seed.run(c, wf, status="running", created=NOW - timedelta(days=30),
                         title="Feature flag expiry tracker")
        seed.step(c, stale, "s1", status="succeeded", started=NOW - timedelta(days=30),
                  completed=NOW - timedelta(days=29))
        seed.step(c, stale, "s2", status="running", started=NOW - timedelta(days=29))
        blocked = seed.run(c, wf, status="blocked", created=NOW - timedelta(days=20),
                           title="Meeting cost meter")
        seed.step(c, blocked, "s1", status="succeeded", started=NOW - timedelta(days=20),
                  completed=NOW - timedelta(days=8))
        moving = seed.run(c, wf, status="running", created=NOW - timedelta(days=12),
                          title="Status page")
        # Started twelve days ago, but its step moved yesterday: still running.
        seed.step(c, moving, "s1", status="succeeded", started=NOW - timedelta(days=12),
                  completed=NOW - timedelta(days=1))
        seed.step(c, moving, "s2", status="running", started=NOW - timedelta(days=1))
        # Bookkeeping touched the stale run's row today; that is not progress.
        c.execute(text("UPDATE workflow_runs SET updated_at = now() WHERE id = :i"), {"i": stale})
    view = _view(client)
    states = {m["run_id"]: m for m in view["missions"]}
    assert states[stale]["state"] == "stalled" and states[stale]["verb"] == "Stalled"
    assert states[blocked]["state"] == "stalled"
    assert states[moving]["state"] == "running"
    assert datetime.fromisoformat(states[stale]["last_progress_at"]) <= NOW - timedelta(days=28)
    assert view["counts"]["running"] == 1 and view["counts"]["stalled"] == 2
    # The live board (Agents › Live) counts the same.
    board = client.get("/v1/agents/live-board", params={"project_id": PID}).json()
    assert board["counts"]["running"] == 1 and board["counts"]["stalled"] == 2
    assert view["status"]["key"] == "running"           # one is really moving

    with engine.begin() as c:
        c.execute(text("UPDATE workflow_runs SET status = 'completed', completed_at = now() "
                       "WHERE id = :i"), {"i": moving})
    view = _view(client)
    assert view["status"]["key"] == "stalled" and view["status"]["text"] == "2 stalled"
    assert view["counts"]["running"] == 0


# ── (b) a build waiting at its plan is waiting on you, on every surface ─────────────────────


def _plan_wait(c, *, title: str = "Interview Scorecard") -> tuple[str, str]:
    did = seed.uid("dec")
    run_id = _build(c, title=title, status="blocked", phase="awaiting_plan_approval",
                    decision_id=did)
    c.execute(text(
        "INSERT INTO decisions (id, project_id, question, recommendation, status, risk_tier, "
        "default_action, deadline_at, created_at) VALUES (:i, :p, :q, CAST(:r AS jsonb), "
        "'expired', 2, 'hold', :dl, :at)"),
        {"i": did, "p": PID, "q": f"Approve the technical plan for “{title}” before BOSS writes code?",
         "r": json.dumps({"action": "resume_code_task", "run_id": run_id}),
         "dl": NOW - timedelta(days=26), "at": NOW - timedelta(days=27)})
    _stage(c, run_id=run_id, role="chief_architect", step="design",
           ended=NOW - timedelta(days=27))
    return run_id, did


def test_a_build_waiting_at_its_plan_needs_you_everywhere(engine, client):
    with engine.begin() as c:
        run_id, did = _plan_wait(c)
    view = _view(client)
    card = next(a for a in view["attention"] if a["id"] == did)
    assert card["kind"] == "approval" and card["title"] == "Approve the plan?"
    assert card["build"]["app"] == "Interview Scorecard"
    assert card["source"] == "Factory"
    assert card["role_key"] == "chief_architect"          # Atlas drafted it
    n = view["counts"]["needs_you"]
    assert n >= 1
    assert client.get("/v1/agents/live", params={"project_id": PID}).json()["attention"]["count"] == n
    assert client.get("/v1/status", params={"project_id": PID}).json()["pending_decisions"] == n
    inbox = client.get("/v1/decisions/inbox", params={"project_id": PID}).json()["items"]
    assert next(i for i in inbox if i["id"] == did)["attention"] == "needs_action"
    with engine.begin() as c:
        row = c.execute(text("SELECT status, deadline_at FROM decisions WHERE id = :i"),
                        {"i": did}).mappings().one()
    assert row["status"] == "pending" and row["deadline_at"] is None
    # And it stays: a second read does not expire it again.
    assert _view(client)["counts"]["needs_you"] == n

    stopped = client.post(f"/v1/code-tasks/{run_id}/cancel",
                          json={"actor": "ceo", "reason": "Not this one."})
    assert stopped.status_code == 200, stopped.text
    with engine.begin() as c:
        assert c.execute(text("SELECT status FROM decisions WHERE id = :i"),
                         {"i": did}).scalar() == "withdrawn"
    after = _view(client)
    assert did not in {a["id"] for a in after["attention"]}
    assert after["counts"]["needs_you"] == n - 1


# ── (d) a decision says what it asks ─────────────────────────────────────────────────────────


def test_a_spend_guard_card_is_a_sentence_with_its_source(engine, client):
    did = seed.uid("dec")
    with engine.begin() as c:
        c.execute(text(
            "INSERT INTO decisions (id, project_id, question, recommendation, status, risk_tier, "
            "default_action, created_at) VALUES (:i, :p, 'Missing active reservation for OpenAI call', "
            "CAST(:r AS jsonb), 'pending', 1, 'hold', now())"),
            {"i": did, "p": PID,
             "r": json.dumps({"action": "create_budget_reservation", "reason": "missing_reservation"})})
    card = next(a for a in _view(client)["attention"] if a["id"] == did)
    assert card["title"] == "Allow an OpenAI call without a reservation"
    assert card["source"] == "Spend guard"
    assert card["detail"] != "missing_reservation" and "_" not in card["detail"]


# ── (c) the pulse ────────────────────────────────────────────────────────────────────────────


def test_the_pulse_counts_finished_agent_steps_by_day_and_says_the_latest_per_app(engine, client):
    day = NOW.replace(hour=12, minute=0, second=0)
    with engine.begin() as c:
        scorecard = _build(c, title="Build an interview scorecard app", status="completed",
                           state={"adversarial_verdict": "refuted"})
        retry = _build(c, title="Build an interview scorecard app", status="completed")
        audit = _build(c, title="Build a subscription audit tool", status="completed")
        roles = ["qa", "software_engineer", "adversarial_reviewer", "chief_architect",
                 "security_reviewer", "pm"]
        counts = {"qa": 6, "software_engineer": 5, "adversarial_reviewer": 4,
                  "chief_architect": 3, "security_reviewer": 2, "pm": 1}
        busiest = day - timedelta(days=20)
        i = 0
        for role in roles:
            for k in range(counts[role]):
                when = busiest if i < 9 else day - timedelta(days=35 + i)
                _stage(c, run_id=audit, role=role, step="write_tests", ended=when)
                i += 1
        # Outside the 14 weeks, and work that is not an agent step: never counted.
        _stage(c, run_id=audit, role="qa", step="write_tests", ended=day - timedelta(days=120))
        seed.act(c, role_key="qa", kind="chat", started=day - timedelta(days=2),
                 ended=day - timedelta(days=2))
        # The newest things: the scorecard was reviewed (changes asked) after its retry built.
        _stage(c, run_id=retry, role="software_engineer", step="implement",
               ended=day - timedelta(hours=5))
        _stage(c, run_id=scorecard, role="adversarial_reviewer", step="review_code",
               ended=day - timedelta(hours=3))
        _stage(c, run_id=audit, role="chief_architect", step="design",
               ended=day - timedelta(hours=4))
    pulse = _view(client)["pulse"]
    assert pulse["weeks"] == 14 and len(pulse["days"]) >= 7 * 13 + 1
    assert datetime.fromisoformat(pulse["start"]).weekday() == 0
    by_day = {d["date"]: d["n"] for d in pulse["days"]}
    assert by_day[busiest.date().isoformat()] == 9
    assert pulse["busiest"] == {"date": busiest.date().isoformat(), "n": 9}
    assert pulse["total"] == 21 + 3
    assert [t["role_key"] for t in pulse["top"]] == \
        ["qa", "software_engineer", "adversarial_reviewer", "chief_architect", "security_reviewer"]
    assert pulse["top"][0]["n"] == 6
    latest = pulse["latest"]
    assert [(x["role_key"], x["verb"]) for x in latest[:2]] == \
        [("adversarial_reviewer", "reviewed"), ("chief_architect", "planned")]
    assert latest[0]["verdict"] == "changes_asked" and latest[0]["kind"] == "build"
    # One per app: the scorecard's earlier "built" is not a second row.
    subjects = [x["subject"] for x in latest]
    assert len(subjects) == len(set(subjects)) == 2


# ── (e) eleven old Shepherd cards are one row, and Clean up clears exactly them ─────────────


def _question(c, *, ref: str, age: timedelta) -> str:
    eid = seed.uid("shesc")
    c.execute(text(
        "INSERT INTO shepherd_escalations (id, project_id, task_ref, question, options, created_at) "
        "VALUES (:i, :p, :r, :q, CAST(:o AS jsonb), :at)"),
        {"i": eid, "p": PID, "r": ref, "o": json.dumps(["continue", "cancel"]),
         "q": f"{ref}: stuck in `coding` with no state change for 53894 minutes. Let it continue, "
              "or cancel it?", "at": NOW - age})
    return eid


def test_old_stuck_build_questions_are_one_row_and_clean_up_clears_them(engine, client):
    with engine.begin() as c:
        ended = [_build(c, title=f"Old build {i}", status="failed") for i in range(3)]
        stuck = _build(c, title="Stuck build", status="running", phase="coding",
                       updated=NOW - timedelta(days=40))
        old = [_question(c, ref=r, age=timedelta(days=40)) for r in ended + [stuck]]
        old.append(_question(c, ref="crun_gone", age=timedelta(days=39)))
        fresh = _question(c, ref=_build(c, title="New build", status="running"),
                          age=timedelta(hours=2))
    view = _view(client)
    group = view["cleanup"]
    assert group["count"] == 5 and set(group["ids"]) == {f"shepherd:{e}" for e in old}
    assert group["title"].startswith("5 old builds stuck since ")
    ids = {a["id"] for a in view["attention"]}
    assert f"shepherd:{fresh}" in ids and not ids & set(group["ids"])

    done = client.post("/v1/command-center/clean-up", json={"project_id": PID, "actor": "ceo"})
    assert done.status_code == 200, done.text
    body = done.json()
    assert body["ok"] and body["closed"] == 5 and body["cancelled"] == 1
    assert body["result"] == "Cleared 5 old questions · stopped 1 stuck build"
    with engine.begin() as c:
        assert c.execute(text("SELECT status FROM code_task_runs WHERE id = :i"),
                         {"i": stuck}).scalar() in ("cancelled",)
        assert c.execute(text("SELECT status FROM shepherd_escalations WHERE id = :i"),
                         {"i": fresh}).scalar() == "open"
        assert c.execute(text("SELECT count(*) FROM system_doctor_actions "
                              "WHERE action = 'clean_up_old'")).scalar() == 5
    after = _view(client)
    assert after["cleanup"] is None
    assert f"shepherd:{fresh}" in {a["id"] for a in after["attention"]}
    # Nothing left to clear: a second press is a no-op, said as such.
    again = client.post("/v1/command-center/clean-up", json={"project_id": PID, "actor": "ceo"})
    assert again.json()["closed"] == 0


def test_clean_up_needs_a_project(client):
    assert client.post("/v1/command-center/clean-up", json={"actor": "ceo"}).status_code == 422


# ── re-test (independent tester, cc/v2 @99926e41) ───────────────────────────────────────────


def test_the_cleanup_row_counts_every_old_question_and_clean_up_clears_them_all(engine, client,
                                                                              monkeypatch):
    """The cards read a page of ten; the row said 10 when there were more. It counts every old
    open question, and Clean up pages through all of them."""
    from boss_engine.services import system_doctor

    monkeypatch.setattr(system_doctor, "CLEAN_UP_PAGE", 5)
    with engine.begin() as c:
        old = [_question(c, ref=_build(c, title=f"Old build {i}", status="failed"),
                         age=timedelta(days=30 + i)) for i in range(14)]
    group = _view(client)["cleanup"]
    assert group["count"] == 14 and group["title"].startswith("14 old builds stuck since ")
    body = client.post("/v1/command-center/clean-up", json={"project_id": PID, "actor": "ceo"}).json()
    assert body["closed"] == 14
    with engine.begin() as c:
        assert c.execute(text("SELECT count(*) FROM shepherd_escalations WHERE id = ANY(:i) "
                              "AND status = 'open'"), {"i": old}).scalar() == 0
    assert _view(client)["cleanup"] is None


def test_a_finding_never_shows_a_raw_id_or_unknown(engine, client):
    import re

    with engine.begin() as c:
        b = _build(c, title="Build a lead triage inbox", status="failed")
        mine = []
        for q in ("unknown — how should this proceed?",
                  f"{b}: stuck in `coding` with no state change for 4000 minutes. Let it continue, "
                  "or cancel it?"):
            eid = seed.uid("shesc")
            c.execute(text("INSERT INTO shepherd_escalations (id, project_id, task_ref, question, "
                           "options, created_at) VALUES (:i, :p, :r, :q, '[]'::jsonb, :at)"),
                      {"i": eid, "p": PID, "r": b, "q": q, "at": NOW - timedelta(hours=1)})
            mine.append(f"shepherd:{eid}")
    cards = [a for a in _view(client)["attention"] if a["id"] in mine]
    assert len(cards) == 2
    for a in cards:
        words = f"{a['title']} {a['detail']}"
        assert not re.search(r"\b(crun|wfr|drun|shesc)_[0-9a-z]{6,}", words), words
        assert "unknown" not in words.lower(), words
        assert "Lead Triage Inbox" in words, words
    assert any("stuck coding for 2 days" in a["title"] for a in cards)


def test_the_latest_feed_says_what_the_team_made(engine, client):
    """A PR opened, a dossier, a panel's PRD are moves too — not only code-task steps."""
    with engine.begin() as c:
        aid = seed.uid("act")
        c.execute(text(
            "INSERT INTO agent_acts (act_id, project_id, role_key, kind, status, root_act_id, "
            "handed_by_kind, how, accountable, label, artifacts, started_at, ended_at) VALUES "
            "(:i, :p, 'pm', 'step', 'complete', :i, 'boss', 'started', 'ceo', "
            "'Meeting · Lead triage inbox', CAST(:a AS jsonb), :s, :e)"),
            {"i": aid, "p": PID, "a": json.dumps([{"kind": "minutes", "ref": "PRD v1"}]),
             "s": NOW - timedelta(hours=3), "e": NOW - timedelta(hours=2)})
        aid = seed.uid("act")
        c.execute(text(
            "INSERT INTO agent_acts (act_id, project_id, role_key, kind, status, root_act_id, "
            "handed_by_kind, how, accountable, label, artifacts, started_at, ended_at) VALUES "
            "(:i, :p, 'software_engineer', 'stage', 'complete', :i, 'boss', 'started', 'ceo', "
            "'Build · Changelog composer', CAST(:a AS jsonb), :s, :e)"),
            {"i": aid, "p": PID, "a": json.dumps([{"kind": "pr", "ref": "#42",
                                                   "url": "https://github.com/a/b/pull/42"}]),
             "s": NOW - timedelta(hours=2), "e": NOW - timedelta(hours=1)})
    latest = _view(client)["pulse"]["latest"]
    said = [(e["verb"], e["subject"]) for e in latest]
    assert ("opened PR #42 for", "Changelog composer") in said
    assert ("wrote the PRD for", "Lead triage inbox") in said
    pr = next(e for e in latest if e["verb"].startswith("opened PR"))
    assert pr["kind"] == "made" and pr["url"] == "https://github.com/a/b/pull/42"


# ── re-check (cc/v2 @ddf673f8) ──────────────────────────────────────────────────────────────


def test_clean_up_never_stops_a_build_that_waits_on_you(engine, client):
    """An 8-day-old question about a build at its plan: Clean up closes the question and leaves
    the build, its decision and its Needs you row exactly as they were."""
    with engine.begin() as c:
        run_id, dec = _plan_wait(c)
        c.execute(text("UPDATE code_task_runs SET updated_at = :u WHERE id = :i"),
                  {"u": NOW - timedelta(days=9), "i": run_id})
        q = _question(c, ref=run_id, age=timedelta(days=8))
    before = _view(client)
    assert any(a.get("decision_id") == dec for a in before["attention"])
    body = client.post("/v1/command-center/clean-up", json={"project_id": PID, "actor": "ceo"}).json()
    assert body["cancelled"] == 0 and body["closed"] == 1
    with engine.begin() as c:
        assert c.execute(text("SELECT status FROM code_task_runs WHERE id = :i"), {"i": run_id}).scalar() == "blocked"
        assert c.execute(text("SELECT answer FROM shepherd_escalations WHERE id = :i"),
                         {"i": q}).scalar() == "cleared: the build waits on a person"
    after = _view(client)
    assert any(a.get("decision_id") == dec for a in after["attention"])
    assert after["counts"]["needs_you"] == before["counts"]["needs_you"]


def test_shepherd_never_asks_about_a_build_that_waits_on_you_and_never_twice(engine):
    from boss_engine.services.shepherd_service import open_question

    with engine.begin() as c:
        run_id, _dec = _plan_wait(c)
        assert open_question(c, project_id=PID, task_ref=run_id, question="Stuck?", options=[],
                             evidence={}) is None
        other = _build(c, title="A running build", status="running", phase="coding")
        first = open_question(c, project_id=PID, task_ref=other, question="Still coding?",
                              options=["continue"], evidence={})
        assert first
        again = open_question(c, project_id=PID, task_ref=_build(c, title="Another", status="running"),
                              question="Still coding?", options=["continue"], evidence={})
        assert again is None


def test_rejecting_a_plan_stops_its_build(engine, client):
    with engine.begin() as c:
        run_id, dec = _plan_wait(c)
    got = client.post(f"/v1/decisions/{dec}/reject", json={"actor": "ceo", "note": "Not this"})
    if got.status_code == 404:
        got = client.post(f"/v1/decisions/{dec}/transition",
                          json={"action": "reject", "actor": "ceo", "note": "Not this"})
    assert got.status_code in (200, 201), got.text
    with engine.begin() as c:
        row = c.execute(text("SELECT status, finalize_ctx FROM code_task_runs WHERE id = :i"),
                        {"i": run_id}).mappings().one()
    assert row["status"] == "cancelled"
    assert (row["finalize_ctx"] or {}).get("plan_rejected") is True
    apps = client.get("/v1/factory/apps", params={"project_id": PID}).json()["apps"]
    mine = next(a for a in apps if run_id in a["run_ids"])
    assert mine["state"] != "needs_plan"
