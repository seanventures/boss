"""Final Community request-body field discovery without changing handler coercion."""

from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[2]
WAVE3_OPERATIONS = {('post', '/v1/agent-runs/{run_id}/resume'): ['worker_id', 'job_id', 'lease_token'],
 ('post', '/v1/agent/threads/ensure'): ['project_id', 'actor'],
 ('post', '/v1/agent/wakeups/tick'): [],
 ('post', '/v1/artifacts/{artifact_id}/rebuild'): ['project_id'],
 ('post', '/v1/captain/playbooks/compile'): [],
 ('post', '/v1/channels/{provider}/connect'): ['project_id'],
 ('post', '/v1/cloud/easy-connect'): ['project_id'],
 ('post', '/v1/cloud/easy-connect/complete'): ['project_id'],
 ('post', '/v1/cloud/easy-connect/{session_id}/complete'): ['project_id'],
 ('post', '/v1/cloud/unlink'): ['project_id'],
 ('post', '/v1/code-tasks/step'): ['run_id'],
 ('post', '/v1/code-tasks/{run_id}/origin'): ['kind', 'actor'],
 ('post', '/v1/code-tasks/{run_id}/redelivered'): ['worker_id', 'job_id', 'lease_token', 'attempt'],
 ('post', '/v1/code-tasks/{run_id}/setup-deployment'): ['actor', 'host_id', 'app_name'],
 ('post', '/v1/console/v2/autonomy'): ['project_id', 'actor'],
 ('post', '/v1/console/v2/autonomy/apply'): ['project_id'],
 ('post', '/v1/delivery-workflows/run-capabilities'): ['project_id'],
 ('post', '/v1/delivery-workflows/schedules/tick'): ['project_id'],
 ('post', '/v1/discoveries'): [],
 ('post', '/v1/discoveries/{run_id}/promote'): [],
 ('post', '/v1/elastic/capacity'): ['project_id', 'desired_workers'],
 ('post', '/v1/elastic/instances/{instance_id}/heartbeat'): ['project_id'],
 ('post', '/v1/elastic/instances/{instance_id}/terminate'): ['project_id'],
 ('post', '/v1/elastic/pools/{worker_pool_id}/reconcile'): ['project_id'],
 ('post', '/v1/email/config'): ['project_id'],
 ('post', '/v1/integrations/plane/sync'): ['run_id', 'event'],
 ('post', '/v1/investigations/{investigation_id}/db'): [],
 ('post', '/v1/investigations/{investigation_id}/do'): [],
 ('post', '/v1/investigations/{investigation_id}/host'): [],
 ('post', '/v1/meeting-roles'): ['project_id', 'title', 'lens'],
 ('post', '/v1/meetings/step'): ['run_id'],
 ('post', '/v1/memory/{memory_id}/forget'): ['project_id', 'actor'],
 ('post', '/v1/memory/{memory_id}/review'): ['project_id', 'actor'],
 ('post', '/v1/mutation-gate'): ['files', 'target_file'],
 ('post', '/v1/pipeline-profiles'): ['project_id', 'name'],
 ('post', '/v1/pipeline-profiles/default'): ['project_id', 'name'],
 ('post', '/v1/previews/{preview_id}/approve'): ['actor'],
 ('post', '/v1/previews/{preview_id}/feedback'): ['message'],
 ('post', '/v1/qa/benchmark-apps/{benchmark_id}/runs'): ['project_id'],
 ('post', '/v1/sandboxes/{sandbox_id}/exec'): [],
 ('post', '/v1/sandboxes/{sandbox_id}/preserve'): [],
 ('post', '/v1/self/host-stall'): [],
 ('post', '/v1/setup/coding-agents/connect'): ['project_id', 'provider'],
 ('post', '/v1/setup/coding-agents/credential'): ['project_id', 'provider'],
 ('post', '/v1/setup/model'): ['project_id', 'provider'],
 ('post', '/v1/shepherd/autopilot'): ['project_id'],
 ('post', '/v1/shepherd/escalations/{escalation_id}/answer'): [],
 ('post', '/v1/shepherd/propose'): ['run_id'],
 ('post', '/v1/shepherd/sweep'): ['project_id'],
 ('post', '/v1/skill-definitions'): ['project_id'],
 ('post', '/v1/verifier-ratchet/capture'): ['run_id', 'label'],
 ('post', '/v1/visual-gate'): [],
 ('post', '/v1/work-trackers/connect'): ['project_id', 'provider_key'],
 ('post', '/v1/work-trackers/set-default'): ['project_id', 'provider_key'],
 ('post', '/v1/work-trackers/test'): ['project_id', 'provider_key'],
 ('put', '/v1/channels/{provider}/client'): ['project_id'],
 ('put', '/v1/console/v2/org/{agent_id}'): ['project_id']}


