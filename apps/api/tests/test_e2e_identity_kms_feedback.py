"""Isolated-DB E2E: identity, KMS, autonomous feedback.

Proves the five acceptance criteria on a dedicated database — never the live
`boss` dogfood DB, never silent production writes, never Cloud Account Connect.

  1. GET /v1/identity-providers 200 catalog; unknown id 404;
     blueprint apply=true 422 dry-run-only.
  2. OIDC enabled without issuer → fail-closed 503/config error.
     SAML ACS without provider → 403.
  3. GET /v1/kms/health includes local_vault; bound-but-broken SecretRef
     does not fall back to env.
  4. Metatron drive auto=false applies 0. Factory launch is open_pr only.
     Feedback requires confirm before dispatch.
  5. Isolated DB. Do not claim Cloud Account Connect.

Not GA. Do not merge main.
"""

from __future__ import annotations

import json
import os
from types import SimpleNamespace
from urllib.parse import urlparse

import pytest

from apps.api.app import main as api_main
from apps.api.app.config import get_settings
from boss_engine.config import Settings
from boss_engine.services.credential_resolve import resolve_project_credential
from boss_engine.services.metatron_captain import BLOCKED_APPLY_KINDS, MetatronCaptain
from boss_engine.services.secret_backends import SecretUnavailable
from sqlalchemy import text


PROJECT_ID = "boss-dogfood"
ACTOR = "ceo"
_LIVE_DB_NAMES = {"boss", "boss_account_connect"}


def _database_name(url: str) -> str:
    parsed = urlparse(url)
    name = (parsed.path or "").lstrip("/").split("/")[0]
    return name.split("?")[0]


@pytest.fixture(autouse=True)
def _ephemeral_kms(monkeypatch, tmp_path):
    monkeypatch.setenv("BOSS_KMS_ALLOW_EPHEMERAL_MASTER", "true")
    monkeypatch.setenv("BOSS_KMS_VAULT_DIR", str(tmp_path / "vault"))
    yield


def test_isolated_db_is_not_live_boss_or_account_connect():
    """Criterion 5: this suite must not run against the live dogfood DB."""
    url = os.environ.get("DATABASE_URL") or ""
    assert url, "DATABASE_URL is required"
    dbname = _database_name(url)
    assert dbname not in _LIVE_DB_NAMES, (
        f"Refusing to run identity/KMS E2E against live database {dbname!r}"
    )
    assert "e2e" in dbname or "test" in dbname, (
        f"Isolated DB name must contain e2e or test, got {dbname!r}"
    )


def test_identity_catalog_unknown_and_blueprint_apply_is_dry_run_only(client):
    """Criterion 1: catalog 200, unknown 404, apply=true 422."""
    catalog = client.get("/v1/identity-providers")
    assert catalog.status_code == 200, catalog.text
    body = catalog.json()
    assert body["count"] == 10
    assert body["product"] == "boss_identity_providers"
    ids = [item["id"] for item in body["items"]]
    assert "okta" in ids
    assert all("client_secret" not in item for item in body["items"])

    unknown = client.get("/v1/identity-providers/not-a-real-idp")
    assert unknown.status_code == 404, unknown.text
    assert "Unknown" in unknown.json()["detail"]

    apply_true = client.post(
        "/v1/identity-providers/okta/blueprint",
        json={
            "project_id": PROJECT_ID,
            "variables": {
                "domain": "dev-123.okta.com",
                "client_id": "0oa_example",
            },
            "apply": True,
        },
    )
    assert apply_true.status_code == 422, apply_true.text
    detail = apply_true.json()["detail"].lower()
    assert "apply" in detail
    assert "dry-run" in detail or "not supported" in detail


