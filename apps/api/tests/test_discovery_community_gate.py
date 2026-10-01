"""Community Discovery runs as a governed job: every research stage is reserved, leased,
receipted and settled on its own, and a retry or a crash never pays for a stage twice.

These are the acceptance conditions DISCOVERY_COMMUNITY_GA_BLOCKER.md set for lifting the
Community gate, run against Postgres with only the research agent's model call faked:
budget denial makes zero model calls; a worker crash cannot duplicate an effect; retries and
failures are visible; a completed run has matching budget, receipt and event evidence.
"""

import json
from uuid import uuid4

import pytest
from sqlalchemy import text

from apps.api.app import main as api_main
from apps.api.app.config import get_settings
from apps.api.app.services import discovery_service, discovery_spend
from boss_engine.services.jobs import JobLeaseService


PROJECT = "boss-dogfood"
STAGES = ["signal_sweep", "synthesize", "vet", "shape"]


class _Agent:
    """Stands in for the Claude CLI research agent. Records which stage each call was."""

    def __init__(self):
        self.calls: list[str] = []
        self.fail_at = None
        self.fail_with = None

    def run_research(self, prompt: str) -> dict:
        # Each stage's prompt opens with its own role.
        name = ("synthesize" if prompt.startswith("You are a market analyst")
                else "vet" if prompt.startswith("You are a sharp, skeptical venture investor")
                else "shape" if prompt.startswith("You are a product strategist")
                else "signal_sweep")
        self.calls.append(name)
        if self.fail_at == name and self.fail_with is not None:
            error, self.fail_with = self.fail_with, None
            raise error
        reply = {
            "brief": {"hypothesis": "h", "sources_consulted": ["https://example.com/a"]},
            "findings": [{"claim": "people want it", "citation_url": "https://example.com/a",
                          "strength": 0.9}],
            "analysis": {"summary": "a market"},
            "critique": {"verdict": "holds"},
            "outcomes": [{"title": "Build it", "confidence": 0.8}],
        }
        return {"summary": json.dumps(reply), "cost_usd_micros": 1_000,
                "model": "fake-research", "turns": 1, "usage": {"total_tokens": 100}}


@pytest.fixture
def governed(client, engine, monkeypatch):
    """Community edition, with a model source the research stages can lease."""
    monkeypatch.setattr(api_main.settings, "boss_edition", "community")
    suffix = uuid4().hex[:8].upper()
    created = client.post("/v1/secret-refs", json={
        "project_id": PROJECT, "ref_kind": "env", "ref_key": f"BOSS_TEST_DISCOVERY_{suffix}",
        "provider": "openai", "secret_kind": "api_token", "status": "active", "actor": "ceo",
    })
    assert created.status_code == 201, created.text
    bound = client.post("/v1/entitlements/sources/openai_api/credential-ref", json={
        "project_id": PROJECT, "secret_ref_id": created.json()["id"], "actor": "ceo"})
    assert bound.status_code == 200, bound.text
    monkeypatch.setattr(discovery_spend, "RESEARCH_SOURCE_ID", "openai_api")
    agent = _Agent()
    monkeypatch.setattr(discovery_service, "make_code_agent", lambda *a, **k: agent)
    return agent


def _start(seed="research this") -> str:
    return discovery_service.start_discovery(
        project_id=PROJECT, title="Governed", seed=seed, context="",
        settings=api_main.settings, actor="ceo")


def _lease(engine, run_id: str) -> dict:
    with engine.begin() as connection:
        job_id = connection.execute(text(
            "SELECT state->>'job_id' FROM discovery_runs WHERE id = :id"), {"id": run_id},
        ).scalar_one()
        lease = JobLeaseService(api_main.settings).lease_owned_job(
            connection, project_id=PROJECT, job_id=job_id, worker_id="worker-test",
            lease_ttl_seconds=600)
    return {"job_id": job_id, "worker_id": "worker-test", "lease_token": lease["lease_token"]}


def _hand_back(engine, lease: dict) -> None:
    """What the worker does after an attempt it did not complete: the job is queued again."""
    with engine.begin() as connection:
        JobLeaseService(api_main.settings).fail(
            connection, lease["worker_id"], lease["job_id"], lease["lease_token"],
            {"message": "attempt ended"}, True, 0)


def _execute(run_id: str, lease: dict) -> dict:
    return discovery_service.execute_discovery_job(api_main.settings, run_id=run_id, **lease)


def _receipts(engine, run_id: str) -> list[str]:
    with engine.connect() as connection:
        return [row[0] for row in connection.execute(text(
            "SELECT status FROM receipts WHERE dedupe_key LIKE :p ORDER BY created_at"),
            {"p": f"model-gateway:{PROJECT}:discovery:{run_id}:%"})]


def _events(engine, run_id: str, kind: str) -> int:
    with engine.connect() as connection:
        return connection.execute(text(
            "SELECT count(*) FROM events WHERE entity_id = :id AND event_type = :t"),
            {"id": run_id, "t": kind}).scalar_one()


