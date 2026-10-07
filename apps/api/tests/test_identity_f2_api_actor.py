"""F2 / H7: under enforce the actor is ALWAYS the verified principal (security test, live IDOR).

Hundreds of routes take a client-supplied `actor`. Under enforce the legacy actor checks returned
early, so the claimed string reached handlers unchecked: the security tester read and listed another
person's agent thread by naming them as the actor. The rule is now central (identity_guard.
_bind_actor): on every enforced request the claimed actor is replaced, in the body FastAPI validates
from and in the query string, by the principal's own actor — and agent threads check ownership.

  * the SWEEP: every route that takes `actor` (body field or query parameter) sees the principal's
    actor, whatever the request claimed;
  * the live IDOR (the tester's harness_idor, turned into a test): a person cannot read, list or
    clear another person's private thread by naming them;
  * the worker keeps the persona its job names (the one exception).
"""
from __future__ import annotations

import dataclasses
import uuid

import pytest
from fastapi import Depends, FastAPI, Request
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient
from sqlalchemy import text

from apps.api.app import identity_guard
from apps.api.app.main import app
from apps.api.tests import identity_matrix
from apps.api.tests.identity_fixtures import IdentityWorld
from boss_engine.identity import scope as scope_module
from boss_engine.identity.declarations import ACCESS_ATTR, access_of
from boss_engine.identity.principal import Principal, PrincipalKind, PrincipalStatus

FORGED = "someone_else_entirely"


def _actor_routes():
    for route in app.router.routes:
        if not isinstance(route, APIRoute):
            continue
        body_field, query_param = identity_guard._route_takes_actor(route)  # noqa: SLF001
        if body_field or query_param:
            for method in sorted(m for m in (route.methods or ()) if m != "HEAD"):
                yield method, route.path, route, body_field, query_param


ACTOR_ROUTES = list(_actor_routes())


def _echo_mirror() -> FastAPI:
    """Every actor-taking route, same path, method and declaration, behind the same guard, with a
    handler that answers the actor it would have been given."""
    mirror = FastAPI(dependencies=[Depends(identity_guard.enforce_access)], openapi_url=None,
                     docs_url=None, redoc_url=None)
    identity_guard.install(mirror)
    for _method, path, route, _b, _q in ACTOR_ROUTES:
        async def echo(request: Request) -> dict:
            try:
                body = await request.json()
            except Exception:  # noqa: BLE001
                body = None
            return {"query": request.query_params.get("actor"),
                    "body": body.get("actor") if isinstance(body, dict) else None}

        echo.__module__ = route.endpoint.__module__
        setattr(echo, ACCESS_ATTR, access_of(route.endpoint))
        mirror.add_api_route(path, echo, methods=sorted(m for m in (route.methods or ())
                                                         if m != "HEAD"))
    return mirror


@pytest.fixture(scope="module")
def world(engine) -> IdentityWorld:
    w = IdentityWorld.create(engine)
    w.claimed()
    return w


@pytest.fixture(scope="module")
def owner(world):
    person = world.person("f2-owner")
    world.bind(person, "owner")
    return person


@pytest.fixture
def placed(monkeypatch, world):
    """Every object a route names is in project A of this org."""
    for kind in scope_module.registered_object_kinds():
        monkeypatch.setitem(scope_module._OBJECT_LOADERS, kind,  # noqa: SLF001
                            lambda db, oid: (world.org, world.project_a))


def test_the_sweep_covers_hundreds_of_routes():
    assert len(ACTOR_ROUTES) > 400, len(ACTOR_ROUTES)


def test_every_route_taking_an_actor_sees_the_principal_not_the_claim(world, owner, placed):
    """THE SWEEP. A forged actor in the query and/or the body is replaced before the handler."""
    mirror = TestClient(_echo_mirror())
    expected = identity_guard.actor_for(world.ctx(owner))
    seen, refused, wrong = 0, [], []
    with world.acting_as(world.ctx(owner)):
        for method, path, route, body_field, query_param in ACTOR_ROUTES:
            req = identity_matrix.request_for(method, path, access_of(route.endpoint),
                                              world.project_a)
            if query_param:
                req["params"] = {**req["params"], "actor": FORGED}
            if body_field and method in ("POST", "PUT", "PATCH", "DELETE"):
                req["json"] = {**req.get("json", {}), "actor": FORGED}
            out = mirror.request(**req)
            if out.status_code != 200:
                refused.append((method, path, out.status_code))
                continue
            seen += 1
            got = out.json()
            if query_param and got["query"] != expected:
                wrong.append((method, path, "query", got["query"]))
            if body_field and "json" in req and got["body"] != expected:
                wrong.append((method, path, "body", got["body"]))
    assert not wrong, wrong[:20]
    # The Owner reaches nearly all of them; the rest are system-only (the worker protocol).
    assert seen >= len(ACTOR_ROUTES) * 0.8, (seen, refused[:20])
    assert all(status in (403,) for _m, _p, status in refused), refused[:20]