def test_community_does_not_claim_cloud_account_connect():
    """Criterion 5: a Community process does not mount hosted Account Connect.

    The shared test client is the enterprise-mounted app, so a paid route that
    is present there answers 405. Community omission is a separate boot.
    """
    import subprocess
    import sys
    from pathlib import Path

    root = Path(__file__).resolve().parents[3]
    environment = os.environ.copy()
    environment["BOSS_EDITION"] = "community"
    environment["PYTHONPATH"] = os.pathsep.join((str(root), str(root / "engine")))
    environment.pop("DATABASE_URL", None)
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "import anyio\n"
                "from apps.api.app.main import app\n"
                "\n"
                "async def call(method, path, body=b''):\n"
                "    messages = []\n"
                "    sent = False\n"
                "    scope = {\n"
                "        'type': 'http', 'asgi': {'version': '3.0'}, 'http_version': '1.1',\n"
                "        'method': method, 'scheme': 'http', 'path': path,\n"
                "        'raw_path': path.encode(), 'query_string': b'',\n"
                "        'headers': [(b'content-type', b'application/json'), (b'host', b'test')],\n"
                "        'client': ('127.0.0.1', 123), 'server': ('test', 80),\n"
                "    }\n"
                "    async def receive():\n"
                "        nonlocal sent\n"
                "        if sent:\n"
                "            return {'type': 'http.disconnect'}\n"
                "        sent = True\n"
                "        return {'type': 'http.request', 'body': body, 'more_body': False}\n"
                "    async def send(message):\n"
                "        messages.append(message)\n"
                "    await app(scope, receive, send)\n"
                "    status = next(m['status'] for m in messages if m['type'] == 'http.response.start')\n"
                "    payload = b''.join(m.get('body', b'') for m in messages if m['type'] == 'http.response.body')\n"
                "    return status, payload\n"
                "\n"
                "get_status, get_body = anyio.run(call, 'GET', '/v1/account-connect/link')\n"
                "assert get_status == 404, get_body\n"
                "post_status, post_body = anyio.run(\n"
                "    call, 'POST', '/v1/account-connect/link',\n"
                "    b'{\"project_id\":\"boss-dogfood\",\"actor\":\"ceo\"}',\n"
                ")\n"
                "assert post_status == 404, post_body\n"
            ),
        ],
        cwd=root,
        env=environment,
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr or result.stdout


def test_oidc_enabled_without_issuer_fails_closed_503(client):
    """Criterion 2: enabled-but-unconfigured OIDC is 503, not anonymous auth."""
    previous = {
        "require_api_auth": api_main.settings.require_api_auth,
        "oidc_enabled": api_main.settings.oidc_enabled,
        "oidc_issuer": api_main.settings.oidc_issuer,
        "oidc_audience": api_main.settings.oidc_audience,
        "oidc_jwks_url": api_main.settings.oidc_jwks_url,
    }
    api_main.settings.require_api_auth = False
    api_main.settings.oidc_enabled = True
    api_main.settings.oidc_issuer = None
    api_main.settings.oidc_audience = None
    api_main.settings.oidc_jwks_url = None
    try:
        response = client.get(
            "/v1/projects",
            headers={"Authorization": "Bearer oidc-token"},
        )
        assert response.status_code == 503, response.text
        detail = response.json()["detail"]
        assert "OIDC authentication is enabled" in detail
        assert "not configured" in detail.lower() or "issuer" in detail.lower()
    finally:
        api_main.settings.require_api_auth = previous["require_api_auth"]
        api_main.settings.oidc_enabled = previous["oidc_enabled"]
        api_main.settings.oidc_issuer = previous["oidc_issuer"]
        api_main.settings.oidc_audience = previous["oidc_audience"]
        api_main.settings.oidc_jwks_url = previous["oidc_jwks_url"]


def test_saml_acs_without_provider_is_403(client):
    """Criterion 2: SAML ACS with no provider is 403, not a silent login."""
    previous = api_main.settings.enterprise_saml_enabled
    api_main.settings.enterprise_saml_enabled = True
    try:
        response = client.post(
            f"/saml/v2/{PROJECT_ID}/acs",
            json={"SAMLResponse": "ZHVtbXk="},
        )
        assert response.status_code == 403, response.text
    finally:
        api_main.settings.enterprise_saml_enabled = previous


def test_kms_health_includes_local_vault(client):
    """Criterion 3: local_vault is present on /v1/kms/health."""
    response = client.get("/v1/kms/health")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["product"] == "boss_agent_kms"
    local = next(item for item in body["backends"] if item["id"] == "local_vault")
    assert local.get("ok") is True


def test_bound_but_broken_secretref_does_not_fall_back_to_env():
    """Criterion 3: a bound unresolvable SecretRef must not become env."""
    env_secret = "sk-from-env-fallback-never-log-e2e"

    class _Mappings:
        def __init__(self, row):
            self._row = row

        def first(self):
            return self._row

    class _Result:
        def __init__(self, row):
            self._row = row

        def mappings(self):
            return _Mappings(self._row)

    class FakeConnection:
        def __init__(self, rows):
            self.rows = list(rows)

        def execute(self, _stmt, params=None):
            params = params or {}
            if "i" in params:
                row = next((r for r in self.rows if r.get("id") == params["i"]), None)
                return _Result(row)
            matches = [
                r
                for r in self.rows
                if r.get("project_id") == params.get("project_id")
                and r.get("provider") == params.get("provider")
                and r.get("status") in ("active", "rotating", "rotation_due")
            ]
            return _Result(matches[0] if matches else None)

    row = {
        "id": "sref_broken_e2e",
        "project_id": "proj_a",
        "provider": "openai",
        "status": "active",
        "ref_kind": "env",
        "ref_key": "BOSS_TEST_MISSING_MODEL_KEY_E2E",
        "storage_backend": "environment",
    }
    with pytest.raises(SecretUnavailable):
        resolve_project_credential(
            FakeConnection([row]),
            SimpleNamespace(openai_api_key=env_secret),
            project_id="proj_a",
            provider="openai",
            env_fallback_attr="openai_api_key",
        )


