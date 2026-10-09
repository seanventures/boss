"""A host deploy of an app built from an `insecure_by_design` template is refused
(APP_TEMPLATES_AND_SECURITY_LOOP.md §3.6 guardrail; §3.8 "host deploy of an insecure_by_design app
is refused (API test)").

templates.deploy_guard.deploy_refusal is the one check; every deploy planner calls it before it
records anything, and the governed host apply calls it again. Per APP (operator decision,
2026-10-09): the app the target names is refused, the project's other apps deploy; a lab project
(templates.lab) deploys nothing; a caller that cannot name the app is refused. Fail closed.
"""
from __future__ import annotations

import inspect
import json
import re
import uuid

import pytest
from sqlalchemy import text

from apps.api.app import main as api_main
from apps.api.tests.infra_seed import clear, seed
from boss_engine.templates import deploy_guard

HOME = "boss-dogfood"


def _org(c) -> str:
    return c.execute(text("SELECT w.org_id FROM projects p JOIN workspaces w ON w.id = p.workspace_id "
                          "WHERE p.id = :p"), {"p": HOME}).scalar()


def _apply(c, *, labels: list[str], status: str = "pr_open", key: str = "banking-insecure",
           app: str = "bank_lab") -> str:
    tid = f"tpl_{uuid.uuid4().hex[:10]}"
    c.execute(text("INSERT INTO app_templates (id, org_id, template_key, version, source, manifest, status) "
                   "VALUES (:i, :o, :k, '1.0.0', 'builtin', CAST(:m AS JSONB), 'active')"),
              {"i": tid, "o": _org(c), "k": f"{key}-{tid[-4:]}", "m": json.dumps({"id": key, "labels": labels})})
    c.execute(text("INSERT INTO app_template_applications (id, project_id, app_key, template_id, version, "
                   "stack, status) VALUES (:i, :p, :app, :t, '1.0.0', 'java', :s)"),
              {"i": f"tapp_{uuid.uuid4().hex[:10]}", "p": HOME, "t": tid, "s": status, "app": app})
    return tid


@pytest.fixture()
def world(engine):
    seed(engine, HOME, samples=False)
    with engine.begin() as c:      # start from a project with no template applied (other suites apply some)
        c.execute(text("DELETE FROM app_template_applications WHERE project_id = :p"), {"p": HOME})
    yield engine
    with engine.begin() as c:
        c.execute(text("DELETE FROM app_template_applications WHERE project_id = :p"), {"p": HOME})
        c.execute(text("DELETE FROM app_templates WHERE template_key LIKE 'banking-insecure-%' "
                       "OR template_key LIKE 'hello-%'"))
        clear(c, HOME)


def test_the_rule(world):
    with world.begin() as c:
        assert deploy_guard.deploy_refusal(c, HOME) is None
        _apply(c, labels=[], key="hello")
        assert deploy_guard.deploy_refusal(c, HOME) is None                 # an ordinary template
        _apply(c, labels=["insecure_by_design"], status="abandoned")
        assert deploy_guard.deploy_refusal(c, HOME) is None                 # abandoned: nothing built
        _apply(c, labels=["insecure_by_design"], status="failed")
        refused = deploy_guard.deploy_refusal(c, HOME)
        assert refused and "insecure by design" in refused and "never deploys it to a host" in refused
        assert deploy_guard.deploy_refusal(c, "another-project") is None
        # PER APP: the insecure app's own target is refused (by its name or its repository's),
        # another app of the same project deploys
        assert "never deploys" in deploy_guard.deploy_refusal(c, HOME, {"app_name": "Bank Lab", "repo": "o/x"})
        assert "never deploys" in deploy_guard.deploy_refusal(c, HOME, {"app_name": "web", "repo": "o/bank_lab"})
        assert deploy_guard.deploy_refusal(c, HOME, {"app_name": "invoice", "repo": "o/invoice"}) is None


