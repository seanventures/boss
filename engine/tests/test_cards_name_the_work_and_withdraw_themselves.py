"""Only real decisions reach a person, in plain words, with context; stale asks withdraw themselves.

The laptop's Needs you, 2026-11-07 (feat/project-experience @cbbf07dd2), three cards against the
operator's rule:

1. "CVE squad is stuck on esbuild … — let it go on, or take it?" stayed while the build it was
   stuck on (crun_de5396b5a6b04224a158) was planning and calling tools. A "stuck" ask whose
   subject is moving again withdraws itself (asks.verdict → "moving").
2. "Reconcile 4 expired budget reservations from knowledge.embed (knowledge) …" — housekeeping
   (test_expired_reservations_settle_themselves pins the policy: settled at the estimate).
3. "Restart the step, or cancel the build." — no work named, no cause. The Shepherd had
   re-diagnosed the build 30 s after its own restart (the progress clock ignored the restart)
   and, finding no rule, said "BOSS found no recorded cause" and asked. Now: the restart resets
   the clock; an unexplained stall is investigated from the facts and the Shepherd restarts the
   step itself; only after its bounded fixes does it ask, naming the work, the cause and the
   recommended answer; and the card and the page's diagnosis clear the moment the build moves.

Plus the coordinator's walk: an incident an agent acknowledged sat idle for days
(MonitoringRulesDropped on boss-dogfood) — nothing stays acknowledged and idle (acked_idle).
"""
from __future__ import annotations

import json
from datetime import timedelta
from uuid import uuid4

from sqlalchemy import text

from boss_engine.config import get_settings
from boss_engine.services import acked_idle, cleared_causes, decision_words, stuck_work
from boss_engine.services.ambient import board, chief_of_staff as cos
from boss_engine.services.core import now_utc

from ._sre_squad_support import eng, project  # noqa: F401
from .test_a_card_from_before_a_fix_is_judged_by_the_rule_now import _stuck_cve_squad
from .test_stuck_work_is_explained_and_diagnosed_by_an_agent import _clean, _job, _run

TITLE = "Open draft PR bumping esbuild 0.21.5 → ≥0.25.0 (GHSA-67mh-4wv8-2f99)"


def _turn(c, pid, rid, *, minutes_ago=0):
    c.execute(text("INSERT INTO agent_turns (id, project_id, code_task_run_id, direction, "
                   "created_at) VALUES (:i, :p, :r, 'out', :at)"),
              {"i": f"turn_{uuid4().hex[:12]}", "p": pid, "r": rid,
               "at": now_utc() - timedelta(minutes=minutes_ago)})


def _quiet_build(c, pid):
    """The laptop's build: running in design for 268 min, a live lease and heartbeat on a
    worker, no agent turn, its setup events repeating every five minutes."""
    rid = _run(c, pid, phase="design", title=TITLE, age_minutes=268)
    _job(c, pid, rid, status="running", lease_in=4, age_minutes=268)
    for i in range(4):
        for event in ("code_task.effort_chosen", "code_task.github_plane",
                      "code_task.base_branch_resolved"):
            c.execute(text("INSERT INTO events (id, project_id, event_type, actor, entity_type, "
                           "entity_id, data, created_at) VALUES (:i, :p, :e, 'boss', "
                           "'code_task_run', :r, '{}', :at)"),
                      {"i": f"evt_{uuid4().hex[:12]}", "p": pid, "e": event, "r": rid,
                       "at": now_utc() - timedelta(minutes=5 * i + 1)})
    return rid


def _card(c, card_id):
    row = c.execute(text("SELECT status, question, recommendation, resolution_note FROM decisions "
                         "WHERE id = :d"), {"d": card_id}).mappings().one()
    return dict(row)


# ── 3. the Shepherd investigates, restarts the step itself, and asks only after its cap ───────

