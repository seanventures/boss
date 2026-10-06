"""/v1/identity/{audit,posture,policy,shadow-report,legacy} (stream S5's router).

The handlers run against the rolled-back test transaction; the request's AuthContext comes from a
fake of S1's authenticate (the router reads it through the guard's own helpers).
"""
from __future__ import annotations

from contextlib import contextmanager
from datetime import timedelta

from fastapi import FastAPI
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient

from boss_engine.identity import access_of
from boss_engine.identity.permissions_gen import PERMISSION_META
from engine.tests.identity_s5_support import (  # noqa: F401, F811  (db, s5_engine: fixtures)
    NOW,
    ORG,
    community_install,
    db,
    events,
    fake_claim,
    s5_engine,
    settings,
)


def app_for(db, monkeypatch, ctx) -> FastAPI:
    from apps.api.app.routers import identity_admin

    @contextmanager
    def same_transaction():
        with db.begin_nested():
            yield db

    monkeypatch.setattr(identity_admin, "begin", same_transaction)
    monkeypatch.setattr(identity_admin, "_context", lambda request: ctx)
    app = FastAPI()
    app.include_router(identity_admin.build_identity_admin_router(start_background=False))
    return app


def owner(db):
    community_install(db)
    return fake_claim(db, name="Param", email="param@example.com", via="setup_token", now=NOW,
                      cfg=settings(legacy_console_auth="network"))


def test_every_route_is_declared_with_a_catalogue_permission_at_org_scope():
    from apps.api.app.routers.identity_admin import build_identity_admin_router
    routes = [r for r in build_identity_admin_router(start_background=False).routes
              if isinstance(r, APIRoute)]
    assert len(routes) == 13
    for route in routes:
        access = access_of(route.endpoint)
        assert access is not None and access.permission in PERMISSION_META, route.path
        assert route.endpoint.__module__.rsplit(".", 1)[-1].startswith("identity")   # enforced


def test_audit_list_filter_get_export_and_verify(db, monkeypatch):
    claim = owner(db)
    client = TestClient(app_for(db, monkeypatch, claim.ctx))
    page = client.get("/v1/identity/audit", params={"event": "bootstrap.*"}).json()
    assert [e["event_type"] for e in page["events"]] == ["bootstrap.claimed"]
    first = page["events"][0]
    assert len(first["hash"]) == 64 and first["actor_principal_id"] == claim.principal_id
    assert client.get(f"/v1/identity/audit/{first['id']}").json()["seq"] == first["seq"]
    assert client.get("/v1/identity/audit/999999").status_code == 404
    bundle = client.post("/v1/identity/audit/export").json()
    assert bundle["verification"]["ok"] and bundle["events"]
    verified = client.post("/v1/identity/audit/verify").json()
    assert verified["ok"] and verified["first_break_seq"] is None
    assert events(db, "audit.verified")[-1]["outcome"] == "success"
    small = client.get("/v1/identity/audit", params={"limit": 1}).json()
    assert len(small["events"]) == 1 and small["next_cursor"]
    older = client.get("/v1/identity/audit", params={"limit": 1,
                                                     "cursor": small["next_cursor"]}).json()
    assert older["events"][0]["seq"] < small["events"][0]["seq"]
    assert client.post("/v1/identity/audit/prune", json={"older_than_days": 1}).status_code == 422