def test_the_worker_keeps_the_persona_its_job_names(world, placed):
    # sys_worker's fixed set holds worker:lease (no row needed: a system principal's set is fixed)
    worker = Principal("sys_worker", PrincipalKind.SYSTEM, world.org, PrincipalStatus.ACTIVE,
                       "BOSS worker")
    mirror = TestClient(_echo_mirror())
    with world.acting_as(world.ctx(worker)):
        out = mirror.post("/v1/meetings/step", json={"project_id": world.project_a,
                                                     "actor": "shepherd"})
    assert out.status_code == 200, out.text
    assert out.json()["body"] == "shepherd"


def test_a_legacy_bridge_acts_as_the_member_it_took_over(world, placed):
    """The console key's principal carries `executive_console`; its actor is that member."""
    claimant = world.person("f2-claimant")
    world.bind(claimant, "owner")
    with world.engine.begin() as db:
        db.execute(text("UPDATE identity_principals SET legacy_member_ids = ARRAY['ceo'] "
                        "WHERE id = :p"), {"p": claimant.id})
    principal = dataclasses.replace(claimant, legacy_member_ids=("ceo",))
    ctx = dataclasses.replace(world.ctx(claimant), principal=principal)
    mirror = TestClient(_echo_mirror())
    with world.acting_as(ctx):
        out = mirror.post("/v1/members", json={"project_id": world.project_a, "actor": FORGED},
                          headers={"sec-fetch-site": "same-origin"})
    assert out.status_code == 200, out.text
    assert out.json()["body"] == "ceo"


# ---- the live IDOR on agent threads (the tester's harness_idor, as a regression test) -----------

@pytest.fixture(scope="module")
def client():
    return TestClient(app, raise_server_exceptions=False)


@pytest.fixture
def pair(world):
    alice = world.person("f2-alice")
    bob = world.person("f2-bob")
    world.bind(alice, "operator", project=world.project_a)
    world.bind(bob, "operator", project=world.project_a)
    thread_id = f"thr_{uuid.uuid4().hex[:10]}"
    with world.engine.begin() as db:
        db.execute(text("INSERT INTO agent_threads (id, project_id, actor, surface) "
                        "VALUES (:i, :p, :a, 'chat')"),
                   {"i": thread_id, "p": world.project_a, "a": alice.id})
    return alice, bob, thread_id


def test_another_persons_thread_cannot_be_read_by_naming_them(client, world, pair):
    alice, bob, thread_id = pair
    params = {"project_id": world.project_a, "actor": alice.id}
    with world.acting_as(world.ctx(bob)):
        stolen = client.get(f"/v1/agent/threads/{thread_id}/messages", params=params)
        ledger = client.get(f"/v1/agent/threads/{thread_id}/ledger")
        progress = client.get(f"/v1/agent/threads/{thread_id}/progress")
    assert stolen.status_code == 404, stolen.text
    assert ledger.status_code == 404, ledger.text
    assert progress.status_code == 404, progress.text
    with world.acting_as(world.ctx(alice)):
        own = client.get(f"/v1/agent/threads/{thread_id}/messages", params=params)
        own_ledger = client.get(f"/v1/agent/threads/{thread_id}/ledger")
    assert own.status_code == 200, own.text
    assert own_ledger.status_code == 200, own_ledger.text


def test_another_persons_threads_are_not_listed_by_naming_them(client, world, pair):
    alice, bob, thread_id = pair
    params = {"project_id": world.project_a, "actor": alice.id, "since_days": 30}
    with world.acting_as(world.ctx(bob)):
        theirs = client.get("/v1/agent/threads", params=params)
    with world.acting_as(world.ctx(alice)):
        mine = client.get("/v1/agent/threads", params=params)
    assert theirs.status_code == 200 and mine.status_code == 200, (theirs.text, mine.text)
    assert thread_id not in str(theirs.json())
    assert thread_id in str(mine.json())


def test_another_persons_thread_cannot_be_cleared_by_naming_them(client, world, pair):
    alice, bob, thread_id = pair
    with world.acting_as(world.ctx(bob)):
        out = client.post(f"/v1/agent/threads/{thread_id}/clear",
                          params={"project_id": world.project_a, "actor": alice.id})
    assert out.status_code in (403, 404), out.text
    with world.engine.connect() as db:
        status = db.execute(text("SELECT status FROM agent_threads WHERE id = :i"),
                            {"i": thread_id}).scalar()
    assert status == "active"


def test_a_required_query_actor_is_filled_from_the_principal(client, world, pair):
    """`actor` is a required query parameter of the thread list; under enforce the caller no longer
    names it at all — the guard fills it before FastAPI validates the query."""
    alice, _bob, thread_id = pair
    with world.acting_as(world.ctx(alice)):
        out = client.get("/v1/agent/threads", params={"project_id": world.project_a,
                                                      "since_days": 30})
    assert out.status_code == 200, out.text
    assert thread_id in str(out.json())
