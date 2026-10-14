"""The console's Anthropic API key, end to end on the database (BOSSAPP-530).

Store credential on the Anthropic API source -> the source is connected and runtime-supported,
the model routing offers it for Claude, `claude` runs resolve to it, and disconnecting the
subscription falls through to it. The key never appears in a response. Engine-only cases are in
engine/tests/test_the_console_anthropic_key_runs_claude.py.
"""
from __future__ import annotations

import pytest
from sqlalchemy import text

from boss_engine.services import claude_credential as cc
from boss_engine.services import secret_backends, secrets_service
from boss_engine.services.secret_backends import SecretNotFound

PROJECT_ID = "boss-dogfood"
TOKEN = "sk-ant-oat01-" + "t" * 48          # shaped like a login; not a real one
KEY = "sk-ant-api03-" + "k" * 48            # shaped like an API key; not a real one


class _Memory:
    def __init__(self, held):
        self.held = held

    def get(self, key):
        if key not in self.held:
            raise SecretNotFound(key)
        return self.held[key]

    def put(self, key, value):
        self.held[key] = value

    def delete(self, key):
        self.held.pop(key, None)


@pytest.fixture
def store(monkeypatch):
    held: dict[str, str] = {}
    mem = _Memory(held)
    monkeypatch.setattr(secret_backends, "backend_for", lambda kind, settings: mem)
    monkeypatch.setattr(secrets_service, "backend_for", lambda kind, settings: mem)
    monkeypatch.setattr(secret_backends, "pick_default_backend", lambda settings: ("memory", []))
    for name in ("CLAUDE_CODE_OAUTH_TOKEN", "ANTHROPIC_API_KEY"):
        monkeypatch.delenv(name, raising=False)
    return held


def _settings():
    from apps.api.app.config import get_settings

    return get_settings().model_copy(update={"claude_code_oauth_token": "",
                                             "anthropic_api_key": None})


def _store_key(client):
    response = client.post("/v1/entitlements/sources/anthropic_api/credential", json={
        "project_id": PROJECT_ID, "value": KEY, "backend": "platform_secret_store",
        "actor": "ceo"})
    assert response.status_code == 201, response.text
    assert KEY not in response.text
    return response.json()


def _sources(client) -> dict:
    response = client.get(f"/v1/entitlements/sources?project_id={PROJECT_ID}")
    assert response.status_code == 200, response.text
    assert KEY not in response.text and TOKEN not in response.text
    return {i["source_id"]: i for i in response.json()["items"]}


def _connect_subscription(engine, store):
    """What the console's subscription connect leaves behind: the login in the store, its
    reference, and the source connected."""
    from apps.api.app.config import get_settings
    from boss_engine.services.cli_harness import SUBSCRIPTION_SECRET_SCOPE
    from boss_engine.services.secrets_service import put_secret

    with engine.begin() as c:
        stored = put_secret(c, get_settings(), project_id=PROJECT_ID, name="CLAUDE_CODE_OAUTH_TOKEN",
                            value=TOKEN, backend="memory", scope_id=SUBSCRIPTION_SECRET_SCOPE,
                            actor="ceo")
        c.execute(text("UPDATE secret_refs SET provider='anthropic' WHERE id=:i"),
                  {"i": stored["secret_ref_id"]})
        c.execute(text("UPDATE entitlement_sources SET connection_status='connected', "
                       "status='active', runtime_supported=true WHERE project_id=:p "
                       "AND source_id='anthropic_claude_code'"), {"p": PROJECT_ID})


def test_a_stored_key_connects_the_source_and_runs_claude(client, engine, store):
    _store_key(client)
    rows = _sources(client)
    api = rows["anthropic_api"]
    assert api["runtime_supported"] is True
    assert api["effective_status"] == "active" and api["connection_status"] == "connected"
    assert api["claude_credential"]["active"] == "stored_key"
    assert api["claude_credential"]["billing"] == "metered"
    assert rows["anthropic_claude_code"]["claude_credential"]["active"] == "stored_key"

    settings = _settings()
    with engine.begin() as c:
        got = cc.resolve_claude_credential(settings, connection=c, project_id=PROJECT_ID)
    assert (got.kind, got.value) == ("stored_key", KEY)
    # A process with no database connection (the chat adapter, a sandboxed agent) finds it too.
    assert cc.resolve_claude_credential(settings).value == KEY

    # Routing: the claude_code lane is offered on the key, as money, and the gateway's router
    # can select the source for an Anthropic model.
    from boss_engine.services.entitlements import EntitlementBrokerService
    from boss_engine.services.model_routing import resolve_job
    from boss_engine.services.setup_service import connected_engines

    with engine.begin() as c:
        lane = connected_engines(c, settings, PROJECT_ID)["claude_code"]
        assert lane["source_id"] == "anthropic_api" and lane["kind"] == "metered"
        job = resolve_job(c, settings, PROJECT_ID, "build")
        assert job.engine == "claude_code" and job.metered
        route = EntitlementBrokerService(settings).simulate_model_route_decision(
            c, PROJECT_ID, skill_id="model.generate_text", task_kind="meeting",
            workload_class="owner_sandbox", risk_tier=1, preferred_capabilities=["reasoning"],
            only_source_id="anthropic_api")
        assert route["selected_source_id"] == "anthropic_api", route.get("rejected_sources")
        assert cc.active_source_id(c, settings, PROJECT_ID) == "anthropic_api"


def test_subscription_first_then_the_preference_then_disconnect_falls_through(client, engine,
                                                                              store):
    from boss_engine.services.setup_service import connected_engines

    _connect_subscription(engine, store)
    _store_key(client)
    settings = _settings()
    with engine.begin() as c:
        got = cc.resolve_claude_credential(settings, connection=c, project_id=PROJECT_ID)
        assert got.kind == "subscription" and got.value == TOKEN
        assert cc.describe(c, settings, PROJECT_ID)["active"] == "subscription"
        lane = connected_engines(c, settings, PROJECT_ID)["claude_code"]
        assert lane["source_id"] == "anthropic_claude_code"

    from boss_engine.services import project_settings

    with engine.begin() as c:
        project_settings.set_one(c, PROJECT_ID, "anthropic_credential_order", "api_key_first",
                                 actor="ceo")
        assert cc.resolve_claude_credential(
            settings, connection=c, project_id=PROJECT_ID).kind == "stored_key"
        project_settings.clear_one(c, PROJECT_ID, "anthropic_credential_order", actor="ceo")

    response = client.post("/v1/entitlements/sources/anthropic_claude_code/disconnect", json={
        "project_id": PROJECT_ID, "reason": "use API credits", "actor": "ceo"})
    assert response.status_code == 200, response.text
    assert TOKEN not in store.values(), "the stored login is forgotten"
    with engine.begin() as c:
        got = cc.resolve_claude_credential(settings, connection=c, project_id=PROJECT_ID)
        assert (got.kind, got.value) == ("stored_key", KEY)
    assert cc.resolve_claude_credential(settings).kind == "stored_key"
    rows = _sources(client)
    assert rows["anthropic_claude_code"]["claude_credential"]["active"] == "stored_key"
    assert rows["anthropic_claude_code"]["claude_credential"]["subscription_disconnected"] is True
