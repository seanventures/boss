"""The HelpDesk AI staging incident of 2026-11-07, replayed (project helpdesk, INC-1).

13:13 release c6d4d3a deployed to STAGING; 13:18 AppErrorRate fired; the squad blamed the release
and proposed rolling back to fa64985. What went wrong, each now held here:

* The critic answered "concern" ("acceptable, with a named risk") and the squad read it as no
  answer: a person was asked, told "Production needs a person" and "0 of 0 approve" — on staging,
  at A2. A concern now agrees (collab.tally); a staging rollback at A2 asks nobody; when a person
  IS asked, the reason is the real one, in the incident's real environment.
* The person approved "Roll back X to Y?", then had to approve "Deploy …@fa64985?" too. The
  squad's decision is now the rollback deploy's approval (stack_deploy.rollback_approval).
* mitigated_at was the approval (13:30), resolved_at an alert clearing in a quiet minute (13:38)
  while c6d4d3a still served; the rollback went live 13:39:43. mitigated_at is now when the
  rollback went live, resolved_at when the squad verified recovery, and the monitor leaves an
  incident a squad works to the squad.
* After the rollback a p95 blip (820 ms vs 800) in the restart's warm-up opened a new incident
  whose squad proposed rolling back to c6d4d3a. A rollback target is the last KNOWN-GOOD release
  (never one rolled back from, never one an incident blamed, never past a rollback), a firing in
  the warm-up after a verified fix settles into the incident it follows, and a stale rollback
  card is withdrawn by the reconsider path.
* Lessons are named by cause → fix, never "helpdesk-ai-v3 · from INC-1".
"""
from __future__ import annotations

import inspect
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import text

from boss_engine.config import get_settings
from boss_engine.services import incident_response as ir
from boss_engine.services import incident_squad as squad
from boss_engine.services import incident_squad_demo as demo
from boss_engine.services import stack_deploy
from boss_engine.services.ambient import asks, board, collab, ledger
from boss_engine.services.decisions import DecisionService
from boss_engine.services.project_experience.knowledge import learned_label

from ._sre_squad_support import add_person, eng, project  # noqa: F401

T0 = datetime(2026, 11, 7, 13, 0, tzinfo=timezone.utc)


def _mind(env: str, critique: str) -> squad.ScriptedMind:
    script = dict(demo.demo_mind(env).script)
    script[("agent:adversarial_reviewer", "critique")] = {
        "key": critique,
        "reason": "Rollback to a known prior commit in staging is generally safe, but confidence "
                  "is only 0.78 and the diff is unknown."}
    return squad.ScriptedMind(script)


def _mitigations(c, pid, item_id):
    return [d for d in ledger.list_decisions(c, pid, work_item_id=item_id)
            if d["question"] == squad.Q_MITIGATE]


def _cards(c, pid):
    return c.execute(text("SELECT id, question, recommendation FROM decisions "
                          "WHERE project_id = :p ORDER BY created_at"), {"p": pid}).mappings().all()


# ── 1. staging at A2: a critic's concern agrees, nobody is asked ─────────────────────────────

def test_a_staging_rollback_with_a_critics_concern_asks_nobody(eng, project):  # noqa: F811
    with eng.begin() as c:
        prepared = demo.prepare(c, project, environment="staging")
        out = squad.route_incident(c, project_id=project, item_id=prepared["item_id"],
                                   mind=_mind("staging", "concern"),
                                   effects=demo.FixtureEffects())
        rows = _mitigations(c, project, prepared["item_id"])
        cards = _cards(c, project)
    assert out["stage"] == "resolved"
    assert cards == [], "a staging rollback at A2 asks no person"
    assert [(d["decided_by"], d["action"]) for d in rows] == [("agent:sre", "staging_rollback")]
    consensus = rows[0]["consensus"]
    assert consensus["result"] == "approved"
    assert [x["party"] for x in consensus["concerns"]] == ["agent:adversarial_reviewer"]


