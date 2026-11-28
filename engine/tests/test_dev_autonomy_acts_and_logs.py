# ruff: noqa: F811 — pytest fixtures imported from test_workflow_capability_runner
"""Dev autonomy decides development cards itself, with the same follow-on a person's Approve runs,
and logs who decided and why (BOSSAPP-1441).

The operator, 2026-11-23: "for local development I dont need to validate just make the output..
then human can validate it." Production, credentials, spend past the budget and the stops a person
set still wait; everything else is decided by the agent, named, with its undo.
"""
from __future__ import annotations

import json
import uuid

import pytest
from sqlalchemy import text

from boss_engine.config import get_settings
from boss_engine.services import autonomy_gate, autonomy_policy
from boss_engine.services import workflow_capability_runner as runner

from .test_workflow_capability_runner import (  # noqa: F401
    Ctx, RecordingRegistry, _publish, _start, _steps, eng, project)


@pytest.fixture(autouse=True)
def _jobs_go_with_the_project(eng, project):
    """A decided card's follow-on queues real jobs; they go before the project does."""
    yield
    with eng.begin() as c:
        for table in ("jobs", "autonomy_policies"):
            column = "scope_id" if table == "autonomy_policies" else "project_id"
            c.execute(text(f"DELETE FROM {table} WHERE {column} = :p"), {"p": project})


def _card(c, project_id, rec, *, risk_tier=2, question=None):
    from boss_engine.services.core import create_blocking_decision

    return create_blocking_decision(
        c, project_id, None, question or f"q {uuid.uuid4().hex[:8]}", rec, {}, "test",
        risk_tier=risk_tier)


def _row(c, decision_id):
    row = dict(c.execute(text("SELECT * FROM decisions WHERE id = :d"),
                         {"d": decision_id}).mappings().one())
    rec = row["recommendation"]
    row["recommendation"] = json.loads(rec) if isinstance(rec, str) else rec
    return row


def _target(c, project_id, environment, *, tier=None, ref="deploy@203.0.113.40:22"):
    target_id = f"dt_{uuid.uuid4().hex[:10]}"
    c.execute(text(
        "INSERT INTO deployment_targets (id, project_id, app_name, repo, environment, target_kind, "
        "target_ref, created_by, status, tier) VALUES (:i, :p, :a, 'o/r', :e, 'ssh_host', :ref, "
        "'test', 'active', :t)"),
        {"i": target_id, "p": project_id, "a": f"app_{target_id}", "e": environment, "ref": ref,
         "t": tier})
    return target_id


def test_a_development_card_is_decided_by_a_named_agent_and_logged(eng, project):
    settings = get_settings()
    with eng.begin() as c:
        decision_id = _card(c, project, {"action": "decide", "summary": "pick the next step"})
        stamped = _row(c, decision_id)["recommendation"]["autonomy"]
        assert stamped["route"] == "act" and stamped["human_reviewed"] is False
        result = autonomy_gate.decide(c, settings, decision_id)
        row = _row(c, decision_id)
        event = c.execute(text(
            "SELECT data FROM events WHERE entity_id = :d AND event_type = :e"),
            {"d": decision_id, "e": autonomy_gate.EVENT}).scalar()
    assert result["decided"] is True
    assert row["status"] == "approved"
    assert row["approved_by"] == "autonomy:dev:product_manager"
    assert "Decided by Product manager under dev autonomy" in row["resolution_note"]
    data = event if isinstance(event, dict) else json.loads(event)
    assert data["undo_kind"] and data["agent"] == "product_manager" and data["evidence"]


def test_an_approved_gate_releases_its_step_the_way_a_persons_answer_does(eng, project,
                                                                         monkeypatch):
    """Same follow-on: the spec_review gate decided by dev autonomy releases its step."""
    monkeypatch.setattr(runner, "_tool_registry", lambda: RecordingRegistry([]))
    settings = get_settings()
    with eng.begin() as c:
        _, version = _publish(c, project, "auto_gate", [
            {"id": "g", "kind": "capability",
             "binding": {"capability": "spec_review", "inputs": {}}, "next": []},
        ], "g")
        run = _start(c, project, version["id"])
        runner.tick(c, Ctx(eng), project_id=project)
        decision_id = c.execute(text(
            "SELECT decision_id FROM workflow_step_runs WHERE workflow_run_id = :r"),
            {"r": run["id"]}).scalar()
        assert autonomy_gate.decide(c, settings, decision_id)["decided"] is True
        runner.tick(c, Ctx(eng), project_id=project)
        steps = _steps(c, run["id"])
    assert steps["g"]["status"] == "succeeded", steps["g"]


