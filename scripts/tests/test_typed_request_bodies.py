"""Mounted request schemas for common Community software and workflow actions."""

from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys


ROOT = Path(__file__).resolve().parents[2]

TYPED_OPERATIONS = {
    ("post", "/v1/code-tasks"),
    ("post", "/v1/code-tasks/{run_id}/follow-up"),
    ("post", "/v1/code-tasks/{run_id}/steer"),
    ("post", "/v1/code-tasks/{run_id}/feedback"),
    ("post", "/v1/prd/decompose"),
    ("post", "/v1/web-app"),
    ("post", "/v1/delivery-workflows/create"),
    ("post", "/v1/delivery-workflows/fork"),
    ("post", "/v1/delivery-workflows/save"),
    ("post", "/v1/delivery-workflows/apply"),
    ("post", "/v1/delivery-workflows/start"),
    ("post", "/v1/delivery-workflows/schedules"),
    ("post", "/v1/delivery-workflows/schedules/{schedule_id}/enabled"),
    ("post", "/v1/delivery-workflows/runs/{run_id}/retry-step"),
    ("post", "/v1/delivery-workflows/runs/{run_id}/recheck"),
    ("post", "/v1/delivery-workflows/runs/{run_id}/input"),
    ("post", "/v1/delivery-workflows/runs/{run_id}/deploy-target"),
    ("post", "/v1/delivery-workflows/custom-steps"),
    ("patch", "/v1/delivery-workflows/custom-steps/{step_key}"),
    ("post", "/v1/delivery-workflows/catalogue/install"),
    ("post", "/v1/projects/{project_id}/agents"),
    ("post", "/v1/role-skills"),
    ("post", "/v1/role-skills/sync"),
}


def _run(code: str) -> None:
    environment = os.environ.copy()
    environment["BOSS_EDITION"] = "community"
    environment["BOSS_REQUIRE_API_AUTH"] = "false"
    environment["PYTHONPATH"] = os.pathsep.join(
        (str(ROOT), str(ROOT / "apps/api"), str(ROOT / "engine"))
    )
    result = subprocess.run(
        [sys.executable, "-c", code],
        cwd=ROOT,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_live_community_openapi_has_field_schemas_for_common_writes():
    _run(
        """
from app.main import app
from scripts.tests.test_typed_request_bodies import TYPED_OPERATIONS

schema = app.openapi()
assert len(TYPED_OPERATIONS) == 23
for method, path in TYPED_OPERATIONS:
    operation = schema['paths'][path][method]
    body = operation['requestBody']['content']['application/json']['schema']
    assert '$ref' in body, (method, path, body)
    model = schema['components']['schemas'][body['$ref'].rsplit('/', 1)[-1]]
    assert model['properties'], (method, path, model)
    assert model['additionalProperties'] is True, (method, path, model)
assert 'model_config' in schema['components']['schemas']['CodeTaskStartBody']['properties']
assert schema['components']['schemas']['WorkflowSaveBody']['required'] == [
    'project_id', 'definition_id', 'spec'
]
assert schema['components']['schemas']['WorkflowScheduleBody']['properties']['weekday']['anyOf'][0]['type'] == 'integer'
"""
    )


def test_http_handlers_keep_business_validation_and_accept_known_payloads():
    _run(
        """
from contextlib import contextmanager
from fastapi import FastAPI
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient
from app import main
from app.request_bodies import CodeTaskStartBody

routes = {
    '/v1/delivery-workflows/create',
    '/v1/delivery-workflows/save',
    '/v1/delivery-workflows/schedules',
    '/v1/code-tasks/{run_id}/steer',
    '/v1/prd/decompose',
}
test_app = FastAPI()
test_app.router.routes.extend(
    route for route in main.app.routes
    if isinstance(route, APIRoute) and route.path in routes
)
client = TestClient(test_app)

assert client.post('/v1/delivery-workflows/create', json={'name': 'One'}).status_code == 400
assert client.post('/v1/delivery-workflows/save', json={'project_id': 'p'}).status_code == 400
assert client.post('/v1/delivery-workflows/schedules', json={}).status_code == 400
assert client.post('/v1/code-tasks/r1/steer', json={'actor': 'alice'}).status_code == 422
main.database_is_reachable = lambda engine: True
assert client.post('/v1/prd/decompose', json={'prd': 'Build it'}).status_code == 422

@contextmanager
def fake_begin(engine):
    yield object()

main.begin = fake_begin
main.require_project_actor = lambda *args: None
main._free_workflow_key = lambda *args: 'one'
main.workflow_runtime.create_definition = lambda connection, **fields: fields
response = client.post('/v1/delivery-workflows/create', json={
    'project_id': 'p', 'name': 'One', 'description': 'A workflow',
    'actor': 'alice', 'client_extension': {'source': 'tui'},
})
assert response.status_code == 200, response.text
assert response.json()['definition'] == {
    'project_id': 'p', 'workflow_key': 'one', 'name': 'One',
    'description': 'A workflow', 'actor': 'alice',
    'namespace': 'product', 'is_catalogue': False,
}
body = CodeTaskStartBody.model_validate({
    'project_id': 'p', 'model_config': {'default': {'engine': 'local'}},
    'client_extension': {'source': 'tui'},
})
assert body.payload()['model_config'] == {'default': {'engine': 'local'}}
assert body.payload()['client_extension'] == {'source': 'tui'}
assert 'simulated' not in body.payload()
"""
    )
