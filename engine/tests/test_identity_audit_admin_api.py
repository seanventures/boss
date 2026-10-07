"""/v1/identity/{audit,posture,policy,shadow-report,legacy} (stream S5's router).

The handlers run against the rolled-back test transaction; the request's AuthContext comes from a
fake of S1's authenticate (the router reads it through the guard's own helpers).
"""
from __future__ import annotations

from contextlib import contextmanager
from datetime import timedelta

from fastapi import FastAPI, Request
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


def test_every_route_is_declared_and_only_the_epoch_is_open_to_any_signed_in_principal():
    from apps.api.app.routers.identity_admin import build_identity_admin_router
    routes = [r for r in build_identity_admin_router(start_background=False).routes
              if isinstance(r, APIRoute)]
    assert len(routes) == 15
    for route in routes:
        access = access_of(route.endpoint)
        assert access is not None, route.path
        if route.path.startswith("/v1/identity/epoch"):
            assert type(access).__name__ == "Authenticated" and access.self_only
        else:
            assert access.permission in PERMISSION_META, route.path
        assert route.endpoint.__module__.rsplit(".", 1)[-1].startswith("identity")   # enforced


def test_audit_list_filter_get_export_and_verify(db, monkeypatch):
    claim = owner(db)
    client = TestClient(app_for(db, monkeypatch, claim.ctx))
    page = client.get("/v1/identity/audit", params={"event": "bootstrap.*"}).json()
    assert set(page) == {"events", "cursor", "chain"}
    assert [e["event_type"] for e in page["events"]] == ["bootstrap.claimed"]
    first = page["events"][0]
    assert len(first["hash"]) == 64 and first["actor"]["id"] == claim.principal_id
    assert first["actor"]["name"] == "Param" and first["outcome"] == "ok"
    assert first["id"] == f"sev_{first['seq']}"
    assert first["raw"]["event_type"] == "bootstrap.claimed"
    assert client.get(f"/v1/identity/audit/{first['id']}").json()["seq"] == first["seq"]
    assert client.get("/v1/identity/audit/sev_999999").status_code == 404
    assert client.get("/v1/identity/audit/nonsense").status_code == 404
    exported = client.post("/v1/identity/audit/export").json()
    assert exported["url"] is None and exported["events"] == len(exported["bundle"]["events"])
    assert exported["bundle"]["verification"]["ok"] and len(exported["anchor"]) == 64
    verified = client.post("/v1/identity/audit/verify").json()
    assert verified["ok"] and verified["first_break"] is None and verified["seq"] >= 1
    assert events(db, "audit.verified")[-1]["outcome"] == "success"
    chain = client.get("/v1/identity/audit").json()["chain"]
    assert chain["ok"] and chain["verified_at"] and chain["first_break"] is None
    small = client.get("/v1/identity/audit", params={"limit": 1}).json()
    assert len(small["events"]) == 1 and isinstance(small["cursor"], str)
    older = client.get("/v1/identity/audit", params={"limit": 1,
                                                     "cursor": small["cursor"]}).json()
    assert older["events"][0]["seq"] < small["events"][0]["seq"]
    assert client.post("/v1/identity/audit/prune", json={"older_than_days": 1}).status_code == 422


