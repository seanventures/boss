"""The app platform's day-1 routes (APP_TEMPLATES_AND_SECURITY_LOOP.md §8.2, stream S0), on the
real app under enforce.

Every route the four router modules declare boots with the API and is guarded by its
declaration. Past the guard, a route its stream has not built yet answers 501
`not_implemented_yet` naming its owner stream. As each stream builds its routes, it adds them to
BUILT (a route that really works no longer answers 501; the stream's own suite tests it).
"""
from __future__ import annotations

import re

import pytest
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient

from apps.api.app import main
from apps.api.tests.identity_fixtures import IdentityWorld
from boss_engine.identity import scope as scope_module
from boss_engine.identity.declarations import Public, access_of

#: The four day-1 router modules and the stream that owns each.
MODULES = {
    "apps.api.app.routes_templates": "S2",
    "apps.api.app.routes_security": "S4",
    "apps.api.app.routes_links": "S5",
    "apps.api.app.oidc_routes": "S6",
}

#: Routes a stream has built (they no longer answer 501), per module: path -> built?
BUILT = {
    "apps.api.app.routes_templates": lambda path: True,      # S2: test_templates_api.py
    "apps.api.app.routes_security": lambda path: True,       # S4: test_security_scan_routes.py
    "apps.api.app.routes_links": lambda path: True,          # S5: test_app_links.py
    # S6 phase 1: the protocol, pools, clients, roles and consents (test_oidc_provider_*.py);
    # customer-pool users are phase 2 and still answer 501.
    "apps.api.app.oidc_routes": lambda path: "/users" not in path,
}


def _is_built(route: APIRoute) -> bool:
    return BUILT.get(route.endpoint.__module__, lambda _p: False)(route.path)


def _operations(*, stubs_only: bool = True):
    for route in main.app.routes:
        if isinstance(route, APIRoute) and route.endpoint.__module__ in MODULES:
            if stubs_only and _is_built(route):
                continue
            for method in sorted(route.methods - {"HEAD", "OPTIONS"}):
                yield method, route


def _call(client: TestClient, method: str, route: APIRoute, project_id: str):
    path = re.sub(r"\{[^}]+\}", "zz1", route.path)
    kw = {"params": {"project_id": project_id}}
    if method not in ("GET", "DELETE"):
        kw["json"] = {"project_id": project_id}
    return client.request(method, path, **kw)


@pytest.fixture(scope="module")
def world(engine) -> IdentityWorld:
    return IdentityWorld.create(engine)


@pytest.fixture
def objects_in_project_a(monkeypatch, world):
    for kind in scope_module.registered_object_kinds():
        monkeypatch.setitem(scope_module._OBJECT_LOADERS, kind,  # noqa: SLF001
                            lambda db, oid, o=world.org, p=world.project_a: (o, p))


def test_the_four_modules_are_mounted():
    mounted = {route.endpoint.__module__ for _, route in _operations(stubs_only=False)}
    assert mounted == set(MODULES)
    assert len(list(_operations(stubs_only=False))) >= 54       # the day-1 surface; streams only add


def test_an_owner_reaches_every_stub_and_it_says_who_builds_it(world, objects_in_project_a):
    owner = world.person("app-platform-owner")
    world.bind(owner, "owner")
    client = TestClient(main.app, raise_server_exceptions=False)
    wrong = []
    with world.acting_as(world.ctx(owner)):
        for method, route in _operations():
            response = _call(client, method, route, world.project_a)
            body = response.json() if response.headers.get("content-type", "").startswith("application/json") else {}
            expected = MODULES[route.endpoint.__module__]
            if response.status_code != 501 or body.get("error") != "not_implemented_yet" \
                    or body.get("owner") != expected:
                wrong.append(f"{method} {route.path}: {response.status_code} {body}")
    assert not wrong, wrong


def test_without_a_credential_only_the_oidc_protocol_answers(world):
    client = TestClient(main.app, raise_server_exceptions=False)
    wrong = []
    with world.acting_as(None):
        for method, route in _operations(stubs_only=False):
            response = _call(client, method, route, world.project_a)
            built = _is_built(route)
            is_public = isinstance(access_of(route.endpoint), Public)
            if is_public != route.path.startswith("/oidc/"):
                wrong.append(f"{method} {route.path}: public={is_public}")
            elif not built and (response.status_code == 501) != is_public:
                wrong.append(f"{method} {route.path}: {response.status_code}")
            elif built and not is_public and response.status_code not in (401, 403):
                wrong.append(f"{method} {route.path}: {response.status_code}")
    assert not wrong, wrong


def test_a_viewer_cannot_import_or_approve_a_template(world):
    viewer = world.person("app-platform-viewer")
    world.bind(viewer, "viewer")
    client = TestClient(main.app, raise_server_exceptions=False)
    with world.acting_as(world.ctx(viewer)):
        assert client.get("/v1/templates").status_code == 200
        assert client.post("/v1/templates/import", json={}).status_code == 403
        assert client.post("/v1/templates/zz1/approve", json={}).status_code == 403
