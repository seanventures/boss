"""A host deploy of an app built from an `insecure_by_design` template is refused
(APP_TEMPLATES_AND_SECURITY_LOOP.md §3.6 guardrail; §3.8 "host deploy of an insecure_by_design app
is refused (API test)").

templates.deploy_guard.deploy_refusal is the one check; every deploy planner calls it before it
records anything, and the governed host apply calls it again. PER APP (operator decision
2026-10-09): the project's other apps deploy; a deploy BOSS cannot attribute to a non-insecure app
is refused (fail closed); a lab project (templates.lab) deploys nothing, whatever the target is called.
"""
from __future__ import annotations

import inspect
import json
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


RUNS: list[str] = []


def _app_run(c, title: str, repo: str) -> str:
    """A finished build of an app called `title` in HOME (apps are grouped from their builds)."""
    rid = f"crun_dg{uuid.uuid4().hex[:10]}"
    c.execute(text("INSERT INTO code_task_runs (id, project_id, title, status, spec) "
                   "VALUES (:i, :p, :t, 'completed', CAST(:s AS JSONB))"),
              {"i": rid, "p": HOME, "t": title, "s": json.dumps({"owner": "acme", "repo": repo})})
    RUNS.append(rid)
    return rid


def _apply(c, *, labels: list[str], status: str = "pr_open", key: str = "banking-insecure",
           app_key: str = "bank lab") -> str:
    tid = f"tpl_{uuid.uuid4().hex[:10]}"
    c.execute(text("INSERT INTO app_templates (id, org_id, template_key, version, source, manifest, status) "
                   "VALUES (:i, :o, :k, '1.0.0', 'builtin', CAST(:m AS JSONB), 'active')"),
              {"i": tid, "o": _org(c), "k": f"{key}-{tid[-4:]}", "m": json.dumps({"id": key, "labels": labels})})
    c.execute(text("INSERT INTO app_template_applications (id, project_id, app_key, template_id, version, "
                   "stack, status) VALUES (:i, :p, :a, :t, '1.0.0', 'java', :s)"),
              {"i": f"tapp_{uuid.uuid4().hex[:10]}", "p": HOME, "a": app_key, "t": tid, "s": status})
    return tid


@pytest.fixture()
def world(engine):
    seed(engine, HOME, samples=False)
    with engine.begin() as c:      # start from a project with no template applied (other suites apply some)
        c.execute(text("DELETE FROM app_template_applications WHERE project_id = :p"), {"p": HOME})
    yield engine
    with engine.begin() as c:
        c.execute(text("DELETE FROM app_template_applications WHERE project_id = :p"), {"p": HOME})
        c.execute(text("DELETE FROM code_task_runs WHERE id = ANY(:r)"), {"r": RUNS})
        RUNS.clear()
        c.execute(text("DELETE FROM app_templates WHERE template_key LIKE 'banking-insecure-%' "
                       "OR template_key LIKE 'hello-%'"))
        clear(c, HOME)


def test_the_rule_without_a_target_is_the_whole_project(world):
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
    with world.begin() as c:     # the target's own app (t_live = invoice, t_stack = shop) is the insecure one
        target_app = "invoice" if "delivery" in path else "shop"
        _app_run(c, target_app.title(), target_app)
        _apply(c, labels=["insecure_by_design"], app_key=target_app)
        before = c.execute(text("SELECT count(*) FROM deployment_runs WHERE project_id = :p"), {"p": HOME}).scalar()
    r = client.post(path, json=body)
    assert r.status_code in (409, 422), r.text
    assert "insecure by design" in r.text and "never deploys it to a host" in r.text
    with world.begin() as c:
        after = c.execute(text("SELECT count(*) FROM deployment_runs WHERE project_id = :p"), {"p": HOME}).scalar()
    assert after == before                                                  # nothing was recorded