def test_an_unexplained_stall_is_investigated_and_the_step_restarted_without_asking(eng, project):  # noqa: F811
    try:
        with eng.begin() as c:
            rid = _quiet_build(c, project)
        with eng.begin() as c:
            d = stuck_work.diagnose(c, get_settings(), rid, use_model=False)
        assert d["chosen"] == "restart_step" and d["acted"] and not d["needs_person"]
        assert d["card_id"] is None
        assert "no recorded cause" not in d["cause"]
        assert "starting over" in d["cause"] and "agent" in d["cause"]   # the repeating setup
        assert "base branch resolved" in d["proof"] or "effort chosen" in d["proof"]
        with eng.begin() as c:
            jobs = dict(c.execute(text("SELECT status, count(*) FROM jobs WHERE "
                                       "payload->>'run_id' = :r GROUP BY status"),
                                  {"r": rid}).all())
            # The restart is a fresh start: not "stuck for 268 min" 30 s later (the laptop).
            assert rid not in stuck_work.due(c)
            ev = stuck_work.evidence(c, rid)
        assert jobs == {"cancelled": 1, "queued": 1}
        assert ev["run"]["minutes_without_progress"] < stuck_work.STUCK_MINUTES
    finally:
        _clean(eng, project)


def test_a_live_worker_inside_a_long_step_is_left_alone_until_the_bound(eng, project):  # noqa: F811
    """A test suite running 40 min inside one turn is not stuck: no restart, no card. Past
    LONG_STEP_MINUTES with nothing new, the step is restarted."""
    try:
        with eng.begin() as c:
            rid = _run(c, project, phase="run_tests", title=TITLE, age_minutes=120)
            _job(c, project, rid, status="running", lease_in=4, age_minutes=120)
            _turn(c, project, rid, minutes_ago=40)
        with eng.begin() as c:
            d = stuck_work.diagnose(c, get_settings(), rid, use_model=False)
        assert d["chosen"] == "wait" and not d["acted"] and not d["needs_person"]
        assert "long step" in d["cause"] and d["card_id"] is None
        with eng.begin() as c:
            c.execute(text("UPDATE agent_turns SET created_at = now() - interval '100 minutes' "
                           "WHERE code_task_run_id = :r"), {"r": rid})
            d = stuck_work.diagnose(c, get_settings(), rid, use_model=False)
        assert d["chosen"] == "restart_step" and d["acted"]
    finally:
        _clean(eng, project)


def test_after_its_bounded_fixes_the_card_names_the_work_the_cause_and_the_answer(eng, project):  # noqa: F811
    try:
        with eng.begin() as c:
            rid = _quiet_build(c, project)
            c.execute(text("UPDATE code_task_runs SET finalize_ctx = finalize_ctx || "
                           "jsonb_build_object('shepherd_fixes', CAST(:n AS int)) WHERE id = :r"),
                      {"n": stuck_work.MAX_FIXES_PER_ITEM, "r": rid})
        with eng.begin() as c:
            d = stuck_work.diagnose(c, get_settings(), rid, use_model=False)
            card = _card(c, d["card_id"])
        assert d["needs_person"] and d["card_id"]
        q = card["question"]
        assert q.startswith("Open draft PR bumping esbuild")             # the work, by title
        assert "Recommended" in q and "\n" not in q and len(q) <= 280
        assert decision_words.card_problems(card) == [], card
        rec = card["recommendation"]
        rec = rec if isinstance(rec, dict) else json.loads(rec)
        assert rec["recommended"] and rec["why_a_person"]
    finally:
        _clean(eng, project)


def test_a_diagnosis_and_its_card_clear_the_moment_the_build_moves(eng, project):  # noqa: F811
    try:
        with eng.begin() as c:
            rid = _quiet_build(c, project)
            c.execute(text("UPDATE code_task_runs SET finalize_ctx = finalize_ctx || "
                           "jsonb_build_object('shepherd_fixes', 3) WHERE id = :r"), {"r": rid})
        with eng.begin() as c:
            d = stuck_work.diagnose(c, get_settings(), rid, use_model=False)
        assert d["card_id"]
        with eng.begin() as c:
            _turn(c, project, rid)                                   # an agent took a turn
            run = stuck_work._load(c, rid)
            # The page stops showing it at once, before any sweep.
            assert stuck_work.public_diagnosis(run["finalize_ctx"], connection=c, run=run) is None
        out = stuck_work.sweep(eng, get_settings(), max_turns=0)
        assert out["recovered"] >= 1 and out["withdrawn"] >= 1
        with eng.begin() as c:
            card = _card(c, d["card_id"])
            ctx = stuck_work._load(c, rid)["finalize_ctx"]
            board_now = stuck_work.board(c, project)
        assert card["status"] == "withdrawn"
        assert card["resolution_note"].startswith("Withdrawn: Open draft PR bumping esbuild")
        assert "moving again" in card["resolution_note"]
        assert "diagnosis" not in ctx and ctx["last_diagnosis"]["cleared_because"]
        assert rid not in [e["run_id"] for e in board_now["needs_person"]]
    finally:
        _clean(eng, project)


