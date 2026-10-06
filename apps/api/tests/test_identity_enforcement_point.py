"""Every route goes through the one enforcement point, and every route is declared or listed.

IDENTITY.md §6.3, §6.6 and §15.4 R1; frozen in Phase 0 (IDENTITY_BUILD.md §2.4).

  - the guard is an app-level dependency, so it sits on every HTTP and WebSocket route;
  - shadow mode refuses nothing and records what it would deny; enforce refuses, fail-closed;
  - the identity API is enforced even while everything else is in shadow;
  - the boot-time coverage check lists undeclared routes, and refuses to boot when told to;
  - tests/identity/undeclared_routes.txt is exactly today's undeclared routes: it may only shrink.
"""
from __future__ import annotations

from pathlib import Path

import pytest
from fastapi import Depends, FastAPI, WebSocket
from fastapi.routing import APIRoute, APIWebSocketRoute
from fastapi.testclient import TestClient

from apps.api.app import identity_guard
from apps.api.app.main import app
from boss_engine.config import get_settings
from boss_engine.identity import P, Permit, PublicReason, Scope, permit, public
from boss_engine.identity.catalogue import is_known
from boss_engine.identity.enforcement import LEDGER, CoverageReport, UndeclaredRoutesError

BASELINE = Path(__file__).resolve().parents[3] / "tests" / "identity" / "undeclared_routes.txt"


def _baseline() -> list[str]:
    return sorted(line.strip() for line in BASELINE.read_text(encoding="utf-8").splitlines()
                  if line.strip() and not line.startswith("#"))


@pytest.fixture
def authz_mode(monkeypatch):
    settings = get_settings()

    def set_mode(mode: str, *, coverage: str = "", routers: str = "") -> None:
        monkeypatch.setattr(settings, "authz_mode", mode)
        monkeypatch.setattr(settings, "authz_coverage", coverage)
        monkeypatch.setattr(settings, "authz_router_modes", routers)

    LEDGER.clear()
    yield set_mode
    LEDGER.clear()


def _fixture_app() -> FastAPI:
    fixture = FastAPI(dependencies=[Depends(identity_guard.enforce_access)])
    identity_guard.install(fixture)

    @fixture.get("/undeclared")
    def undeclared():
        return {"ok": True}

    @fixture.get("/health")
    @public(reason=PublicReason.LIVENESS)
    def health():
        return {"ok": True}

    @fixture.get("/projects/{project_id}/settings")
    @permit(P.SETTINGS_READ, scope=Scope.project(path="project_id"))
    def settings_view(project_id: str):
        return {"project_id": project_id}

    @fixture.websocket("/ws")
    async def ws(websocket: WebSocket):
        await websocket.accept()
        await websocket.send_text("hi")
        await websocket.close()

    return fixture


def test_the_guard_is_on_every_route_of_the_real_app():
    for route in app.router.routes:
        if isinstance(route, (APIRoute, APIWebSocketRoute)):
            calls = [d.call for d in route.dependant.dependencies]
            assert identity_guard.enforce_access in calls, f"{route.path} bypasses the guard"


def test_every_route_is_declared_or_on_the_shrinking_list():
    report = CoverageReport(identity_guard.route_inventory(app))
    undeclared, baseline = set(report.undeclared), set(_baseline())
    assert not undeclared - baseline, (
        "declare these new routes with @permit, @public or @authenticated: "
        f"{sorted(undeclared - baseline)}")
    assert not baseline - undeclared, (
        f"declared now (or gone); remove from {BASELINE.name}: {sorted(baseline - undeclared)}")


def test_every_declared_permission_is_in_the_catalogue():
    for entry in identity_guard.route_inventory(app):
        if isinstance(entry.access, Permit):
            assert is_known(entry.access.permission), entry.labels()


def test_shadow_mode_refuses_nothing_and_records_what_it_would_deny(authz_mode):
    authz_mode("shadow")
    client = TestClient(_fixture_app())
    assert client.get("/undeclared").status_code == 200
    assert client.get("/projects/p1/settings").status_code == 200   # no credential at all
    with client.websocket_connect("/ws") as ws:
        assert ws.receive_text() == "hi"
    reasons = {(k.path_template, k.reason) for k in LEDGER.snapshot()}
    assert ("/undeclared", "undeclared_route") in reasons
    # S5's legacy bridge answers for a request with no credential: unauthenticated (it was
    # not_evaluated while authentication was a stub; the 503 path is pinned in the engine tests)
    assert ("/projects/{project_id}/settings", "unauthenticated") in reasons
    assert ("/ws", "undeclared_route") in reasons
    assert not any(k.path_template == "/health" for k in LEDGER.snapshot())


def test_enforce_mode_refuses_with_the_spec_error_body_and_fails_closed(authz_mode,
                                                                       monkeypatch):
    authz_mode("enforce")
    client = TestClient(_fixture_app())
    refused = client.get("/undeclared")
    assert refused.status_code == 403 and refused.json() == {"error": "forbidden"}
    anonymous = client.get("/projects/p1/settings")
    assert anonymous.status_code == 401 and anonymous.json() == {"error": "unauthenticated"}
    assert client.get("/health").status_code == 200
    # a known principal whose decision cannot be made yet (S2's check is a stub): 503, not 200
    from boss_engine.identity import AuthContext, AuthMethod, Principal, PrincipalKind, \
        PrincipalStatus
    bridged = AuthContext(Principal("sys_legacy_console", PrincipalKind.SYSTEM, "org_boss",
                                    PrincipalStatus.ACTIVE, "Legacy console key"),
                          AuthMethod.LEGACY_API_KEY, legacy="console_api_key")
    monkeypatch.setattr(identity_guard.legacy, "bridge", lambda creds: bridged)
    unavailable = client.get("/projects/p1/settings")
    assert unavailable.status_code == 503
    assert unavailable.json() == {"error": "identity_unavailable"}


def test_off_turns_the_guard_off(authz_mode):
    authz_mode("off")
    assert TestClient(_fixture_app()).get("/undeclared").status_code == 200
    assert not LEDGER.snapshot()


def test_the_identity_api_is_enforced_even_in_shadow(authz_mode):
    authz_mode("shadow")
    response = TestClient(app).get("/v1/identity/me")
    assert response.status_code == 401 and response.json() == {"error": "unauthenticated"}


def test_the_boot_check_lists_undeclared_routes_and_refuses_to_boot_on_fail(authz_mode):
    authz_mode("shadow")
    lines: list[str] = []
    report = identity_guard.check_app_coverage(_fixture_app(), echo=lines.append)
    assert "GET /undeclared" in report.undeclared and "WS /ws" in report.undeclared
    assert "GET /health" in report.declared
    authz_mode("shadow", coverage="fail")
    with pytest.raises(UndeclaredRoutesError, match="GET /undeclared"):
        identity_guard.check_app_coverage(_fixture_app(), echo=lambda _: None)
    authz_mode("enforce")
    with pytest.raises(UndeclaredRoutesError):
        identity_guard.check_app_coverage(_fixture_app(), echo=lambda _: None)
