"""A call a subscription answers costs $0 of money; its list-price value is kept, not counted.

boss-1 (2026-10-03) runs on a Claude Code subscription and a Codex subscription, no API key.
Its $10 project budget still went from $2.97 to $9.08 "actual" in a day of test runs and turned
red; the job gate then held every non-P0 job, and three research runs sat queued under "no
worker has picked it up". The Claude CLI reports `total_cost_usd` — the API list price of the
call, subscription or not — and every settlement booked it as money (top driver
`anthropic:claude-sonnet`, the route's model on the `anthropic_claude_code` source).

These drive the real BudgetGovernor, model gateway, Discovery stage spend, governed chat adapter,
job gate and readiness against Postgres; only the model call itself is faked.
"""

from __future__ import annotations

import json
from datetime import timedelta
from uuid import uuid4

import pytest
from sqlalchemy import text

from apps.api.app import main as api_main
from apps.api.app.services import discovery_service, discovery_spend
from boss_engine.services.budget import BudgetGovernor
from boss_engine.services.jobs import JobLeaseService
from boss_engine.services.model_failures import explain
from boss_engine.services.model_routing import GovernedChatAdapter, ModelChoice

PROJECT = "boss-dogfood"


class _Agent:
    """The Claude CLI research agent: every stage reports the list-price cost the CLI does."""

    def __init__(self):
        self.calls = 0

    def run_research(self, prompt: str) -> dict:
        self.calls += 1
        reply = {
            "brief": {"hypothesis": "h", "sources_consulted": ["https://example.com/a"]},
            "findings": [{"claim": "c", "citation_url": "https://example.com/a",
                          "strength": 0.9}],
            "analysis": {"summary": "a"}, "critique": {"verdict": "holds"},
            "outcomes": [{"title": "Build it", "confidence": 0.8}],
        }
        return {"summary": json.dumps(reply), "cost_usd_micros": 250_000,
                "model": "claude-sonnet-4-5", "turns": 1, "usage": {"total_tokens": 100}}


