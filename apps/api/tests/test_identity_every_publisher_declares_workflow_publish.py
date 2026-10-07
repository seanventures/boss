"""Publishing a workflow version needs workflow:publish, on every path (IDENTITY.md §6.3; FX-B P0-6).

POST /v1/delivery-workflows/save and /fork were declared workflow:create (delegable, no step-up)
while calling publish_version — so an agent run could publish a workflow, and nobody stepped up.
The class: every route handler and every MCP tool whose code calls a version publisher declares
workflow:publish. A new publishing route fails here until it does.
"""
from __future__ import annotations

import ast
import inspect
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
#: the functions that write a new runnable workflow version
PUBLISHERS = ("publish_version", "publish_from_editor")
#: catalogue installs publish the catalogue's own reviewed spec: that is what workflow:install means
INSTALL_PERMISSION = "P.WORKFLOW_INSTALL"


def _route_publishers():
    out = []
    for path in (ROOT / "apps/api/app").rglob("*.py"):
        source = path.read_text(encoding="utf-8")
        if not any(p in source for p in PUBLISHERS):
            continue
        for fn in ast.walk(ast.parse(source)):
            if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            permits = [d for d in fn.decorator_list if isinstance(d, ast.Call)
                       and ast.unparse(d.func).endswith("permit")]
            if not permits:
                continue
            calls = {ast.unparse(n.func).rsplit(".", 1)[-1] for n in ast.walk(fn) if isinstance(n, ast.Call)}
            if calls & set(PUBLISHERS):
                out.append((f"{path.name}:{fn.name}", ast.unparse(permits[0].args[0])))
    return out


def test_every_route_that_publishes_a_workflow_version_declares_workflow_publish():
    found = _route_publishers()
    names = {name for name, _ in found}
    # the scan must see the routes this rule was written for, or it is not looking
    assert {"main.py:save_delivery_workflow", "main.py:fork_delivery_workflow",
            "main.py:publish_delivery_workflow"} <= names, names
    wrong = [f"{name} declares {declared}" for name, declared in found
             if declared not in ("P.WORKFLOW_PUBLISH", INSTALL_PERMISSION)]
    assert not wrong, "\n".join(wrong)


def test_every_tool_that_publishes_a_workflow_version_declares_workflow_publish():
    from boss_engine.tools import build_default_registry

    registry = build_default_registry()
    wrong, seen = [], []
    for spec in registry.specs():
        try:
            source = inspect.getsource(spec.handler)
        except (OSError, TypeError):
            continue
        if any(p + "(" in source for p in PUBLISHERS):
            seen.append(spec.name)
            if spec.permission != "workflow:publish":
                wrong.append(f"{spec.name} declares {spec.permission}")
    assert "draft_workflow" in seen, seen
    assert not wrong, wrong


# ---- over HTTP: an agent run and a stale session cannot publish by "saving" ---------------------------

def test_saving_or_forking_needs_a_fresh_human_not_an_agent_run(engine):
    from fastapi.testclient import TestClient

    from apps.api.app.main import app
    from apps.api.tests.identity_fixtures import IdentityWorld

    world = IdentityWorld.create(engine)
    person = world.person("wf-saver")
    world.bind(person, "operator", project=world.project_a)
    agent = world.agent("wf-agent", ceiling=("workflow:create", "workflow:read", "project:read"), sponsor=person)
    run = world.run_ctx(person, (agent,), grant_permissions=("workflow:create", "workflow:read", "project:read"),
                        token_scope=("workflow:create", "workflow:read", "project:read"),
                        project_id=world.project_a)
    client = TestClient(app, raise_server_exceptions=False)
    bodies = {"/v1/delivery-workflows/save": {"project_id": world.project_a, "definition_id": "wfd_none",
                                              "spec": {"steps": []}},
              "/v1/delivery-workflows/fork": {"project_id": world.project_a, "workflow_key": "none"}}
    for url, body in bodies.items():
        with world.acting_as(run):
            by_agent = client.post(url, json=body)
        with world.acting_as(world.ctx(person, mfa=False)):
            stale = client.post(url, json=body)
        with world.acting_as(world.ctx(person)):
            fresh = client.post(url, json=body)
        assert by_agent.status_code == 403 and "workflow:publish" in by_agent.text, (url, by_agent.text)
        assert stale.status_code == 401 and "step_up_required" in stale.text, (url, stale.text)
        assert fresh.status_code not in (401, 403), (url, fresh.text)