def test_a_lab_project_deploys_nothing_whatever_the_target_is_called(world):
    import json as _json

    with world.begin() as c:
        c.execute(text("INSERT INTO project_settings (project_id, key, value) VALUES (:p, 'template_lab', "
                       "CAST(:v AS JSONB)) ON CONFLICT (project_id, key) DO UPDATE SET value = EXCLUDED.value"),
                  {"p": HOME, "v": _json.dumps({"insecure_by_design": True, "app_name": "Training bank",
                                                "template_key": "banking-starter-insecure"})})
        try:
            refused = deploy_guard.deploy_refusal(c, HOME, {"app_name": "anything-else", "repo": "o/r"})
            assert refused and "lab project" in refused and "Training bank" in refused
        finally:
            c.execute(text("DELETE FROM project_settings WHERE project_id = :p AND key = 'template_lab'"),
                      {"p": HOME})


def test_unknown_is_refused():
    class Broken:
        def begin_nested(self):
            raise RuntimeError("db down")

    assert "does not deploy" in deploy_guard.deploy_refusal(Broken(), HOME)


@pytest.mark.parametrize("path,body", [
    # an app on an SSH host: the governed host deploy (delivery -> plan_deployment_run -> host plan)
    ("/v1/delivery/deploy", {"target_id": "t_live", "actor": "ceo", "ref": "main"}),
    # a stack on a host: the Deploy button's stack route
    ("/v1/infra/targets/t_stack/deploy", {"project_id": HOME, "actor": "ceo"}),
])
def test_a_host_deploy_is_refused_in_words(client, world, monkeypatch, path, body):
    monkeypatch.setattr(api_main.settings, "boss_edition", "community")
    with world.begin() as c:
        app = c.execute(text("SELECT app_name FROM deployment_targets WHERE id = :t"),
                        {"t": "t_live" if "delivery" in path else "t_stack"}).scalar()
        _apply(c, labels=["insecure_by_design"], app=app)
        before = c.execute(text("SELECT count(*) FROM deployment_runs WHERE project_id = :p"), {"p": HOME}).scalar()
    r = client.post(path, json=body)
    assert r.status_code in (409, 422), r.text
    assert "insecure by design" in r.text and "never deploys it to a host" in r.text
    with world.begin() as c:
        after = c.execute(text("SELECT count(*) FROM deployment_runs WHERE project_id = :p"), {"p": HOME}).scalar()
    assert after == before                                                  # nothing was recorded


def test_the_registry_planner_refuses_it_too(world):
    from boss_engine.services.resource_plane import ResourcePlaneService

    with world.begin() as c:
        _apply(c, labels=["insecure_by_design"], app="billing")
    with world.begin() as c, pytest.raises(ValueError, match="insecure by design"):  # GovernedHostDeployError
        ResourcePlaneService(api_main.settings).plan_deployment_run(
            c, project_id=HOME, deployment_target_id="t_cloud", commit_ref="main", release_packet={},
            expected_cost_usd_micros=0, reservation_id=None, actor="ceo")


def test_every_deploy_planner_and_the_host_apply_ask_first():
    """THE CLASS: each planner checks before it records anything; the governed host apply checks
    again, so an approval made before the template was applied still cannot ship it."""
    from boss_engine.services import governed_host_deploy, resource_plane, stack_deploy

    for fn, first_write in ((resource_plane.ResourcePlaneService.plan_deployment_run, "INSERT INTO"),
                            (governed_host_deploy.plan_host_deployment, "reserve("),
                            (stack_deploy.plan_stack_deploy, "resolve_commit(")):
        src = inspect.getsource(fn)
        assert "deploy_refusal(" in src and src.index("deploy_refusal(") < src.index(first_write), fn.__qualname__
        # PER APP: every planner names the target it deploys
        assert re.search(r"deploy_refusal\(connection, project_id, target\)", src), fn.__qualname__
    apply_src = inspect.getsource(governed_host_deploy._apply_on_host)  # noqa: SLF001
    assert apply_src.index("deploy_refusal(") < apply_src.index("conn.apply(")