def test_production_and_unknown_targets_still_ask(eng, project):
    settings = get_settings()
    with eng.begin() as c:
        prod = _target(c, project, "production")
        unknown = _target(c, project, "eu1")
        local = _target(c, project, "eu1", ref="deploy@localhost:22")
        staging = _target(c, project, "staging", tier="staging")
        answers = {}
        for name, target in (("prod", prod), ("unknown", unknown), ("local", local),
                             ("staging", staging)):
            found = autonomy_gate.route(c, {"project_id": project, "risk_tier": 2,
                                            "recommendation": {"action": "deploy_host",
                                                               "target_id": target}})
            answers[name] = found
        prod_card = _card(c, project, {"action": "deploy_host", "target_id": prod})
        refused = autonomy_gate.decide(c, settings, prod_card)
        still = _row(c, prod_card)
    assert not answers["prod"].acts and answers["prod"].cls == autonomy_gate.ASK_PROD_RELEASE
    assert not answers["unknown"].acts and "could not tell" in answers["unknown"].why
    assert answers["local"].acts and answers["staging"].acts
    assert refused["decided"] is False and still["status"] == "pending"
    assert still["recommendation"]["autonomy"]["route"] == "ask"


def test_a_merge_into_a_branch_that_ships_to_production_asks(eng, project):
    with eng.begin() as c:
        rel = autonomy_gate.route(c, {"project_id": project, "recommendation": {
            "action": "review_pull_request", "pr_base": "rel/rc11", "repo": "o/unknown"}})
        main = autonomy_gate.route(c, {"project_id": project, "recommendation": {
            "action": "review_pull_request", "pr_base": "main", "repo": "o/unknown"}})
    assert rel.acts
    assert not main.acts and main.cls == autonomy_gate.ASK_PROD_RELEASE


def test_a_merge_with_failing_checks_is_never_decided_by_autonomy(eng, project):
    from boss_engine.services import checks_failing

    red = checks_failing.collect(["tests_pass", "security_clean"])
    assert red["gates"][0]["gate"] == "security_clean"          # the record the build writes
    with eng.begin() as c:
        failing = autonomy_gate.route(c, {"project_id": project, "recommendation": {
            "action": "review_pull_request", "pr_base": "rel/rc11", "repo": "o/unknown",
            "checks_failing": red}})
        green = autonomy_gate.route(c, {"project_id": project, "recommendation": {
            "action": "review_pull_request", "pr_base": "rel/rc11", "repo": "o/unknown"}})
    assert not failing.acts and failing.cls == autonomy_gate.EXPLICIT_STOP
    assert "checks are failing" in failing.why
    assert green.acts


def test_a_stop_a_person_set_is_honoured(eng, project):
    settings = get_settings()
    with eng.begin() as c:
        decision_id = _card(c, project, {"action": "execute_agent_tool",
                                         "tool": "publish_document",
                                         "explicit_stop": "Review the plan first"})
        result = autonomy_gate.decide(c, settings, decision_id)
        row = _row(c, decision_id)
    assert result["decided"] is False and row["status"] == "pending"
    assert row["recommendation"]["autonomy"]["class"] == autonomy_gate.EXPLICIT_STOP


