"""GitHub effects require an approved decision for the exact target."""

import json
from uuid import uuid4

from boss_engine.services.github_adapter import IntegrationConfigurationError
from sqlalchemy import text

from apps.api.app import main as api_main


PROJECT = "boss-dogfood"
PR_URL = "https://github.com/example/boss/pull/42"


def _decision(engine, *, status: str, recommendation: dict) -> str:
    decision_id = f"dec_ga_effect_{uuid4().hex[:12]}"
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO decisions (id, project_id, question, recommendation, status) "
                "VALUES (:id, :project_id, 'Review GitHub effect', "
                "CAST(:recommendation AS jsonb), :status)"
            ),
            {
                "id": decision_id, "project_id": PROJECT, "status": status,
                "recommendation": json.dumps(recommendation),
            },
        )
    return decision_id


def _merge(decision_id: str) -> dict:
    return {
        "project_id": PROJECT, "decision_id": decision_id,
        "dedupe_key": f"ga_merge_{uuid4().hex[:12]}", "pull_request_number": 42,
        "pull_request_url": PR_URL, "title": "Reviewed change",
    }


def _deploy(decision_id: str) -> dict:
    return {
        "project_id": PROJECT, "decision_id": decision_id,
        "dedupe_key": f"ga_deploy_{uuid4().hex[:12]}", "merged_sha": "abc123",
        "pull_request_url": PR_URL, "title": "Reviewed deployment",
        "environment": api_main.settings.sandbox_deploy_environment,
    }


def test_merge_rejects_pending_rejected_and_wrong_target_before_github(
    client, engine, monkeypatch,
):
    calls = []

    def fake_merge(*_args):
        calls.append("merge")
        raise IntegrationConfigurationError("test adapter stopped the effect")

    monkeypatch.setattr(api_main.software_factory.github, "merge_pull_request", fake_merge)
    approved = {"action": "review_pull_request", "pull_request_url": PR_URL}
    for status, recommendation in [
        ("pending", approved), ("rejected", approved),
        ("approved", {**approved, "pull_request_url": PR_URL + "-other"}),
        ("approved", {**approved, "action": "review_deployment"}),
    ]:
        response = client.post(
            "/v1/software-factory/merges",
            json=_merge(_decision(engine, status=status, recommendation=recommendation)),
        )
        assert response.status_code == 422, response.text
    assert calls == []

    ready = _decision(engine, status="approved", recommendation=approved)
    accepted = client.post("/v1/software-factory/merges", json=_merge(ready))
    assert accepted.status_code == 200, accepted.text
    assert accepted.json()["status"] == "blocked"
    assert calls == ["merge"]


def test_deployment_rejects_unapproved_and_mismatched_target_before_github(
    client, engine, monkeypatch,
):
    calls = []

    def fake_deploy(*_args):
        calls.append("deploy")
        raise IntegrationConfigurationError("test adapter stopped the effect")

    monkeypatch.setattr(api_main.software_factory.github, "create_deployment", fake_deploy)
    approved = {
        "action": "review_deployment", "pull_request_url": PR_URL,
        "merged_sha": "abc123", "environment": api_main.settings.sandbox_deploy_environment,
    }
    for status, recommendation in [
        ("pending", approved), ("rejected", approved),
        ("approved", {**approved, "merged_sha": "different-sha"}),
        ("approved", {**approved, "environment": "different-stage"}),
    ]:
        response = client.post(
            "/v1/software-factory/deployments",
            json=_deploy(_decision(engine, status=status, recommendation=recommendation)),
        )
        assert response.status_code == 422, response.text
    assert calls == []

    ready = _decision(engine, status="approved", recommendation=approved)
    accepted = client.post("/v1/software-factory/deployments", json=_deploy(ready))
    assert accepted.status_code == 200, accepted.text
    assert accepted.json()["status"] == "blocked"
    assert calls == ["deploy"]


def test_production_deployment_decision_requires_rollback_plan(engine):
    recommendation = {
        "action": "review_deployment", "pull_request_url": PR_URL,
        "merged_sha": "abc123", "environment": "production",
    }
    decision_id = _decision(engine, status="approved", recommendation=recommendation)
    with engine.begin() as connection:
        try:
            api_main.software_factory._require_approved_effect_decision(
                connection, PROJECT, decision_id, "review_deployment",
                pull_request_url=PR_URL, merged_sha="abc123", environment="production",
            )
        except ValueError as error:
            assert "rollback plan" in str(error)
        else:
            raise AssertionError("Production deployment without a rollback plan was allowed")


def test_repaired_change_gets_new_merge_decision_for_its_head(engine):
    from boss_engine.services.core import create_blocking_decision
    from sqlalchemy import text

    with engine.begin() as db:
        project = db.execute(text("SELECT id FROM projects ORDER BY id LIMIT 1")).scalar_one()
        rec = {"action": "review_pull_request", "pull_request_url": "http://boss-local/qa/pull/1",
               "head_sha": "1" * 40, "run_id": "first-build"}
        def ask(recommendation):
            return create_blocking_decision(db, project, None, "Review QA change", recommendation, {},
                                            "merge_decision_required")

        original = ask({**rec, "checks_failing": {"failing_gates": ["web_package_honest"]}})
        repaired = {**rec, "head_sha": "2" * 40, "run_id": "repair"}
        current = ask(repaired)
        assert current != original
        assert ask(repaired) == current
        evidence = db.execute(text("SELECT recommendation FROM decisions WHERE id = :id"),
                              {"id": current}).scalar_one()
        assert evidence["head_sha"] == repaired["head_sha"]
        assert "checks_failing" not in evidence


def test_any_card_that_names_a_head_is_that_heads_card(engine):
    """Not only the merge card: a repeat of any question for a different commit is a new card,
    and a repeat for the same commit (or none) still merges into the one already open."""
    from boss_engine.services.core import create_blocking_decision
    from sqlalchemy import text

    with engine.begin() as db:
        project = db.execute(text("SELECT id FROM projects ORDER BY id LIMIT 1")).scalar_one()
        question = f"Fix these on the change {uuid4().hex[:8]}"

        def ask(**rec):
            return create_blocking_decision(db, project, None, question,
                                            {"action": "start_recheck_followup", **rec}, {}, "t")

        first = ask(head_sha="a" * 40)
        assert ask(head_sha="a" * 40) == first
        assert ask(head_sha="b" * 40) != first
        unbound = ask()
        assert ask(head_sha="") == unbound, "empty and absent heads are one value"
