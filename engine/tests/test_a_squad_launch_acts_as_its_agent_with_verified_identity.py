"""An always-on squad launches work as its agent, with a verified identity (BOSSAPP-908).

On the laptop the helpdesk security squads (vite, undici) had every launch refused twice —
"Could not start dependency_cve: forbidden" — even after a person approved the card. Cause:
`ToolLaunchers._invoke` called the tool registry with no identity of its own, so the registry
decided the call as whoever was on the thread: the worker's `sys_worker` on the ambient tick,
which holds only worker:lease / spend:reserve / secret:lease.

Now a launch acts as the squad lead's Passport, verified by authz from the ledger, live:

* a person approved the card -> on behalf of that person (who must hold the key there), the
  decision's act id as lineage; the audit row names the agent AND the person;
* the class level lets the squad act (A2, maker-checker agreed) -> the agent alone, under the
  project's autonomy, its Passport sponsor accountable;
* no grant (the class level wants a person and none approved) -> refused;
* another project -> refused; a key other than the two launch keys -> refused.
"""
from __future__ import annotations

from uuid import uuid4

import pytest
from sqlalchemy import text

from boss_engine.config import get_settings
from boss_engine.identity import ambient_launch, authz, passports, users
from boss_engine.identity.principal import (AuthContext, AuthMethod, Principal, PrincipalKind,
                                            PrincipalStatus)
from boss_engine.identity.scope import ResolvedScope
from boss_engine.services import workflows
from boss_engine.services.ambient import ledger, policy, squad_plugins, squad_turns, squads
from boss_engine.tools import registry as registry_module

from ._ambient_support import eng, make_project, project, purge  # noqa: F401
from ._squad_support import CVE_ROLES, add_roles, settings, squad_item
from .test_a_squad_hands_work_to_a_workflow_and_gets_the_result_back import _decide_a_draft_pr

ORG = "org_amb"
WORKER = AuthContext(Principal("sys_worker", PrincipalKind.SYSTEM, ORG, PrincipalStatus.ACTIVE, "BOSS"),
                     AuthMethod.SYSTEM)


def _publish(c, pid: str, key: str = "squad_demo_build") -> None:
    definition = workflows.create_definition(c, project_id=pid, workflow_key=key, name=key,
                                             description=None, actor="test", namespace="product")
    version = workflows.publish_version(
        c, project_id=pid, definition_id=definition["id"], actor="test",
        spec={"entry_step": "m", "steps": [
            {"id": "m", "kind": "capability",
             "binding": {"capability": "meeting", "inputs": {"directive": "Fix it"}},
             "next": []}]})
    c.execute(text("UPDATE workflow_definitions SET active_version_id = :v WHERE id = :d"),
              {"v": version["id"], "d": definition["id"]})


def _staff(c, pid: str, *, person: str, agent: str) -> None:
    add_roles(c, pid, CVE_ROLES)
    c.execute(text("UPDATE roles SET agent_id = :a WHERE project_id = :p AND role_key = "
                   "'security_reviewer'"), {"a": agent, "p": pid})
    _publish(c, pid)


@pytest.fixture()
def world(eng, project, monkeypatch):  # noqa: F811
    s = get_settings()
    monkeypatch.setattr(s, "authz_mode", "enforce")
    monkeypatch.setattr(s, "authz_router_modes", "")
    # Whether the project can finish the run (a connected model, a repo) is the start's own
    # preflight, with its own suite; here every start is ready, so a refusal is identity's.
    from boss_engine.services import workflow_readiness

    monkeypatch.setattr(workflow_readiness, "readiness_of_version",
                        lambda *a, **k: {"applies": False, "missing": []})
    sfx = uuid4().hex[:8]
    w = {"person": f"usr_amb_{sfx}", "outsider": f"usr_out_{sfx}", "agent": f"agt_sec{sfx}",
         "project": project}
    with eng.begin() as c:
        for pid, name in ((w["person"], "Param"), (w["outsider"], "Outsider")):
            c.execute(text("INSERT INTO identity_principals (id, org_id, kind, status, display_name) "
                           "VALUES (:i, :o, 'user', 'active', :n)"), {"i": pid, "o": ORG, "n": name})
        users.add_binding(c, org_id=ORG, subject_id=w["person"], role_key="operator",
                          scope_type="project", scope_id=project, granted_by="test")
        passports.ensure_passport(org_id=ORG, agent_id=w["agent"], role_key="security_reviewer",
                                  display_name="Security reviewer", sponsor_principal_id=w["person"],
                                  db=c)
        _staff(c, project, person=w["person"], agent=w["agent"])
    # The ambient tick runs inside the worker's request: sys_worker is on the thread.
    token = registry_module.CURRENT_TOOL_AUTH.set(WORKER)
    try:
        yield w
    finally:
        registry_module.CURRENT_TOOL_AUTH.reset(token)
        with eng.begin() as c:
            c.execute(text("DELETE FROM identity_role_bindings WHERE subject_id IN (:a, :b)"),
                      {"a": w["person"], "b": w["outsider"]})


