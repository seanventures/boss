"""The P2 identity API surface FX-B added (IDENTITY.md §7.1, §7.3, §7.12, §10; A13), over HTTP:

  * GET /v1/identity/agents/{id}/lineage, and the Passport's own `lineage` (no longer `[]`);
  * POST /v1/identity/agents/{id}/reissue (revoked only; the ceiling must be the caller's);
  * POST /v1/identity/me/delegations/preview (ok exactly when create would; writes nothing);
  * GET /v1/identity/providers/{id} (no secrets), and private_key_jwt client assertions;
  * POST /v1/identity/access-requests/{id}/approve|deny (every §6.11 rule; no self-approval);
  * grant reminders 3 days before expiry, once;
  * history labels "Param (was: ceo)" on the activity log.
"""
from __future__ import annotations

import json
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from apps.api.app.main import app
from apps.api.tests.identity_fixtures import IdentityWorld

H = {"sec-fetch-site": "same-origin"}


@pytest.fixture
def world(engine) -> IdentityWorld:
    w = IdentityWorld.create(engine)
    w.claimed()
    return w


def _client():
    return TestClient(app, raise_server_exceptions=False)


# ---- lineage and re-issue ----------------------------------------------------------------------------

def test_an_agents_lineage_is_served_and_shown_on_its_passport(world):
    owner = world.person("lin-owner")
    world.bind(owner, "owner")
    agent = world.agent("lin-agent", ceiling=("code_task:read",), sponsor=owner)
    client = _client()
    with world.acting_as(world.ctx(owner)):
        changed = client.patch(f"/v1/identity/agents/{agent.id}/passport",
                               json={"ceiling": ["code_task:read", "code_task:create"]}, headers=H)
        assert changed.status_code == 200, changed.text
        lineage = client.get(f"/v1/identity/agents/{agent.id}/lineage").json()
        passport = client.get(f"/v1/identity/agents/{agent.id}").json()
        stranger = client.get("/v1/identity/agents/agt_nobody/lineage")
    events = [e["event"] for e in lineage["events"]]
    assert "agent.passport.ceiling_changed" in events, lineage
    first = lineage["events"][0]
    assert first["detail"] == "added code_task:create" and first["actor"]["id"] == owner.id
    assert passport["lineage"] and passport["lineage"][0]["event"] == "Ceiling changed"
    assert stranger.status_code == 404


def test_only_a_revoked_passport_is_reissued_and_only_within_the_callers_access(world):
    owner = world.person("re-owner")
    world.bind(owner, "owner")
    agent = world.agent("re-agent", ceiling=("code_task:read",), sponsor=owner)
    client = _client()
    with world.acting_as(world.ctx(owner)):
        live = client.post(f"/v1/identity/agents/{agent.id}/reissue", json={}, headers=H)
        assert live.status_code == 409 and live.json()["error"] == "not_revoked"
        assert client.post(f"/v1/identity/agents/{agent.id}/revoke", json={"reason": "t"}, headers=H).status_code == 200
        new = client.post(f"/v1/identity/agents/{agent.id}/reissue", json={}, headers=H)
    assert new.status_code == 201, new.text
    assert new.json()["reissued_from"] == agent.id and new.json()["id"] != agent.id


# ---- delegation preview -----------------------------------------------------------------------------

def test_the_delegation_preview_answers_what_create_would_and_writes_nothing(world):
    person = world.person("dp-person")
    world.bind(person, "operator")
    agent = world.agent("dp-agent", ceiling=("code_task:read", "code_task:create"), sponsor=person)
    client = _client()
    body = {"agents": [agent.id], "permissions": ["code_task:read", "code_task:create"],
            "projects": [world.project_a], "kind": "standing", "days": 7}
    too_much = {**body, "permissions": ["code_task:read", "code_task:steer", "workflow:publish"]}
    with world.acting_as(world.ctx(person)):
        ok = client.post("/v1/identity/me/delegations/preview", json=body, headers=H)
        refused = client.post("/v1/identity/me/delegations/preview", json=too_much, headers=H)
        bad = client.post("/v1/identity/me/delegations/preview", json={**body, "user": "x"}, headers=H)
    assert ok.status_code == 200 and ok.json()["ok"] is True, ok.text
    assert ok.json()["permissions"]["grantable"] == ["code_task:create", "code_task:read"]
    assert refused.json()["ok"] is False
    perms = refused.json()["permissions"]
    assert perms["not_delegable"] == ["workflow:publish"] and "code_task:steer" in perms["over_ceiling"]
    assert bad.status_code == 422
    with world.engine.connect() as db:
        assert db.execute(text("SELECT count(*) FROM identity_delegation_grants WHERE user_principal_id = :p"),
                          {"p": person.id}).scalar() == 0


