"""Waiting for what, and what the agent found: nobody has to dig (stuck_work).

The operator, 2026-11-07: "When I click on Build it takes to Code task page where it does not show
anything just waiting (and waiting for what ???)" and "Why these things are failing, there is no
enough reason and investigation done by Agent". Four CVE builds sat "Queued 0 of 7" for hours
while every attempt crashed in five seconds; the Needs-you card said only "CVE squad is stuck".

Pinned here, on Postgres:
  * every non-working build has a plain reason computed from real state (crash loop, a person's
    grant, a build slot held by named builds, a retry timer, a lost worker, no job at all);
  * the Shepherd investigates without being asked: five steps (stuck → checked → cause → tried →
    next), the safe move taken itself, ONE card only when a person must act;
  * its turn goes through the one agent pipeline (a stub model's choice is what is carried out);
  * with no model it still diagnoses by BOSS's rules and says so.
"""
from __future__ import annotations

import json
from datetime import timedelta
from uuid import uuid4

from sqlalchemy import text

from boss_engine.config import get_settings
from boss_engine.services import stuck_work
from boss_engine.services.core import now_utc

from ._ambient_support import eng, make_project, project, purge  # noqa: F401


def _run(c, pid, *, status="running", phase="queued", title="Bump esbuild", ctx=None,
         age_minutes=90, error=None) -> str:
    rid = f"crun_sw_{uuid4().hex[:12]}"
    c.execute(text(
        "INSERT INTO code_task_runs (id, project_id, status, phase, title, finalize_ctx, error, "
        "created_at, updated_at) VALUES (:id, :p, :s, :ph, :t, CAST(:f AS jsonb), :e, :at, :at)"),
        {"id": rid, "p": pid, "s": status, "ph": phase, "t": title, "f": json.dumps(ctx or {}),
         "e": error, "at": now_utc() - timedelta(minutes=age_minutes)})
    return rid


def _job(c, pid, rid, *, status="running", attempt=1, max_attempts=3, lease_in=4,
         last_error=None, run_after_in=0, age_minutes=60) -> str:
    jid = f"job_sw_{uuid4().hex[:12]}"
    c.execute(text(
        "INSERT INTO jobs (id, project_id, kind, status, payload, attempt, max_attempts, "
        "lease_owner, lease_expires_at, heartbeat_at, last_error, run_after, created_at, "
        "on_behalf_of, chain, on_behalf_system) VALUES (:j, :p, 'code.task', :s, "
        "CAST(:pl AS jsonb), :a, :m, :o, :exp, now(), CAST(:le AS jsonb), :ra, :ca, 'sys_worker', "
        "'[]'::jsonb, true)"),
        {"j": jid, "p": pid, "s": status, "pl": json.dumps({"run_id": rid}), "a": attempt,
         "m": max_attempts, "o": "community-worker-1-s1" if status in ("running", "leased") else None,
         "exp": now_utc() + timedelta(minutes=lease_in) if status in ("running", "leased") else None,
         "le": json.dumps(last_error) if last_error else None,
         "ra": now_utc() + timedelta(minutes=run_after_in),
         "ca": now_utc() - timedelta(minutes=age_minutes)})
    return jid


def _clean(eng, pid):
    with eng.begin() as c:
        c.execute(text("DELETE FROM deadletters WHERE project_id = :p"), {"p": pid})
        c.execute(text("DELETE FROM jobs WHERE project_id = :p"), {"p": pid})


def _reason(c, rid):
    return stuck_work.wait_reason(c, stuck_work._load(c, rid))