def test_a_person_asked_hears_the_real_reason_never_production_on_staging(eng, project):  # noqa: F811
    with eng.begin() as c:
        prepared = demo.prepare(c, project, environment="staging")
        out = squad.route_incident(c, project_id=project, item_id=prepared["item_id"],
                                   mind=_mind("staging", "object"),
                                   effects=demo.FixtureEffects())
        rec = next(d for d in _mitigations(c, project, prepared["item_id"])
                   if d["decided_by"].startswith("squad:"))
    assert out["stage"] == "needs_you"
    assert "Production" not in rec["rationale"] and "0 of 0" not in rec["rationale"]
    assert "the critic objects" in rec["rationale"]
    assert "The critic objected: a person decides." in rec["rationale"]


def test_a_production_card_says_production_and_its_votes(eng, project):  # noqa: F811
    with eng.begin() as c:
        prepared = demo.prepare(c, project)
        squad.route_incident(c, project_id=project, item_id=prepared["item_id"],
                             mind=demo.demo_mind(), effects=demo.FixtureEffects())
        rec = next(d for d in _mitigations(c, project, prepared["item_id"])
                   if d["decided_by"].startswith("squad:"))
    assert "2 of 3 approve, 1 dissent" in rec["rationale"]
    assert "A rollback in production needs a person" in rec["rationale"]


def test_incident_classes_are_exempt_from_a_new_projects_grace():
    from boss_engine.services.ambient import policy

    assert set(squad.INCIDENT_CLASSES) <= set(policy._GRACE_EXEMPT)  # noqa: SLF001


# ── 2. one approval for one rollback ─────────────────────────────────────────────────────────

class _Recording(demo.FixtureEffects):
    def __init__(self):
        self.authority = []

    def rollback(self, connection, *, project_id, ctx, actor):
        self.authority.append(dict(ctx.rollback_authority or {}))
        return super().rollback(connection, project_id=project_id, ctx=ctx, actor=actor)


def test_the_persons_answer_is_the_rollbacks_authority(eng, project, monkeypatch):  # noqa: F811
    effects = _Recording()
    monkeypatch.setattr(demo, "FixtureEffects", lambda: effects)
    with eng.begin() as c:
        prepared = demo.prepare(c, project)
        out = squad.route_incident(c, project_id=project, item_id=prepared["item_id"],
                                   mind=demo.demo_mind(), effects=effects)
        add_person(c, project)
        DecisionService(get_settings()).transition(c, out["decision_id"], action="approve",
                                                   actor="ceo", note=None)
        cards = _cards(c, project)
    # (prepare's earlier occurrence is the squad's own staging rollback; this one is the card's)
    assert effects.authority[-1] == {"approved_by": "ceo", "decision_id": out["decision_id"]}
    assert [d["id"] for d in cards] == [out["decision_id"]], "one question, not two"


def test_the_squad_acting_at_a2_is_its_own_rollbacks_authority(eng, project):  # noqa: F811
    effects = _Recording()
    with eng.begin() as c:
        prepared = demo.prepare(c, project, environment="staging")
        squad.route_incident(c, project_id=project, item_id=prepared["item_id"],
                             mind=_mind("staging", "agree"), effects=effects)
    assert effects.authority and effects.authority[-1]["approved_by"] == "agent:sre"
    assert effects.authority[-1]["decision_id"] is None


def test_boss_effects_pass_the_authority_to_the_rollback_deploy(monkeypatch):
    seen = {}

    def fake(engine, settings, *, deployment_id, actor, authority=None):
        seen.update(deployment_id=deployment_id, authority=authority)
        return {"id": "sdep_rb", "commit_sha": "fa64985777bc"}

    monkeypatch.setattr(stack_deploy, "rollback_stack_deploy", fake)

    class _C:
        engine = None

    ctx = squad.Ctx(item={"id": "w"}, signals=[], alert=None, incident=None,
                    fctx={"deploy": {"stack_deployment_id": "sdep_bad"}})
    ctx.rollback_authority = {"approved_by": "ceo", "decision_id": "dec_card"}
    done = squad.BossEffects(None).rollback(_C(), project_id="p", ctx=ctx, actor="person:ceo")
    assert seen == {"deployment_id": "sdep_bad",
                    "authority": {"approved_by": "ceo", "decision_id": "dec_card"}}
    assert done["pending"] and done["deployment_id"] == "sdep_rb"
    assert done["word"] == "Redeploying fa64985"