def test_the_registry_planner_refuses_it_too(world):
    from boss_engine.services.resource_plane import ResourcePlaneService

    with world.begin() as c:    # t_cloud deploys "billing", the insecure app
        _app_run(c, "Billing", "billing")
        _apply(c, labels=["insecure_by_design"], app_key="billing")
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
        call = src[src.index("deploy_refusal("):src.index("deploy_refusal(") + 300]
        assert "target=" in call and "commit_ref=" in call, f"{fn.__qualname__} must say what it deploys"
    apply_src = inspect.getsource(governed_host_deploy._apply_on_host)  # noqa: SLF001
    assert apply_src.index("deploy_refusal(") < apply_src.index("conn.apply(")
    call = apply_src[apply_src.index("deploy_refusal("):apply_src.index("deploy_refusal(") + 300]
    assert "target=" in call and "commit_ref=" in call and "release_packet=" in call


# ---- per app (operator decision 2026-10-09) ---------------------------------------------------------

def _target(app: str, repo: str) -> dict:
    return {"id": "t_x", "app_name": app, "repo": f"acme/{repo}"}


@pytest.fixture()
def lab(world):
    """A lab project: the insecure Bank lab (its own repository) and an ordinary Invoice app."""
    with world.begin() as c:
        lab_run = _app_run(c, "Bank lab", "bank-lab")
        invoice_run = _app_run(c, "Invoice", "invoice")
        _apply(c, labels=["insecure_by_design"], app_key="bank lab")
    return world, lab_run, invoice_run


def test_the_projects_other_apps_deploy(lab):
    world, _lab_run, invoice_run = lab
    with world.begin() as c:
        assert deploy_guard.deploy_refusal(c, HOME, target=_target("Invoice", "invoice"), commit_ref="main") is None
        assert deploy_guard.deploy_refusal(c, HOME, target=_target("invoice", "invoice"), commit_ref="a1b2c3d",
                                           release_packet={"code_task_run_id": invoice_run}) is None
        assert deploy_guard.deploy_refusal(c, HOME, target=_target(invoice_run, "invoice"),
                                           commit_ref=f"boss/code/{invoice_run}") is None


@pytest.mark.parametrize("how", ["target_name", "target_name_only", "target_run_id", "source_build", "build_branch", "stack_run"])
def test_the_insecure_app_is_refused_by_every_name(lab, how):
    world, lab_run, invoice_run = lab
    kw = {"target": _target("Invoice", "invoice"), "commit_ref": "main"}
    if how == "target_name":
        kw["target"] = _target("Bank lab", "bank-lab")
    elif how == "target_name_only":   # named after it, its builds unknown to BOSS
        kw["target"] = _target("BANK LAB", "elsewhere")
        kw["release_packet"] = {"code_task_run_id": invoice_run}
    elif how == "target_run_id":
        kw["target"] = _target(lab_run, "bank-lab")
    elif how == "source_build":     # a target named after the ordinary app, deploying the insecure build
        kw["release_packet"] = {"code_task_run_id": lab_run}
    elif how == "build_branch":
        kw["commit_ref"] = f"boss/code/{lab_run}"
    else:
        kw["source_run_id"] = lab_run
    with world.begin() as c:
        refused = deploy_guard.deploy_refusal(c, HOME, **kw)
    assert refused and "bank lab was built from banking-insecure" in refused
    assert "never deploys it to a host" in refused and "preview it, never deploy it" in refused.action


@pytest.mark.parametrize("kw,says", [
    ({"target": _target("Mystery service", "invoice")}, "which app target 'Mystery service' deploys"),
    ({"target": _target("Invoice", "invoice"), "release_packet": {"code_task_run_id": "crun_nobody"}},
     "which app build crun_nobody belongs to"),
    ({"target": _target("Invoice", "invoice"), "commit_ref": "boss/code/crun_nobody"},
     "which app branch boss/code/crun_nobody belongs to"),
    ({"target": _target("Invoice", "invoice"), "release_packet": "{not json"}, "could not check"),
])
def test_what_boss_cannot_attribute_is_refused(lab, kw, says):
    world, *_ = lab
    with world.begin() as c:
        refused = deploy_guard.deploy_refusal(c, HOME, **kw)
    assert refused and says in refused