def test_every_waiting_build_says_what_it_waits_for(eng, project):  # noqa: F811
    try:
        with eng.begin() as c:
            loop = _run(c, project, ctx={"redelivered_attempts": list(range(2, 31))})
            _job(c, project, loop, attempt=30)
            person = _run(c, project, status="waiting_on_person", phase="awaiting_grant",
                          ctx={"person_wait": {"builder": "Helm", "ask": "Let Helm build",
                                               "opened_at": now_utc().isoformat()}})
            busy = _run(c, project, phase="implementing", title="Add SSO login")
            _job(c, project, busy, age_minutes=5)
            waiting = _run(c, project, age_minutes=3)
            _job(c, project, waiting, status="queued", age_minutes=3)
            retry = _run(c, project)
            _job(c, project, retry, status="failed_retryable", run_after_in=4,
                 last_error={"message": "model rate limited"})
            lost = _run(c, project)
            _job(c, project, lost, lease_in=-20)
            nojob = _run(c, project)
        with eng.begin() as c:
            r = {k: _reason(c, v) for k, v in dict(loop=loop, person=person, busy=busy,
                                                     waiting=waiting, retry=retry, lost=lost,
                                                     nojob=nojob).items()}
        assert r["loop"]["code"] == "crash_loop" and "every attempt crashed (29×)" in r["loop"]["words"]
        assert r["person"]["who"] == "you" and "let Helm build for you" in r["person"]["words"]
        assert r["busy"] is None                       # an agent is on it: working, not waiting
        assert r["waiting"]["code"] == "slot"
        assert r["waiting"]["words"].startswith("Waiting for a build slot:")
        assert " in use by " in r["waiting"]["words"]       # names what holds the slots
        assert r["retry"]["code"] == "retrying" and "model rate limited" in r["retry"]["words"]
        assert r["lost"]["code"] == "lost" and "stopped answering" in r["lost"]["words"]
        assert r["nojob"]["code"] == "no_job"
        for v in r.values():
            if v is not None:
                assert v["words"] not in ("Queued", "Waiting", "")
    finally:
        _clean(eng, project)


def test_a_crash_loop_is_stopped_explained_and_handed_to_a_person_once(eng, project, monkeypatch):  # noqa: F811
    """A loop on THIS version (its crashes came after this BOSS started) is stopped and reported."""
    from boss_engine.services import jobs as jobs_mod

    monkeypatch.setattr(jobs_mod, "PROCESS_STARTED_AT", now_utc() - timedelta(days=1))
    crash = {"type": "ObjectNotExecutableError", "count": 5, "where": "person_wait.py:125 _load_run",
             "message": "ObjectNotExecutableError: Not an executable object: 'SELECT …'"}
    try:
        with eng.begin() as c:
            a = _run(c, project, ctx={"last_crash": crash})
            _job(c, project, a, attempt=6)
            b = _run(c, project, title="Bump minimatch", ctx={"last_crash": crash})
            _job(c, project, b, attempt=6)
        with eng.begin() as c:
            da = stuck_work.diagnose(c, get_settings(), a, use_model=False)
            db = stuck_work.diagnose(c, get_settings(), b, use_model=False)
        assert da["chosen"] == "stop_crash_loop" and da["acted"] is True
        assert [s["k"] for s in da["steps"]] == ["stuck", "checked", "cause", "tried", "next"]
        assert "person_wait.py:125" in da["cause"]
        assert da["needs_person"] and da["card_id"] and da["card_id"] == db["card_id"]
        assert da["by"]["source"].startswith("BOSS's rules")
        with eng.begin() as c:
            run = c.execute(text("SELECT status, error, finalize_ctx->'diagnosis'->>'headline' AS h "
                                 "FROM code_task_runs WHERE id = :r"), {"r": a}).mappings().one()
            jobs = c.execute(text("SELECT status FROM jobs WHERE payload->>'run_id' = :r"),
                             {"r": a}).scalars().all()
        assert run["status"] == "failed" and "crashed on every attempt" in run["error"]
        assert "person_wait.py:125" in run["error"]
        assert run["h"] and set(jobs) == {"deadlettered"}
    finally:
        _clean(eng, project)