def test_metatron_drive_auto_false_applies_zero(client):
    """Criterion 4: drive auto=false proposes only; applied == 0."""
    drive = client.post(
        "/v1/metatron/drive",
        json={"project_id": PROJECT_ID, "actor": ACTOR, "auto": False},
    )
    assert drive.status_code == 200, drive.text
    body = drive.json()
    assert body.get("auto") is False
    assert body.get("status") == "proposed"
    assert body.get("applied") == []
    assert len(body.get("applied") or []) == 0
    for move in body.get("moves") or []:
        assert move.get("status") != "applied"


def test_factory_launch_is_open_pr_only_never_silent_prod(monkeypatch):
    """Criterion 4: factory apply is open_pr=True and never writes production."""
    captured: dict = {}

    def fake_start(_connection, **kwargs):
        captured.update(kwargs)
        return "crun_self_mod_e2e"

    class FakeJobs:
        def __init__(self, _settings):
            pass

    monkeypatch.setattr(
        "boss_engine.services.code_task_service.start_code_task", fake_start
    )
    monkeypatch.setattr("boss_engine.services.jobs.JobLeaseService", FakeJobs)

    captain = MetatronCaptain(Settings())
    captain.receipts = SimpleNamespace(
        create_or_reuse=lambda *args, **kwargs: ({"id": "rcpt_self_mod_e2e"}, False)
    )
    captain._scope = lambda _connection, _project_id: {
        "org_id": "org_test",
        "workspace_id": "ws_test",
    }
    result = captain._apply_validated_move(
        SimpleNamespace(),
        project_id=PROJECT_ID,
        actor=ACTOR,
        move={"kind": "launch_factory_job", "detail": "Improve BOSS self-improvement"},
        verify_run=None,
        directive="change BOSS captain overwatch",
    )
    assert result["status"] == "applied"
    assert result["self_mod"] is True
    assert captured["open_pr"] is True
    assert captured["finalize_ctx"]["open_pr"] is True
    assert "Open a PR only" in captured["instructions"]
    assert "Do not apply to production in place." in captured["instructions"]
    assert "escalate_decision" in BLOCKED_APPLY_KINDS
    assert "propose_deadletter_review" in BLOCKED_APPLY_KINDS


def _insert_completed_run(engine, run_id: str) -> None:
    settings = get_settings()
    spec = {
        "instructions": "Improve the checkout page.",
        "acceptance_criteria": ["checkout remains usable"],
        "test_commands": ["python3 -m pytest"],
        "owner": "acme",
        "repo": "widget",
        "base_branch": "main",
    }
    fctx = {"execution_substrate": "local_subprocess", "attempt": 1}
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO code_task_runs "
                "(id,project_id,status,phase,title,pull_request_url,spec,state,finalize_ctx) "
                "VALUES (:id,:p,'completed','done','Checkout task',:pr_url,CAST(:spec AS JSONB),"
                "'{}'::jsonb,CAST(:fctx AS JSONB))"
            ),
            {
                "id": run_id,
                "p": settings.default_project_id,
                "pr_url": f"https://example.test/pull/{run_id}",
                "spec": json.dumps(spec),
                "fctx": json.dumps(fctx),
            },
        )


def test_feedback_confirm_required_before_dispatch(client, engine):
    """Criterion 4: proposal does not dispatch; confirm is required."""
    run_id = "crun_e2e_feedback"
    _insert_completed_run(engine, run_id)
    proposed = client.post(
        f"/v1/code-tasks/{run_id}/feedback",
        json={
            "message": "On checkout, clicking Pay leaves the form idle.",
            "expected": "Show progress and submit exactly once.",
            "anchor": "/checkout",
            "selected_element": "button#pay",
            "severity": "high",
        },
    )
    assert proposed.status_code == 200, proposed.text
    body = proposed.json()
    assert body["dispatched"] is False
    assert body["status"] == "awaiting_confirmation"
    thread_id = body["thread_id"]

    confirmed = client.post(
        f"/v1/feedback-threads/{thread_id}/confirm",
        json={"actor": ACTOR},
    )
    assert confirmed.status_code == 200, confirmed.text
    result = confirmed.json()
    assert result["dispatched"] is True
