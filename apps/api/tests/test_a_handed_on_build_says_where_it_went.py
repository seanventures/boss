"""A blocked attempt whose repair continued says "Continued in attempt N", in plain words.

The e2e recipe-box walk (2026-11-18): crun_87a4's page still read "It failed: Blocked: this
attempt failed its checks… YOU: Read the failure and press Retry", with Steer and Stop, after the
repair it handed on to had merged as change #2. Its repair attempt's page quoted the raw repair
prompt as the retry note, its title ended "· retry 1", and its wait line named its own repair as
the build holding the slot ("next in about 1h 17m").

Pinned on the cockpit (GET /v1/code-tasks/{id}/cockpit), the page's one read: where the repair
went, no Retry / Steer / Stop and no stale diagnosis on the handed-on attempt, and no internal
words (plain_words.leaks) anywhere the page renders a sentence.
"""
from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone

from sqlalchemy import text

from apps.api.tests import _factory_support as fx
from boss_engine.services import plain_words, stuck_work

REPAIR_NOTE = ("Autonomous review-repair: the quality gate blocked on code_review_passed, "
               "acceptance_met. Address EVERY review finding below so ALL gates pass.")


def _cockpit(client, run_id):
    r = client.get(f"/v1/code-tasks/{run_id}/cockpit", params={"project_id": fx.PROJECT})
    assert r.status_code == 200, r.text
    return r.json()


def _chain(engine):
    """Attempt 1 blocked → attempt 2 blocked → attempt 3 merged as change #2."""
    one, two, three = (f"crun_{uuid.uuid4().hex[:12]}" for _ in range(3))
    title = "Recipe box: Fix the open review item"
    stale = {"at": datetime.now(timezone.utc).isoformat(), "headline": "It failed",
             "cause": "It failed: Blocked: this attempt failed its checks",
             "next": "Read the failure and press Retry, or change the request.", "who": "you",
             "needs_person": True, "steps": []}
    fx.new_run(engine, run_id=one, status="blocked", phase="done", title=title, minutes_ago=40,
               error=f"Blocked: this attempt failed its checks; the repair continued in {two}. "
                     "Open that attempt for its outcome.",
               fctx={"review_repair_next_run": two, "diagnosis": stale,
                     "failing_gates": ["code_review_passed", "acceptance_met"]},
               state={"code_review_passed_skipped": None,
                      "security_check_skipped": "not convened: profile 'lean' does not include "
                                                "this seat"})
    fx.new_run(engine, run_id=two, status="blocked", phase="done", title=f"{title} · retry 1",
               minutes_ago=20,
               error=f"Blocked: this attempt failed its checks; the repair continued in {three}.",
               fctx={"attempt": 2, "root_run_id": one, "parent_run_id": one,
                     "review_repair_next_run": three,
                     "feedback_history": [{"attempt": 2, "parent_run_id": one,
                                           "fix_note": REPAIR_NOTE}]})
    fx.new_run(engine, run_id=three, status="completed", phase="done", title=f"{title} · retry 2",
               minutes_ago=5, pr=2,
               fctx={"attempt": 3, "root_run_id": one, "parent_run_id": two,
                     "merge": {"sha": "a" * 40, "at": datetime.now(timezone.utc).isoformat()}})
    return one, two, three


def _rendered(body) -> list[str]:
    """Every sentence the build page renders from the cockpit."""
    out = [body["run"].get("title") or "", body["state"].get("word") or "",
           body["state"].get("tip") or ""]
    out += [str(e.get("title") or "") for e in body.get("lineage") or []]
    why = body.get("why") or {}
    out += [str(why.get("words") or ""), str(why.get("detail") or "")]
    d = body.get("diagnosis") or {}
    out += [str(d.get(k) or "") for k in ("headline", "cause", "next")]
    s = body.get("summary") or {}
    out += [str(s.get("note") or "")] + [str(g) for g in ((s.get("gates") or {}).get("failed") or [])]
    for g in ((body.get("outputs") or {}).get("checks") or {}).get("gates") or []:
        out += [str(g.get("label") or ""), str(g.get("evidence") or "")]
    return out


