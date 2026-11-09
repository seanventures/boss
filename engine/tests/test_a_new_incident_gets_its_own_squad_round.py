"""The HelpDesk AI staging incidents of 2026-11-07, replayed (project helpdesk, INC-1 → INC-2).

13:13 release c6d4d3a went live; 13:18 AppErrorRate fired (INC-1); the squad rolled back to
fa64985 and resolved it. A p95 blip after the rollback opened a second item on the same app; its
rollback card was stale, the person held it, and it stayed in_progress with no fix running.
14:43 release-1.3 (d3a0740), a NEW bad release, went live; 14:48 AppErrorRate fired again and
opened a NEW incident (INC-2) — but its signal was merged into the stale item ("Seen again ×1"):
no triage, no cause, no rollback, errors for 10+ minutes.

Held here:

* a signal joins a live item only when it is the SAME incident again; a different incident, or a
  release that went live after the item opened (not the item's own rollback), is its own round;
* an incident item with no progress (held, fix forward, nothing running) is closed with a plain
  note and gives back what it absorbed, so the live case recovers on the next tick;
* INC-2's round: cause → "seen before" (INC-1's lesson) → roll back to fa64985, the last
  known-good release, never c6d4d3a or d3a0740 → verified → the lesson reused.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy import text

from boss_engine.config import get_settings
from boss_engine.services import incident_squad as squad
from boss_engine.services import incident_squad_demo as demo
from boss_engine.services.ambient import board, chief_of_staff as cos, collab, guards, ledger
from boss_engine.services.ambient import signals

from ._sre_squad_support import eng, project  # noqa: F401

APP = "helpdesk-ai-v3"
FA = "fa64985777bc4b9ff939707297980127a91c22e5"     # known good
C6 = "c6d4d3a59f8676d973d9bcb562b56cdb4fc32a0e"     # INC-1's bad release
D3 = "d3a07402e6155c2a8e0e02b91e5062316828997b"     # release-1.3, INC-2's bad release
ROUTE = "POST /api/tickets"


def _route_settings():
    return SimpleNamespace(**{**get_settings().__dict__, "ambient_cos_mode": "route",
                              "ambient_cos_model_calls": 0})


def _mind() -> squad.ScriptedMind:
    return squad.ScriptedMind({
        ("agent:operations_engineer", "verdict"): {
            "key": "top", "confidence": 0.9,
            "reason": "The release-branch routing table lookup throws again, as in INC-1."},
        ("agent:sre", "same_lesson"): {"key": "same", "reason": "Same lookup, same throw."},
        ("agent:adversarial_reviewer", "critique"): {
            "key": "agree", "reason": "Staging, a known-good target and an undo."},
    })


class Replay(demo.FixtureEffects):
    """The real known-good rule (BossEffects.rollback_target over the target's deploy history);
    a rollback is recorded as the healthy rollback deployment it would leave."""

    def __init__(self):
        self.rolled: list[str] = []

    def rollback_target(self, connection, *, ctx):
        return squad.BossEffects(None).rollback_target(connection, ctx=ctx)

    def rollback(self, connection, *, project_id, ctx, actor):
        to = self.rollback_target(connection, ctx=ctx)
        assert to, "a rollback runs only to a known-good release"
        self.rolled.append(str(to))
        _deploy(connection, project_id, ctx.target_id, str(to), minutes=0,
                rollback_of=ctx.deploy.get("stack_deployment_id"))
        return {"ok": True, "word": f"Rolled back to {str(to)[:7]}"}

    def start_fix(self, connection, *, project_id, ctx, brief, actor):
        return {"ok": True, "link": ("pull_request", f"paramg/{APP}#{len(self.rolled) + 8}"),
                "word": "Fix PR opened"}


def _deploy(c, pid, tid, sha, *, minutes, rollback_of=None):
    did = f"sdep_{uuid4().hex[:12]}"
    at = datetime.now(timezone.utc) - timedelta(minutes=minutes)
    c.execute(text(
        "INSERT INTO stack_deployments (id, project_id, target_id, repository, ref, commit_sha, "
        "status, rollback_of, apply_started_at, created_by, created_at, finished_at) VALUES "
        "(:i, :p, :t, :r, :c, :c, 'healthy', :rb, :at, 'test', :at, :at)"),
        {"i": did, "p": pid, "t": tid, "r": f"paramg/{APP}", "c": sha, "rb": rollback_of,
         "at": at})
    return did, at


def _incident(c, pid, tid, *, sha, dep, prev, at, kind="error_rate", incident_id=None,
              title="AppErrorRate"):
    """An incident (or a firing attached to an existing one) as app_alerts writes it, and the
    signal the app_alerts adapter records for its alert."""
    inc = incident_id or f"inc_{uuid4().hex[:12]}"
    if incident_id is None:
        fctx = {"app": {"target_id": tid, "app_name": APP, "environment": "staging"},
                "deploy": {"commit_sha": sha, "previous_commit_sha": prev,
                           "stack_deployment_id": dep, "repository": f"paramg/{APP}",
                           "deployed_at": at.isoformat()},
                "alert": {"error_rate": 0.091, "routes": [
                    {"route": ROUTE, "requests": 400, "errors": 36}]},
                "occurrence": datetime.now(timezone.utc).isoformat()}
        c.execute(text(
            "INSERT INTO incidents (id, project_id, dedupe_key, title, service, severity, status, "
            "first_seen, last_seen, finalize_ctx) VALUES (:i, :p, :k, :t, :s, 'critical', 'open',"
            " clock_timestamp(), clock_timestamp(), CAST(:c AS JSONB))"),
            {"i": inc, "p": pid, "k": f"{APP}:{inc}", "t": f"{APP} (staging): {title}",
             "s": APP, "c": json.dumps(fctx)})
        c.execute(text(
            "INSERT INTO incident_evidence (id, incident_id, kind, ref, payload) VALUES "
            "(:id, :i, 'logs', :a, CAST(:p AS JSONB))"),
            {"id": f"ev_{uuid4().hex[:12]}", "i": inc, "a": APP, "p": json.dumps({
                "signatures": [{"exc_type": "KeyError", "location": "app/routing.py:31",
                                "route": ROUTE, "count": 36,
                                "sample": "release-branch routing table lookup"}]})})
    aid = f"alrt_{uuid4().hex[:12]}"
    c.execute(text(
        "INSERT INTO app_alerts (id, project_id, target_id, rule_kind, source_kind, fingerprint, "
        "alertname, labels, annotations, value, threshold, unit, state, active_at, fired_at, "
        "incident_id, release) VALUES (:id, :p, :t, :k, 'rule', :fp, :n, CAST(:l AS JSONB), "
        "CAST(:an AS JSONB), 9.1, 5, '%', 'firing', now(), now(), :i, CAST(:rel AS JSONB))"),
        {"id": aid, "p": pid, "t": tid, "k": kind, "fp": f"fp_{aid}", "n": title,
         "l": json.dumps({"app": APP, "env": "staging"}),
         "an": json.dumps({"summary": f"{title} on {APP}"}), "i": inc,
         "rel": json.dumps({"commit_sha": sha, "deployed_at": at.isoformat()})})
    sig = signals.record_signal(
        c, project_id=pid, source="alert", kind="alert.firing", title=f"{title} on {APP}",
        fingerprint_parts=("alert", f"fp_{aid}"), subject_kind="app", subject_id=tid, app=APP,
        severity="critical", evidence=[{"kind": "alert", "ref": aid}],
        source_ref=f"app_alert:{aid}#1")["signal"]["id"]
    return inc, aid, sig


@pytest.fixture()
def target(eng, project):  # noqa: F811
    tid = f"deptgt_{project[-10:]}"
    with eng.begin() as c:
        c.execute(text("INSERT INTO deployment_targets (id, project_id, app_name, repo, "
                       "environment, target_kind, target_ref, status, created_by) VALUES "
                       "(:t, :p, :a, :r, 'staging', 'stack', :a, 'active', 'test')"),
                  {"t": tid, "p": project, "a": APP, "r": f"paramg/{APP}"})
    yield tid
    with eng.begin() as c:
        c.execute(text("DELETE FROM stack_deployments WHERE target_id = :t"), {"t": tid})
        c.execute(text("UPDATE app_alerts SET target_id = NULL WHERE target_id = :t"),
                  {"t": tid})
        c.execute(text("DELETE FROM deployment_targets WHERE id = :t"), {"t": tid})


def _triage(c, pid, sig):
    cos.triage_project(c, _route_settings(), pid, mode="route", max_model_calls=0)
    return c.execute(text("SELECT work_item_id FROM signals WHERE id = :s"), {"s": sig}).scalar()


def _questions(c, pid, item_id):
    return {d["question"]: d for d in ledger.list_decisions(c, pid, work_item_id=item_id)}


@pytest.fixture()
def quiet(monkeypatch):
    """The stale item's board is append-only: "no move for NO_PROGRESS_MINUTES" (14:17 → 14:48
    live) is replayed by setting the threshold to now."""
    monkeypatch.setattr(squad, "NO_PROGRESS_MINUTES", 0)


def _inc1_then_the_blip(c, pid, tid, effects):
    """INC-1 worked end to end (it leaves the lesson), then the p95 blip's item, held by a
    person with fix forward and nothing running — the stale item of 14:48."""
    _deploy(c, pid, tid, FA, minutes=114)
    c6_dep, c6_at = _deploy(c, pid, tid, C6, minutes=95)
    inc1, _, sig1 = _incident(c, pid, tid, sha=C6, dep=c6_dep, prev=FA, at=c6_at)
    item1 = _triage(c, pid, sig1)
    out1 = squad.route_incident(c, project_id=pid, item_id=item1, mind=_mind(), effects=effects)
    assert out1["stage"] == "resolved" and effects.rolled == [FA]
    lesson = c.execute(text("SELECT id FROM kg_nodes WHERE project_id = :p AND kind = "
                            "'precedent'"), {"p": pid}).scalar()
    assert lesson, "INC-1 leaves a lesson"
    # The blip: a p95 firing the warm-up rule attached to INC-1, on the rolled-back fa64985.
    rb_at = datetime.now(timezone.utc) - timedelta(minutes=70)   # the rollback went live 13:39
    _, _, sig2 = _incident(c, pid, tid, sha=FA, dep=None, prev=None, at=rb_at,
                           kind="latency_p95", incident_id=inc1, title="AppLatencyP95")
    item2 = _triage(c, pid, sig2)
    assert item2 and item2 != item1
    # Its squad found no known-good target (a rollback offers none): fix forward; the fix did
    # not start; the person held it. In progress, held, nothing running.
    class _NoFix(Replay):
        def start_fix(self, connection, **kw):
            return {"ok": False, "word": "Fix waits for a person"}

        def check_recovery(self, connection, *, project_id, ctx):
            return {"status": "unknown", "word": "No traffic to judge"}

    squad.route_incident(c, project_id=pid, item_id=item2, mind=_mind(), effects=_NoFix())
    if board.get(c, item2)["lifecycle"] == "needs_you":
        board.answered(c, item2, actor="person:ceo")
    board.take_over(c, item2, actor="person:ceo", reason="held from Needs you")
    c.execute(text("UPDATE work_items SET lifecycle = 'in_progress' WHERE id = :w"),
              {"w": item2})
    return inc1, item1, item2, lesson


def _release_1_3_and_inc2(c, pid, tid):
    fa_now = c.execute(text("SELECT id FROM stack_deployments WHERE target_id = :t AND "
                            "rollback_of IS NOT NULL"), {"t": tid}).scalar()
    assert fa_now
    d3_dep, d3_at = _deploy(c, pid, tid, D3, minutes=0)
    inc2, _, sig3 = _incident(c, pid, tid, sha=D3, dep=d3_dep, prev=FA, at=d3_at)
    return inc2, sig3


def _assert_inc2_round(c, pid, item3, inc2, lesson, effects, out):
    asked = _questions(c, pid, item3)
    assert out["stage"] == "resolved", out
    for q in (squad.Q_TRIAGE, squad.Q_CAUSE, squad.Q_LEARN, squad.Q_MITIGATE, squad.Q_VERIFY):
        assert q in asked, f"INC-2's round answers {q!r}"
    assert asked[squad.Q_LEARN]["chosen"].startswith("Reuse"), "INC-1's lesson is recalled"
    assert asked[squad.Q_MITIGATE]["action"] == "staging_rollback"
    assert effects.rolled[-1] == FA, "rolled back to the known-good fa64985"
    assert not any(r.startswith((C6[:7], D3[:7])) for r in effects.rolled)
    assert asked[squad.Q_VERIFY]["chosen"] == "Recovered"
    links = {(l["kind"], l["ref"]) for l in board.links(c, item3)}
    assert ("incident", inc2) in links
    reused = c.execute(text("SELECT (attrs->'detail'->>'reuse_count')::int FROM kg_nodes "
                            "WHERE id = :n"), {"n": lesson}).scalar()
    assert reused >= 1, "the lesson's reuse is counted"
    edge = c.execute(text("SELECT 1 FROM kg_edges WHERE project_id = :p AND kind = 'reused' "
                          "AND dst_id = :n"), {"p": pid, "n": lesson}).first()
    assert edge is not None


# ── 1. the fixed path: a new incident never joins the stale item ─────────────────────────────

def test_a_new_incident_on_a_new_release_gets_its_own_round(eng, project, target, quiet):  # noqa: F811
    effects = Replay()
    with eng.begin() as c:
        inc1, item1, item2, lesson = _inc1_then_the_blip(c, project, target, effects)
        inc2, sig3 = _release_1_3_and_inc2(c, project, target)
        item3 = _triage(c, project, sig3)
        assert item3 not in (None, item1, item2), "INC-2 opens its own item"
        seen_again = [m for m in collab.messages(c, item2, kinds=("finding",))
                      if str(m["body"].get("summary", "")).startswith("Seen again")]
        assert seen_again == [], "the stale item never absorbs INC-2"
        out = squad.route_incident(c, project_id=project, item_id=item3, mind=_mind(),
                                   effects=effects)
        _assert_inc2_round(c, project, item3, inc2, lesson, effects, out)
        stale = board.get(c, item2)
        assert stale["lifecycle"] == "resolved"
        assert stale["outcome"].startswith("No progress:")
        assert "(held by a person)" in stale["outcome"] and "no fix running" in stale["outcome"]
        assert "a new incident (INC-2) has its own round" in stale["outcome"]


# ── 2. the live case: the signal was already merged; the tick recovers it ────────────────────

def test_a_signal_merged_into_a_stale_item_recovers_on_the_tick(eng, project, target, quiet):  # noqa: F811
    effects = Replay()
    with eng.begin() as c:
        inc1, item1, item2, lesson = _inc1_then_the_blip(c, project, target, effects)
        inc2, sig3 = _release_1_3_and_inc2(c, project, target)
        # What the old build did at 14:48: joined the app's live item by its fingerprint.
        stale = board.get(c, item2)
        joined = board.open_item(c, None, project_id=project, fingerprint=stale["fingerprint"],
                                 title="Error rate", task_class=stale["task_class"],
                                 signal_ids=[sig3], actor="agent:chief_of_staff")
        assert joined["outcome"] == "joined"
    # The normal tick, after install: no DB edits.
    report = cos.tick(eng, _route_settings(), project_id=project, sweep=False, mode="route",
                      incident_mind=_mind(), incident_effects=effects)
    with eng.begin() as c:
        assert report["rounds"][project][0]["handed"] == [sig3]
        stale = board.get(c, item2)
        assert stale["lifecycle"] == "resolved" and stale["outcome"].startswith("No progress:")
        assert "new round" in stale["outcome"]
        item3 = c.execute(text("SELECT work_item_id FROM signals WHERE id = :s"),
                          {"s": sig3}).scalar()
        assert item3 not in (None, item2)
        worked = [w for w in report["owners"]["worked"] if w["work_item_id"] == item3]
        assert worked and worked[0]["stage"] == "resolved", report["owners"]
        out = {"stage": board.get(c, item3)["lifecycle"]}
        _assert_inc2_round(c, project, item3, inc2, lesson, effects, out)


# ── 3. true repeats still join; a working item is never closed ──────────────────────────────

def test_a_repeat_of_the_same_incident_still_joins(eng, project, target):  # noqa: F811
    with eng.begin() as c:
        dep, at = _deploy(c, project, target, C6, minutes=10)
        inc, _, sig = _incident(c, project, target, sha=C6, dep=dep, prev=FA, at=at)
        item = _triage(c, project, sig)
        _, _, again = _incident(c, project, target, sha=C6, dep=dep, prev=FA, at=at,
                                incident_id=inc)
        assert _triage(c, project, again) == item, "the same incident, the same item"


def test_a_new_incident_beside_a_working_item_leaves_it_working(eng, project, target):  # noqa: F811
    with eng.begin() as c:
        dep, at = _deploy(c, project, target, C6, minutes=40)
        _, _, sig = _incident(c, project, target, sha=C6, dep=dep, prev=FA, at=at)
        first = _triage(c, project, sig)
        c.execute(text("UPDATE work_items SET created_at = now() - interval '30 minutes' "
                       "WHERE id = :w"), {"w": first})
        dep2, at2 = _deploy(c, project, target, D3, minutes=1)
        _, _, sig2 = _incident(c, project, target, sha=D3, dep=dep2, prev=C6, at=at2)
        second = _triage(c, project, sig2)
        assert second not in (None, first)
        assert board.get(c, first)["lifecycle"] in board.LIVE
        assert squad.sweep_rounds(c, project_id=project) == [], "not worked yet: not stalled"
        # Once the first is over, a repeat of the second incident still finds ITS item.
        board.finish(c, first, actor="boss", lifecycle="resolved", outcome="done")
        inc2 = squad.incident_facts(c, [signals.get(c, sig2)])["incident_id"]
        _, _, again = _incident(c, project, target, sha=D3, dep=dep2, prev=C6, at=at2,
                                incident_id=inc2)
        assert _triage(c, project, again) == second


def test_the_items_own_rollback_going_live_is_not_a_new_release(eng, project, target):  # noqa: F811
    with eng.begin() as c:
        dep, at = _deploy(c, project, target, C6, minutes=40)
        _, _, sig = _incident(c, project, target, sha=C6, dep=dep, prev=FA, at=at)
        item = _triage(c, project, sig)
        c.execute(text("UPDATE work_items SET created_at = now() - interval '30 minutes' "
                       "WHERE id = :w"), {"w": item})
        collab.post(c, item, kind="proposal", sender="agent:sre", body={
            "action": "staging_rollback", "summary": f"Roll back {APP} to {FA[:7]}",
            "to_commit": FA, "confidence": 0.9})
        facts = {"incident_id": None, "commit": FA,
                 "deployed_at": datetime.now(timezone.utc).isoformat()}
        assert squad.new_round_reason(c, board.get(c, item), facts) is None
        facts["commit"] = D3
        assert "went live after this item opened" in squad.new_round_reason(
            c, board.get(c, item), facts)


def test_an_incident_never_waits_for_a_concurrency_slot(eng, project):  # noqa: F811
    from boss_engine.services.ambient import policy

    with eng.begin() as c:
        limit = int(policy.settings_for(c, project)["max_concurrent"])
        for i in range(limit):
            c.execute(text("INSERT INTO work_items (id, project_id, subject_type, subject_id, "
                           "title, ambient, lifecycle, task_class, status) VALUES (:i, :p, "
                           "'ambient', :i, 'patch', true, 'in_progress', 'security_patch', "
                           "'active')"), {"i": f"work_busy_{i}_{project[-6:]}", "p": project})
        assert not guards.admit_start(c, project_id=project)["ok"]
        assert not guards.admit_start(c, project_id=project, task_class="security_patch")["ok"]
        assert guards.admit_start(c, project_id=project,
                                  task_class="incident_staging_mitigation")["ok"]
