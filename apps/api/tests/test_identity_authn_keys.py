"""Stream S1: personal access tokens, service-account keys, rotation and revocation.

Adversarial ids (IDENTITY.md §15.3): A21–A30. Each fails if its protection is removed.
"""
from __future__ import annotations

import inspect
import statistics
import time

from fastapi.testclient import TestClient
from sqlalchemy import text

from apps.api.tests.identity_s1_world import (  # noqa: F401  (world is a fixture)
    HEADERS,
    ORIGIN,
    csrf,
    fresh_client,
    make_user,
    promote_fresh_mfa,
    sign_in,
    world,
)
from boss_engine.identity import api_keys, authn


def _client(ip: str = "203.0.113.7") -> TestClient:
    from apps.api.app.main import app

    return TestClient(app, base_url=ORIGIN, client=(ip, 50000))


def _owner_session(engine, email: str = "own@example.test", role: str = "owner"):
    make_user(engine, email, role=role)
    client = fresh_client()
    assert sign_in(client, email).status_code == 200
    promote_fresh_mfa(engine, client.cookies.get(authn.SESSION_COOKIE))
    return client


def _new_pat(client, scopes, **extra) -> dict:
    res = client.post("/v1/identity/me/api-keys", json={"name": "ci", "scopes": scopes, **extra},
                      headers=csrf(client))
    assert res.status_code == 201, res.text
    return _flat(res.json())


def _flat(issued: dict) -> dict:
    """IssuedSecret {key: ApiKey, secret, replaces} -> {id, key} for these tests."""
    assert set(issued) >= {"key", "secret", "replaces"} and issued["key"]["status"] == "active"
    return {"id": issued["key"]["id"], "key": issued["secret"]}


