"""deployment:production on EVERY path that ships to a target (IDENTITY.md §6.2; identity int-b).

The decision comes from the TARGET's row (or the deployment run's, or the build's latest
deployment), never only from a caller-supplied environment string, and the check runs at the
target's project. A matrix over every deploying route and tool:

  * a Deployer (every deploy permission EXCEPT deployment:production) aimed at a production target
    is refused 403 naming deployment:production;
  * the same Deployer aimed at a staging target is allowed (the control: only the target differs);
  * a Production deployer (the same plus deployment:production) aimed at production is allowed.

Guard-level routes run on the stub mirror (identity_matrix.stub_app: the real enforcement point,
the real PDP, real target rows; stub handlers), so "allowed" means the access decision let it
through. The handler-level checks (an SSH deploy to a caller-named host, a delivery workflow's
target input) and identity_access.require_production's per-route mode are tested directly.
"""
from __future__ import annotations

import uuid
from dataclasses import replace

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from sqlalchemy import text

from apps.api.app import identity_guard
from apps.api.app.main import app
from apps.api.tests import identity_matrix
from apps.api.tests.identity_fixtures import IdentityWorld

DEPLOY_PERMISSIONS = ("project:read", "deployment:read", "deployment:create", "deployment_target:manage",
                      "workflow_run:read", "workflow_run:control", "workflow_run:start",
                      "code_task:read", "host:read")


@pytest.fixture(scope="module")
def mirror():
    return TestClient(identity_matrix.stub_app(app))


@pytest.fixture(scope="module")
def world(engine) -> IdentityWorld:
    return IdentityWorld.create(engine)


@pytest.fixture
def rows(world):
    """A production and a staging target in project A, a deployment run against each, and a
    build whose latest deployment is the production one. Per test: the suite's clean_tables
    wipes deployment rows before every test."""
    tag = uuid.uuid4().hex[:8]
    out = {"prod_target": f"tgt_p_{tag}", "stage_target": f"tgt_s_{tag}",
           "prod_run": f"dr_p_{tag}", "stage_run": f"dr_s_{tag}",
           "prod_build": f"crun_p_{tag}", "stage_build": f"crun_s_{tag}"}
    with world.engine.begin() as db:
        for env, target, run, build in (("production", out["prod_target"], out["prod_run"], out["prod_build"]),
                                        ("staging", out["stage_target"], out["stage_run"], out["stage_build"])):
            db.execute(text(
                "INSERT INTO deployment_targets (id, project_id, app_name, repo, environment, "
                "target_kind, target_ref, created_by) VALUES (:t, :p, :a, 'r', :e, 'ssh', 'h', 'test')"),
                {"t": target, "p": world.project_a, "a": f"app-{env}-{tag}", "e": env})
            db.execute(text("INSERT INTO code_task_runs (id, project_id, title) VALUES (:i, :p, 't')"),
                       {"i": build, "p": world.project_a})
            db.execute(text(
                "INSERT INTO deployment_runs (id, project_id, deployment_target_id, app_name, environment, "
                "repo, target_kind, target_ref, created_by, release_packet) VALUES "
                "(:r, :p, :t, 'a', :e, 'r', 'ssh', 'h', 'test', CAST(:rp AS jsonb))"),
                {"r": run, "p": world.project_a, "t": target, "e": env,
                 "rp": '{"code_task_run_id": "%s"}' % build})
    return out


@pytest.fixture(scope="module")
def people(world):
    deployer, prod = world.person("deployer"), world.person("prod-deployer")
    world.bind(deployer, world.custom_role("deployer", DEPLOY_PERMISSIONS), project=world.project_a)
    world.bind(prod, world.custom_role("prod-deployer", DEPLOY_PERMISSIONS + ("deployment:production",)),
               project=world.project_a)
    return {"deployer": deployer, "prod": prod}


def _post(url: str, body: dict) -> dict:
    return {"method": "POST", "url": url, "json": body}


