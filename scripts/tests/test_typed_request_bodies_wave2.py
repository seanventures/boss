"""Community OpenAPI fields for deployment, secrets, usage, and admin writes."""

from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys


ROOT = Path(__file__).resolve().parents[2]

TYPED_OPERATIONS = {
    ("post", "/v1/orgs"): ["name"],
    ("post", "/v1/deployment-targets"): [
        "project_id", "app_name", "repo", "environment", "target_kind", "target_ref", "actor"
    ],
    ("patch", "/v1/deployment-targets/{target_id}"): ["actor"],
    ("post", "/v1/deployment-targets/{target_id}/teardown"): ["actor"],
    ("post", "/v1/deployment-targets/{target_id}/purge"): ["actor", "confirm"],
    ("post", "/v1/secrets"): ["project_id", "name", "value"],
    ("post", "/v1/secrets/keypair"): ["project_id", "name"],
    ("post", "/v1/deployment-targets/{target_id}/secrets"): [
        "project_id", "name", "secret_ref_id"
    ],
    ("post", "/v1/observability/stack/deploy"): ["project_id", "host_target_id"],
    ("post", "/v1/delivery/deploy"): ["target_id", "actor"],
    ("patch", "/v1/deployment-hosts/{host_id}"): ["actor"],
    ("post", "/v1/deployment-hosts"): ["project_id", "name", "address", "actor"],
    ("post", "/v1/deployment-hosts/{host_id}/adopt-key"): ["actor", "private_key"],
    ("post", "/v1/deployment-hosts/{host_id}/attach"): ["target_id", "actor"],
    ("post", "/v1/deployment-hosts/backfill"): ["project_id", "actor"],
    ("post", "/v1/deployment-targets/{target_id}/auto-deploy"): ["actor"],
    ("post", "/v1/deployments/{deployment_run_id}/promote"): ["actor"],
    ("post", "/v1/delivery/first-deploy"): ["project_id", "run_id", "actor"],
    ("post", "/v1/setup/decision-model/key"): ["project_id"],
    ("post", "/v1/provider-usage/ingest"): ["project_id", "provider", "operation"],
    ("post", "/v1/provider-usage/sync"): ["project_id", "provider_account_id"],
    ("put", "/v1/settings/{key}"): ["project_id"],
}


def _run(code: str) -> None:
    environment = os.environ.copy()
    environment["BOSS_EDITION"] = "community"
    environment["BOSS_REQUIRE_API_AUTH"] = "false"
    environment["PYTHONPATH"] = os.pathsep.join(
        (str(ROOT), str(ROOT / "apps/api"), str(ROOT / "engine"))
    )
    result = subprocess.run(
        [sys.executable, "-c", code], cwd=ROOT, env=environment,
        capture_output=True, text=True, check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_live_community_openapi_has_typed_fields_and_handler_required_flags():
    _run(
        """
from app.main import app
from scripts.tests.test_typed_request_bodies_wave2 import TYPED_OPERATIONS

schema = app.openapi()
assert len(TYPED_OPERATIONS) == 22
for (method, path), required in TYPED_OPERATIONS.items():
    operation = schema['paths'][path][method]
    body = operation['requestBody']['content']['application/json']['schema']
    assert '$ref' in body, (method, path, body)
    model = schema['components']['schemas'][body['$ref'].rsplit('/', 1)[-1]]
    assert model['properties'], (method, path, model)
    assert model['additionalProperties'] is True, (method, path, model)
    assert model['required'] == required, (method, path, model['required'])
assert schema['components']['schemas']['SecretPutBody']['properties']['value']['writeOnly']
assert schema['components']['schemas']['DeploymentHostAdoptKeyBody']['properties']['private_key']['writeOnly']
assert 'reservation_id' in schema['components']['schemas']['FirstDeployBody']['properties']
assert 'expected_cost_usd_micros' in schema['components']['schemas']['ManualDeployBody']['properties']
"""
    )


def test_http_handlers_keep_business_statuses_and_extension_keys():
    _run(
        """
from contextlib import contextmanager
from fastapi import FastAPI
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient
from app import main
from app.request_bodies import AutoDeployBody, ManualDeployBody, OrgOnboardBody, SecretPutBody
from boss_engine.services import project_settings as ps

routes = {
    '/v1/orgs', '/v1/secrets', '/v1/delivery/deploy', '/v1/delivery/first-deploy',
    '/v1/deployment-hosts', '/v1/deployment-targets/{target_id}/purge',
    '/v1/provider-usage/ingest', '/v1/settings/{key}',
}
test_app = FastAPI()
test_app.router.routes.extend(
    route for route in main.app.routes
    if isinstance(route, APIRoute) and route.path in routes
)
client = TestClient(test_app)
main.database_is_reachable = lambda engine: True

assert client.post('/v1/orgs', json={}).status_code == 422
assert client.post('/v1/secrets', json={'project_id': 'p'}).status_code == 422
assert client.post('/v1/delivery/deploy', json={'actor': 'ceo'}).status_code == 422
assert client.post('/v1/delivery/first-deploy', json={'actor': 'ceo'}).status_code == 400
assert client.post('/v1/deployment-hosts', json={'project_id': 'p'}).status_code == 422
assert client.post('/v1/deployment-targets/t1/purge', json={}).status_code == 422
assert client.post('/v1/provider-usage/ingest', json={'project_id': 'p'}).status_code == 422
assert client.put('/v1/settings/codex_cli_model', json={}).status_code == 400

@contextmanager
def fake_begin(engine):
    yield object()

main.begin = fake_begin
main.require_project_actor = lambda *args: None
seen = {}
def fake_set_one(connection, project_id, key, value, actor):
    seen.update(project_id=project_id, key=key, value=value, actor=actor)
    return value
ps.set_one = fake_set_one
response = client.put('/v1/settings/codex_cli_model', json={
    'project_id': 'p', 'actor': 'ceo', 'value': 'gpt-6-sol',
    'client_extension': {'source': 'tui'},
})
assert response.status_code == 200, response.text
assert seen == {'project_id': 'p', 'key': 'codex_cli_model',
                'value': 'gpt-6-sol', 'actor': 'ceo'}
assert ManualDeployBody.model_validate({
    'target_id': 't', 'actor': 'ceo', 'expected_cost_usd_micros': '500',
    'client_extension': {'source': 'mcp'},
}).payload()['client_extension'] == {'source': 'mcp'}
assert SecretPutBody.model_validate({
    'project_id': 'p', 'name': 'token', 'value': 'secret', 'vendor_extension': 1,
}).payload()['vendor_extension'] == 1
assert OrgOnboardBody.model_validate({'name': 123}).payload()['name'] == 123
assert AutoDeployBody.model_validate({'actor': 'ceo', 'auto_deploy': 'false'}).payload()['auto_deploy'] == 'false'
"""
    )
