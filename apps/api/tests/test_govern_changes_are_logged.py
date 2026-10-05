"""Every Govern change is an event the Log can show (`Govern changes`), with from/to and actor —
and a refusal is recorded beside itself, never instead of itself.

Plus the two operator-decided tightenings (activity-govern spec §10 Q3/Q4) and optional
If-Match on every Govern write.
"""

from __future__ import annotations

from datetime import datetime, timezone
from uuid import uuid4

import pytest
from sqlalchemy import text

from apps.api.app import main as api_main
from boss_engine.services import budget as budget_module
from boss_engine.services.budget import BudgetGovernor

PROJECT = "boss-dogfood"


def _events(engine, event_type: str) -> list[dict]:
    with engine.begin() as c:
        return [dict(r) for r in c.execute(text(
            "SELECT actor, data FROM events WHERE project_id = :p AND event_type = :t "
            "ORDER BY created_at"), {"p": PROJECT, "t": event_type}).mappings()]


@pytest.fixture
def fresh(engine):
    def wipe():
        with engine.begin() as c:
            c.execute(text("DELETE FROM events WHERE project_id = :p AND event_type IN "
                           "('settings.changed', 'token_policy.changed', 'budget.refused', "
                           "'budget.intensity_set', 'budget.monthly_cap_set')"), {"p": PROJECT})
            c.execute(text("DELETE FROM project_settings WHERE project_id = :p "
                           "AND key = 'code_agent_max_turns'"), {"p": PROJECT})
            c.execute(text("DELETE FROM cost_receipts WHERE project_id = :p"), {"p": PROJECT})
            c.execute(text("DELETE FROM budget_reservations WHERE project_id = :p"),
                      {"p": PROJECT})
    wipe()
    yield
    wipe()


def test_settings_set_and_clear_are_recorded_with_from_to_and_actor(client, engine, fresh):
    body = {"key": "code_agent_max_turns", "project_id": PROJECT, "actor": "ceo"}
    assert client.put("/v1/settings/code_agent_max_turns", json={**body, "value": 37}
                      ).status_code == 200
    assert client.put("/v1/settings/code_agent_max_turns", json={**body, "value": 37}
                      ).status_code == 200  # no change, no record
    assert client.put("/v1/settings/code_agent_max_turns", json={**body, "value": ""}
                      ).status_code == 200
    changes = _events(engine, "settings.changed")
    assert [(e["data"]["from"], e["data"]["to"]) for e in changes] == [(None, 37), (37, None)]
    assert changes[0]["actor"] == "ceo" and changes[0]["data"]["label"] == "Turn budget per agent"
    assert changes[1]["data"]["source_after"] == "default"


def test_token_policy_pace_and_monthly_cap_are_recorded(client, engine, fresh):
    assert client.put("/v1/token-efficiency/policies", json={
        "project_id": PROJECT, "actor": "ceo",
        "policies": [{"stage": "implement", "max_total_tokens_per_call": 48000}]}
    ).status_code == 200
    token = _events(engine, "token_policy.changed")
    assert token[-1]["data"]["stage"] == "implement"
    assert token[-1]["data"]["fields"]["max_total_tokens_per_call"]["to"] == 48000
    assert token[-1]["actor"] == "ceo"
    client.post("/v1/budget/set_intensity", json={"scope_id": PROJECT, "intensity": 40,
                                                  "actor": "ceo"})
    assert _events(engine, "budget.intensity_set")[-1]["data"]["intensity"] == 40
    client.put("/v1/setup/budget", json={"project_id": PROJECT, "monthly_cap_usd": 150,
                                         "via": "govern"})
    monthly = _events(engine, "budget.monthly_cap_set")[-1]["data"]
    assert monthly == {"monthly_cap_usd_micros": 150_000_000, "via": "govern"}
    client.put("/v1/setup/budget", json={"project_id": PROJECT, "monthly_cap_usd": 160})
    assert _events(engine, "budget.monthly_cap_set")[-1]["data"]["via"] == "repo_and_models"


def test_a_refusal_is_the_same_whether_or_not_its_record_can_be_written(engine, fresh,
                                                                        monkeypatch):
    governor = BudgetGovernor(api_main.settings)
    with engine.begin() as c:
        c.execute(text(
            "INSERT INTO cost_receipts (id, project_id, category, skill_or_model_id, "
            "estimated_usd_micros, actual_usd_micros, status) VALUES (:i, :p, 'model', 'm', "
            "9900000, 9900000, 'actual')"), {"i": f"cost_{uuid4().hex[:10]}", "p": PROJECT})
        recorded = governor.reserve(c, PROJECT, "model", 500_000, "record test", 60)
    assert _events(engine, "budget.refused")[-1]["data"]["kind"] == "cap"

    def broken(*_a, **_k):
        raise RuntimeError("the events table is on fire")

    monkeypatch.setattr(budget_module, "_write_refusal_record", broken)
    with engine.begin() as c:
        unrecorded = governor.reserve(c, PROJECT, "model", 500_000, "record test", 60)
    assert {k: v for k, v in recorded.items() if k != "decision_id"} == \
        {k: v for k, v in unrecorded.items() if k != "decision_id"}


