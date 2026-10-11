"""`oidc:read` (integration default, 2026-10-09): every App sign-in admin list/detail GET is
declared with it (org, read-only, NO step-up; Owner, Admin, Auditor), so the console loads without
step-up on every visit; every write keeps its step-up permission."""
from __future__ import annotations

from fastapi.routing import APIRoute
from fastapi.testclient import TestClient

from apps.api.app import main
from apps.api.tests.identity_fixtures import IdentityWorld
from boss_engine.identity.catalogue import load
from boss_engine.identity.declarations import access_of


def _oidc_admin_ops():
    for route in main.app.routes:
        if isinstance(route, APIRoute) and route.path.startswith("/v1/oidc/"):
            for method in route.methods - {"HEAD", "OPTIONS"}:
                yield method, route.path, getattr(access_of(route.endpoint), "permission", None)


def test_the_permission_is_read_only_org_wide_and_never_step_up():
    meta = {p.key: p for p in load().permissions}["oidc:read"]
    assert meta.scope == "org" and not meta.step_up and not meta.critical
    assert set(meta.roles) == {"owner", "admin", "auditor"}


def test_every_admin_get_reads_with_it_and_every_write_keeps_step_up():
    meta = {p.key: p for p in load().permissions}
    ops = list(_oidc_admin_ops())
    gets = [(p, perm) for m, p, perm in ops if m == "GET"]
    writes = [(m, p, perm) for m, p, perm in ops if m != "GET"]
    # reads are oidc:read (org-wide) or oidc_client:read (a project's clients, P1-6); neither steps up
    assert gets and all(perm in ("oidc:read", "oidc_client:read") and not meta[perm].step_up for _, perm in gets), gets
    assert writes and all(meta[perm].step_up for _, _, perm in writes), writes


def test_an_auditor_without_step_up_lists_pools_and_clients(engine):
    world = IdentityWorld.create(engine)
    auditor = world.person("oidc-auditor")
    world.bind(auditor, "auditor")
    client = TestClient(main.app, raise_server_exceptions=False)
    with world.acting_as(world.ctx(auditor, mfa=False)):
        for path in ("/v1/oidc/pools", "/v1/oidc/clients", "/v1/oidc/consents"):
            r = client.get(path)
            assert r.status_code == 200, (path, r.status_code, r.text[:300])
        assert client.post("/v1/oidc/pools", json={"name": "x"}).status_code == 403   # reading is not writing