# ── 1. a squad's "stuck" ask withdraws itself when its subject moves again ───────────────────

def test_a_stuck_squads_card_is_withdrawn_once_its_build_moves(eng, project, monkeypatch):  # noqa: F811
    from boss_engine.services.ambient import squad_plugins

    from ._squad_support import Launchers

    from boss_engine.services.ambient import asks

    launchers = Launchers(refuse="forbidden")
    monkeypatch.setattr(squad_plugins, "ToolLaunchers", lambda s: launchers)
    # Only the moving rule is under test here (a launch retry has its own tests).
    monkeypatch.setattr(asks, "launch_to_retry", lambda c, item: None)
    try:
        with eng.begin() as c:
            item_id, card = _stuck_cve_squad(c, project)
            # The build its launch started, through the workflow run (as on the laptop).
            wfr = f"wfr_t_{uuid4().hex[:10]}"
            rid = _run(c, project, phase="design", title=TITLE, age_minutes=5,
                       ctx={"workflow_run_id": wfr})
            c.execute(text("INSERT INTO work_item_links (work_item_id, kind, ref, project_id, "
                           "created_by) VALUES (:w, 'workflow_run', :r, :p, 'test')"),
                      {"w": item_id, "r": wfr, "p": project})
            # Still not moving: the ask stands.
            assert cos.reconsider(c, project, mode="route") == []
            assert _card(c, card)["status"] == "pending"
            _turn(c, project, rid)
            done = cos.reconsider(c, project, mode="route")
            status = _card(c, card)
            item = board.get(c, item_id)
        assert [s["verdict"] for r in done for s in r.get("settled", [])] == ["moving"]
        assert status["status"] == "withdrawn" and "moving again" in status["resolution_note"]
        assert item["lifecycle"] != "needs_you" and launchers.calls == []
    finally:
        _clean(eng, project)


# ── nothing stays acknowledged and idle ──────────────────────────────────────────────────────

def _incident(c, pid, *, acked_hours, title, alertname, dropped=None, tid=None):
    iid = f"inc_t_{uuid4().hex[:10]}"
    at = now_utc() - timedelta(hours=acked_hours)
    c.execute(text("INSERT INTO incidents (id, project_id, dedupe_key, title, severity, status, "
                   "lifecycle, acked_by, acked_at, first_seen, last_seen, finalize_ctx) VALUES "
                   "(:i, :p, :i, :t, 'warning', 'diagnosing', 'investigating', 'agent:sre', :at, "
                   ":at, now(), '{}'::jsonb)"), {"i": iid, "p": pid, "t": title, "at": at})
    aid = f"alrt_t_{uuid4().hex[:10]}"
    c.execute(text("INSERT INTO app_alerts (id, project_id, target_id, rule_kind, fingerprint, "
                   "alertname, annotations, state, active_at, fired_at, incident_id) VALUES "
                   "(:a, :p, :t, 'monitoring_drift', :a, :n, CAST(:an AS jsonb), 'firing', :at, "
                   ":at, :i)"),
              {"a": aid, "p": pid, "t": tid, "n": alertname, "i": iid, "at": at,
               "an": json.dumps({"dropped": dropped or []})})
    for name in dropped or []:
        c.execute(text("INSERT INTO app_alert_rules (id, project_id, target_id, kind, "
                       "alert, expr, source, managed_by, enabled, dropped_from_manifest_at) VALUES "
                       "(:i, :p, :t, 'custom', :n, 'up == 0', 'yaml', 'code', true, :at)"),
                  {"i": f"arule_{uuid4().hex[:10]}", "p": pid, "t": tid, "n": name, "at": at})
    return iid, aid