def test_credentials_and_spend_still_wait(eng, project):
    with eng.begin() as c:
        secret = autonomy_gate.route(c, {"project_id": project, "recommendation": {
            "action": "retry_when_secret_set"}})
        spend = autonomy_gate.route(c, {"project_id": project, "recommendation": {
            "action": "increase_budget_or_defer"}})
        over = autonomy_gate.route(c, {"project_id": project, "recommendation": {
            "action": "budget.override", "amount_usd": 500}})
        within = autonomy_gate.route(c, {"project_id": project, "recommendation": {
            "action": "budget.override", "amount_usd": 0.5}})
    assert (secret.acts, secret.cls) == (False, autonomy_gate.ASK_CREDENTIAL)
    assert (spend.acts, spend.cls) == (False, autonomy_gate.ASK_SPEND)
    assert not over.acts and within.acts


def test_a_project_that_says_ask_in_development_keeps_its_cards(eng, project):
    settings = get_settings()
    with eng.begin() as c:
        autonomy_policy.save(c, kind=autonomy_policy.PROJECT, scope_id=project,
                             raw={"dev": "ask"}, actor="ceo")
        decision_id = _card(c, project, {"action": "decide"})
        result = autonomy_gate.decide(c, settings, decision_id)
        c.execute(text("DELETE FROM autonomy_policies WHERE scope_id = :p"), {"p": project})
    assert result["decided"] is False


def test_decided_for_you_lists_it_latest_first_with_its_undo(eng, project):
    settings = get_settings()
    with eng.begin() as c:
        first = _card(c, project, {"action": "decide"})
        autonomy_gate.decide(c, settings, first)
        second = _card(c, project, {"action": "resume_code_task", "run_id": "crun_none"})
        autonomy_gate.decide(c, settings, second)
        items = autonomy_gate.decided_for_you(c, project)
        with pytest.raises(autonomy_gate.UndoRefused):
            autonomy_gate.undo(c, settings, first, project_id=project, actor="ceo")
        reviewed = autonomy_gate.mark_reviewed(c, first, project_id=project, actor="ceo")
    ids = [i["decision_id"] for i in items]
    assert set(ids) >= {first, second}
    by_id = {i["decision_id"]: i for i in items}
    assert by_id[first]["undo"] is None and by_id[first]["undo_unavailable"]
    assert by_id[second]["undo"] == "send_back"
    assert reviewed["reviewed"] is True


def test_the_sweep_decides_a_card_written_without_the_sink(eng, project):
    settings = get_settings()
    decision_id = f"dec_{uuid.uuid4().hex[:12]}"
    with eng.begin() as c:
        c.execute(text(
            "INSERT INTO decisions (id, project_id, question, recommendation, risk_tier, "
            "cost_summary, default_action, status) VALUES (:i, :p, 'direct', "
            "CAST(:r AS JSONB), 2, '{}'::jsonb, 'hold', 'pending')"),
            {"i": decision_id, "p": project, "r": json.dumps({"action": "review_stalled_meeting"})})
    out = autonomy_gate.sweep(eng, settings, project_id=project)
    with eng.begin() as c:
        row = _row(c, decision_id)
    assert out["decided"] >= 1
    assert row["status"] == "approved" and row["approved_by"].startswith("autonomy:dev:")


def test_a_tier_three_card_waits_for_two_people_not_for_autonomy(eng, project):
    """#307 review P1-2: dev autonomy decides through transition(system_policy=True), which skips
    the two-person rule, so a card of a two-person tier is never decided by it."""
    settings = get_settings()
    with eng.begin() as c:
        decision_id = _card(c, project, {"capability": "security_review"}, risk_tier=3)
        result = autonomy_gate.decide(c, settings, decision_id)
        row = _row(c, decision_id)
    assert result["decided"] is False and "two-person" in result["why"]
    assert row["status"] == "pending"


def test_a_deploy_steps_answer_honours_its_author_and_its_own_target(eng, project):
    """#307 review P2: deploy_policy.step_answer — a step set to ask is a person's; the step's
    own target_id decides, not only its environment name."""
    from boss_engine.services import deploy_policy

    with eng.begin() as c:
        prod = _target(c, project, "eu1", tier="prod")
        local = _target(c, project, "eu1", ref="deploy@localhost:22")

        def step(**extra):
            inputs = {"environment": "staging", **extra.pop("inputs", {})}
            return {"id": "d", "kind": "capability",
                    "binding": {"capability": "deploy", "inputs": inputs}, **extra}

        plain = deploy_policy.step_answer(c, project, step())
        manual = deploy_policy.step_answer(c, project, step(approval={"mode": "manual"}))
        to_prod = deploy_policy.step_answer(c, project, step(inputs={"target_id": prod}))
        to_local = deploy_policy.step_answer(c, project, step(inputs={"target_id": local}))
        run_target = deploy_policy.step_answer(c, project, step(), {"target_id": prod})
    assert plain["who"] == "agent"
    assert manual["who"] == "you"
    assert to_prod["who"] == "you" and to_local["who"] == "agent"
    assert run_target["who"] == "you"