def test_audit_filters_q_outcome_actor_and_grant(db, monkeypatch):
    from boss_engine.identity import audit
    from boss_engine.identity.audit import Outcome, SecurityEvent
    claim = owner(db)
    agent_ctx = dict(org_id=ORG, actor_principal_id="agt_forge", actor_kind="agent",
                     on_behalf_of=claim.principal_id, grant_id="dlg_01JB7Q",
                     chain=["agt_atlas#run_8811", "agt_forge#run_8812"], token_jti="rt_7R")
    audit.record(SecurityEvent(event_type="authz.denied", outcome=Outcome.DENIED,
                               permission="deployment:production", scope="project:acme-web",
                               request_id="req_findme_123", details={"reason": "not_delegable"},
                               **agent_ctx), db=db)
    audit.record(SecurityEvent(event_type="token.exchanged", outcome=Outcome.SUCCESS,
                               **agent_ctx), db=db)
    audit.record(SecurityEvent(org_id=ORG, event_type="auth.login.failed",
                               outcome=Outcome.FAILURE, ip="45.155.205.9",
                               details={"identifier": audit.identifier_hmac("x@y.z")}), db=db)
    client = TestClient(app_for(db, monkeypatch, claim.ctx))

    def types(**params):
        return [e["event_type"] for e in client.get("/v1/identity/audit", params=params)
                .json()["events"]]

    assert types(grant="dlg_01JB7Q") == ["token.exchanged", "authz.denied"]
    assert types(outcome="denied") == ["authz.denied"]
    assert types(outcome="denied,failed") == ["auth.login.failed", "authz.denied"]
    assert types(outcome="failure") == ["auth.login.failed"]        # the stored word works too
    assert types(actor="agt_forge") == ["token.exchanged", "authz.denied"]
    # an agent's events are also the person's it acted for
    assert {"token.exchanged", "authz.denied", "bootstrap.claimed"} <= set(
        types(actor=claim.principal_id))
    assert types(q="production") == ["authz.denied"]
    assert types(q="req_findme") == ["authz.denied"]               # by request id
    assert "bootstrap.claimed" in types(q="param")                   # by the actor's name
    assert types(q="100%_nothing") == []                             # LIKE wildcards are literal
    denied = client.get("/v1/identity/audit", params={"outcome": "denied"}).json()["events"][0]
    assert denied["on_behalf_of"]["id"] == claim.principal_id
    assert [h["id"] for h in denied["chain"]] == [claim.principal_id, "agt_atlas", "agt_forge"]
    assert denied["run_id"] == "run_8812" and denied["request_id"] == "req_findme_123"
    assert denied["what"] == "deployment:production · project:acme-web"
    failed = client.get("/v1/identity/audit", params={"outcome": "failed"}).json()["events"][0]
    assert failed["actor"]["kind"] == "unknown" and failed["source"] == "45.155.205.9"


def test_every_request_gets_one_request_id_on_its_events(db, monkeypatch):
    from fastapi import FastAPI

    from boss_engine.identity import audit
    from boss_engine.identity.audit import Outcome, SecurityEvent
    from boss_engine.identity.request_ids import RequestIdMiddleware, current_request_id
    app = FastAPI()
    app.add_middleware(RequestIdMiddleware)
    community_install(db)

    @app.post("/change")
    def change(request: Request):
        audit.record(SecurityEvent(org_id=ORG, event_type="policy.changed",
                                   outcome=Outcome.SUCCESS, actor_principal_id="usr_x"), db=db)
        return {"header": request.headers.get("x-request-id"), "var": current_request_id.get()}

    client = TestClient(app)
    minted = client.post("/change")
    body = minted.json()
    assert body["header"] == body["var"] == minted.headers["x-request-id"]
    assert body["header"].startswith("req_")
    assert events(db, "policy.changed")[-1]["request_id"] == body["header"]
    kept = client.post("/change", headers={"X-Request-Id": "console-click-0001"})
    assert kept.headers["x-request-id"] == "console-click-0001"
    assert events(db, "policy.changed")[-1]["request_id"] == "console-click-0001"
    hostile = client.post("/change", headers={"X-Request-Id": "x\r\nSet-Cookie: a=b"})
    assert hostile.headers["x-request-id"].startswith("req_")