# ---- providers --------------------------------------------------------------------------------------

def _provider(world, *, protocol="oidc", preset="generic", client_auth="client_secret"):
    pid = f"idp_{uuid.uuid4().hex[:10]}"
    with world.engine.begin() as db:
        db.execute(text("INSERT INTO identity_providers (id, org_id, protocol, preset, name, issuer, client_id, "
                        "scopes, allowed_algs, domains, jit_mode, created_by, client_auth) VALUES (:id, :o, :pr, "
                        ":ps, 'Okta', 'https://idp.example.test', 'boss-client', ARRAY['openid'], "
                        "ARRAY['RS256'], ARRAY[]::text[], 'off', 'test', :ca)"),
                   {"id": pid, "o": world.org, "pr": protocol, "ps": preset, "ca": client_auth})
    return pid


def test_one_provider_is_readable_without_any_secret(world):
    owner, auditor = world.person("pv-owner"), world.person("pv-auditor")
    world.bind(owner, "owner")
    world.bind(auditor, "auditor")
    pid = _provider(world)
    client = _client()
    with world.acting_as(world.ctx(auditor)):
        one = client.get(f"/v1/identity/providers/{pid}")
        missing = client.get("/v1/identity/providers/idp_nobody")
    assert one.status_code == 200, one.text
    body = one.json()
    assert body["id"] == pid and body["group_mappings"] == [] and body["scim_keys"] == []
    assert "client_secret" not in body and "client_secret_vault_item_id" not in body
    assert missing.status_code == 404


def test_private_key_jwt_signs_a_client_assertion_and_never_sends_a_secret():
    import jwt

    from boss_engine.identity import sso_oidc

    pem = sso_oidc.generate_client_key()
    kid = sso_oidc.client_key_thumbprint(pem)
    jwk = sso_oidc.public_jwk(pem, kid)
    assert "d" not in jwk and jwk["kid"] == kid and jwk["alg"] == "ES256"
    provider = sso_oidc.Provider("idp_x", "org_x", "oidc", "okta", "Okta", "https://idp.example.test",
                                 "boss-client", None, ("openid",), ("RS256",), None, (), (), "off", False,
                                 False, None, "active", client_auth="private_key_jwt",
                                 client_key_vault_item_id="vlt_x", client_key_kid=kid)
    assertion = sso_oidc.client_assertion(provider, pem, token_endpoint="https://idp.example.test/token")
    public = jwt.algorithms.ECAlgorithm.from_jwk(json.dumps(jwk))
    claims = jwt.decode(assertion, public, algorithms=["ES256"], audience="https://idp.example.test/token")
    assert claims["iss"] == claims["sub"] == "boss-client" and claims["exp"] - claims["iat"] == 300
    assert jwt.get_unverified_header(assertion)["kid"] == kid
    apple = sso_oidc.Provider(**{**provider.__dict__, "preset": "apple", "client_assertion_issuer": "TEAM123",
                                 "issuer": "https://appleid.apple.com"})
    apple_jwt = sso_oidc.client_assertion(apple, pem, token_endpoint="https://appleid.apple.com/auth/token")
    apple_claims = jwt.decode(apple_jwt, public, algorithms=["ES256"], audience="https://appleid.apple.com")
    assert apple_claims["iss"] == "TEAM123" and apple_claims["sub"] == "boss-client"


def test_the_token_request_carries_the_assertion_not_a_secret(monkeypatch):
    from boss_engine.identity import sso_oidc

    pem = sso_oidc.generate_client_key()
    provider = sso_oidc.Provider("idp_x", "org_x", "oidc", "okta", "Okta", "https://idp.example.test",
                                 "boss-client", "vlt_secret", ("openid",), ("RS256",), None, (), (), "off",
                                 False, False, None, "active", client_auth="private_key_jwt",
                                 client_key_vault_item_id="vlt_key", client_key_kid="k1")
    monkeypatch.setattr(sso_oidc, "_vaulted", lambda db, p, item: pem if item == "vlt_key" else "s3cret")
    fields = sso_oidc._client_authentication(None, provider, "https://idp.example.test/token")
    assert set(fields) == {"client_assertion_type", "client_assertion"} and "s3cret" not in json.dumps(fields)


