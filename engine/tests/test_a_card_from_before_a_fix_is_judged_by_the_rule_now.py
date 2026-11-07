"""Cards raised before a fix are judged by the rule NOW (HelpDesk AI, 2026-11-07, after d40f26cd3).

* "Roll back helpdesk-ai-v3 to c6d4d3a (known fix)?" — raised before the known-good rule — sat
  pending for minutes after it: the reconsider pass skipped the incident squad's mitigation card
  ("its own card"). It is still its own card (never batched, never withdrawn for the mode), but
  it is re-judged for being VALID: a rollback to a release that is no longer the last known-good
  one is withdrawn with a note and the squad fixes forward.
* "(known fix)": INC-1 rolled back c6d4d3a → fa64985, and its lesson is "roll back to the last
  good release". Applying that lesson on a later bad release R rolls back R → fa64985 (the
  CURRENT last known-good release), never to c6d4d3a, even where the deploy history alone would
  have offered it.
* "CVE squad is stuck on esbuild …": the launch failed ("no workflow 'dependency_cve_patch'"),
  so the squad asked a person. A workflow the lead names that the project lacks now falls back
  to the squad's own, and the reconsider pass tries a launch refused once, once more.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import text

from boss_engine.services import incident_squad as squad
from boss_engine.services import incident_squad_demo as demo
from boss_engine.services import stack_deploy
from boss_engine.services.ambient import (asks, board, chief_of_staff as cos, collab, squad_plugins,
                                          squad_turns)

from ._sre_squad_support import eng, project  # noqa: F401
from ._squad_support import (CRITIC, ENGINEER, LEAD, Launchers, ScriptedSquadModel, move,
                             settings, squad_item)

FA = "fa64985777bc4b9ff939707297980127a91c22e5"
C6 = "c6d4d3a59f8676d973d9bcb562b56cdb4fc32a0e"
T0 = datetime(2026, 11, 7, 12, 54, tzinfo=timezone.utc)


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


def _deploys(c, pid, tid, rows):
    """[(name, commit, minutes after T0, rollback_of name)] -> {name: id}; all applied, healthy."""
    ids = {}
    for name, sha, minutes, rollback_of in rows:
        ids[name] = f"sdep_{name}_{pid[-6:]}"
        c.execute(text(
            "INSERT INTO stack_deployments (id, project_id, target_id, repository, ref, "
            "commit_sha, created_by, status, apply_started_at, created_at, rollback_of) VALUES "
            "(:i, :p, :t, 'acme/order-api', :c, :c, 'test', 'healthy', :at, :at, :rb)"),
            {"i": ids[name], "p": pid, "t": tid, "c": sha,
             "at": T0 + timedelta(minutes=minutes), "rb": ids.get(rollback_of)})
    return ids


def _point_incident_at(c, incident_id, tid, deployment_id, **deploy):
    c.execute(text(
        "UPDATE incidents SET finalize_ctx = finalize_ctx || jsonb_build_object("
        "'app', COALESCE(finalize_ctx->'app', '{}'::jsonb) || jsonb_build_object('target_id', "
        "CAST(:t AS TEXT)), 'deploy', COALESCE(finalize_ctx->'deploy', '{}'::jsonb) || "
        "CAST(:d AS JSONB)) WHERE id = :i"),
        {"t": tid, "i": incident_id,
         "d": json.dumps({"stack_deployment_id": deployment_id, **deploy})})


def _proposals(c, item_id):
    return [m["body"] for m in collab.messages(c, item_id, kinds=("proposal",))
            if m["body"]["action"].endswith("rollback")]


def _status(c, decision_id):
    return c.execute(text("SELECT status FROM decisions WHERE id = :d"),
                     {"d": decision_id}).scalar()


# ── 1. the mitigation card is re-judged for being valid ──────────────────────────────────────

def _old_card(c, pid, tid, *, history, serving, proposed):
    """The squad's production card as the code before the known-good rule raised it: the
    effects had no deploy history, so the target was the commit the serving release replaced."""
    prepared = demo.prepare(c, pid)
    ids = _deploys(c, pid, tid, history)
    _point_incident_at(c, prepared["incident_id"], tid, ids[serving],
                       previous_commit_sha=proposed)
    out = squad.route_incident(c, project_id=pid, item_id=prepared["item_id"],
                               mind=demo.demo_mind(), effects=demo.FixtureEffects())
    assert out["stage"] == "needs_you"
    c.execute(text("UPDATE decisions SET recommendation = recommendation || "
                   "'{\"demo\": false}'::jsonb WHERE id = :d"), {"d": out["decision_id"]})
    return prepared, out["decision_id"]


def test_a_rollback_card_to_a_release_rolled_back_from_is_withdrawn(eng, project, target):  # noqa: F811
    with eng.begin() as c:
        # 12:54 fa64985 → 13:13 c6d4d3a (INC-1) → 13:30 rollback to fa64985, serving now.
        prepared, card = _old_card(
            c, project, target, serving="rb", proposed=C6,
            history=[("fa", FA, 0, None), ("c6", C6, 19, None), ("rb", FA, 36, "c6")])
        assert [p["summary"] for p in _proposals(c, prepared["item_id"])][-1].endswith(
            f"to {C6[:7]} (known fix)")
        done = cos.reconsider(c, project, mode="route")
        mitigations = [d for d in squad.ledger.list_decisions(
            c, project, work_item_id=prepared["item_id"]) if d["question"] == squad.Q_MITIGATE]
        note = c.execute(text("SELECT resolution_note FROM decisions WHERE id = :d"),
                         {"d": card}).scalar()
        pending = c.execute(text("SELECT count(*) FROM decisions WHERE project_id = :p AND "
                                 "status = 'pending'"), {"p": project}).scalar()
        assert _status(c, card) == "withdrawn"
    assert [r["decision_id"] for r in done] == [card]
    assert done[0]["settled"][0]["outcome"] == "fix_forward"
    assert "not to a known-good release" in note
    assert mitigations[-1]["chosen"] == "Fix forward"
    assert mitigations[-1]["rationale"].startswith("Rollback withdrawn:")
    assert pending == 0, "nothing moved onto another card: the squad fixes forward"


def test_a_valid_rollback_card_stays_its_own_card(eng, project, target):  # noqa: F811
    with eng.begin() as c:
        # c6d4d3a serving, fa64985 before it: rolling back to fa64985 is still right.
        _, card = _old_card(c, project, target, serving="c6", proposed=FA,
                            history=[("fa", FA, 0, None), ("c6", C6, 19, None)])
        before = c.execute(text("SELECT question, recommendation FROM decisions WHERE id = :d"),
                           {"d": card}).mappings().one()
        assert cos.reconsider(c, project, mode="route") == []
        assert cos.reconsider(c, project, mode="off") == [], "never withdrawn for the mode"
        after = c.execute(text("SELECT status, question, recommendation FROM decisions "
                               "WHERE id = :d"), {"d": card}).mappings().one()
    assert after["status"] == "pending"
    assert (after["question"], after["recommendation"]) == (before["question"],
                                                           before["recommendation"])
    assert "card" not in after["recommendation"], "never batched onto the project's one card"


# ── 2. applying a rollback lesson rolls back to the CURRENT last known-good release ──────────

class _History(demo.FixtureEffects):
    """The fixture's effects, but the rollback target comes from the real deploy history."""

    def rollback_target(self, connection, *, ctx):
        return squad.BossEffects(None).rollback_target(connection, ctx=ctx)