def _bind_source(client, monkeypatch) -> _Agent:
    """The research stages lease `openai_api` (re-kinded to a subscription where a test says)."""
    monkeypatch.setattr(api_main.settings, "boss_edition", "community")
    suffix = uuid4().hex[:8].upper()
    created = client.post("/v1/secret-refs", json={
        "project_id": PROJECT, "ref_kind": "env", "ref_key": f"BOSS_TEST_SUB_{suffix}",
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


def _make_subscription(engine) -> None:
    """The source becomes what boss-1's is: a project-owned flat-rate subscription."""
    with engine.begin() as c:
        c.execute(text(
            "UPDATE entitlement_sources SET entitlement_kind = 'subscription', "
            "cost_mode = 'subscription', multi_tenant_allowed = false, "
            "owner_type = 'project', owner_id = :p "
            "WHERE project_id = :p AND source_id = 'openai_api'"), {"p": PROJECT})


def _run_discovery(engine) -> str:
    run_id = discovery_service.start_discovery(
        project_id=PROJECT, title="Money", seed="research this", context="",
        settings=api_main.settings, actor="ceo")
    with engine.begin() as c:
        job_id = c.execute(text("SELECT state->>'job_id' FROM discovery_runs WHERE id = :id"),
                           {"id": run_id}).scalar_one()
        lease = JobLeaseService(api_main.settings).lease_owned_job(
            c, project_id=PROJECT, job_id=job_id, worker_id="worker-test",
            lease_ttl_seconds=600)
    result = discovery_service.execute_discovery_job(
        api_main.settings, run_id=run_id, job_id=job_id, worker_id="worker-test",
        lease_token=lease["lease_token"])
    assert result == {"status": "completed", "retry": False}, result
    return run_id


def _stage_receipts(engine, run_id: str) -> list[dict]:
    with engine.connect() as c:
        return [dict(r) for r in c.execute(text(
            "SELECT cr.actual_usd_micros, cr.equivalent_usd_micros, "
            "       cr.included_in_subscription, br.amount_usd_micros AS reserved "
            "FROM cost_receipts cr JOIN budget_reservations br ON br.id = cr.reservation_id "
            "WHERE br.reason LIKE :r"), {"r": f"Discovery {run_id} stage %"}).mappings()]


def _status(engine) -> dict:
    with engine.begin() as c:
        return BudgetGovernor(api_main.settings).status_for_project(c, PROJECT)


def test_a_subscription_stage_settles_zero_money_and_keeps_its_list_price_value(
    client, engine, monkeypatch,
):
    agent = _bind_source(client, monkeypatch)
    _make_subscription(engine)
    before = _status(engine)
    run_id = _run_discovery(engine)
    assert agent.calls == 4
    receipts = _stage_receipts(engine, run_id)
    assert len(receipts) == 4
    for receipt in receipts:
        assert receipt["included_in_subscription"] is True
        assert receipt["actual_usd_micros"] == 0, "a subscription call is $0 of money"
        assert receipt["equivalent_usd_micros"] == 250_000, "its list-price value is kept"
        assert receipt["reserved"] == 0, "the router knew the source: nothing to hold"
    after = _status(engine)
    assert after["actual_usd_micros"] == before["actual_usd_micros"], "no money spent"
    assert (after["included_in_subscription_usd_micros"]
            - before["included_in_subscription_usd_micros"]) == 1_000_000
    assert after["budget_state"] == before["budget_state"]
    assert all(d["amount_usd_micros"] > 0 for d in after["top_cost_drivers"]), (
        "a subscription's value is not a money cost driver")
    with engine.connect() as c:
        rows = c.execute(text(
            "SELECT included_in_subscription, cost_usd_micros FROM llm_calls "
            "WHERE project_id = :p AND stage = 'model_gateway' ORDER BY created_at DESC LIMIT 4"),
            {"p": PROJECT}).all()
    assert rows and all(included and cost == 250_000 for included, cost in rows), rows


def test_an_api_key_stage_still_costs_money_and_counts(client, engine, monkeypatch):
    agent = _bind_source(client, monkeypatch)
    before = _status(engine)
    run_id = _run_discovery(engine)
    assert agent.calls == 4
    receipts = _stage_receipts(engine, run_id)
    assert len(receipts) == 4
    for receipt in receipts:
        assert receipt["included_in_subscription"] is False
        assert receipt["actual_usd_micros"] == 250_000
        assert receipt["equivalent_usd_micros"] is None
        assert receipt["reserved"] > 0, "a metered call reserves its estimate"
    after = _status(engine)
    assert after["actual_usd_micros"] - before["actual_usd_micros"] == 1_000_000


# ── the governed chat adapter (agent turns, steps) ────────────────────────────────────────────

class _Inner:
    def __init__(self, usage: dict):
        self.usage = usage

    def create_chat_completion(self, messages, **kwargs):
        return {"content": "ok", "tool_calls": []}, dict(self.usage)


@pytest.mark.parametrize("engine_name,billing,money", [
    ("claude_code", "subscription", 0),
    ("codex", None, 0),                     # no stamp: the engine decides — a subscription
    ("claude_code", "metered", 400_000),    # the CLI on an ANTHROPIC_API_KEY is money
    ("native", "metered", 400_000),
])
def test_a_governed_turn_settles_money_by_who_pays(engine, engine_name, billing, money):
    usage = {"model": "claude-sonnet-4-5", "cost_usd_micros": 400_000}
    if billing:
        usage["billing"] = billing
    with engine.begin() as c:
        adapter = GovernedChatAdapter(
            _Inner(usage), c, api_main.settings, PROJECT, skill_id="test.turn", ref="t",
            estimate_usd_micros=0, choice=ModelChoice(engine_name, "test"))
        adapter.create_chat_completion([{"role": "user", "content": "hi"}])
        row = c.execute(text(
            "SELECT actual_usd_micros, equivalent_usd_micros, included_in_subscription "
            "FROM cost_receipts WHERE project_id = :p AND skill_or_model_id = 'test.turn' "
            "ORDER BY created_at DESC LIMIT 1"), {"p": PROJECT}).mappings().one()
    assert row["actual_usd_micros"] == money
    assert row["included_in_subscription"] is (money == 0)
    assert row["equivalent_usd_micros"] == (400_000 if money == 0 else None)


# ── budget state is money ─────────────────────────────────────────────────────────────────────

def _receipt(c, amount: int, *, included: bool) -> None:
    c.execute(text(
        "INSERT INTO cost_receipts (id, project_id, category, skill_or_model_id, "
        "estimated_usd_micros, actual_usd_micros, status, equivalent_usd_micros, "
        "included_in_subscription) VALUES (:i, :p, 'model', 'anthropic:claude-sonnet', "
        ":a, :actual, 'actual', :e, :inc)"),
        {"i": f"cost_{uuid4().hex[:12]}", "p": PROJECT, "a": amount,
         "actual": amount, "e": amount if included else None, "inc": included})


@pytest.fixture
def ten_dollar_cap(engine):
    with engine.begin() as c:
        saved = c.execute(text(
            "SELECT daily_cap_usd_micros FROM budget_policies WHERE scope_type = 'project' "
            "AND scope_id = :p"), {"p": PROJECT}).scalar_one()
        c.execute(text("UPDATE budget_policies SET daily_cap_usd_micros = 10000000 "
                       "WHERE scope_type = 'project' AND scope_id = :p"), {"p": PROJECT})
        c.execute(text("DELETE FROM cost_receipts WHERE project_id = :p"), {"p": PROJECT})
        c.execute(text("UPDATE budget_reservations SET status = 'released' "
                       "WHERE project_id = :p AND status = 'active'"), {"p": PROJECT})
    yield
    with engine.begin() as c:
        c.execute(text("UPDATE budget_policies SET daily_cap_usd_micros = :v "
                       "WHERE scope_type = 'project' AND scope_id = :p"),
                  {"v": saved, "p": PROJECT})


def test_subscription_value_never_moves_the_budget_state(engine, ten_dollar_cap):
    """boss-1's day, replayed: $9.08 of subscription value is not $9.08 spent. A receipt
    classified as included — including one written before this rule with its list price in
    `actual_usd_micros` (the schema backfill keeps amounts) — counts as $0 of money."""
    with engine.begin() as c:
        _receipt(c, 9_080_000, included=True)
    status = _status(engine)
    assert status["budget_state"] == "green", status
    assert status["actual_usd_micros"] == 0
    assert status["included_in_subscription_usd_micros"] == 9_080_000
    assert status["included_cost_drivers"][0]["skill_or_model_id"] == "anthropic:claude-sonnet"
    with engine.begin() as c:
        _receipt(c, 9_500_000, included=False)
    assert _status(engine)["budget_state"] == "red", "money still governs"


# ── held work says it is held ─────────────────────────────────────────────────────────────────

def test_a_job_the_red_budget_holds_says_so_not_no_worker(engine, ten_dollar_cap):
    with engine.begin() as c:
        _receipt(c, 9_500_000, included=False)
    kind = f"test.held.{uuid4().hex[:8]}"
    leases = JobLeaseService(api_main.settings)
    with engine.begin() as c:
        job = leases.create_job(c, PROJECT, kind, {"discovery_run_id": "none"})
        claimed = leases.claim_job(c, "worker-test", [kind], 60)
        assert claimed is None, "red holds non-P0 work"
        error = c.execute(text("SELECT last_error FROM jobs WHERE id = :j"),
                          {"j": job["id"]}).scalar_one()
    assert error["kind"] == "budget_hold"
    assert error["message"].startswith(
        "Held — the budget is red (95% of today's $10 used), so only P0 work runs. "
        "It resets at 00:00 UTC, or raise the daily cap in Budget."), error
    said = explain(error["message"])
    assert said["kind"] == "budget_hold"
    assert said["headline"].startswith("Held — the budget is red (95% of today's $10 used)")
    assert said["action"].startswith("It resets at 00:00 UTC, or raise the daily cap in Budget.")
    assert said["href"] == "/?area=budget"

    # The discovery attention reads the job, and says held — not "no worker has picked it up".
    row = {"status": "running", "error": None, "job_status": "queued",
           "job_error": json.dumps(error), "job_lease_owner": None,
           "job_run_after": None, "job_age": timedelta(minutes=10)}
    attention = discovery_service._attention(_Row(row), {"job_id": job["id"]})
    assert attention["kind"] == "budget_hold", attention
    assert "no worker" not in attention["headline"].lower()
    assert attention["headline"].startswith("Held — the budget is red")


class _Row(dict):
    def keys(self):  # noqa: D401 — the shape discovery_service reads a row through
        return list(super().keys())


def test_readiness_at_start_flags_a_red_budget_and_notes_an_orange_one(engine, ten_dollar_cap):
    from boss_engine.services import workflow_readiness as ready

    spec = {"steps": [{"id": "research", "kind": "capability",
                       "binding": {"capability": "research"}}]}
    with engine.begin() as c:
        _receipt(c, 8_700_000, included=False)
        orange = ready.preflight(c, api_main.settings, PROJECT, spec)
    assert all(m["key"] != "budget" for m in orange["missing"])
    assert [n["key"] for n in orange["notes"]] == ["budget"]
    assert "orange" in orange["notes"][0]["text"]

    with engine.begin() as c:
        _receipt(c, 800_000, included=False)
        red = ready.preflight(c, api_main.settings, PROJECT, spec)
    budget = next(m for m in red["missing"] if m["key"] == "budget")
    assert budget["label"] == "Budget is red — work will be held"
    assert budget["area"] == "budget"
    assert "95% of today's $10 used" in budget["detail"]
    assert budget["fix"].startswith("It resets at 00:00 UTC, or raise the daily cap in Budget.")
    assert not red["ready"]