def test_a_provider_can_be_switched_to_private_key_jwt_and_publishes_only_its_public_key(world, monkeypatch):
    from boss_engine.identity import vault

    stored = {}

    def store(*, org_id, owner_principal_id, kind, provider, secret, db, **_kw):
        item = f"vlt_{uuid.uuid4().hex[:8]}"
        stored[item] = secret
        return item

    monkeypatch.setattr(vault, "store", store)
    from boss_engine.identity import sso_oidc

    monkeypatch.setattr(sso_oidc, "_vaulted", lambda db, p, item: stored.get(item))
    owner = world.person("pk-owner")
    world.bind(owner, "owner")
    pid = _provider(world)
    client = _client()
    with world.acting_as(world.ctx(owner)):
        patched = client.patch(f"/v1/identity/providers/{pid}", json={"client_auth": "private_key_jwt"}, headers=H)
        jwks = client.get(f"/v1/identity/providers/{pid}/jwks")
        bad = client.patch(f"/v1/identity/providers/{pid}", json={"client_private_key": "not a key"}, headers=H)
    assert patched.status_code == 200, patched.text
    assert patched.json()["client_auth"] == "private_key_jwt" and patched.json()["has_client_key"] is True
    keys = jwks.json()["keys"]
    assert len(keys) == 1 and "d" not in keys[0] and "PRIVATE" not in jwks.text
    assert bad.status_code == 422


# ---- access requests ---------------------------------------------------------------------------------

def _request_access(world, requester, permission, project=None):
    client = _client()
    with world.acting_as(world.ctx(requester)):
        out = client.post("/v1/identity/access-requests",
                          json={"permission": permission, "project_id": project, "reason": "need it"}, headers=H)
    assert out.status_code == 202, out.text
    return out.json()["id"]


def test_an_access_request_is_granted_with_a_role_under_the_grant_rules(world):
    admin, asker = world.person("ar-admin"), world.person("ar-asker")
    world.bind(admin, "admin")
    rid = _request_access(world, asker, "code_task:create", world.project_a)
    client = _client()
    with world.acting_as(world.ctx(admin)):
        listing = client.get("/v1/identity/access-requests").json()["requests"]
        wrong_role = client.post(f"/v1/identity/access-requests/{rid}/approve",
                                 json={"role_id": "arl_viewer"}, headers=H)
        granted = client.post(f"/v1/identity/access-requests/{rid}/approve",
                              json={"role_id": "arl_operator"}, headers=H)
        again = client.post(f"/v1/identity/access-requests/{rid}/approve",
                            json={"role_id": "arl_operator"}, headers=H)
    mine = [r for r in listing if r["id"] == rid][0]
    assert any(c["id"] == "arl_operator" for c in mine["candidate_roles"])
    assert not any(c["id"] in ("arl_owner", "arl_auditor") for c in mine["candidate_roles"])   # org-only
    assert wrong_role.status_code == 422 and wrong_role.json()["error"] == "role_lacks_permission"
    assert granted.status_code == 200, granted.text
    assert granted.json()["request"]["status"] == "granted"
    assert granted.json()["binding"]["scope"] == f"project:{world.project_a}"
    assert again.status_code == 409 and again.json()["error"] == "already_resolved"


def test_an_access_request_is_never_self_approved_or_granted_beyond_the_approver(world):
    admin = world.person("ar-admin2")
    world.bind(admin, "admin")
    own = _request_access(world, admin, "identity_provider:manage")
    other = world.person("ar-other")
    owner_only = _request_access(world, other, "identity_provider:manage")
    client = _client()
    with world.acting_as(world.ctx(admin)):
        self_approved = client.post(f"/v1/identity/access-requests/{own}/approve",
                                    json={"role_id": "arl_owner"}, headers=H)
        beyond = client.post(f"/v1/identity/access-requests/{owner_only}/approve",
                             json={"role_id": "arl_owner"}, headers=H)
        denied = client.post(f"/v1/identity/access-requests/{owner_only}/deny",
                             json={"reason": "not now"}, headers=H)
    assert self_approved.status_code == 403, self_approved.text
    assert beyond.status_code == 403, beyond.text
    assert denied.status_code == 200 and denied.json()["request"]["status"] == "declined"