def test_an_approval_gate_its_author_set_to_ask_waits_for_a_person(eng, project):
    """An Approval gate step with `approval: {mode: manual}` is a stop a person set: the gate card
    workflows.py opens carries it, and dev autonomy leaves the card to the person (the enterprise
    lane's step-up check got 409 "already approved" when BOSS answered the person's gate)."""
    import inspect

    from boss_engine.services import workflows

    source = inspect.getsource(workflows)
    assert '{"approval_mode_requested": "manual"}' in source
    with eng.begin() as c:
        manual = autonomy_gate.route(c, {"project_id": project, "recommendation": {
            "action": "resume_workflow", "approval_mode_requested": "manual"}})
        plain = autonomy_gate.route(c, {"project_id": project, "recommendation": {
            "action": "resume_workflow"}})
    assert not manual.acts and manual.cls == autonomy_gate.EXPLICIT_STOP
    assert plain.acts


# ── BOSSAPP-1461: BOSS-side actions act autonomously with real Undo; customer/prod/cap ask ───

def test_boss_side_actions_act_autonomously_and_undo_works(eng, project, monkeypatch):
    from boss_engine.services import (
        agent_requests,
        reflection,
        skill_definitions,
    )

    monkeypatch.setenv("BOSS_INSTALL_TIER", "dev")
    settings = get_settings()

    with eng.begin() as c:
        # Clean project artifacts we create at end via try/finally or cleanup.
        # 1. role.hire + undo (retire_hire)
        hire = agent_requests.propose_hire(
            c, project_id=project, proposer="agent:engineering_captain",
            name="Latency Watcher", purpose="Watches p95 latency regressions.",
            reason="Need a dedicated latency watcher.")
        assert hire["status"] == "asked"
        dec_hire = hire["decision_id"]
        res_hire = autonomy_gate.decide(c, settings, dec_hire)
        assert res_hire["decided"] is True and res_hire["undo"] == "retire_hire"
        assert c.execute(text(
            "SELECT 1 FROM roles WHERE project_id = :p AND role_key = 'latency_watcher'"),
            {"p": project}).first() is not None
        undo_hire = autonomy_gate.undo(c, settings, dec_hire, project_id=project, actor="ceo")
        assert undo_hire["undo"] == "retire_hire"
        assert c.execute(text(
            "SELECT 1 FROM roles WHERE project_id = :p AND role_key = 'latency_watcher'"),
            {"p": project}).first() is None

        # 2. role.autonomy_change + undo (revert_autonomy)
        role_id = f"role_{uuid.uuid4().hex[:8]}"
        c.execute(text(
            "INSERT INTO roles (id, project_id, role_key, display_name, autonomy_level, spec) "
            "VALUES (:i, :p, 'qa_bot', 'QA Bot', 1, '{}'::jsonb)"),
            {"i": role_id, "p": project})
        chg_id = f"ac_{uuid.uuid4().hex[:8]}"
        c.execute(text(
            "INSERT INTO autonomy_changes (id, project_id, role_id, role_key, change_type, "
            "trigger, from_level, to_level, status, reason, created_by) VALUES "
            "(:i, :p, :r, 'qa_bot', 'upgrade', 'scorecard', 1, 2, 'pending_decision', "
            "'Strong scorecard', 'agent:vp_agents')"),
            {"i": chg_id, "p": project, "r": role_id})
        dec_auto = _card(c, project, {
            "action": "role.autonomy_change", "autonomy_change_id": chg_id, "role_key": "qa_bot"
        }, risk_tier=2)
        res_auto = autonomy_gate.decide(c, settings, dec_auto)
        assert res_auto["decided"] is True and res_auto["undo"] == "revert_autonomy"
        assert c.execute(text("SELECT autonomy_level FROM roles WHERE id = :r"), {"r": role_id}).scalar() == 2
        undo_auto = autonomy_gate.undo(c, settings, dec_auto, project_id=project, actor="ceo")
        assert undo_auto["undo"] == "revert_autonomy"
        assert c.execute(text("SELECT autonomy_level FROM roles WHERE id = :r"), {"r": role_id}).scalar() == 1

        # 3. apply_agent_prompt_diff + undo (remove_instruction)
        c.execute(text(
            "INSERT INTO roles (id, project_id, role_key, display_name) "
            "VALUES (:i, :p, 'qa_bot', 'QA Bot') ON CONFLICT DO NOTHING"),
            {"i": f"role_{uuid.uuid4().hex[:8]}", "p": project})
        pdiff = reflection._propose_prompt_diff(
            c, project, "qa_bot",
            [{"kind": "feedback", "id": "fb_1", "target_kind": "agent",
              "target": "qa_bot", "said": "Check edge cases."}],
            "rfl_1",
        )
        # Ensure we use change_consent + recommendation directly if _propose_prompt_diff skipped threshold
        if pdiff is None:
            from boss_engine.services import change_consent
            proposed = {"name": "Edge-case checklist", "body": "Always test boundary values.",
                        "source": "pasted", "modes": ["work"]}
            opened = change_consent.request(
                c, project_id=project, subject_kind="prompt", subject_key="qa_bot",
                base_hash="", proposed=proposed, diff="+ Always test boundary values.",
                requested_by="agent:reflection", question="Add Edge-case checklist to qa_bot?",
                action="apply_agent_prompt_diff",
                recommendation={"role_key": "qa_bot", "proposed": proposed},
                reason="reflection_prompt_diff", risk_tier=1,
            )
            dec_pdiff = opened["decision_id"]
        else:
            dec_pdiff = pdiff["decision_id"]
        res_pdiff = autonomy_gate.decide(c, settings, dec_pdiff)
        assert res_pdiff["decided"] is True and res_pdiff["undo"] == "remove_instruction"
        assert c.execute(text(
            "SELECT count(*) FROM agent_instructions WHERE project_id = :p AND role_key = 'qa_bot'"),
            {"p": project}).scalar() == 1
        undo_pdiff = autonomy_gate.undo(c, settings, dec_pdiff, project_id=project, actor="ceo")
        assert undo_pdiff["undo"] == "remove_instruction"
        assert c.execute(text(
            "SELECT count(*) FROM agent_instructions WHERE project_id = :p AND role_key = 'qa_bot'"),
            {"p": project}).scalar() == 0

        # 4. activate_authored_skill + undo (disable_skill)
        drafted = skill_definitions.draft_for_approval(
            c, project_id=project, actor="agent:engineering_manager",
            skill_key="auto_skill_check", label="Auto Skill Check",
            behaviour_kind="guide", summary="Checks stuff", knowledge="Do X.", risk_tier=1,
        )
        dec_skill = drafted["decision_id"]
        res_skill = autonomy_gate.decide(c, settings, dec_skill)
        assert res_skill["decided"] is True and res_skill["undo"] == "disable_skill"
        assert c.execute(text(
            "SELECT enabled FROM skill_definitions WHERE project_id = :p AND skill_key = 'auto_skill_check'"),
            {"p": project}).scalar() is True
        undo_skill = autonomy_gate.undo(c, settings, dec_skill, project_id=project, actor="ceo")
        assert undo_skill["undo"] == "disable_skill"
        assert c.execute(text(
            "SELECT enabled FROM skill_definitions WHERE project_id = :p AND skill_key = 'auto_skill_check'"),
            {"p": project}).scalar() is False

        # 5. review_tool_change (tier 2, reviewed server) + undo (quarantine_tool)
        srv_id = f"mcp_{uuid.uuid4().hex[:8]}"
        tools_json = [{
            "name": "list_tickets", "risk_tier": 2, "quarantine": "ask",
            "schema_sha256": "s1", "description_sha256": "d1",
        }]
        c.execute(text(
            "INSERT INTO mcp_servers (id, project_id, name, url, risk_tier, enabled, "
            "provenance, tools, reachable) VALUES (:i, :p, 'tracker', 'http://127.0.0.1:9/mcp', "
            "2, true, '{\"trusted\": true}'::jsonb, CAST(:t AS JSONB), true)"),
            {"i": srv_id, "p": project, "t": json.dumps(tools_json)})
        dec_tool = _card(c, project, {
            "action": "review_tool_change", "server_id": srv_id, "tool": "list_tickets",
            "schema_sha256": "s1", "description_sha256": "d1",
        }, risk_tier=2)
        res_tool = autonomy_gate.decide(c, settings, dec_tool)
        assert res_tool["decided"] is True and res_tool["undo"] == "quarantine_tool"
        undo_tool = autonomy_gate.undo(c, settings, dec_tool, project_id=project, actor="ceo")
        assert undo_tool["undo"] == "quarantine_tool"
        updated_tools = c.execute(text("SELECT tools FROM mcp_servers WHERE id = :i"), {"i": srv_id}).scalar()
        entry = next(t for t in updated_tools if t["name"] == "list_tickets")
        assert entry.get("quarantine") == "ask"

        # 6. self_improvement.begin_canary + undo (rollback_self_improvement)
        task_id = f"tsk_{uuid.uuid4().hex[:8]}"
        c.execute(text(
            "INSERT INTO tasks (id, project_id, title, description, dedupe_key) VALUES "
            "(:i, :p, 'Improve', 'Body', :i)"), {"i": task_id, "p": project})
        prop_id = f"imp_{uuid.uuid4().hex[:8]}"
        c.execute(text(
            "INSERT INTO self_improvement_proposals (id, project_id, task_id, title, summary, "
            "change_ref, diff_summary, status, risk_tier, created_by) VALUES "
            "(:i, :p, :t, 'Speedup', 'Faster', 'ref1', 'diff', 'proposed', 2, 'agent:em')"),
            {"i": prop_id, "p": project, "t": task_id})
        dec_imp = _card(c, project, {"action": "self_improvement.begin_canary", "proposal_id": prop_id},
                        risk_tier=2)
        res_imp = autonomy_gate.decide(c, settings, dec_imp)
        assert res_imp["decided"] is True and res_imp["undo"] == "rollback_self_improvement"
        assert c.execute(text("SELECT status FROM self_improvement_proposals WHERE id = :i"),
                         {"i": prop_id}).scalar() == "canary_approved"
        undo_imp = autonomy_gate.undo(c, settings, dec_imp, project_id=project, actor="ceo")
        assert undo_imp["undo"] == "rollback_self_improvement"
        assert c.execute(text("SELECT status FROM self_improvement_proposals WHERE id = :i"),
                         {"i": prop_id}).scalar() == "rejected"

        # 7. budget.override with real override.extra_usd_micros <= $1.00 acts & undo (revoke_override)
        run_id = f"crun_{uuid.uuid4().hex[:8]}"
        c.execute(text(
            "INSERT INTO code_task_runs (id, project_id, title, status, phase) "
            "VALUES (:i, :p, 'Build', 'failed', 'done')"), {"i": run_id, "p": project})
        ov = agent_requests.request_override(
            c, project_id=project, asker="agent:backend", run_id=run_id,
            extra_usd_micros=500_000, reason="Need $0.50 to finish tests")
        res_ov = autonomy_gate.decide(c, settings, ov["decision_id"])
        assert res_ov["decided"] is True and res_ov["undo"] == "revoke_override"
        assert agent_requests.override_headroom(c, project, run_id) == 500_000
        undo_ov = autonomy_gate.undo(c, settings, ov["decision_id"], project_id=project, actor="ceo")
        assert undo_ov["undo"] == "revoke_override"
        assert agent_requests.override_headroom(c, project, run_id) == 0

        # Cleanup FK-dependent rows for project teardown
        for tbl in ("budget_overrides", "self_improvement_proposals", "artifacts", "code_task_runs",
                    "tasks", "mcp_servers", "skill_versions", "skill_definitions",
                    "project_skills", "change_consents", "agent_instructions",
                    "autonomy_changes", "roles"):
            c.execute(text(f"DELETE FROM {tbl} WHERE project_id = :p"), {"p": project})


