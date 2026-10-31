"""KG P6b: HTTP API over P6a's external-agent registration (console-facing CRUD+test).

Covers the acceptance criteria from the PRD/tech plan that are reachable through the HTTP API
and the shipped doc, without any console/browser surface (that follows in a later pass):
AC1 (register, never returns the secret), AC2 (credential-in-url rejected), AC3 (test is
binary reachable/unreachable only), AC4 (suspend/remove change status), AC5 (secret never
leaks across any response surface), AC10 (the doc exists with its required sections).

Module import names (``apps.api.app.routers.external_agents`` is not wired yet) are the
intended ones from the technical plan; this file is written before that router exists.
"""
from __future__ import annotations

import secrets

from pathlib import Path

from boss_engine.config import get_settings as get_engine_settings
from boss_engine.db import begin, get_engine
from boss_engine.services.secrets_service import put_secret

PROJECT = "boss-dogfood"
ACTOR = "ceo"
ROOT = Path(__file__).resolve().parents[3]


def _make_secret_ref(secret_value: str) -> str:
    settings = get_engine_settings()
    with begin(get_engine()) as connection:
        stored = put_secret(
            connection,
            settings,
            project_id=PROJECT,
            name=f"outside-agent-{secrets.token_hex(4)}",
            value=secret_value,
            secret_kind="api_token",
            backend="database",
            actor=ACTOR,
        )
    return stored["secret_ref_id"]


def _register(client, *, endpoint_url: str, secret_ref_id: str, name: str = "sample-ca"):
    return client.post(
        "/v1/external-agents",
        json={
            "project_id": PROJECT,
            "display_name": name,
            "endpoint_url": endpoint_url,
            "secret_ref_id": secret_ref_id,
            "sponsor_principal_id": ACTOR,
            "actor": ACTOR,
        },
    )


def test_p0_register_creates_agent_and_list_never_echoes_the_secret(client):
    secret_value = secrets.token_urlsafe(24)
    secret_ref_id = _make_secret_ref(secret_value)

    created = _register(client, endpoint_url="http://localhost:8765/agent", secret_ref_id=secret_ref_id)
    assert created.status_code == 201, created.text
    body = created.json()
    assert body["display_name"] == "sample-ca"
    assert secret_value not in created.text
    assert secret_ref_id not in created.text or "value" not in body

    listed = client.get("/v1/external-agents", params={"project_id": PROJECT})
    assert listed.status_code == 200, listed.text
    ids = {item["id"] for item in listed.json()["items"]}
    assert body["id"] in ids
    assert secret_value not in listed.text


def test_p0_register_rejects_an_endpoint_with_embedded_credentials():
    client_secret_ref_id = _make_secret_ref(secrets.token_urlsafe(16))

    def _try(client):
        return _register(client, endpoint_url="https://user:pass@example.com/hook",
                          secret_ref_id=client_secret_ref_id)

    from fastapi.testclient import TestClient
    from apps.api.app.main import app

    rejected = _try(TestClient(app))
    assert rejected.status_code in (400, 422), rejected.text


def test_p1_test_endpoint_reports_reachable_or_unreachable_only(client):
    secret_ref_id = _make_secret_ref(secrets.token_urlsafe(16))
    created = _register(client, endpoint_url="http://localhost:1/unreachable-agent",
                         secret_ref_id=secret_ref_id)
    assert created.status_code == 201, created.text
    agent_id = created.json()["id"]

    tested = client.post(
        f"/v1/external-agents/{agent_id}/test",
        json={"project_id": PROJECT, "actor": ACTOR},
    )
    assert tested.status_code == 200, tested.text
    result = tested.json()
    assert set(result.keys()) == {"reachable"}
    assert result["reachable"] is False


def test_p1_suspend_then_remove_change_the_agents_status(client):
    secret_ref_id = _make_secret_ref(secrets.token_urlsafe(16))
    created = _register(client, endpoint_url="http://localhost:8765/agent-lifecycle",
                         secret_ref_id=secret_ref_id)
    assert created.status_code == 201, created.text
    agent_id = created.json()["id"]

    suspended = client.post(
        f"/v1/external-agents/{agent_id}/suspend",
        json={"project_id": PROJECT, "actor": ACTOR},
    )
    assert suspended.status_code == 200, suspended.text
    assert suspended.json()["status"] == "suspended"

    fetched = client.get(f"/v1/external-agents/{agent_id}", params={"project_id": PROJECT})
    assert fetched.status_code == 200, fetched.text
    assert fetched.json()["status"] == "suspended"

    removed = client.post(
        f"/v1/external-agents/{agent_id}/remove",
        json={"project_id": PROJECT, "actor": ACTOR},
    )
    assert removed.status_code == 200, removed.text

    still_listed = client.get("/v1/external-agents", params={"project_id": PROJECT})
    assert still_listed.status_code == 200, still_listed.text
    remaining = next((i for i in still_listed.json()["items"] if i["id"] == agent_id), None)
    assert remaining is None or remaining["status"] != "active"


def test_p1_secret_value_never_appears_on_any_response_surface(client):
    secret_value = secrets.token_urlsafe(32)
    secret_ref_id = _make_secret_ref(secret_value)
    created = _register(client, endpoint_url="http://localhost:1/leak-check",
                         secret_ref_id=secret_ref_id)
    assert created.status_code == 201, created.text
    agent_id = created.json()["id"]

    surfaces = [
        created,
        client.get("/v1/external-agents", params={"project_id": PROJECT}),
        client.get(f"/v1/external-agents/{agent_id}", params={"project_id": PROJECT}),
        client.post(f"/v1/external-agents/{agent_id}/test", json={"project_id": PROJECT, "actor": ACTOR}),
    ]
    for response in surfaces:
        assert secret_value not in response.text, response.text


def test_p1_outside_agents_doc_covers_the_required_sections():
    doc = ROOT / "docs" / "community" / "OUTSIDE_AGENTS.md"
    assert doc.exists(), f"missing {doc}"
    text_lower = doc.read_text(encoding="utf-8").lower()
    for term in ("token", "scope", "suspend", "revoke", "sample_external_agent", "a2a"):
        assert term in text_lower, f"{doc} is missing coverage of {term!r}"