def test_a_dead_job_is_restarted_by_the_shepherd_without_asking(eng, project):  # noqa: F811
    try:
        with eng.begin() as c:
            rid = _run(c, project)
        with eng.begin() as c:
            d = stuck_work.diagnose(c, get_settings(), rid, use_model=False)
        assert d["chosen"] == "restart_job" and d["acted"] and not d["needs_person"]
        assert d["card_id"] is None
        with eng.begin() as c:
            assert c.execute(text("SELECT count(*) FROM jobs WHERE payload->>'run_id' = :r AND "
                                  "status = 'queued'"), {"r": rid}).scalar() == 1
    finally:
        _clean(eng, project)


def test_a_build_lost_on_the_old_version_is_tried_once_on_this_one(eng, project):  # noqa: F811
    try:
        with eng.begin() as c:
            rid = _run(c, project, status="failed", error="Every attempt (31 of 3) stopped")
            _job(c, project, rid, status="deadlettered",
                 last_error={"code": "lease_lost", "message": "lost", "lost_on_older_boss": True})
        with eng.begin() as c:
            d = stuck_work.diagnose(c, get_settings(), rid, use_model=False)
        assert d["chosen"] == "retry_on_this_version" and d["acted"]
        with eng.begin() as c:
            st = c.execute(text("SELECT status FROM code_task_runs WHERE id = :r"), {"r": rid}).scalar()
            again = stuck_work.diagnose(c, get_settings(), rid, use_model=False)
        assert st == "running"
        assert again["chosen"] != "retry_on_this_version"     # once, never a loop
    finally:
        _clean(eng, project)


class _Model:
    """A stub engine answering the one pipeline's JSON contract."""

    def __init__(self, action: str):
        self.action, self.prompts = action, []

    def create_text_response(self, prompt, system_prompt=""):
        self.prompts.append(prompt)
        return json.dumps({"action": self.action, "confidence": 0.9,
                           "reason": "Its worker died holding the lease 20 minutes ago; another "
                                     "slot can take it now.",
                           "evidence": ["lease expired"]})


def test_the_shepherd_decides_through_the_one_agent_pipeline(eng, project):  # noqa: F811
    try:
        with eng.begin() as c:
            rid = _run(c, project)
            _job(c, project, rid, lease_in=-20)
        model = _Model("requeue_lost")
        with eng.begin() as c:
            d = stuck_work.diagnose(c, get_settings(), rid, adapter=model)
        assert model.prompts, "the agent was asked"
        assert d["chosen"] == "requeue_lost" and d["acted"]
        assert "worker died" in d["headline"]
        assert d["by"].get("agent") == "Shepherd"
        assert not str(d["by"].get("source", "")).startswith("BOSS's rules")
    finally:
        _clean(eng, project)


def test_the_sweep_finds_what_is_due_and_leaves_working_builds_alone(eng, project):  # noqa: F811
    try:
        with eng.begin() as c:
            stuck = _run(c, project)
            fresh = _run(c, project, age_minutes=2)
            _job(c, project, fresh, status="queued", age_minutes=2)
        with eng.begin() as c:
            due = stuck_work.due(c)
        assert stuck in due and fresh not in due
        out = stuck_work.sweep(eng, get_settings(), max_turns=0)
        assert out["diagnosed"] >= 1
        with eng.begin() as c:
            assert stuck not in stuck_work.due(c)          # diagnosed: not again until it changes
    finally:
        _clean(eng, project)


# ── the operator's standing rule (2026-11-07): one agent, bounded fixes, a reason for every failure ──