def _lesson_rolled_back_from(c, pid, commit):
    """INC-1's lesson: roll back to the last good release; it rolled back from `commit`."""
    node = demo.learned_before(c, pid)
    c.execute(text("UPDATE kg_nodes SET attrs = attrs || CAST(:a AS JSONB) WHERE id = :n"),
              {"a": json.dumps({"rolled_back_from": [commit], "incident_ref": "INC-1"}),
               "n": node})
    return node


@pytest.mark.parametrize("history", [
    # the rollback is linked: fa64985 → c6d4d3a → rollback to fa64985 → R
    [("fa", FA, 0, None), ("c6", C6, 19, None), ("rb", FA, 36, "c6"), ("r", demo.SERVING, 60,
                                                                       None)],
    # the history alone does not know c6d4d3a was bad (no link, nothing blamed): only the lesson
    [("fa", FA, 0, None), ("c6", C6, 19, None), ("r", demo.SERVING, 60, None)],
], ids=["linked", "lesson_only"])
def test_a_known_fix_rolls_back_to_the_current_known_good_never_the_old_bad_one(
        eng, project, target, history):  # noqa: F811
    with eng.begin() as c:
        demo.learn_first(c, project)
        _lesson_rolled_back_from(c, project, C6[:12])   # older lessons name it by 12 characters
        prepared = demo.prepare(c, project, environment="staging")
        ids = _deploys(c, project, target, history)
        _point_incident_at(c, prepared["incident_id"], target, ids["r"],
                           previous_commit_sha=C6)
        squad.route_incident(c, project_id=project, item_id=prepared["item_id"],
                             mind=demo.demo_mind("staging"), effects=_History())
        [proposal] = _proposals(c, prepared["item_id"])
        learned = [d for d in squad.ledger.list_decisions(
            c, project, work_item_id=prepared["item_id"]) if d["question"] == squad.Q_LEARN]
        console = stack_deploy.get_stack_deployment(c, ids["r"])
    assert proposal["summary"].endswith(f"to {FA[:7]} (known fix)")
    assert proposal["to_commit"] == FA
    assert C6[:7] not in json.dumps(proposal)
    assert f"Never back to {C6[:7]}" in learned[-1]["rationale"]
    # One rule everywhere: the console's button and the redeploy name the same release.
    assert console["rollback_commit"] == FA