def test_an_acknowledged_idle_incident_is_fixed_closed_or_escalated(eng, project):  # noqa: F811
    tid = f"deptgt_t_{project[-8:]}"
    with eng.begin() as c:
        c.execute(text("INSERT INTO deployment_targets (id, project_id, app_name, repo, "
                       "environment, target_kind, target_ref, created_by) VALUES (:t, :p, "
                       "'Order API', 'acme/order-api', 'staging', 'ssh', 'x', 'test')"),
                  {"t": tid, "p": project})
        dropped, dropped_alert = _incident(
            c, project, acked_hours=10, tid=tid, alertname="MonitoringRulesDropped",
            title="Order API (staging): 2 monitoring rules dropped from boss.deploy.yaml",
            dropped=["OrdersListLatencyHigh", "OrderCreateLatencyHigh"])
        unknown, _ = _incident(c, project, acked_hours=6, tid=tid, alertname="DiskAlmostFull",
                               title="Order API (staging): disk almost full")
        fresh, _ = _incident(c, project, acked_hours=1, tid=tid, alertname="DiskAlmostFull",
                             title="Order API (staging): disk filling")
    try:
        stuck_work.sweep(eng, get_settings(), max_turns=0)
        with eng.begin() as c:
            inc = {r["id"]: dict(r) for r in c.execute(text(
                "SELECT id, lifecycle, finalize_ctx FROM incidents WHERE project_id = :p"),
                {"p": project}).mappings()}
            rules = c.execute(text("SELECT source, dropped_from_manifest_at FROM app_alert_rules "
                                   "WHERE target_id = :t"), {"t": tid}).all()
            alert_state = c.execute(text("SELECT state, cleared_reason FROM app_alerts WHERE "
                                         "id = :a"), {"a": dropped_alert}).one()
            card = c.execute(text("SELECT id, question, recommendation, status FROM decisions "
                                  "WHERE project_id = :p AND recommendation->>'action' = :a"),
                             {"p": project, "a": acked_idle.CARD_ACTION}).mappings().all()
            left = acked_idle.due(c)
        # FIX: the dropped rules are BOSS's own now, the alert resolved, the incident closed.
        assert inc[dropped]["lifecycle"] == "resolved"
        assert inc[dropped]["finalize_ctx"]["ack_idle"]["outcome"] == "closed"
        assert "adopted the 2 monitoring rules" in inc[dropped]["finalize_ctx"]["ack_idle"]["why"]
        assert all(r[0] == "console" and r[1] is None for r in rules)
        assert tuple(alert_state) == ("resolved", "rules_adopted")
        # ESCALATE: no safe fix -> one plain card, in words, with the recommended answer.
        assert inc[unknown]["finalize_ctx"]["ack_idle"]["outcome"] == "escalated"
        assert len(card) == 1 and card[0]["question"].startswith("Order API (staging): disk")
        assert decision_words.card_problems(dict(card[0])) == []
        # Within the bound: left alone.
        assert inc[fresh]["lifecycle"] == "investigating" and not inc[fresh]["finalize_ctx"]
        # THE INVARIANT: nothing acknowledged and idle past the bound.
        assert [i["id"] for i in left if i["project_id"] == project] == []
        # A second sweep asks nothing new.
        stuck_work.sweep(eng, get_settings(), max_turns=0)
        with eng.begin() as c:
            assert c.execute(text("SELECT count(*) FROM decisions WHERE project_id = :p AND "
                                  "recommendation->>'action' = :a"),
                             {"p": project, "a": acked_idle.CARD_ACTION}).scalar() == 1
    finally:
        with eng.begin() as c:
            c.execute(text("DELETE FROM monitoring_changes WHERE project_id = :p"), {"p": project})
            c.execute(text("DELETE FROM app_alerts WHERE project_id = :p"), {"p": project})
            c.execute(text("DELETE FROM app_alert_rules WHERE project_id = :p"), {"p": project})
            c.execute(text("DELETE FROM incident_evidence WHERE incident_id IN (SELECT id FROM "
                           "incidents WHERE project_id = :p)"), {"p": project})
            c.execute(text("DELETE FROM incidents WHERE project_id = :p"), {"p": project})
            c.execute(text("DELETE FROM deployment_targets WHERE id = :t"), {"t": tid})


def test_the_stuck_cards_are_a_registered_cleared_cause():
    assert "stuck_work_needs_person" in cleared_causes.STUCK_QUESTIONS