def test_the_epoch_moves_on_any_identity_change_and_not_on_bookkeeping(db, monkeypatch):
    from sqlalchemy import text

    from boss_engine.identity import change_feed, migration
    claim = owner(db)
    client = TestClient(app_for(db, monkeypatch, claim.ctx))
    epoch = lambda: client.get("/v1/identity/epoch").json()["epoch"]  # noqa: E731
    start = epoch()
    assert start == change_feed.current_epoch(ORG, db=db) > 0
    migration.bind(db, ORG, claim.principal_id, "arl_auditor", "org", ORG)
    after_bind = epoch()
    assert after_bind > start
    db.execute(text("UPDATE identity_principals SET status = 'disabled' WHERE id = "
                    "'usr_legacy_shared'"))
    after_status = epoch()
    assert after_status > after_bind
    db.execute(text("UPDATE identity_principals SET updated_at = now() WHERE id = :p"),
               {"p": claim.principal_id})
    db.execute(text("UPDATE identity_webauthn_credentials SET sign_count = sign_count + 1"))
    db.execute(text("UPDATE identity_legacy_mappings SET use_count = use_count + 1"))
    assert epoch() == after_status                                   # bookkeeping: no move
    client.put("/v1/identity/policy", json={"step_up_minutes": 5})
    assert epoch() > after_status


def test_the_epoch_push_yields_at_once_on_change_and_heartbeats_otherwise():
    import asyncio

    from boss_engine.identity.change_feed import EpochFeed, sse

    async def scenario():
        feed = EpochFeed()
        feed.publish(41)
        gone = {"now": False}

        async def disconnected():
            return gone["now"]

        stream = feed.changes(heartbeat=0.05, is_disconnected=disconnected)
        first = await stream.__anext__()
        beat = await stream.__anext__()
        asyncio.get_running_loop().call_later(0.01, feed.publish, 42)
        nxt = await stream.__anext__()
        while nxt is None:
            nxt = await stream.__anext__()
        feed.publish(40)                                       # never goes backwards
        assert feed.latest == 42
        gone["now"] = True
        rest = [x async for x in stream]
        return first, beat, nxt, rest, len(feed._subscribers)

    first, beat, nxt, rest, left = asyncio.run(scenario())
    assert (first, beat, nxt, rest, left) == (41, None, 42, [], 0)
    assert sse(42) == 'event: epoch\ndata: {"epoch": 42}\n\n' and sse(None).startswith(":")


#: S4's shapes at id/s4-console@dd992eda (apps/web/lib/identity/types.ts)
S4_POLICY_KEYS = {"mfa_required_roles", "session_idle_minutes", "session_absolute_hours",
                  "password_min_length", "breach_check", "key_max_days",
                  "key_no_expiry_for_service_accounts", "delegation_max_days",
                  "delegation_max_depth", "delegation_until_revoked",
                  "github_require_user_repo_access", "network_mode", "signup", "smtp",
                  "public_origin"}
S4_POSTURE_KEYS = {"score", "headline", "summary", "checks", "counts", "acting_now", "audit",
                   "legacy"}
S4_CHECK_KEYS = {"id", "label", "status", "value", "unit", "detail", "fix"}
S4_COUNTS = {"people", "active", "invited", "disabled", "agents", "suspended_agents", "groups",
             "roles_builtin", "roles_custom", "service_accounts", "active_grants", "runs_acting",
             "tokens_out"}