def test_a_lesson_names_the_release_it_rolled_back_from():
    attrs = {"detail": {"fix_summary": "Roll back to the last healthy release, then fix"},
             "evidence": [{"type": "metric", "ref": "inc_1"},
                          {"type": "diff", "ref": C6[:12]}]}
    assert squad.precedent_bad_releases(attrs) == [C6[:12]]
    assert squad.precedent_bad_releases({**attrs, "rolled_back_from": [C6]}) == [C6]
    fix_forward = {**attrs, "detail": {"fix_summary": "Fix forward with a regression test"}}
    assert squad.precedent_bad_releases(fix_forward) == []


def test_without_a_history_the_target_is_never_a_blamed_release(eng, project):  # noqa: F811
    with eng.begin() as c:
        prepared = demo.prepare(c, project, environment="staging")
        c.execute(text("UPDATE incidents SET finalize_ctx = jsonb_set(finalize_ctx, '{squad}', "
                       "CAST(:s AS JSONB)) WHERE id = :i"),
                  {"s": json.dumps({"blamed_commits": [demo.PREVIOUS[:12]]}),
                   "i": prepared["incident_id"]})
        squad.route_incident(c, project_id=project, item_id=prepared["item_id"],
                             mind=demo.demo_mind("staging"), effects=demo.FixtureEffects())
        assert _proposals(c, prepared["item_id"]) == []


# ── 3. a stuck squad's launch is tried once more ─────────────────────────────────────────────