def _launch_events(c, item_id: str) -> list[dict]:
    return [dict(r) for r in c.execute(text(
        "SELECT actor_principal_id, actor_kind, on_behalf_of, outcome, details "
        "FROM identity_security_events WHERE event_type = 'agent.ambient_launch' "
        "AND target_id = :w ORDER BY seq"), {"w": item_id}).mappings()]


def _person_decision(c, pid: str, item: dict, person: str, action: str = "open_draft_pr") -> dict:
    return ledger.record_decision(
        c, project_id=pid, decided_by=f"person:{person}", question="Open the draft PR?",
        chosen=action, options=[{"key": action, "label": "Open"}, {"key": "hold", "label": "Hold"}],
        rationale="Approved.", reversibility="reversible", work_item_id=item["id"],
        task_class=item["task_class"], action=action, undo={"action": "close_pr"})


def _proposal() -> dict:
    return {"body": {"action": "open_draft_pr", "summary": "Bump undici to 6.21.1",
                     "workflow": "squad_demo_build"}}


def test_the_cause_the_worker_on_the_thread_holds_no_launch_key(world):
    """What every launch was decided as before: sys_worker, refused workflow_run:start."""
    decision = authz.check(WORKER, "workflow_run:start", ResolvedScope.in_project(ORG, world["project"]))
    assert decision.allowed is False


def test_a_person_approved_launch_runs_on_their_behalf_and_is_audited(eng, world):
    pid = world["project"]
    with eng.begin() as c:
        got = squad_item(c, pid)
        item = got["item"]
        decision = _person_decision(c, pid, item, world["person"])
        squad = squads.roster(c, item["id"])
        out = squad_plugins.launch_workflow(c, item, squad=squad, proposal=_proposal(),
                                            decision=decision,
                                            launchers=squad_plugins.ToolLaunchers(settings()))
        events = _launch_events(c, item["id"])
    assert out.get("kind") == "workflow_run" and out["ref"].startswith("wfr"), out
    assert len(events) == 1
    e = events[0]
    assert (e["actor_principal_id"], e["actor_kind"], e["on_behalf_of"], e["outcome"]) == (
        world["agent"], "agent", world["person"], "success")
    assert e["details"]["decision"] == decision["act_id"]
    assert e["details"]["basis"] == ambient_launch.PERSON_APPROVAL
    assert e["details"]["ref"] == out["ref"]


def test_at_a2_the_squad_launches_as_its_agent_under_the_class_level(eng, world):
    pid = world["project"]
    with eng.begin() as c:
        policy.set_level(c, pid, "security_patch", actor="person:owner", level="A2")
        item_id = squad_item(c, pid)["item"]["id"]
        settled = _decide_a_draft_pr(c, item_id, squad_plugins.ToolLaunchers(settings()))
        events = _launch_events(c, item_id)
    assert settled["state"] == "decided", settled
    assert settled["executed"].get("kind") == "workflow_run", settled["executed"]
    assert len(events) == 1
    e = events[0]
    assert (e["actor_principal_id"], e["on_behalf_of"], e["outcome"]) == (world["agent"], None,
                                                                         "success")
    assert e["details"]["basis"] == ambient_launch.AUTONOMY
    assert e["details"]["decision"] == settled["act_id"]