def test_policy_read_validate_write_and_posture(db, monkeypatch):
    claim = owner(db)
    client = TestClient(app_for(db, monkeypatch, claim.ctx))
    current = client.get("/v1/identity/policy").json()
    assert current["legacy_cutoff_days"] == 30 and "extra" not in current
    for bad in ({"nope": 1}, {"password_min_length": 3}, {"mfa_required_roles": ["god"]},
                {"public_origin": "http://plain"}, {"extra": {"x": 1}}):
        response = client.put("/v1/identity/policy", json=bad)
        assert response.status_code == 422 and response.json()["error"] == "invalid_policy", bad
    ok = client.put("/v1/identity/policy", json={"legacy_cutoff_days": 45})
    assert ok.status_code == 200 and ok.json()["legacy_cutoff_days"] == 45
    diff = events(db, "policy.changed")[-1]["details"]["diff"]
    assert diff == {"legacy_cutoff_days": {"from": 30, "to": 45}}
    paths = {p["kind"]: p for p in client.get("/v1/identity/legacy").json()["paths"]}
    assert paths["network_mode"]["days_left"] in (44, 45)     # moved with the policy
    report = client.get("/v1/identity/posture").json()
    assert {c["key"] for c in report["checks"]} == {"mfa_coverage", "stale_api_keys",
                                                    "risky_delegations", "legacy_credentials",
                                                    "orphans", "audit_chain"}


def test_legacy_retire_and_extend(db, monkeypatch):
    community_install(db)
    from boss_engine.identity import migration
    migration.seed(db, settings=settings(legacy_console_auth="network"), now=NOW)
    from boss_engine.identity.principal import AuthContext, AuthMethod, Principal, \
        PrincipalKind, PrincipalStatus
    early = AuthContext(Principal("usr_x", PrincipalKind.USER, ORG, PrincipalStatus.ACTIVE, "X"),
                        AuthMethod.SESSION)
    client = TestClient(app_for(db, monkeypatch, early))
    assert client.post("/v1/identity/legacy/network_mode/retire").status_code == 409
    claim = fake_claim(db, name="Param", email="param@example.com", via="network_bridge",
                       now=NOW, cfg=settings(legacy_console_auth="network"))
    client = TestClient(app_for(db, monkeypatch, claim.ctx))
    assert client.post("/v1/identity/legacy/nope/retire").status_code == 404
    moved = client.post("/v1/identity/legacy/network_mode/extend",
                        json={"until": (NOW + timedelta(days=50)).isoformat()})
    assert moved.status_code == 200
    assert client.post("/v1/identity/legacy/network_mode/extend",
                       json={"until": "soon"}).status_code == 422
    done = client.post("/v1/identity/legacy/network_mode/retire")
    assert done.status_code == 200 and done.json()["retired"]
    assert client.post("/v1/identity/legacy/network_mode/extend",
                       json={"until": (NOW + timedelta(days=5)).isoformat()}).status_code == 409


def test_shadow_report_and_explanations(db, monkeypatch):
    from boss_engine.identity.authz import DenyReason
    from boss_engine.identity.enforcement import LEDGER, AuthzMode, Outcome
    claim = owner(db)
    client = TestClient(app_for(db, monkeypatch, claim.ctx))
    LEDGER.clear()
    LEDGER.record(router_key="identity_admin", method="GET", path_template="/v1/identity/audit",
                  outcome=Outcome(False, DenyReason.UNAUTHENTICATED, "audit:read"),
                  mode=AuthzMode.ENFORCE)
    try:
        report = client.get("/v1/identity/shadow-report").json()
    finally:
        LEDGER.clear()
    mine = {r["router_key"]: r for r in report["routers"]}["identity_admin"]
    assert mine["operations"] == 13 and mine["undeclared"] == 0
    assert mine["would_deny"] == 1 and mine["unexplained"] == 1 and not mine["ready_to_enforce"]
    assert mine["top"][0]["principal_kind"] == "anonymous"
    explained = client.post("/v1/identity/shadow-report/explain", json={
        "router_key": "identity_admin", "method": "GET", "path_template": "/v1/identity/audit",
        "reason": "unauthenticated", "note": "anonymous probes are meant to be refused"})
    assert explained.status_code == 200
    again = {r["router_key"]: r for r in client.get("/v1/identity/shadow-report").json()[
        "routers"]}["identity_admin"]
    assert again["unexplained"] == 0
    assert client.post("/v1/identity/shadow-report/explain", json={"note": "x"}).status_code == 422