def _stuck_cve_squad(c, pid):
    """The esbuild CVE squad of 14:15: the lead names a workflow the project lacks, the squad
    agrees, BOSS cannot start it, the lead asks a person."""
    item_id = squad_item(c, pid)["item"]["id"]
    model = ScriptedSquadModel({
        LEAD: [move("proposal", action="open_draft_pr", summary="Bump esbuild past 0.21.5",
                    verify="full build green", workflow="dependency_cve_patch"),
               move("escalate", reason="no workflow 'dependency_cve_patch': which one opens a "
                                       "draft PR here?",
                    blocked="open the draft PR without a patch workflow",
                    need="name the workflow")],
        ENGINEER: [move("critique", position="agree", summary="Patch level")],
        CRITIC: [move("critique", position="agree", summary="Fine with a full build")]})
    refused = Launchers(refuse="no workflow 'dependency_cve_patch' in this project")
    for who in (LEAD, ENGINEER, CRITIC):
        squad_turns.take_turn(c, settings(), item_id, who, model=model, launchers=refused)
    settled = squad_turns.settle(c, settings(), item_id, end_of_round=True, launchers=refused)
    assert "error" in settled["executed"]
    asked = squad_turns.take_turn(c, settings(), item_id, LEAD, model=model, launchers=refused)
    assert asked["status"] == "needs_you", asked
    card = board.get(c, item_id)["needs_you_decision_id"]
    # Fixed since: the project has the squad's own workflow (fast_fix).
    c.execute(text("INSERT INTO workflow_definitions (id, project_id, workflow_key, name, "
                   "description, created_by) VALUES (:i, :p, 'fast_fix', 'Fast Fix', 'x', 't')"),
              {"i": f"wfd_ff_{pid[-8:]}", "p": pid})
    return item_id, card


def test_a_stuck_launch_is_tried_once_more_and_the_card_goes(eng, project, monkeypatch):  # noqa: F811
    launchers = Launchers()
    monkeypatch.setattr(squad_plugins, "ToolLaunchers", lambda s: launchers)
    with eng.begin() as c:
        item_id, card = _stuck_cve_squad(c, project)
        done = cos.reconsider(c, project, mode="route")
        item = board.get(c, item_id)
        links = [l for l in board.links(c, item_id) if l["kind"] == "workflow_run"]
        again = cos.reconsider(c, project, mode="route")
        status = _status(c, card)
    assert [s["outcome"] for r in done for s in r["settled"]] == ["relaunched"]
    assert status == "withdrawn"
    assert [x["start"]["workflow_key"] for x in launchers.calls] == ["fast_fix"]
    assert launchers.calls[0]["start"]["authority"]["act_id"], "under the squad's decision"
    assert item["lifecycle"] == "in_progress" and len(links) == 1
    assert again == [] and len(launchers.calls) == 1, "once"


def test_a_retry_refused_again_is_the_policys_answer(eng, project, monkeypatch):  # noqa: F811
    launchers = Launchers(refuse="forbidden")
    monkeypatch.setattr(squad_plugins, "ToolLaunchers", lambda s: launchers)
    with eng.begin() as c:
        item_id, card = _stuck_cve_squad(c, project)
        done = cos.reconsider(c, project, mode="route")
        item = board.get(c, item_id)
        assert asks.launch_to_retry(c, item) is None
    assert [s["outcome"] for r in done for s in r["settled"]] == ["stopped_by_policy"]
    assert item["lifecycle"] == "failed" and len(launchers.calls) == 1


def test_a_named_workflow_the_project_lacks_falls_back_to_the_squads_own(eng, project):  # noqa: F811
    with eng.begin() as c:
        assert squad_plugins.workflow_to_start(c, project, "dependency_cve_patch",
                                               "fast_fix") == "dependency_cve_patch"
        c.execute(text("INSERT INTO workflow_definitions (id, project_id, workflow_key, name, "
                       "description, created_by) VALUES (:i, :p, 'fast_fix', 'Fast Fix', 'x', "
                       "'t')"), {"i": f"wfd_ff_{project[-8:]}", "p": project})
        assert squad_plugins.workflow_to_start(c, project, "dependency_cve_patch",
                                               "fast_fix") == "fast_fix"
        assert squad_plugins.workflow_to_start(c, project, "Fast Fix", "x") == "fast_fix"
        assert squad_plugins.workflow_to_start(c, project, None, "fast_fix") == "fast_fix"