#: every guard-level deploying route: (label, request builder(kind)) where kind is "prod"/"stage"
#: and the request reaches the target by the route's own means (body/path target, run, or env).
def routes(world, rows):
    a = world.project_a

    def target(k):
        return rows[f"{k}_target"]

    def run(k):
        return rows[f"{k}_run"]

    def env(k):
        return "production" if k == "prod" else "staging"

    return {
        "POST /v1/deployment-runs": lambda k: _post(
            "/v1/deployment-runs", {"project_id": a, "deployment_target_id": target(k)}),
        "POST /v1/deployment-runs/{id}/rollout/start": lambda k: _post(
            f"/v1/deployment-runs/{run(k)}/rollout/start", {}),
        "POST /v1/deployment-runs/{id}/rollout/advance": lambda k: _post(
            f"/v1/deployment-runs/{run(k)}/rollout/advance", {}),
        "POST /v1/delivery/deployments/{id}/redeploy": lambda k: _post(
            f"/v1/delivery/deployments/{run(k)}/redeploy", {}),
        "POST /v1/delivery/deployments/{id}/execute": lambda k: _post(
            f"/v1/delivery/deployments/{run(k)}/execute", {}),
        "POST /v1/software-factory/deployments": lambda k: _post(
            "/v1/software-factory/deployments", {"project_id": a, "environment": env(k)}),
        "POST /v1/software-factory/deployments/queue": lambda k: _post(
            "/v1/software-factory/deployments/queue", {"project_id": a, "environment": env(k)}),
        "POST /v1/delivery/first-deploy (target)": lambda k: _post(
            "/v1/delivery/first-deploy", {"project_id": a, "run_id": "crun_x", "target_id": target(k)}),
        "POST /v1/delivery/first-deploy (environment)": lambda k: _post(
            "/v1/delivery/first-deploy", {"project_id": a, "run_id": "crun_x", "environment": env(k)}),
        "POST /v1/delivery/deploy": lambda k: _post("/v1/delivery/deploy", {"target_id": target(k)}),
        "POST /v1/infra/targets/{id}/deploy": lambda k: _post(
            f"/v1/infra/targets/{target(k)}/deploy", {"project_id": a}),
        "POST /v1/delivery-workflows/runs/{id}/deploy-target": lambda k: _post(
            "/v1/delivery-workflows/runs/wfr_none/deploy-target",
            {"project_id": a, "host_id": "dh_none", "environment": env(k)}),
        "POST /v1/deployments/stack/plan": lambda k: _post(
            "/v1/deployments/stack/plan", {"project_id": a, "target_id": target(k), "ref": "main"}),
    }


def _is_production_refusal(response) -> bool:
    return response.status_code == 403 and "deployment:production" in response.text


def test_every_deploying_route_needs_deployment_production_for_a_production_target(
        mirror, world, rows, people):
    wrong = []
    for label, build in routes(world, rows).items():
        with world.acting_as(world.ctx(people["deployer"])):
            refused = mirror.request(**build("prod"))
            staging = mirror.request(**build("stage"))
        with world.acting_as(world.ctx(people["prod"])):
            allowed = mirror.request(**build("prod"))
        if not _is_production_refusal(refused):
            wrong.append(f"{label}: production as Deployer -> {refused.status_code} {refused.text[:100]}")
        if staging.status_code != 200:
            wrong.append(f"{label}: staging as Deployer -> {staging.status_code} {staging.text[:100]}")
        if allowed.status_code != 200:
            wrong.append(f"{label}: production as Production deployer -> {allowed.status_code} "
                         f"{allowed.text[:100]}")
    assert not wrong, "\n".join(wrong)


def test_the_environment_string_cannot_launder_a_production_target(mirror, world, rows, people):
    """first-deploy names a production TARGET and says environment=staging: the target decides."""
    with world.acting_as(world.ctx(people["deployer"])):
        out = mirror.post("/v1/delivery/first-deploy", json={
            "project_id": world.project_a, "run_id": "crun_x", "target_id": rows["prod_target"],
            "environment": "staging"})
    assert _is_production_refusal(out), out.text


def test_every_deployment_create_route_says_where_its_target_comes_from():
    """The class: a route that creates a deployment declares production= (or is listed here with
    the reason it cannot ship to a production target)."""
    from boss_engine.identity.declarations import Permit

    no_target = {
        "POST /v1/deployment-runs/{deployment_run_id}/health-check": "checks a run; ships nothing",
        "POST /v1/code-tasks/{run_id}/deploy": "a caller-named SSH host: the handler treats every "
                                               "ssh deploy as production (require_production always)",
        "POST /v1/change-sets/{cs_id}/deploy": "Deploy all ships each component to its own target: "
                                               "the handler treats it as production (require_production "
                                               "always)",
    }
    wrong = []
    for entry in identity_guard.route_inventory(app):
        access = entry.access
        if not (isinstance(access, Permit) and access.permission == "deployment:create"):
            continue
        for label in entry.labels():
            if label.startswith("GET "):
                continue
            if access.production is None and label not in no_target:
                wrong.append(label)
    assert not wrong, wrong


# ---- the tools ------------------------------------------------------------------------------------

@pytest.fixture
def tools():
    from boss_engine.tools import build_default_registry
    from boss_engine.tools.registry import ToolRegistry

    real, reg, calls = build_default_registry(), ToolRegistry(), []
    for name in ("deploy_app", "redeploy"):
        reg.register(replace(real.get(name),
                             handler=lambda ctx, n=name, **kw: calls.append(n) or {"ran": n}))
    return reg, calls