def test_a_repository_shared_with_the_insecure_app(lab):
    """The ordinary app deploys from the insecure app's repository only with a ref BOSS can place
    and while the insecure change is not merged there."""
    world, lab_run, invoice_run = lab
    shared = _target("Invoice", "bank-lab")
    with world.begin() as c:
        assert deploy_guard.deploy_refusal(c, HOME, target=shared, commit_ref="main") is None
        assert deploy_guard.deploy_refusal(c, HOME, target=shared, commit_ref=f"boss/code/{invoice_run}") is None
        sha = deploy_guard.deploy_refusal(c, HOME, target=shared, commit_ref="0123abcd4567")
        assert sha and "cannot place 0123abcd4567" in sha and "own build branch" in sha.action
        c.execute(text("UPDATE app_template_applications SET status = 'applied' WHERE project_id = :p"),
                  {"p": HOME})
        merged = deploy_guard.deploy_refusal(c, HOME, target=shared, commit_ref="main")
        assert merged and "was merged into acme/bank-lab" in merged
        # its own repository still deploys: the merge happened elsewhere
        assert deploy_guard.deploy_refusal(c, HOME, target=_target("Invoice", "invoice"), commit_ref="main") is None


def test_an_insecure_app_whose_repository_is_unknown_is_assumed_to_share_it(world):
    with world.begin() as c:
        _app_run(c, "Invoice", "invoice")
        _apply(c, labels=["insecure_by_design"], app_key="ghost lab")      # no build BOSS can read
        assert deploy_guard.deploy_refusal(c, HOME, target=_target("Invoice", "invoice"), commit_ref="main") is None
        refused = deploy_guard.deploy_refusal(c, HOME, target=_target("Invoice", "invoice"), commit_ref="feature/x")
    assert refused and "cannot place feature/x" in refused


def test_the_host_route_deploys_the_other_app(client, world, monkeypatch):
    """Through POST /v1/delivery/deploy: t_live deploys Invoice while the project holds Bank lab."""
    monkeypatch.setattr(api_main.settings, "boss_edition", "community")
    with world.begin() as c:
        _app_run(c, "Invoice", "invoice")
        _app_run(c, "Bank lab", "bank-lab")
        _apply(c, labels=["insecure_by_design"], app_key="bank lab")
    r = client.post("/v1/delivery/deploy", json={"target_id": "t_live", "actor": "ceo", "ref": "main"})
    assert "insecure by design" not in r.text and "cannot tell" not in r.text
    # past the guard (both planners): the seeded target then stops at the next ordinary check
    assert r.status_code < 400 or "has no app port" in r.text, r.text


def test_a_lab_project_deploys_nothing_whatever_the_target_is_called(world):
    """templates.lab: an insecure template is applied as a new app in a project made for it; that
    project deploys nothing, even a target BOSS could attribute to another app (from fx-secbackend)."""
    with world.begin() as c:
        c.execute(text("INSERT INTO project_settings (project_id, key, value) VALUES (:p, 'template_lab', "
                       "CAST(:v AS JSONB)) ON CONFLICT (project_id, key) DO UPDATE SET value = EXCLUDED.value"),
                  {"p": HOME, "v": json.dumps({"insecure_by_design": True, "app_name": "Training bank",
                                               "template_key": "banking-starter-insecure"})})
        try:
            for target in (None, {"app_name": "anything-else", "repo": "o/r"}):
                refused = deploy_guard.deploy_refusal(c, HOME, target=target, commit_ref="main")
                assert refused and "lab project" in refused and "Training bank" in refused
        finally:
            c.execute(text("DELETE FROM project_settings WHERE project_id = :p AND key = 'template_lab'"),
                      {"p": HOME})
