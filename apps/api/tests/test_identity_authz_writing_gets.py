"""GETs that write are not CSRF-able (IDENTITY.md §4.3; identity int-b item 6).

The session cookie is SameSite=Lax, so a cross-site top-level GET navigation carries it. A GET
that writes (seeds defaults, reaps, freezes the telemetry payload, probes a host) is therefore:

  * declared `writes_on_get=True`, which makes the enforcement point treat a cookie-authenticated
    GET to it like a write: it needs `Sec-Fetch-Site: same-origin` (the console's own fetch) or a
    valid X-Boss-CSRF, in shadow as in enforce;
  * also served as POST at `<path>/load` (same endpoint, same declaration): the method its callers
    move to; the GET stays as a deprecated alias.
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from apps.api.app import identity_guard
from apps.api.app.main import app
from apps.api.tests import identity_matrix
from apps.api.tests.identity_fixtures import IdentityWorld
from apps.api.tests.test_identity_authz_read_only import GETS_THAT_WRITE
from boss_engine.identity import scope as scope_module
from boss_engine.identity.declarations import Permit
from boss_engine.identity.principal import AuthMethod
from boss_engine.identity.sessions import csrf_token


def _routes():
    by_label = {}
    for entry in identity_guard.route_inventory(app):
        for label in entry.labels():
            by_label[label] = entry
    return by_label


ROUTES = _routes()
WRITING = sorted(GETS_THAT_WRITE)


def test_every_writing_get_is_flagged_and_has_a_post_twin():
    wrong = []
    for label in WRITING:
        entry = ROUTES[label]
        access = entry.access
        if not (isinstance(access, Permit) and access.writes_on_get):
            wrong.append(f"{label}: not writes_on_get")
        path = label.split(" ", 1)[1]
        twin = ROUTES.get("POST " + identity_guard.writing_get_twin(path))
        if twin is None or twin.access is not access:
            wrong.append(f"{label}: no POST twin with the same declaration")
    assert not wrong, wrong


def test_only_the_writing_gets_carry_the_flag():
    """A new GET that writes is added to GETS_THAT_WRITE on purpose (and gets its twin)."""
    flagged = {label for label, e in ROUTES.items()
               if label.startswith("GET ") and isinstance(e.access, Permit) and e.access.writes_on_get}
    assert flagged == set(GETS_THAT_WRITE), sorted(flagged ^ set(GETS_THAT_WRITE))


@pytest.fixture(scope="module")
def mirror():
    return TestClient(identity_matrix.stub_app(app))


@pytest.fixture(scope="module")
def world(engine) -> IdentityWorld:
    return IdentityWorld.create(engine)


@pytest.fixture
def placed(monkeypatch, world):
    for kind in scope_module.registered_object_kinds():
        monkeypatch.setitem(scope_module._OBJECT_LOADERS, kind,  # noqa: SLF001
                            lambda db, oid: (world.org, world.project_a))


@pytest.fixture(scope="module")
def owner(world):
    person = world.person("owner")
    world.bind(person, "owner")
    return person


def _get(label, world, headers):
    path = label.split(" ", 1)[1]
    req = identity_matrix.request_for("GET", path, ROUTES[label].access, world.project_a)
    req["headers"] = headers
    return req


@pytest.mark.parametrize("mode", ["enforce", "shadow"])
def test_a_cookie_get_to_a_writing_route_needs_same_origin_or_the_csrf_token(mirror, world, owner,
                                                                            placed, mode):
    ctx = world.ctx(owner)                      # a session (cookie) principal
    token = csrf_token(ctx.session.id)
    wrong = []
    with world.acting_as(ctx, mode=mode):
        for label in WRITING:
            bare = mirror.request(**_get(label, world, {}))
            cross = mirror.request(**_get(label, world, {"sec-fetch-site": "cross-site"}))
            forged = mirror.request(**_get(label, world, {"x-boss-csrf": "nope"}))
            same = mirror.request(**_get(label, world, {"sec-fetch-site": "same-origin"}))
            with_token = mirror.request(**_get(label, world, {"x-boss-csrf": token}))
            for name, got in (("bare", bare), ("cross-site", cross), ("forged token", forged)):
                if got.status_code != 403 or got.json() != {"error": "csrf_failed"}:
                    wrong.append(f"{label} {name}: {got.status_code} {got.text[:80]}")
            for name, got in (("same-origin", same), ("token", with_token)):
                if got.status_code != 200:
                    wrong.append(f"{label} {name}: {got.status_code} {got.text[:80]}")
    assert not wrong, "\n".join(wrong)


def test_a_bearer_get_and_an_ordinary_get_are_not_affected(mirror, world, owner, placed):
    """CSRF is a cookie problem: a key needs no token; a GET that writes nothing needs none."""
    label = "GET /v1/status"
    key = world.key_ctx(owner, ("system:read", "project:read"))
    assert key.method == AuthMethod.API_KEY
    with world.acting_as(key):
        assert mirror.request(**_get(label, world, {})).status_code == 200
    with world.acting_as(world.ctx(owner)):
        plain = identity_matrix.request_for("GET", "/v1/code-tasks", ROUTES["GET /v1/code-tasks"].access,
                                            world.project_a)
        plain["headers"] = {}
        assert mirror.request(**plain).status_code == 200


def test_the_post_twin_answers_with_the_same_declaration(mirror, world, owner, placed):
    with world.acting_as(world.ctx(owner)):
        for label in WRITING:
            path = label.split(" ", 1)[1]
            twin = identity_guard.writing_get_twin(path)
            req = identity_matrix.request_for("POST", twin, ROUTES[label].access, world.project_a)
            got = mirror.request(**req)
            assert got.status_code == 200, (label, got.status_code, got.text[:120])
    viewer = world.person("viewer-no-system")
    world.bind(viewer, "viewer", project=world.project_b)
    with world.acting_as(world.ctx(viewer)):
        req = identity_matrix.request_for("POST", "/v1/status/load", ROUTES["GET /v1/status"].access,
                                          world.project_a)
        assert mirror.request(**req).status_code == 403
