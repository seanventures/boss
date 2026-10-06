"""System-only routes are the worker's, never a person's (IDENTITY.md §3, §6.2; int-b item 7).

  * every route the review found humans calling is declared with a system-only permission
    (worker:lease, spend:reserve, secret:lease): REQUIRED pins them;
  * a person — even an Owner, who holds everything a human can — is refused on each, and the
    worker's principal (sys_worker) is let through, by the real enforcement point;
  * every worker-protocol handler calls require_worker_principal() itself, and that check verifies
    the request's principal (it never trusts the route's declaration alone);
  * the console's one system-only call (reserving comms spend before queueing a message) moved
    server-side: /v1/comms/outbox/{id}/queue reserves for a person who may send.
"""
from __future__ import annotations

import inspect

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from apps.api.app import identity_guard
from apps.api.app.main import app
from apps.api.tests import identity_matrix
from apps.api.tests.identity_fixtures import IdentityWorld
from boss_engine.identity import scope as scope_module
from boss_engine.identity.declarations import Permit
from boss_engine.identity.permissions_gen import PERMISSION_META
from boss_engine.identity.principal import AuthContext, AuthMethod, Principal, PrincipalKind, PrincipalStatus

REQUIRED = {
    "POST /v1/budget/reserve", "POST /v1/budget/consume", "POST /v1/budget/release",
    "POST /v1/kms/keys/{key_entry_id}/lease", "POST /v1/kms/leases/{lease_id}/resolve",
    "POST /v1/workflow-runs/{run_id}/steps/claim", "POST /v1/workflow-step-runs/{step_run_id}/heartbeat",
    "POST /v1/workflow-step-runs/{step_run_id}/complete", "POST /v1/workflow-step-runs/{step_run_id}/fail",
    "POST /v1/workflow-runs/{run_id}/compensations/claim", "POST /v1/a2a/poll", "POST /v1/telemetry/tick",
    "POST /v1/self/host-stall", "POST /v1/deployments/stack/{deployment_id}/execute",
    "POST /v1/delivery/deployments/{deployment_run_id}/apply",
    "POST /v1/github/code-sync-runs/{sync_run_id}/apply", "POST /v1/github/resolution-plans/{plan_id}/apply",
    "POST /v1/github/merge-authorizations/{authorization_id}/apply",
    "POST /v1/elastic/instances/{instance_id}/heartbeat", "POST /v1/activity/retention/prune",
    "POST /v1/orchestration/next-move/execute",
}

#: Worker-protocol routes whose handler receives the check under another name.
CHECK_NAMES = ("require_worker_principal(", "require_worker()")


def _system_only():
    out = {}
    for entry in identity_guard.route_inventory(app):
        access = entry.access
        if isinstance(access, Permit) and PERMISSION_META[access.permission].system_only \
                and not access.permission.startswith("scim:"):
            for label in entry.labels():
                out[label] = entry
    return out


SYSTEM_ONLY = _system_only()


def test_the_routes_people_called_are_system_only():
    assert REQUIRED <= set(SYSTEM_ONLY), sorted(REQUIRED - set(SYSTEM_ONLY))


def test_every_worker_protocol_handler_checks_the_worker_principal():
    wrong = []
    for label, entry in SYSTEM_ONLY.items():
        if entry.access.permission != "worker:lease":
            continue   # spend:reserve / secret:lease are delegable: an agent run may hold them
        route = next(r for r in app.routes if getattr(r, "path", None) == label.split(" ", 1)[1]
                     and getattr(r, "endpoint", None) is not None
                     and label.split(" ", 1)[0] in getattr(r, "methods", ()))
        source = inspect.getsource(route.endpoint)
        if not any(name in source for name in CHECK_NAMES):
            wrong.append(label)
    assert not wrong, wrong


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


def _worker(world) -> AuthContext:
    return AuthContext(Principal("sys_worker", PrincipalKind.SYSTEM, world.org, PrincipalStatus.ACTIVE,
                                 "worker"), AuthMethod.SYSTEM)


def test_a_person_is_refused_and_the_worker_is_allowed_on_every_system_only_route(mirror, world, placed):
    owner, operator = world.person("owner"), world.person("operator")
    world.bind(owner, "owner")
    world.bind(operator, "operator", project=world.project_a)
    wrong = []
    for label, entry in sorted(SYSTEM_ONLY.items()):
        method, _, path = label.partition(" ")
        req = identity_matrix.request_for(method, path, entry.access, world.project_a)
        for name, ctx in (("owner", world.ctx(owner)), ("operator", world.ctx(operator))):
            with world.acting_as(ctx):
                got = mirror.request(**req)
            if got.status_code != 403:
                wrong.append(f"{label} as {name}: {got.status_code}")
        with world.acting_as(_worker(world)):
            got = mirror.request(**req)
        if got.status_code != 200:
            wrong.append(f"{label} as the worker: {got.status_code} {got.text[:80]}")
    assert not wrong, "\n".join(wrong)


def _with_identity(ctx, *, enforced: bool):
    tokens = (identity_guard.REQUEST_IDENTITY.set(ctx), identity_guard.IDENTITY_ENFORCED.set(enforced))

    def reset():
        identity_guard.IDENTITY_ENFORCED.reset(tokens[1])
        identity_guard.REQUEST_IDENTITY.reset(tokens[0])
    return reset


@pytest.mark.parametrize("enforced", [True, False])
def test_require_worker_principal_checks_the_principal_itself(world, enforced, monkeypatch):
    """Never trusts the declaration: a person on an enforced route (a mis-declared one, say) is
    refused, and the worker passes; in shadow a person is refused when transport auth is on."""
    monkeypatch.setattr(identity_guard, "_legacy_worker_ok", lambda: False)
    person = world.person("someone")
    reset = _with_identity(world.ctx(person), enforced=enforced)
    try:
        with pytest.raises(HTTPException) as refused:
            identity_guard.require_worker_principal()
        assert refused.value.status_code == 403
    finally:
        reset()
    reset = _with_identity(_worker(world), enforced=enforced)
    try:
        identity_guard.require_worker_principal()
    finally:
        reset()


def test_an_agent_run_is_not_the_worker(world):
    """Even a run token carried by a system principal is an agent run, not the worker."""
    worker = _worker(world)
    as_run = AuthContext(worker.principal, AuthMethod.RUN_TOKEN, run_token=object())
    agent = AuthContext(Principal("agt_x", PrincipalKind.AGENT, world.org, PrincipalStatus.ACTIVE, "a"),
                        AuthMethod.RUN_TOKEN, run_token=object())
    assert identity_guard.is_worker_principal(as_run) is False
    assert identity_guard.is_worker_principal(agent) is False
    assert identity_guard.is_worker_principal(worker) is True