def test_boss_side_self_fix_and_negative_asks_under_dev_autonomy(eng, project, monkeypatch):
    with eng.begin() as c:
        # self_fix.install acts autonomously in dev (ASK_PROD_ONLY) and asks in prod;
        # self_fix.remove & self_improvement.* act autonomously even when install_tier == prod.
        monkeypatch.setenv("BOSS_INSTALL_TIER", "dev")
        rt_install_dev = autonomy_gate.route(c, {"project_id": project, "risk_tier": 2,
                                                  "recommendation": {"action": "self_fix.install"}})
        assert rt_install_dev.acts is True, rt_install_dev

        monkeypatch.setenv("BOSS_INSTALL_TIER", "prod")
        rt_install_prod = autonomy_gate.route(c, {"project_id": project, "risk_tier": 2,
                                                   "recommendation": {"action": "self_fix.install"}})
        assert rt_install_prod.acts is False and rt_install_prod.cls == autonomy_gate.ASK_PROD_RELEASE
        for action in ("self_fix.remove", "self_improvement.begin_canary", "self_improvement.rollout"):
            rt = autonomy_gate.route(c, {"project_id": project, "risk_tier": 2,
                                         "recommendation": {"action": action}})
            assert rt.acts is True, (action, rt)

        # Negative checks: these MUST keep asking!
        for rec, tier in (
            ({"action": "budget.override", "override": {"extra_usd_micros": 2_000_000}}, 2),
            ({"action": "grant_builder_authority"}, 2),
            ({"action": "store_proposed_secret"}, 2),
            ({"action": "promote_agent_deployment"}, 2),
            ({"action": "review_tool_change", "tool_screen": "untrusted"}, 2),
            ({"action": "review_tool_change", "unreviewed_server": True}, 2),
            ({"action": "review_tool_change"}, 3),
            ({"action": "decide", "explicit_stop": "Person asked to review first"}, 2),
        ):
            rt = autonomy_gate.route(c, {"project_id": project, "risk_tier": tier,
                                         "recommendation": rec})
            assert rt.acts is False, (rec, tier, rt)

        # Daily per_day cap enforcement for budget.override ($10.00 default).
        run_id = f"crun_{uuid.uuid4().hex[:8]}"
        dec_existing = f"dec_{uuid.uuid4().hex[:8]}"
        c.execute(text(
            "INSERT INTO decisions (id, project_id, question, recommendation, risk_tier, "
            "cost_summary, default_action, status) VALUES (:d, :p, 'q', '{}'::jsonb, 2, "
            "'{}'::jsonb, 'hold', 'approved')"), {"d": dec_existing, "p": project})
        c.execute(text(
            "INSERT INTO budget_overrides (id, project_id, root_run_id, run_id, extra_usd_micros, "
            "decision_id, approved_by, status, expires_at) VALUES "
            "(:i, :p, :r, :r, 9500000, :d, 'autonomy:dev:finops', 'active', now() + interval '1 day')"),
            {"i": f"bov_{uuid.uuid4().hex[:8]}", "p": project, "r": run_id, "d": dec_existing})
        over_day = autonomy_gate.route(c, {"project_id": project, "risk_tier": 2,
                                           "recommendation": {"action": "budget.override",
                                                              "override": {"extra_usd_micros": 800_000}}})
        assert over_day.acts is False and over_day.cls == autonomy_gate.ASK_SPEND
        c.execute(text("DELETE FROM budget_overrides WHERE project_id = :p"), {"p": project})