def test_without_a_grant_the_launch_is_refused(eng, world):
    """The squad decided at A2, then a person lowered the class to A1 before the launch: the
    class level now wants a person, none approved, and the squad's own decision grants nothing
    (checked live at the launch, not when it was decided)."""
    pid = world["project"]

    class Lowered(squad_plugins.ToolLaunchers):
        def start(self, connection, **kw):
            policy.set_level(connection, pid, "security_patch", actor="person:owner", level="A1")
            return super().start(connection, **kw)

    with eng.begin() as c:
        policy.set_level(c, pid, "security_patch", actor="person:owner", level="A2")
        item_id = squad_item(c, pid)["item"]["id"]
        settled = _decide_a_draft_pr(c, item_id, Lowered(settings()))
        events = _launch_events(c, item_id)
        runs = c.execute(text("SELECT count(*) FROM workflow_runs WHERE project_id = :p"),
                         {"p": pid}).scalar()
    assert settled["state"] == "decided" and settled["executed"]["error"] == "forbidden", settled
    assert runs == 0
    assert [e["outcome"] for e in events] == ["denied"]


def test_a_person_without_the_key_grants_nothing(eng, world):
    """On behalf of a person who cannot start a run there: refused, never widened by the agent."""
    pid = world["project"]
    with eng.begin() as c:
        got = squad_item(c, pid)
        item = got["item"]
        decision = _person_decision(c, pid, item, world["outsider"])
        out = squad_plugins.launch_workflow(c, item, squad=squads.roster(c, item["id"]),
                                            proposal=_proposal(), decision=decision,
                                            launchers=squad_plugins.ToolLaunchers(settings()))
    assert out["error"] == "forbidden", out


def test_a_launch_into_a_foreign_project_is_refused(eng, world):
    """The authority is bound to the work item's project: naming another refuses."""
    pid = world["project"]
    other = make_project(eng)
    try:
        with eng.begin() as c:
            users.add_binding(c, org_id=ORG, subject_id=world["person"], role_key="operator",
                              scope_type="project", scope_id=other, granted_by="test")
            _publish(c, other)
            got = squad_item(c, pid)
            item = got["item"]
            decision = _person_decision(c, pid, item, world["person"])
            ctx = ambient_launch.launch_context(c, project_id=pid, work_item_id=item["id"],
                                                act_id=decision["act_id"],
                                                role_key="security_reviewer")
            here = authz.check(ctx, "workflow_run:start", ResolvedScope.in_project(ORG, pid))
            there = authz.check(ctx, "workflow_run:start", ResolvedScope.in_project(ORG, other))
            other_key = authz.check(ctx, "deployment:create", ResolvedScope.in_project(ORG, pid))
            from boss_engine.tools.builtin import build_default_registry
            from boss_engine.context import EngineContext

            tool = build_default_registry().invoke(
                "start_workflow", EngineContext(settings=settings(), engine=c.engine),
                project_id=other, actor="security_reviewer", operator_confirmed=True, auth=ctx,
                args={"workflow_key": "squad_demo_build", "directive": "d", "title": "t"})
    finally:
        purge(eng, other)
    assert here.allowed is True
    assert there.allowed is False and there.reason.value == "out_of_scope"
    assert other_key.allowed is False
    assert tool["error"] == "forbidden" and not tool.get("run_id")


def test_a_launch_with_no_decision_has_no_identity(eng, world):
    with eng.begin() as c:
        item = squad_item(c, world["project"])["item"]
        got = squad_plugins.ToolLaunchers(settings()).start(
            c, project_id=world["project"], actor="security_reviewer", workflow_key="squad_demo_build",
            directive="d", title="t", inputs={}, authority={"work_item_id": item["id"]})
    assert "no verified identity" in got["error"]


def test_a_person_approval_carried_by_the_squad_round_launches(eng, world):
    """The card's path: a person answers, the squad's next round carries it out."""
    pid = world["project"]
    with eng.begin() as c:
        policy.set_level(c, pid, "security_patch", actor="person:owner", level="A1")
        item_id = squad_item(c, pid)["item"]["id"]
        asked = _decide_a_draft_pr(c, item_id, squad_plugins.ToolLaunchers(settings()))
        assert asked["state"] == "needs_you", asked
        squad_turns.person_answered(c, item_id, actor=world["person"], approve=True,
                                    rationale="Go")
        done = squad_turns.execute_person_approvals(
            c, item_id, launchers=squad_plugins.ToolLaunchers(settings()))
        events = _launch_events(c, item_id)
    assert done and done[0].get("kind") == "workflow_run", done
    assert events[-1]["on_behalf_of"] == world["person"] and events[-1]["outcome"] == "success"