def test_the_plan_takes_a_rollback_authority_only_for_a_rollback():
    src = inspect.getsource(stack_deploy.plan_stack_deploy)
    assert "if not approval and rollback_of and rollback_authority:" in src
    assert "rollback_approval(" in src
    # The release card's guard and dual control still apply after it.
    assert src.index("rollback_approval(") < src.index("required_approvers_for_changeset")
    assert src.index("rollback_approval(") < src.index("NO POLICY SHIPS REJECTED")


def _card(c, pid, did, action="incident_squad.mitigate", status="approved"):
    import json

    c.execute(text("INSERT INTO decisions (id, project_id, question, status, approved_by, "
                   "recommendation) VALUES (:d, :p, 'Roll back?', :s, 'ceo', CAST(:r AS JSONB))"),
              {"d": did, "p": pid, "s": status, "r": json.dumps({"action": action})})


def test_rollback_approval_holds_only_for_the_known_good_commit(eng, project, monkeypatch):  # noqa: F811
    monkeypatch.setattr(stack_deploy, "_row", lambda c, i, **k: {"project_id": project})
    monkeypatch.setattr(stack_deploy, "get_stack_deployment",
                        lambda c, i: {"rollback_commit": "fa64985", "actions": ["rollback"]})
    with eng.begin() as c:
        _card(c, project, f"dec_sq_{project[-6:]}")
        _card(c, project, f"dec_ot_{project[-6:]}", action="deploy_stack")
        ok = stack_deploy.rollback_approval(
            c, project, {"approved_by": "ceo", "decision_id": f"dec_sq_{project[-6:]}"},
            rollback_of="sdep_bad", commit="fa64985")
        wrong_commit = stack_deploy.rollback_approval(
            c, project, {"approved_by": "ceo", "decision_id": f"dec_sq_{project[-6:]}"},
            rollback_of="sdep_bad", commit="c6d4d3a")
        other_card = stack_deploy.rollback_approval(
            c, project, {"approved_by": "ceo", "decision_id": f"dec_ot_{project[-6:]}"},
            rollback_of="sdep_bad", commit="fa64985")
        agent = stack_deploy.rollback_approval(
            c, project, {"approved_by": "agent:sre", "decision_id": None},
            rollback_of="sdep_bad", commit="fa64985")
        nobody = stack_deploy.rollback_approval(c, project, {}, rollback_of="sdep_bad",
                                                commit="fa64985")
    assert ok == {"source": "incident_squad", "decision_id": f"dec_sq_{project[-6:]}",
                  "approved_by": "ceo"}
    assert wrong_commit is None and other_card is None and nobody is None
    assert agent == {"source": "incident_squad", "decision_id": None, "approved_by": "agent:sre"}


# ── 5. a rollback target is the last known-good release ──────────────────────────────────────

def _h(i, sha, minutes, status="healthy", rollback_of=None, implicated=False):
    return {"id": i, "status": status, "commit_sha": sha, "apply_started_at": T0,
            "created_at": T0 + timedelta(minutes=minutes), "rollback_of": rollback_of,
            "implicated": implicated}


def test_after_rolling_back_from_a_release_there_is_no_rollback_back_to_it():
    # 12:54 fa64985 → 13:13 c6d4d3a (bad) → 13:30 rollback to fa64985.
    hist = [_h("sdep_old", "aaa1111", 0), _h("sdep_63a", "fa64985", 54),
            _h("sdep_b2e", "c6d4d3a", 73), _h("sdep_267", "fa64985", 90, rollback_of="sdep_b2e")]
    assert stack_deploy.rollback_option(hist[-1], hist)["rollback_available"] is False
    # Before the rollback, the bad release rolls back to fa64985.
    before = hist[:3]
    assert stack_deploy.rollback_option(before[-1], before)["rollback_commit"] == "fa64985"