def test_autonomy_gate_handles_non_finite_override_zero_amount_bad_tier_and_rollback_id(
    eng, project, monkeypatch
):
    from types import SimpleNamespace
    from boss_engine.services import self_improvement

    with eng.begin() as c:
        for bad in (float("nan"), float("inf"), float("-inf")):
            r = autonomy_gate.route(c, {"project_id": project, "risk_tier": 2,
                                        "recommendation": {"action": "budget.override",
                                                           "override": {"extra_usd_micros": bad}}})
            assert r.acts is False and r.cls == autonomy_gate.ASK_SPEND
        zero_with_override = autonomy_gate.route(
            c,
            {
                "project_id": project,
                "risk_tier": 2,
                "recommendation": {
                    "action": "budget.override",
                    "amount_usd": 0,
                    "override": {"extra_usd_micros": 500_000},
                },
            },
        )
        assert zero_with_override.acts is False and zero_with_override.cls == autonomy_gate.ASK_SPEND

        bad_tier = autonomy_gate.route(
            c,
            {
                "project_id": project,
                "risk_tier": "bad",
                "recommendation": {"action": "review_tool_change"},
            },
        )
        assert bad_tier.acts is False and bad_tier.cls == autonomy_gate.ASK_PROD_RELEASE

        max_tier_3 = autonomy_gate.route(
            c,
            {
                "project_id": project,
                "risk_tier": 2,
                "recommendation": {"action": "review_tool_change", "max_tier": 3},
            },
        )
        assert max_tier_3.acts is False and max_tier_3.cls == autonomy_gate.ASK_PROD_RELEASE

        tools_tier_3 = autonomy_gate.route(
            c,
            {
                "project_id": project,
                "risk_tier": 2,
                "recommendation": {
                    "action": "review_tool_change",
                    "tools": [{"name": "deploy_app", "tier": 3}],
                },
            },
        )
        assert tools_tier_3.acts is False and tools_tier_3.cls == autonomy_gate.ASK_PROD_RELEASE

    class _FakeSelfImprovementService:
        def __init__(self, _settings):
            pass

        def get_proposal(self, _conn, proposal_id, for_update=False):
            return {"id": proposal_id, "project_id": project, "status": "rolled_out"}

        def rollback(self, *_a, **_k):
            return {"id": "sip_1", "status": "rolled_back", "rollback_artifact_id": "art_rb_42"}

    monkeypatch.setattr(self_improvement, "SelfImprovementService", _FakeSelfImprovementService)
    msg = autonomy_gate._undo_rollback_self_improvement(
        SimpleNamespace(), get_settings(), "dec_1", project, {"proposal_id": "sip_1"}, "ceo"
    )
    assert msg == "rolled back sip_1 (art_rb_42)"


