"""EVERY production deployment meets the production quality bar (BOSSAPP-1362), as a class.

One check, ResourcePlaneService.plan_deployment_run -> quality_bar.plan_refusal. The ONLY exemptions
are a redeploy/rollback that re-ships the exact commit of a prior production deployment on the same
target (a TRUSTED argument from delivery_pipeline.redeploy, never a request or packet field) and a
config-only target. Each door is walked: the redeploy route with a caller commit_ref, the create
route with a caller packet, the skill executor, and the manual deploy.
"""
import json
from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy import text

from boss_engine.services import quality_bar
from boss_engine.services.resource_plane import ResourcePlaneService

PROJECT = "boss-dogfood"
PASSED = '["code_review_passed","security_clean","consistent","acceptance_met"]'
GAPS = {
    "acceptance": {"verdict": "not_met", "stack": {}, "items": [
        {"n": 1, "text": "Export handles a thousand-row file", "status": "partial", "tier": "P2"}]},
    "review_summary": "FINDING: MAJOR server/a.ts:3 — the list ignores the filter\nVERDICT: REFUTED",
}


@pytest.fixture
def world(engine):
    suffix = uuid4().hex[:8]
    run_id, tgt, prior = f"crun_sb{suffix}", f"deptgt_sb{suffix}", f"deprun_sbp{suffix}"
    with engine.begin() as c:
        c.execute(text(
            "INSERT INTO code_task_runs (id, project_id, status, title, spec, finalize_ctx) "
            "VALUES (:r, :p, 'completed', 'App', '{}'::jsonb, '{}'::jsonb)"),
            {"r": run_id, "p": PROJECT})
        c.execute(text(
            "INSERT INTO quality_gates (id, project_id, code_task_run_id, risk_tier, status, "
            "fail_closed, gates_passed, gates_failed, evidence) VALUES (:i, :p, :r, 1, 'pass', "
            "true, CAST(:gp AS jsonb), '[]'::jsonb, CAST(:e AS jsonb))"),
            {"i": f"qg_{suffix}", "p": PROJECT, "r": run_id, "gp": PASSED, "e": json.dumps(GAPS)})
        c.execute(text(
            "INSERT INTO deployment_targets (id, project_id, app_name, repo, environment, "
            "target_kind, target_ref, status, created_by) VALUES (:i, :p, :a, 'example/app', "
            "'production', 'ssh', 'root@203.0.113.25', 'active', 'ceo')"),
            {"i": tgt, "p": PROJECT, "a": f"sb-{suffix}"})
        c.execute(text(
            "INSERT INTO deployment_runs (id, project_id, deployment_target_id, app_name, "
            "environment, repo, target_kind, target_ref, status, created_by, commit_ref, "
            "release_packet) VALUES (:i, :p, :t, :a, 'production', 'example/app', 'ssh', "
            "'root@203.0.113.25', 'succeeded', 'ceo', 'prodsha1', CAST(:pk AS jsonb))"),
            {"i": prior, "p": PROJECT, "t": tgt, "a": f"sb-{suffix}",
             "pk": json.dumps({"source": "operator_deploy", "code_task_run_id": run_id})})
    return SimpleNamespace(run=run_id, target=tgt, prior=prior, suffix=suffix)


def _count(engine, target):
    with engine.begin() as c:
        return c.execute(text("SELECT count(*) FROM deployment_runs WHERE deployment_target_id = :t"),
                         {"t": target}).scalar()


def test_a_redeploy_with_a_callers_commit_is_judged_but_the_same_commit_is_not(client, engine, world):
    before = _count(engine, world.target)
    other = client.post(f"/v1/delivery/deployments/{world.prior}/redeploy",
                        json={"actor": "ceo", "commit_ref": "someothersha"})
    assert other.status_code == 409 and "production quality bar" in other.text, other.text
    assert "thousand-row" in other.text
    assert _count(engine, world.target) == before, "a new commit reached production past the bar"
    same = client.post(f"/v1/delivery/deployments/{world.prior}/redeploy", json={"actor": "ceo"})
    assert "production quality bar" not in same.text, same.text        # re-ships prodsha1: exempt


def test_a_callers_packet_exempts_nothing_on_the_create_route(client, engine, world):
    before = _count(engine, world.target)
    for packet in ({"source": "redeploy"}, {"source": "rollback", "redeploy_of": world.prior}, {}):
        response = client.post("/v1/deployment-runs", json={
            "project_id": PROJECT, "deployment_target_id": world.target, "commit_ref": "evil",
            "release_packet": packet, "expected_cost_usd_micros": 0, "actor": "ceo"})
        assert response.status_code == 409, (packet, response.text)
        assert "production quality bar" in response.text and "names no build" in response.text
    # naming a build that does not pass the bar is refused with its list
    response = client.post("/v1/deployment-runs", json={
        "project_id": PROJECT, "deployment_target_id": world.target, "commit_ref": "evil",
        "release_packet": {"code_task_run_id": world.run}, "expected_cost_usd_micros": 0,
        "actor": "ceo"})
    assert response.status_code == 409 and "thousand-row" in response.text, response.text
    assert _count(engine, world.target) == before


def test_the_skill_executor_cannot_plan_a_production_release_without_a_passing_build(engine, world):
    from boss_engine.services import skill_executors

    with engine.begin() as c:
        context = SimpleNamespace(
            connection=c, project_id=PROJECT, actor="ceo",
            deployments=ResourcePlaneService(__import__("apps.api.app.main", fromlist=["settings"]).settings))
        inputs = {"deployment_target_id": world.target, "commit_ref": "evil",
                  "release_packet_artifact_id": "art_1"}
        with pytest.raises(ValueError, match="names no build"):
            skill_executors.execute_deployment_run_plan(context, inputs)
        with pytest.raises(ValueError, match="thousand-row"):
            skill_executors.execute_deployment_run_plan(
                context, {**inputs, "code_task_run_id": world.run})


def test_a_manual_deploy_without_a_build_is_refused_on_a_production_target(client, engine, world):
    before = _count(engine, world.target)
    response = client.post("/v1/delivery/deploy", json={
        "target_id": world.target, "actor": "ceo", "ref": "main", "wait": True})
    assert "production quality bar" in response.text, (response.status_code, response.text)
    assert _count(engine, world.target) == before


def test_the_exemptions_are_exactly_a_same_commit_reship_and_a_config_target(engine, world):
    target = {"id": world.target, "environment": "production", "target_kind": "ssh"}
    with engine.begin() as c:
        # a packet naming itself a redeploy exempts nothing
        assert "names no build" in quality_bar.plan_refusal(
            c, target=target, release_packet={"source": "redeploy"}, commit_ref="prodsha1")
        # the trusted re-ship: same target, production, SAME commit
        assert quality_bar.plan_refusal(c, target=target, release_packet={}, commit_ref="prodsha1",
                                        reships=world.prior) is None
        # a different commit, another target, or a prior that is not production: judged
        assert quality_bar.plan_refusal(c, target=target, release_packet={}, commit_ref="other",
                                        reships=world.prior)
        assert quality_bar.plan_refusal(c, target={**target, "id": "deptgt_other"},
                                        release_packet={}, commit_ref="prodsha1",
                                        reships=world.prior)
        assert quality_bar.plan_refusal(c, target=target, release_packet={}, commit_ref=None,
                                        reships=world.prior)
        # a config-only target has its own checks; staging is not production
        assert quality_bar.plan_refusal(
            c, target={**target, "target_kind": "config_dir"}, release_packet={}) is None
        assert quality_bar.plan_refusal(
            c, target={**target, "environment": "staging"}, release_packet={}) is None