def _run(code: str) -> None:
    env = os.environ.copy()
    env["BOSS_EDITION"] = "community"
    env["BOSS_REQUIRE_API_AUTH"] = "false"
    env["PYTHONPATH"] = os.pathsep.join(
        (str(ROOT), str(ROOT / "apps/api"), str(ROOT / "engine"))
    )
    result = subprocess.run(
        [sys.executable, "-c", code], cwd=ROOT, env=env,
        capture_output=True, text=True, check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_all_remaining_community_json_bodies_have_documented_fields():
    _run(
        """
from app.main import app
from boss_engine.mcp.api_surface import ApiSurface
from scripts.tests.test_typed_request_bodies_wave3 import WAVE3_OPERATIONS

schema = app.openapi()
surface = ApiSurface(schema, edition='community')
assert len(WAVE3_OPERATIONS) == 62
assert sum(surface.mounted_input_schema_quality(op) == 'loose_json_body'
           for op in surface.operations.values()) == 0
for (method, path), required in WAVE3_OPERATIONS.items():
    body = schema['paths'][path][method]['requestBody']['content']['application/json']['schema']
    assert '$ref' in body, (method, path, body)
    model = schema['components']['schemas'][body['$ref'].rsplit('/', 1)[-1]]
    assert model['properties'], (method, path, model)
    assert model['additionalProperties'] is True, (method, path, model)
    assert model.get('required', []) == required, (method, path, model.get('required'))
    assert not any('example' in field or 'default' in field
                   for field in model['properties'].values()), (method, path)
assert schema['paths']['/v1/artifacts/{artifact_id}/rebuild']['post']['requestBody'].get('required') is not True
assert {'route', 'force', 'repair_retries'} <= set(
    schema['components']['schemas']['BenchmarkAppLaunchBody']['properties'])
assert {'message', 'expected', 'anchor'} <= set(
    schema['components']['schemas']['PreviewFeedbackBody']['properties'])
for model_name, fields in {
    'ResumeAgentRunBody': ['lease_token'],
    'CodeTaskRedeliveredBody': ['lease_token'],
    'CodeTaskStepBody': ['fencing_token'],
    'ChannelOAuthClientBody': ['client_secret'],
    'CodingAgentCredentialBody': ['credential'],
    'ModelSetupBody': ['api_key'],
    'EmailConfigBody': ['password'],
    'WorkTrackerConnectBody': ['api_token'],
    'CloudEasyCompleteBody': ['code'],
    'CloudEasyCompleteByStateBody': ['code'],
}.items():
    model = schema['components']['schemas'][model_name]
    for field in fields:
        assert model['properties'][field]['writeOnly'] is True, (model_name, field)
"""
    )


def test_pass_through_models_preserve_inputs_and_handler_statuses():
    _run(
        """
from fastapi import FastAPI
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient
from app.main import app
from app.request_bodies import CloudEasyConnectBody, MemoryReviewBody, WorkTrackerConnectBody

body = CloudEasyConnectBody.model_validate({
    'project_id': 123, 'method': ['browser'], 'client_extension': {'source': 'mcp'},
}).payload()
assert body == {'project_id': 123, 'method': ['browser'],
                'client_extension': {'source': 'mcp'}}
assert MemoryReviewBody.model_validate({'project_id': 12}).payload()['project_id'] == 12
assert WorkTrackerConnectBody.model_validate({'test': 'false'}).payload()['test'] == 'false'

paths = {
    '/v1/cloud/easy-connect', '/v1/console/v2/autonomy', '/v1/elastic/capacity',
    '/v1/work-trackers/connect', '/v1/memory/{memory_id}/review',
    '/v1/artifacts/{artifact_id}/rebuild', '/v1/captain/playbooks/compile',
    '/v1/visual-gate',
}
test_app = FastAPI()
test_app.router.routes.extend(
    route for route in app.routes
    if isinstance(route, APIRoute) and route.path in paths
)
client = TestClient(test_app)
assert client.post('/v1/cloud/easy-connect', json={}).status_code == 422
assert client.post('/v1/console/v2/autonomy', json={}).status_code == 400
assert client.post('/v1/elastic/capacity', json={}).status_code == 422
assert client.post('/v1/work-trackers/connect', json={}).status_code == 422
assert client.post('/v1/memory/m1/review', json={}).status_code == 400
assert client.post('/v1/artifacts/a1/rebuild').status_code == 400
assert client.post('/v1/captain/playbooks/compile', json={}).status_code == 422
assert client.post('/v1/visual-gate', json={}).status_code == 200
"""
    )


def test_mcp_matrix_distinguishes_documented_pass_through_from_field_validation():
    _run(
        """
from scripts.audit_mcp_api_parity import mounted_rows
rows = {(row['method'], row['path']): row for row in mounted_rows('community')}
assert rows['POST', '/v1/cloud/easy-connect']['request_body_type_enforcement'] == 'pass_through_documented'
assert rows['POST', '/v1/code-tasks']['request_body_type_enforcement'] == 'pydantic_fields'
assert rows['GET', '/v1/cloud/status']['request_body_type_enforcement'] == 'no_json_body'
assert rows['POST', '/v1/cloud/easy-connect']['runtime_validated_input_schema'] is True
"""
    )