def test_a_handed_on_attempt_says_where_its_repair_went(client, engine):
    one, two, three = _chain(engine)
    body = _cockpit(client, one)
    assert body["state"]["word"] == "Continued"
    assert body["why"]["code"] == "continued"
    assert body["why"]["words"] == "Continued in attempt 2 · merged as change #2"
    assert three not in body["why"]["words"] and two in (body["why"].get("href") or "")
    # Nothing to do on it: no Retry, Steer, Stop, and the stale "press Retry" diagnosis is gone.
    assert body["retryable"] is False and body["stoppable"] is False
    assert body["diagnosis"] is None
    assert body["continued"]["last_run_id"] == three
    for said in _rendered(body):
        assert "press Retry" not in said, said
        assert plain_words.leaks(said) == [], said


def test_a_repair_attempt_page_speaks_plainly(client, engine):
    _one, two, _three = _chain(engine)
    body = _cockpit(client, two)
    assert body["run"]["title"] == "Recipe box: Fix the open review item"
    assert body["summary"]["note"] == ("BOSS is fixing what its checks found: Review asked for "
                                       "changes, Must-haves not all met")
    for said in _rendered(body):
        assert plain_words.leaks(said) == [], said


def test_its_own_repair_holding_the_slot_is_not_a_queue_wait(engine, monkeypatch):
    one, two, _three = _chain(engine)
    with engine.begin() as c:
        c.execute(text("UPDATE code_task_runs SET status = 'repairing', phase = 'review' "
                       "WHERE id = :i"), {"i": two})
        run = stuck_work._load(c, two)
    own = {"id": "crun_mine_retry", "title": "Recipe box: Fix the open review item · retry 2",
           "project_id": fx.PROJECT, "job_at": None, "heartbeat_at": None, "root": one}
    monkeypatch.setattr(stuck_work, "_running_builds", lambda *a, **k: [own])
    monkeypatch.setattr(stuck_work, "_needs_model", lambda *a, **k: None)
    job = {"id": "job_x", "status": "queued", "last_error": None, "run_after": None,
           "created_at": datetime.now(timezone.utc), "updated_at": datetime.now(timezone.utc)}
    with engine.begin() as c:
        why = stuck_work.wait_reason(c, run, job=job)
    assert why["code"] == "own_repair" and why["who"] == "boss"
    assert "slot" not in why["words"] and "next in about" not in why["words"]
    assert plain_words.leaks(why["words"]) == []
    # Another build's slot still reads as the queue, naming only that build.
    other = {**own, "id": "crun_other", "title": "Another app", "root": None}
    monkeypatch.setattr(stuck_work, "_running_builds", lambda *a, **k: [own, other])
    with engine.begin() as c:
        why = stuck_work.wait_reason(c, run, job=job)
    assert why["code"] == "slot" and "Another app" in why["words"] and "Recipe box" not in why["words"]
    assert json.dumps(why)


def test_one_follow_up_per_brief_in_flight(client, engine):
    """"Let BOSS fix these" pressed twice (or from another tab) queues ONE paid build: the same
    parent and the same brief while it is queued or running returns that build (review #224).
    A different brief, or the same brief once the first has ended, starts its own."""
    from apps.api.tests.test_a_feature_builds_onto_its_app import APP_RUN, _the_app

    _the_app(engine)
    brief = "Fix the open review findings on this build:\n- tests/recipes.test.js:43: calls it directly"

    def press(words):
        got = client.post(f"/v1/code-tasks/{APP_RUN}/follow-up",
                          json={"instructions": words, "actor": "ceo"})
        assert got.status_code in (200, 201), got.text
        return got.json()

    first = press(brief)
    again = press("  " + brief.replace("\n", "\n  ") + "\n")   # whitespace is not a new brief
    assert first["status"] == "queued"
    assert again["status"] == "existing" and again["run_id"] == first["run_id"]
    with engine.begin() as c:
        n = c.execute(text("SELECT count(*) FROM code_task_runs WHERE "
                           "finalize_ctx->>'parent_run_id' = :p AND finalize_ctx->>'followup_brief' "
                           "IS NOT NULL AND status IN ('queued', 'running')"),
                      {"p": APP_RUN}).scalar()
    assert n == 1
    other = press(brief + "\n- pages/index.js:67: stale search prop")
    assert other["status"] == "queued" and other["run_id"] != first["run_id"]
    with engine.begin() as c:
        c.execute(text("UPDATE code_task_runs SET status = 'completed' WHERE id = :i"),
                  {"i": first["run_id"]})
    after = press(brief)
    assert after["status"] == "queued" and after["run_id"] != first["run_id"]
