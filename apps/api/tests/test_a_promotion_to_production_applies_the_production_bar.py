"""A staging -> production promotion applies the PRODUCTION quality bar (BOSSAPP-1362).

The real route (POST /v1/deployments/{id}/promote) -> PromotionOrchestrator -> the one place every
production deployment is planned (ResourcePlaneService.plan_deployment_run). A build that passed at
a lighter bar and deployed to staging is refused at production with the list of what is needed.
"""
import json
from uuid import uuid4

from sqlalchemy import text

from boss_engine.services import quality_bar

PROJECT = "boss-dogfood"
GAPS = {
    "acceptance": {"verdict": "not_met", "stack": {}, "items": [
        {"n": 1, "text": "Export handles a thousand-row file", "status": "partial", "tier": "P2"}]},
    "review_summary": "FINDING: MAJOR server/a.ts:3 — the list ignores the filter\nVERDICT: REFUTED",
}


def _seed(engine, suffix, evidence):
    run_id, tgt_s, tgt_p, dep = (f"crun_pb{suffix}", f"deptgt_pbs{suffix}", f"deptgt_pbp{suffix}",
                                 f"deprun_pb{suffix}")
    with engine.begin() as c:
        c.execute(text(
            "INSERT INTO code_task_runs (id, project_id, status, title, spec, finalize_ctx) "
            "VALUES (:r, :p, 'completed', 'App', '{}'::jsonb, '{}'::jsonb)"),
            {"r": run_id, "p": PROJECT})
        c.execute(text(
            "INSERT INTO quality_gates (id, project_id, code_task_run_id, risk_tier, status, "
            "fail_closed, gates_passed, gates_failed, evidence) VALUES (:i, :p, :r, 1, 'pass', "
            "true, '[\"code_review_passed\",\"security_clean\",\"consistent\",\"acceptance_met\"]'::jsonb, '[]'::jsonb, CAST(:e AS jsonb))"),
            {"i": f"qg_{suffix}", "p": PROJECT, "r": run_id, "e": json.dumps(evidence)})
        for tid, env in ((tgt_s, "staging"), (tgt_p, "production")):
            c.execute(text(
                "INSERT INTO deployment_targets (id, project_id, app_name, repo, environment, "
                "target_kind, target_ref, status, created_by) VALUES (:i, :p, :a, 'example/app', "
                ":e, 'ssh', 'root@203.0.113.25', 'active', 'ceo')"),
                {"i": tid, "p": PROJECT, "a": f"pb-{suffix}", "e": env})
        c.execute(text(
            "INSERT INTO deployment_runs (id, project_id, deployment_target_id, app_name, "
            "environment, repo, target_kind, target_ref, status, created_by, commit_ref, release_packet) "
            "VALUES (:i, :p, :t, :a, 'staging', 'example/app', 'ssh', 'root@203.0.113.25', "
            "'succeeded', 'ceo', 'abc1234', CAST(:pk AS jsonb))"),
            {"i": dep, "p": PROJECT, "t": tgt_s, "a": f"pb-{suffix}",
             "pk": json.dumps({"source": "operator_deploy", "code_task_run_id": run_id})})
    return dep, tgt_p


def test_promoting_a_standard_pass_with_gaps_to_production_is_refused_with_the_list(
        client, engine, monkeypatch):
    """The real promote route creates the promotion; the orchestrator's production step (reached
    once staging is healthy) plans through plan_deployment_run, which refuses with the list."""
    from apps.api.app import main as api_main
    from boss_engine.services.promotion_orchestrator import PromotionOrchestrator
    from boss_engine.services.resource_plane import ResourcePlaneService

    dep, _ = _seed(engine, uuid4().hex[:8], GAPS)
    response = client.post(f"/v1/deployments/{dep}/promote",
                           json={"actor": "ceo", "note": "ship it"})
    assert response.status_code == 200, response.text
    promotion_id = response.json()["id"]
    packet = {"source": "promotion_gate", "promoted_from_run": dep, "approved_by": "ceo"}
    with engine.begin() as c:
        # staging is planned and healthy (what the staging gate waits for): the next step is the
        # production plan, the one under test
        c.execute(text(
            "UPDATE deployment_promotions SET status = 'staging_planned', "
            "staging_deployment_run_id = :s, gate_result = CAST(:g AS jsonb) WHERE id = :i"),
            {"s": dep, "i": promotion_id, "g": json.dumps({"release_packet": packet})})
    monkeypatch.setattr(
        ResourcePlaneService, "run_deployment_health_checks",
        lambda self, connection, **kw: {
            "deployment_run": {"id": dep, "status": "healthy", "health_check_results": []},
            "health_artifact_id": "a", "rollback_artifact_id": "b", "decision_id": None})
    with engine.begin() as c:
        detail = PromotionOrchestrator(api_main.settings).advance_after_staging_gate(
            c, project_id=PROJECT, promotion_id=promotion_id, actor="ceo")
    assert "production quality bar" in json.dumps(detail, default=str), detail
    assert "thousand-row" in json.dumps(detail, default=str)
    with engine.begin() as c:
        planned = c.execute(text(
            "SELECT count(*) FROM deployment_runs WHERE environment = 'production' "
            "AND release_packet->>'promotion_id' = :p"), {"p": promotion_id}).scalar()
    assert planned == 0, "a production deployment was planned past the bar"


def test_plan_refusal_is_only_about_production_and_names_the_promoted_build(engine):
    suffix = uuid4().hex[:8]
    dep, _ = _seed(engine, suffix, GAPS)
    with engine.begin() as c:
        assert quality_bar.plan_refusal(c, target={"environment": "staging"},
                                        release_packet={"code_task_run_id": f"crun_pb{suffix}"}) is None
        said = quality_bar.plan_refusal(c, target={"environment": "production"},
                                        release_packet={"promotion_id": "p", "staging_gate": {
                                            "deployment_run_id": dep}})
        assert said and "thousand-row" in said
        assert "names no build" in quality_bar.plan_refusal(
            c, target={"environment": "production"}, release_packet={"promotion_id": "p"})