def test_policy_has_s4s_shape_and_a_round_trip_changes_nothing(db, monkeypatch):
    claim = owner(db)
    client = TestClient(app_for(db, monkeypatch, claim.ctx))
    current = client.get("/v1/identity/policy").json()
    assert S4_POLICY_KEYS <= set(current) and "extra" not in current
    assert current["network_mode"]["active"] is True and current["network_mode"]["ends_at"]
    assert current["signup"] == {"enabled": False, "domains": []}
    assert current["breach_check"] is False and current["delegation_max_depth"] == 3
    assert current["smtp"] is False and current["github_require_user_repo_access_auto"] is True
    assert current["github_require_user_repo_access"] is False      # one person: automatic off
    before = len(events(db, "policy.changed"))
    same = client.put("/v1/identity/policy", json=current)          # the console saves it all back
    assert same.status_code == 200 and same.json() == current
    assert len(events(db, "policy.changed")) == before               # nothing changed
    assert same.json()["github_require_user_repo_access_auto"] is True   # D28 stays automatic

    changed = client.put("/v1/identity/policy", json={
        **current, "breach_check": True, "delegation_max_depth": 2, "legacy_cutoff_days": 45,
        "signup": {"enabled": True, "domains": ["acme.dev"]}}).json()
    assert changed["breach_check"] and changed["delegation_max_depth"] == 2
    assert changed["signup"] == {"enabled": True, "domains": ["acme.dev"]}
    diff = events(db, "policy.changed")[-1]["details"]["diff"]
    assert set(diff) == {"hibp_check", "org_max_depth", "legacy_cutoff_days", "signup_enabled",
                         "signup_domains"}
    # F3/M3: no policy moves a bridge past the absolute cutoff (claim + 30 days)
    assert changed["network_mode"]["ends_at"] == current["network_mode"]["ends_at"]
    pinned = client.put("/v1/identity/policy",
                        json={"github_require_user_repo_access": True}).json()
    assert pinned["github_require_user_repo_access"] and not \
        pinned["github_require_user_repo_access_auto"]
    auto = client.put("/v1/identity/policy", json={"github_require_user_repo_access": None}).json()
    assert auto["github_require_user_repo_access_auto"] is True


def test_policy_refusals(db, monkeypatch):
    claim = owner(db)
    client = TestClient(app_for(db, monkeypatch, claim.ctx))
    for bad in ({"nope": 1}, {"password_min_length": 3}, {"mfa_required_roles": ["god"]},
                {"public_origin": "http://plain"}, {"extra": {"x": 1}}, {"smtp": True},
                {"signup": {"enabled": True, "admin": True}}, ["not", "an", "object"],
                {"network_mode": "off"}):
        response = client.put("/v1/identity/policy", json=bad)
        assert response.status_code == 422 and response.json()["error"] == "invalid_policy", bad


def test_require_sign_in_through_the_policy_and_never_back(db, monkeypatch):
    claim = owner(db)
    client = TestClient(app_for(db, monkeypatch, claim.ctx))
    current = client.get("/v1/identity/policy").json()
    off = client.put("/v1/identity/policy",
                     json={**current, "network_mode": {"active": False, "ends_at": None}})
    assert off.status_code == 200 and off.json()["network_mode"] == {"active": False,
                                                                    "ends_at": None}
    assert events(db, "legacy.path_retired")[-1]["actor_principal_id"] == claim.principal_id
    again = client.put("/v1/identity/policy",
                       json={"network_mode": {"active": True, "ends_at": None}})
    assert again.status_code == 409 and again.json()["error"] == "policy_refused"


def test_posture_has_s4s_shape(db, monkeypatch):
    claim = owner(db)
    client = TestClient(app_for(db, monkeypatch, claim.ctx))
    report = client.get("/v1/identity/posture").json()
    assert S4_POSTURE_KEYS <= set(report)
    assert [c["id"] for c in report["checks"]] == ["mfa", "keys", "delegations", "legacy",
                                                   "owners", "audit", "enforcement", "origin", "github"]
    for check in report["checks"]:
        assert S4_CHECK_KEYS <= set(check) and check["status"] in ("ok", "warn", "bad")
        assert check["fix"] is None or set(check["fix"]) == {"label", "href"}
    assert set(report["counts"]) == S4_COUNTS
    assert report["counts"]["people"] == 1 and report["counts"]["active"] == 1   # placeholders
    mfa = report["checks"][0]                                                  # are not people
    assert (mfa["value"], mfa["unit"], mfa["status"]) == ("1", "of 1", "ok")
    legacy = {item["kind"]: item for item in report["legacy"]}
    assert legacy["network_bridge"]["until"] and report["checks"][3]["status"] == "bad"
    assert set(report["audit"]) == {"verified_at", "seq", "ok"} and report["audit"]["seq"] >= 1
    assert 0 <= report["score"] <= 100 and report["headline"].startswith("Posture:")
    assert report["acting_now"] == [] and len(report["summary"]) == 3


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
    assert mine["operations"] == 15 and mine["undeclared"] == 0
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
