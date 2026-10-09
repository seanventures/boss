"""The app platform's day-1 routes (APP_TEMPLATES_AND_SECURITY_LOOP.md §8.2, stream S0), on the
real app under enforce.

Every route the four router modules declare boots with the API, is guarded by its declaration,
and, past the guard, answers 501 `not_implemented_yet` naming its owner stream. As each stream
builds its routes, it narrows STUB_OWNERS (a route that really works no longer answers 501).
S5 (links) is built: apps/api/tests/test_app_links.py.
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

STUB_OWNERS = {
    "apps.api.app.routes_templates": "S2",
    "apps.api.app.routes_security": "S4",
    "apps.api.app.oidc_routes": "S6",
}


def _operations():
    for route in main.app.routes:
        if isinstance(route, APIRoute) and route.endpoint.__module__ in STUB_OWNERS:
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


def test_the_stub_modules_are_mounted():
    mounted = {route.endpoint.__module__ for _, route in _operations()}
    assert mounted == set(STUB_OWNERS)
    assert len(list(_operations())) >= 49       # the day-1 surface less S5's five; streams only add


def test_an_owner_reaches_every_stub_and_it_says_who_builds_it(world, objects_in_project_a):
    owner = world.person("app-platform-owner")
    world.bind(owner, "owner")
    client = TestClient(main.app, raise_server_exceptions=False)
    wrong = []
    with world.acting_as(world.ctx(owner)):
        for method, route in _operations():
            response = _call(client, method, route, world.project_a)
            body = response.json() if response.headers.get("content-type", "").startswith("application/json") else {}
            expected = STUB_OWNERS[route.endpoint.__module__]
            if response.status_code != 501 or body.get("error") != "not_implemented_yet" \
                    or body.get("owner") != expected:
                wrong.append(f"{method} {route.path}: {response.status_code} {body}")
    assert not wrong, wrong


def test_without_a_credential_only_the_oidc_protocol_answers(world):
    client = TestClient(main.app, raise_server_exceptions=False)
    wrong = []
    with world.acting_as(None):
        for method, route in _operations():
            response = _call(client, method, route, world.project_a)
            is_public = isinstance(access_of(route.endpoint), Public)
            if is_public != route.path.startswith("/oidc/"):
                wrong.append(f"{method} {route.path}: public={is_public}")
            elif (response.status_code == 501) != is_public:
                wrong.append(f"{method} {route.path}: {response.status_code}")
    assert not wrong, wrong


def test_a_viewer_cannot_import_or_approve_a_template(world):
    viewer = world.person("app-platform-viewer")
    world.bind(viewer, "viewer")
    client = TestClient(main.app, raise_server_exceptions=False)
    with world.acting_as(world.ctx(viewer)):
        assert client.get("/v1/templates").status_code == 501
        assert client.post("/v1/templates/import", json={}).status_code == 403
        assert client.post("/v1/templates/zz1/approve", json={}).status_code == 403