def test_the_shepherd_never_fixes_one_build_for_ever(eng, project):  # noqa: F811
    """A build whose job keeps vanishing gets at most MAX_FIXES_PER_ITEM restarts; then the
    Shepherd stops, says why it is not fixed, and raises ONE card."""
    try:
        with eng.begin() as c:
            rid = _run(c, project)
        restarts = 0
        for i in range(stuck_work.MAX_FIXES_PER_ITEM + 3):
            with eng.begin() as c:
                c.execute(text("DELETE FROM jobs WHERE payload->>'run_id' = :r"), {"r": rid})
                d = stuck_work.diagnose(c, get_settings(), rid, use_model=False)
            restarts += 1 if d["chosen"] == "restart_job" and d["acted"] else 0
        assert restarts == stuck_work.MAX_FIXES_PER_ITEM
        assert d["chosen"] == "ask_person" and d["needs_person"]
        assert d["why_not_fixed"] == stuck_work.WHY_NOT["capped"]
        assert d["card_id"] and d["fixes"] == stuck_work.MAX_FIXES_PER_ITEM
        assert d["links"]["log"].endswith("logs=build") and rid in d["links"]["log"]
        with eng.begin() as c:
            cards = c.execute(text("SELECT count(*) FROM decisions WHERE project_id = :p AND "
                                   "recommendation->>'action' = :a AND status = 'pending'"),
                              {"p": project, "a": stuck_work.CARD_ACTION}).scalar()
            question = c.execute(text("SELECT question FROM decisions WHERE id = :d"),
                                 {"d": d["card_id"]}).scalar()
        assert cards == 1
        assert "\n" not in question and len(question) <= 280      # one line: what to do
    finally:
        _clean(eng, project)


#: Every way a build can be not-working, as the database holds it.
def _every_state(c, pid):
    made = {}
    made["failed_without_error"] = _run(c, pid, status="failed")
    made["failed"] = _run(c, pid, status="failed", error="Tests failed: 3 of 40")
    made["blocked"] = _run(c, pid, status="blocked", error="Over the build budget")
    made["awaiting_plan"] = _run(c, pid, status="blocked", phase="awaiting_plan_approval")
    made["person_wait"] = _run(c, pid, status="waiting_on_person", phase="awaiting_grant",
                               ctx={"person_wait": {"builder": "Helm"}})
    made["no_job"] = _run(c, pid)
    r = made["crash_loop"] = _run(c, pid, ctx={"redelivered_attempts": [2, 3, 4, 5]})
    _job(c, pid, r, attempt=6)
    r = made["lost"] = _run(c, pid)
    _job(c, pid, r, lease_in=-30)
    r = made["job_ended"] = _run(c, pid)
    _job(c, pid, r, status="deadlettered", last_error={"message": "boom"})
    r = made["retrying"] = _run(c, pid)
    _job(c, pid, r, status="failed_retryable", run_after_in=3, last_error={"message": "rate limited"})
    r = made["budget"] = _run(c, pid)
    _job(c, pid, r, status="queued", last_error={"kind": "budget_hold", "message": "cap reached"})
    r = made["host"] = _run(c, pid)
    _job(c, pid, r, status="queued", last_error={"code": "host_capacity", "message": "Queued: low memory"})
    r = made["quota"] = _run(c, pid)
    _job(c, pid, r, status="queued", last_error={"code": "resource_allocation_required"})
    r = made["never_taken"] = _run(c, pid)
    _job(c, pid, r, status="queued", age_minutes=40)
    return made


BARE = {"", "queued", "waiting", "failed", "blocked", "stuck", "running"}


def test_every_failed_or_stuck_state_has_a_reason(eng, project):  # noqa: F811
    """THE INVARIANT: no failed, blocked, waiting or stuck build is ever without a reason in
    words -- and once the Shepherd has looked, every one of them has a cause and a next step."""
    try:
        with eng.begin() as c:
            made = _every_state(c, project)
        with eng.begin() as c:
            for name, rid in made.items():
                why = _reason(c, rid)
                assert why is not None, f"{name}: no reason"
                assert why["words"].strip().lower() not in BARE, f"{name}: bare {why['words']!r}"
                assert why["who"] in ("you", "boss", "queue", "stuck"), name
        with eng.begin() as c:
            for name, rid in made.items():
                d = stuck_work.diagnose(c, get_settings(), rid, use_model=False)
                assert d["cause"] and d["next"], f"{name}: diagnosis without a cause or next step"
                assert [s["k"] for s in d["steps"]] == ["stuck", "checked", "cause", "tried", "next"]
                if d["needs_person"]:
                    assert d["why_not_fixed"], f"{name}: handed to a person without saying why"
        with eng.begin() as c:
            # The build that failed with no error now carries the Shepherd's reason.
            run = stuck_work._load(c, made["failed_without_error"])
            assert "no reason was recorded" not in _reason(c, made["failed_without_error"])["words"]
            assert stuck_work.public_diagnosis(run["finalize_ctx"])["cause"]
    finally:
        _clean(eng, project)


