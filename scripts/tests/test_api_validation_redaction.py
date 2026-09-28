"""Invalid API requests must not reflect submitted secret material."""

from __future__ import annotations

import os
import subprocess
import sys


def test_validation_response_never_echoes_credential_input():
    code = """
from fastapi.testclient import TestClient
from apps.api.app.main import app

client = TestClient(app)
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
    env["BOSS_REQUIRE_API_AUTH"] = "false"
    env["PYTHONPATH"] = "apps/api:engine"
    result = subprocess.run(
        [sys.executable, "-c", code],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