@pytest.mark.parametrize("decision", [
    {"risk_tier": 2, "recommendation": {"action": "grant_tool_access", "tools": [{"tier": "bad"}]}},
    {"risk_tier": 2, "recommendation": {"action": "grant_tool_access", "tools": 123}},
    {"risk_tier": 2, "recommendation": {"action": "grant_tool_access",
                                        "tools": [{"tier": float("inf")}]}},
    {"risk_tier": 2, "recommendation": {"action": "review_tool_change", "tools": 123}},
    {"risk_tier": 2, "recommendation": {"action": "review_tool_change",
                                        "tools": [{"tier": float("inf")}]}},
    {"risk_tier": float("inf"), "recommendation": {"action": "review_tool_change"}},
    {"risk_tier": "bad", "recommendation": {"action": "execute_agent_tool", "tool": "read_file"}},
    {"risk_tier": float("nan"), "recommendation": {"action": "execute_agent_tool",
                                                  "tool": "read_file"}},
    {"risk_tier": "bad", "recommendation": {"capability": "custom_cap"}},
    {"risk_tier": float("inf"), "recommendation": {"capability": "custom_cap"}},
])
def test_malformed_tools_or_risk_tier_asks_instead_of_crashing(decision):
    """BOSSAPP-1461: malformed card JSON (a non-list tools, a non-integer or non-finite tier)
    asks a person; route() never raises on it."""
    r = autonomy_gate.route(None, {"project_id": "p1", **decision},
                            policy=autonomy_policy.Policy())
    assert r.acts is False
    assert r.cls in (autonomy_gate.ASK_PROD_RELEASE, "unclassified")
