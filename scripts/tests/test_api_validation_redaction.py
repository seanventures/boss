"""Invalid API requests must not reflect submitted secret material."""

from __future__ import annotations

import os
import subprocess
import sys


def test_validation_response_never_echoes_credential_input():
    # The API's own 422 handler, on a route shaped like the credential one. Through the real app
    # the request is refused 401 before validation (sign-in is required since identity landed),
    # so the subprocess this test used to run never reached the handler and went red on main
    # (BOSSAPP-1429). The app registering THIS handler is asserted too.
    code = """
from fastapi import FastAPI
from fastapi.exceptions import RequestValidationError
from fastapi.testclient import TestClient
from pydantic import BaseModel
from apps.api.app.main import app, request_validation_without_input

assert app.exception_handlers[RequestValidationError] is request_validation_without_input

class Credential(BaseModel):
    project_id: str
    value: str

probe = FastAPI()
probe.add_exception_handler(RequestValidationError, request_validation_without_input)

@probe.post('/v1/entitlements/sources/{source}/credential')
def store(source: str, body: Credential):
    return {'ok': True}

client = TestClient(probe)
marker = 'fake-secret-marker-for-validation-test'
missing_field = client.post(
    '/v1/entitlements/sources/openai_api/credential',
    json={'value': marker},
)
assert missing_field.status_code == 422, missing_field.text
assert marker not in missing_field.text
assert missing_field.json()['detail'][0]['loc'] == ['body', 'project_id']

bad_json = client.post(
    '/v1/entitlements/sources/openai_api/credential',
    content='{"value":"' + marker + '",',
    headers={'Content-Type': 'application/json'},
)
assert bad_json.status_code == 422, bad_json.text
assert marker not in bad_json.text
"""
    env = os.environ.copy()
    env["PYTHONPATH"] = ".:apps/api:engine"
    result = subprocess.run(
        [sys.executable, "-c", code],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