def test_a_release_rolled_back_from_or_blamed_is_never_a_target():
    hist = [_h("d1", "aaa1111", 0), _h("d2", "c6d4d3a", 10),
            _h("d3", "fa64985", 20, rollback_of="d2"), _h("d4", "bbb2222", 30),
            _h("d5", "ccc3333", 40, implicated=True), _h("d6", "ddd4444", 50)]
    got = stack_deploy.rollback_option(hist[-1], hist)
    assert got["rollback_commit"] == "bbb2222"
    blamed_only = [_h("e1", "aaa1111", 0), _h("e2", "c6d4d3a", 10, implicated=True),
                   _h("e3", "fa64985", 20)]
    assert stack_deploy.rollback_option(blamed_only[-1], blamed_only)["rollback_commit"] == \
        "aaa1111"
    nothing = [_h("f1", "c6d4d3a", 0, implicated=True), _h("f2", "fa64985", 10)]
    assert stack_deploy.rollback_option(nothing[-1], nothing)["rollback_available"] is False


def test_no_known_good_target_means_the_squad_fixes_forward(eng, project):  # noqa: F811
    class _NoTarget(demo.FixtureEffects):
        def rollback_target(self, connection, *, ctx):
            return None

        def rollback(self, *a, **k):  # pragma: no cover - must never run
            raise AssertionError("no rollback without a known-good target")

    with eng.begin() as c:
        prepared = demo.prepare(c, project, environment="staging")
        squad.route_incident(c, project_id=project, item_id=prepared["item_id"],
                             mind=_mind("staging", "agree"), effects=_NoTarget())
        rows = _mitigations(c, project, prepared["item_id"])
        proposals = collab.messages(c, prepared["item_id"], kinds=("proposal",))
    assert [d["chosen"] for d in rows] == ["Fix forward"]
    assert "No known-good release" in rows[0]["rationale"]
    assert not any(p["body"]["action"].endswith("rollback") for p in proposals)


def test_a_stale_rollback_card_is_withdrawn_by_reconsider(eng, project, monkeypatch):  # noqa: F811
    with eng.begin() as c:
        prepared = demo.prepare(c, project)
        out = squad.route_incident(c, project_id=project, item_id=prepared["item_id"],
                                   mind=demo.demo_mind(), effects=demo.FixtureEffects())
        card = c.execute(text("SELECT * FROM decisions WHERE id = :d"),
                         {"d": out["decision_id"]}).mappings().one()
        rec = {**dict(card["recommendation"]), "demo": False}
        item = board.get(c, prepared["item_id"])
        # The demo card: not judged (no BOSS deployment behind it).
        assert squad.rollback_still_known_good(c, item, {**rec, "demo": True}) is None
        c.execute(text("UPDATE incidents SET finalize_ctx = jsonb_set(finalize_ctx, "
                       "'{deploy,stack_deployment_id}', '\"sdep_x\"') WHERE id = :i"),
                  {"i": prepared["incident_id"]})
        monkeypatch.setattr(squad.BossEffects, "rollback_target",
                            lambda self, connection, *, ctx: demo.PREVIOUS)
        assert squad.rollback_still_known_good(c, item, rec) is True
        monkeypatch.setattr(squad.BossEffects, "rollback_target",
                            lambda self, connection, *, ctx: None)
        assert squad.rollback_still_known_good(c, item, rec) is False
        live_card = {**dict(card), "recommendation": rec}
        entry = asks.legacy_entry(live_card)
        verdict, got = asks.verdict(c, live_card, entry, "on")
        assert verdict == "stale_rollback"
        settled = asks.settle(c, project, card["id"], got, entry, verdict)
        rows = _mitigations(c, project, prepared["item_id"])
    assert settled == "fix_forward"
    assert rows[-1]["decided_by"] == "agent:sre" and rows[-1]["chosen"] == "Fix forward"
    assert rows[-1]["rationale"].startswith("Rollback withdrawn: the rollback it proposed")


# ── 3/4. the real clock ──────────────────────────────────────────────────────────────────────

