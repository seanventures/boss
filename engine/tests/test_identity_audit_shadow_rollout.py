"""Shadow -> enforce, one router at a time (IDENTITY.md §12 M2/M4, D21): the report says which
routers are ready, why the others are not, and the exact BOSS_AUTHZ_ROUTER_MODES to set."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from boss_engine.identity import P, PublicReason, Scope, access_of, permit, public
from boss_engine.identity import shadow_report
from boss_engine.identity.authz import DenyReason
from boss_engine.identity.enforcement import AuthzMode, Outcome, RouteEntry, ShadowLedger
from boss_engine.identity.policy import set_extra
from engine.tests.identity_s5_support import (  # noqa: F401, F811  (db, s5_engine: fixtures)
    ORG,
    community_install,
    db,
    events,
    s5_engine,
    settings,
)

DECLARED = access_of(permit(P.SETTINGS_READ, scope=Scope.org())(lambda: None))
HEALTH = access_of(public(reason=PublicReason.LIVENESS)(lambda: None))


def routes():
    return [RouteEntry(("GET",), "/v1/setup", "http", "setup", DECLARED),
            RouteEntry(("GET",), "/health", "http", "setup", HEALTH),
            RouteEntry(("GET", "POST"), "/v1/github/x", "http", "github", DECLARED),
            RouteEntry(("GET",), "/v1/github/y", "http", "github", None),
            RouteEntry(("GET",), "/v1/deploy", "http", "deployments", DECLARED)]


def test_the_report_names_ready_routers_and_the_exact_setting_to_flip_them(db):
    community_install(db)
    cfg = settings(authz_router_modes="deployments=enforce")
    now = datetime.now(timezone.utc)
    ledger = ShadowLedger()
    ledger.record(router_key="setup", method="GET", path_template="/v1/setup",
                  outcome=Outcome(False, DenyReason.MISSING_PERMISSION, "settings:read",
                                  principal_kind="user"), mode=AuthzMode.SHADOW)
    shadow_report.flush(db, ledger=ledger, settings=cfg, now=now)
    assert events(db, "authz.mode_changed")[-1]["details"]["to"]["router_modes"] == \
        "deployments=enforce"

    early = shadow_report.report(db, routes=routes(), settings=cfg, now=now)
    by = {r["router_key"]: r for r in early["routers"]}
    assert not by["setup"]["ready_to_enforce"] and "observed 0 of 7 days" in by["setup"]["blockers"]
    assert by["github"]["undeclared"] == 1
    assert by["github"]["undeclared_sample"] == ["GET /v1/github/y"]
    assert by["deployments"]["mode"] == "enforce"

    set_extra(ORG, "shadow_started_at", (now - timedelta(days=8)).isoformat(), db=db)
    later = shadow_report.report(db, routes=routes(), settings=cfg, now=now)
    by = {r["router_key"]: r for r in later["routers"]}
    assert by["setup"]["blockers"] == ["1 unexplained would-denies in the last 7 days"]
    assert by["setup"]["top"][0]["principal_kind"] == "user"
    assert not by["github"]["ready_to_enforce"]
    assert later["suggested_router_modes"] == "deployments=enforce"

    from boss_engine.identity.principal import (AuthContext, AuthMethod, Principal,
                                                PrincipalKind, PrincipalStatus)
    owner = AuthContext(Principal("usr_o", PrincipalKind.USER, ORG, PrincipalStatus.ACTIVE, "O"),
                        AuthMethod.SESSION)
    shadow_report.explain_denial(ORG, {"router_key": "setup", "method": "GET",
                                       "path_template": "/v1/setup",
                                       "reason": "missing_permission",
                                       "note": "viewers are meant to be refused"}, db=db, ctx=owner)
    ready = shadow_report.report(db, routes=routes(), settings=cfg, now=now)
    by = {r["router_key"]: r for r in ready["routers"]}
    assert by["setup"]["ready_to_enforce"] and by["setup"]["blockers"] == []
    assert ready["suggested_router_modes"] == "deployments=enforce,setup=enforce"
    assert "BOSS_AUTHZ_ROUTER_MODES=deployments=enforce,setup=enforce" in ready["next_step"]
    assert not ready["global_ready"]                     # github still has an undeclared route

    flipped = settings(authz_router_modes="deployments=enforce,setup=enforce")
    shadow_report.flush(db, ledger=ShadowLedger(), settings=flipped, now=now)
    assert len(events(db, "authz.mode_changed")) == 2
