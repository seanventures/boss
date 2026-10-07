"""A job's EFFECT is authorized by its kind (IDENTITY.md §6.3, §7.14; FX-B P0-6).

POST /v1/jobs is declared work_item:manage (an Operator holds it, and it is delegable), but the kind
decides what the job does: `infra.droplet.delete` terminates a server. The class fix is ONE map,
boss_engine.identity.job_permissions, checked by the same PDP at the job's project:

  * every kind a BOSS worker leases is mapped to a catalogue permission (no unmapped effect);
  * every route that enqueues a fixed kind declares that kind's permission, or checks it in-handler;
  * over HTTP, under enforce: an Operator cannot queue an infra/merge/host job through /v1/jobs, an
    Admin can, an unknown kind is refused, and an agent run cannot queue a non-delegable kind.
"""
from __future__ import annotations

import ast
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from apps.api.app.main import app
from apps.api.tests.identity_fixtures import IdentityWorld
from boss_engine.identity.job_permissions import JOB_KIND_PERMISSIONS, permission_for
from boss_engine.identity.permissions_gen import PERMISSION_META

ROOT = Path(__file__).resolve().parents[3]


def test_every_kind_a_worker_leases_is_mapped_to_a_catalogue_permission():
    from apps.worker.boss_worker.runner import capabilities

    unmapped = sorted(k for k in capabilities() if k not in JOB_KIND_PERMISSIONS)
    assert not unmapped, f"job kinds with no permission in job_permissions.py: {unmapped}"
    unknown = sorted(p for p in JOB_KIND_PERMISSIONS.values() if p not in PERMISSION_META)
    assert not unknown
    assert not [p for p in JOB_KIND_PERMISSIONS.values() if PERMISSION_META[p].system_only]


def test_an_effect_that_needs_more_than_work_item_manage_says_so():
    """The point of the map: the kinds that change the outside world are not work_item:manage."""
    for kind, needed in {"infra.droplet.create": "infra:provision", "infra.droplet.delete": "infra:terminate",
                         "github.pull_request.merge": "github:merge", "deployment.host_apply": "host:manage",
                         "investigation.run": "host:exec", "comms.outbox.deliver": "comms:send",
                         "webhook.outbound.deliver": "integration:manage"}.items():
        assert permission_for(kind) == needed, kind
    assert permission_for("no.such.kind") is None


def _routes_enqueueing_a_literal_kind():
    """(file, handler, kind, declared permission, checks the kind in-handler) for every route
    handler under apps/api/app that calls jobs.create_job with a literal kind."""
    out = []
    for path in (ROOT / "apps/api/app").rglob("*.py"):
        source = path.read_text(encoding="utf-8")
        if "create_job" not in source:
            continue
        for fn in ast.walk(ast.parse(source)):
            if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            permits = [d for d in fn.decorator_list if isinstance(d, ast.Call)
                       and ast.unparse(d.func).endswith("permit")]
            if not permits:
                continue
            declared = ast.unparse(permits[0].args[0]) if permits[0].args else ""
            checks = "_require_job_kind_permission" in ast.unparse(fn)
            for node in ast.walk(fn):
                if isinstance(node, ast.Call) and ast.unparse(node.func).endswith("create_job"):
                    for kw in node.keywords:
                        if kw.arg == "kind" and isinstance(kw.value, ast.Constant):
                            out.append((path.name, fn.name, kw.value.value, declared, checks))
    return out


def test_every_route_that_enqueues_a_fixed_kind_holds_that_kinds_permission():
    found = _routes_enqueueing_a_literal_kind()
    assert found, "the scan found no route enqueueing a job: it is broken"
    wrong = []
    for file, handler, kind, declared, checks in found:
        needed = permission_for(kind)
        if needed is None:
            wrong.append(f"{file}:{handler} enqueues {kind!r}, which job_permissions.py does not map")
            continue
        constant = "P." + needed.upper().replace(":", "_")
        if declared != constant and not checks:
            wrong.append(f"{file}:{handler} enqueues {kind!r} (needs {needed}) but declares {declared} "
                         "and does not call _require_job_kind_permission")
    assert not wrong, "\n".join(wrong)


# ---- over HTTP, under enforce ------------------------------------------------------------------------

@pytest.fixture(scope="module")
def world(engine) -> IdentityWorld:
    return IdentityWorld.create(engine)


@pytest.fixture(scope="module")
def people(world):
    operator, admin = world.person("job-operator"), world.person("job-admin")
    world.bind(operator, "operator", project=world.project_a)
    world.bind(admin, "admin", project=world.project_a)
    return {"operator": operator, "admin": admin}


def _queue(client, world, kind):
    return client.post("/v1/jobs", json={"project_id": world.project_a, "kind": kind, "payload": {}},
                       headers={"sec-fetch-site": "same-origin"})


@pytest.mark.parametrize("kind,permission", [
    ("infra.droplet.delete", "infra:terminate"),
    ("infra.droplet.create", "infra:provision"),
    ("deployment.host_apply", "host:manage"),
    ("investigation.run", "host:exec"),
    ("webhook.outbound.deliver", "integration:manage"),
])
def test_an_operator_cannot_queue_an_effect_beyond_work_item_manage(world, people, kind, permission):
    client = TestClient(app, raise_server_exceptions=False)
    with world.acting_as(world.ctx(people["operator"])):
        out = _queue(client, world, kind)
    assert out.status_code == 403, out.text
    assert permission in out.text


def test_an_admin_may_queue_the_same_effect(world, people):
    client = TestClient(app, raise_server_exceptions=False)
    with world.acting_as(world.ctx(people["admin"])):
        out = _queue(client, world, "infra.droplet.delete")
    assert out.status_code not in (401, 403), out.text


def test_an_operator_may_queue_a_plan(world, people):
    client = TestClient(app, raise_server_exceptions=False)
    with world.acting_as(world.ctx(people["operator"])):
        out = _queue(client, world, "plan.generate")
    assert out.status_code not in (401, 403, 422), out.text


def test_an_unknown_kind_is_refused_under_enforce(world, people):
    client = TestClient(app, raise_server_exceptions=False)
    with world.acting_as(world.ctx(people["admin"])):
        out = _queue(client, world, "made.up.kind")
    assert out.status_code == 422 and "unknown_job_kind" in out.text, out.text


def test_an_agent_run_cannot_queue_a_non_delegable_kind(world, people):
    """work_item:manage is delegable, so an agent may reach the route; infra:terminate is not, so
    the job-kind check refuses the run token even though its person holds it."""
    agent = world.agent("job-agent", ceiling=("work_item:manage", "project:read"),
                        sponsor=people["admin"])
    run = world.run_ctx(people["admin"], (agent,), grant_permissions=("work_item:manage", "project:read"),
                        token_scope=("work_item:manage", "project:read"), project_id=world.project_a)
    client = TestClient(app, raise_server_exceptions=False)
    with world.acting_as(run):
        out = _queue(client, world, "infra.droplet.delete")
    assert out.status_code == 403, out.text