@pytest.fixture()
def target(eng, project):  # noqa: F811
    tid = f"deptgt_{project[-10:]}"
    with eng.begin() as c:
        c.execute(text("INSERT INTO deployment_targets (id, project_id, app_name, repo, "
                       "environment, target_kind, target_ref, created_by) VALUES (:t, :p, "
                       "'order-api', 'acme/order-api', 'staging', 'ssh', 'x', 'test')"),
                  {"t": tid, "p": project})
    yield tid
    with eng.begin() as c:
        c.execute(text("DELETE FROM stack_deployments WHERE target_id = :t"), {"t": tid})
        c.execute(text("DELETE FROM deployment_targets WHERE id = :t"), {"t": tid})


class _Pending(demo.FixtureEffects):
    """BossEffects' shape: the rollback is a planned redeploy that goes live later."""

    def __init__(self, deployment_id):
        self.deployment_id = deployment_id

    def rollback(self, connection, *, project_id, ctx, actor):
        return {"ok": True, "pending": True, "deployment_id": self.deployment_id,
                "word": f"Redeploying {demo.PREVIOUS[:7]}"}


def test_mitigated_when_live_and_resolved_when_verified_in_order(eng, project, target):  # noqa: F811
    dep = f"sdep_rb_{project[-8:]}"
    with eng.begin() as c:
        c.execute(text("INSERT INTO stack_deployments (id, project_id, target_id, repository, "
                       "ref, commit_sha, created_by, status) VALUES (:d, :p, :t, 'acme/order-api',"
                       " :c, :c, 'test', 'awaiting_approval')"),
                  {"d": dep, "p": project, "t": target, "c": demo.PREVIOUS})
        prepared = demo.prepare(c, project, environment="staging")
        effects = _Pending(dep)
        out = squad.route_incident(c, project_id=project, item_id=prepared["item_id"],
                                   mind=_mind("staging", "agree"), effects=effects)
        inc = c.execute(text("SELECT mitigated_at, resolved_at, status FROM incidents "
                             "WHERE id = :i"), {"i": prepared["incident_id"]}).mappings().one()
        assert out["stage"] == "verifying"
        assert inc["mitigated_at"] is None and inc["resolved_at"] is None, \
            "decided is not mitigated: the rollback is not live yet"
        # The monitor's alert-clear leaves it to the squad while the squad works it.
        assert ir.squad_working(c, prepared["incident_id"]) == prepared["item_id"]
        # Still not live on the next tick: still waiting, still not mitigated.
        squad.tick(c, project_id=project, mind=_mind("staging", "agree"), effects=effects)
        assert board.get(c, prepared["item_id"])["lifecycle"] not in board.TERMINAL
        live = c.execute(text("UPDATE stack_deployments SET status = 'healthy', "
                              "finished_at = clock_timestamp() WHERE id = :d "
                              "RETURNING finished_at"), {"d": dep}).scalar()
        squad.tick(c, project_id=project, mind=_mind("staging", "agree"), effects=effects)
        inc = c.execute(text("SELECT first_seen, mitigated_at, resolved_at, status FROM "
                             "incidents WHERE id = :i"),
                        {"i": prepared["incident_id"]}).mappings().one()
        report = ir.report(c, project, prepared["incident_id"], persist=False)
    assert inc["status"] == "resolved"
    assert inc["mitigated_at"] == live
    assert inc["first_seen"] <= inc["mitigated_at"] <= inc["resolved_at"]
    assert report["mitigated_at"] is not None
    assert any(t["kind"] == "mitigated" for t in report["timeline"])


def test_the_monitor_never_resolves_an_incident_a_squad_works():
    src = inspect.getsource(__import__(
        "boss_engine.services.app_alerts", fromlist=["x"])._maybe_resolve_incidents)
    assert "squad_working(connection, inc_id)" in src


def test_a_reopened_incident_starts_its_own_clock():
    src = inspect.getsource(__import__("boss_engine.services.app_alerts", fromlist=["x"])._group)
    for col in ("acked_at", "mitigated_at", "resolved_at"):
        assert f" {col} = CASE WHEN status IN ('resolved', 'dismissed') THEN NULL" in src


