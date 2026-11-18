"""A STACK plan to a production target passes the same chokepoint as every other production plan
(BOSSAPP-1362): plan_stack_deploy -> quality_bar.stack_plan_decision. Walked through the direct
plan route, the console's target deploy, and rollback (exempt only to the previous healthy commit).
"""
import json
from uuid import uuid4

from sqlalchemy import text

from boss_engine.services import quality_bar

from apps.api.tests.test_a_stack_deploys_only_through_an_approved_changeset import (  # noqa: F401
    HOME, stack)

PASSED = '["code_review_passed","security_clean","consistent","acceptance_met"]'
JUDGED = "a" * 40
GAPS = {"acceptance": {"verdict": "not_met", "stack": {}, "items": [
    {"n": 1, "text": "Export handles a thousand-row file", "status": "partial", "tier": "P2"}]},
        "review_summary": "FINDING: MAJOR server/a.ts:3 — the list ignores the filter\nVERDICT: REFUTED"}


def _build(engine, evidence, sha=JUDGED):
    run = f"crun_stk{uuid4().hex[:8]}"
    with engine.begin() as c:
        c.execute(text(
            "INSERT INTO code_task_runs (id, project_id, status, title, spec, finalize_ctx) "
            "VALUES (:r, :p, 'completed', 'App', '{}'::jsonb, CAST(:f AS jsonb))"),
            {"r": run, "p": HOME, "f": json.dumps({"final_head_sha": sha})})
        c.execute(text(
            "INSERT INTO quality_gates (id, project_id, code_task_run_id, risk_tier, status, "
            "fail_closed, gates_passed, gates_failed, evidence) VALUES (:i, :p, :r, 1, 'pass', "
            "true, CAST(:gp AS jsonb), '[]'::jsonb, CAST(:e AS jsonb))"),
            {"i": f"qg_{run}", "p": HOME, "r": run, "gp": PASSED, "e": json.dumps(evidence)})
    return run


def _production(engine, stack):
    with engine.begin() as c:
        c.execute(text("UPDATE deployment_targets SET environment = 'production' WHERE id = :i"),
                  {"i": stack["target"]["id"]})
    return stack["target"]["id"]


def _count(engine, target):
    with engine.begin() as c:
        return c.execute(text("SELECT count(*) FROM stack_deployments WHERE target_id = :t"),
                         {"t": target}).scalar()


def test_the_direct_plan_route_needs_a_build_that_passes_and_ships_its_commit(client, engine, stack):
    target = _production(engine, stack)

    def plan(**extra):
        return client.post("/v1/deployments/stack/plan", json={
            "project_id": HOME, "target_id": target, "ref": "main", "actor": "ceo", **extra})

    for ref in ("main", "pull/7", JUDGED):
        none = client.post("/v1/deployments/stack/plan", json={
            "project_id": HOME, "target_id": target, "ref": ref, "actor": "ceo"})
        assert none.status_code == 409 and "names no build" in none.text, (ref, none.text)
    gaps = plan(run_id=_build(engine, GAPS))
    assert gaps.status_code == 409 and "thousand-row" in gaps.text, gaps.text
    good = _build(engine, {})
    other = plan(run_id=good, ref="b" * 40)
    assert other.status_code == 409 and "not the one the build judged" in other.text, other.text
    assert _count(engine, target) == 0, "a production stack release was planned past the bar"


def test_the_console_target_deploy_is_judged_too(client, engine, stack):
    target = _production(engine, stack)
    response = client.post(f"/v1/infra/targets/{target}/deploy", json={
        "project_id": HOME, "actor": "ceo"})
    assert "production quality bar" in response.text, (response.status_code, response.text)
    assert _count(engine, target) == 0


def test_a_stack_rollback_is_exempt_only_to_the_previous_healthy_commit(engine, stack):
    target = _production(engine, stack)
    other = f"deptgt_other{uuid4().hex[:6]}"
    ids = {}
    with engine.begin() as c:
        for key, sha, status, when in (("old", "1" * 40, "healthy", "2026-01-01"),
                                       ("bad", "2" * 40, "failed", "2026-01-02"),
                                       ("cur", "3" * 40, "healthy", "2026-01-03")):
            ids[key] = f"stkdep_{key}{uuid4().hex[:6]}"
            c.execute(text(
                "INSERT INTO stack_deployments (id, project_id, target_id, repository, ref, "
                "commit_sha, manifest, manifest_text, status, created_by, created_at, "
                "apply_started_at) VALUES (:i, :p, :t, 'acme/shop', 'main', :s, '{}'::jsonb, '', "
                ":st, 'ceo', :w, :w)"),
                {"i": ids[key], "p": HOME, "t": target, "s": sha, "st": status, "w": when})
        t = {"id": target, "environment": "production", "target_kind": "ssh"}
        assert quality_bar._stack_rollback_ok(c, target, ids["cur"], "1" * 40) is True
        assert quality_bar._stack_rollback_ok(c, target, ids["cur"], "1" * 12) is True
        # a commit that never ran healthy before it, the current one, or another target: judged
        assert quality_bar._stack_rollback_ok(c, target, ids["cur"], "2" * 40) is False
        assert quality_bar._stack_rollback_ok(c, target, ids["cur"], "9" * 40) is False
        assert quality_bar._stack_rollback_ok(c, other, ids["cur"], "1" * 40) is False
        assert quality_bar._stack_rollback_ok(c, target, None, "1" * 40) is False
        # the decision: exempt for the rollback commit, judged (no build) for anything else
        assert quality_bar.stack_plan_decision(c, target=t, run_id=None, ref="1" * 40,
                                               rollback_of=ids["cur"]) == (None, None)
        said, _ = quality_bar.stack_plan_decision(c, target=t, run_id=None, ref="9" * 40,
                                                  rollback_of=ids["cur"])
        assert said and "names no build" in said