# ---- grant reminders ----------------------------------------------------------------------------------

def test_a_standing_grant_is_reminded_once_three_days_before_it_ends(world, monkeypatch):
    from boss_engine.identity import authn_support, email_signin, grants

    sent = []
    monkeypatch.setattr(authn_support, "smtp_configured", lambda: True)
    monkeypatch.setattr(email_signin, "DELIVER", lambda to, s, b: sent.append((to, s)))
    person = world.person("gr-person")
    with world.engine.begin() as db:
        db.execute(text("INSERT INTO identity_users (principal_id, email) VALUES (:p, 'gr@acme.test')"),
                   {"p": person.id})
    agent = world.agent("gr-agent", ceiling=("code_task:read",), sponsor=person)
    now = datetime.now(timezone.utc)
    ids = {}
    for label, when in (("soon", now + timedelta(days=2)), ("later", now + timedelta(days=20))):
        gid = f"dlg_{uuid.uuid4().hex[:10]}"
        ids[label] = gid
        with world.engine.begin() as db:
            db.execute(text("INSERT INTO identity_delegation_grants (id, org_id, user_principal_id, agent_ids, "
                            "project_ids, status, expires_at) VALUES (:g, :o, :u, :a, :p, 'active', :e)"),
                       {"g": gid, "o": world.org, "u": person.id, "a": [agent.id], "p": [world.project_a],
                        "e": when})
    with world.engine.begin() as db:
        first = grants.remind_expiring(db=db)
    with world.engine.begin() as db:
        second = grants.remind_expiring(db=db)
    assert ids["soon"] in first and ids["later"] not in first
    assert ids["soon"] not in second
    assert [to for to, _s in sent].count("gr@acme.test") == 1


# ---- history labels ---------------------------------------------------------------------------------

def test_history_names_who_a_legacy_actor_is_now(world):
    from boss_engine.identity import legacy, migration

    person = world.person("Param")
    with world.engine.begin() as db:
        migration.put_mapping(db, world.org, migration.LegacyKind.MEMBER_ID, "ceo", person.id)
        labels = legacy.actor_labels(world.org, ["ceo", person.id, "nobody"], db=db)
    assert labels == {"ceo": "Param (was: ceo)", person.id: "Param"}


def test_a_project_admin_decides_their_projects_requests_and_nothing_else(world):
    """Access requests are an object kind: a project request is decided at its project (a project
    Admin with role_binding:assign there may approve or deny it), an org-wide one at the org."""
    padmin = world.person("ar-project-admin")
    world.bind(padmin, "admin", project=world.project_a)
    mine = _request_access(world, world.person("ar-a"), "code_task:create", world.project_a)
    theirs = _request_access(world, world.person("ar-b"), "code_task:create", world.project_b)
    org_wide = _request_access(world, world.person("ar-o"), "user:read")
    client = _client()
    with world.acting_as(world.ctx(padmin)):
        queue = client.get(f"/v1/identity/projects/{world.project_a}/access-requests").json()["requests"]
        other_queue = client.get(f"/v1/identity/projects/{world.project_b}/access-requests")
        approved = client.post(f"/v1/identity/access-requests/{mine}/approve",
                               json={"role_id": "arl_operator"}, headers=H)
        other = client.post(f"/v1/identity/access-requests/{theirs}/deny", json={}, headers=H)
        org = client.post(f"/v1/identity/access-requests/{org_wide}/deny", json={}, headers=H)
        unknown = client.post("/v1/identity/access-requests/acr_nobody/deny", json={}, headers=H)
    assert [r["id"] for r in queue] == [mine]
    assert other_queue.status_code in (403, 404)
    assert approved.status_code == 200, approved.text
    assert approved.json()["binding"]["scope"] == f"project:{world.project_a}"
    assert other.status_code in (403, 404) and org.status_code in (403, 404), (other.text, org.text)
    assert unknown.status_code == 404
    oadmin = world.person("ar-org-admin")
    world.bind(oadmin, "admin")
    with world.acting_as(world.ctx(oadmin)):
        assert client.post(f"/v1/identity/access-requests/{org_wide}/deny", json={}, headers=H).status_code == 200