def test_starting_queues_a_governed_job_and_runs_nothing_in_process(client, engine, governed):
    response = client.post("/v1/discoveries", json={"project_id": PROJECT, "seed": "new idea"})
    assert response.status_code == 200, response.text
    run_id = response.json()["run_id"]
    assert governed.calls == [], "no model call on the request — the worker runs it"
    with engine.connect() as connection:
        job = connection.execute(text(
            "SELECT j.kind, j.status, j.payload FROM discovery_runs d JOIN jobs j "
            "ON j.id = d.state->>'job_id' WHERE d.id = :id"), {"id": run_id}).mappings().one()
    assert job["kind"] == "discovery.run" and job["status"] == "queued"
    assert job["payload"]["agent_id"] == "discovery"


def test_a_completed_run_has_matching_budget_receipt_and_event_evidence(engine, governed):
    run_id = _start()
    result = _execute(run_id, _lease(engine, run_id))
    assert result == {"status": "completed", "retry": False}
    assert governed.calls == STAGES
    assert _receipts(engine, run_id) == ["succeeded"] * 4, "one settled receipt per stage"
    assert _events(engine, run_id, "discovery.stage_settled") == 4
    assert _events(engine, run_id, "discovery.completed") == 1
    with engine.connect() as connection:
        reservations = connection.execute(text(
            "SELECT status, count(*) FROM budget_reservations WHERE reason LIKE :r "
            "GROUP BY status"), {"r": f"Discovery {run_id} stage %"}).all()
        status = connection.execute(text(
            "SELECT status FROM discovery_runs WHERE id = :id"), {"id": run_id}).scalar_one()
    assert dict(reservations) == {"consumed": 4}, reservations
    assert status == "completed"


def test_a_retry_after_a_failed_stage_does_not_pay_again_for_finished_stages(engine, governed):
    run_id = _start()
    governed.fail_at, governed.fail_with = "vet", RuntimeError("research tool crashed")
    lease = _lease(engine, run_id)
    first = _execute(run_id, lease)
    assert first["status"] == "failed" and first["retry"] is True
    assert governed.calls == ["signal_sweep", "synthesize", "vet"]
    assert _events(engine, run_id, "discovery.stage_failed") == 1, "the failure is visible"

    _hand_back(engine, lease)
    second = _execute(run_id, _lease(engine, run_id))
    assert second == {"status": "completed", "retry": False}
    assert governed.calls == ["signal_sweep", "synthesize", "vet", "vet", "shape"], (
        "finished stages replay from their receipts; only the failed one is called again")
    assert _receipts(engine, run_id).count("succeeded") == 4
    assert _events(engine, run_id, "discovery.stage_replayed") == 2


def test_a_crash_mid_call_blocks_for_reconciliation_and_is_never_called_again(engine, governed):
    run_id = _start()
    governed.fail_at, governed.fail_with = "synthesize", TimeoutError("read timed out")
    lease = _lease(engine, run_id)
    first = _execute(run_id, lease)
    assert first["status"] == "failed" and first["retry"] is False
    assert "reconcile" in first["error"].lower()
    assert _receipts(engine, run_id) == ["succeeded", "in_progress"]
    calls = list(governed.calls)

    with engine.begin() as connection:  # an operator re-opens it without reconciling
        connection.execute(text("UPDATE discovery_runs SET status = 'running' WHERE id = :id"),
                           {"id": run_id})
    _hand_back(engine, lease)
    again = _execute(run_id, _lease(engine, run_id))
    assert again["retry"] is False and governed.calls == calls, (
        "a stage that may already be charged is never called again")
    assert _events(engine, run_id, "discovery.reconciliation_required") >= 2


@pytest.fixture
def budget_policy(engine):
    """Change this project's budget policy for one test, and put it back."""
    with engine.connect() as connection:
        saved = dict(connection.execute(text(
            "SELECT daily_cap_usd_micros, burst_cap_usd_micros_per_min FROM budget_policies "
            "WHERE scope_type = 'project' AND scope_id = :p"), {"p": PROJECT}).mappings().one())

    def set_policy(**values):
        with engine.begin() as connection:
            for column, value in values.items():
                connection.execute(text(
                    f"UPDATE budget_policies SET {column} = :v "
                    "WHERE scope_type = 'project' AND scope_id = :p"), {"v": value, "p": PROJECT})

    yield set_policy
    set_policy(**saved)


def test_a_hard_budget_refusal_makes_zero_model_calls(engine, governed, budget_policy):
    budget_policy(daily_cap_usd_micros=1)
    run_id = _start()
    result = _execute(run_id, _lease(engine, run_id))
    assert result["status"] == "failed" and result["retry"] is False
    assert governed.calls == []
    assert _receipts(engine, run_id) == []
    assert _events(engine, run_id, "discovery.spend_refused") == 1