def _bearer(key: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {key}"}


def test_the_key_format_is_prefixed_checksummed_and_shown_once(world, engine):
    client = _owner_session(engine)
    created = _new_pat(client, ["user:read"])
    key = created["key"]
    assert key.startswith("boss_pat_") and len(key) == len("boss_pat_") + 43 + 6
    assert authn.parse_api_key(key).checksum_ok
    with engine.begin() as db:
        stored = db.execute(text("SELECT token_hash FROM identity_api_credentials WHERE id = :id"),
                            {"id": created["id"]}).scalar()
        dump = str(db.execute(text("SELECT * FROM identity_api_credentials")).fetchall())
    assert stored == authn.hash_token(key) and key[9:30] not in dump
    me = _client().get("/v1/identity/me", headers=_bearer(key))
    assert me.status_code == 200 and me.json()["session_id"] is None


def test_a24_a_key_is_never_in_any_response_after_creation(world, engine):
    client = _owner_session(engine)
    key = _new_pat(client, ["user:read", "api_key:read"])["key"]
    body = key[len("boss_pat_"):]
    for path in ("/v1/identity/me/api-keys", "/v1/identity/api-keys", "/v1/identity/me",
                 "/v1/identity/users"):
        res = client.get(path)
        assert res.status_code == 200, (path, res.text)
        for fragment in (key, body[:8], body[-8:], body[10:18]):
            assert fragment not in res.text, path
    assert all(fragment not in str(e) for e in world.events for fragment in (key, body[:12]))


def test_a21_an_expired_key_is_401(world, engine):
    client = _owner_session(engine)
    created = _new_pat(client, ["user:read"])
    with engine.begin() as db:
        db.execute(text("UPDATE identity_api_credentials SET expires_at = now() - interval '1 second' "
                        "WHERE id = :id"), {"id": created["id"]})
    res = _client().get("/v1/identity/me", headers=_bearer(created["key"]))
    assert res.status_code == 401 and res.json() == {"error": "unauthenticated"}


def test_a22_a_key_outside_its_cidr_is_401(world, engine):
    client = _owner_session(engine)
    created = _new_pat(client, ["user:read"], cidrs=["198.51.100.0/24"])
    assert _client("198.51.100.20").get("/v1/identity/me", headers=_bearer(created["key"])).status_code == 200
    assert _client("203.0.113.9").get("/v1/identity/me", headers=_bearer(created["key"])).status_code == 401
    # a client-sent X-Forwarded-For does not move it inside the range
    spoofed = _client("203.0.113.9").get("/v1/identity/me",
                                         headers={**_bearer(created["key"]), "X-Forwarded-For": "198.51.100.20"})
    assert spoofed.status_code == 401


def test_a23_use_from_a_new_ip_emits_an_event(world, engine):
    client = _owner_session(engine)
    key = _new_pat(client, ["user:read"])["key"]
    assert _client("198.51.100.1").get("/v1/identity/me", headers=_bearer(key)).status_code == 200
    assert world.events_of("api_key.first_used")
    assert not world.events_of("api_key.used_from_new_ip")
    assert _client("198.51.100.2").get("/v1/identity/me", headers=_bearer(key)).status_code == 200
    moved = world.events_of("api_key.used_from_new_ip")
    assert moved and moved[-1].details["previous_ip"] == "198.51.100.1"


def test_a25_an_admin_cannot_mint_a_key_for_a_service_account_stronger_than_them(world, engine):
    owner = _owner_session(engine, "o1@example.test")
    admin = _owner_session(engine, "a1@example.test", role="admin")
    owner_id = owner.get("/v1/identity/me").json()["principal_id"]
    sa = owner.post("/v1/identity/service-accounts", json={"name": "deployer", "owner_principal_ids": [owner_id]},
                    headers=csrf(owner))
    assert sa.status_code == 201, sa.text
    sa_id = sa.json()["id"]
    with engine.begin() as db:
        from boss_engine.identity import users

        users.add_binding(db, org_id=db.execute(text("SELECT org_id FROM identity_principals WHERE id=:i"),
                                                {"i": sa_id}).scalar(),
                          subject_id=sa_id, role_key="owner", granted_by=owner_id)
    res = admin.post(f"/v1/identity/service-accounts/{sa_id}/keys",
                     json={"name": "k", "scopes": ["deployment:read"]}, headers=csrf(admin))
    assert res.status_code == 403 and res.json()["error"] == "owner_exceeds_creator"
    with engine.begin() as db:
        assert db.execute(text("SELECT count(*) FROM identity_api_credentials WHERE principal_id = :p"),
                          {"p": sa_id}).scalar() == 0
    # the Owner, who holds everything the account holds, can
    ok = owner.post(f"/v1/identity/service-accounts/{sa_id}/keys",
                    json={"name": "k", "scopes": ["deployment:read"]}, headers=csrf(owner))
    assert ok.status_code == 201 and ok.json()["secret"].startswith("boss_sak_")


def test_scopes_beyond_the_owner_are_refused(world, engine):
    viewer = _owner_session(engine, "v1@example.test", role="viewer")
    res = viewer.post("/v1/identity/me/api-keys", json={"name": "x", "scopes": ["user:disable"]},
                      headers=csrf(viewer))
    assert res.status_code == 403 and res.json()["error"] == "scopes_exceed_owner"
    unknown = viewer.post("/v1/identity/me/api-keys", json={"name": "x", "scopes": ["workflow:launch"]},
                          headers=csrf(viewer))
    assert unknown.status_code == 422 and unknown.json()["error"] == "unknown_permission"


def test_a26_a_pat_cannot_use_a_step_up_permission(world, engine):
    client = _owner_session(engine)
    key = _new_pat(client, ["api_key:manage_own", "user:read"])["key"]
    res = _client().post("/v1/identity/me/api-keys", json={"name": "y", "scopes": ["user:read"]},
                         headers=_bearer(key))
    assert res.status_code == 401 and res.json() == {"error": "step_up_required"}
    # and a bearer can never step up
    step = _client().post("/v1/auth/step-up", json={"method": "password", "password": "x" * 12},
                          headers=_bearer(key))
    assert step.status_code == 401 and step.json()["error"] == "step_up_required"
    with engine.begin() as db:
        ctx = authn.authenticate(authn.RequestCredentials("GET", "/v1/identity/me",
                                                          {"authorization": f"Bearer {key}"}, {}), db=db)
    assert ctx.session is None and ctx.credential.scopes == frozenset({"api_key:manage_own", "user:read"})


def test_a27_lookup_is_by_hash_and_timing_does_not_leak_a_prefix(world, engine):
    source = inspect.getsource(api_keys.authenticate_key)
    assert "token_hash = :th" in source and "LIKE" not in source.upper()
    client = _owner_session(engine)
    real = _new_pat(client, ["user:read"])["key"]
    near = authn.mint_api_key("pat")
    near = real[:-15] + near[-15:]                     # shares 40 characters with the real key
    body = near[len("boss_pat_"):-6]
    near = "boss_pat_" + body + authn._checksum(body)   # still checksum-valid
    far = authn.mint_api_key("pat")
    timings = {"near": [], "far": []}
    with engine.begin() as db:
        for _ in range(15):
            for label, key in (("near", near), ("far", far)):
                creds = authn.RequestCredentials("GET", "/x", {"authorization": f"Bearer {key}"}, {})
                started = time.perf_counter()
                try:
                    authn.authenticate(creds, db=db)
                except authn.AuthnError:
                    pass
                timings[label].append(time.perf_counter() - started)
    near_m, far_m = statistics.median(timings["near"]), statistics.median(timings["far"])
    assert abs(near_m - far_m) / max(near_m, far_m) < 0.5, (near_m, far_m)


def test_a28_a_checksum_invalid_key_is_refused_before_any_database_access(world):
    key = authn.mint_api_key("sak")
    broken = key[:-1] + ("0" if key[-1] != "0" else "1")

    class NoDatabase:
        def __getattr__(self, name):
            raise AssertionError("the database was touched")

    creds = authn.RequestCredentials("GET", "/v1/identity/me", {"authorization": f"Bearer {broken}"}, {})
    try:
        authn.authenticate(creds, db=NoDatabase())
        raise AssertionError("a broken checksum must not authenticate")
    except authn.AuthnError as exc:
        assert exc.code == "invalid_credentials"
    assert authn.parse_api_key(broken).checksum_ok is False
    assert authn.parse_api_key("boss_pat_short").checksum_ok is False
    assert authn.parse_api_key("ghp_notboss") is None


def test_a29_a_revoked_key_is_refused_on_the_very_next_request(world, engine):
    client = _owner_session(engine)
    created = _new_pat(client, ["user:read"])
    api = _client()
    assert api.get("/v1/identity/me", headers=_bearer(created["key"])).status_code == 200
    res = client.post(f"/v1/identity/me/api-keys/{created['id']}/revoke", headers=csrf(client))
    assert res.status_code == 200
    assert api.get("/v1/identity/me", headers=_bearer(created["key"])).status_code == 401
    assert world.events_of("api_key.revoked")


def test_a30_a_keys_reach_shrinks_the_moment_its_owner_is_demoted(world, engine):
    client = _owner_session(engine, "adm@example.test", role="admin")
    key = _new_pat(client, ["user:read"])["key"]
    api = _client()
    assert api.get("/v1/identity/users", headers=_bearer(key)).status_code == 200
    with engine.begin() as db:
        db.execute(text("UPDATE identity_role_bindings SET role_id = 'arl_viewer' WHERE subject_id = "
                        "(SELECT principal_id FROM identity_users WHERE email = 'adm@example.test')"))
    res = api.get("/v1/identity/users", headers=_bearer(key))
    assert res.status_code == 403 and res.json() == {"error": "forbidden", "permission": "user:read"}


def test_rotation_keeps_the_old_key_for_its_grace_then_refuses_it(world, engine):
    client = _owner_session(engine)
    old = _new_pat(client, ["user:read"])
    rotated = client.post(f"/v1/identity/me/api-keys/{old['id']}/rotate", json={"grace": "1h"},
                          headers=csrf(client))
    assert rotated.status_code == 201
    new = _flat(rotated.json())
    assert rotated.json()["replaces"]["id"] == old["id"]
    api = _client()
    assert api.get("/v1/identity/me", headers=_bearer(old["key"])).status_code == 200
    assert api.get("/v1/identity/me", headers=_bearer(new["key"])).status_code == 200
    listed = {k["id"]: k["status"] for k in client.get("/v1/identity/me/api-keys").json()["keys"]}
    assert listed[old["id"]] == "grace" and listed[new["id"]] == "active"
    with engine.begin() as db:
        db.execute(text("UPDATE identity_api_credentials SET grace_until = now() - interval '1 second' "
                        "WHERE id = :id"), {"id": old["id"]})
    assert api.get("/v1/identity/me", headers=_bearer(old["key"])).status_code == 401
    immediate = client.post(f"/v1/identity/me/api-keys/{new['id']}/rotate", json={"grace": "0"},
                            headers=csrf(client))
    assert immediate.status_code == 201
    assert api.get("/v1/identity/me", headers=_bearer(new["key"])).status_code == 401


def test_expiry_defaults_to_90_days_and_never_exceeds_the_policy_max(world, engine):
    client = _owner_session(engine)
    created = _new_pat(client, ["user:read"])
    with engine.begin() as db:
        days = db.execute(text("SELECT extract(day FROM expires_at - created_at) FROM identity_api_credentials "
                               "WHERE id = :id"), {"id": created["id"]}).scalar()
    assert int(days) in (89, 90)
    too_long = client.post("/v1/identity/me/api-keys", json={"name": "x", "scopes": ["user:read"],
                                                             "expires_in_days": 366}, headers=csrf(client))
    assert too_long.status_code == 422 and too_long.json()["error"] == "expiry_too_long"
    forever = client.post("/v1/identity/me/api-keys", json={"name": "x", "scopes": ["user:read"],
                                                            "expires_in_days": 0}, headers=csrf(client))
    assert forever.status_code == 422 and forever.json()["error"] == "expiry_required"


def test_a_disabled_owner_s_keys_stop_working(world, engine):
    owner = _owner_session(engine, "boss@example.test")
    admin_client = _owner_session(engine, "adm2@example.test", role="admin")
    key = _new_pat(admin_client, ["user:read"])["key"]
    admin_id = admin_client.get("/v1/identity/me").json()["principal_id"]
    res = owner.post(f"/v1/identity/users/{admin_id}/disable", headers=csrf(owner))
    assert res.status_code == 200, res.text
    assert _client().get("/v1/identity/me", headers=_bearer(key)).status_code == 401
    assert admin_client.get("/v1/identity/me").status_code == 401
    assert ("person_disabled", admin_id) in world.cascades