def test_a_waiting_request_is_recorded_once_not_per_retry(engine, fresh):
    governor = BudgetGovernor(api_main.settings)
    with engine.begin() as c:
        c.execute(text("UPDATE budget_policies SET burst_cap_usd_micros_per_min = 100000, "
                       "daily_cap_usd_micros = 100000000, monthly_cap_usd_micros = 1000000000 "
                       "WHERE scope_type = 'project' AND scope_id = :p"), {"p": PROJECT})
        c.execute(text(
            "INSERT INTO cost_receipts (id, project_id, category, skill_or_model_id, "
            "estimated_usd_micros, actual_usd_micros, status) VALUES (:i, :p, 'model', 'm', "
            "150000, 150000, 'actual')"), {"i": f"cost_{uuid4().hex[:10]}", "p": PROJECT})
        for _ in range(4):
            assert governor.reserve(c, PROJECT, "model", 10_000, "same waiter", 60)[
                "refusal"] == "burst"
    burst = [e for e in _events(engine, "budget.refused") if e["data"]["kind"] == "burst"]
    assert len(burst) == 1


# ── The decided tightenings and If-Match ─────────────────────────────────────────────────────

def _authenticated(monkeypatch, engine):
    monkeypatch.setattr(api_main.settings, "require_api_auth", True)
    monkeypatch.setattr(api_main.settings, "api_keys", "ceo-key,provider-key,budget-key")
    monkeypatch.setattr(api_main.settings, "api_key_principals",
                        "ceo-key:ceo,provider-key:gv_provider,budget-key:gv_budget")
    monkeypatch.setattr(api_main.settings, "worker_principal", "ceo")
    with engine.begin() as c:
        for member, permissions in (("gv_provider", ["providerops_manage"]),
                                    ("gv_budget", ["budget_manage"])):
            api_main.members.upsert_member(
                c, project_id=PROJECT, member_id=member, display_name=member,
                member_type="human", role_keys=[], permissions=permissions, actor="ceo")


def test_token_guides_need_budget_rights_now(client, engine, fresh, monkeypatch):
    """§10 Q3, approved: the token-policy write had no permission check at all."""
    _authenticated(monkeypatch, engine)
    body = {"project_id": PROJECT,
            "policies": [{"stage": "implement", "max_total_tokens_per_call": 64000}]}
    refused = client.put("/v1/token-efficiency/policies", json={**body, "actor": "gv_provider"},
                         headers={"Authorization": "Bearer provider-key"})
    assert refused.status_code == 403
    allowed = client.put("/v1/token-efficiency/policies", json={**body, "actor": "gv_budget"},
                         headers={"Authorization": "Bearer budget-key"})
    assert allowed.status_code == 200, allowed.text


def test_a_signed_in_actor_without_budget_rights_is_refused_pace_and_finops(
        client, engine, fresh, monkeypatch):
    """§10 Q4, approved: the console now sends the signed-in actor on Pace and FinOps writes
    (it sent `executive_console` / `system`). An operator without budget rights gets 403."""
    _authenticated(monkeypatch, engine)
    provider = {"Authorization": "Bearer provider-key"}
    assert client.post("/v1/budget/set_intensity", headers=provider, json={
        "scope_id": PROJECT, "intensity": 40, "actor": "gv_provider"}).status_code == 403
    assert client.post("/v1/finops/reconcile-commitments", headers=provider, json={
        "project_id": PROJECT, "actor": "gv_provider"}).status_code == 403
    assert client.post("/v1/finops/initiative-budget", headers=provider, json={
        "project_id": PROJECT, "actor": "gv_provider", "title": "x", "tier": 1,
        "amount_usd_micros": 1}).status_code == 403


def test_if_match_is_optional_and_guards_when_sent(client, engine, fresh):
    knob = {"key": "code_agent_max_turns", "project_id": PROJECT, "actor": "ceo"}
    path = "/v1/settings/code_agent_max_turns"
    assert client.put(path, json={**knob, "value": 40}).status_code == 200  # no If-Match
    version = next(c for c in client.get("/v1/govern", params={"project_id": PROJECT}).json()[
        "controls"] if c["key"] == "code_agent_max_turns")["version"]
    assert client.put(path, json={**knob, "value": 41},
                      headers={"If-Match": f'"{version}"'}).status_code == 200
    stale = client.put(path, json={**knob, "value": 42}, headers={"If-Match": f'"{version}"'})
    assert stale.status_code == 412
    assert stale.json()["detail"]["message"] == "Changed by ceo just now"

    budget_version = client.get("/v1/govern", params={"project_id": PROJECT}).json()[
        "versions"]["budget"]
    assert client.post("/v1/budget/set_intensity", json={
        "scope_id": PROJECT, "intensity": 30, "actor": "ceo"},
        headers={"If-Match": budget_version}).status_code == 200
    assert client.put("/v1/setup/budget", json={"project_id": PROJECT, "monthly_cap_usd": 99},
                      headers={"If-Match": budget_version}).status_code == 412
    stage = {"project_id": PROJECT, "actor": "ceo",
             "policies": [{"stage": "repair", "max_total_tokens_per_call": 50000}]}
    assert client.put("/v1/token-efficiency/policies", json=stage,
                      headers={"If-Match": "unset"}).status_code == 200
    assert client.put("/v1/token-efficiency/policies", json=stage,
                      headers={"If-Match": "unset"}).status_code == 412
