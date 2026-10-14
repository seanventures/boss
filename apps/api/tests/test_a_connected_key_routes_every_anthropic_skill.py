"""A key connected in Setup → Add a model routes every skill its provider's API would.

Found 2026-10-13 on the laptop: with the Claude subscription disconnected, the only working
Anthropic source was the console key `setup-anthropic`. Connecting saved it with
`workload_classes_allowed = []`, so the router refused architecture.draft (owner_sandbox work)
with `no_eligible_source` and the HelpDesk run deadlettered. Its supported model was the concrete
id it was proved with (`claude-sonnet-4-5`), which never matched the registry's families.
"""
from __future__ import annotations

import json
import uuid

import pytest
from sqlalchemy import text

from apps.api.app.config import get_settings
from boss_engine.services.entitlements import EntitlementBrokerService

KEY = "setup-anthropic"


@pytest.fixture()
def project(engine):
    p = "keyroute_" + uuid.uuid4().hex[:10]
    with engine.begin() as c:
        c.execute(text("INSERT INTO orgs(id,name) VALUES(:p,:p)"), {"p": p})
        c.execute(text("INSERT INTO workspaces(id,org_id,name) VALUES(:p,:p,:p)"), {"p": p})
        c.execute(text("INSERT INTO projects(id,workspace_id,name,slug) VALUES(:p,:p,:p,:p)"),
                  {"p": p})
        c.execute(text("INSERT INTO budget_policies(id,scope_type,scope_id,daily_cap_usd_micros,"
                       "monthly_cap_usd_micros,burst_cap_usd_micros_per_min) "
                       "VALUES(:p,'project',:p,10000000,10000000,0)"), {"p": p})
        EntitlementBrokerService(get_settings()).seed_default_sources(c, p, "test")
        # The operator disconnected the subscription on purpose.
        c.execute(text("UPDATE entitlement_sources SET connection_status='revoked', "
                       "status='missing_credentials' WHERE project_id=:p "
                       "AND source_id='anthropic_claude_code'"), {"p": p})
    return p


def _connect_key(connection, broker, p, model="claude-sonnet-4-5"):
    """The payload Setup's key connection (setup_actions._connect_key) saves: no classes."""
    broker.upsert_source(connection, {
        "project_id": p, "provider": "anthropic", "source_id": KEY, "source_kind": "api",
        "entitlement_kind": "api_key", "owner_type": "project", "owner_id": p,
        "cost_mode": "usage_based_api", "auth_method": "api_key", "status": "active",
        "connection_status": "connected", "runtime_supported": True,
        "observed_capacity": {"credential_present": True},
        "supported_models": [model],
        "connection_metadata": {"base_url": "https://api.anthropic.com", "model": model}},
        actor="test", emit_events=False)


def _route(broker, connection, p, only=None, skill="architecture.draft"):
    return broker.preview_route(connection, p, skill_id=skill, task_kind="reasoning",
                                workload_class="owner_sandbox", risk_tier=1,
                                preferred_capabilities=["reasoning", "writing"],
                                only_source_id=only)


def test_connecting_a_key_gives_it_the_providers_api_workload_classes(engine, project):
    broker = EntitlementBrokerService(get_settings())
    with engine.begin() as c:
        _connect_key(c, broker, project)
        classes = c.execute(text("SELECT workload_classes_allowed FROM entitlement_sources "
                                 "WHERE project_id=:p AND source_id=:s"),
                            {"p": project, "s": KEY}).scalar_one()
    assert {"owner_sandbox", "single_tenant_customer"} <= set(classes)


def test_architecture_routes_to_the_connected_key_when_the_subscription_is_gone(engine, project):
    broker = EntitlementBrokerService(get_settings())
    with engine.begin() as c:
        _connect_key(c, broker, project)
        for only in (KEY, None):
            route = _route(broker, c, project, only=only)
            assert route["source_id"] == KEY, route["rejected_sources"]
            assert route["model_id"] == "anthropic:claude-sonnet"
        refused = _route(broker, c, project, only="anthropic_claude_code")
        assert refused["source_id"] is None
        assert refused["denial_reason"] == "no_eligible_source"


def test_a_key_saved_without_classes_is_repaired_at_startup(engine, project):
    broker = EntitlementBrokerService(get_settings())
    with engine.begin() as c:
        _connect_key(c, broker, project)
        c.execute(text("UPDATE entitlement_sources SET workload_classes_allowed='[]'::jsonb "
                       "WHERE project_id=:p AND source_id=:s"), {"p": project, "s": KEY})
        assert _route(broker, c, project, only=KEY)["source_id"] is None
        broker.seed_default_sources(c, project, "startup")  # what boot runs
        assert _route(broker, c, project, only=KEY)["source_id"] == KEY
        assert broker.repair_connected_key_sources(c, project) == []  # idempotent


@pytest.mark.parametrize("model,family", [
    ("claude-sonnet-5", "claude-sonnet"), ("claude-sonnet-4-5", "claude-sonnet"),
    ("claude-opus-4-1", "claude-opus"), ("claude-haiku-4-5", "claude-haiku"),
    ("claude-sonnet", "claude-sonnet"), ("anthropic:claude-opus", "claude-opus"),
])
def test_a_concrete_model_id_matches_its_family(model, family):
    broker = EntitlementBrokerService(get_settings())
    assert broker._registry_families("anthropic", [model]) == {family}
    ids = {m["id"] for m in broker._models_for_source("anthropic", [model], [])}
    assert ids == {f"anthropic:{family}"}


def test_an_unrelated_model_matches_nothing():
    broker = EntitlementBrokerService(get_settings())
    assert broker._registry_families("anthropic", ["claude-sonnetish"]) == set()
    assert json.dumps(sorted(broker._registry_families("anthropic", ["gpt-5"]))) == "[]"
