"""The upgrade promise over HTTP (docs/community/IDENTITY_UPGRADE.md): the host tools' service-account
key in either header, the one /v1/auth/session, the shared password's email-less sign-in, and the
Deprecation/Sunset headers on a legacy credential's responses."""
from __future__ import annotations

from fastapi.testclient import TestClient

from apps.api.app import main
from boss_engine.config import get_settings
from boss_engine.identity import host_tools, legacy, migration


def _org(engine) -> str:
    with engine.begin() as db:
        org = migration.default_org(db)
    assert org, "the test database has no org"
    return org


def test_the_host_tools_key_works_in_either_header_and_says_who_it_is(engine):
    org = _org(engine)
    with engine.begin() as db:
        created = host_tools.issue_host_key(db, org_id=org)
    client = TestClient(main.app)
    for headers in ({"Authorization": f"Bearer {created.key}"}, {"X-Boss-Api-Key": created.key}):
        res = client.get("/v1/auth/session", headers=headers)
        assert res.status_code == 200, res.text
        body = res.json()
        # identity's fields (IDENTITY.md §10) and rc5's, from the one route
        assert body["principal"]["id"] == host_tools.HOST_TOOLS_PRINCIPAL
        assert body["principal"]["kind"] == "service_account"
        assert body["auth_method"] == "api_key" and body["transport_authenticated"] is True
        assert body["key_projects"] is None
        assert "deprecation" not in {k.lower() for k in res.headers}


def test_the_mcp_key_reports_the_one_project_it_is_pinned_to(engine):
    org = _org(engine)
    project = get_settings().default_project_id or "boss-dogfood"
    with engine.begin() as db:
        created = host_tools.issue_mcp_key(db, org_id=org, project_id=project)
    res = TestClient(main.app).get("/v1/auth/session",
                                   headers={"Authorization": f"Bearer {created.key}"})
    assert res.status_code == 200, res.text
    assert res.json()["key_projects"] == [project]


def test_the_shared_password_signs_in_without_an_email(engine):
    """The body needs no email (the console's "Use the shared password"); the answer is the
    sign-in's, never a 422 for the missing field."""
    res = TestClient(main.app).post("/v1/auth/login", json={"password": "not the password"},
                                    headers={"Origin": "http://testserver"})
    assert res.status_code in (401, 409), res.text
    assert res.json()["error"] in ("invalid_credentials", "claim_first")


def test_a_legacy_key_is_told_its_end_on_every_response(engine, monkeypatch):
    key = "legacy-console-key-fxa-0000000000000000"
    for settings in (main.settings, get_settings()):
        monkeypatch.setattr(settings, "require_api_auth", True)
        monkeypatch.setattr(settings, "api_keys", key)
        monkeypatch.setattr(settings, "api_key_principals", f"{key}:ceo")
        monkeypatch.setattr(settings, "legacy_console_api_key", key)
    legacy.invalidate_cache()
    migration.forget_seeded()
    res = TestClient(main.app).get("/v1/auth/session", headers={"Authorization": f"Bearer {key}"})
    assert res.status_code == 200, res.text
    assert res.headers.get("deprecation") == "true"