def test_the_squad_closes_with_its_own_verify_time_not_an_earlier_stamp(eng, project):  # noqa: F811
    with eng.begin() as c:
        prepared = demo.prepare(c, project, environment="staging")
        early = datetime.now(timezone.utc) - timedelta(hours=1)
        c.execute(text("UPDATE incidents SET resolved_at = :t WHERE id = :i"),
                  {"t": early, "i": prepared["incident_id"]})
        squad.route_incident(c, project_id=project, item_id=prepared["item_id"],
                             mind=_mind("staging", "agree"), effects=demo.FixtureEffects())
        inc = c.execute(text("SELECT mitigated_at, resolved_at FROM incidents WHERE id = :i"),
                        {"i": prepared["incident_id"]}).mappings().one()
    assert inc["resolved_at"] > early
    assert inc["mitigated_at"] <= inc["resolved_at"]


def test_the_flow_diagram_carries_the_real_times(eng, project):  # noqa: F811
    from boss_engine.services.project_experience import health

    with eng.begin() as c:
        prepared = demo.prepare(c, project, environment="staging")
        squad.route_incident(c, project_id=project, item_id=prepared["item_id"],
                             mind=_mind("staging", "agree"), effects=demo.FixtureEffects())
        rows = health.incidents(c, project)
        inc = c.execute(text("SELECT mitigated_at, resolved_at FROM incidents WHERE id = :i"),
                        {"i": prepared["incident_id"]}).mappings().one()
    row = next(r for r in rows if r["id"] == prepared["item_id"])
    by = {n.get("type") or n["label"]: n for n in row["mini"]}
    assert by["fix"]["at"] == inc["mitigated_at"].isoformat().replace("+00:00", "Z") or \
        by["fix"]["at"].startswith(inc["mitigated_at"].strftime("%Y-%m-%dT%H:%M"))
    assert by["Recovered"]["at"].startswith(inc["resolved_at"].strftime("%Y-%m-%dT%H:%M"))
    assert row["ended_at"].startswith(inc["resolved_at"].strftime("%Y-%m-%dT%H:%M"))


def test_the_report_names_when_it_was_mitigated():
    md = ir.report_markdown({"title": "t", "service": "s", "opened_at": "13:18",
                             "mitigated_at": "13:39:43", "resolved_at": "13:41:45",
                             "minutes_to_recover": 23.6, "verification": {}, "cost": {}})
    assert "- **Mitigated:** 13:39:43" in md and "- **Resolved:** 13:41:45" in md


# ── 6. the warm-up after a verified fix ──────────────────────────────────────────────────────

def test_a_blip_in_the_warm_up_after_the_rollback_settles_into_the_incident():
    live = datetime(2026, 11, 7, 13, 39, 43, tzinfo=timezone.utc)
    inc = {"status": "resolved", "finalize_ctx": {"verification": {
        "state": "recovered", "since": live.isoformat(), "at": "2026-11-07T13:41:45+00:00"}}}
    latency = {"kind": "latency_p95", "params": {"window_minutes": 5}}
    until = ir.settling_until(inc, latency)
    blip = datetime(2026, 11, 7, 13, 43, 11, tzinfo=timezone.utc)
    assert until == live + timedelta(minutes=5) + ir.DEPLOY_WARMUP
    assert blip < until, "the 13:43 p95 blip is held with INC-1, not a new incident"
    assert ir.rule_settle_window(latency) == timedelta(minutes=5)


# ── 7. lessons are named by cause and fix ────────────────────────────────────────────────────

def test_a_lesson_is_named_by_its_cause_and_fix_never_the_app():
    title = squad.lesson_title(
        {"means": "A release-branch routing table lookup threw on an unknown queue in "
                  "helpdesk-ai-v3. The rest."}, "POST /api/tickets 5xx", True,
        app="helpdesk-ai-v3")
    assert title == ("A release-branch routing table lookup threw on an unknown queue → roll "
                     "back to the last good release")
    old = learned_label("precedent", "helpdesk-ai-v3: Release c6d4d3a changed the serving "
                        "release", {"detail": {
                            "cause_summary": "Release c6d4d3a changed the serving release",
                            "fix_summary": "Roll back to the last healthy release, then fix"}})
    assert "helpdesk" not in old.lower() and old.endswith("→ roll back")