def test_the_deploy_tools_need_deployment_production_for_a_production_target(world, rows, people, tools):
    reg, calls = tools
    a = world.project_a
    cases = {
        "deploy_app (target)": ("deploy_app", lambda k: {"run_id": rows[f"{k}_build"],
                                                          "target_id": rows[f"{k}_target"],
                                                          "environment": "staging"}),
        "deploy_app (environment)": ("deploy_app", lambda k: {"run_id": rows[f"{k}_build"],
                                                               "environment": "production" if k == "prod"
                                                               else "staging"}),
        "redeploy": ("redeploy", lambda k: {"run_id": rows[f"{k}_build"]}),
    }
    wrong = []
    for label, (tool, args) in cases.items():
        deployer, prod = world.ctx(people["deployer"]), world.ctx(people["prod"])
        with world.acting_as(deployer):
            refused = reg.invoke(tool, None, project_id=a, actor="x", args=args("prod"), auth=deployer)
            staging = reg.invoke(tool, None, project_id=a, actor="x", args=args("stage"), auth=deployer)
        with world.acting_as(prod):
            allowed = reg.invoke(tool, None, project_id=a, actor="x", args=args("prod"), auth=prod)
        if refused.get("permission") != "deployment:production" or refused.get("ok") is not False:
            wrong.append(f"{label}: production as Deployer -> {refused}")
        if staging.get("ran") != tool:
            wrong.append(f"{label}: staging as Deployer -> {staging}")
        if allowed.get("ran") != tool:
            wrong.append(f"{label}: production as Production deployer -> {allowed}")
    assert not wrong, "\n".join(map(str, wrong))


# ---- handler-side: require_production and its users -------------------------------------------------

def test_an_ssh_deploy_to_a_caller_named_host_counts_as_production(world, rows, people):
    client = TestClient(app, raise_server_exceptions=False)
    with world.acting_as(world.ctx(people["deployer"])):
        refused = client.post(f"/v1/code-tasks/{rows['stage_build']}/deploy",
                              json={"actor": "x", "mode": "ssh", "host": "10.0.0.9"},
                              headers={"sec-fetch-site": "same-origin"})
        dry = client.post(f"/v1/code-tasks/{rows['stage_build']}/deploy",
                          json={"actor": "x", "mode": "dry_run"})
    assert _is_production_refusal(refused), refused.text
    assert not _is_production_refusal(dry)


def test_a_workflow_runs_production_target_input_needs_deployment_production(world, rows, people):
    from apps.api.app import main

    def attempt(person, run_input):
        ctx = world.ctx(person)
        tokens = (identity_guard.IDENTITY_MODE.set("enforce"), identity_guard.REQUEST_IDENTITY.set(ctx),
                  identity_guard.IDENTITY_ENFORCED.set(True))
        try:
            with world.engine.begin() as db, world.acting_as(ctx):
                main._authorize_delivery_target_input(db, world.project_a, "x", run_input)
            return None
        except HTTPException as exc:
            return exc
        finally:
            identity_guard.IDENTITY_ENFORCED.reset(tokens[2])
            identity_guard.REQUEST_IDENTITY.reset(tokens[1])
            identity_guard.IDENTITY_MODE.reset(tokens[0])

    refused = attempt(people["deployer"], {"target_id": rows["prod_target"]})
    by_env = attempt(people["deployer"], {"environment": "production"})
    assert refused is not None and refused.status_code == 403 and "deployment:production" in str(refused.detail)
    assert by_env is not None and by_env.status_code == 403
    assert attempt(people["deployer"], {"target_id": rows["stage_target"]}) is None
    assert attempt(people["prod"], {"target_id": rows["prod_target"]}) is None


def test_require_production_follows_the_routes_own_mode_not_the_global_one(world, people):
    """The router is enforced while BOSS_AUTHZ_MODE stays shadow: the per-route mode decides."""
    from apps.api.app.routers.identity_access import require_production

    ctx = world.ctx(people["deployer"])

    class _Req:
        def __init__(self, mode):
            self.state = type("S", (), {"identity_mode": mode, "identity": ctx})()
            self.scope = {"path": "/x"}

    with world.acting_as(ctx, mode="shadow"):
        with pytest.raises(HTTPException) as refused:
            require_production(_Req("enforce"), world.project_a, "production")
        assert refused.value.status_code == 403
        require_production(_Req("shadow"), world.project_a, "production")     # recorded, not refused
        require_production(_Req("off"), world.project_a, "production")
        require_production(_Req("enforce"), world.project_a, "staging")       # not production