def test_the_stalled_work_board_says_fixed_trying_and_needs_a_person(eng, project):  # noqa: F811
    try:
        with eng.begin() as c:
            fixed = _run(c, project)                       # no job: restarted, then it worked
            trying = _run(c, project)                      # worker lost: handed back, not moved yet
            _job(c, project, trying, lease_in=-30)
            person = _run(c, project, status="waiting_on_person", phase="awaiting_grant",
                          ctx={"person_wait": {"builder": "Helm", "decision_id": "dec_x"}})
        with eng.begin() as c:
            for rid in (fixed, trying, person):
                stuck_work.diagnose(c, get_settings(), rid, use_model=False)
            c.execute(text("UPDATE code_task_runs SET status = 'completed', phase = 'done' "
                           "WHERE id = :r"), {"r": fixed})
        with eng.begin() as c:
            b = stuck_work.board(c, project)
        ids = {k: {e["run_id"] for e in b[k]} for k in ("fixed", "trying", "needs_person")}
        assert fixed in ids["fixed"] and trying in ids["trying"] and person in ids["needs_person"]
        assert b["owner"] == "Shepherd" and b["max_fixes"] == stuck_work.MAX_FIXES_PER_ITEM
        np = next(e for e in b["needs_person"] if e["run_id"] == person)
        assert np["why_not_fixed"] and np["next"] and np["links"]["log"]
    finally:
        _clean(eng, project)


def test_one_shepherd_scans_every_project(eng):  # noqa: F811
    """Not per project: one sweep finds stalled builds in two projects at once."""
    a, b = make_project(eng), make_project(eng)
    try:
        with eng.begin() as c:
            ra, rb = _run(c, a), _run(c, b)
        with eng.begin() as c:
            due = stuck_work.due(c, limit=500)
        assert ra in due and rb in due
    finally:
        for p in (a, b):
            _clean(eng, p)
            purge(eng, p)


def test_a_crash_loop_from_the_old_version_gets_one_fresh_try_after_the_update(eng, project, monkeypatch):  # noqa: F811
    """The laptop's case after the install: 29 attempts lost while the OLD BOSS ran. They say
    nothing about the new code, so the Shepherd starts it fresh once -- never 'install the update'
    to someone who just did -- and a second loop on this version is stopped and reported."""
    from boss_engine.services import jobs as jobs_mod

    try:
        with eng.begin() as c:
            rid = _run(c, project, ctx={"redelivered_attempts": list(range(2, 31))})
            old = _job(c, project, rid, attempt=30)
        monkeypatch.setattr(jobs_mod, "PROCESS_STARTED_AT", now_utc() + timedelta(minutes=1))
        with eng.begin() as c:
            d = stuck_work.diagnose(c, get_settings(), rid, use_model=False)
        assert d["chosen"] == "retry_on_this_version" and d["acted"] and not d["needs_person"]
        with eng.begin() as c:
            st = dict(c.execute(text("SELECT id, status FROM jobs WHERE payload->>'run_id' = :r"),
                                {"r": rid}).all())
        assert st[old] == "cancelled" and sorted(st.values()) == ["cancelled", "queued"]
        with eng.begin() as c:
            again = stuck_work.diagnose(c, get_settings(), rid, use_model=False)
        assert again["chosen"] != "retry_on_this_version"
    finally:
        _clean(eng, project)