def test_a_per_minute_cap_smaller_than_a_stage_waits_and_retries_instead_of_failing(
    engine, governed, budget_policy,
):
    """boss-1's default policy allows $0.10/min and a stage estimate is larger: the stage used
    to reserve its full estimate, could never be granted, and the run failed at its first
    stage. A stage now reserves no more than the cap, and a burst refusal is retried."""
    budget_policy(burst_cap_usd_micros_per_min=50_000)
    run_id = _start()
    for _ in range(8):
        lease = _lease(engine, run_id)
        result = _execute(run_id, lease)
        if result["status"] == "completed":
            break
        # A wait is not a failed attempt: the job completes and a fresh one is queued.
        assert result["status"] == "deferred" and result["retry"] is False, result
        with engine.begin() as connection:  # a minute passes
            connection.execute(text(
                "UPDATE budget_reservations SET created_at = created_at - interval '2 minutes' "
                "WHERE project_id = :p"), {"p": PROJECT})
    assert result == {"status": "completed", "retry": False}
    assert governed.calls == STAGES, "each stage called exactly once across the retries"
    with engine.connect() as connection:
        amounts = [row[0] for row in connection.execute(text(
            "SELECT amount_usd_micros FROM budget_reservations WHERE reason LIKE :r"),
            {"r": f"Discovery {run_id} stage %"})]
        decisions = connection.execute(text(
            "SELECT count(*) FROM decisions WHERE question LIKE :q"),
            {"q": f"Budget refused Discovery {run_id}%"}).scalar_one()
    assert amounts and max(amounts) <= 50_000, amounts
    assert decisions == 0, "a refusal that clears by itself asks nobody to decide anything"
    assert _events(engine, run_id, "discovery.waiting_for_budget") >= 3


@pytest.fixture
def owned_source(engine):
    """Make the research source owner-scoped, as a seeded subscription is, then restore it."""
    with engine.connect() as connection:
        saved = dict(connection.execute(text(
            "SELECT owner_type, owner_id, entitlement_kind, multi_tenant_allowed "
            "FROM entitlement_sources "
            "WHERE project_id = :p AND source_id = 'openai_api'"), {"p": PROJECT}).mappings().one())

    def own(owner_type: str, owner_id: str) -> None:
        with engine.begin() as connection:
            connection.execute(text(
                "UPDATE entitlement_sources SET owner_type = :t, owner_id = :o, "
                "entitlement_kind = 'subscription', multi_tenant_allowed = false "
                "WHERE project_id = :p AND source_id = 'openai_api'"),
                {"t": owner_type, "o": owner_id, "p": PROJECT})

    yield own
    with engine.begin() as connection:
        connection.execute(text(
            "UPDATE entitlement_sources SET owner_type = :owner_type, owner_id = :owner_id, "
            "entitlement_kind = :entitlement_kind, multi_tenant_allowed = :multi_tenant_allowed "
            "WHERE project_id = :p AND source_id = 'openai_api'"),
            {**saved, "p": PROJECT})


def test_the_workspace_operators_subscription_serves_research_started_by_a_member(
    engine, governed, owned_source,
):
    """The seed gives every subscription to the workspace operator ("founder"); research started
    by a member ("ceo") was refused as pooling — every governed stage on a default install."""
    owned_source("workspace_operator", "founder")
    run_id = _start()
    assert _execute(run_id, _lease(engine, run_id)) == {"status": "completed", "retry": False}
    assert governed.calls == STAGES


def test_someone_elses_personal_subscription_is_still_not_pooled(engine, governed, owned_source):
    owned_source("operator", "somebody_else")
    run_id = _start()
    result = _execute(run_id, _lease(engine, run_id))
    assert result["status"] == "failed" and result["retry"] is False
    assert "pooled" in result["error"] and governed.calls == []


def test_the_worker_route_runs_the_leased_job(client, engine, governed):
    run_id = _start()
    response = client.post(f"/v1/discoveries/{run_id}/execute", json=_lease(engine, run_id))
    assert response.status_code == 200, response.text
    assert response.json()["status"] == "completed"


def test_startup_leaves_job_owned_runs_to_the_job_system_and_reconciles_legacy_ones(
    engine, monkeypatch,
):
    suffix = uuid4().hex[:10]
    legacy, owned = f"drun_legacy_{suffix}", f"drun_owned_{suffix}"
    settings = get_settings()
    monkeypatch.setattr(settings, "boss_edition", "community")
    with engine.begin() as connection:
        for run_id, state in ((legacy, {"governance": {"reservation_id": "res_unknown"}}),
                              (owned, {"job_id": "job_still_leased"})):
            connection.execute(text(
                "INSERT INTO discovery_runs (id, project_id, status, phase, title, seed, state) "
                "VALUES (:id, :p, 'running', 'synthesize', 'Interrupted', 'research this', "
                "CAST(:s AS jsonb))"), {"id": run_id, "p": PROJECT, "s": json.dumps(state)})

    def no_model(*_args, **_kwargs):
        raise AssertionError("startup replayed model spend")

    monkeypatch.setattr(discovery_service, "make_code_agent", no_model)
    result = discovery_service.resume_interrupted(settings)
    assert legacy in result["blocked_run_ids"] and owned not in result["blocked_run_ids"]
    with engine.connect() as connection:
        statuses = dict(connection.execute(text(
            "SELECT id, status FROM discovery_runs WHERE id IN (:a, :b)"),
            {"a": legacy, "b": owned}).all())
    assert statuses == {legacy: "failed", owned: "running"}
