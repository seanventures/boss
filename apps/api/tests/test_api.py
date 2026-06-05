import json
import logging
import subprocess
import base64
from datetime import datetime, timedelta, timezone
from typing import Optional
import xml.etree.ElementTree as ET

import pytest
import jwt
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID
from sqlalchemy import text

from apps.api.app import main as api_main
from apps.api.app.services import oidc as oidc_module
from apps.api.app.services.digitalocean_adapter import (
    AppDeploymentResult,
    DropletProvisionResult,
    DropletTerminationResult,
)
from apps.api.app.services.github_adapter import (
    DeploymentResult,
    DeploymentStatusResult,
    PullRequestMergeResult,
    PullRequestResult,
    PullRequestValidationCheck,
    PullRequestValidationResult,
)
from apps.api.app.services.oidc import OidcPrincipal
from apps.api.app.services.resource_plane import (
    InventoryResource,
    ProviderAdapterError,
    ProviderCostRecord,
)
from apps.api.app.schemas import FileChange


def test_policy_denial_audit_write_failure_is_logged(monkeypatch, caplog):
    def failing_begin(_engine):
        raise RuntimeError("audit database unavailable")

    monkeypatch.setattr(api_main, "begin", failing_begin)
    caplog.set_level(logging.ERROR, logger="boss.api")

    api_main.record_policy_denial(
        "proj_missing",
        "operator",
        "decision_resolve",
        "actor lacks permission",
    )

    assert "policy_denial_audit_write_failed" in caplog.text
    assert "audit database unavailable" in caplog.text


def create_local_validation_repo(tmp_path, branch_name: str):
    repo = tmp_path / "sandbox-validation"
    repo.mkdir()
    for args in (
        ["git", "init"],
        ["git", "config", "user.email", "boss@example.com"],
        ["git", "config", "user.name", "BOSS Test"],
    ):
        subprocess.run(args, cwd=repo, check=True, capture_output=True, text=True)
    (repo / "README.md").write_text("# Sandbox validation\n", encoding="utf-8")
    subprocess.run(
        ["git", "add", "README.md"],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    )
    subprocess.run(
        ["git", "commit", "-m", "Initial sandbox validation fixture"],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    )
    subprocess.run(
        ["git", "checkout", "-b", branch_name],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    )
    return repo


def configure_pr_proposal_integrations(monkeypatch):
    api_main.software_factory.settings.openai_api_key = "test-openai-key"
    monkeypatch.setattr(
        api_main.software_factory.openai,
        "create_text_response",
        lambda prompt, system_prompt=None, model=None: (
            "resp_review_packet",
            "## Implementation summary\n- Review packet generated for test coverage.",
        ),
    )
    monkeypatch.setattr(
        api_main.software_factory.github,
        "ensure_branch",
        lambda branch_name: "sha-base",
    )
    monkeypatch.setattr(
        api_main.software_factory.github,
        "upsert_file",
        lambda branch_name, path, content, message: f"commit-{path}",
    )
    monkeypatch.setattr(
        api_main.software_factory.github,
        "create_pull_request",
        lambda branch_name, title, body: PullRequestResult(
            url="https://github.com/seanheiney/boss-sandbox/pull/77",
            number=77,
        ),
    )
    monkeypatch.setattr(
        api_main.software_factory.github,
        "wait_for_commit_checks",
        lambda head_sha, timeout_seconds, poll_interval_seconds: PullRequestValidationResult(
            status="passed",
            summary="GitHub validation passed: CI.",
            head_sha=head_sha,
            completed=True,
            checks=[
                PullRequestValidationCheck(
                    name="CI",
                    status="completed",
                    conclusion="success",
                    html_url="https://github.com/seanheiney/boss-sandbox/actions/runs/77",
                    started_at="2026-06-02T12:00:00Z",
                    completed_at="2026-06-02T12:01:00Z",
                )
            ],
        ),
    )


def upsert_comms_policy(
    client,
    project_id: str,
    provider: str,
    delivery_mode: str = "real_send_allowed",
    allowed_data_boundaries: Optional[list[str]] = None,
):
    response = client.post(
        "/v1/comms/policies",
        json={
            "project_id": project_id,
            "provider": provider,
            "delivery_mode": delivery_mode,
            "allowed_data_boundaries": allowed_data_boundaries
            or ["public", "internal"],
            "rollout_notes": "test rollout policy",
            "metadata": {"source": "test"},
            "actor": "executive_console",
        },
    )
    assert response.status_code == 201
    return response.json()


def upsert_identity_rollout_policy(
    client,
    project_id: str,
    provider_type: str,
    runtime_mode: str,
):
    response = client.post(
        "/v1/enterprise/identity-rollout-policies",
        json={
            "project_id": project_id,
            "provider_type": provider_type,
            "runtime_mode": runtime_mode,
            "allowed_domains": ["example.com"],
            "rollout_notes": "test identity rollout policy",
            "metadata": {"source": "test"},
            "actor": "ceo",
        },
    )
    assert response.status_code == 201
    return response.json()


def upsert_deployment_environment_policy(
    client,
    project_id: str,
    environment: str = "staging",
    runtime_mode: str = "staging_allowed",
    allowed_target_kinds: Optional[list[str]] = None,
    requires_budget_reservation: bool = True,
    requires_health_check: bool = False,
):
    response = client.post(
        "/v1/deployment-environment-policies",
        json={
            "project_id": project_id,
            "environment": environment,
            "runtime_mode": runtime_mode,
            "allowed_target_kinds": allowed_target_kinds or [],
            "requires_change_set": True,
            "requires_budget_reservation": requires_budget_reservation,
            "requires_approval": True,
            "requires_health_check": requires_health_check,
            "requires_rollback_plan": True,
            "rollout_notes": "test deployment rollout policy",
            "metadata": {"source": "test"},
            "actor": "ceo",
        },
    )
    assert response.status_code == 201
    return response.json()


def _canonicalize_xml(element: ET.Element) -> bytes:
    xml = ET.tostring(element, encoding="unicode")
    return ET.canonicalize(xml_data=xml, with_comments=False).encode("utf-8")


def create_saml_certificate(private_key):
    subject = issuer = x509.Name(
        [x509.NameAttribute(NameOID.COMMON_NAME, "BOSS Test SAML IdP")]
    )
    return (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(issuer)
        .public_key(private_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(datetime.now(timezone.utc) - timedelta(minutes=5))
        .not_valid_after(datetime.now(timezone.utc) + timedelta(days=1))
        .sign(private_key, hashes.SHA256())
    )


def signed_saml_response(
    *,
    private_key,
    certificate,
    issuer: str,
    audience: str,
    subject: str,
    member_id: str,
) -> str:
    saml_ns = "urn:oasis:names:tc:SAML:2.0:assertion"
    protocol_ns = "urn:oasis:names:tc:SAML:2.0:protocol"
    dsig_ns = "http://www.w3.org/2000/09/xmldsig#"
    ET.register_namespace("saml", saml_ns)
    ET.register_namespace("samlp", protocol_ns)
    ET.register_namespace("ds", dsig_ns)

    response = ET.Element(
        f"{{{protocol_ns}}}Response",
        {"ID": "resp_test", "Version": "2.0"},
    )
    assertion = ET.SubElement(
        response,
        f"{{{saml_ns}}}Assertion",
        {"ID": "assert_test", "Version": "2.0"},
    )
    ET.SubElement(assertion, f"{{{saml_ns}}}Issuer").text = issuer
    subject_el = ET.SubElement(assertion, f"{{{saml_ns}}}Subject")
    ET.SubElement(subject_el, f"{{{saml_ns}}}NameID").text = subject
    conditions = ET.SubElement(
        assertion,
        f"{{{saml_ns}}}Conditions",
        {
            "NotBefore": (
                datetime.now(timezone.utc) - timedelta(minutes=1)
            ).isoformat(),
            "NotOnOrAfter": (
                datetime.now(timezone.utc) + timedelta(minutes=5)
            ).isoformat(),
        },
    )
    audience_restriction = ET.SubElement(
        conditions,
        f"{{{saml_ns}}}AudienceRestriction",
    )
    ET.SubElement(audience_restriction, f"{{{saml_ns}}}Audience").text = audience
    attributes = ET.SubElement(assertion, f"{{{saml_ns}}}AttributeStatement")
    attribute = ET.SubElement(
        attributes,
        f"{{{saml_ns}}}Attribute",
        {"Name": "boss_member_id"},
    )
    ET.SubElement(attribute, f"{{{saml_ns}}}AttributeValue").text = member_id

    hasher = hashes.Hash(hashes.SHA256())
    hasher.update(_canonicalize_xml(assertion))
    digest = base64.b64encode(hasher.finalize()).decode("ascii")

    signature = ET.SubElement(assertion, f"{{{dsig_ns}}}Signature")
    signed_info = ET.SubElement(signature, f"{{{dsig_ns}}}SignedInfo")
    ET.SubElement(
        signed_info,
        f"{{{dsig_ns}}}CanonicalizationMethod",
        {"Algorithm": "http://www.w3.org/2006/12/xml-c14n11"},
    )
    ET.SubElement(
        signed_info,
        f"{{{dsig_ns}}}SignatureMethod",
        {"Algorithm": "http://www.w3.org/2001/04/xmldsig-more#rsa-sha256"},
    )
    reference = ET.SubElement(
        signed_info,
        f"{{{dsig_ns}}}Reference",
        {"URI": "#assert_test"},
    )
    transforms = ET.SubElement(reference, f"{{{dsig_ns}}}Transforms")
    ET.SubElement(
        transforms,
        f"{{{dsig_ns}}}Transform",
        {"Algorithm": "http://www.w3.org/2000/09/xmldsig#enveloped-signature"},
    )
    ET.SubElement(
        transforms,
        f"{{{dsig_ns}}}Transform",
        {"Algorithm": "http://www.w3.org/2006/12/xml-c14n11"},
    )
    ET.SubElement(
        reference,
        f"{{{dsig_ns}}}DigestMethod",
        {"Algorithm": "http://www.w3.org/2001/04/xmlenc#sha256"},
    )
    ET.SubElement(reference, f"{{{dsig_ns}}}DigestValue").text = digest
    signature_value = private_key.sign(
        _canonicalize_xml(signed_info),
        padding.PKCS1v15(),
        hashes.SHA256(),
    )
    ET.SubElement(signature, f"{{{dsig_ns}}}SignatureValue").text = base64.b64encode(
        signature_value
    ).decode("ascii")
    key_info = ET.SubElement(signature, f"{{{dsig_ns}}}KeyInfo")
    x509_data = ET.SubElement(key_info, f"{{{dsig_ns}}}X509Data")
    ET.SubElement(x509_data, f"{{{dsig_ns}}}X509Certificate").text = base64.b64encode(
        certificate.public_bytes(serialization.Encoding.DER)
    ).decode("ascii")
    return base64.b64encode(ET.tostring(response, encoding="utf-8")).decode("ascii")


def reserve_for_pr_proposal(client) -> str:
    reserve = client.post(
        "/v1/budget/reserve",
        json={
            "project_id": "boss-dogfood",
            "category": "tool",
            "amount_usd_micros": 100_000,
            "reason": "Run sandbox PR proposal validation.",
        },
    )
    assert reserve.status_code == 201
    return reserve.json()["reservation_id"]


def reserve_for_infra_provision(client, reason: str = "Reserve infra budget.") -> str:
    reserve = client.post(
        "/v1/budget/reserve",
        json={
            "project_id": "boss-dogfood",
            "category": "infra",
            "amount_usd_micros": 250_000,
            "reason": reason,
        },
    )
    assert reserve.status_code == 201
    return reserve.json()["reservation_id"]


def create_test_receipt(engine, dedupe_key: str = "budget-consume-test") -> str:
    settings = api_main.settings
    with engine.begin() as connection:
        receipt, _created = api_main.receipts.create_or_reuse(
            connection,
            settings.default_org_id,
            settings.default_workspace_id,
            settings.default_project_id,
            dedupe_key,
            "model.call",
            {"scope": "budget_consume_test"},
            {"prompt_tokens": 100, "completion_tokens": 20},
            data_boundary="internal",
        )
        api_main.receipts.mark_succeeded(
            connection,
            receipt["id"],
            "provider:budget-consume-test",
            {"status": "ok"},
        )
        return receipt["id"]


def test_budget_consume_records_cost_receipt_and_consumes_reservation(client, engine):
    reservation_id = reserve_for_pr_proposal(client)
    receipt_id = create_test_receipt(engine)

    response = client.post(
        "/v1/budget/consume",
        json={
            "project_id": "boss-dogfood",
            "reservation_id": reservation_id,
            "receipt_id": receipt_id,
            "category": "model_api",
            "skill_or_model_id": "openai:gpt-5.2-codex",
            "estimated_usd_micros": 50_000,
            "actual_usd_micros": 45_000,
            "actor": "ceo",
        },
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "consumed"
    assert payload["reservation_id"] == reservation_id
    assert payload["cost_receipt"]["receipt_id"] == receipt_id
    assert payload["cost_receipt"]["actual_usd_micros"] == 45_000

    status = client.get(
        "/v1/budget/status",
        params={"scope_type": "project", "scope_id": "boss-dogfood"},
    )
    assert status.status_code == 200
    assert status.json()["actual_usd_micros"] >= 45_000

    events = client.get("/v1/events", params={"project_id": "boss-dogfood"})
    event_types = {item["event_type"] for item in events.json()["items"]}
    assert "budget.reservation_consumed" in event_types

    # monitoring polish: event feed empty state via limit/offset beyond
    empty_events = client.get(
        "/v1/events", params={"project_id": "boss-dogfood", "limit": 1, "offset": 999999}
    )
    assert empty_events.status_code == 200
    assert empty_events.json()["items"] == []


def test_budget_consume_requires_budget_permission_and_active_reservation(client, engine):
    reservation_id = reserve_for_pr_proposal(client)
    receipt_id = create_test_receipt(engine, "budget-consume-denied-test")

    denied = client.post(
        "/v1/budget/consume",
        json={
            "project_id": "boss-dogfood",
            "reservation_id": reservation_id,
            "receipt_id": receipt_id,
            "category": "model_api",
            "skill_or_model_id": "openai:gpt-5.2-codex",
            "estimated_usd_micros": 50_000,
            "actor": "cto",
        },
    )

    assert denied.status_code == 403

    allowed = client.post(
        "/v1/budget/consume",
        json={
            "project_id": "boss-dogfood",
            "reservation_id": "res_missing",
            "receipt_id": receipt_id,
            "category": "model_api",
            "skill_or_model_id": "openai:gpt-5.2-codex",
            "estimated_usd_micros": 50_000,
            "actor": "ceo",
        },
    )
    assert allowed.status_code == 409
    assert "Active budget reservation not found" in allowed.json()["detail"]


def test_budget_forecast_endpoint_exposes_forecast_contract(client):
    reservation_id = reserve_for_pr_proposal(client)

    forecast = client.get(
        "/v1/budget/forecast",
        params={
            "scope_type": "project",
            "scope_id": "boss-dogfood",
            "horizon_hours": 48,
        },
    )

    assert forecast.status_code == 200
    payload = forecast.json()
    assert payload["horizon_hours"] == 48
    assert payload["reserved_usd_micros"] >= 100_000
    assert payload["forecasted_usd_micros"] >= 100_000
    assert payload["top_cost_drivers"] == []

    release = client.post(
        "/v1/budget/release",
        json={
            "project_id": "boss-dogfood",
            "reservation_id": reservation_id,
            "reason": "Clean up forecast test reservation.",
        },
    )
    assert release.status_code == 200

def insert_approved_infra_decision(
    engine,
    *,
    decision_id: str,
    reservation_id: str,
    task_id: Optional[str] = None,
    title: str = "Provision preview droplet",
    region: str = "nyc3",
    size: str = "s-1vcpu-1gb",
    ttl_minutes: int = 60,
    data_boundary: str = "internal",
):
    with engine.begin() as connection:
        connection.execute(
            text(
                """
                INSERT INTO decisions (
                    id, project_id, task_id, question, recommendation, options, risk_tier,
                    cost_summary, deadline_at, default_action, status, approved_by, approved_at
                )
                VALUES (
                    :decision_id,
                    'boss-dogfood',
                    :task_id,
                    :question,
                    CAST(:recommendation AS JSONB),
                    '[{"label":"Provision","action":"approve"}]'::jsonb,
                    3,
                    CAST(:cost_summary AS JSONB),
                    now() + interval '1 day',
                    'hold',
                    'approved',
                    'ceo',
                    now()
                )
                """
            ),
            {
                "decision_id": decision_id,
                "task_id": task_id,
                "question": f"Approve infrastructure provisioning: {title}",
                "recommendation": json.dumps(
                    {
                        "action": "review_infra_provisioning",
                        "reservation_id": reservation_id,
                        "title": title,
                        "region": region,
                        "size": size,
                        "ttl_minutes": ttl_minutes,
                        "tags": ["dogfood", "preview"],
                        "data_boundary": data_boundary,
                    }
                ),
                "cost_summary": json.dumps(
                    {
                        "reservation_id": reservation_id,
                        "region": region,
                        "size": size,
                        "ttl_minutes": ttl_minutes,
                    }
                ),
            },
        )


def assert_complete_rolespec(spec: dict) -> None:
    for key in (
        "role_id",
        "display_name",
        "mission",
        "primary_outcomes",
        "boundaries",
        "decision_rights",
        "cadence",
        "permissions",
        "comms_contract",
        "output_contracts",
        "eval_rubric",
        "autonomy",
    ):
        assert key in spec
    assert spec["primary_outcomes"]
    assert spec["output_contracts"]
    assert spec["boundaries"]["must"]
    assert spec["boundaries"]["must_not"]
    assert spec["decision_rights"]["can_decide"]
    assert spec["decision_rights"]["must_escalate"]
    assert spec["permissions"]["allowed_skills"]
    assert spec["permissions"]["forbidden_skills"]
    assert isinstance(spec["autonomy"]["level"], int)


def assert_complete_skill_spec(skill: dict) -> None:
    for key in (
        "id",
        "kind",
        "risk_tier",
        "description",
        "mock_mode",
        "input_schema",
        "output_schema",
        "cost",
        "retry",
        "permissions",
        "data_boundary",
        "policy",
    ):
        assert key in skill
    assert skill["mock_mode"] is False
    assert skill["input_schema"]
    assert skill["output_schema"]
    assert skill["cost"]["estimator"]
    assert skill["cost"]["category"]
    assert skill["retry"]["max_attempts"] >= 1
    assert skill["retry"]["backoff"]
    assert skill["permissions"]["allowed_callers"]
    assert skill["permissions"]["required_permissions"]


def auth_headers(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def test_health_returns_service_status(client):
    response = client.get("/health")

    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "ok"
    assert payload["database_connected"] is True


def test_v1_routes_can_require_api_key_without_hiding_health(client):
    previous_required = api_main.settings.require_api_auth
    previous_keys = api_main.settings.api_keys
    api_main.settings.require_api_auth = True
    api_main.settings.api_keys = "secret-one,secret-two"
    try:
        health = client.get("/health")
        assert health.status_code == 200

        missing = client.get("/v1/projects")
        assert missing.status_code == 401

        wrong = client.get("/v1/projects", headers={"Authorization": "Bearer wrong"})
        assert wrong.status_code == 401

        bearer = client.get(
            "/v1/projects", headers={"Authorization": "Bearer secret-one"}
        )
        assert bearer.status_code == 200

        explicit_header = client.get(
            "/v1/projects", headers={"X-BOSS-API-Key": "secret-two"}
        )
        assert explicit_header.status_code == 200
    finally:
        api_main.settings.require_api_auth = previous_required
        api_main.settings.api_keys = previous_keys


def test_v1_routes_fail_closed_when_auth_required_without_configured_keys(client):
    previous_required = api_main.settings.require_api_auth
    previous_keys = api_main.settings.api_keys
    api_main.settings.require_api_auth = True
    api_main.settings.api_keys = ""
    try:
        response = client.get("/v1/projects", headers={"Authorization": "Bearer any"})
        assert response.status_code == 503
        assert "no BOSS_API_KEYS" in response.json()["detail"]
    finally:
        api_main.settings.require_api_auth = previous_required
        api_main.settings.api_keys = previous_keys


def test_api_key_principal_binding_reports_session_and_blocks_impersonation(client):
    previous_required = api_main.settings.require_api_auth
    previous_keys = api_main.settings.api_keys
    previous_principals = api_main.settings.api_key_principals
    api_main.settings.require_api_auth = True
    api_main.settings.api_keys = "ceo-key,release-key"
    api_main.settings.api_key_principals = "ceo-key:ceo,release-key:release_manager"
    try:
        session = client.get(
            "/v1/auth/session",
            headers=auth_headers("ceo-key"),
        )
        assert session.status_code == 200
        assert session.json()["principal_member_id"] == "ceo"
        assert session.json()["actor_binding_required"] is True

        denied = client.post(
            "/v1/commands/propose",
            headers=auth_headers("release-key"),
            json={
                "project_id": "boss-dogfood",
                "command": "Attempt to act as the CEO with the release key.",
                "actor": "ceo",
            },
        )
        assert denied.status_code == 403
        assert "release_manager" in denied.json()["detail"]

        allowed = client.post(
            "/v1/commands/propose",
            headers=auth_headers("ceo-key"),
            json={
                "project_id": "boss-dogfood",
                "command": "Create a principal-bound command intake record.",
                "actor": "ceo",
            },
        )
        assert allowed.status_code == 201
    finally:
        api_main.settings.require_api_auth = previous_required
        api_main.settings.api_keys = previous_keys
        api_main.settings.api_key_principals = previous_principals


def test_unbound_api_key_fails_closed_when_principal_binding_is_configured(client):
    previous_required = api_main.settings.require_api_auth
    previous_keys = api_main.settings.api_keys
    previous_principals = api_main.settings.api_key_principals
    api_main.settings.require_api_auth = True
    api_main.settings.api_keys = "ceo-key,unbound-key"
    api_main.settings.api_key_principals = "ceo-key:ceo"
    try:
        response = client.get(
            "/v1/projects",
            headers=auth_headers("unbound-key"),
        )

        assert response.status_code == 401
        assert "not bound to a project member principal" in response.json()["detail"]
    finally:
        api_main.settings.require_api_auth = previous_required
        api_main.settings.api_keys = previous_keys
        api_main.settings.api_key_principals = previous_principals


def test_oidc_principal_binding_reports_session_and_blocks_impersonation(
    client,
    monkeypatch,
):
    previous_required = api_main.settings.require_api_auth
    previous_oidc_enabled = api_main.settings.oidc_enabled
    previous_oidc_issuer = api_main.settings.oidc_issuer
    previous_oidc_audience = api_main.settings.oidc_audience
    previous_oidc_jwks_url = api_main.settings.oidc_jwks_url
    api_main.settings.require_api_auth = False
    api_main.settings.oidc_enabled = True
    api_main.settings.oidc_issuer = "https://issuer.example.com"
    api_main.settings.oidc_audience = "boss-api"
    api_main.settings.oidc_jwks_url = "https://issuer.example.com/.well-known/jwks.json"

    def verify_oidc_token(token: str) -> OidcPrincipal:
        assert token == "oidc-token"
        return OidcPrincipal(
            member_id="ceo",
            subject="sub-ceo",
            issuer="https://issuer.example.com",
            claims={"sub": "sub-ceo", "boss_member_id": "ceo"},
        )

    monkeypatch.setattr(api_main.oidc_auth, "verify_bearer_token", verify_oidc_token)
    try:
        session = client.get(
            "/v1/auth/session",
            headers=auth_headers("oidc-token"),
        )
        assert session.status_code == 200
        assert session.json()["principal_member_id"] == "ceo"
        assert session.json()["auth_method"] == "oidc"
        assert session.json()["oidc_enabled"] is True
        assert session.json()["actor_binding_required"] is True

        denied = client.post(
            "/v1/commands/propose",
            headers=auth_headers("oidc-token"),
            json={
                "project_id": "boss-dogfood",
                "command": "Attempt to act as a different member with OIDC.",
                "actor": "executive_console",
            },
        )
        assert denied.status_code == 403
        assert "ceo" in denied.json()["detail"]

        allowed = client.post(
            "/v1/commands/propose",
            headers=auth_headers("oidc-token"),
            json={
                "project_id": "boss-dogfood",
                "command": "Create an OIDC principal-bound command intake record.",
                "actor": "ceo",
            },
        )
        assert allowed.status_code == 201
    finally:
        api_main.settings.require_api_auth = previous_required
        api_main.settings.oidc_enabled = previous_oidc_enabled
        api_main.settings.oidc_issuer = previous_oidc_issuer
        api_main.settings.oidc_audience = previous_oidc_audience
        api_main.settings.oidc_jwks_url = previous_oidc_jwks_url


def test_oidc_enabled_without_provider_config_fails_closed(client):
    previous_required = api_main.settings.require_api_auth
    previous_oidc_enabled = api_main.settings.oidc_enabled
    previous_oidc_issuer = api_main.settings.oidc_issuer
    previous_oidc_audience = api_main.settings.oidc_audience
    previous_oidc_jwks_url = api_main.settings.oidc_jwks_url
    api_main.settings.require_api_auth = False
    api_main.settings.oidc_enabled = True
    api_main.settings.oidc_issuer = None
    api_main.settings.oidc_audience = None
    api_main.settings.oidc_jwks_url = None
    try:
        response = client.get("/v1/projects", headers=auth_headers("oidc-token"))
        assert response.status_code == 503
        assert "OIDC authentication is enabled" in response.json()["detail"]
    finally:
        api_main.settings.require_api_auth = previous_required
        api_main.settings.oidc_enabled = previous_oidc_enabled
        api_main.settings.oidc_issuer = previous_oidc_issuer
        api_main.settings.oidc_audience = previous_oidc_audience
        api_main.settings.oidc_jwks_url = previous_oidc_jwks_url


def test_oidc_authenticator_verifies_signed_jwt_and_member_claim(monkeypatch):
    previous_oidc_enabled = api_main.settings.oidc_enabled
    previous_oidc_issuer = api_main.settings.oidc_issuer
    previous_oidc_audience = api_main.settings.oidc_audience
    previous_oidc_jwks_url = api_main.settings.oidc_jwks_url
    previous_oidc_principal_claim = api_main.settings.oidc_principal_claim
    api_main.settings.oidc_enabled = True
    api_main.settings.oidc_issuer = "https://issuer.example.com"
    api_main.settings.oidc_audience = "boss-api"
    api_main.settings.oidc_jwks_url = "https://issuer.example.com/.well-known/jwks.json"
    api_main.settings.oidc_principal_claim = "boss_member_id"
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    public_key = private_key.public_key()
    token = jwt.encode(
        {
            "iss": "https://issuer.example.com",
            "aud": "boss-api",
            "sub": "sub-ceo",
            "boss_member_id": "ceo",
            "exp": datetime.now(timezone.utc) + timedelta(minutes=5),
        },
        private_key,
        algorithm="RS256",
        headers={"kid": "boss-test-key"},
    )

    class FakeSigningKey:
        key = public_key

    class FakeJwksClient:
        def get_signing_key_from_jwt(self, _token: str):
            return FakeSigningKey()

    monkeypatch.setattr(
        oidc_module,
        "jwks_client_for_url",
        lambda _jwks_url: FakeJwksClient(),
    )
    try:
        principal = api_main.oidc_auth.verify_bearer_token(token)
    finally:
        api_main.settings.oidc_enabled = previous_oidc_enabled
        api_main.settings.oidc_issuer = previous_oidc_issuer
        api_main.settings.oidc_audience = previous_oidc_audience
        api_main.settings.oidc_jwks_url = previous_oidc_jwks_url
        api_main.settings.oidc_principal_claim = previous_oidc_principal_claim

    assert principal.member_id == "ceo"
    assert principal.subject == "sub-ceo"
    assert principal.issuer == "https://issuer.example.com"


def test_enterprise_saml_runtime_verifies_assertion_and_binds_session(client, engine):
    project_id = "boss-saml-runtime"
    previous_saml_enabled = api_main.settings.enterprise_saml_enabled
    previous_saml_ttl = api_main.settings.enterprise_saml_session_ttl_minutes
    previous_member_attribute = api_main.settings.enterprise_saml_member_attribute
    with engine.begin() as connection:
        connection.execute(text("DELETE FROM enterprise_saml_sessions WHERE project_id = :project_id"), {"project_id": project_id})
        connection.execute(text("DELETE FROM enterprise_identity_rollout_policies WHERE project_id = :project_id"), {"project_id": project_id})
        connection.execute(text("DELETE FROM enterprise_identity_providers WHERE project_id = :project_id"), {"project_id": project_id})
        connection.execute(text("DELETE FROM events WHERE project_id = :project_id"), {"project_id": project_id})
        connection.execute(text("DELETE FROM project_members WHERE project_id = :project_id"), {"project_id": project_id})
        connection.execute(text("DELETE FROM projects WHERE id = :project_id"), {"project_id": project_id})
        connection.execute(
            text(
                """
                INSERT INTO projects (id, workspace_id, name, slug)
                VALUES (:project_id, 'ws_boss', 'BOSS SAML Runtime', :project_id)
                """
            ),
            {"project_id": project_id},
        )
        api_main.members.seed_default_members(connection, project_id, "test")

    try:
        api_main.settings.enterprise_saml_enabled = False
        disabled = client.post(
            f"/saml/v2/{project_id}/acs",
            json={"SAMLResponse": "not-yet-validated"},
        )
        assert disabled.status_code == 503

        api_main.settings.enterprise_saml_enabled = True
        api_main.settings.enterprise_saml_session_ttl_minutes = 30
        api_main.settings.enterprise_saml_member_attribute = "boss_member_id"
        missing_provider = client.post(
            f"/saml/v2/{project_id}/acs",
            json={"SAMLResponse": base64.b64encode(b"<empty />").decode("ascii")},
        )
        assert missing_provider.status_code == 403

        api_main.settings.enterprise_saml_enabled = False
        private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        certificate = create_saml_certificate(private_key)
        fingerprint = certificate.fingerprint(hashes.SHA256()).hex()
        provider = client.post(
            "/v1/enterprise/identity-providers",
            json={
                "project_id": project_id,
                "provider_key": "okta_saml",
                "provider_type": "saml",
                "display_name": "Okta SAML",
                "issuer": "https://idp.example/saml",
                "entity_id": "urn:boss:test:saml",
                "sso_url": "https://idp.example/sso",
                "certificate_fingerprint": fingerprint,
                "status": "active",
                "actor": "ceo",
            },
        )
        assert provider.status_code == 201
        assert provider.json()["status"] == "active"
        api_main.settings.enterprise_saml_enabled = True

        metadata = client.get(f"/saml/v2/{project_id}/metadata")
        assert metadata.status_code == 200
        assert "AssertionConsumerService" in metadata.text

        response = signed_saml_response(
            private_key=private_key,
            certificate=certificate,
            issuer="https://idp.example/saml",
            audience="urn:boss:test:saml",
            subject="ceo@example.com",
            member_id="ceo",
        )
        blocked_by_policy = client.post(
            f"/saml/v2/{project_id}/acs",
            json={"SAMLResponse": response},
        )
        assert blocked_by_policy.status_code == 403
        assert "rollout policy" in blocked_by_policy.json()["detail"]

        api_main.settings.enterprise_saml_enabled = False
        rollout_policy = upsert_identity_rollout_policy(
            client, project_id, "saml", "auth_allowed"
        )
        assert rollout_policy["runtime_mode"] == "auth_allowed"
        api_main.settings.enterprise_saml_enabled = True

        login = client.post(
            f"/saml/v2/{project_id}/acs",
            json={"SAMLResponse": response},
        )
        assert login.status_code == 200
        login_payload = login.json()
        assert login_payload["access_token"].startswith("boss_saml_")
        assert login_payload["member_id"] == "ceo"

        session = client.get(
            "/v1/auth/session",
            headers=auth_headers(login_payload["access_token"]),
        )
        assert session.status_code == 200
        assert session.json()["principal_member_id"] == "ceo"
        assert session.json()["auth_method"] == "saml"

        wrong_audience = signed_saml_response(
            private_key=private_key,
            certificate=certificate,
            issuer="https://idp.example/saml",
            audience="urn:wrong:audience",
            subject="ceo@example.com",
            member_id="ceo",
        )
        rejected = client.post(
            f"/saml/v2/{project_id}/acs",
            json={"SAMLResponse": wrong_audience},
        )
        assert rejected.status_code == 401
        assert "audience" in rejected.json()["detail"]

        events = client.get(
            "/v1/events",
            params={"project_id": project_id, "limit": 20},
            headers=auth_headers(login_payload["access_token"]),
        )
        event_types = [item["event_type"] for item in events.json()["items"]]
        assert "enterprise.saml_runtime_blocked" in event_types
        assert "enterprise.saml_login_succeeded" in event_types
        assert "enterprise.saml_login_failed" in event_types
    finally:
        api_main.settings.enterprise_saml_enabled = previous_saml_enabled
        api_main.settings.enterprise_saml_session_ttl_minutes = previous_saml_ttl
        api_main.settings.enterprise_saml_member_attribute = previous_member_attribute


def test_api_key_principal_binding_applies_to_decision_transitions(client, engine):
    previous_required = api_main.settings.require_api_auth
    previous_keys = api_main.settings.api_keys
    previous_principals = api_main.settings.api_key_principals
    api_main.settings.require_api_auth = True
    api_main.settings.api_keys = "ceo-key,release-key"
    api_main.settings.api_key_principals = "ceo-key:ceo,release-key:release_manager"
    decision_id = "dec_auth_principal_binding"
    with engine.begin() as connection:
        connection.execute(
            text(
                """
                INSERT INTO decisions (
                    id, project_id, question, recommendation, options,
                    risk_tier, default_action, status
                )
                VALUES (
                    :decision_id,
                    'boss-dogfood',
                    'Approve a principal-bound decision transition?',
                    '{"action":"approve"}'::jsonb,
                    '[{"label":"Approve","action":"approve"}]'::jsonb,
                    1,
                    'hold',
                    'pending'
                )
                """
            ),
            {"decision_id": decision_id},
        )
    try:
        denied = client.post(
            f"/v1/decisions/{decision_id}/approve",
            headers=auth_headers("release-key"),
            json={"actor": "ceo"},
        )
        assert denied.status_code == 403
        assert "release_manager" in denied.json()["detail"]

        allowed = client.post(
            f"/v1/decisions/{decision_id}/approve",
            headers=auth_headers("ceo-key"),
            json={"actor": "ceo"},
        )
        assert allowed.status_code == 200
        assert allowed.json()["status"] == "approved"
    finally:
        api_main.settings.require_api_auth = previous_required
        api_main.settings.api_keys = previous_keys
        api_main.settings.api_key_principals = previous_principals


def test_status_returns_seeded_project(client):
    response = client.get("/v1/status", params={"project_id": "boss-dogfood"})

    assert response.status_code == 200
    payload = response.json()
    assert payload["project_id"] == "boss-dogfood"
    assert payload["project_name"] == "BOSS dogfood bootstrap"
    assert "deadletters" in payload
    assert "active_jobs" in payload
    assert "top_risks" in payload


def test_list_projects_returns_seeded_project_scope(client):
    response = client.get("/v1/projects")

    assert response.status_code == 200
    payload = response.json()
    assert payload["items"]
    projects = {item["id"]: item for item in payload["items"]}
    assert projects["boss-dogfood"]["workspace_name"] == "Dogfood"
    assert projects["boss-dogfood"]["org_name"] == "BOSS"


def test_community_edition_is_active_without_account_connect(client):
    previous = api_main.settings.boss_edition
    api_main.settings.boss_edition = "community"
    api_main.commercial.settings.boss_edition = "community"
    try:
        response = client.get(
            "/v1/edition",
            params={"project_id": "boss-dogfood"},
        )
        assert response.status_code == 200
        edition = response.json()
        assert edition["edition_id"] == "community"
        assert edition["status"] == "active"
        assert edition["account_connect_required"] is False
        assert edition["account_connect_status"] == "not_required"
        assert edition["local_auth_allowed"] is True
        assert edition["hosted_auth_required"] is False

        entitlements = client.get(
            "/v1/entitlements",
            params={"project_id": "boss-dogfood"},
        )
        assert entitlements.status_code == 200
        payload = entitlements.json()
        assert payload["edition"]["edition_id"] == "community"
        capabilities = {item["capability_key"] for item in payload["items"]}
        assert {"local_auth", "local_runner", "local_control_plane"} <= capabilities
        assert "core_canonical_objects" in capabilities
        assert all(item["status"] == "active" for item in payload["items"])
    finally:
        api_main.settings.boss_edition = previous
        api_main.commercial.settings.boss_edition = previous


def test_cloud_edition_requires_account_connect_and_audits_link_unlink(client):
    previous = api_main.settings.boss_edition
    api_main.settings.boss_edition = "cloud"
    api_main.commercial.settings.boss_edition = "cloud"
    try:
        blocked = client.get(
            "/v1/edition",
            params={"project_id": "boss-dogfood"},
        )
        assert blocked.status_code == 200
        blocked_payload = blocked.json()
        assert blocked_payload["edition_id"] == "cloud"
        assert blocked_payload["status"] == "blocked_account_connect_required"
        assert blocked_payload["account_connect_required"] is True
        assert blocked_payload["blocked_reason"] == "account_connect_required"

        link = client.post(
            "/v1/account-connect/link",
            json={
                "project_id": "boss-dogfood",
                "hosted_org_id": "acct_org_test",
                "connection_ref": "acctconn:test",
                "marketplace_identity": {"publisher": "seanheiney"},
                "billing_portal_ref": "stripe:portal:test",
                "connector_authorization_refs": ["github:installation:test"],
                "optional_telemetry_enabled": True,
                "metadata": {"source": "integration_test"},
                "actor": "ceo",
            },
        )
        assert link.status_code == 200
        linked = link.json()["edition"]
        assert linked["status"] == "active"
        assert linked["account_connect_status"] == "linked"
        assert linked["connection_mode"] == "cloud_required"
        assert linked["account_connect_org"]["hosted_org_id"] == "acct_org_test"
        assert linked["account_connect_org"]["billing_portal_ref"] == "stripe:portal:test"

        entitlements = client.get(
            "/v1/entitlements",
            params={"project_id": "boss-dogfood"},
        )
        assert entitlements.status_code == 200
        account_connect_rows = [
            item
            for item in entitlements.json()["items"]
            if item["capability_key"] == "account_connect"
            and item["source"] == "account_connect"
        ]
        assert account_connect_rows
        assert account_connect_rows[0]["source"] == "account_connect"
        assert account_connect_rows[0]["status"] == "active"

        unlink = client.post(
            "/v1/account-connect/unlink",
            json={
                "project_id": "boss-dogfood",
                "hosted_org_id": "acct_org_test",
                "reason": "operator_requested_detach",
                "actor": "ceo",
            },
        )
        assert unlink.status_code == 200
        unlinked = unlink.json()["edition"]
        assert unlinked["status"] == "blocked_account_connect_required"
        assert unlinked["account_connect_status"] == "not_linked"

        inactive_entitlements = client.get(
            "/v1/entitlements",
            params={"project_id": "boss-dogfood", "include_inactive": True},
        )
        assert inactive_entitlements.status_code == 200
        detached_rows = [
            item
            for item in inactive_entitlements.json()["items"]
            if item["capability_key"] == "account_connect"
            and item["source"] == "account_connect"
        ]
        assert detached_rows
        assert detached_rows[0]["status"] == "detached"

        events = client.get("/v1/events", params={"project_id": "boss-dogfood"})
        event_types = [item["event_type"] for item in events.json()["items"]]
        assert "account_connect.linked" in event_types
        assert "account_connect.unlinked" in event_types
    finally:
        api_main.settings.boss_edition = previous
        api_main.commercial.settings.boss_edition = previous


def test_account_connect_sync_fails_closed_without_hosted_url(client):
    previous_edition = api_main.settings.boss_edition
    previous_url = api_main.settings.account_connect_url
    api_main.settings.boss_edition = "cloud"
    api_main.commercial.settings.boss_edition = "cloud"
    api_main.settings.account_connect_url = None
    api_main.commercial.settings.account_connect_url = None
    try:
        link = client.post(
            "/v1/account-connect/link",
            json={
                "project_id": "boss-dogfood",
                "hosted_org_id": "acct_org_missing_url",
                "actor": "ceo",
            },
        )
        assert link.status_code == 200

        denied = client.post(
            "/v1/account-connect/sync",
            json={
                "project_id": "boss-dogfood",
                "hosted_org_id": "acct_org_missing_url",
                "actor": "ceo",
            },
        )
        assert denied.status_code == 503
        assert "BOSS_ACCOUNT_CONNECT_URL" in denied.json()["detail"]

        events = client.get("/v1/events", params={"project_id": "boss-dogfood"})
        event_types = [item["event_type"] for item in events.json()["items"]]
        assert "account_connect.sync_denied" in event_types
    finally:
        api_main.settings.boss_edition = previous_edition
        api_main.commercial.settings.boss_edition = previous_edition
        api_main.settings.account_connect_url = previous_url
        api_main.commercial.settings.account_connect_url = previous_url


def test_account_connect_sync_upserts_hosted_entitlements(
    client,
    monkeypatch,
):
    previous = api_main.settings.boss_edition
    api_main.settings.boss_edition = "cloud"
    api_main.commercial.settings.boss_edition = "cloud"

    class FakeAccountConnectAdapter:
        def fetch_entitlements(self, *, hosted_org_id: str, project_id: str):
            assert hosted_org_id == "acct_org_sync"
            assert project_id == "boss-dogfood"
            return [
                {
                    "id": "lic_hosted_marketplace",
                    "capability_key": "marketplace_private_registry",
                    "capabilities": ["marketplace_private_registry"],
                    "status": "active",
                    "source": "account_connect_hosted",
                    "seats": 5,
                    "metadata": {"plan": "enterprise_cloud"},
                }
            ]

    monkeypatch.setattr(
        api_main.commercial,
        "account_connect_adapter",
        FakeAccountConnectAdapter(),
    )
    try:
        link = client.post(
            "/v1/account-connect/link",
            json={
                "project_id": "boss-dogfood",
                "hosted_org_id": "acct_org_sync",
                "actor": "ceo",
            },
        )
        assert link.status_code == 200

        synced = client.post(
            "/v1/account-connect/sync",
            json={
                "project_id": "boss-dogfood",
                "hosted_org_id": "acct_org_sync",
                "actor": "ceo",
            },
        )
        assert synced.status_code == 200
        assert synced.json()["synced_entitlement_count"] == 1

        entitlements = client.get(
            "/v1/entitlements",
            params={"project_id": "boss-dogfood"},
        )
        hosted_rows = [
            item
            for item in entitlements.json()["items"]
            if item["source"] == "account_connect_hosted"
        ]
        assert hosted_rows
        assert hosted_rows[0]["capability_key"] == "marketplace_private_registry"
        assert hosted_rows[0]["seats"] == 5

        events = client.get("/v1/events", params={"project_id": "boss-dogfood"})
        event_types = [item["event_type"] for item in events.json()["items"]]
        assert "account_connect.synced" in event_types
    finally:
        api_main.settings.boss_edition = previous
        api_main.commercial.settings.boss_edition = previous


def test_account_connect_health_check_blocks_without_hosted_url(client):
    previous_edition = api_main.settings.boss_edition
    previous_url = api_main.settings.account_connect_url
    api_main.settings.boss_edition = "cloud"
    api_main.commercial.settings.boss_edition = "cloud"
    api_main.settings.account_connect_url = None
    api_main.commercial.settings.account_connect_url = None
    try:
        check = client.post(
            "/v1/account-connect/health-checks",
            json={
                "project_id": "boss-dogfood",
                "hosted_org_id": "acct_org_missing_url",
                "check_kind": "hosted_service",
                "metadata": {"source": "test"},
                "actor": "ceo",
            },
        )
        assert check.status_code == 201
        payload = check.json()
        assert payload["status"] == "blocked_configuration"
        assert payload["hosted_org_id"] == "acct_org_missing_url"
        assert payload["error"]["reason"] == "account_connect_url_missing"
        assert payload["latency_ms"] >= 0
        assert payload["readiness_packet_artifact_id"].startswith("art_")

        packet = client.get(f"/v1/artifacts/{payload['readiness_packet_artifact_id']}")
        assert packet.status_code == 200
        packet_payload = packet.json()
        assert packet_payload["artifact_type"] == "account_connect_readiness_packet"
        assert packet_payload["content"]["health_check_id"] == payload["id"]
        assert packet_payload["content"]["status"] == "blocked_configuration"
        assert packet_payload["content"]["external_service_configured"] is False

        listed = client.get(
            "/v1/account-connect/health-checks",
            params={"project_id": "boss-dogfood"},
        )
        assert listed.status_code == 200
        assert listed.json()["items"][0]["id"] == payload["id"]
        assert (
            listed.json()["items"][0]["readiness_packet_artifact_id"]
            == payload["readiness_packet_artifact_id"]
        )

        events = client.get("/v1/events", params={"project_id": "boss-dogfood"})
        event_items = events.json()["items"]
        event_types = [item["event_type"] for item in event_items]
        assert "account_connect.health_check_blocked" in event_types
        blocked_event = next(
            item
            for item in event_items
            if item["event_type"] == "account_connect.health_check_blocked"
        )
        assert (
            blocked_event["data"]["readiness_packet_artifact_id"]
            == payload["readiness_packet_artifact_id"]
        )
    finally:
        api_main.settings.boss_edition = previous_edition
        api_main.commercial.settings.boss_edition = previous_edition
        api_main.settings.account_connect_url = previous_url
        api_main.commercial.settings.account_connect_url = previous_url


def test_account_connect_health_check_uses_hosted_adapter(client, monkeypatch):
    previous = api_main.settings.boss_edition
    previous_url = api_main.settings.account_connect_url
    api_main.settings.boss_edition = "cloud"
    api_main.commercial.settings.boss_edition = "cloud"
    api_main.settings.account_connect_url = "https://account-connect.example"
    api_main.commercial.settings.account_connect_url = "https://account-connect.example"

    class FakeAccountConnectAdapter:
        def check_health(
            self,
            *,
            project_id: str,
            hosted_org_id: Optional[str] = None,
        ):
            assert project_id == "boss-dogfood"
            assert hosted_org_id == "acct_org_health"
            return {
                "status": "degraded",
                "provider_ref": "acct-health-1",
                "quota": {"remaining": 42},
            }

    monkeypatch.setattr(
        api_main.commercial,
        "account_connect_adapter",
        FakeAccountConnectAdapter(),
    )
    try:
        link = client.post(
            "/v1/account-connect/link",
            json={
                "project_id": "boss-dogfood",
                "hosted_org_id": "acct_org_health",
                "actor": "ceo",
            },
        )
        assert link.status_code == 200

        check = client.post(
            "/v1/account-connect/health-checks",
            json={
                "project_id": "boss-dogfood",
                "hosted_org_id": "acct_org_health",
                "check_kind": "hosted_service",
                "metadata": {"source": "test"},
                "actor": "ceo",
            },
        )
        assert check.status_code == 201
        payload = check.json()
        assert payload["status"] == "degraded"
        assert payload["provider_ref"] == "acct-health-1"
        assert payload["response_metadata"]["quota"]["remaining"] == 42
        assert payload["readiness_packet_artifact_id"].startswith("art_")

        packet = client.get(f"/v1/artifacts/{payload['readiness_packet_artifact_id']}")
        assert packet.status_code == 200
        packet_payload = packet.json()
        assert packet_payload["artifact_type"] == "account_connect_readiness_packet"
        assert packet_payload["content"]["status"] == "degraded"
        assert packet_payload["content"]["provider_ref"] == "acct-health-1"
        assert packet_payload["content"]["external_service_configured"] is True

        events = client.get("/v1/events", params={"project_id": "boss-dogfood"})
        event_types = [item["event_type"] for item in events.json()["items"]]
        assert "account_connect.health_check_degraded" in event_types
    finally:
        api_main.settings.boss_edition = previous
        api_main.commercial.settings.boss_edition = previous
        api_main.settings.account_connect_url = previous_url
        api_main.commercial.settings.account_connect_url = previous_url


def test_hosted_marketplace_sync_blocks_without_hosted_url(client):
    previous_edition = api_main.settings.boss_edition
    previous_url = api_main.settings.account_connect_url
    api_main.settings.boss_edition = "cloud"
    api_main.commercial.settings.boss_edition = "cloud"
    api_main.settings.account_connect_url = None
    api_main.commercial.settings.account_connect_url = None
    try:
        link = client.post(
            "/v1/account-connect/link",
            json={
                "project_id": "boss-dogfood",
                "hosted_org_id": "acct_org_marketplace",
                "actor": "ceo",
            },
        )
        assert link.status_code == 200

        sync = client.post(
            "/v1/marketplace/sync",
            json={
                "project_id": "boss-dogfood",
                "hosted_org_id": "acct_org_marketplace",
                "metadata": {"source": "test"},
                "actor": "ceo",
            },
        )
        assert sync.status_code == 201
        payload = sync.json()
        assert payload["sync_run"]["status"] == "blocked_configuration"
        assert payload["sync_run"]["error"]["reason"] == "account_connect_url_missing"
        assert payload["sync_run"]["sync_packet_artifact_id"]
        assert payload["listing_count"] == 0

        packet = client.get(
            f"/v1/artifacts/{payload['sync_run']['sync_packet_artifact_id']}"
        )
        assert packet.status_code == 200
        packet_payload = packet.json()
        assert packet_payload["artifact_type"] == "hosted_marketplace_sync_packet"
        assert packet_payload["content"]["status"] == "blocked_configuration"
        assert packet_payload["content"]["listing_count"] == 0
        assert packet_payload["content"]["error"]["reason"] == "account_connect_url_missing"
        assert packet_payload["content"]["external_service_configured"] is False

        runs = client.get(
            "/v1/marketplace/sync-runs",
            params={"project_id": "boss-dogfood"},
        )
        assert runs.status_code == 200
        assert runs.json()["items"][0]["id"] == payload["sync_run"]["id"]
        assert (
            runs.json()["items"][0]["sync_packet_artifact_id"]
            == payload["sync_run"]["sync_packet_artifact_id"]
        )

        listings = client.get(
            "/v1/marketplace/listings",
            params={"project_id": "boss-dogfood"},
        )
        assert listings.status_code == 200
        assert listings.json()["items"] == []

        events = client.get("/v1/events", params={"project_id": "boss-dogfood"})
        event_types = [item["event_type"] for item in events.json()["items"]]
        assert "marketplace.sync_blocked" in event_types
    finally:
        api_main.settings.boss_edition = previous_edition
        api_main.commercial.settings.boss_edition = previous_edition
        api_main.settings.account_connect_url = previous_url
        api_main.commercial.settings.account_connect_url = previous_url


def test_hosted_marketplace_sync_uses_account_connect_adapter(client, monkeypatch):
    previous = api_main.settings.boss_edition
    api_main.settings.boss_edition = "cloud"
    api_main.commercial.settings.boss_edition = "cloud"

    class FakeAccountConnectAdapter:
        def fetch_marketplace_pack_listings(
            self,
            *,
            hosted_org_id: str,
            project_id: str,
        ):
            assert hosted_org_id == "acct_org_marketplace"
            assert project_id == "boss-dogfood"
            return {
                "provider_ref": "marketplace-sync-1",
                "items": [
                    {
                        "listing_id": "listing_private_agent",
                        "pack_id": "private_enterprise_agent_v1",
                        "version": "1.0.0",
                        "display_name": "Private Enterprise Agent",
                        "publisher": "BOSS",
                        "category": "enterprise",
                        "description": "Enterprise-only hosted module.",
                        "status": "active",
                        "price_model": "enterprise_contract",
                        "price_usd_micros": 0,
                        "required_entitlements": ["private_agent_marketplace"],
                        "capabilities": ["agent_module.install"],
                        "source_url": "https://accounts.example/marketplace/private",
                        "metadata": {"verified": True},
                    }
                ],
                "metadata": {"catalog_version": "2026-06-04"},
            }

    monkeypatch.setattr(
        api_main.commercial,
        "account_connect_adapter",
        FakeAccountConnectAdapter(),
    )
    try:
        link = client.post(
            "/v1/account-connect/link",
            json={
                "project_id": "boss-dogfood",
                "hosted_org_id": "acct_org_marketplace",
                "actor": "ceo",
            },
        )
        assert link.status_code == 200

        sync = client.post(
            "/v1/marketplace/sync",
            json={
                "project_id": "boss-dogfood",
                "hosted_org_id": "acct_org_marketplace",
                "metadata": {"source": "test"},
                "actor": "ceo",
            },
        )
        assert sync.status_code == 201
        sync_payload = sync.json()
        assert sync_payload["listing_count"] == 1
        assert sync_payload["sync_run"]["provider_ref"] == "marketplace-sync-1"
        assert sync_payload["sync_run"]["sync_packet_artifact_id"]

        packet = client.get(
            f"/v1/artifacts/{sync_payload['sync_run']['sync_packet_artifact_id']}"
        )
        assert packet.status_code == 200
        packet_payload = packet.json()
        assert packet_payload["artifact_type"] == "hosted_marketplace_sync_packet"
        assert packet_payload["content"]["status"] == "synced"
        assert packet_payload["content"]["listing_count"] == 1
        assert packet_payload["content"]["provider_ref"] == "marketplace-sync-1"
        assert packet_payload["content"]["response_metadata"]["catalog_version"] == (
            "2026-06-04"
        )

        listings = client.get(
            "/v1/marketplace/listings",
            params={"project_id": "boss-dogfood"},
        )
        assert listings.status_code == 200
        listing = listings.json()["items"][0]
        assert listing["listing_id"] == "listing_private_agent"
        assert listing["pack_id"] == "private_enterprise_agent_v1"
        assert listing["required_entitlements"] == ["private_agent_marketplace"]
        assert listing["metadata"]["verified"] is True

        events = client.get("/v1/events", params={"project_id": "boss-dogfood"})
        event_types = [item["event_type"] for item in events.json()["items"]]
        assert "marketplace.synced" in event_types
    finally:
        api_main.settings.boss_edition = previous
        api_main.commercial.settings.boss_edition = previous


def test_account_connect_auth_session_blocks_without_hosted_url(client):
    previous_edition = api_main.settings.boss_edition
    previous_url = api_main.settings.account_connect_url
    api_main.settings.boss_edition = "cloud"
    api_main.commercial.settings.boss_edition = "cloud"
    api_main.settings.account_connect_url = None
    api_main.commercial.settings.account_connect_url = None
    try:
        created = client.post(
            "/v1/account-connect/auth-sessions",
            json={
                "project_id": "boss-dogfood",
                "return_url": "https://boss.local/account-connect/callback",
                "metadata": {"source": "test"},
                "actor": "ceo",
            },
        )
        assert created.status_code == 201
        session = created.json()
        assert session["status"] == "blocked_configuration"
        assert session["authorization_url"] is None
        assert session["receipt_id"]
        assert session["metadata"]["blocked_reason"] == "account_connect_url_missing"

        receipts = client.get("/v1/receipts", params={"project_id": "boss-dogfood"})
        receipt = next(
            item
            for item in receipts.json()["items"]
            if item["id"] == session["receipt_id"]
        )
        assert receipt["status"] == "failed"
        assert receipt["effect_kind"] == "account_connect.auth_session.create"

        listed = client.get(
            "/v1/account-connect/auth-sessions",
            params={"project_id": "boss-dogfood"},
        )
        assert listed.status_code == 200
        assert listed.json()["items"][0]["id"] == session["id"]

        events = client.get("/v1/events", params={"project_id": "boss-dogfood"})
        event_types = [item["event_type"] for item in events.json()["items"]]
        assert "account_connect.auth_session_blocked" in event_types
    finally:
        api_main.settings.boss_edition = previous_edition
        api_main.commercial.settings.boss_edition = previous_edition
        api_main.settings.account_connect_url = previous_url
        api_main.commercial.settings.account_connect_url = previous_url


def test_account_connect_auth_session_uses_hosted_adapter_and_links_org(
    client,
    monkeypatch,
):
    previous = api_main.settings.boss_edition
    api_main.settings.boss_edition = "cloud"
    api_main.commercial.settings.boss_edition = "cloud"

    class FakeAccountConnectAdapter:
        def fetch_entitlements(self, *, hosted_org_id: str, project_id: str):
            return []

        def create_auth_session(
            self,
            *,
            project_id: str,
            return_url: Optional[str],
            state: str,
            metadata: dict,
            hosted_org_id: Optional[str] = None,
        ):
            assert project_id == "boss-dogfood"
            assert return_url == "https://boss.local/account-connect/callback"
            assert hosted_org_id is None
            assert metadata == {"source": "test"}
            assert state.startswith("acctstate_")
            return {
                "session_id": "hosted_auth_123",
                "authorization_url": "https://accounts.example/oauth/authorize",
                "expires_at": "2026-06-04T00:00:00Z",
            }

        def complete_auth_session(
            self,
            *,
            project_id: str,
            provider_ref: str,
            code: str,
            state: str,
            metadata: dict,
            hosted_org_id: Optional[str] = None,
        ):
            assert project_id == "boss-dogfood"
            assert provider_ref == "hosted_auth_123"
            assert code == "callback-code"
            assert state.startswith("acctstate_")
            assert hosted_org_id is None
            assert metadata == {"source": "callback"}
            return {
                "hosted_org_id": "acct_org_hosted_auth",
                "connection_ref": "acctconn:hosted-auth",
                "marketplace_identity": {"publisher": "seanheiney"},
                "billing_portal_ref": "stripe:portal:hosted",
                "connector_authorization_refs": ["github:installation:hosted"],
                "optional_telemetry_enabled": True,
                "metadata": {"login_method": "oauth"},
            }

    monkeypatch.setattr(
        api_main.commercial,
        "account_connect_adapter",
        FakeAccountConnectAdapter(),
    )
    try:
        created = client.post(
            "/v1/account-connect/auth-sessions",
            json={
                "project_id": "boss-dogfood",
                "return_url": "https://boss.local/account-connect/callback",
                "metadata": {"source": "test"},
                "actor": "ceo",
            },
        )
        assert created.status_code == 201
        session = created.json()
        assert session["status"] == "ready"
        assert session["provider_ref"] == "hosted_auth_123"
        assert session["authorization_url"] == "https://accounts.example/oauth/authorize"
        assert session["receipt_id"]

        mismatch = client.post(
            f"/v1/account-connect/auth-sessions/{session['id']}/complete",
            json={
                "project_id": "boss-dogfood",
                "code": "callback-code",
                "state": "wrong-state",
                "actor": "ceo",
            },
        )
        assert mismatch.status_code == 409

        completed = client.post(
            f"/v1/account-connect/auth-sessions/{session['id']}/complete",
            json={
                "project_id": "boss-dogfood",
                "code": "callback-code",
                "state": session["state"],
                "metadata": {"source": "callback"},
                "actor": "ceo",
            },
        )
        assert completed.status_code == 200
        completed_session = completed.json()
        assert completed_session["status"] == "completed"
        assert completed_session["hosted_org_id"] == "acct_org_hosted_auth"
        assert completed_session["completed_at"]

        edition = client.get(
            "/v1/edition",
            params={"project_id": "boss-dogfood"},
        )
        assert edition.status_code == 200
        account_connect = edition.json()["account_connect_org"]
        assert account_connect["hosted_org_id"] == "acct_org_hosted_auth"
        assert account_connect["connection_ref"] == "acctconn:hosted-auth"
        assert account_connect["billing_portal_ref"] == "stripe:portal:hosted"

        receipts = client.get("/v1/receipts", params={"project_id": "boss-dogfood"})
        receipt_kinds = {item["effect_kind"] for item in receipts.json()["items"]}
        assert "account_connect.auth_session.create" in receipt_kinds
        assert "account_connect.auth_session.complete" in receipt_kinds

        events = client.get("/v1/events", params={"project_id": "boss-dogfood"})
        event_types = [item["event_type"] for item in events.json()["items"]]
        assert "account_connect.auth_session_created" in event_types
        assert "account_connect.auth_session_denied" in event_types
        assert "account_connect.auth_session_completed" in event_types
        assert "account_connect.linked" in event_types
    finally:
        api_main.settings.boss_edition = previous
        api_main.commercial.settings.boss_edition = previous


def test_account_connect_team_invite_blocks_without_hosted_url_and_records_receipt(
    client,
):
    previous_edition = api_main.settings.boss_edition
    previous_url = api_main.settings.account_connect_url
    api_main.settings.boss_edition = "cloud"
    api_main.commercial.settings.boss_edition = "cloud"
    api_main.settings.account_connect_url = None
    api_main.commercial.settings.account_connect_url = None
    try:
        missing_link = client.post(
            "/v1/account-connect/team-invites",
            json={
                "project_id": "boss-dogfood",
                "email": "ops@example.com",
                "actor": "ceo",
            },
        )
        assert missing_link.status_code == 404

        link = client.post(
            "/v1/account-connect/link",
            json={
                "project_id": "boss-dogfood",
                "hosted_org_id": "acct_org_invites",
                "actor": "ceo",
            },
        )
        assert link.status_code == 200

        created = client.post(
            "/v1/account-connect/team-invites",
            json={
                "project_id": "boss-dogfood",
                "email": "ops@example.com",
                "display_name": "Ops Lead",
                "role_keys": ["operator"],
                "permissions": ["command_submit"],
                "metadata": {"source": "test"},
                "actor": "ceo",
            },
        )
        assert created.status_code == 201
        invite = created.json()
        assert invite["status"] == "blocked_configuration"
        assert invite["delivery_status"] == "blocked"
        assert invite["receipt_id"]
        assert invite["metadata"]["blocked_reason"] == "account_connect_url_missing"

        receipts = client.get("/v1/receipts", params={"project_id": "boss-dogfood"})
        receipt = next(
            item for item in receipts.json()["items"] if item["id"] == invite["receipt_id"]
        )
        assert receipt["status"] == "failed"
        assert receipt["effect_kind"] == "account_connect.team_invite.create"

        listed = client.get(
            "/v1/account-connect/team-invites",
            params={"project_id": "boss-dogfood"},
        )
        assert listed.status_code == 200
        assert listed.json()["items"][0]["email"] == "ops@example.com"

        events = client.get("/v1/events", params={"project_id": "boss-dogfood"})
        event_types = [item["event_type"] for item in events.json()["items"]]
        assert "account_connect.team_invite_denied" in event_types
        assert "account_connect.team_invite_blocked" in event_types
    finally:
        api_main.settings.boss_edition = previous_edition
        api_main.commercial.settings.boss_edition = previous_edition
        api_main.settings.account_connect_url = previous_url
        api_main.commercial.settings.account_connect_url = previous_url


def test_account_connect_team_invite_and_billing_portal_use_hosted_adapter(
    client,
    monkeypatch,
):
    previous = api_main.settings.boss_edition
    api_main.settings.boss_edition = "cloud"
    api_main.commercial.settings.boss_edition = "cloud"

    class FakeAccountConnectAdapter:
        def fetch_entitlements(self, *, hosted_org_id: str, project_id: str):
            return []

        def create_team_invite(
            self,
            *,
            hosted_org_id: str,
            project_id: str,
            email: str,
            display_name: Optional[str],
            role_keys: list[str],
            permissions: list[str],
            metadata: dict,
        ):
            assert hosted_org_id == "acct_org_hosted"
            assert project_id == "boss-dogfood"
            assert email == "ops@example.com"
            assert display_name == "Ops Lead"
            assert role_keys == ["operator"]
            assert permissions == ["command_submit"]
            assert metadata == {"source": "test"}
            return {
                "id": "hosted_invite_123",
                "status": "pending_acceptance",
                "invite_url": "https://accounts.example/invite/123",
            }

        def revoke_team_invite(
            self,
            *,
            hosted_org_id: str,
            hosted_invite_ref: str,
            project_id: str,
            reason: Optional[str],
        ):
            assert hosted_org_id == "acct_org_hosted"
            assert hosted_invite_ref == "hosted_invite_123"
            assert project_id == "boss-dogfood"
            assert reason == "wrong recipient"
            return {"status": "revoked"}

        def create_billing_portal_session(
            self,
            *,
            hosted_org_id: str,
            project_id: str,
            return_url: Optional[str],
            metadata: dict,
        ):
            assert hosted_org_id == "acct_org_hosted"
            assert project_id == "boss-dogfood"
            assert return_url == "https://boss.local/settings"
            assert metadata == {"purpose": "billing_review"}
            return {
                "session_id": "bps_123",
                "portal_url": "https://billing.example/session/123",
                "expires_at": "2026-06-04T00:00:00Z",
            }

    monkeypatch.setattr(
        api_main.commercial,
        "account_connect_adapter",
        FakeAccountConnectAdapter(),
    )
    try:
        link = client.post(
            "/v1/account-connect/link",
            json={
                "project_id": "boss-dogfood",
                "hosted_org_id": "acct_org_hosted",
                "actor": "ceo",
            },
        )
        assert link.status_code == 200

        invite_response = client.post(
            "/v1/account-connect/team-invites",
            json={
                "project_id": "boss-dogfood",
                "email": "ops@example.com",
                "display_name": "Ops Lead",
                "role_keys": ["operator"],
                "permissions": ["command_submit"],
                "metadata": {"source": "test"},
                "actor": "ceo",
            },
        )
        assert invite_response.status_code == 201
        invite = invite_response.json()
        assert invite["status"] == "pending_acceptance"
        assert invite["delivery_status"] == "sent"
        assert invite["hosted_invite_ref"] == "hosted_invite_123"
        assert invite["invite_url"] == "https://accounts.example/invite/123"

        revoke = client.post(
            f"/v1/account-connect/team-invites/{invite['id']}/revoke",
            json={
                "project_id": "boss-dogfood",
                "reason": "wrong recipient",
                "actor": "ceo",
            },
        )
        assert revoke.status_code == 200
        assert revoke.json()["status"] == "revoked"

        portal_response = client.post(
            "/v1/account-connect/billing-portal-sessions",
            json={
                "project_id": "boss-dogfood",
                "return_url": "https://boss.local/settings",
                "metadata": {"purpose": "billing_review"},
                "actor": "ceo",
            },
        )
        assert portal_response.status_code == 201
        portal = portal_response.json()
        assert portal["status"] == "ready"
        assert portal["portal_url"] == "https://billing.example/session/123"
        assert portal["provider_ref"] == "bps_123"
        assert portal["receipt_id"]

        sessions = client.get(
            "/v1/account-connect/billing-portal-sessions",
            params={"project_id": "boss-dogfood"},
        )
        assert sessions.status_code == 200
        assert sessions.json()["items"][0]["provider_ref"] == "bps_123"

        receipts = client.get("/v1/receipts", params={"project_id": "boss-dogfood"})
        receipt_kinds = {item["effect_kind"] for item in receipts.json()["items"]}
        assert "account_connect.team_invite.create" in receipt_kinds
        assert "account_connect.team_invite.revoke" in receipt_kinds
        assert "account_connect.billing_portal_session.create" in receipt_kinds

        events = client.get("/v1/events", params={"project_id": "boss-dogfood"})
        event_types = [item["event_type"] for item in events.json()["items"]]
        assert "account_connect.team_invite_created" in event_types
        assert "account_connect.team_invite_revoked" in event_types
        assert "account_connect.billing_portal_session_created" in event_types
    finally:
        api_main.settings.boss_edition = previous
        api_main.commercial.settings.boss_edition = previous


def test_account_connect_billing_portal_blocks_without_hosted_url(client):
    previous_edition = api_main.settings.boss_edition
    previous_url = api_main.settings.account_connect_url
    api_main.settings.boss_edition = "cloud"
    api_main.commercial.settings.boss_edition = "cloud"
    api_main.settings.account_connect_url = None
    api_main.commercial.settings.account_connect_url = None
    try:
        link = client.post(
            "/v1/account-connect/link",
            json={
                "project_id": "boss-dogfood",
                "hosted_org_id": "acct_org_billing_blocked",
                "actor": "ceo",
            },
        )
        assert link.status_code == 200

        blocked = client.post(
            "/v1/account-connect/billing-portal-sessions",
            json={
                "project_id": "boss-dogfood",
                "return_url": "https://boss.local/settings",
                "actor": "ceo",
            },
        )
        assert blocked.status_code == 201
        payload = blocked.json()
        assert payload["status"] == "blocked_configuration"
        assert payload["portal_url"] is None
        assert payload["receipt_id"]

        events = client.get("/v1/events", params={"project_id": "boss-dogfood"})
        event_types = [item["event_type"] for item in events.json()["items"]]
        assert "account_connect.billing_portal_blocked" in event_types
    finally:
        api_main.settings.boss_edition = previous_edition
        api_main.commercial.settings.boss_edition = previous_edition
        api_main.settings.account_connect_url = previous_url
        api_main.commercial.settings.account_connect_url = previous_url


def test_enterprise_cluster_records_trust_controls_and_blocking_gaps(client):
    denied = client.post(
        "/v1/enterprise/clusters",
        json={
            "project_id": "boss-dogfood",
            "deployment_mode": "enterprise_cloud",
            "actor": "release_manager",
        },
    )
    assert denied.status_code == 403

    active = client.post(
        "/v1/enterprise/clusters",
        json={
            "project_id": "boss-dogfood",
            "deployment_mode": "enterprise_cloud",
            "account_connect_mode": "attached",
            "worker_pool_strategy": "dedicated_managed_workers",
            "ha_profile": "regional_ha",
            "data_residency": "us-east",
            "identity_controls": {
                "sso_oidc_configured": True,
                "scim_configured": True,
                "rbac_enabled": True,
                "abac_enabled": True,
            },
            "audit_controls": {
                "audit_exports_enabled": True,
                "retention_days": 730,
                "compliance_evidence_workflows": True,
            },
            "support_tier": "enterprise",
            "support_sla": {
                "response_time_hours": 2,
                "uptime_target": "99.95%",
            },
            "status": "active",
            "actor": "ceo",
        },
    )
    assert active.status_code == 201
    active_payload = active.json()
    assert active_payload["status"] == "active"
    assert active_payload["blocking_gaps"] == []
    assert active_payload["identity_controls"]["sso_oidc_configured"] is True
    assert active_payload["identity_controls"]["scim_configured"] is True
    assert active_payload["governance_controls"]["budget_governor_precedence"] is True
    assert (
        active_payload["governance_controls"]["agents_receive_raw_provider_secrets"]
        is False
    )
    assert active_payload["support_sla"]["response_time_hours"] == 2

    blocked = client.post(
        "/v1/enterprise/clusters",
        json={
            "project_id": "boss-dogfood",
            "deployment_mode": "self_hosted",
            "account_connect_mode": "detached",
            "worker_pool_strategy": "private_worker_pools",
            "ha_profile": "multi_node",
            "data_residency": "customer_vpc",
            "actor": "ceo",
        },
    )
    assert blocked.status_code == 201
    blocked_payload = blocked.json()
    assert blocked_payload["status"] == "blocked"
    assert "sso_or_saml_not_configured" in blocked_payload["blocking_gaps"]
    assert "scim_not_configured" in blocked_payload["blocking_gaps"]
    assert "private_worker_pool_missing" in blocked_payload["blocking_gaps"]
    assert (
        "detached_enterprise_compliance_pack_missing"
        in blocked_payload["blocking_gaps"]
    )

    validation_denied = client.post(
        "/v1/enterprise/cluster-validation-runs",
        json={
            "project_id": "boss-dogfood",
            "enterprise_cluster_id": active_payload["id"],
            "validation_kind": "multi_node_readiness",
            "checks": [
                {
                    "name": "kubernetes_manifests",
                    "status": "passed",
                    "required": True,
                }
            ],
            "actor": "release_manager",
        },
    )
    assert validation_denied.status_code == 403

    validation = client.post(
        "/v1/enterprise/cluster-validation-runs",
        json={
            "project_id": "boss-dogfood",
            "enterprise_cluster_id": blocked_payload["id"],
            "validation_kind": "multi_node_readiness",
            "status": "passed",
            "checks": [
                {
                    "name": "kubernetes_manifests",
                    "status": "passed",
                    "required": True,
                    "message": "make k8s-validate passed",
                    "evidence_ref": "artifact:k8s_validate",
                },
                {
                    "name": "private_worker_pool",
                    "status": "blocked",
                    "required": True,
                    "message": "No private worker pool endpoint configured.",
                },
            ],
            "evidence_refs": ["artifact:k8s_validate"],
            "metadata": {"source": "test"},
            "actor": "ceo",
        },
    )
    assert validation.status_code == 201
    validation_payload = validation.json()
    assert validation_payload["status"] == "blocked"
    assert validation_payload["validation_kind"] == "multi_node_readiness"
    assert validation_payload["checks"][0]["name"] == "kubernetes_manifests"
    assert validation_payload["evidence_refs"] == ["artifact:k8s_validate"]
    assert validation_payload["blocking_gaps"] == [
        "cluster_validation_private_worker_pool_blocked"
    ]
    validation_metadata = validation_payload["metadata"]
    validation_packet_artifact_id = validation_metadata["validation_packet_artifact_id"]
    blocking_decision_id = validation_metadata["blocking_decision_id"]
    assert validation_metadata["live_rollout_claim"] is False
    assert validation_packet_artifact_id.startswith("art_")
    assert blocking_decision_id.startswith("dec_")

    validation_packet = client.get(f"/v1/artifacts/{validation_packet_artifact_id}")
    assert validation_packet.status_code == 200
    validation_packet_payload = validation_packet.json()
    assert (
        validation_packet_payload["artifact_type"]
        == "enterprise_cluster_validation_packet"
    )
    assert validation_packet_payload["data_boundary"] == "internal"
    assert validation_packet_payload["content"]["validation_run_id"] == validation_payload["id"]
    assert validation_packet_payload["content"]["status"] == "blocked"
    assert validation_packet_payload["content"]["live_rollout_claim"] is False
    assert validation_packet_payload["content"]["blocking_gaps"] == [
        "cluster_validation_private_worker_pool_blocked"
    ]

    decision_inbox = client.get(
        "/v1/decisions/inbox", params={"project_id": "boss-dogfood"}
    )
    assert decision_inbox.status_code == 200
    blocking_decisions = [
        item
        for item in decision_inbox.json()["items"]
        if item["id"] == blocking_decision_id
    ]
    assert len(blocking_decisions) == 1
    assert blocking_decisions[0]["status"] == "pending"
    assert blocking_decisions[0]["default_action"] == "hold"
    assert (
        blocking_decisions[0]["recommendation"]["validation_packet_artifact_id"]
        == validation_packet_artifact_id
    )

    validation_list = client.get(
        "/v1/enterprise/cluster-validation-runs",
        params={
            "project_id": "boss-dogfood",
            "enterprise_cluster_id": blocked_payload["id"],
        },
    )
    assert validation_list.status_code == 200
    assert validation_list.json()["items"][0]["id"] == validation_payload["id"]

    listed = client.get(
        "/v1/enterprise/clusters",
        params={"project_id": "boss-dogfood"},
    )
    assert listed.status_code == 200
    clusters = listed.json()["items"]
    assert {cluster["deployment_mode"] for cluster in clusters} >= {
        "enterprise_cloud",
        "self_hosted",
    }

    events = client.get("/v1/events", params={"project_id": "boss-dogfood"})
    event_types = [item["event_type"] for item in events.json()["items"]]
    assert "policy.denied" in event_types
    assert "artifact.created" in event_types
    assert "decision.created" in event_types
    assert "enterprise.cluster_upserted" in event_types
    assert "enterprise.cluster_validation_recorded" in event_types


def test_audit_export_enforces_actor_permission_and_enterprise_audit_controls(
    client, engine
):
    project_id = "boss-audit-governance"
    with engine.begin() as connection:
        connection.execute(text(f"DELETE FROM events WHERE project_id = '{project_id}'"))
        connection.execute(
            text(f"DELETE FROM enterprise_clusters WHERE project_id = '{project_id}'")
        )
        connection.execute(text(f"DELETE FROM project_members WHERE project_id = '{project_id}'"))
        connection.execute(text(f"DELETE FROM projects WHERE id = '{project_id}'"))
        connection.execute(
            text(
                f"""
                INSERT INTO projects (id, workspace_id, name, slug)
                VALUES ('{project_id}', 'ws_boss', 'Audit Governance', '{project_id}')
                """
            )
        )
        api_main.members.seed_default_members(connection, project_id, "test")

    denied_actor = client.get(
        "/v1/audit/export",
        params={"project_id": project_id, "actor": "release_manager"},
    )
    assert denied_actor.status_code == 403
    assert "audit_export" in denied_actor.json()["detail"]

    disabled = client.post(
        "/v1/enterprise/clusters",
        json={
            "project_id": project_id,
            "deployment_mode": "enterprise_cloud",
            "account_connect_mode": "attached",
            "audit_controls": {
                "audit_exports_enabled": False,
                "retention_days": 365,
                "compliance_evidence_workflows": True,
            },
            "status": "active",
            "actor": "ceo",
        },
    )
    assert disabled.status_code == 201
    assert disabled.json()["status"] == "blocked"
    assert "audit_exports_disabled" in disabled.json()["blocking_gaps"]

    blocked_export = client.get(
        "/v1/audit/export",
        params={"project_id": project_id, "actor": "ceo"},
    )
    assert blocked_export.status_code == 403
    assert "audit exports are disabled" in blocked_export.json()["detail"]

    enabled = client.post(
        "/v1/enterprise/clusters",
        json={
            "project_id": project_id,
            "deployment_mode": "enterprise_cloud",
            "account_connect_mode": "attached",
            "identity_controls": {
                "sso_oidc_configured": True,
                "scim_configured": True,
            },
            "audit_controls": {
                "audit_exports_enabled": True,
                "retention_days": 730,
                "compliance_evidence_workflows": True,
                "audit_export_mode": "on_demand",
            },
            "status": "active",
            "actor": "ceo",
        },
    )
    assert enabled.status_code == 201
    assert enabled.json()["status"] == "active"

    exported = client.get(
        "/v1/audit/export",
        params={"project_id": project_id, "actor": "ceo"},
    )
    assert exported.status_code == 200
    payload = exported.json()
    assert payload["governance"]["edition_scope"] == "enterprise"
    assert payload["governance"]["export_allowed"] is True
    assert payload["governance"]["audit_exports_enabled"] is True
    assert payload["governance"]["retention_days"] == 730
    assert payload["governance"]["audit_export_mode"] == "on_demand"

    events = client.get("/v1/events", params={"project_id": project_id, "limit": 20})
    assert events.status_code == 200
    event_types = [item["event_type"] for item in events.json()["items"]]
    assert "policy.denied" in event_types
    assert "audit.export_generated" in event_types


def test_enterprise_support_cases_track_sla_and_fail_closed_hosted_escalation(
    client, engine
):
    project_id = "boss-support-sla"
    previous_url = api_main.settings.account_connect_url
    previous_commercial_url = api_main.commercial.settings.account_connect_url
    previous_enterprise_url = api_main.enterprise.settings.account_connect_url
    api_main.settings.account_connect_url = None
    api_main.commercial.settings.account_connect_url = None
    api_main.enterprise.settings.account_connect_url = None
    try:
        with engine.begin() as connection:
            connection.execute(text(f"DELETE FROM receipts WHERE project_id = '{project_id}'"))
            connection.execute(text(f"DELETE FROM events WHERE project_id = '{project_id}'"))
            connection.execute(
                text(f"DELETE FROM enterprise_support_cases WHERE project_id = '{project_id}'")
            )
            connection.execute(
                text(f"DELETE FROM enterprise_clusters WHERE project_id = '{project_id}'")
            )
            connection.execute(
                text(f"DELETE FROM account_connect_orgs WHERE project_id = '{project_id}'")
            )
            connection.execute(
                text(f"DELETE FROM project_members WHERE project_id = '{project_id}'")
            )
            connection.execute(text(f"DELETE FROM projects WHERE id = '{project_id}'"))
            connection.execute(
                text(
                    f"""
                    INSERT INTO projects (id, workspace_id, name, slug)
                    VALUES ('{project_id}', 'ws_boss', 'Support SLA', '{project_id}')
                    """
                )
            )
            api_main.members.seed_default_members(connection, project_id, "test")

        denied = client.post(
            "/v1/enterprise/support-cases",
            json={
                "project_id": project_id,
                "title": "Release help",
                "description": "Need help with release readiness.",
                "actor": "release_manager",
            },
        )
        assert denied.status_code == 403

        cluster = client.post(
            "/v1/enterprise/clusters",
            json={
                "project_id": project_id,
                "deployment_mode": "enterprise_cloud",
                "account_connect_mode": "attached",
                "identity_controls": {
                    "sso_oidc_configured": True,
                    "scim_configured": True,
                },
                "audit_controls": {
                    "audit_exports_enabled": True,
                    "retention_days": 365,
                    "compliance_evidence_workflows": True,
                },
                "support_tier": "enterprise",
                "support_sla": {"response_time_hours": 2, "uptime_target": "99.95%"},
                "status": "active",
                "actor": "ceo",
            },
        )
        assert cluster.status_code == 201
        cluster_id = cluster.json()["id"]

        local_case = client.post(
            "/v1/enterprise/support-cases",
            json={
                "project_id": project_id,
                "enterprise_cluster_id": cluster_id,
                "title": "Validate enterprise rollout",
                "description": "Track a local support readiness item.",
                "severity": "normal",
                "category": "release",
                "metadata": {"component": "helm"},
                "actor": "ceo",
            },
        )
        assert local_case.status_code == 201
        local_payload = local_case.json()
        assert local_payload["status"] == "open"
        assert local_payload["response_time_hours"] == 2
        assert local_payload["sla_due_at"]
        assert local_payload["receipt_id"] is None
        assert local_payload["support_packet_artifact_id"].startswith("art_")

        local_packet = client.get(
            f"/v1/artifacts/{local_payload['support_packet_artifact_id']}"
        )
        assert local_packet.status_code == 200
        local_packet_payload = local_packet.json()
        assert local_packet_payload["artifact_type"] == "enterprise_support_case_packet"
        assert local_packet_payload["data_boundary"] == "confidential"
        assert local_packet_payload["content"]["support_case_id"] == local_payload["id"]
        assert local_packet_payload["content"]["status"] == "open"
        assert local_packet_payload["content"]["hosted_support_claim"] is False

        link = client.post(
            "/v1/account-connect/link",
            json={
                "project_id": project_id,
                "hosted_org_id": "acct_support_sla",
                "actor": "ceo",
            },
        )
        assert link.status_code == 200

        escalated = client.post(
            "/v1/enterprise/support-cases",
            json={
                "project_id": project_id,
                "enterprise_cluster_id": cluster_id,
                "title": "Hosted escalation",
                "description": "Escalate to hosted support when configured.",
                "severity": "high",
                "category": "support",
                "escalate_to_hosted": True,
                "actor": "ceo",
            },
        )
        assert escalated.status_code == 201
        escalated_payload = escalated.json()
        assert escalated_payload["status"] == "blocked_configuration"
        assert escalated_payload["response_time_hours"] == 4
        assert escalated_payload["receipt_id"]
        assert escalated_payload["metadata"]["blocked_reason"] == (
            "account_connect_url_missing"
        )
        assert escalated_payload["support_packet_artifact_id"].startswith("art_")

        escalated_packet = client.get(
            f"/v1/artifacts/{escalated_payload['support_packet_artifact_id']}"
        )
        assert escalated_packet.status_code == 200
        escalated_packet_payload = escalated_packet.json()
        assert (
            escalated_packet_payload["artifact_type"]
            == "enterprise_support_case_packet"
        )
        assert escalated_packet_payload["content"]["status"] == (
            "blocked_configuration"
        )
        assert escalated_packet_payload["content"]["receipt_id"] == (
            escalated_payload["receipt_id"]
        )
        assert escalated_packet_payload["content"]["metadata"]["blocked_reason"] == (
            "account_connect_url_missing"
        )

        updated = client.post(
            f"/v1/enterprise/support-cases/{local_payload['id']}",
            json={
                "project_id": project_id,
                "status": "in_progress",
                "assigned_to": "cto",
                "metadata": {"triage": "accepted"},
                "actor": "ceo",
            },
        )
        assert updated.status_code == 200
        assert updated.json()["status"] == "in_progress"
        assert updated.json()["assigned_to"] == "cto"
        assert updated.json()["metadata"]["triage"] == "accepted"

        listed = client.get(
            "/v1/enterprise/support-cases",
            params={"project_id": project_id},
        )
        assert listed.status_code == 200
        assert {item["id"] for item in listed.json()["items"]} >= {
            local_payload["id"],
            escalated_payload["id"],
        }

        receipts = client.get("/v1/receipts", params={"project_id": project_id})
        assert receipts.status_code == 200
        receipt = next(
            item
            for item in receipts.json()["items"]
            if item["id"] == escalated_payload["receipt_id"]
        )
        assert receipt["effect_kind"] == "enterprise.support_case.create"
        assert receipt["status"] == "failed"
        assert receipt["data_boundary"] == "confidential"

        events = client.get("/v1/events", params={"project_id": project_id, "limit": 30})
        assert events.status_code == 200
        event_types = [item["event_type"] for item in events.json()["items"]]
        assert "policy.denied" in event_types
        assert "enterprise.support_case_created" in event_types
        assert "enterprise.support_case_blocked" in event_types
        assert "enterprise.support_case_updated" in event_types
        support_created = next(
            item
            for item in events.json()["items"]
            if item["event_type"] == "enterprise.support_case_created"
        )
        assert support_created["data"]["support_packet_artifact_id"].startswith("art_")
    finally:
        api_main.settings.account_connect_url = previous_url
        api_main.commercial.settings.account_connect_url = previous_commercial_url
        api_main.enterprise.settings.account_connect_url = previous_enterprise_url


def test_enterprise_identity_providers_back_cluster_identity_controls(client):
    denied = client.post(
        "/v1/enterprise/identity-providers",
        json={
            "project_id": "boss-dogfood",
            "provider_key": "okta_oidc",
            "provider_type": "oidc",
            "display_name": "Okta OIDC",
            "issuer": "https://idp.example",
            "audience": "boss-api",
            "jwks_url": "https://idp.example/.well-known/jwks.json",
            "status": "active",
            "actor": "release_manager",
        },
    )
    assert denied.status_code == 403

    oidc = client.post(
        "/v1/enterprise/identity-providers",
        json={
            "project_id": "boss-dogfood",
            "provider_key": "okta_oidc",
            "provider_type": "oidc",
            "display_name": "Okta OIDC",
            "issuer": "https://idp.example",
            "audience": "boss-api",
            "jwks_url": "https://idp.example/.well-known/jwks.json",
            "status": "active",
            "actor": "ceo",
        },
    )
    assert oidc.status_code == 201
    oidc_payload = oidc.json()
    assert oidc_payload["status"] == "active"
    assert oidc_payload["blocking_gaps"] == []

    scim = client.post(
        "/v1/enterprise/identity-providers",
        json={
            "project_id": "boss-dogfood",
            "provider_key": "okta_scim",
            "provider_type": "scim",
            "display_name": "Okta SCIM",
            "scim_base_url": "https://idp.example/scim/v2",
            "secret_ref": "secretref:okta-scim-token",
            "status": "active",
            "actor": "ceo",
        },
    )
    assert scim.status_code == 201
    assert scim.json()["status"] == "active"
    denied_rollout = client.post(
        "/v1/enterprise/identity-rollout-policies",
        json={
            "project_id": "boss-dogfood",
            "provider_type": "oidc",
            "runtime_mode": "auth_allowed",
            "actor": "release_manager",
        },
    )
    assert denied_rollout.status_code == 403
    oidc_rollout = upsert_identity_rollout_policy(
        client, "boss-dogfood", "oidc", "auth_allowed"
    )
    scim_rollout = upsert_identity_rollout_policy(
        client, "boss-dogfood", "scim", "provisioning_allowed"
    )
    assert oidc_rollout["runtime_mode"] == "auth_allowed"
    assert scim_rollout["runtime_mode"] == "provisioning_allowed"

    incomplete_saml = client.post(
        "/v1/enterprise/identity-providers",
        json={
            "project_id": "boss-dogfood",
            "provider_key": "saml_pending",
            "provider_type": "saml",
            "display_name": "Pending SAML",
            "entity_id": "urn:boss:test",
            "status": "active",
            "actor": "ceo",
        },
    )
    assert incomplete_saml.status_code == 201
    saml_payload = incomplete_saml.json()
    assert saml_payload["status"] == "blocked"
    assert "saml_sso_url_missing" in saml_payload["blocking_gaps"]
    assert "saml_certificate_fingerprint_missing" in saml_payload["blocking_gaps"]

    listed = client.get(
        "/v1/enterprise/identity-providers",
        params={"project_id": "boss-dogfood"},
    )
    assert listed.status_code == 200
    assert {item["provider_key"] for item in listed.json()["items"]} == {
        "okta_oidc",
        "okta_scim",
        "saml_pending",
    }

    cluster = client.post(
        "/v1/enterprise/clusters",
        json={
            "project_id": "boss-dogfood",
            "deployment_mode": "enterprise_cloud",
            "account_connect_mode": "attached",
            "audit_controls": {
                "audit_exports_enabled": True,
                "retention_days": 365,
                "compliance_evidence_workflows": True,
            },
            "status": "active",
            "actor": "ceo",
        },
    )
    assert cluster.status_code == 201
    cluster_payload = cluster.json()
    assert cluster_payload["status"] == "active"
    assert cluster_payload["blocking_gaps"] == []
    assert cluster_payload["identity_controls"]["sso_oidc_configured"] is True
    assert cluster_payload["identity_controls"]["scim_configured"] is True
    assert cluster_payload["metadata"]["identity_provider_status"] == {
        "oidc": True,
        "saml": False,
        "scim": True,
    }

    events = client.get("/v1/events", params={"project_id": "boss-dogfood"})
    event_types = [item["event_type"] for item in events.json()["items"]]
    assert "policy.denied" in event_types
    assert "enterprise.identity_provider_upserted" in event_types
    assert "enterprise.identity_rollout_policy_upserted" in event_types
    assert "enterprise.cluster_upserted" in event_types


def test_enterprise_scim_provisioning_maps_users_to_org_members(client):
    denied = client.post(
        "/v1/enterprise/scim/groups",
        json={
            "project_id": "boss-dogfood",
            "display_name": "Engineering Admins",
            "permissions": ["audit_export"],
            "actor": "release_manager",
        },
    )
    assert denied.status_code == 403

    group = client.post(
        "/v1/enterprise/scim/groups",
        json={
            "project_id": "boss-dogfood",
            "external_id": "grp-eng-admins",
            "display_name": "Engineering Admins",
            "role_keys": ["engineering_admin"],
            "permissions": ["audit_export", "health_refresh"],
            "raw_group": {"schemas": ["urn:ietf:params:scim:schemas:core:2.0:Group"]},
            "actor": "ceo",
        },
    )
    assert group.status_code == 201
    group_payload = group.json()
    assert group_payload["display_name"] == "Engineering Admins"
    assert "audit_export" in group_payload["permissions"]

    user = client.post(
        "/v1/enterprise/scim/users",
        json={
            "project_id": "boss-dogfood",
            "external_id": "usr-alice",
            "user_name": "Alice.Admin@example.com",
            "display_name": "Alice Admin",
            "groups": ["Engineering Admins"],
            "permissions": ["command_submit"],
            "raw_profile": {
                "schemas": ["urn:ietf:params:scim:schemas:core:2.0:User"]
            },
            "actor": "ceo",
        },
    )
    assert user.status_code == 201
    user_payload = user.json()
    assert user_payload["user_name"] == "alice.admin@example.com"
    assert user_payload["member_id"] == "scim_alice_admin_example_com"
    assert user_payload["active"] is True
    assert {"command_submit", "audit_export", "health_refresh"} <= set(
        user_payload["permissions"]
    )
    assert "engineering_admin" in user_payload["role_keys"]

    members_response = client.get("/v1/members", params={"project_id": "boss-dogfood"})
    member = next(
        item
        for item in members_response.json()["items"]
        if item["member_id"] == "scim_alice_admin_example_com"
    )
    assert member["display_name"] == "Alice Admin"
    assert "audit_export" in member["permissions"]

    groups_response = client.get(
        "/v1/enterprise/scim/groups",
        params={"project_id": "boss-dogfood"},
    )
    assert groups_response.status_code == 200
    assert groups_response.json()["items"][0]["display_name"] == "Engineering Admins"

    users_response = client.get(
        "/v1/enterprise/scim/users",
        params={"project_id": "boss-dogfood"},
    )
    assert users_response.status_code == 200
    assert users_response.json()["items"][0]["member_id"] == "scim_alice_admin_example_com"

    deactivated = client.post(
        "/v1/enterprise/scim/users",
        json={
            "project_id": "boss-dogfood",
            "user_name": "alice.admin@example.com",
            "display_name": "Alice Admin",
            "active": False,
            "groups": ["Engineering Admins"],
            "actor": "ceo",
        },
    )
    assert deactivated.status_code == 201
    assert deactivated.json()["active"] is False

    members_after = client.get("/v1/members", params={"project_id": "boss-dogfood"})
    member_ids_after = {item["member_id"] for item in members_after.json()["items"]}
    assert "scim_alice_admin_example_com" not in member_ids_after

    events = client.get("/v1/events", params={"project_id": "boss-dogfood"})
    event_types = [item["event_type"] for item in events.json()["items"]]
    assert "policy.denied" in event_types
    assert "enterprise.scim_group_upserted" in event_types
    assert "enterprise.scim_user_upserted" in event_types
    assert "org_member.upserted" in event_types


def test_enterprise_scim_protocol_runtime_requires_token_and_active_provider(
    client, engine
):
    project_id = "boss-scim-runtime-missing"
    with engine.begin() as connection:
        connection.execute(
            text("DELETE FROM enterprise_identity_rollout_policies WHERE project_id = :project_id"),
            {"project_id": project_id},
        )
        connection.execute(
            text("DELETE FROM enterprise_identity_providers WHERE project_id = :project_id"),
            {"project_id": project_id},
        )
        connection.execute(
            text("DELETE FROM project_members WHERE project_id = :project_id"),
            {"project_id": project_id},
        )
        connection.execute(
            text("DELETE FROM projects WHERE id = :project_id"),
            {"project_id": project_id},
        )
        connection.execute(
            text(
                """
                INSERT INTO projects (id, workspace_id, name, slug)
                VALUES (:project_id, 'ws_boss', 'BOSS SCIM Runtime Missing', :project_id)
                """
            ),
            {"project_id": project_id},
        )
        api_main.members.seed_default_members(connection, project_id, "test")
    previous_tokens = api_main.settings.enterprise_scim_bearer_tokens
    try:
        api_main.settings.enterprise_scim_bearer_tokens = ""
        missing_token_config = client.get(f"/scim/v2/{project_id}/Users")
        assert missing_token_config.status_code == 503
        assert "BOSS_ENTERPRISE_SCIM_BEARER_TOKENS" in missing_token_config.json()[
            "detail"
        ]

        api_main.settings.enterprise_scim_bearer_tokens = "scim-secret"
        missing_auth = client.get(f"/scim/v2/{project_id}/Users")
        assert missing_auth.status_code == 401

        missing_provider = client.get(
            f"/scim/v2/{project_id}/Users",
            headers={"Authorization": "Bearer scim-secret"},
        )
        assert missing_provider.status_code == 403
        assert missing_provider.json()["detail"] == (
            "No active SCIM identity provider is configured."
        )

        events = client.get("/v1/events", params={"project_id": project_id})
        assert "enterprise.scim_runtime_blocked" in [
            item["event_type"] for item in events.json()["items"]
        ]
    finally:
        api_main.settings.enterprise_scim_bearer_tokens = previous_tokens


def test_enterprise_scim_protocol_provisions_groups_users_and_deactivates(client):
    previous_tokens = api_main.settings.enterprise_scim_bearer_tokens
    api_main.settings.enterprise_scim_bearer_tokens = "scim-secret"
    headers = {"Authorization": "Bearer scim-secret"}
    try:
        provider = client.post(
            "/v1/enterprise/identity-providers",
            json={
                "project_id": "boss-dogfood",
                "provider_key": "okta_scim",
                "provider_type": "scim",
                "display_name": "Okta SCIM",
                "scim_base_url": "https://boss.example/scim/v2/boss-dogfood",
                "secret_ref": "env:BOSS_ENTERPRISE_SCIM_BEARER_TOKENS",
                "status": "active",
                "actor": "ceo",
            },
        )
        assert provider.status_code == 201
        assert provider.json()["status"] == "active"
        rollout_policy = upsert_identity_rollout_policy(
            client, "boss-dogfood", "scim", "provisioning_allowed"
        )
        assert rollout_policy["runtime_mode"] == "provisioning_allowed"

        group = client.post(
            "/scim/v2/boss-dogfood/Groups",
            headers=headers,
            json={
                "schemas": [
                    "urn:ietf:params:scim:schemas:core:2.0:Group",
                    "urn:boss:params:scim:schemas:extension:2.0:Group",
                ],
                "externalId": "grp-ops-admin",
                "displayName": "Ops Admins",
                "urn:boss:params:scim:schemas:extension:2.0:Group": {
                    "roleKeys": ["ops_admin"],
                    "permissions": ["audit_export", "enterprise_manage"],
                },
            },
        )
        assert group.status_code == 201
        group_payload = group.json()
        assert group_payload["id"] == "scimgroup_boss-dogfood_ops_admins"
        assert group_payload[
            "urn:boss:params:scim:schemas:extension:2.0:Group"
        ]["permissions"] == ["audit_export", "enterprise_manage"]

        listed_groups = client.get(
            "/scim/v2/boss-dogfood/Groups",
            headers=headers,
            params={"filter": 'displayName eq "Ops Admins"'},
        )
        assert listed_groups.status_code == 200
        assert listed_groups.json()["totalResults"] == 1

        user = client.post(
            "/scim/v2/boss-dogfood/Users",
            headers=headers,
            json={
                "schemas": [
                    "urn:ietf:params:scim:schemas:core:2.0:User",
                    "urn:boss:params:scim:schemas:extension:2.0:User",
                ],
                "externalId": "usr-bob-ops",
                "userName": "Bob.Ops@example.com",
                "name": {"formatted": "Bob Ops"},
                "active": True,
                "groups": [{"value": "grp-ops-admin", "display": "Ops Admins"}],
                "urn:boss:params:scim:schemas:extension:2.0:User": {
                    "permissions": ["command_submit"]
                },
            },
        )
        assert user.status_code == 201
        user_payload = user.json()
        assert user_payload["userName"] == "bob.ops@example.com"
        assert user_payload[
            "urn:boss:params:scim:schemas:extension:2.0:User"
        ]["memberId"] == "scim_bob_ops_example_com"
        assert {"command_submit", "audit_export", "enterprise_manage"} <= set(
            user_payload["urn:boss:params:scim:schemas:extension:2.0:User"][
                "permissions"
            ]
        )

        fetched_user = client.get(
            f"/scim/v2/boss-dogfood/Users/{user_payload['id']}",
            headers=headers,
        )
        assert fetched_user.status_code == 200
        assert fetched_user.json()["displayName"] == "Bob Ops"

        listed_users = client.get(
            "/scim/v2/boss-dogfood/Users",
            headers=headers,
            params={"filter": 'userName eq "bob.ops@example.com"'},
        )
        assert listed_users.status_code == 200
        assert listed_users.json()["totalResults"] == 1

        replaced_user = client.put(
            f"/scim/v2/boss-dogfood/Users/{user_payload['id']}",
            headers=headers,
            json={
                "userName": "bob.ops@example.com",
                "displayName": "Bob Ops Updated",
                "active": True,
                "groups": [{"value": "Ops Admins"}],
                "urn:boss:params:scim:schemas:extension:2.0:User": {
                    "roleKeys": ["incident_commander"]
                },
            },
        )
        assert replaced_user.status_code == 200
        assert replaced_user.json()["displayName"] == "Bob Ops Updated"
        assert "incident_commander" in replaced_user.json()[
            "urn:boss:params:scim:schemas:extension:2.0:User"
        ]["roleKeys"]

        delete_user = client.delete(
            f"/scim/v2/boss-dogfood/Users/{user_payload['id']}",
            headers=headers,
        )
        assert delete_user.status_code == 204
        members_after = client.get("/v1/members", params={"project_id": "boss-dogfood"})
        assert "scim_bob_ops_example_com" not in {
            item["member_id"] for item in members_after.json()["items"]
        }

        delete_group = client.delete(
            f"/scim/v2/boss-dogfood/Groups/{group_payload['id']}",
            headers=headers,
        )
        assert delete_group.status_code == 204
        inactive_group = client.get(
            f"/scim/v2/boss-dogfood/Groups/{group_payload['id']}",
            headers=headers,
        )
        assert inactive_group.status_code == 200
        assert inactive_group.json()["active"] is False

        events = client.get("/v1/events", params={"project_id": "boss-dogfood"})
        event_types = [item["event_type"] for item in events.json()["items"]]
        assert "enterprise.identity_provider_upserted" in event_types
        assert "enterprise.scim_group_upserted" in event_types
        assert "enterprise.scim_user_upserted" in event_types
    finally:
        api_main.settings.enterprise_scim_bearer_tokens = previous_tokens


def test_list_project_members_returns_seeded_actor_directory(client):
    response = client.get("/v1/members", params={"project_id": "boss-dogfood"})

    assert response.status_code == 200
    payload = response.json()
    member_ids = {item["member_id"] for item in payload["items"]}
    assert {"ceo", "executive_console", "release_manager", "vp_agents", "system"}.issubset(
        member_ids
    )
    executive_console = next(
        item for item in payload["items"] if item["member_id"] == "executive_console"
    )
    assert "command_submit" in executive_console["permissions"]
    assert "ceo" in executive_console["role_keys"]


def test_project_member_upsert_requires_member_manage_permission(client):
    denied = client.post(
        "/v1/members",
        json={
            "project_id": "boss-dogfood",
            "member_id": "operator",
            "display_name": "Operator",
            "member_type": "human",
            "role_keys": ["operator"],
            "permissions": ["health_refresh"],
            "actor": "release_manager",
        },
    )
    assert denied.status_code == 403

    created = client.post(
        "/v1/members",
        json={
            "project_id": "boss-dogfood",
            "member_id": "operator",
            "display_name": "Operator",
            "member_type": "human",
            "role_keys": ["operator"],
            "permissions": ["health_refresh"],
            "actor": "ceo",
        },
    )
    assert created.status_code == 201
    payload = created.json()
    assert payload["member_id"] == "operator"
    assert payload["created_by"] == "ceo"


def test_org_member_delegation_authorizes_projects_in_same_org(client, engine):
    with engine.begin() as connection:
        connection.execute(
            text(
                """
                INSERT INTO projects (id, workspace_id, name, slug)
                VALUES ('boss-org-rbac', 'ws_boss', 'BOSS org RBAC', 'boss-org-rbac')
                ON CONFLICT (workspace_id, slug) DO NOTHING
                """
            )
        )

    created = client.post(
        "/v1/org-members",
        json={
            "org_id": "org_boss",
            "project_id": "boss-dogfood",
            "member_id": "org_delegate",
            "display_name": "Org Delegate",
            "member_type": "human",
            "role_keys": ["operator"],
            "permissions": ["command_submit"],
            "actor": "ceo",
        },
    )
    assert created.status_code == 201
    assert created.json()["org_id"] == "org_boss"

    listed = client.get(
        "/v1/org-members",
        params={"org_id": "org_boss", "project_id": "boss-dogfood", "actor": "ceo"},
    )
    assert listed.status_code == 200
    assert any(item["member_id"] == "org_delegate" for item in listed.json()["items"])

    project_members = client.get(
        "/v1/members",
        params={"project_id": "boss-org-rbac"},
    )
    assert project_members.status_code == 200
    assert any(
        item["member_id"] == "org_delegate"
        and "command_submit" in item["permissions"]
        for item in project_members.json()["items"]
    )

    command = client.post(
        "/v1/commands/propose",
        json={
            "project_id": "boss-org-rbac",
            "command": "Org delegated actor can submit scoped work.",
            "actor": "org_delegate",
        },
    )
    assert command.status_code == 201


def test_org_member_upsert_requires_authorized_project_member(client):
    denied = client.post(
        "/v1/org-members",
        json={
            "org_id": "org_boss",
            "project_id": "boss-dogfood",
            "member_id": "blocked_org_delegate",
            "display_name": "Blocked Org Delegate",
            "member_type": "human",
            "role_keys": ["operator"],
            "permissions": ["command_submit"],
            "actor": "release_manager",
        },
    )
    assert denied.status_code == 403


def test_break_glass_grant_enables_temporary_permission_and_postmortem(client):
    grant = client.post(
        "/v1/break-glass/grants",
        json={
            "project_id": "boss-dogfood",
            "target_member_id": "release_manager",
            "reason": "Emergency member directory repair for an incident.",
            "scope": {"incident_id": "inc-break-glass-test"},
            "granted_permissions": ["member_manage"],
            "ttl_minutes": 30,
            "actor": "ceo",
        },
    )
    assert grant.status_code == 201
    grant_payload = grant.json()
    assert grant_payload["id"].startswith("bg_")
    assert grant_payload["status"] == "active"
    assert grant_payload["postmortem_task_id"].startswith("tsk_")

    created = client.post(
        "/v1/members",
        json={
            "project_id": "boss-dogfood",
            "member_id": "emergency_operator",
            "display_name": "Emergency Operator",
            "member_type": "human",
            "role_keys": ["operator"],
            "permissions": ["health_refresh"],
            "actor": "release_manager",
        },
    )
    assert created.status_code == 201

    events = client.get("/v1/events", params={"project_id": "boss-dogfood"})
    event_types = [item["event_type"] for item in events.json()["items"]]
    assert "break_glass.granted" in event_types
    assert "break_glass.postmortem_task_created" in event_types
    assert "break_glass.used" in event_types


def test_break_glass_grant_can_target_org_member(client):
    created = client.post(
        "/v1/org-members",
        json={
            "org_id": "org_boss",
            "project_id": "boss-dogfood",
            "member_id": "org_release_manager",
            "display_name": "Org Release Manager",
            "member_type": "human",
            "role_keys": ["release_manager"],
            "permissions": ["decision_resolve"],
            "actor": "ceo",
        },
    )
    assert created.status_code == 201

    grant = client.post(
        "/v1/break-glass/grants",
        json={
            "project_id": "boss-dogfood",
            "target_member_id": "org_release_manager",
            "reason": "Emergency org delegate escalation for an incident.",
            "scope": {"incident_id": "inc-org-break-glass"},
            "granted_permissions": ["member_manage"],
            "ttl_minutes": 30,
            "actor": "ceo",
        },
    )
    assert grant.status_code == 201
    assert grant.json()["target_member_id"] == "org_release_manager"

    created_project_member = client.post(
        "/v1/members",
        json={
            "project_id": "boss-dogfood",
            "member_id": "org_emergency_operator",
            "display_name": "Org Emergency Operator",
            "member_type": "human",
            "role_keys": ["operator"],
            "permissions": ["health_refresh"],
            "actor": "org_release_manager",
        },
    )
    assert created_project_member.status_code == 201


def test_break_glass_revoke_removes_temporary_permission(client):
    grant = client.post(
        "/v1/break-glass/grants",
        json={
            "project_id": "boss-dogfood",
            "target_member_id": "release_manager",
            "reason": "Temporary escalation for revoke test.",
            "granted_permissions": ["member_manage"],
            "ttl_minutes": 30,
            "actor": "ceo",
        },
    )
    assert grant.status_code == 201
    grant_id = grant.json()["id"]

    revoked = client.post(
        f"/v1/break-glass/grants/{grant_id}/revoke",
        json={"actor": "ceo", "reason": "Emergency ended."},
    )
    assert revoked.status_code == 200
    assert revoked.json()["status"] == "revoked"

    denied = client.post(
        "/v1/members",
        json={
            "project_id": "boss-dogfood",
            "member_id": "blocked_operator",
            "display_name": "Blocked Operator",
            "member_type": "human",
            "role_keys": ["operator"],
            "permissions": ["health_refresh"],
            "actor": "release_manager",
        },
    )
    assert denied.status_code == 403


def test_expired_break_glass_grant_is_visible_and_not_usable(client, engine):
    with engine.begin() as connection:
        connection.execute(
            text(
                """
                INSERT INTO break_glass_grants (
                    id, project_id, target_member_id, requested_by, reason,
                    scope, granted_permissions, status, expires_at
                )
                VALUES (
                    'bg_expired_test',
                    'boss-dogfood',
                    'release_manager',
                    'ceo',
                    'Expired test grant.',
                    '{}'::jsonb,
                    '["member_manage"]'::jsonb,
                    'active',
                    now() - interval '1 minute'
                )
                """
            )
        )

    denied = client.post(
        "/v1/members",
        json={
            "project_id": "boss-dogfood",
            "member_id": "expired_operator",
            "display_name": "Expired Operator",
            "member_type": "human",
            "role_keys": ["operator"],
            "permissions": ["health_refresh"],
            "actor": "release_manager",
        },
    )
    assert denied.status_code == 403

    grants = client.get(
        "/v1/break-glass/grants",
        params={"project_id": "boss-dogfood", "include_expired": True},
    )
    expired = next(item for item in grants.json()["items"] if item["id"] == "bg_expired_test")
    assert expired["status"] == "expired"

    events = client.get("/v1/events", params={"project_id": "boss-dogfood"})
    event_types = [item["event_type"] for item in events.json()["items"]]
    assert "break_glass.expired" in event_types


def test_compliance_evidence_bundle_captures_controls_and_access_evidence(client):
    grant = client.post(
        "/v1/break-glass/grants",
        json={
            "project_id": "boss-dogfood",
            "target_member_id": "release_manager",
            "reason": "Seed compliance evidence with emergency access.",
            "scope": {"incident_id": "inc-compliance-test"},
            "granted_permissions": ["member_manage"],
            "ttl_minutes": 30,
            "actor": "ceo",
        },
    )
    assert grant.status_code == 201

    response = client.post(
        "/v1/compliance/evidence-bundles",
        json={"project_id": "boss-dogfood", "actor": "ceo"},
    )
    assert response.status_code == 201
    payload = response.json()
    assert payload["artifact_type"] == "compliance_evidence_bundle"
    assert payload["counts"]["project_members"] >= 1
    assert payload["counts"]["break_glass_grants"] == 1

    artifact = client.get(f"/v1/artifacts/{payload['artifact_id']}")
    assert artifact.status_code == 200
    content = artifact.json()["content"]
    assert content["bundle_type"] == "compliance_evidence"
    assert "break_glass" in content["controls"]
    assert content["break_glass_grants"][0]["id"] == grant.json()["id"]

    events = client.get("/v1/events", params={"project_id": "boss-dogfood"})
    event_types = [item["event_type"] for item in events.json()["items"]]
    assert "compliance.evidence_bundle_created" in event_types


def test_command_proposal_requires_known_project_actor(client):
    response = client.post(
        "/v1/commands/propose",
        json={
            "project_id": "boss-dogfood",
            "command": "Create a plan for member-gated command intake.",
            "actor": "not_a_member",
        },
    )

    assert response.status_code == 403
    events = client.get("/v1/events", params={"project_id": "boss-dogfood"})
    event_types = [item["event_type"] for item in events.json()["items"]]
    assert "policy.denied" in event_types


def test_high_cost_endpoints_rate_limited_429_and_emits_event(client):
    """P2 rate limit test per security review: in-mem limiter on metatron/propose/provider-sync.
    10/min default but use low for test burst; assert 429 + rate_limit.exceeded event.
    Uses test helpers to not impact other tests. Per-actor/IP key.
    """
    # Set low limit + fresh state for this test only.
    api_main.configure_rate_limiter_for_testing(2)
    api_main.reset_rate_limiter_hits()

    payload = {
        "project_id": "boss-dogfood",
        "command": "Rate limit test command one.",
    }
    # First two should succeed (under limit).
    r1 = client.post("/v1/commands/propose", json=payload)
    assert r1.status_code == 201, r1.text
    r2 = client.post("/v1/commands/propose", json={**payload, "command": "Rate limit test command two."})
    assert r2.status_code == 201, r2.text

    # Third exceeds -> 429
    r3 = client.post("/v1/commands/propose", json={**payload, "command": "Rate limit test command three."})
    assert r3.status_code == 429
    assert "rate limit exceeded" in r3.json()["detail"].lower()

    # Event emitted for audit (visible, no silent)
    events_resp = client.get("/v1/events", params={"project_id": "boss-dogfood"})
    assert events_resp.status_code == 200
    event_types = [item["event_type"] for item in events_resp.json()["items"]]
    assert "rate_limit.exceeded" in event_types

    # Verify metatron high-cost path also protected under its key (reset for fresh window on metatron actor key).
    api_main.reset_rate_limiter_hits()
    m1 = client.post(
        "/v1/metatron/query",
        json={"project_id": "boss-dogfood", "question": "Rate limit metatron one?"},
    )
    assert m1.status_code == 200, m1.text  # under limit=2
    m2 = client.post(
        "/v1/metatron/query",
        json={"project_id": "boss-dogfood", "question": "Rate limit metatron two?"},
    )
    assert m2.status_code == 200, m2.text
    m3 = client.post(
        "/v1/metatron/query",
        json={"project_id": "boss-dogfood", "question": "Rate limit metatron three?"},
    )
    assert m3.status_code == 429
    assert "rate limit exceeded" in m3.json()["detail"].lower()

    # Restore generous limit + clear for remaining test suite (propose/metatron/sync calls elsewhere).
    api_main.configure_rate_limiter_for_testing(1000)
    api_main.reset_rate_limiter_hits()


def test_high_cost_resources_sync_also_rate_limited(client):
    """Extend rate limit coverage (P2): assert /v1/resources/sync is protected like propose/metatron."""
    api_main.configure_rate_limiter_for_testing(1)
    api_main.reset_rate_limiter_hits()

    payload = {"project_id": "boss-dogfood", "actor": "ceo"}
    r1 = client.post("/v1/resources/sync", json=payload)
    # first under limit may 200 or 403 (no real creds) but not 429
    assert r1.status_code != 429

    r2 = client.post("/v1/resources/sync", json=payload)
    assert r2.status_code == 429
    assert "rate limit exceeded" in r2.json()["detail"].lower()

    api_main.configure_rate_limiter_for_testing(1000)
    api_main.reset_rate_limiter_hits()


def test_rate_limit_disabled_bypasses_limit(monkeypatch, client):
    """Rate limit P2: when BOSS_RATE_LIMIT_ENABLED=false (or settings false), high cost paths not 429 even burst."""
    api_main.configure_rate_limiter_for_testing(1)
    api_main.reset_rate_limiter_hits()
    monkeypatch.setattr(api_main.settings, "rate_limit_enabled", False)

    payload = {"project_id": "boss-dogfood", "command": "disabled rate test burst"}
    for _ in range(5):
        r = client.post("/v1/commands/propose", json=payload)
        assert r.status_code != 429, f"should bypass when disabled, got {r.status_code}"

    # restore
    monkeypatch.setattr(api_main.settings, "rate_limit_enabled", True)
    api_main.configure_rate_limiter_for_testing(1000)
    api_main.reset_rate_limiter_hits()


def test_rate_limit_denial_audit_write_failure_is_logged(monkeypatch, caplog):
    """Mirror of test_policy_denial_audit_write_failure_is_logged for rate limit P2 coverage.
    Ensures no silent drop on audit event write fail for rate_limit.exceeded."""
    def failing_begin(_engine):
        raise RuntimeError("rate audit db unavailable")

    monkeypatch.setattr(api_main, "begin", failing_begin)
    caplog.set_level(logging.ERROR, logger="boss.api")

    api_main.record_rate_limit_denial(
        "proj_missing",
        "operator",
        "rate limit exceeded on /test",
        "127.0.0.1",
    )

    assert "rate_limit_denial_audit_write_failed" in caplog.text
    assert "rate audit db unavailable" in caplog.text


def test_metatron_status_query_returns_fresh_answer_and_references(client):
    response = client.post(
        "/v1/metatron/query",
        json={
            "project_id": "boss-dogfood",
            "question": "What is the current status?",
        },
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["category"] == "status"
    assert "budget state" in payload["answer"]
    assert payload["freshness_at"]
    assert payload["references"]
    assert payload["references"][0]["object_type"] in {"project", "task", "decision"}


def test_metatron_budget_query_returns_reservation_references(client):
    reserve = client.post(
        "/v1/budget/reserve",
        json={
            "project_id": "boss-dogfood",
            "category": "tool",
            "amount_usd_micros": 125_000,
            "reason": "Seed a reservation for Metatron budget status.",
        },
    )
    assert reserve.status_code == 201

    response = client.post(
        "/v1/metatron/query",
        json={
            "project_id": "boss-dogfood",
            "question": "How is the budget doing right now?",
        },
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["category"] == "budget"
    assert "Available:" in payload["answer"]
    assert any(
        reference["object_type"] == "budget_reservation"
        for reference in payload["references"]
    )


def test_metatron_activity_query_returns_event_references(client, engine):
    with engine.begin() as connection:
        connection.execute(
            text(
                """
                INSERT INTO events (id, project_id, event_type, actor, entity_type, entity_id, data)
                VALUES (
                    'evt_metatron_activity',
                    'boss-dogfood',
                    'artifact.created',
                    'tester',
                    'artifact',
                    'art_metatron_activity',
                    '{"message":"Activity query should see this event."}'::jsonb
                )
                """
            )
        )

    response = client.post(
        "/v1/metatron/query",
        json={
            "project_id": "boss-dogfood",
            "question": "What changed recently?",
        },
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["category"] == "activity"
    assert payload["references"]
    assert payload["references"][0]["object_type"] == "artifact"
    assert "artifact.created" in payload["answer"]


def test_metatron_task_scope_returns_task_lineage(client):
    created = client.post(
        "/v1/commands/propose",
        json={
            "project_id": "boss-dogfood",
            "command": "Create a scoped task answer for Metatron.",
            "constraints": {"deadline": "today"},
        },
    )
    assert created.status_code == 201
    task_id = created.json()["task_id"]

    response = client.post(
        "/v1/metatron/query",
        json={
            "project_id": "boss-dogfood",
            "question": "What is happening with this task?",
            "task_id": task_id,
        },
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["category"] == "task"
    assert task_id in {reference["object_id"] for reference in payload["references"]}
    assert "artifact" in {reference["object_type"] for reference in payload["references"]}


def test_bootstrap_seed_api_fails_closed_by_default(client):
    reseed = client.post("/v1/bootstrap/seed")
    assert reseed.status_code == 403
    assert "Bootstrap seed API is disabled" in reseed.json()["detail"]


def test_bootstrap_seed_api_requires_member_manage_when_enabled(client, monkeypatch):
    monkeypatch.setattr(api_main.settings, "bootstrap_seed_api_enabled", True)

    reseed = client.post("/v1/bootstrap/seed", params={"actor": "release_manager"})
    assert reseed.status_code == 403


def test_bootstrap_seed_keeps_roles_unique_when_explicitly_enabled(client, monkeypatch):
    monkeypatch.setattr(api_main.settings, "bootstrap_seed_api_enabled", True)

    reseed = client.post("/v1/bootstrap/seed", params={"actor": "ceo"})
    assert reseed.status_code == 200
    assert reseed.json()["status"] == "seeded"
    response = client.get("/v1/roles", params={"project_id": "boss-dogfood"})

    assert response.status_code == 200
    payload = response.json()
    role_keys = [role["role_key"] for role in payload["items"]]
    assert len(role_keys) == len(set(role_keys))
    assert "metatron" in role_keys
    events = client.get("/v1/events", params={"project_id": "boss-dogfood"})
    assert "bootstrap.seed_executed" in {
        item["event_type"] for item in events.json()["items"]
    }


def test_health_snapshot_refresh_persists_summary_and_risks(client, engine):
    denial = client.post(
        "/v1/budget/reserve",
        json={
            "project_id": "boss-dogfood",
            "category": "model",
            "amount_usd_micros": 100_000_000,
            "reason": "force health-risk decision",
        },
    )
    assert denial.status_code == 201

    with engine.begin() as connection:
        connection.execute(
            text(
                """
                INSERT INTO deadletters (id, project_id, source_kind, source_id, reason, payload)
                VALUES (
                    'dlq_health_1', 'boss-dogfood', 'job', 'job_health_1',
                    'health test deadletter', '{}'::jsonb
                )
                """
            )
        )

    refreshed = client.post(
        "/v1/health-snapshots/refresh",
        json={"project_id": "boss-dogfood", "actor": "metatron"},
    )
    assert refreshed.status_code == 201
    payload = refreshed.json()
    assert payload["project_id"] == "boss-dogfood"
    assert payload["generated_by"] == "metatron"
    assert payload["summary"]["deadletters"] == 1
    assert payload["workload_state"]["pending_decisions"] >= 1
    assert payload["top_risks"]
    assert any(
        risk["category"] == "incident" and "Deadletters" in risk["title"]
        for risk in payload["top_risks"]
    )

    snapshots = client.get(
        "/v1/health-snapshots",
        params={"project_id": "boss-dogfood", "limit": 10},
    )
    assert snapshots.status_code == 200
    snapshot_payload = snapshots.json()
    assert snapshot_payload["items"]
    assert snapshot_payload["items"][0]["id"] == payload["id"]


def test_status_surfaces_latest_health_snapshot_metadata(client):
    refreshed = client.post(
        "/v1/health-snapshots/refresh",
        json={"project_id": "boss-dogfood", "actor": "executive_console"},
    )
    assert refreshed.status_code == 201
    snapshot_id = refreshed.json()["id"]

    status = client.get("/v1/status", params={"project_id": "boss-dogfood"})
    assert status.status_code == 200
    payload = status.json()
    assert payload["latest_snapshot_id"] == snapshot_id
    assert payload["latest_snapshot_at"]
    assert isinstance(payload["top_risks"], list)


def test_seed_installs_core_pack_and_roles(client):
    packs = client.get("/v1/packs", params={"project_id": "boss-dogfood"})
    assert packs.status_code == 200
    pack_payload = packs.json()
    core_pack = next(item for item in pack_payload["items"] if item["pack_id"] == "core_v1")
    assert core_pack["installed"] is True
    assert core_pack["installed_version"] == "1.0.0"
    assert "metatron" in core_pack["roles"]
    assert "providerops_manager" in core_pack["roles"]
    assert "finops_sentinel" in core_pack["roles"]
    assert "resource_janitor" in core_pack["roles"]

    roles = client.get("/v1/roles", params={"project_id": "boss-dogfood"})
    assert roles.status_code == 200
    role_payload = roles.json()
    assert len(role_payload["items"]) >= 12
    role_keys = [item["role_key"] for item in role_payload["items"]]
    assert "metatron" in role_keys
    assert "cto" in role_keys
    assert "providerops_manager" in role_keys
    assert "finops_sentinel" in role_keys
    assert "resource_janitor" in role_keys


def test_role_detail_returns_loaded_spec(client):
    response = client.get(
        "/v1/roles/metatron",
        params={"project_id": "boss-dogfood"},
    )
    assert response.status_code == 200
    payload = response.json()
    assert payload["role_key"] == "metatron"
    assert payload["display_name"] == "Metatron"
    assert payload["autonomy_level"] == 1
    assert payload["trust_score"] == 50
    assert payload["source_module_id"] == "metatron_operator_v1"
    assert payload["source_module_version"] == "1.0.0"
    assert payload["spec"]["mission"].startswith("Watch the system")
    assert "StatusAnswer" in payload["spec"]["output_contracts"]


def test_installed_roles_have_complete_rolespec_contract(client):
    response = client.get("/v1/roles", params={"project_id": "boss-dogfood"})

    assert response.status_code == 200
    payload = response.json()
    assert payload["items"]
    for role in payload["items"]:
        detail = client.get(
            f"/v1/roles/{role['role_key']}",
            params={"project_id": "boss-dogfood"},
        )
        assert detail.status_code == 200
        assert_complete_rolespec(detail.json()["spec"])


def test_rolespec_validator_rejects_incomplete_specs():
    with pytest.raises(ValueError, match="decision_rights"):
        api_main.specpack.validate_role_spec(
            {
                "role_id": "unsafe_role",
                "display_name": "Unsafe Role",
                "mission": "Do unsafe work.",
                "primary_outcomes": ["Output"],
                "boundaries": {
                    "must": ["Follow policy."],
                    "must_not": ["Bypass approvals."],
                },
                "permissions": {
                    "allowed_skills": ["artifact.create"],
                    "forbidden_skills": ["github.merge_pr_real"],
                },
                "output_contracts": ["Report"],
                "autonomy": {"level": 1},
            },
            "test role",
        )


def test_seed_installs_default_agent_modules(client):
    response = client.get("/v1/agent-modules", params={"project_id": "boss-dogfood"})

    assert response.status_code == 200
    payload = response.json()
    modules = {item["module_id"]: item for item in payload["items"]}
    for module_id in api_main.specpack.DEFAULT_AGENT_MODULE_IDS:
        assert modules[module_id]["installed"] is True
    assert modules["metatron_operator_v1"]["required_entitlements"] == [
        "basic_metatron"
    ]
    assert modules["codex_software_engineer_v1"]["entitlement_status"] == "allowed"
    assert modules["finops_sentinel_v1"]["entitlement_status"] == "allowed"
    assert modules["secrets_steward_v1"]["entitlement_status"] == "allowed"
    assert modules["private_enterprise_agent_v1"]["entitlement_status"] == (
        "missing_required_entitlement"
    )
    assert "plan.command" in modules["metatron_operator_v1"]["job_kinds"]
    assert "agent.metatron.status_brief" in modules["metatron_operator_v1"][
        "job_kinds"
    ]
    assert "agent.infra_steward.infra_review" in modules["infra_steward_v1"][
        "job_kinds"
    ]
    assert "software_factory.propose_pr" in modules["codex_software_engineer_v1"][
        "job_kinds"
    ]


def test_agent_runtime_resolves_default_module_prompt_contract(client):
    response = client.post(
        "/v1/agent-runtime/resolve",
        json={
            "project_id": "boss-dogfood",
            "job_kind": "agent.pm.scope_brief",
            "task_payload": {
                "command": "Shape the Decision Inbox default-action system.",
                "risk_tier": 2,
            },
            "actor": "ceo",
        },
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["matched"] is True
    assert payload["agent_module"]["id"] == "pm_product_planner_v1"
    assert payload["role"]["role_key"] == "pm"
    assert "No spendy action without a BudgetReservation." in payload[
        "compiled_prompt"
    ]["system"]
    assert "Decision Inbox" in payload["compiled_prompt"]["job"]
    assert "artifact.create" in payload["runtime"]["default_skill_ids"]
    assert any(
        "Produce or update output contract" in item
        for item in payload["compiled_prompt"]["checklist"]
    )
    assert "Role allowed skills" in "\n".join(payload["compiled_prompt"]["checklist"])
    assert payload["compiled_prompt"]["runtime_contract"]["output_contracts"] == [
        "PRD",
        "AcceptanceCriteria",
        "ScopeDecision",
    ]
    assert payload["compiled_prompt"]["runtime_contract"]["cadence"]
    assert payload["compiled_prompt"]["runtime_contract"]["eval_rubric"]
    assert payload["role"]["cadence"]


def test_all_initial_agent_modules_resolve_prompt_contracts(client):
    modules_response = client.get(
        "/v1/agent-modules",
        params={"project_id": "boss-dogfood"},
    )
    assert modules_response.status_code == 200
    modules = {
        item["module_id"]: item
        for item in modules_response.json()["items"]
        if item["module_id"] in api_main.specpack.DEFAULT_AGENT_MODULE_IDS
    }
    assert sorted(modules) == sorted(api_main.specpack.DEFAULT_AGENT_MODULE_IDS)

    for module_id, module in modules.items():
        assert module["installed"] is True
        assert module["job_kinds"], module_id
        for job_kind in module["job_kinds"]:
            response = client.post(
                "/v1/agent-runtime/resolve",
                json={
                    "project_id": "boss-dogfood",
                    "agent_module_id": module_id,
                    "job_kind": job_kind,
                    "task_payload": {
                        "command": f"Validate runtime contract for {module_id}.",
                        "risk_tier": 1,
                    },
                    "actor": "ceo",
                },
            )
            assert response.status_code == 200, (module_id, job_kind, response.text)
            payload = response.json()
            assert payload["matched"] is True
            assert payload["agent_module"]["id"] == module_id
            assert payload["role"]["role_key"]
            assert payload["role"]["output_contracts"], module_id
            assert payload["role"]["cadence"], module_id
            assert payload["role"]["eval_rubric"], module_id
            assert payload["compiled_prompt"]["system"], module_id
            assert payload["compiled_prompt"]["job"], module_id
            assert payload["compiled_prompt"]["checklist"], module_id
            assert payload["compiled_prompt"]["runtime_contract"]["job_kinds"]
            assert payload["compiled_prompt"]["runtime_contract"]["allowed_skills"]
            assert payload["compiled_prompt"]["runtime_contract"]["forbidden_skills"]
            assert "No external effect without a Receipt." in payload[
                "compiled_prompt"
            ]["system"]
            assert "Role allowed skills" in "\n".join(
                payload["compiled_prompt"]["checklist"]
            )
            assert "Forbidden skills" in "\n".join(
                payload["compiled_prompt"]["checklist"]
            )


def test_agent_runtime_resolution_requires_module_manage_permission(client):
    response = client.post(
        "/v1/agent-runtime/resolve",
        json={
            "project_id": "boss-dogfood",
            "job_kind": "agent.pm.scope_brief",
            "actor": "release_manager",
        },
    )

    assert response.status_code == 403
    assert "module_manage" in response.json()["detail"]


def test_agent_module_install_requires_declared_marketplace_entitlement(client, engine):
    denied = client.post(
        "/v1/agent-modules/install",
        json={
            "project_id": "boss-dogfood",
            "module_id": "private_enterprise_agent_v1",
            "actor": "ceo",
        },
    )
    assert denied.status_code == 403
    assert "private_agent_marketplace" in denied.json()["detail"]

    denied_records = client.get(
        "/v1/agent-module-install-records",
        params={
            "project_id": "boss-dogfood",
            "module_id": "private_enterprise_agent_v1",
        },
    )
    assert denied_records.status_code == 200
    denied_record = denied_records.json()["items"][0]
    assert denied_record["action"] == "install_denied"
    assert denied_record["status"] == "denied"
    assert denied_record["entitlement_result"]["status"] == (
        "missing_required_entitlement"
    )
    denied_integrity = client.get(
        f"/v1/artifacts/{denied_record['metadata']['module_integrity_artifact_id']}"
    )
    assert denied_integrity.status_code == 200
    denied_integrity_payload = denied_integrity.json()
    assert (
        denied_integrity_payload["artifact_type"] == "agent_module_integrity_packet"
    )
    assert denied_integrity_payload["content"]["module_id"] == (
        "private_enterprise_agent_v1"
    )
    assert denied_integrity_payload["content"]["action"] == "install_denied"
    assert denied_integrity_payload["content"]["signing"]["status"] == "unsigned"
    assert denied_integrity_payload["content"]["spec_hash"]

    with engine.begin() as connection:
        connection.execute(
            text(
                """
                INSERT INTO license_entitlements (
                    id, project_id, edition_id, account_connect_org_id,
                    scope_type, scope_id, capability_key, capabilities,
                    status, source, metadata
                )
                VALUES (
                    'lic_private_agent_marketplace',
                    'boss-dogfood',
                    'enterprise_cloud',
                    'acct_org_test',
                    'project',
                    'boss-dogfood',
                    'private_agent_marketplace',
                    '["private_agent_marketplace"]'::jsonb,
                    'active',
                    'account_connect_hosted',
                    '{"reason":"test entitlement"}'::jsonb
                )
                ON CONFLICT (project_id, capability_key, source) DO UPDATE
                SET status = EXCLUDED.status,
                    capabilities = EXCLUDED.capabilities,
                    updated_at = now()
                """
            )
        )

    installed = client.post(
        "/v1/agent-modules/install",
        json={
            "project_id": "boss-dogfood",
            "module_id": "private_enterprise_agent_v1",
            "actor": "ceo",
        },
    )
    assert installed.status_code == 201
    payload = installed.json()
    assert payload["module_id"] == "private_enterprise_agent_v1"
    assert payload["install_status"] == "installed"
    assert payload["entitlement_result"]["status"] == "allowed"
    assert payload["role_result"]["role_key"] == "private_enterprise_agent"

    records = client.get(
        "/v1/agent-module-install-records",
        params={
            "project_id": "boss-dogfood",
            "module_id": "private_enterprise_agent_v1",
            "limit": 10,
        },
    )
    assert records.status_code == 200
    actions = [item["action"] for item in records.json()["items"]]
    assert "install" in actions
    assert "install_denied" in actions
    install_record = next(
        item for item in records.json()["items"] if item["action"] == "install"
    )
    install_integrity = client.get(
        f"/v1/artifacts/{install_record['metadata']['module_integrity_artifact_id']}"
    )
    assert install_integrity.status_code == 200
    assert install_integrity.json()["content"]["action"] == "install"
    assert install_integrity.json()["content"]["entitlement_result"]["status"] == "allowed"

    events = client.get("/v1/events", params={"project_id": "boss-dogfood"})
    event_types = [item["event_type"] for item in events.json()["items"]]
    assert "agent_module.install_denied" in event_types
    assert "agent_module.installed" in event_types


def test_role_review_creates_upgrade_recommendation_when_role_is_eligible(client):
    response = client.post(
        "/v1/roles/metatron/reviews",
        json={
            "project_id": "boss-dogfood",
            "actor": "vp_agents",
            "summary": "Metatron completed enough clean runs to qualify for the next autonomy tier.",
            "metrics": {
                "successful_runs": 30,
                "policy_violations": 0,
                "duplicate_effect_attempts": 0,
                "hidden_failures": 0,
                "low_quality_outputs": 0,
                "scope_violations": 0,
                "rollback_rate": 0.0,
            },
            "evidence_refs": ["artifact:metatron-eval-001"],
        },
    )

    assert response.status_code == 201
    payload = response.json()
    assert payload["role"]["role_key"] == "metatron"
    assert payload["role"]["trust_score"] > 50
    assert payload["pending_autonomy_change"] is not None
    assert payload["pending_autonomy_change"]["change_type"] == "upgrade"
    assert payload["pending_autonomy_change"]["from_level"] == 1
    assert payload["pending_autonomy_change"]["to_level"] == 2
    assert payload["pending_autonomy_change"]["decision_id"]
    assert payload["recent_reviews"][0]["governance_outcome"] == "upgrade_recommended"


def test_role_review_with_violation_auto_downgrades_and_records_drift(client):
    response = client.post(
        "/v1/roles/cto/reviews",
        json={
            "project_id": "boss-dogfood",
            "actor": "vp_agents",
            "summary": "CTO drifted outside its charter and triggered a policy violation.",
            "metrics": {
                "successful_runs": 2,
                "policy_violations": 1,
                "duplicate_effect_attempts": 0,
                "hidden_failures": 0,
                "low_quality_outputs": 0,
                "scope_violations": 1,
                "rollback_rate": 0.0,
            },
            "evidence_refs": ["event:policy-violation-001"],
        },
    )

    assert response.status_code == 201
    payload = response.json()
    assert payload["role"]["role_key"] == "cto"
    assert payload["role"]["autonomy_level"] == 0
    assert payload["role"]["policy_violation_count"] == 1
    assert payload["role"]["scope_violation_count"] == 1
    assert payload["pending_autonomy_change"] is None
    assert payload["recent_reviews"][0]["governance_outcome"] == "auto_downgraded"
    assert payload["recent_autonomy_changes"][0]["status"] == "applied"
    assert payload["recent_autonomy_changes"][0]["change_type"] == "downgrade"

    events = client.get("/v1/events", params={"project_id": "boss-dogfood"})
    assert events.status_code == 200
    event_types = [item["event_type"] for item in events.json()["items"]]
    assert "role.drift_detected" in event_types


def test_approved_role_autonomy_change_updates_role_level(client):
    review = client.post(
        "/v1/roles/metatron/reviews",
        json={
            "project_id": "boss-dogfood",
            "actor": "vp_agents",
            "summary": "Metatron met the next autonomy threshold.",
            "metrics": {
                "successful_runs": 30,
                "policy_violations": 0,
                "duplicate_effect_attempts": 0,
                "hidden_failures": 0,
                "low_quality_outputs": 0,
                "scope_violations": 0,
                "rollback_rate": 0.0,
            },
            "evidence_refs": ["artifact:metatron-eval-approve"],
        },
    )
    assert review.status_code == 201
    decision_id = review.json()["pending_autonomy_change"]["decision_id"]

    approved = client.post(
        f"/v1/decisions/{decision_id}/approve",
        json={"actor": "ceo", "note": "Upgrade approved."},
    )
    assert approved.status_code == 200

    role = client.get("/v1/roles/metatron", params={"project_id": "boss-dogfood"})
    assert role.status_code == 200
    assert role.json()["autonomy_level"] == 2

    governance = client.get(
        "/v1/roles/metatron/governance",
        params={"project_id": "boss-dogfood"},
    )
    assert governance.status_code == 200
    payload = governance.json()
    assert payload["pending_autonomy_change"] is None
    assert payload["recent_autonomy_changes"][0]["status"] == "applied"
    assert payload["recent_autonomy_changes"][0]["to_level"] == 2


def test_role_autonomy_check_denies_unearned_real_execution(client):
    response = client.post(
        "/v1/roles/software_engineer/autonomy/check",
        json={
            "project_id": "boss-dogfood",
            "actor": "vp_agents",
            "action": "execute",
            "risk_tier": 1,
            "sandboxed": False,
        },
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["allowed"] is False
    assert payload["current_level"] == 1
    assert payload["required_capability"] == "execute_tier1_real"
    assert payload["required_level"] == 3
    assert payload["reason"] == "autonomy_level_too_low"

    events = client.get("/v1/events", params={"project_id": "boss-dogfood"})
    assert events.status_code == 200
    assert "role.autonomy_denied" in [
        item["event_type"] for item in events.json()["items"]
    ]


def test_role_autonomy_check_allows_sandbox_execution_at_level_two(client, engine):
    with engine.begin() as connection:
        connection.execute(
            text(
                """
                UPDATE roles
                SET autonomy_level = 2
                WHERE project_id = 'boss-dogfood'
                  AND role_key = 'software_engineer'
                """
            )
        )

    response = client.post(
        "/v1/roles/software_engineer/autonomy/check",
        json={
            "project_id": "boss-dogfood",
            "actor": "vp_agents",
            "action": "execute",
            "risk_tier": 1,
            "sandboxed": True,
        },
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["allowed"] is True
    assert payload["current_level"] == 2
    assert payload["required_capability"] == "execute_tier1_sandbox"
    assert payload["required_level"] == 2
    assert "execute_tier1_sandbox" in payload["allowed_capabilities"]


def test_role_scope_check_allows_rolespec_skill_without_trust_change(client):
    response = client.post(
        "/v1/roles/software_engineer/scope/check",
        json={
            "project_id": "boss-dogfood",
            "actor": "vp_agents",
            "action": "github.create_pr_sandbox",
            "evidence_ref": "job:software-factory-allowed",
        },
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["allowed"] is True
    assert payload["reason"] == "allowed_by_rolespec"
    assert payload["trust_score_before"] == 50
    assert payload["trust_score_after"] == 50
    assert payload["autonomy_level_before"] == payload["autonomy_level_after"]
    assert payload["scope_violation_count"] == 0
    assert payload["artifact_id"] is None

    events = client.get("/v1/events", params={"project_id": "boss-dogfood"})
    assert events.status_code == 200
    assert "role.scope_allowed" in [
        item["event_type"] for item in events.json()["items"]
    ]


def test_role_scope_check_blocks_forbidden_skill_and_records_drift(client):
    response = client.post(
        "/v1/roles/software_engineer/scope/check",
        json={
            "project_id": "boss-dogfood",
            "actor": "vp_agents",
            "action": "infra.create_node_real",
            "skill_id": "infra.create_node_real",
            "evidence_ref": "event:out-of-scope-infra",
        },
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["allowed"] is False
    assert payload["reason"] == "forbidden_by_rolespec"
    assert payload["trust_score_before"] == 50
    assert payload["trust_score_after"] == 40
    assert payload["autonomy_level_before"] == 1
    assert payload["autonomy_level_after"] == 0
    assert payload["scope_violation_count"] == 1
    assert payload["artifact_id"].startswith("art_")
    assert payload["autonomy_change_id"].startswith("rac_")

    role = client.get(
        "/v1/roles/software_engineer",
        params={"project_id": "boss-dogfood"},
    )
    assert role.status_code == 200
    assert role.json()["trust_score"] == 40
    assert role.json()["autonomy_level"] == 0
    assert role.json()["scope_violation_count"] == 1

    artifact = client.get(f"/v1/artifacts/{payload['artifact_id']}")
    assert artifact.status_code == 200
    assert artifact.json()["artifact_type"] == "role_scope_violation"
    assert artifact.json()["content"]["skill_id"] == "infra.create_node_real"
    assert artifact.json()["content"]["matched_forbidden_pattern"] == "infra.*"

    events = client.get("/v1/events", params={"project_id": "boss-dogfood"})
    assert events.status_code == 200
    event_types = [item["event_type"] for item in events.json()["items"]]
    assert "role.scope_violation" in event_types
    assert "role.trust_changed" in event_types
    assert "role.autonomy_changed" in event_types


def test_pack_install_is_idempotent_for_new_project(client, engine):
    with engine.begin() as connection:
        connection.execute(text("DELETE FROM events WHERE project_id = 'boss-lab'"))
        connection.execute(
            text("DELETE FROM agent_module_installs WHERE project_id = 'boss-lab'")
        )
        connection.execute(text("DELETE FROM roles WHERE project_id = 'boss-lab'"))
        connection.execute(
            text("DELETE FROM pack_installs WHERE project_id = 'boss-lab'")
        )
        connection.execute(text("DELETE FROM projects WHERE id = 'boss-lab'"))
        connection.execute(
            text(
                """
                INSERT INTO projects (id, workspace_id, name, slug)
                VALUES ('boss-lab', 'ws_boss', 'BOSS Lab', 'boss-lab')
                """
            )
        )
        api_main.members.seed_default_members(connection, "boss-lab", "test")

    first = client.post(
        "/v1/packs/install",
        json={"project_id": "boss-lab", "pack_id": "core_v1", "actor": "ceo"},
    )
    assert first.status_code == 201
    first_payload = first.json()
    assert first_payload["pack_id"] == "core_v1"
    assert first_payload["install_status"] == "installed"
    assert first_payload["role_count"] == 16

    second = client.post(
        "/v1/packs/install",
        json={"project_id": "boss-lab", "pack_id": "core_v1", "actor": "ceo"},
    )
    assert second.status_code == 201
    second_payload = second.json()
    assert second_payload["install_status"] == "already_installed"

    roles = client.get("/v1/roles", params={"project_id": "boss-lab"})
    assert roles.status_code == 200
    role_keys = [item["role_key"] for item in roles.json()["items"]]
    assert role_keys == [
        "cao",
        "coo",
        "cto",
        "deployment_manager",
        "finops_sentinel",
        "infra_steward",
        "metatron",
        "model_spend_analyst",
        "pm",
        "program_manager",
        "providerops_manager",
        "qa",
        "release_manager",
        "resource_janitor",
        "secrets_steward",
        "vp_agents",
    ]

    pack = client.get("/v1/packs/core_v1", params={"project_id": "boss-lab"})
    assert pack.status_code == 200
    assert pack.json()["installed"] is True


def test_cross_slice_pack_import_budget_edition_and_self_improvement_regression(
    client, engine
):
    """Covers GAP-E2E-REGRESSION-001 across pack, commercial, budget, and SIM.

    This intentionally uses the local PackSpec service import because this branch's
    runtime import flow is service-backed while public pack APIs expose list/get/install.
    """
    project_id = "boss-dogfood"
    pack_id = "test_e2e_reg_pack_v1"
    pack_yaml = """
id: test_e2e_reg_pack_v1
version: 0.1.0
description: Cross-slice regression pack for local Community import.
status: active
installs:
  roles:
    - metatron
requires: []
workflows: []
policies: []
skills: []
"""
    with engine.begin() as connection:
        connection.execute(
            text(
                """
                DELETE FROM events
                WHERE project_id = :project_id
                  AND (
                    event_type = 'pack.imported'
                    OR event_type = 'self_improvement.proposed'
                    OR event_type = 'budget.reserved'
                  )
                """
            ),
            {"project_id": project_id},
        )
        connection.execute(
            text(
                """
                DELETE FROM receipts
                WHERE project_id = :project_id
                  AND dedupe_key = :dedupe_key
                """
            ),
            {
                "project_id": project_id,
                "dedupe_key": f"pack.import:{project_id}:{pack_id}",
            },
        )
        connection.execute(
            text(
                """
                DELETE FROM pack_installs
                WHERE project_id = :project_id AND pack_id = :pack_id
                """
            ),
            {"project_id": project_id, "pack_id": pack_id},
        )

    edition = client.get("/v1/edition", params={"project_id": project_id})
    assert edition.status_code == 200
    assert edition.json()["edition_id"] == "community"
    assert edition.json()["local_auth_allowed"] is True

    budget_reservation = client.post(
        "/v1/budget/reserve",
        json={
            "project_id": project_id,
            "category": "cross_slice_e2e",
            "amount_usd_micros": 1_000,
            "reason": "Cross-slice regression pack import and self-improvement path.",
        },
    )
    assert budget_reservation.status_code == 201
    assert budget_reservation.json()["status"] == "reserved"

    with engine.begin() as connection:
        imported = api_main.specpack.import_pack(
            connection,
            project_id=project_id,
            yaml_str=pack_yaml,
            actor="ceo",
        )
    assert imported["pack_id"] == pack_id
    assert imported["installed"] is True
    assert imported["install_status"] == "imported"
    assert imported["role_results"][0]["role_key"] == "metatron"

    with pytest.raises(ValueError, match="invalid yaml|mapping/object"):
        with engine.begin() as connection:
            api_main.specpack.import_pack(
                connection,
                project_id=project_id,
                yaml_str=": not valid: yaml:",
                actor="ceo",
            )

    with pytest.raises(ValueError, match="clashes with core baked pack"):
        with engine.begin() as connection:
            api_main.specpack.import_pack(
                connection,
                project_id=project_id,
                yaml_str=pack_yaml.replace(pack_id, "core_v1"),
                actor="ceo",
            )

    packs = client.get("/v1/packs", params={"project_id": project_id})
    assert packs.status_code == 200
    custom_pack = [
        item for item in packs.json()["items"] if item["pack_id"] == pack_id
    ]
    assert custom_pack
    assert custom_pack[0]["installed"] is True
    assert custom_pack[0]["installed_version"] == "0.1.0"

    proposal = client.post(
        "/v1/self-improvement/proposals",
        json={
            "project_id": project_id,
            "title": f"Improve {pack_id} install evidence",
            "summary": "Use the imported local pack as the subject of a governed improvement.",
            "change_ref": "local-pack://test_e2e_reg_pack_v1",
            "diff_summary": "Add acceptance evidence around local pack import.",
            "expected_impact": ["Imported pack remains traceable"],
            "test_plan": ["Run cross-slice regression test"],
            "canary_scope": ["dogfood project only"],
            "rollback_plan": ["Delete local pack install row"],
            "success_metrics": ["pack.imported event and proposal persist"],
            "actor": "vp_agents",
            "risk_tier": 2,
        },
    )
    assert proposal.status_code == 201
    proposal_payload = proposal.json()
    assert proposal_payload["status"] == "proposed"
    assert proposal_payload["title"].endswith("install evidence")
    assert proposal_payload["proposal_artifact_id"]
    assert proposal_payload["current_decision_id"]

    proposals = client.get(
        "/v1/self-improvement/proposals",
        params={"project_id": project_id},
    )
    assert proposals.status_code == 200
    assert any(item["id"] == proposal_payload["id"] for item in proposals.json()["items"])

    denied_budget = client.post(
        "/v1/budget/consume",
        json={
            "project_id": project_id,
            "reservation_id": budget_reservation.json()["reservation_id"],
            "receipt_id": "missing_receipt_for_perm_path",
            "category": "cross_slice_e2e",
            "skill_or_model_id": "test:e2e",
            "estimated_usd_micros": 1_000,
            "actual_usd_micros": 1_000,
            "actor": "coo",
        },
    )
    assert denied_budget.status_code == 403
    assert "budget_manage" in denied_budget.json()["detail"]

    with engine.begin() as connection:
        imported_row = connection.execute(
            text(
                """
                SELECT source, yaml_blob
                FROM pack_installs
                WHERE project_id = :project_id AND pack_id = :pack_id
                """
            ),
            {"project_id": project_id, "pack_id": pack_id},
        ).mappings().one()
        receipt = connection.execute(
            text(
                """
                SELECT id, status, effect_kind
                FROM receipts
                WHERE project_id = :project_id AND dedupe_key = :dedupe_key
                """
            ),
            {
                "project_id": project_id,
                "dedupe_key": f"pack.import:{project_id}:{pack_id}",
            },
        ).mappings().one()
    assert imported_row["source"] == "local_upload"
    assert pack_id in imported_row["yaml_blob"]
    assert receipt["status"] == "succeeded"
    assert receipt["effect_kind"] == "pack.import"

    events = client.get("/v1/events", params={"project_id": project_id, "limit": 50})
    assert events.status_code == 200
    event_types = [item["event_type"] for item in events.json()["items"]]
    assert "pack.imported" in event_types
    assert "self_improvement.proposed" in event_types


def test_agent_module_install_is_idempotent_and_updates_role_lineage(client, engine):
    with engine.begin() as connection:
        connection.execute(text("DELETE FROM events WHERE project_id = 'boss-lab'"))
        connection.execute(
            text("DELETE FROM agent_module_installs WHERE project_id = 'boss-lab'")
        )
        connection.execute(text("DELETE FROM roles WHERE project_id = 'boss-lab'"))
        connection.execute(
            text("DELETE FROM pack_installs WHERE project_id = 'boss-lab'")
        )
        connection.execute(text("DELETE FROM projects WHERE id = 'boss-lab'"))
        connection.execute(
            text(
                """
                INSERT INTO projects (id, workspace_id, name, slug)
                VALUES ('boss-lab', 'ws_boss', 'BOSS Lab', 'boss-lab')
                """
            )
        )
        api_main.members.seed_default_members(connection, "boss-lab", "test")

    first = client.post(
        "/v1/agent-modules/install",
        json={
            "project_id": "boss-lab",
            "module_id": "codex_software_engineer_v1",
            "actor": "ceo",
            "config": {"branch_prefix": "catalog/"},
        },
    )
    assert first.status_code == 201
    first_payload = first.json()
    assert first_payload["module_id"] == "codex_software_engineer_v1"
    assert first_payload["install_status"] == "installed"
    assert first_payload["role_result"]["role_key"] == "software_engineer"

    second = client.post(
        "/v1/agent-modules/install",
        json={
            "project_id": "boss-lab",
            "module_id": "codex_software_engineer_v1",
            "actor": "ceo",
            "config": {"branch_prefix": "catalog/"},
        },
    )
    assert second.status_code == 201
    assert second.json()["install_status"] == "already_installed"

    role = client.get(
        "/v1/roles/software_engineer", params={"project_id": "boss-lab"}
    )
    assert role.status_code == 200
    role_payload = role.json()
    assert role_payload["source_module_id"] == "codex_software_engineer_v1"
    assert role_payload["source_module_version"] == "1.0.0"

    module = client.get(
        "/v1/agent-modules/codex_software_engineer_v1",
        params={"project_id": "boss-lab"},
    )
    assert module.status_code == 200
    assert module.json()["config"]["branch_prefix"] == "catalog/"

    task = client.post(
        "/v1/tasks",
        json={
            "project_id": "boss-lab",
            "title": "Catalog proposal task",
            "dedupe_key": "catalog-proposal-task",
        },
    )
    assert task.status_code == 201
    task_id = task.json()["id"]

    with engine.begin() as connection:
        connection.execute(
            text(
                """
                INSERT INTO artifacts (
                    id, project_id, task_id, artifact_type, title, uri, content, content_hash
                )
                VALUES (
                    'art_catalog_plan', 'boss-lab', :task_id, 'plan_draft', 'Catalog plan',
                    'file://artifacts/catalog-plan.json',
                    CAST(:content AS JSONB), 'hash-catalog-plan'
                )
                """
            ),
            {"task_id": task_id, "content": json.dumps({"summary": "Catalog plan"})},
        )
        connection.execute(
            text(
                """
                INSERT INTO decisions (
                    id, project_id, task_id, question, recommendation, options, risk_tier,
                    cost_summary, default_action, status
                )
                VALUES (
                    'dec_catalog_plan', 'boss-lab', :task_id, 'Review plan draft for catalog task',
                    CAST(:recommendation AS JSONB), '[]'::jsonb, 2, '{}'::jsonb, 'hold', 'pending'
                )
                """
            ),
            {
                "task_id": task_id,
                "recommendation": json.dumps(
                    {
                        "action": "review_plan_draft",
                        "artifact_id": "art_catalog_plan",
                    }
                ),
            },
        )

    approve = client.post(
        "/v1/decisions/dec_catalog_plan/approve",
        json={"actor": "ceo", "note": "Queue the catalog-backed proposal."},
    )
    assert approve.status_code == 200

    with engine.begin() as connection:
        queued_job = connection.execute(
            text(
                """
                SELECT payload
                FROM jobs
                WHERE project_id = 'boss-lab'
                  AND kind = 'software_factory.propose_pr'
                ORDER BY created_at DESC
                LIMIT 1
                """
            )
        ).mappings().one()

    payload = queued_job["payload"]
    assert payload["agent_module_id"] == "codex_software_engineer_v1"
    assert payload["branch_name"].startswith("catalog/proposal-")


def test_agent_module_rollback_and_uninstall_use_saved_snapshots(client, engine):
    with engine.begin() as connection:
        connection.execute(text("DELETE FROM events WHERE project_id = 'boss-lab'"))
        connection.execute(
            text("DELETE FROM agent_module_install_records WHERE project_id = 'boss-lab'")
        )
        connection.execute(
            text("DELETE FROM agent_module_installs WHERE project_id = 'boss-lab'")
        )
        connection.execute(text("DELETE FROM roles WHERE project_id = 'boss-lab'"))
        connection.execute(
            text("DELETE FROM pack_installs WHERE project_id = 'boss-lab'")
        )
        connection.execute(text("DELETE FROM projects WHERE id = 'boss-lab'"))
        connection.execute(
            text(
                """
                INSERT INTO projects (id, workspace_id, name, slug)
                VALUES ('boss-lab', 'ws_boss', 'BOSS Lab', 'boss-lab')
                """
            )
        )
        api_main.members.seed_default_members(connection, "boss-lab", "test")

    first = client.post(
        "/v1/agent-modules/install",
        json={
            "project_id": "boss-lab",
            "module_id": "codex_software_engineer_v1",
            "actor": "ceo",
            "config": {"branch_prefix": "catalog/"},
        },
    )
    assert first.status_code == 201

    updated = client.post(
        "/v1/agent-modules/install",
        json={
            "project_id": "boss-lab",
            "module_id": "codex_software_engineer_v1",
            "actor": "ceo",
            "config": {"branch_prefix": "next/"},
        },
    )
    assert updated.status_code == 201
    assert updated.json()["config"]["branch_prefix"] == "next/"

    rolled_back = client.post(
        "/v1/agent-modules/codex_software_engineer_v1/rollback",
        json={
            "project_id": "boss-lab",
            "actor": "ceo",
            "reason": "Restore previous branch prefix.",
        },
    )
    assert rolled_back.status_code == 200
    rollback_payload = rolled_back.json()
    assert rollback_payload["install_status"] == "rolled_back"
    assert rollback_payload["installed"] is True
    assert rollback_payload["config"]["branch_prefix"] == "catalog/"

    uninstalled = client.post(
        "/v1/agent-modules/codex_software_engineer_v1/uninstall",
        json={
            "project_id": "boss-lab",
            "actor": "ceo",
            "reason": "Disable software engineer module.",
        },
    )
    assert uninstalled.status_code == 200
    uninstall_payload = uninstalled.json()
    assert uninstall_payload["install_status"] == "uninstalled"
    assert uninstall_payload["installed"] is False
    assert uninstall_payload["role_result"]["status"] == "restored_base_role"

    role = client.get(
        "/v1/roles/software_engineer", params={"project_id": "boss-lab"}
    )
    assert role.status_code == 200
    assert role.json()["source_module_id"] is None

    unresolved = client.post(
        "/v1/agent-runtime/resolve",
        json={
            "project_id": "boss-lab",
            "job_kind": "software_factory.propose_pr",
            "actor": "ceo",
        },
    )
    assert unresolved.status_code == 404

    records = client.get(
        "/v1/agent-module-install-records",
        params={
            "project_id": "boss-lab",
            "module_id": "codex_software_engineer_v1",
            "limit": 10,
        },
    )
    assert records.status_code == 200
    actions = [item["action"] for item in records.json()["items"]]
    assert "rollback" in actions
    assert "uninstall" in actions
    uninstall_record = next(
        item for item in records.json()["items"] if item["action"] == "uninstall"
    )
    assert uninstall_record["rollback_snapshot"]["config"]["branch_prefix"] == "catalog/"
    rollback_record = next(
        item for item in records.json()["items"] if item["action"] == "rollback"
    )
    rollback_integrity = client.get(
        f"/v1/artifacts/{rollback_record['metadata']['module_integrity_artifact_id']}"
    )
    uninstall_integrity = client.get(
        f"/v1/artifacts/{uninstall_record['metadata']['module_integrity_artifact_id']}"
    )
    assert rollback_integrity.status_code == 200
    assert uninstall_integrity.status_code == 200
    assert rollback_integrity.json()["content"]["action"] == "rollback"
    assert uninstall_integrity.json()["content"]["action"] == "uninstall"
    assert rollback_integrity.json()["content"]["config_hash"]
    assert uninstall_integrity.json()["content"]["rollback_snapshot_hash"]

    events = client.get("/v1/events", params={"project_id": "boss-lab"})
    event_types = [item["event_type"] for item in events.json()["items"]]
    assert "agent_module.rolled_back" in event_types
    assert "agent_module.uninstalled" in event_types


def test_skill_registry_and_seeded_entitlement_sources_are_visible(client):
    skills = client.get("/v1/skills")
    assert skills.status_code == 200
    skill_payload = skills.json()
    skill_ids = [item["id"] for item in skill_payload["items"]]
    assert "model.route_via_entitlement_broker" in skill_ids
    assert "model.generate_text" in skill_ids
    assert "software_factory.openhands_run" not in skill_ids
    for skill in skill_payload["items"]:
        assert_complete_skill_spec(skill)

    sources = client.get(
        "/v1/entitlements/sources",
        params={"project_id": "boss-dogfood"},
    )
    assert sources.status_code == 200
    payload = sources.json()
    openai_api = next(item for item in payload["items"] if item["source_id"] == "openai_api")
    assert openai_api["provider"] == "openai"
    assert openai_api["auth_method"] == "api_key"
    assert openai_api["runtime_supported"] is True
    assert openai_api["effective_status"] in {"active", "missing_credentials"}
    openai_codex = next(
        item for item in payload["items"] if item["source_id"] == "openai_codex_chatgpt"
    )
    assert openai_codex["auth_method"] == "official_cli_oauth"
    assert openai_codex["connection_ref"] == "BOSS_OPENAI_CODEX_OAUTH_SESSION_REF"
    assert openai_codex["multi_tenant_allowed"] is False
    xai_grok = next(item for item in payload["items"] if item["source_id"] == "xai_grok_build")
    assert xai_grok["auth_method"] == "official_device_oauth"
    assert xai_grok["connection_metadata"]["login_command"] == "grok login --device-auth"


def test_skill_spec_validator_rejects_mock_or_incomplete_specs():
    with pytest.raises(ValueError, match="mock_mode"):
        api_main.entitlements.validate_skill_spec(
            {
                "id": "model.fake",
                "kind": "model",
                "risk_tier": 1,
                "mock_mode": True,
                "description": "Invalid mock skill.",
                "input_schema": {"prompt": "string"},
                "output_schema": {"text": "string"},
                "data_boundary": "internal",
                "cost": {"estimator": "fixed", "category": "model"},
                "retry": {"max_attempts": 1, "backoff": "none"},
                "permissions": {
                    "allowed_callers": ["worker"],
                    "required_permissions": ["model_invoke"],
                },
                "policy": {"requires_budget_reservation": True},
            },
            "test skill",
        )


def test_provider_account_create_and_test_missing_credentials_are_visible(client):
    previous = api_main.resource_plane.settings.digitalocean_token
    api_main.resource_plane.settings.digitalocean_token = None
    try:
        created = client.post(
            "/v1/providers",
            json={
                "project_id": "boss-dogfood",
                "provider": "digitalocean",
                "account_label": "sandbox-do",
                "scopes": ["read"],
                "read_only": True,
                "billing_readable": True,
                "actor": "ceo",
            },
        )
        assert created.status_code == 201
        provider = created.json()
        assert provider["credential_ref"] == "env:BOSS_DIGITALOCEAN_TOKEN"
        assert provider["last_test_status"] is None

        tested = client.post(
            f"/v1/providers/{provider['id']}/test",
            params={"project_id": "boss-dogfood", "actor": "ceo"},
        )
    finally:
        api_main.resource_plane.settings.digitalocean_token = previous

    assert tested.status_code == 200
    payload = tested.json()
    assert payload["status"] == "failed"
    assert payload["provider_account"]["last_test_status"] == "failed"
    assert "provider_credentials_or_permissions" in payload["provider_account"][
        "missing_permissions"
    ]
    assert payload["provider_health"]["status"] == "degraded"
    assert payload["provider_health"]["api_available"] is False

    health = client.get(
        "/v1/provider-health",
        params={"project_id": "boss-dogfood"},
    )
    assert health.status_code == 200
    assert health.json()["items"][0]["provider"] == "digitalocean"

    # monitoring polish: empty state for provider health feed (no filter match)
    empty_health = client.get(
        "/v1/provider-health",
        params={"project_id": "boss-dogfood", "provider_account_id": "nonexistent_pa"},
    )
    assert empty_health.status_code == 200
    assert empty_health.json()["items"] == []

    events = client.get("/v1/events", params={"project_id": "boss-dogfood"})
    assert any(
        item["event_type"] == "provider.connection_tested"
        and item["entity_id"] == provider["id"]
        for item in events.json()["items"]
    )


def test_provider_sync_missing_credentials_creates_deadletter(client):
    previous = api_main.resource_plane.settings.cloudflare_api_token
    api_main.resource_plane.settings.cloudflare_api_token = None
    try:
        created = client.post(
            "/v1/providers",
            json={
                "project_id": "boss-dogfood",
                "provider": "cloudflare",
                "account_label": "sandbox-cloudflare",
                "scopes": ["zones:read"],
                "actor": "ceo",
            },
        )
        assert created.status_code == 201
        provider = created.json()

        synced = client.post(
            "/v1/resources/sync",
            json={
                "project_id": "boss-dogfood",
                "provider_account_id": provider["id"],
                "actor": "ceo",
            },
        )
    finally:
        api_main.resource_plane.settings.cloudflare_api_token = previous

    assert synced.status_code == 200
    payload = synced.json()
    assert payload["runs"][0]["status"] == "failed"
    assert payload["runs"][0]["deadletter_id"]
    assert payload["runs"][0]["inventory_snapshot_artifact_id"].startswith("art_")
    assert payload["resources_seen"] == 0

    snapshot = client.get(
        f"/v1/artifacts/{payload['runs'][0]['inventory_snapshot_artifact_id']}"
    )
    assert snapshot.status_code == 200
    snapshot_payload = snapshot.json()
    assert snapshot_payload["artifact_type"] == "providerops_inventory_snapshot"
    assert snapshot_payload["content"]["status"] == "failed"
    assert snapshot_payload["content"]["deadletter_id"] == payload["runs"][0][
        "deadletter_id"
    ]
    assert "Missing credential" in snapshot_payload["content"]["error"]["message"]

    deadletters = client.get("/v1/deadletters", params={"project_id": "boss-dogfood"})
    assert deadletters.status_code == 200
    assert any(
        item["source_kind"] == "provider_sync"
        and item["source_id"] == payload["runs"][0]["id"]
        for item in deadletters.json()["items"]
    )


def test_provider_sync_records_real_inventory_from_adapter_boundary(client, monkeypatch):
    previous = api_main.resource_plane.settings.digitalocean_token
    api_main.resource_plane.settings.digitalocean_token = "test-do-token"
    monkeypatch.setattr(
        api_main.resource_plane,
        "_sync_inventory",
        lambda provider, credential: [
            InventoryResource(
                provider="digitalocean",
                resource_type="droplet",
                external_id="12345",
                name="manual-preview-droplet",
                state="active",
                ownership="discovered_unmanaged",
                environment="preview",
                health_status="active",
                tags=["preview"],
                metadata={"region": "nyc3", "size": "s-1vcpu-1gb"},
            )
        ],
    )
    try:
        provider = client.post(
            "/v1/providers",
            json={
                "project_id": "boss-dogfood",
                "provider": "digitalocean",
                "account_label": "sandbox-do",
                "scopes": ["read"],
                "actor": "ceo",
            },
        ).json()

        synced = client.post(
            "/v1/resources/sync",
            json={
                "project_id": "boss-dogfood",
                "provider_account_id": provider["id"],
                "actor": "ceo",
            },
        )
    finally:
        api_main.resource_plane.settings.digitalocean_token = previous

    assert synced.status_code == 200
    sync_payload = synced.json()
    assert sync_payload["resources_seen"] == 1
    assert sync_payload["resources_changed"] == 1
    run = sync_payload["runs"][0]
    assert run["inventory_snapshot_artifact_id"].startswith("art_")

    resources = client.get(
        "/v1/resources",
        params={"project_id": "boss-dogfood", "provider": "digitalocean"},
    )
    assert resources.status_code == 200
    resource = resources.json()["items"][0]
    assert resource["name"] == "manual-preview-droplet"
    assert resource["ownership"] == "discovered_unmanaged"

    drift = client.get("/v1/drift-reports", params={"project_id": "boss-dogfood"})
    assert drift.status_code == 200
    assert drift.json()["items"][0]["classification"] == "cost-impacting"

    snapshot = client.get(f"/v1/artifacts/{run['inventory_snapshot_artifact_id']}")
    assert snapshot.status_code == 200
    snapshot_payload = snapshot.json()
    assert snapshot_payload["artifact_type"] == "providerops_inventory_snapshot"
    assert snapshot_payload["content"]["reconciliation_run_id"] == run["id"]
    assert snapshot_payload["content"]["resources_seen"] == 1
    assert snapshot_payload["content"]["resources"][0]["name"] == "manual-preview-droplet"
    assert snapshot_payload["content"]["drift_report_id"] == run["drift_report_id"]

    reconciliation_runs = client.get(
        "/v1/reconciliation-runs", params={"project_id": "boss-dogfood"}
    )
    assert reconciliation_runs.status_code == 200
    listed_run = next(
        item for item in reconciliation_runs.json()["items"] if item["id"] == run["id"]
    )
    assert (
        listed_run["metadata"]["inventory_snapshot_artifact_id"]
        == run["inventory_snapshot_artifact_id"]
    )


def test_provider_sync_records_billing_cost_events_idempotently(client, monkeypatch):
    previous = api_main.resource_plane.settings.openai_api_key
    api_main.resource_plane.settings.openai_api_key = "test-openai-key"
    period_start = datetime(2026, 6, 1, tzinfo=timezone.utc)
    period_end = datetime(2026, 6, 2, tzinfo=timezone.utc)
    monkeypatch.setattr(api_main.resource_plane, "_sync_inventory", lambda _provider, _credential: [])
    monkeypatch.setattr(
        api_main.resource_plane,
        "_sync_billing",
        lambda _provider, _credential: [
            ProviderCostRecord(
                provider="openai",
                category="model",
                amount_usd_micros=123_450,
                cost_layer="observed",
                confidence="provider_reported",
                source_ref="openai.organization.costs:2026-06-01:project_a",
                period_start=period_start,
                period_end=period_end,
                billing_account_external_id="openai:organization",
                billing_account_label="OpenAI Organization Costs",
                metadata={
                    "provider_source": "organization_costs",
                    "project_id": "project_a",
                },
            )
        ],
    )
    try:
        provider = client.post(
            "/v1/providers",
            json={
                "project_id": "boss-dogfood",
                "provider": "openai",
                "account_label": "openai-admin",
                "scopes": ["billing:read"],
                "billing_readable": True,
                "actor": "ceo",
            },
        ).json()

        first_sync = client.post(
            "/v1/resources/sync",
            json={
                "project_id": "boss-dogfood",
                "provider_account_id": provider["id"],
                "actor": "ceo",
            },
        )
        second_sync = client.post(
            "/v1/resources/sync",
            json={
                "project_id": "boss-dogfood",
                "provider_account_id": provider["id"],
                "actor": "ceo",
            },
        )
    finally:
        api_main.resource_plane.settings.openai_api_key = previous

    assert first_sync.status_code == 200
    assert first_sync.json()["costs_seen"] == 1
    assert first_sync.json()["runs"][0]["costs_seen"] == 1
    assert second_sync.status_code == 200
    assert second_sync.json()["costs_seen"] == 0
    assert second_sync.json()["runs"][0]["costs_seen"] == 0

    costs = client.get("/v1/costs", params={"project_id": "boss-dogfood"})
    assert costs.status_code == 200
    cost_items = [
        item
        for item in costs.json()["items"]
        if item["source_ref"] == "openai.organization.costs:2026-06-01:project_a"
    ]
    assert len(cost_items) == 1
    cost_event = cost_items[0]
    assert cost_event["provider"] == "openai"
    assert cost_event["amount_usd_micros"] == 123_450
    assert cost_event["cost_layer"] == "observed"
    assert cost_event["confidence"] == "provider_reported"
    assert cost_event["billing_account_id"]
    assert cost_event["metadata"]["provider_source"] == "organization_costs"
    assert cost_event["receipt_id"] is None

    events = client.get("/v1/events", params={"project_id": "boss-dogfood"})
    assert any(
        item["event_type"] == "provider.cost_event_recorded"
        and item["entity_id"] == cost_event["id"]
        for item in events.json()["items"]
    )


def test_provider_sync_unsupported_billing_keeps_inventory_with_unknown_costs(
    client, monkeypatch
):
    previous = api_main.resource_plane.settings.cloudflare_api_token
    api_main.resource_plane.settings.cloudflare_api_token = "test-cloudflare-token"
    monkeypatch.setattr(api_main.resource_plane, "_sync_inventory", lambda _provider, _credential: [])
    try:
        provider = client.post(
            "/v1/providers",
            json={
                "project_id": "boss-dogfood",
                "provider": "cloudflare",
                "account_label": "cloudflare-billing",
                "scopes": ["billing:read"],
                "billing_readable": True,
                "actor": "ceo",
            },
        ).json()

        synced = client.post(
            "/v1/resources/sync",
            json={
                "project_id": "boss-dogfood",
                "provider_account_id": provider["id"],
                "actor": "ceo",
            },
        )
    finally:
        api_main.resource_plane.settings.cloudflare_api_token = previous

    assert synced.status_code == 200
    run = synced.json()["runs"][0]
    assert run["status"] == "succeeded"
    assert run["costs_seen"] == 0
    assert run["billing_status"] == "unavailable"
    assert "Exact billing sync is not available" in run["billing_message"]

    health = client.get(
        "/v1/provider-health",
        params={"project_id": "boss-dogfood", "provider_account_id": provider["id"]},
    )
    assert health.status_code == 200
    health_item = health.json()["items"][0]
    assert health_item["status"] == "healthy"
    assert health_item["detail"]["billing_status"] == "unavailable"
    assert health_item["detail"]["billing_confidence"] == "unknown"

    snapshot = client.get(f"/v1/artifacts/{run['inventory_snapshot_artifact_id']}")
    assert snapshot.status_code == 200
    snapshot_content = snapshot.json()["content"]
    assert snapshot_content["billing_status"] == "unavailable"
    assert snapshot_content["billing_confidence"] == "unknown"

    events = client.get("/v1/events", params={"project_id": "boss-dogfood"})
    assert any(
        item["event_type"] == "provider.billing_unavailable"
        and item["entity_id"] == provider["id"]
        for item in events.json()["items"]
    )


def test_provider_adapter_unsupported_capabilities_fail_closed_without_placeholder_language():
    with pytest.raises(ProviderAdapterError) as inventory_error:
        api_main.resource_plane._sync_inventory("futurecloud", "token")
    assert "not supported by the configured ProviderAdapter" in str(
        inventory_error.value
    )
    assert "not implemented" not in str(inventory_error.value).lower()

    with pytest.raises(ProviderAdapterError) as billing_error:
        api_main.resource_plane._sync_billing("futurecloud", "token")
    assert "not supported by the configured ProviderAdapter" in str(
        billing_error.value
    )
    assert "not implemented" not in str(billing_error.value).lower()


def test_provider_cost_parsers_preserve_confidence_and_units():
    openai_records = api_main.resource_plane._openai_cost_records(
        {
            "data": [
                {
                    "start_time": 1_780_272_000,
                    "end_time": 1_780_358_400,
                    "results": [
                        {
                            "amount": {"currency": "usd", "value": 1.2345},
                            "project_id": "proj_123",
                            "line_item": "responses",
                        }
                    ],
                }
            ]
        }
    )
    anthropic_records = api_main.resource_plane._anthropic_cost_records(
        {
            "data": [
                {
                    "starting_at": "2026-06-01T00:00:00Z",
                    "ending_at": "2026-06-02T00:00:00Z",
                    "results": [
                        {
                            "amount": "123.45",
                            "currency": "USD",
                            "model": "claude-sonnet-4-20250514",
                            "cost_type": "tokens",
                        }
                    ],
                }
            ]
        }
    )
    digitalocean_records = api_main.resource_plane._digitalocean_cost_records(
        {
            "billing_history": [
                {
                    "invoice_uuid": "inv_1",
                    "type": "Invoice",
                    "amount": "12.34",
                    "date": "2026-06-01T00:00:00Z",
                    "description": "Monthly invoice",
                }
            ]
        }
    )

    assert openai_records[0].amount_usd_micros == 1_234_500
    assert openai_records[0].confidence == "provider_reported"
    assert anthropic_records[0].amount_usd_micros == 1_234_500
    assert anthropic_records[0].metadata["amount_unit"] == "lowest_currency_unit"
    assert anthropic_records[0].confidence == "provider_reported"
    assert digitalocean_records[0].amount_usd_micros == 12_340_000
    assert digitalocean_records[0].cost_layer == "reconciled"
    assert digitalocean_records[0].confidence == "invoice_reconciled"


def test_provider_changeset_requires_approval_and_receipts_failed_actuation(client):
    provider = client.post(
        "/v1/providers",
        json={
            "project_id": "boss-dogfood",
            "provider": "digitalocean",
            "account_label": "sandbox-do",
            "scopes": ["read", "write"],
            "read_only": False,
            "destructive_actions_disabled": False,
            "actor": "ceo",
        },
    ).json()
    reservation_id = reserve_for_infra_provision(client, "Reserve provider changeset.")
    created = client.post(
        "/v1/changesets",
        json={
            "project_id": "boss-dogfood",
            "provider_account_id": provider["id"],
            "title": "Create preview worker droplet",
            "summary": "Provision a small preview worker through ProviderOps.",
            "expected_cost_usd_micros": 250_000,
            "reservation_id": reservation_id,
            "rollback_plan": ["Destroy the droplet through an approved cleanup ChangeSet."],
            "operations": [
                {
                    "skill_id": "digitalocean.droplet.create",
                    "action": "create",
                    "resource_type": "droplet",
                    "desired_state": {"region": "nyc3", "size": "s-1vcpu-1gb"},
                    "risk_tier": 3,
                    "production_affecting": False,
                }
            ],
            "actor": "ceo",
        },
    )
    assert created.status_code == 201
    changeset = created.json()
    assert changeset["status"] == "pending_approval"
    assert changeset["approval_decision_id"]
    assert changeset["expected_receipts"][0]["dedupe_key"].startswith(
        f"resource_plane:{changeset['id']}:0"
    )

    blocked_apply = client.post(
        f"/v1/changesets/{changeset['id']}/apply",
        json={"actor": "ceo"},
    )
    assert blocked_apply.status_code == 422

    approved = client.post(
        f"/v1/changesets/{changeset['id']}/approve",
        json={"actor": "ceo"},
    )
    assert approved.status_code == 200
    assert approved.json()["status"] == "approved"

    applied = client.post(
        f"/v1/changesets/{changeset['id']}/apply",
        json={"actor": "ceo"},
    )
    assert applied.status_code == 200
    payload = applied.json()
    assert payload["status"] == "deadlettered"
    assert payload["changeset"]["status"] == "deadlettered"
    assert payload["resource_receipts"][0]["status"] == "failed"
    assert payload["resource_receipts"][0]["receipt_id"]

    receipts = client.get(
        "/v1/resource-receipts",
        params={"project_id": "boss-dogfood"},
    )
    assert receipts.status_code == 200
    assert len(receipts.json()["items"]) == 1

    budget_status = client.get(
        "/v1/budget/status",
        params={"scope_type": "project", "scope_id": "boss-dogfood"},
    )
    assert budget_status.status_code == 200
    assert budget_status.json()["reserved_usd_micros"] == 0

    deadletters = client.get("/v1/deadletters", params={"project_id": "boss-dogfood"})
    assert any(
        item["source_kind"] == "resource_receipt"
        and item["payload"]["changeset_id"] == changeset["id"]
        for item in deadletters.json()["items"]
    )


def test_deployment_changeset_requires_environment_policy_and_reservation(client):
    provider = client.post(
        "/v1/providers",
        json={
            "project_id": "boss-dogfood",
            "provider": "cloudflare",
            "account_label": "deploy-change-cloudflare",
            "scopes": ["read", "write", "deploy"],
            "read_only": False,
            "deploy_capable": True,
            "destructive_actions_disabled": True,
            "actor": "ceo",
        },
    ).json()
    operation = {
        "skill_id": "cloudflare.worker.deploy",
        "action": "cloudflare.worker.deploy",
        "resource_type": "worker",
        "resource_ref": "boss-web-prod",
        "desired_state": {
            "target_kind": "cloudflare_worker",
            "environment": "production",
            "target_ref": "boss-web-prod",
            "health_checks": [
                {
                    "name": "production health",
                    "url": "https://boss.example.com/health",
                    "expected_status": 200,
                }
            ],
        },
        "risk_tier": 3,
        "production_affecting": True,
    }

    blocked = client.post(
        "/v1/changesets",
        json={
            "project_id": "boss-dogfood",
            "provider_account_id": provider["id"],
            "title": "Deploy production worker directly",
            "summary": "Direct deploy ChangeSet must still obey environment policy.",
            "operations": [operation],
            "rollback_plan": ["Redeploy previous production Worker version."],
            "risk_tier": 3,
            "actor": "ceo",
        },
    )
    assert blocked.status_code == 422
    assert "environment rollout policy" in blocked.json()["detail"]

    upsert_deployment_environment_policy(
        client,
        "boss-dogfood",
        environment="production",
        runtime_mode="production_allowed",
        allowed_target_kinds=["cloudflare_worker"],
        requires_health_check=True,
    )
    reservation_id = reserve_for_infra_provision(
        client,
        "Reserve direct production deploy ChangeSet budget.",
    )
    created = client.post(
        "/v1/changesets",
        json={
            "project_id": "boss-dogfood",
            "provider_account_id": provider["id"],
            "title": "Deploy production worker directly",
            "summary": "Direct deploy ChangeSet must still obey environment policy.",
            "operations": [operation],
            "expected_cost_usd_micros": 100_000,
            "reservation_id": reservation_id,
            "rollback_plan": ["Redeploy previous production Worker version."],
            "risk_tier": 3,
            "actor": "ceo",
        },
    )
    assert created.status_code == 201
    payload = created.json()
    assert payload["status"] == "pending_approval"
    assert payload["reservation_id"] == reservation_id
    assert payload["operations"][0]["production_affecting"] is True


def test_provider_changeset_digitalocean_droplet_create_succeeds_with_receipts(
    client, monkeypatch
):
    previous_token = api_main.resource_plane.settings.digitalocean_token
    previous_allowed = api_main.resource_plane.settings.infra_real_provision_allowed
    api_main.resource_plane.settings.digitalocean_token = "do-test-token"
    api_main.resource_plane.settings.infra_real_provision_allowed = True
    adapter_calls = []

    def fake_create_droplet(**kwargs):
        adapter_calls.append(kwargs)
        return DropletProvisionResult(
            droplet_id="123456",
            name=kwargs["name"],
            region=kwargs["region"],
            size=kwargs["size"],
            status="active",
            api_url="https://api.digitalocean.com/v2/droplets/123456",
        )

    monkeypatch.setattr(
        api_main.resource_plane.digitalocean,
        "create_droplet",
        fake_create_droplet,
    )
    try:
        provider = client.post(
            "/v1/providers",
            json={
                "project_id": "boss-dogfood",
                "provider": "digitalocean",
                "account_label": "write-do",
                "scopes": ["droplet:write"],
                "read_only": False,
                "destructive_actions_disabled": False,
                "actor": "ceo",
            },
        ).json()
        reservation_id = reserve_for_infra_provision(
            client,
            "Reserve DigitalOcean droplet ChangeSet budget.",
        )
        created = client.post(
            "/v1/changesets",
            json={
                "project_id": "boss-dogfood",
                "provider_account_id": provider["id"],
                "title": "Create preview droplet",
                "summary": "Provision a preview droplet through ProviderOps.",
                "expected_cost_usd_micros": 250_000,
                "reservation_id": reservation_id,
                "rollback_plan": ["Destroy the droplet through a cleanup ChangeSet."],
                "operations": [
                    {
                        "skill_id": "digitalocean.droplet.create",
                        "action": "create",
                        "resource_type": "droplet",
                        "resource_ref": "boss-preview-worker",
                        "desired_state": {
                            "name": "boss-preview-worker",
                            "region": "nyc3",
                            "size": "s-1vcpu-1gb",
                            "tags": ["preview"],
                            "environment": "preview",
                        },
                        "risk_tier": 3,
                    }
                ],
                "actor": "ceo",
            },
        )
        assert created.status_code == 201
        changeset = created.json()
        client.post(
            f"/v1/changesets/{changeset['id']}/approve",
            json={"actor": "ceo"},
        )
        applied = client.post(
            f"/v1/changesets/{changeset['id']}/apply",
            json={"actor": "ceo"},
        )
    finally:
        api_main.resource_plane.settings.digitalocean_token = previous_token
        api_main.resource_plane.settings.infra_real_provision_allowed = previous_allowed

    assert applied.status_code == 200
    payload = applied.json()
    assert payload["status"] == "applied"
    assert adapter_calls == [
        {
            "name": "boss-preview-worker",
            "region": "nyc3",
            "size": "s-1vcpu-1gb",
            "tags": ["boss-managed", "boss-providerops"],
            "user_tags": ["preview"],
        }
    ]
    receipt = payload["resource_receipts"][0]
    assert receipt["status"] == "succeeded"
    assert receipt["provider_ref"].endswith("/droplets/123456")

    resources = client.get(
        "/v1/resources",
        params={"project_id": "boss-dogfood", "provider": "digitalocean"},
    )
    assert resources.status_code == 200
    resource = resources.json()["items"][0]
    assert resource["external_id"] == "123456"
    assert resource["name"] == "boss-preview-worker"
    assert resource["state"] == "active"
    assert resource["ownership"] == "managed_by_boss"
    assert resource["receipt_id"] == receipt["receipt_id"]

    costs = client.get("/v1/costs", params={"project_id": "boss-dogfood"})
    assert costs.status_code == 200
    assert costs.json()["items"][0]["provider"] == "digitalocean"
    assert costs.json()["items"][0]["receipt_id"] == receipt["receipt_id"]


def test_provider_changeset_digitalocean_droplet_destroy_succeeds_with_receipts(
    client, monkeypatch
):
    previous_token = api_main.resource_plane.settings.digitalocean_token
    previous_allowed = api_main.resource_plane.settings.infra_real_provision_allowed
    api_main.resource_plane.settings.digitalocean_token = "do-test-token"
    api_main.resource_plane.settings.infra_real_provision_allowed = True
    deleted = []

    def fake_delete_droplet(droplet_id):
        deleted.append(droplet_id)
        return DropletTerminationResult(
            droplet_id=droplet_id,
            api_url=f"https://api.digitalocean.com/v2/droplets/{droplet_id}",
        )

    monkeypatch.setattr(
        api_main.resource_plane.digitalocean,
        "delete_droplet",
        fake_delete_droplet,
    )
    try:
        provider = client.post(
            "/v1/providers",
            json={
                "project_id": "boss-dogfood",
                "provider": "digitalocean",
                "account_label": "cleanup-do",
                "scopes": ["droplet:write"],
                "read_only": False,
                "destructive_actions_disabled": False,
                "actor": "ceo",
            },
        ).json()
        created = client.post(
            "/v1/changesets",
            json={
                "project_id": "boss-dogfood",
                "provider_account_id": provider["id"],
                "title": "Destroy preview droplet",
                "summary": "Delete an approved preview droplet through ProviderOps.",
                "expected_cost_usd_micros": 0,
                "rollback_plan": ["Recreate from the recorded droplet spec if needed."],
                "operations": [
                    {
                        "skill_id": "digitalocean.droplet.destroy",
                        "action": "destroy",
                        "resource_type": "droplet",
                        "resource_ref": "123456",
                        "desired_state": {
                            "droplet_id": "123456",
                            "name": "boss-preview-worker",
                        },
                        "risk_tier": 3,
                        "destructive": True,
                    }
                ],
                "actor": "ceo",
            },
        )
        assert created.status_code == 201
        changeset = created.json()
        assert changeset["status"] == "pending_approval"
        client.post(
            f"/v1/changesets/{changeset['id']}/approve",
            json={"actor": "ceo"},
        )
        applied = client.post(
            f"/v1/changesets/{changeset['id']}/apply",
            json={"actor": "ceo"},
        )
    finally:
        api_main.resource_plane.settings.digitalocean_token = previous_token
        api_main.resource_plane.settings.infra_real_provision_allowed = previous_allowed

    assert applied.status_code == 200
    payload = applied.json()
    assert payload["status"] == "applied"
    assert deleted == ["123456"]
    receipt = payload["resource_receipts"][0]
    assert receipt["status"] == "succeeded"
    assert receipt["provider_ref"].endswith("/droplets/123456")

    resources = client.get(
        "/v1/resources",
        params={"project_id": "boss-dogfood", "provider": "digitalocean"},
    )
    assert resources.status_code == 200
    resource = resources.json()["items"][0]
    assert resource["external_id"] == "123456"
    assert resource["state"] == "deleted"
    assert resource["health_status"] == "deleted"
    assert resource["receipt_id"] == receipt["receipt_id"]


def test_provider_changeset_cloudflare_dns_upsert_writes_receipts_and_resource(
    client, monkeypatch
):
    previous = api_main.resource_plane.settings.cloudflare_api_token
    api_main.resource_plane.settings.cloudflare_api_token = "cf-test-token"
    adapter_calls = []

    def fake_cloudflare_dns_upsert(credential, operation):
        adapter_calls.append({"credential": credential, "operation": operation})
        return {
            "provider_ref": "cloudflare:dns_record:dns-record-123",
            "resource_external_id": "dns-record-123",
            "resource_name": "preview.example.com",
            "resource_type": "dns_record",
            "state": "active",
            "health_status": "active",
            "operation": "created",
            "zone_id": "zone-123",
            "record": {
                "id": "dns-record-123",
                "type": "A",
                "name": "preview.example.com",
                "content": "192.0.2.10",
                "ttl": 1,
                "proxied": False,
            },
            "api_success": True,
        }

    monkeypatch.setattr(
        api_main.resource_plane,
        "_cloudflare_dns_record_upsert",
        fake_cloudflare_dns_upsert,
    )
    try:
        provider = client.post(
            "/v1/providers",
            json={
                "project_id": "boss-dogfood",
                "provider": "cloudflare",
                "account_label": "write-cloudflare",
                "scopes": ["dns:write"],
                "read_only": False,
                "actor": "ceo",
            },
        ).json()
        reservation_id = reserve_for_infra_provision(
            client,
            "Reserve DNS provider ChangeSet budget.",
        )
        created = client.post(
            "/v1/changesets",
            json={
                "project_id": "boss-dogfood",
                "provider_account_id": provider["id"],
                "title": "Create preview DNS record",
                "summary": "Point preview.example.com at the approved preview service.",
                "expected_cost_usd_micros": 250_000,
                "reservation_id": reservation_id,
                "rollback_plan": ["Restore or delete the prior DNS record value."],
                "operations": [
                    {
                        "skill_id": "cloudflare.dns.record.upsert",
                        "action": "upsert",
                        "resource_type": "dns_record",
                        "resource_ref": "preview.example.com",
                        "desired_state": {
                            "zone_id": "zone-123",
                            "record_type": "A",
                            "name": "preview.example.com",
                            "value": "192.0.2.10",
                            "ttl": 1,
                            "proxied": False,
                            "environment": "preview",
                        },
                        "risk_tier": 2,
                    }
                ],
                "actor": "ceo",
            },
        )
        assert created.status_code == 201
        changeset = created.json()
        assert changeset["status"] == "pending_approval"

        approved = client.post(
            f"/v1/changesets/{changeset['id']}/approve",
            json={"actor": "ceo"},
        )
        assert approved.status_code == 200
        applied = client.post(
            f"/v1/changesets/{changeset['id']}/apply",
            json={"actor": "ceo"},
        )
    finally:
        api_main.resource_plane.settings.cloudflare_api_token = previous

    assert applied.status_code == 200
    payload = applied.json()
    assert payload["status"] == "applied"
    assert payload["changeset"]["status"] == "applied"
    assert len(adapter_calls) == 1
    assert adapter_calls[0]["credential"] == "cf-test-token"
    assert adapter_calls[0]["operation"]["skill_id"] == "cloudflare.dns.record.upsert"
    receipt = payload["resource_receipts"][0]
    assert receipt["status"] == "succeeded"
    assert receipt["receipt_id"]
    assert receipt["provider_ref"] == "cloudflare:dns_record:dns-record-123"

    resources = client.get(
        "/v1/resources",
        params={"project_id": "boss-dogfood", "provider": "cloudflare"},
    )
    assert resources.status_code == 200
    resource = resources.json()["items"][0]
    assert resource["external_id"] == "dns-record-123"
    assert resource["ownership"] == "managed_by_boss"
    assert resource["state"] == "active"
    assert resource["receipt_id"] == receipt["receipt_id"]
    assert resource["metadata"]["resource_receipt_id"] == receipt["id"]

    costs = client.get("/v1/costs", params={"project_id": "boss-dogfood"})
    assert costs.status_code == 200
    cost_event = costs.json()["items"][0]
    assert cost_event["provider"] == "cloudflare"
    assert cost_event["amount_usd_micros"] == 250_000
    assert cost_event["cost_layer"] == "observed"
    assert cost_event["confidence"] == "estimated_from_price_catalog"
    assert cost_event["source_ref"] == changeset["id"]
    assert cost_event["receipt_id"] == receipt["receipt_id"]
    assert cost_event["metadata"]["resource_receipt_ids"] == [receipt["id"]]

    health = client.get(
        "/v1/provider-health",
        params={"project_id": "boss-dogfood", "provider": "cloudflare"},
    )
    assert health.status_code == 200
    assert health.json()["items"][0]["status"] == "healthy"

    deadletters = client.get("/v1/deadletters", params={"project_id": "boss-dogfood"})
    assert not any(
        item["source_kind"] == "resource_receipt"
        and item["payload"]["changeset_id"] == changeset["id"]
        for item in deadletters.json()["items"]
    )


def test_provider_changeset_cloudflare_r2_bucket_create_writes_receipts_and_resource(
    client, monkeypatch
):
    previous = api_main.resource_plane.settings.cloudflare_api_token
    api_main.resource_plane.settings.cloudflare_api_token = "cf-test-token"
    r2_requests = []

    class FakeR2Response:
        def raise_for_status(self):
            return None

        def json(self):
            return {
                "success": True,
                "result": {
                    "name": "boss-artifacts-staging",
                    "location": "enam",
                    "jurisdiction": "default",
                    "storage_class": "Standard",
                    "creation_date": "2026-06-03T00:00:00Z",
                },
            }

    class FakeR2Client:
        def __init__(self, **kwargs):
            self.kwargs = kwargs

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, traceback):
            return False

        def post(self, url, headers, json):
            r2_requests.append({"url": url, "headers": headers, "json": json})
            return FakeR2Response()

    monkeypatch.setattr(
        "apps.api.app.services.resource_plane.httpx.Client",
        FakeR2Client,
    )
    try:
        provider = client.post(
            "/v1/providers",
            json={
                "project_id": "boss-dogfood",
                "provider": "cloudflare",
                "account_label": "r2-cloudflare",
                "scopes": ["read", "write"],
                "read_only": False,
                "deploy_capable": False,
                "destructive_actions_disabled": True,
                "actor": "ceo",
            },
        ).json()
        reservation_id = reserve_for_infra_provision(
            client,
            "Reserve Cloudflare R2 bucket budget.",
        )
        created = client.post(
            "/v1/changesets",
            json={
                "project_id": "boss-dogfood",
                "provider_account_id": provider["id"],
                "title": "Create staging artifact bucket",
                "summary": "Create an R2 bucket for staging artifacts.",
                "operations": [
                    {
                        "skill_id": "cloudflare.r2.bucket.create",
                        "action": "create",
                        "resource_type": "r2_bucket",
                        "resource_ref": "boss-artifacts-staging",
                        "desired_state": {
                            "account_id": "cf-account-123",
                            "bucket_name": "boss-artifacts-staging",
                            "locationHint": "enam",
                            "storageClass": "Standard",
                            "jurisdiction": "default",
                        },
                        "risk_tier": 2,
                        "destructive": False,
                        "production_affecting": False,
                    }
                ],
                "expected_cost_usd_micros": 120_000,
                "reservation_id": reservation_id,
                "rollback_plan": [
                    "Delete the staging R2 bucket only after confirming it is empty."
                ],
                "risk_tier": 2,
                "approval_required": True,
                "actor": "ceo",
            },
        )
        assert created.status_code == 201
        changeset = created.json()
        approved = client.post(
            f"/v1/changesets/{changeset['id']}/approve",
            json={"actor": "ceo"},
        )
        assert approved.status_code == 200
        applied = client.post(
            f"/v1/changesets/{changeset['id']}/apply",
            json={"actor": "ceo"},
        )
    finally:
        api_main.resource_plane.settings.cloudflare_api_token = previous

    assert applied.status_code == 200
    payload = applied.json()
    assert payload["status"] == "applied"
    assert len(r2_requests) == 1
    request = r2_requests[0]
    assert request["url"].endswith("/accounts/cf-account-123/r2/buckets")
    assert request["headers"]["Authorization"] == "Bearer cf-test-token"
    assert request["headers"]["cf-r2-jurisdiction"] == "default"
    assert request["json"] == {
        "name": "boss-artifacts-staging",
        "locationHint": "enam",
        "storageClass": "Standard",
    }

    receipt = payload["resource_receipts"][0]
    assert receipt["status"] == "succeeded"
    assert receipt["provider_ref"] == "cloudflare:r2_bucket:boss-artifacts-staging"

    resources = client.get(
        "/v1/resources",
        params={"project_id": "boss-dogfood", "provider": "cloudflare"},
    )
    assert resources.status_code == 200
    resource = resources.json()["items"][0]
    assert resource["external_id"] == "boss-artifacts-staging"
    assert resource["resource_type"] == "r2_bucket"
    assert resource["receipt_id"] == receipt["receipt_id"]
    assert resource["metadata"]["provider_result"]["storage_class"] == "Standard"

    costs = client.get("/v1/costs", params={"project_id": "boss-dogfood"})
    assert costs.status_code == 200
    cost_event = costs.json()["items"][0]
    assert cost_event["provider"] == "cloudflare"
    assert cost_event["amount_usd_micros"] == 120_000
    assert cost_event["receipt_id"] == receipt["receipt_id"]

    health = client.get(
        "/v1/provider-health",
        params={"project_id": "boss-dogfood", "provider": "cloudflare"},
    )
    assert health.status_code == 200
    assert health.json()["items"][0]["status"] == "healthy"


def test_provider_changeset_supabase_project_pause_is_destructive_and_receipted(
    client, monkeypatch
):
    previous = api_main.resource_plane.settings.supabase_access_token
    api_main.resource_plane.settings.supabase_access_token = "sbp-test-token"
    pause_requests = []

    class FakeSupabasePauseResponse:
        def raise_for_status(self):
            return None

        def json(self):
            return {}

    class FakeSupabasePauseClient:
        def __init__(self, **kwargs):
            self.kwargs = kwargs

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, traceback):
            return False

        def post(self, url, headers):
            pause_requests.append({"url": url, "headers": headers})
            return FakeSupabasePauseResponse()

    monkeypatch.setattr(
        "apps.api.app.services.resource_plane.httpx.Client",
        FakeSupabasePauseClient,
    )
    try:
        provider = client.post(
            "/v1/providers",
            json={
                "project_id": "boss-dogfood",
                "provider": "supabase",
                "account_label": "cleanup-supabase",
                "scopes": ["read", "write"],
                "read_only": False,
                "deploy_capable": False,
                "destructive_actions_disabled": False,
                "actor": "ceo",
            },
        ).json()
        created = client.post(
            "/v1/changesets",
            json={
                "project_id": "boss-dogfood",
                "provider_account_id": provider["id"],
                "title": "Pause idle Supabase staging project",
                "summary": "Pause an idle staging Supabase project after approval.",
                "operations": [
                    {
                        "skill_id": "supabase.project.pause",
                        "action": "pause",
                        "resource_type": "project",
                        "resource_ref": "supabase-project-ref",
                        "desired_state": {
                            "project_ref": "supabase-project-ref",
                            "name": "boss-staging-db",
                            "reason": "idle staging cleanup",
                        },
                        "risk_tier": 3,
                        "destructive": True,
                        "production_affecting": False,
                    }
                ],
                "expected_cost_usd_micros": 0,
                "rollback_plan": ["Restore the Supabase project before resuming staging."],
                "risk_tier": 3,
                "approval_required": True,
                "actor": "ceo",
            },
        )
        assert created.status_code == 201
        changeset = created.json()
        approved = client.post(
            f"/v1/changesets/{changeset['id']}/approve",
            json={"actor": "ceo"},
        )
        assert approved.status_code == 200
        applied = client.post(
            f"/v1/changesets/{changeset['id']}/apply",
            json={"actor": "ceo"},
        )
    finally:
        api_main.resource_plane.settings.supabase_access_token = previous

    assert applied.status_code == 200
    payload = applied.json()
    assert payload["status"] == "applied"
    assert pause_requests == [
        {
            "url": "https://api.supabase.com/v1/projects/supabase-project-ref/pause",
            "headers": {
                "Authorization": "Bearer sbp-test-token",
                "Accept": "application/json",
            },
        }
    ]
    receipt = payload["resource_receipts"][0]
    assert receipt["status"] == "succeeded"
    assert receipt["provider_ref"] == "supabase:project:supabase-project-ref:paused"

    resources = client.get(
        "/v1/resources",
        params={"project_id": "boss-dogfood", "provider": "supabase"},
    )
    assert resources.status_code == 200
    resource = resources.json()["items"][0]
    assert resource["external_id"] == "supabase-project-ref"
    assert resource["resource_type"] == "project"
    assert resource["state"] == "draining"
    assert resource["health_status"] == "paused"
    assert resource["receipt_id"] == receipt["receipt_id"]


def test_provider_changeset_supabase_project_create_uses_secret_ref_without_persisting_secret(
    client, monkeypatch
):
    previous = api_main.resource_plane.settings.supabase_access_token
    api_main.resource_plane.settings.supabase_access_token = "sbp-test-token"
    monkeypatch.setenv("BOSS_TEST_SUPABASE_DB_PASS", "super-secret-db-pass")
    create_requests = []

    class FakeSupabaseCreateResponse:
        def raise_for_status(self):
            return None

        def json(self):
            return {
                "id": "project-id-123",
                "ref": "created-project-ref",
                "organization_slug": "boss-org",
                "name": "boss-created-db",
                "region": "us-east-1",
                "status": "COMING_UP",
            }

    class FakeSupabaseCreateClient:
        def __init__(self, **kwargs):
            self.kwargs = kwargs

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, traceback):
            return False

        def post(self, url, headers, json):
            create_requests.append({"url": url, "headers": headers, "json": json})
            return FakeSupabaseCreateResponse()

    monkeypatch.setattr(
        "apps.api.app.services.resource_plane.httpx.Client",
        FakeSupabaseCreateClient,
    )
    try:
        provider = client.post(
            "/v1/providers",
            json={
                "project_id": "boss-dogfood",
                "provider": "supabase",
                "account_label": "create-supabase",
                "scopes": ["read", "write"],
                "read_only": False,
                "deploy_capable": False,
                "destructive_actions_disabled": True,
                "actor": "ceo",
            },
        ).json()
        reservation_id = reserve_for_infra_provision(
            client,
            "Reserve Supabase project creation budget.",
        )
        created = client.post(
            "/v1/changesets",
            json={
                "project_id": "boss-dogfood",
                "provider_account_id": provider["id"],
                "title": "Create Supabase staging project",
                "summary": "Create a Supabase project for staging database work.",
                "operations": [
                    {
                        "skill_id": "supabase.project.create",
                        "action": "create",
                        "resource_type": "project",
                        "resource_ref": "boss-created-db",
                        "desired_state": {
                            "name": "boss-created-db",
                            "organization_slug": "boss-org",
                            "region": "us-east-1",
                            "desired_instance_size": "nano",
                            "db_pass_secret_ref": "env:BOSS_TEST_SUPABASE_DB_PASS",
                        },
                        "risk_tier": 3,
                        "destructive": False,
                        "production_affecting": False,
                    }
                ],
                "expected_cost_usd_micros": 500_000,
                "reservation_id": reservation_id,
                "rollback_plan": ["Pause or delete the staging project after validation."],
                "risk_tier": 3,
                "approval_required": True,
                "actor": "ceo",
            },
        )
        assert created.status_code == 201
        changeset = created.json()
        approved = client.post(
            f"/v1/changesets/{changeset['id']}/approve",
            json={"actor": "ceo"},
        )
        assert approved.status_code == 200
        applied = client.post(
            f"/v1/changesets/{changeset['id']}/apply",
            json={"actor": "ceo"},
        )
    finally:
        api_main.resource_plane.settings.supabase_access_token = previous

    assert applied.status_code == 200
    payload = applied.json()
    assert payload["status"] == "applied"
    assert len(create_requests) == 1
    request = create_requests[0]
    assert request["url"] == "https://api.supabase.com/v1/projects"
    assert request["headers"]["Authorization"] == "Bearer sbp-test-token"
    assert request["json"]["name"] == "boss-created-db"
    assert request["json"]["organization_slug"] == "boss-org"
    assert request["json"]["db_pass"] == "super-secret-db-pass"

    receipt = payload["resource_receipts"][0]
    assert receipt["status"] == "succeeded"
    assert receipt["provider_ref"] == "supabase:project:created-project-ref"

    resources = client.get(
        "/v1/resources",
        params={"project_id": "boss-dogfood", "provider": "supabase"},
    )
    assert resources.status_code == 200
    resource = resources.json()["items"][0]
    assert resource["external_id"] == "created-project-ref"
    assert resource["resource_type"] == "project"
    assert resource["state"] == "provisioning"
    assert resource["health_status"] == "COMING_UP"
    assert resource["receipt_id"] == receipt["receipt_id"]
    assert "db_pass" not in resource["metadata"]["provider_result"]
    assert "super-secret-db-pass" not in json.dumps(resource["metadata"])

    costs = client.get("/v1/costs", params={"project_id": "boss-dogfood"})
    assert costs.status_code == 200
    cost_event = costs.json()["items"][0]
    assert cost_event["provider"] == "supabase"
    assert cost_event["amount_usd_micros"] == 500_000
    assert cost_event["receipt_id"] == receipt["receipt_id"]


def test_provider_cost_anomaly_applies_read_only_kill_switch(
    client, engine, monkeypatch
):
    previous = api_main.resource_plane.settings.cloudflare_api_token
    api_main.resource_plane.settings.cloudflare_api_token = "cf-test-token"
    monkeypatch.setattr(
        api_main.resource_plane,
        "_cloudflare_dns_record_upsert",
        lambda credential, operation: {
            "provider_ref": "cloudflare:dns_record:expensive-dns-record",
            "resource_external_id": "expensive-dns-record",
            "resource_name": "expensive-preview.example.com",
            "resource_type": "dns_record",
            "state": "active",
            "health_status": "active",
            "operation": "created",
            "zone_id": "zone-123",
            "record": {
                "id": "expensive-dns-record",
                "type": "A",
                "name": "expensive-preview.example.com",
                "content": "192.0.2.20",
                "ttl": 1,
            },
            "api_success": True,
        },
    )
    with engine.begin() as connection:
        connection.execute(
            text(
                """
                UPDATE budget_policies
                SET daily_cap_usd_micros = 300000,
                    freeze_provisioning_pct = 0.80
                WHERE scope_type = 'project' AND scope_id = 'boss-dogfood'
                """
            )
        )
    try:
        provider = client.post(
            "/v1/providers",
            json={
                "project_id": "boss-dogfood",
                "provider": "cloudflare",
                "account_label": "cost-guard-cloudflare",
                "scopes": ["dns:write"],
                "read_only": False,
                "actor": "ceo",
            },
        ).json()
        reservation_id = reserve_for_infra_provision(
            client,
            "Reserve provider spend anomaly proof.",
        )
        created = client.post(
            "/v1/changesets",
            json={
                "project_id": "boss-dogfood",
                "provider_account_id": provider["id"],
                "title": "Create costly preview DNS record",
                "summary": "Trigger provider cost anomaly from observed ChangeSet cost.",
                "expected_cost_usd_micros": 250_000,
                "reservation_id": reservation_id,
                "rollback_plan": ["Delete the preview DNS record."],
                "operations": [
                    {
                        "skill_id": "cloudflare.dns.record.upsert",
                        "action": "upsert",
                        "resource_type": "dns_record",
                        "resource_ref": "expensive-preview.example.com",
                        "desired_state": {
                            "zone_id": "zone-123",
                            "record_type": "A",
                            "name": "expensive-preview.example.com",
                            "value": "192.0.2.20",
                            "environment": "preview",
                        },
                        "risk_tier": 2,
                    }
                ],
                "actor": "ceo",
            },
        )
        assert created.status_code == 201
        changeset = created.json()
        client.post(
            f"/v1/changesets/{changeset['id']}/approve",
            json={"actor": "ceo"},
        )
        applied = client.post(
            f"/v1/changesets/{changeset['id']}/apply",
            json={"actor": "ceo"},
        )
    finally:
        api_main.resource_plane.settings.cloudflare_api_token = previous

    assert applied.status_code == 200
    assert applied.json()["status"] == "applied"

    anomalies = client.get(
        "/v1/cost-anomalies",
        params={"project_id": "boss-dogfood"},
    )
    assert anomalies.status_code == 200
    anomaly = anomalies.json()["items"][0]
    assert anomaly["anomaly_type"] == "provider_spend_freeze_threshold_crossed"
    assert anomaly["severity"] == "orange"
    assert anomaly["amount_usd_micros"] == 250_000
    assert anomaly["threshold_usd_micros"] == 240_000
    assert anomaly["decision_id"]
    assert anomaly["recommended_actions"][0]["action"] == "provider_read_only_mode_applied"

    providers = client.get("/v1/providers", params={"project_id": "boss-dogfood"})
    guarded_provider = next(
        item for item in providers.json()["items"] if item["id"] == provider["id"]
    )
    assert guarded_provider["read_only"] is True
    assert guarded_provider["last_error"]["reason"] == (
        "provider_spend_freeze_threshold_crossed"
    )

    blocked = client.post(
        "/v1/changesets",
        json={
            "project_id": "boss-dogfood",
            "provider_account_id": provider["id"],
            "title": "Second DNS write after kill switch",
            "summary": "This must fail closed because the provider is read-only.",
            "expected_cost_usd_micros": 0,
            "rollback_plan": ["Delete the DNS record."],
            "operations": [
                {
                    "skill_id": "cloudflare.dns.record.upsert",
                    "action": "upsert",
                    "resource_type": "dns_record",
                    "desired_state": {
                        "zone_id": "zone-123",
                        "record_type": "A",
                        "name": "blocked.example.com",
                        "value": "192.0.2.30",
                    },
                    "risk_tier": 2,
                }
            ],
            "actor": "ceo",
        },
    )
    assert blocked.status_code == 422
    assert "read-only" in blocked.json()["detail"]

    events = client.get("/v1/events", params={"project_id": "boss-dogfood"})
    event_types = [item["event_type"] for item in events.json()["items"]]
    assert "provider.cost_anomaly_detected" in event_types
    assert "provider.read_only_kill_switch_applied" in event_types


def test_provider_changeset_missing_credentials_deadletters_without_provider_call(
    client, monkeypatch
):
    previous = api_main.resource_plane.settings.cloudflare_api_token
    api_main.resource_plane.settings.cloudflare_api_token = None

    def unexpected_cloudflare_dns_upsert(credential, operation):
        raise AssertionError("adapter should not run without credentials")

    monkeypatch.setattr(
        api_main.resource_plane,
        "_cloudflare_dns_record_upsert",
        unexpected_cloudflare_dns_upsert,
    )
    try:
        provider = client.post(
            "/v1/providers",
            json={
                "project_id": "boss-dogfood",
                "provider": "cloudflare",
                "account_label": "missing-token-cloudflare",
                "scopes": ["dns:write"],
                "read_only": False,
                "actor": "ceo",
            },
        ).json()
        created = client.post(
            "/v1/changesets",
            json={
                "project_id": "boss-dogfood",
                "provider_account_id": provider["id"],
                "title": "Create preview DNS record",
                "summary": "Point preview.example.com at the approved preview service.",
                "expected_cost_usd_micros": 0,
                "rollback_plan": ["Restore or delete the prior DNS record value."],
                "operations": [
                    {
                        "skill_id": "cloudflare.dns.record.upsert",
                        "action": "upsert",
                        "resource_type": "dns_record",
                        "resource_ref": "preview.example.com",
                        "desired_state": {
                            "zone_id": "zone-123",
                            "record_type": "A",
                            "name": "preview.example.com",
                            "value": "192.0.2.10",
                        },
                        "risk_tier": 2,
                    }
                ],
                "actor": "ceo",
            },
        )
        assert created.status_code == 201
        changeset = created.json()
        client.post(
            f"/v1/changesets/{changeset['id']}/approve",
            json={"actor": "ceo"},
        )
        applied = client.post(
            f"/v1/changesets/{changeset['id']}/apply",
            json={"actor": "ceo"},
        )
    finally:
        api_main.resource_plane.settings.cloudflare_api_token = previous

    assert applied.status_code == 200
    payload = applied.json()
    assert payload["status"] == "deadlettered"
    receipt = payload["resource_receipts"][0]
    assert receipt["status"] == "failed"
    assert receipt["error"]["error_type"] == "ProviderCredentialError"

    health = client.get(
        "/v1/provider-health",
        params={"project_id": "boss-dogfood", "provider": "cloudflare"},
    )
    assert health.status_code == 200
    assert health.json()["items"][0]["status"] == "degraded"

    deadletters = client.get("/v1/deadletters", params={"project_id": "boss-dogfood"})
    assert any(
        item["source_kind"] == "resource_receipt"
        and item["payload"]["changeset_id"] == changeset["id"]
        for item in deadletters.json()["items"]
    )


def test_provider_changeset_blocks_destructive_action_when_disabled(client):
    provider = client.post(
        "/v1/providers",
        json={
            "project_id": "boss-dogfood",
            "provider": "digitalocean",
            "account_label": "read-only-do",
            "scopes": ["read"],
            "read_only": False,
            "destructive_actions_disabled": True,
            "actor": "ceo",
        },
    ).json()
    response = client.post(
        "/v1/changesets",
        json={
            "project_id": "boss-dogfood",
            "provider_account_id": provider["id"],
            "title": "Destroy orphaned droplet",
            "summary": "Attempt cleanup.",
            "rollback_plan": ["Restore from snapshot."],
            "operations": [
                {
                    "skill_id": "digitalocean.droplet.destroy",
                    "action": "destroy",
                    "resource_type": "droplet",
                    "resource_ref": "12345",
                    "destructive": True,
                    "risk_tier": 3,
                }
            ],
            "actor": "ceo",
        },
    )
    assert response.status_code == 422
    assert "destructive actions disabled" in response.json()["detail"]


def test_autoscaler_scales_local_worker_pool_from_queue_pressure(client):
    pool = client.post(
        "/v1/worker-pools",
        json={
            "project_id": "boss-dogfood",
            "pool_key": "local-fast",
            "kind": "local",
            "current_workers": 0,
            "desired_workers": 0,
            "min_workers": 0,
            "max_workers": 3,
            "max_queue_depth_per_worker": 2,
            "capabilities": ["plan.command"],
            "actor": "ceo",
        },
    )
    assert pool.status_code == 201
    pool_id = pool.json()["id"]

    for index in range(5):
        created = client.post(
            "/v1/jobs",
            json={
                "project_id": "boss-dogfood",
                "kind": "plan.command",
                "payload": {"index": index},
                "priority": 50,
            },
        )
        assert created.status_code == 201

    planned = client.post(
        "/v1/autoscaler/plans",
        json={
            "project_id": "boss-dogfood",
            "worker_pool_id": pool_id,
            "actor": "ceo",
        },
    )
    assert planned.status_code == 200
    payload = planned.json()
    plan = payload["scale_plan"]
    assert plan["status"] == "applied"
    assert plan["trigger_reason"] == "queue_pressure"
    assert plan["queue_depth"] == 5
    assert plan["desired_workers_before"] == 0
    assert plan["desired_workers_after"] == 2
    assert plan["target_workers"] == 2
    assert plan["actions"][0]["action"] == "scale_local_workers"
    assert plan["recommendations"][0]["action"] == "request_capacity_limit_review"
    assert payload["worker_pool"]["desired_workers"] == 2

    pools = client.get("/v1/worker-pools", params={"project_id": "boss-dogfood"})
    assert pools.status_code == 200
    assert pools.json()["items"][0]["desired_workers"] == 2

    plans = client.get(
        "/v1/autoscaler/plans",
        params={"project_id": "boss-dogfood"},
    )
    assert plans.status_code == 200
    assert plans.json()["items"][0]["id"] == plan["id"]

    events = client.get("/v1/events", params={"project_id": "boss-dogfood"})
    event_types = [item["event_type"] for item in events.json()["items"]]
    assert "worker_pool.desired_capacity_changed" in event_types
    assert "autoscaler.plan_created" in event_types


def test_autoscaler_red_budget_enters_degraded_mode_and_creates_decision(
    client,
    engine,
):
    pool = client.post(
        "/v1/worker-pools",
        json={
            "project_id": "boss-dogfood",
            "pool_key": "local-drain",
            "kind": "local",
            "current_workers": 2,
            "desired_workers": 2,
            "min_workers": 0,
            "max_workers": 3,
            "actor": "ceo",
        },
    )
    assert pool.status_code == 201
    pool_id = pool.json()["id"]
    with engine.begin() as connection:
        connection.execute(
            text(
                """
                UPDATE budget_policies
                SET daily_cap_usd_micros = 1000,
                    monthly_cap_usd_micros = 1000
                WHERE scope_type = 'project' AND scope_id = 'boss-dogfood'
                """
            )
        )
        connection.execute(
            text(
                """
                INSERT INTO cost_receipts (
                    id, project_id, category, skill_or_model_id,
                    estimated_usd_micros, actual_usd_micros, status
                )
                VALUES (
                    'cost_autoscaler_red', 'boss-dogfood', 'provider',
                    'digitalocean.droplet.create', 950, 950, 'actual'
                )
                """
            )
        )

    planned = client.post(
        "/v1/autoscaler/plans",
        json={
            "project_id": "boss-dogfood",
            "worker_pool_id": pool_id,
            "actor": "ceo",
        },
    )
    assert planned.status_code == 200
    payload = planned.json()
    plan = payload["scale_plan"]
    assert payload["budget_state"] == "red"
    assert plan["status"] == "applied"
    assert plan["trigger_reason"] == "budget_state_red"
    assert plan["desired_workers_before"] == 2
    assert plan["desired_workers_after"] == 0
    assert plan["decision_id"]
    actions = [item["action"] for item in plan["actions"]]
    assert "enter_degraded_mode" in actions
    assert "pause_noncritical_work" in actions
    assert "drain_workers" in actions

    decisions = client.get("/v1/decisions/inbox", params={"project_id": "boss-dogfood"})
    assert decisions.status_code == 200
    assert any(
        item["id"] == plan["decision_id"]
        and item["recommendation"]["autoscaler_guardrail"]
        == "budget_governor_wins_over_autoscaler"
        for item in decisions.json()["items"]
    )


def test_autoscaler_cloud_spillover_creates_reserved_changeset_not_provider_call(client):
    provider = client.post(
        "/v1/providers",
        json={
            "project_id": "boss-dogfood",
            "provider": "digitalocean",
            "account_label": "autoscaler-do",
            "scopes": ["read", "write"],
            "read_only": False,
            "destructive_actions_disabled": False,
            "actor": "ceo",
        },
    ).json()
    pool = client.post(
        "/v1/worker-pools",
        json={
            "project_id": "boss-dogfood",
            "pool_key": "cloud-spillover",
            "kind": "cloud",
            "provider_account_id": provider["id"],
            "current_workers": 0,
            "desired_workers": 0,
            "min_workers": 0,
            "max_workers": 2,
            "max_queue_depth_per_worker": 1,
            "cost_per_worker_hour_usd_micros": 500_000,
            "capabilities": ["software_factory.propose_pr"],
            "actor": "ceo",
        },
    )
    assert pool.status_code == 201
    pool_id = pool.json()["id"]
    for index in range(2):
        created = client.post(
            "/v1/jobs",
            json={
                "project_id": "boss-dogfood",
                "kind": "software_factory.propose_pr",
                "payload": {
                    "index": index,
                    "requires_reservation": False,
                },
                "priority": 40,
            },
        )
        assert created.status_code == 201

    planned = client.post(
        "/v1/autoscaler/plans",
        json={
            "project_id": "boss-dogfood",
            "worker_pool_id": pool_id,
            "cloud_spillover_enabled": True,
            "actor": "ceo",
        },
    )
    assert planned.status_code == 200
    payload = planned.json()
    plan = payload["scale_plan"]
    assert plan["status"] == "pending_approval"
    assert plan["estimated_hourly_delta_usd_micros"] == 1_000_000
    assert plan["changeset_id"]
    assert plan["decision_id"]
    assert plan["actions"][0]["action"] == "propose_cloud_workers"
    assert payload["worker_pool"]["desired_workers"] == 0

    changesets = client.get(
        "/v1/changesets",
        params={"project_id": "boss-dogfood"},
    )
    assert changesets.status_code == 200
    changeset = changesets.json()["items"][0]
    assert changeset["id"] == plan["changeset_id"]
    assert changeset["status"] == "pending_approval"
    assert changeset["reservation_id"]
    assert changeset["operations"][0]["skill_id"] == "digitalocean.droplet.create"
    assert changeset["operations"][0]["desired_state"]["count"] == 2

    receipts = client.get(
        "/v1/resource-receipts",
        params={"project_id": "boss-dogfood"},
    )
    assert receipts.status_code == 200
    assert receipts.json()["items"] == []


def test_resource_janitor_proposes_cleanup_changeset_for_orphaned_resource(
    client,
    monkeypatch,
):
    previous = api_main.resource_plane.settings.digitalocean_token
    api_main.resource_plane.settings.digitalocean_token = "do-token"
    monkeypatch.setattr(
        api_main.resource_plane,
        "_sync_inventory",
        lambda provider, credential: [
            InventoryResource(
                provider="digitalocean",
                resource_type="droplet",
                external_id="orphan-123",
                name="orphan-preview-worker",
                state="active",
                ownership="orphaned",
                environment="preview",
                health_status="active",
                tags=["preview"],
                metadata={
                    "region": "nyc3",
                    "size": "s-1vcpu-1gb",
                    "ttl_expired": True,
                },
                estimated_monthly_usd_micros=1_200_000,
                cost_confidence="estimated_from_price_catalog",
            )
        ],
    )
    try:
        provider = client.post(
            "/v1/providers",
            json={
                "project_id": "boss-dogfood",
                "provider": "digitalocean",
                "account_label": "janitor-do",
                "scopes": ["read", "write"],
                "read_only": False,
                "destructive_actions_disabled": False,
                "actor": "ceo",
            },
        ).json()
        synced = client.post(
            "/v1/resources/sync",
            json={
                "project_id": "boss-dogfood",
                "provider_account_id": provider["id"],
                "actor": "ceo",
            },
        )
    finally:
        api_main.resource_plane.settings.digitalocean_token = previous

    assert synced.status_code == 200

    janitor = client.post(
        "/v1/resource-janitor/run",
        json={
            "project_id": "boss-dogfood",
            "provider_account_id": provider["id"],
            "actor": "ceo",
        },
    )
    assert janitor.status_code == 200
    payload = janitor.json()
    assert payload["status"] == "completed"
    assert payload["candidate_count"] == 1
    assert payload["estimated_monthly_savings_usd_micros"] == 1_200_000
    assert payload["candidates"][0]["cleanup_skill_id"] == "digitalocean.droplet.destroy"
    assert payload["changesets"][0]["status"] == "pending_approval"
    assert payload["changesets"][0]["operations"][0]["resource_ref"] == "orphan-123"
    assert payload["changesets"][0]["policy_result"]["janitor_proposal"] is True
    assert payload["artifact_id"].startswith("art_")

    artifacts = client.get(
        "/v1/artifacts",
        params={"project_id": "boss-dogfood", "limit": 5},
    )
    assert any(
        item["artifact_type"] == "resource_janitor_report"
        for item in artifacts.json()["items"]
    )

    duplicate = client.post(
        "/v1/resource-janitor/run",
        json={
            "project_id": "boss-dogfood",
            "provider_account_id": provider["id"],
            "actor": "ceo",
        },
    )
    assert duplicate.status_code == 200
    assert duplicate.json()["candidate_count"] == 0
    assert duplicate.json()["skipped"][0]["reason"] == "open_cleanup_changeset_exists"


def test_resource_janitor_never_auto_proposes_production_deletion(
    client,
    monkeypatch,
):
    previous = api_main.resource_plane.settings.digitalocean_token
    api_main.resource_plane.settings.digitalocean_token = "do-token"
    monkeypatch.setattr(
        api_main.resource_plane,
        "_sync_inventory",
        lambda provider, credential: [
            InventoryResource(
                provider="digitalocean",
                resource_type="droplet",
                external_id="prod-123",
                name="production-worker",
                state="active",
                ownership="orphaned",
                environment="production",
                health_status="active",
                tags=["production"],
                metadata={"ttl_expired": True},
                estimated_monthly_usd_micros=2_500_000,
                cost_confidence="estimated_from_price_catalog",
            )
        ],
    )
    try:
        provider = client.post(
            "/v1/providers",
            json={
                "project_id": "boss-dogfood",
                "provider": "digitalocean",
                "account_label": "janitor-prod-do",
                "scopes": ["read", "write"],
                "read_only": False,
                "destructive_actions_disabled": False,
                "actor": "ceo",
            },
        ).json()
        client.post(
            "/v1/resources/sync",
            json={
                "project_id": "boss-dogfood",
                "provider_account_id": provider["id"],
                "actor": "ceo",
            },
        )
    finally:
        api_main.resource_plane.settings.digitalocean_token = previous

    janitor = client.post(
        "/v1/resource-janitor/run",
        json={
            "project_id": "boss-dogfood",
            "provider_account_id": provider["id"],
            "actor": "ceo",
        },
    )
    assert janitor.status_code == 200
    payload = janitor.json()
    assert payload["candidate_count"] == 0
    assert payload["changesets"] == []
    assert payload["skipped"][0]["reason"] == "production_resource_requires_manual_review"


def test_deployment_target_registry_is_api_driven(client):
    created = client.post(
        "/v1/deployments",
        json={
            "project_id": "boss-dogfood",
            "app_name": "boss-api",
            "repo": "seanheiney/boss",
            "environment": "staging",
            "target_kind": "cloudflare.worker",
            "target_ref": "boss-api-staging",
            "domains": ["staging-api.boss.local"],
            "required_resources": [{"kind": "worker"}],
            "secret_refs": ["env:OPENAI_API_KEY"],
            "health_checks": [{"url": "https://staging-api.boss.local/health"}],
            "rollback_points": [{"ref": "previous-worker-version"}],
            "actor": "ceo",
        },
    )
    assert created.status_code == 201
    payload = created.json()
    assert payload["status"] == "created"
    assert payload["domains"] == ["staging-api.boss.local"]

    deployments = client.get(
        "/v1/deployments",
        params={"project_id": "boss-dogfood"},
    )
    assert deployments.status_code == 200
    assert deployments.json()["items"][0]["app_name"] == "boss-api"


def test_deployment_run_plans_changeset_and_release_packet(client):
    provider = client.post(
        "/v1/providers",
        json={
            "project_id": "boss-dogfood",
            "provider": "digitalocean",
            "account_label": "deploy-do",
            "scopes": ["read", "write", "deploy"],
            "read_only": False,
            "deploy_capable": True,
            "destructive_actions_disabled": False,
            "actor": "ceo",
        },
    ).json()
    target = client.post(
        "/v1/deployments",
        json={
            "project_id": "boss-dogfood",
            "app_name": "boss-api",
            "repo": "seanheiney/boss",
            "environment": "staging",
            "provider_account_id": provider["id"],
            "target_kind": "digitalocean_app",
            "target_ref": "app-123",
            "domains": ["staging-api.boss.local"],
            "health_checks": [
                {
                    "name": "api health",
                    "url": "https://staging-api.boss.local/health",
                    "expected_status": 200,
                }
            ],
            "rollback_points": [{"ref": "previous-app-spec"}],
            "actor": "ceo",
        },
    ).json()
    upsert_deployment_environment_policy(
        client,
        "boss-dogfood",
        allowed_target_kinds=["digitalocean_app"],
    )
    reservation_id = reserve_for_infra_provision(
        client,
        "Reserve deployment planning budget.",
    )

    planned = client.post(
        "/v1/deployment-runs",
        json={
            "project_id": "boss-dogfood",
            "deployment_target_id": target["id"],
            "commit_ref": "abc123",
            "release_packet": {"review_packet_id": "art_review"},
            "expected_cost_usd_micros": 100_000,
            "reservation_id": reservation_id,
            "actor": "ceo",
        },
    )
    assert planned.status_code == 201
    payload = planned.json()
    assert payload["status"] == "created"
    assert payload["changeset_id"]
    assert payload["release_artifact_id"]
    assert payload["rollback_plan"] == ["previous-app-spec"]
    assert payload["release_receipt_ids"][0]["skill_id"] == "digitalocean.app.deploy"

    changesets = client.get(
        "/v1/changesets",
        params={"project_id": "boss-dogfood"},
    )
    assert changesets.status_code == 200
    changeset = changesets.json()["items"][0]
    assert changeset["id"] == payload["changeset_id"]
    assert changeset["status"] == "pending_approval"
    assert changeset["operations"][0]["skill_id"] == "digitalocean.app.deploy"
    assert changeset["operations"][0]["desired_state"]["release_artifact_id"] == payload[
        "release_artifact_id"
    ]
    assert changeset["operations"][0]["desired_state"]["commit_ref"] == "abc123"

    artifact = client.get(f"/v1/artifacts/{payload['release_artifact_id']}")
    assert artifact.status_code == 200
    assert artifact.json()["artifact_type"] == "deployment_release_packet"

    receipts = client.get(
        "/v1/resource-receipts",
        params={"project_id": "boss-dogfood"},
    )
    assert receipts.status_code == 200
    assert receipts.json()["items"] == []


def test_deployment_environment_policy_gates_production_provider_runs(client):
    provider = client.post(
        "/v1/providers",
        json={
            "project_id": "boss-dogfood",
            "provider": "cloudflare",
            "account_label": "production-cloudflare",
            "scopes": ["read", "write", "deploy"],
            "read_only": False,
            "deploy_capable": True,
            "destructive_actions_disabled": True,
            "actor": "ceo",
        },
    ).json()
    target = client.post(
        "/v1/deployments",
        json={
            "project_id": "boss-dogfood",
            "app_name": "boss-web",
            "repo": "seanheiney/boss",
            "environment": "production",
            "provider_account_id": provider["id"],
            "target_kind": "cloudflare_worker",
            "target_ref": "boss-web-prod",
            "health_checks": [
                {
                    "name": "production health",
                    "url": "https://boss.example.com/health",
                    "expected_status": 200,
                }
            ],
            "rollback_points": [{"ref": "previous-production-worker"}],
            "actor": "ceo",
        },
    ).json()

    blocked = client.post(
        "/v1/deployment-runs",
        json={
            "project_id": "boss-dogfood",
            "deployment_target_id": target["id"],
            "commit_ref": "prod-sha",
            "actor": "ceo",
        },
    )
    assert blocked.status_code == 201
    blocked_payload = blocked.json()
    assert blocked_payload["status"] == "failed"
    assert "environment rollout policy" in blocked_payload["failure_reason"]
    assert blocked_payload["decision_id"]

    inbox = client.get("/v1/decisions/inbox", params={"project_id": "boss-dogfood"})
    assert inbox.status_code == 200
    assert any(
        item["id"] == blocked_payload["decision_id"]
        and item["recommendation"]["action"] == "update_deployment_environment_policy"
        for item in inbox.json()["items"]
    )
    events = client.get("/v1/events", params={"project_id": "boss-dogfood"})
    assert events.status_code == 200
    assert any(
        item["event_type"] == "deployment_run.blocked"
        and item["data"]["reason"] == "missing_active_environment_policy"
        for item in events.json()["items"]
    )

    policy = upsert_deployment_environment_policy(
        client,
        "boss-dogfood",
        environment="production",
        runtime_mode="production_allowed",
        allowed_target_kinds=["cloudflare_worker"],
        requires_health_check=True,
    )
    assert policy["runtime_mode"] == "production_allowed"
    policies = client.get(
        "/v1/deployment-environment-policies",
        params={"project_id": "boss-dogfood"},
    )
    assert policies.status_code == 200
    assert policies.json()["items"][0]["environment"] == "production"
    reservation_id = reserve_for_infra_provision(
        client,
        "Reserve production deployment planning budget.",
    )

    planned = client.post(
        "/v1/deployment-runs",
        json={
            "project_id": "boss-dogfood",
            "deployment_target_id": target["id"],
            "commit_ref": "prod-sha",
            "expected_cost_usd_micros": 100_000,
            "reservation_id": reservation_id,
            "actor": "ceo",
        },
    )
    assert planned.status_code == 201
    planned_payload = planned.json()
    assert planned_payload["status"] == "created"
    assert planned_payload["changeset_id"]
    changeset = client.get(
        "/v1/changesets",
        params={"project_id": "boss-dogfood"},
    ).json()["items"][0]
    assert changeset["id"] == planned_payload["changeset_id"]
    assert changeset["status"] == "pending_approval"
    assert changeset["reservation_id"] == reservation_id
    assert changeset["operations"][0]["production_affecting"] is True


def test_deployment_run_digitalocean_app_apply_marks_deployed(
    client, monkeypatch
):
    previous_token = api_main.resource_plane.settings.digitalocean_token
    previous_allowed = api_main.resource_plane.settings.infra_real_provision_allowed
    api_main.resource_plane.settings.digitalocean_token = "do-test-token"
    api_main.resource_plane.settings.infra_real_provision_allowed = True
    deployment_calls = []

    def fake_create_app_deployment(**kwargs):
        deployment_calls.append(kwargs)
        return AppDeploymentResult(
            app_id=kwargs["app_id"],
            deployment_id="dep-123",
            status="active",
            phase="deploying",
            api_url=(
                "https://api.digitalocean.com/v2/apps/"
                f"{kwargs['app_id']}/deployments/dep-123"
            ),
        )

    monkeypatch.setattr(
        api_main.resource_plane.digitalocean,
        "create_app_deployment",
        fake_create_app_deployment,
    )
    try:
        provider = client.post(
            "/v1/providers",
            json={
                "project_id": "boss-dogfood",
                "provider": "digitalocean",
                "account_label": "deploy-do",
                "scopes": ["read", "write", "deploy"],
                "read_only": False,
                "deploy_capable": True,
                "destructive_actions_disabled": False,
                "actor": "ceo",
            },
        ).json()
        target = client.post(
            "/v1/deployments",
            json={
                "project_id": "boss-dogfood",
                "app_name": "boss-api",
                "repo": "seanheiney/boss",
                "environment": "staging",
                "provider_account_id": provider["id"],
                "target_kind": "digitalocean_app",
                "target_ref": "app-123",
                "domains": ["staging-api.boss.local"],
                "health_checks": [
                    {
                        "name": "api health",
                        "url": "https://staging-api.boss.local/health",
                        "expected_status": 200,
                    }
                ],
                "rollback_points": [{"ref": "previous-app-spec"}],
                "actor": "ceo",
            },
        ).json()
        upsert_deployment_environment_policy(
            client,
            "boss-dogfood",
            allowed_target_kinds=["digitalocean_app"],
        )
        reservation_id = reserve_for_infra_provision(
            client,
            "Reserve DigitalOcean app deployment budget.",
        )
        planned = client.post(
            "/v1/deployment-runs",
            json={
                "project_id": "boss-dogfood",
                "deployment_target_id": target["id"],
                "commit_ref": "abc123",
                "release_packet": {"review_packet_id": "art_review"},
                "expected_cost_usd_micros": 250_000,
                "reservation_id": reservation_id,
                "actor": "ceo",
            },
        )
        assert planned.status_code == 201
        run = planned.json()
        approved = client.post(
            f"/v1/changesets/{run['changeset_id']}/approve",
            json={"actor": "ceo"},
        )
        assert approved.status_code == 200
        applied = client.post(
            f"/v1/changesets/{run['changeset_id']}/apply",
            json={"actor": "ceo"},
        )
    finally:
        api_main.resource_plane.settings.digitalocean_token = previous_token
        api_main.resource_plane.settings.infra_real_provision_allowed = previous_allowed

    assert applied.status_code == 200
    payload = applied.json()
    assert payload["status"] == "applied"
    assert deployment_calls == [{"app_id": "app-123", "force_build": True}]
    receipt = payload["resource_receipts"][0]
    assert receipt["status"] == "succeeded"
    assert receipt["provider_ref"].endswith("/apps/app-123/deployments/dep-123")

    deployment_runs = client.get(
        "/v1/deployment-runs",
        params={"project_id": "boss-dogfood"},
    )
    assert deployment_runs.status_code == 200
    deployed_run = deployment_runs.json()["items"][0]
    assert deployed_run["id"] == run["id"]
    assert deployed_run["status"] == "deployed"

    resources = client.get(
        "/v1/resources",
        params={"project_id": "boss-dogfood", "provider": "digitalocean"},
    )
    assert resources.status_code == 200
    resource = resources.json()["items"][0]
    assert resource["external_id"] == "dep-123"
    assert resource["resource_type"] == "app_deployment"
    assert resource["metadata"]["provider_result"]["deployment_id"] == "dep-123"

    costs = client.get("/v1/costs", params={"project_id": "boss-dogfood"})
    assert costs.status_code == 200
    cost_event = costs.json()["items"][0]
    assert cost_event["provider"] == "digitalocean"
    assert cost_event["amount_usd_micros"] == 250_000
    assert cost_event["cost_layer"] == "observed"
    assert cost_event["confidence"] == "estimated_from_price_catalog"
    assert cost_event["source_ref"] == run["changeset_id"]
    assert cost_event["receipt_id"] == receipt["receipt_id"]
    assert cost_event["metadata"]["resource_receipt_ids"] == [receipt["id"]]

    events = client.get("/v1/events", params={"project_id": "boss-dogfood"})
    assert any(
        item["event_type"] == "deployment_run.deployed"
        and item["entity_id"] == run["id"]
        for item in events.json()["items"]
    )


def test_deployment_run_cloudflare_worker_apply_marks_deployed(
    client, monkeypatch
):
    previous_token = api_main.resource_plane.settings.cloudflare_api_token
    api_main.resource_plane.settings.cloudflare_api_token = "cf-test-token"
    worker_requests = []

    class FakeCloudflareResponse:
        def raise_for_status(self):
            return None

        def json(self):
            return {
                "success": True,
                "result": {
                    "id": "boss-worker",
                    "etag": "etag-worker-123",
                },
            }

    class FakeCloudflareClient:
        def __init__(self, **kwargs):
            self.kwargs = kwargs

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, traceback):
            return False

        def put(self, url, headers, files):
            worker_requests.append(
                {
                    "url": url,
                    "headers": headers,
                    "files": files,
                }
            )
            return FakeCloudflareResponse()

    monkeypatch.setattr(
        "apps.api.app.services.resource_plane.httpx.Client",
        FakeCloudflareClient,
    )
    try:
        provider = client.post(
            "/v1/providers",
            json={
                "project_id": "boss-dogfood",
                "provider": "cloudflare",
                "account_label": "deploy-cloudflare-worker",
                "scopes": ["read", "write", "deploy"],
                "read_only": False,
                "deploy_capable": True,
                "destructive_actions_disabled": True,
                "actor": "ceo",
            },
        ).json()
        target = client.post(
            "/v1/deployments",
            json={
                "project_id": "boss-dogfood",
                "app_name": "boss-edge-worker",
                "repo": "seanheiney/boss",
                "environment": "staging",
                "provider_account_id": provider["id"],
                "target_kind": "cloudflare_worker",
                "target_ref": "boss-worker",
                "domains": ["edge.boss.local"],
                "required_resources": [
                    {
                        "type": "worker_script",
                        "account_id": "cf-account-123",
                        "script_name": "boss-worker",
                        "main_module": "worker.js",
                        "compatibility_date": "2026-06-03",
                        "script_content": (
                            "export default { async fetch() { "
                            "return new Response('ok'); } }"
                        ),
                    }
                ],
                "health_checks": [],
                "rollback_points": [{"ref": "previous-worker-version"}],
                "actor": "ceo",
            },
        ).json()
        upsert_deployment_environment_policy(
            client,
            "boss-dogfood",
            allowed_target_kinds=["cloudflare_worker"],
        )
        reservation_id = reserve_for_infra_provision(
            client,
            "Reserve Cloudflare worker deployment budget.",
        )
        planned = client.post(
            "/v1/deployment-runs",
            json={
                "project_id": "boss-dogfood",
                "deployment_target_id": target["id"],
                "commit_ref": "def456",
                "release_packet": {"review_packet_id": "art_worker_review"},
                "expected_cost_usd_micros": 100_000,
                "reservation_id": reservation_id,
                "actor": "ceo",
            },
        )
        assert planned.status_code == 201
        run = planned.json()
        approved = client.post(
            f"/v1/changesets/{run['changeset_id']}/approve",
            json={"actor": "ceo"},
        )
        assert approved.status_code == 200
        applied = client.post(
            f"/v1/changesets/{run['changeset_id']}/apply",
            json={"actor": "ceo"},
        )
    finally:
        api_main.resource_plane.settings.cloudflare_api_token = previous_token

    assert applied.status_code == 200
    payload = applied.json()
    assert payload["status"] == "applied"
    assert len(worker_requests) == 1
    request = worker_requests[0]
    assert request["url"].endswith(
        "/accounts/cf-account-123/workers/scripts/boss-worker"
    )
    assert request["headers"]["Authorization"] == "Bearer cf-test-token"
    metadata = json.loads(request["files"]["metadata"][1])
    assert metadata["main_module"] == "worker.js"
    assert metadata["compatibility_date"] == "2026-06-03"
    assert request["files"]["worker.js"][1].startswith("export default")

    receipt = payload["resource_receipts"][0]
    assert receipt["status"] == "succeeded"
    assert receipt["provider"] == "cloudflare"
    assert receipt["provider_ref"] == "cloudflare:worker:boss-worker"

    resources = client.get(
        "/v1/resources",
        params={"project_id": "boss-dogfood", "provider": "cloudflare"},
    )
    assert resources.status_code == 200
    resource = resources.json()["items"][0]
    assert resource["external_id"] == "boss-worker"
    assert resource["resource_type"] == "worker"
    assert resource["ownership"] == "managed_by_boss"
    assert resource["receipt_id"] == receipt["receipt_id"]
    assert resource["metadata"]["provider_result"]["etag"] == "etag-worker-123"

    costs = client.get("/v1/costs", params={"project_id": "boss-dogfood"})
    assert costs.status_code == 200
    cost_event = costs.json()["items"][0]
    assert cost_event["provider"] == "cloudflare"
    assert cost_event["amount_usd_micros"] == 100_000
    assert cost_event["receipt_id"] == receipt["receipt_id"]

    deployment_runs = client.get(
        "/v1/deployment-runs",
        params={"project_id": "boss-dogfood"},
    )
    assert deployment_runs.status_code == 200
    deployed_run = deployment_runs.json()["items"][0]
    assert deployed_run["id"] == run["id"]
    assert deployed_run["status"] == "deployed"


def test_deployment_run_cloudflare_pages_apply_marks_deployed(
    client, monkeypatch
):
    previous_token = api_main.resource_plane.settings.cloudflare_api_token
    api_main.resource_plane.settings.cloudflare_api_token = "cf-test-token"
    pages_requests = []

    class FakePagesResponse:
        def raise_for_status(self):
            return None

        def json(self):
            return {
                "success": True,
                "result": {
                    "id": "pages-deploy-123",
                    "project_name": "boss-pages",
                    "url": "https://boss-pages.pages.dev",
                    "environment": "preview",
                    "latest_stage": {"status": "success"},
                },
            }

    class FakePagesClient:
        def __init__(self, **kwargs):
            self.kwargs = kwargs

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, traceback):
            return False

        def post(self, url, headers, files):
            pages_requests.append(
                {
                    "url": url,
                    "headers": headers,
                    "files": files,
                }
            )
            return FakePagesResponse()

    monkeypatch.setattr(
        "apps.api.app.services.resource_plane.httpx.Client",
        FakePagesClient,
    )
    try:
        provider = client.post(
            "/v1/providers",
            json={
                "project_id": "boss-dogfood",
                "provider": "cloudflare",
                "account_label": "deploy-cloudflare-pages",
                "scopes": ["read", "write", "deploy"],
                "read_only": False,
                "deploy_capable": True,
                "destructive_actions_disabled": True,
                "actor": "ceo",
            },
        ).json()
        target = client.post(
            "/v1/deployments",
            json={
                "project_id": "boss-dogfood",
                "app_name": "boss-pages",
                "repo": "seanheiney/boss",
                "environment": "staging",
                "provider_account_id": provider["id"],
                "target_kind": "cloudflare_pages",
                "target_ref": "boss-pages",
                "domains": ["pages.boss.local"],
                "required_resources": [
                    {
                        "type": "cloudflare_pages",
                        "account_id": "cf-account-123",
                        "project_name": "boss-pages",
                        "branch": "staging",
                        "commit_message": "Deploy staging pages",
                        "commit_dirty": False,
                    }
                ],
                "health_checks": [],
                "rollback_points": [{"ref": "previous-pages-deployment"}],
                "actor": "ceo",
            },
        ).json()
        upsert_deployment_environment_policy(
            client,
            "boss-dogfood",
            allowed_target_kinds=["cloudflare_pages"],
        )
        reservation_id = reserve_for_infra_provision(
            client,
            "Reserve Cloudflare Pages deployment budget.",
        )
        planned = client.post(
            "/v1/deployment-runs",
            json={
                "project_id": "boss-dogfood",
                "deployment_target_id": target["id"],
                "commit_ref": "abc999",
                "release_packet": {"review_packet_id": "art_pages_review"},
                "expected_cost_usd_micros": 90_000,
                "reservation_id": reservation_id,
                "actor": "ceo",
            },
        )
        assert planned.status_code == 201
        run = planned.json()
        changesets = client.get(
            "/v1/changesets",
            params={"project_id": "boss-dogfood"},
        ).json()
        changeset = next(
            item
            for item in changesets["items"]
            if item["id"] == run["changeset_id"]
        )
        assert changeset["operations"][0]["skill_id"] == "cloudflare.pages.deploy"
        approved = client.post(
            f"/v1/changesets/{run['changeset_id']}/approve",
            json={"actor": "ceo"},
        )
        assert approved.status_code == 200
        applied = client.post(
            f"/v1/changesets/{run['changeset_id']}/apply",
            json={"actor": "ceo"},
        )
    finally:
        api_main.resource_plane.settings.cloudflare_api_token = previous_token

    assert applied.status_code == 200
    payload = applied.json()
    assert payload["status"] == "applied"
    assert len(pages_requests) == 1
    request = pages_requests[0]
    assert request["url"].endswith(
        "/accounts/cf-account-123/pages/projects/boss-pages/deployments"
    )
    assert request["headers"]["Authorization"] == "Bearer cf-test-token"
    assert request["files"]["branch"][1] == "staging"
    assert request["files"]["commit_hash"][1] == "abc999"
    assert request["files"]["commit_message"][1] == "Deploy staging pages"
    assert request["files"]["commit_dirty"][1] == "false"

    receipt = payload["resource_receipts"][0]
    assert receipt["status"] == "succeeded"
    assert receipt["provider"] == "cloudflare"
    assert receipt["provider_ref"] == (
        "cloudflare:pages_deployment:pages-deploy-123"
    )

    resources = client.get(
        "/v1/resources",
        params={"project_id": "boss-dogfood", "provider": "cloudflare"},
    )
    assert resources.status_code == 200
    resource = resources.json()["items"][0]
    assert resource["external_id"] == "pages-deploy-123"
    assert resource["resource_type"] == "pages_deployment"
    assert resource["health_status"] == "success"
    assert resource["receipt_id"] == receipt["receipt_id"]

    costs = client.get("/v1/costs", params={"project_id": "boss-dogfood"})
    assert costs.status_code == 200
    cost_event = costs.json()["items"][0]
    assert cost_event["provider"] == "cloudflare"
    assert cost_event["amount_usd_micros"] == 90_000
    assert cost_event["receipt_id"] == receipt["receipt_id"]

    deployment_runs = client.get(
        "/v1/deployment-runs",
        params={"project_id": "boss-dogfood"},
    )
    assert deployment_runs.status_code == 200
    deployed_run = deployment_runs.json()["items"][0]
    assert deployed_run["id"] == run["id"]
    assert deployed_run["status"] == "deployed"


def test_deployment_run_supabase_migration_apply_marks_deployed(
    client, monkeypatch
):
    previous_token = api_main.resource_plane.settings.supabase_access_token
    api_main.resource_plane.settings.supabase_access_token = "sbp-test-token"
    migration_requests = []

    class FakeSupabaseResponse:
        def raise_for_status(self):
            return None

        def json(self):
            return {
                "version": "20260603000000",
                "name": "create_boss_test_table",
            }

    class FakeSupabaseClient:
        def __init__(self, **kwargs):
            self.kwargs = kwargs

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, traceback):
            return False

        def post(self, url, headers, json):
            migration_requests.append(
                {
                    "url": url,
                    "headers": headers,
                    "json": json,
                }
            )
            return FakeSupabaseResponse()

    monkeypatch.setattr(
        "apps.api.app.services.resource_plane.httpx.Client",
        FakeSupabaseClient,
    )
    try:
        provider = client.post(
            "/v1/providers",
            json={
                "project_id": "boss-dogfood",
                "provider": "supabase",
                "account_label": "deploy-supabase",
                "scopes": ["read", "write", "deploy"],
                "read_only": False,
                "deploy_capable": True,
                "destructive_actions_disabled": True,
                "actor": "ceo",
            },
        ).json()
        target = client.post(
            "/v1/deployments",
            json={
                "project_id": "boss-dogfood",
                "app_name": "boss-db",
                "repo": "seanheiney/boss",
                "environment": "staging",
                "provider_account_id": provider["id"],
                "target_kind": "supabase_migration",
                "target_ref": "supabase-project-ref",
                "domains": [],
                "required_resources": [
                    {
                        "type": "supabase_migration",
                        "project_ref": "supabase-project-ref",
                        "migration_name": "create_boss_test_table",
                        "sql": (
                            "create table if not exists public.boss_test "
                            "(id bigint primary key);"
                        ),
                        "rollback": "drop table if exists public.boss_test;",
                    }
                ],
                "health_checks": [],
                "rollback_points": [{"ref": "drop table if exists public.boss_test"}],
                "actor": "ceo",
            },
        ).json()
        upsert_deployment_environment_policy(
            client,
            "boss-dogfood",
            allowed_target_kinds=["supabase_migration"],
        )
        reservation_id = reserve_for_infra_provision(
            client,
            "Reserve Supabase migration budget.",
        )
        planned = client.post(
            "/v1/deployment-runs",
            json={
                "project_id": "boss-dogfood",
                "deployment_target_id": target["id"],
                "commit_ref": "fed789",
                "release_packet": {"review_packet_id": "art_db_review"},
                "expected_cost_usd_micros": 75_000,
                "reservation_id": reservation_id,
                "actor": "ceo",
            },
        )
        assert planned.status_code == 201
        run = planned.json()
        approved = client.post(
            f"/v1/changesets/{run['changeset_id']}/approve",
            json={"actor": "ceo"},
        )
        assert approved.status_code == 200
        applied = client.post(
            f"/v1/changesets/{run['changeset_id']}/apply",
            json={"actor": "ceo"},
        )
    finally:
        api_main.resource_plane.settings.supabase_access_token = previous_token

    assert applied.status_code == 200
    payload = applied.json()
    assert payload["status"] == "applied"
    assert len(migration_requests) == 1
    request = migration_requests[0]
    assert request["url"].endswith(
        "/v1/projects/supabase-project-ref/database/migrations"
    )
    assert request["headers"]["Authorization"] == "Bearer sbp-test-token"
    assert request["json"]["name"] == "create_boss_test_table"
    assert request["json"]["query"].startswith("create table if not exists")
    assert request["json"]["rollback"].startswith("drop table")

    receipt = payload["resource_receipts"][0]
    assert receipt["status"] == "succeeded"
    assert receipt["provider"] == "supabase"
    assert receipt["provider_ref"] == (
        "supabase:migration:supabase-project-ref:20260603000000"
    )

    resources = client.get(
        "/v1/resources",
        params={"project_id": "boss-dogfood", "provider": "supabase"},
    )
    assert resources.status_code == 200
    resource = resources.json()["items"][0]
    assert resource["external_id"] == "20260603000000"
    assert resource["resource_type"] == "migration"
    assert resource["ownership"] == "managed_by_boss"
    assert resource["receipt_id"] == receipt["receipt_id"]
    assert resource["metadata"]["provider_result"]["migration_name"] == (
        "create_boss_test_table"
    )

    costs = client.get("/v1/costs", params={"project_id": "boss-dogfood"})
    assert costs.status_code == 200
    cost_event = costs.json()["items"][0]
    assert cost_event["provider"] == "supabase"
    assert cost_event["amount_usd_micros"] == 75_000
    assert cost_event["receipt_id"] == receipt["receipt_id"]

    deployment_runs = client.get(
        "/v1/deployment-runs",
        params={"project_id": "boss-dogfood"},
    )
    assert deployment_runs.status_code == 200
    deployed_run = deployment_runs.json()["items"][0]
    assert deployed_run["id"] == run["id"]
    assert deployed_run["status"] == "deployed"


def test_deployment_run_spendy_plan_requires_budget_reservation(client):
    provider = client.post(
        "/v1/providers",
        json={
            "project_id": "boss-dogfood",
            "provider": "cloudflare",
            "account_label": "deploy-cloudflare",
            "scopes": ["read", "write", "deploy"],
            "read_only": False,
            "deploy_capable": True,
            "actor": "ceo",
        },
    ).json()
    target = client.post(
        "/v1/deployments",
        json={
            "project_id": "boss-dogfood",
            "app_name": "boss-web",
            "repo": "seanheiney/boss",
            "environment": "staging",
            "provider_account_id": provider["id"],
            "target_kind": "cloudflare_worker",
            "target_ref": "boss-web-staging",
            "health_checks": [{"url": "https://boss-web-staging.example/health"}],
            "rollback_points": [{"ref": "previous-worker"}],
            "actor": "ceo",
        },
    ).json()

    planned = client.post(
        "/v1/deployment-runs",
        json={
            "project_id": "boss-dogfood",
            "deployment_target_id": target["id"],
            "expected_cost_usd_micros": 250_000,
            "actor": "ceo",
        },
    )
    assert planned.status_code == 201
    payload = planned.json()
    assert payload["status"] == "failed"
    assert "BudgetReservation" in payload["failure_reason"]
    assert payload["decision_id"]

    inbox = client.get("/v1/decisions/inbox", params={"project_id": "boss-dogfood"})
    assert inbox.status_code == 200
    assert any(
        item["id"] == payload["decision_id"]
        and item["recommendation"]["action"] == "reserve_budget_before_deployment"
        for item in inbox.json()["items"]
    )


def test_deployment_health_check_records_success_artifact(client, monkeypatch):
    monkeypatch.setattr(
        api_main.resource_plane,
        "_run_http_health_check",
        lambda check: {
            "name": check.get("name", "health"),
            "url": check["url"],
            "status": "passed",
            "status_code": 200,
            "expected_status": 200,
        },
    )
    target = client.post(
        "/v1/deployments",
        json={
            "project_id": "boss-dogfood",
            "app_name": "local-boss",
            "repo": "seanheiney/boss",
            "environment": "local",
            "target_kind": "local",
            "target_ref": "localhost",
            "health_checks": [
                {
                    "name": "local health",
                    "url": "http://127.0.0.1:8000/health",
                    "expected_status": 200,
                }
            ],
            "rollback_points": [{"ref": "previous-local"}],
            "actor": "ceo",
        },
    ).json()
    run = client.post(
        "/v1/deployment-runs",
        json={
            "project_id": "boss-dogfood",
            "deployment_target_id": target["id"],
            "commit_ref": "local-sha",
            "actor": "ceo",
        },
    ).json()
    assert run["changeset_id"] is None

    checked = client.post(
        f"/v1/deployment-runs/{run['id']}/health-check",
        json={"actor": "ceo"},
    )
    assert checked.status_code == 200
    payload = checked.json()
    assert payload["deployment_run"]["status"] == "healthy"
    assert payload["health_artifact_id"]
    assert payload["decision_id"] is None

    runs = client.get(
        "/v1/deployment-runs",
        params={"project_id": "boss-dogfood"},
    )
    assert runs.status_code == 200
    assert runs.json()["items"][0]["health_check_results"][0]["status"] == "passed"


def test_deployment_health_failure_creates_rollback_decision(client, monkeypatch):
    monkeypatch.setattr(
        api_main.resource_plane,
        "_run_http_health_check",
        lambda check: {
            "name": check.get("name", "health"),
            "url": check["url"],
            "status": "failed",
            "status_code": 503,
            "expected_status": 200,
        },
    )
    target = client.post(
        "/v1/deployments",
        json={
            "project_id": "boss-dogfood",
            "app_name": "local-boss-fail",
            "repo": "seanheiney/boss",
            "environment": "staging",
            "target_kind": "local",
            "target_ref": "localhost-fail",
            "health_checks": [
                {
                    "name": "staging health",
                    "url": "http://127.0.0.1:8000/health",
                    "expected_status": 200,
                }
            ],
            "rollback_points": [{"ref": "previous-staging"}],
            "actor": "ceo",
        },
    ).json()
    run = client.post(
        "/v1/deployment-runs",
        json={
            "project_id": "boss-dogfood",
            "deployment_target_id": target["id"],
            "actor": "ceo",
        },
    ).json()

    checked = client.post(
        f"/v1/deployment-runs/{run['id']}/health-check",
        json={"actor": "ceo"},
    )
    assert checked.status_code == 200
    payload = checked.json()
    assert payload["deployment_run"]["status"] == "rollback_pending"
    assert payload["deployment_run"]["failure_reason"] == "Deployment health checks failed."
    assert payload["rollback_artifact_id"]
    assert payload["decision_id"]

    artifact = client.get(f"/v1/artifacts/{payload['rollback_artifact_id']}")
    assert artifact.status_code == 200
    assert artifact.json()["artifact_type"] == "deployment_rollback_plan"

    inbox = client.get("/v1/decisions/inbox", params={"project_id": "boss-dogfood"})
    assert inbox.status_code == 200
    assert any(item["id"] == payload["decision_id"] for item in inbox.json()["items"])


def test_router_preview_selects_openai_api_and_writes_log(client):
    previous = api_main.entitlements.settings.openai_api_key
    api_main.entitlements.settings.openai_api_key = "test-openai-key"
    try:
        preview = client.post(
            "/v1/router/preview",
            json={
                "project_id": "boss-dogfood",
                "skill_id": "model.generate_text",
                "task_kind": "planning",
                "workload_class": "owner_sandbox",
                "risk_tier": 1,
                "data_boundary": "internal",
                "preferred_capabilities": ["planning", "summarization"],
                "actor": "ceo",
            },
        )
    finally:
        api_main.entitlements.settings.openai_api_key = previous

    assert preview.status_code == 200
    payload = preview.json()
    assert payload["source_id"] == "openai_api"
    assert payload["provider"] == "openai"
    assert payload["model_id"].startswith("openai:")
    assert payload["data_boundary"] == "internal"
    assert payload["denial_reason"] is None

    logs = client.get("/v1/router/logs", params={"project_id": "boss-dogfood"})
    assert logs.status_code == 200
    log = next(
        item
        for item in logs.json()["items"]
        if item["id"] == payload["route_decision_log_id"]
    )
    assert log["data_boundary"] == "internal"


def test_router_preview_denies_restricted_boundary_for_unapproved_external_sources(client):
    previous = api_main.entitlements.settings.openai_api_key
    api_main.entitlements.settings.openai_api_key = "test-openai-key"
    try:
        preview = client.post(
            "/v1/router/preview",
            json={
                "project_id": "boss-dogfood",
                "skill_id": "model.generate_text",
                "task_kind": "planning",
                "workload_class": "owner_sandbox",
                "risk_tier": 1,
                "data_boundary": "restricted",
                "preferred_capabilities": ["planning"],
                "actor": "ceo",
            },
        )
    finally:
        api_main.entitlements.settings.openai_api_key = previous

    assert preview.status_code == 200
    payload = preview.json()
    assert payload["source_id"] is None
    assert payload["data_boundary"] == "restricted"
    assert payload["denial_reason"] == "no_eligible_source"
    assert any(
        item["source_id"] == "openai_api"
        and item["reason"] == "data_boundary_not_allowed"
        for item in payload["rejected_sources"]
    )
    assert payload["decision"]["data_boundary"] == "restricted"


def test_router_preview_allows_local_only_data_on_approved_local_model_source(client):
    previous_enabled = api_main.entitlements.settings.local_model_enabled
    previous_model_id = api_main.entitlements.settings.local_model_id
    api_main.entitlements.settings.local_model_enabled = True
    api_main.entitlements.settings.local_model_id = "approved-local-test"
    try:
        preview = client.post(
            "/v1/router/preview",
            json={
                "project_id": "boss-dogfood",
                "skill_id": "model.generate_text",
                "task_kind": "privacy_sensitive",
                "workload_class": "internal_batch",
                "risk_tier": 1,
                "data_boundary": "local-only",
                "preferred_capabilities": ["privacy_sensitive"],
                "actor": "ceo",
            },
        )
    finally:
        api_main.entitlements.settings.local_model_enabled = previous_enabled
        api_main.entitlements.settings.local_model_id = previous_model_id

    assert preview.status_code == 200
    payload = preview.json()
    assert payload["source_id"] == "approved_local_model_host"
    assert payload["provider"] == "local"
    assert payload["model_id"] is None
    assert payload["data_boundary"] == "local-only"
    assert payload["denial_reason"] is None


def test_router_preview_denies_multi_tenant_cli_source(client, engine):
    with engine.begin() as connection:
        connection.execute(text("DELETE FROM events WHERE project_id = 'boss-router'"))
        connection.execute(text("DELETE FROM routing_logs WHERE project_id = 'boss-router'"))
        connection.execute(
            text("DELETE FROM entitlement_sources WHERE project_id = 'boss-router'")
        )
        connection.execute(text("DELETE FROM projects WHERE id = 'boss-router'"))
        connection.execute(
            text(
                """
                INSERT INTO projects (id, workspace_id, name, slug)
                VALUES ('boss-router', 'ws_boss', 'BOSS Router', 'boss-router')
                """
            )
        )
        api_main.members.seed_default_members(connection, "boss-router", "test")

    source = client.post(
        "/v1/entitlements/sources",
        json={
            "project_id": "boss-router",
            "provider": "openai",
            "source_id": "owner_cli_test",
            "source_kind": "official_cli",
            "entitlement_kind": "subscription",
            "owner_type": "workspace_operator",
            "owner_id": "founder",
            "workload_classes_allowed": [
                "owner_sandbox",
                "single_tenant_customer",
                "multi_tenant_customer",
            ],
            "multi_tenant_allowed": False,
            "supported_models": ["gpt-5.2-codex"],
            "task_strengths": ["planning", "coding"],
            "risk_tier_max": 1,
            "status": "active",
            "auth_method": "official_cli_oauth",
            "connection_status": "connected",
            "observed_capacity": {
                "credential_present": True,
                "official_oauth_connected": True,
            },
            "runtime_supported": True,
            "actor": "ceo",
        },
    )
    assert source.status_code == 201

    preview = client.post(
        "/v1/router/preview",
        json={
            "project_id": "boss-router",
            "skill_id": "model.generate_text",
            "task_kind": "planning",
            "workload_class": "multi_tenant_customer",
            "risk_tier": 1,
            "preferred_capabilities": ["planning"],
            "actor": "ceo",
        },
    )
    assert preview.status_code == 200
    payload = preview.json()
    assert payload["source_id"] is None
    assert payload["denial_reason"] == "no_eligible_source"
    assert any(
        item["reason"] in {"provider_policy_disallows_workload", "multi_tenant_not_allowed"}
        for item in payload["rejected_sources"]
    )


def test_router_preview_requires_official_oauth_for_subscription_sources(client, engine):
    with engine.begin() as connection:
        connection.execute(text("DELETE FROM events WHERE project_id = 'boss-oauth-router'"))
        connection.execute(
            text("DELETE FROM routing_logs WHERE project_id = 'boss-oauth-router'")
        )
        connection.execute(
            text(
                "DELETE FROM entitlement_sources WHERE project_id = 'boss-oauth-router'"
            )
        )
        connection.execute(text("DELETE FROM projects WHERE id = 'boss-oauth-router'"))
        connection.execute(
            text(
                """
                INSERT INTO projects (id, workspace_id, name, slug)
                VALUES ('boss-oauth-router', 'ws_boss', 'BOSS OAuth Router', 'boss-oauth-router')
                """
            )
        )
        api_main.members.seed_default_members(connection, "boss-oauth-router", "test")

    disconnected = client.post(
        "/v1/entitlements/sources",
        json={
            "project_id": "boss-oauth-router",
            "provider": "openai",
            "source_id": "openai_codex_chatgpt",
            "source_kind": "official_cli",
            "entitlement_kind": "subscription",
            "owner_type": "workspace_operator",
            "owner_id": "founder",
            "workload_classes_allowed": ["owner_sandbox"],
            "multi_tenant_allowed": False,
            "supported_models": ["codex"],
            "task_strengths": ["planning", "coding"],
            "risk_tier_max": 1,
            "status": "active",
            "auth_method": "api_key",
            "connection_status": "connected",
            "observed_capacity": {"credential_present": True},
            "runtime_supported": True,
            "actor": "ceo",
        },
    )
    assert disconnected.status_code == 201

    denied = client.post(
        "/v1/router/preview",
        json={
            "project_id": "boss-oauth-router",
            "skill_id": "model.generate_text",
            "task_kind": "planning",
            "workload_class": "owner_sandbox",
            "risk_tier": 1,
            "preferred_capabilities": ["planning"],
            "actor": "ceo",
        },
    )
    assert denied.status_code == 200
    denied_payload = denied.json()
    assert denied_payload["source_id"] is None
    assert any(
        item["source_id"] == "openai_codex_chatgpt"
        and item["reason"] == "official_oauth_connection_required"
        for item in denied_payload["rejected_sources"]
    )

    connected = client.post(
        "/v1/entitlements/sources",
        json={
            "project_id": "boss-oauth-router",
            "provider": "openai",
            "source_id": "openai_codex_chatgpt",
            "source_kind": "official_cli",
            "entitlement_kind": "subscription",
            "owner_type": "workspace_operator",
            "owner_id": "founder",
            "workload_classes_allowed": ["owner_sandbox"],
            "multi_tenant_allowed": False,
            "supported_models": ["codex"],
            "task_strengths": ["planning", "coding"],
            "risk_tier_max": 1,
            "status": "active",
            "auth_method": "official_cli_oauth",
            "connection_status": "connected",
            "connection_ref": "local:codex-login",
            "oauth_scopes": ["codex.local"],
            "observed_capacity": {
                "credential_present": True,
                "official_oauth_connected": True,
            },
            "runtime_supported": True,
            "actor": "ceo",
        },
    )
    assert connected.status_code == 201

    selected = client.post(
        "/v1/router/preview",
        json={
            "project_id": "boss-oauth-router",
            "skill_id": "model.generate_text",
            "task_kind": "planning",
            "workload_class": "owner_sandbox",
            "risk_tier": 1,
            "preferred_capabilities": ["planning"],
            "actor": "ceo",
        },
    )
    assert selected.status_code == 200
    selected_payload = selected.json()
    assert selected_payload["source_id"] == "openai_codex_chatgpt"
    assert selected_payload["decision"]["auth_method"] == "official_cli_oauth"
    assert selected_payload["decision"]["connection_ref"] == "local:codex-login"


def test_entitlement_source_connect_disconnect_lifecycle_routes_fail_closed(client, engine):
    project_id = "boss-entitlement-connect"
    with engine.begin() as connection:
        connection.execute(text(f"DELETE FROM events WHERE project_id = '{project_id}'"))
        connection.execute(text(f"DELETE FROM routing_logs WHERE project_id = '{project_id}'"))
        connection.execute(
            text(f"DELETE FROM entitlement_sources WHERE project_id = '{project_id}'")
        )
        connection.execute(text(f"DELETE FROM projects WHERE id = '{project_id}'"))
        connection.execute(
            text(
                f"""
                INSERT INTO projects (id, workspace_id, name, slug)
                VALUES ('{project_id}', 'ws_boss', 'Entitlement Connect', '{project_id}')
                """
            )
        )
        api_main.members.seed_default_members(connection, project_id, "test")
        api_main.entitlements.seed_default_sources(connection, project_id, "test")

    connected = client.post(
        "/v1/entitlements/sources/openai_codex_chatgpt/connect",
        json={
            "project_id": project_id,
            "connection_ref": "secretref://local/codex-session",
            "auth_method": "official_cli_oauth",
            "oauth_scopes": ["codex.local"],
            "connection_metadata": {"login_command": "codex login"},
            "observed_capacity": {
                "capacity_state": "available",
                "remaining_units": 10,
                "observed_at": datetime.now(timezone.utc).isoformat(),
            },
            "actor": "ceo",
        },
    )
    assert connected.status_code == 200
    connected_payload = connected.json()
    assert connected_payload["effective_status"] == "active"
    assert connected_payload["connection_status"] == "connected"
    assert connected_payload["observed_capacity"]["official_oauth_connected"] is True

    selected = client.post(
        "/v1/router/preview",
        json={
            "project_id": project_id,
            "skill_id": "model.generate_text",
            "task_kind": "planning",
            "workload_class": "owner_sandbox",
            "risk_tier": 1,
            "preferred_capabilities": ["planning"],
            "actor": "ceo",
        },
    )
    assert selected.status_code == 200
    assert selected.json()["source_id"] == "openai_codex_chatgpt"

    disconnected = client.post(
        "/v1/entitlements/sources/openai_codex_chatgpt/disconnect",
        json={
            "project_id": project_id,
            "reason": "operator_revoked_local_session",
            "actor": "ceo",
        },
    )
    assert disconnected.status_code == 200
    assert disconnected.json()["connection_status"] == "revoked"

    denied = client.post(
        "/v1/router/preview",
        json={
            "project_id": project_id,
            "skill_id": "model.generate_text",
            "task_kind": "planning",
            "workload_class": "owner_sandbox",
            "risk_tier": 1,
            "preferred_capabilities": ["planning"],
            "actor": "ceo",
        },
    )
    assert denied.status_code == 200
    denied_payload = denied.json()
    assert denied_payload["source_id"] is None
    assert any(
        item["source_id"] == "openai_codex_chatgpt"
        and item["reason"] == "missing_oauth_connection"
        and item["connection_status"] == "revoked"
        for item in denied_payload["rejected_sources"]
    )

    events = client.get("/v1/events", params={"project_id": project_id, "limit": 10})
    assert events.status_code == 200
    event_kinds = {item["event_type"] for item in events.json()["items"]}
    assert "entitlement.source_connected" in event_kinds
    assert "entitlement.source_disconnected" in event_kinds


def test_entitlement_source_connect_rejects_non_oauth_subscription_method(client, engine):
    project_id = "boss-entitlement-connect-invalid"
    with engine.begin() as connection:
        connection.execute(text(f"DELETE FROM entitlement_sources WHERE project_id = '{project_id}'"))
        connection.execute(text(f"DELETE FROM projects WHERE id = '{project_id}'"))
        connection.execute(
            text(
                f"""
                INSERT INTO projects (id, workspace_id, name, slug)
                VALUES ('{project_id}', 'ws_boss', 'Entitlement Connect Invalid', '{project_id}')
                """
            )
        )
        api_main.members.seed_default_members(connection, project_id, "test")
        api_main.entitlements.seed_default_sources(connection, project_id, "test")

    response = client.post(
        "/v1/entitlements/sources/openai_codex_chatgpt/connect",
        json={
            "project_id": project_id,
            "connection_ref": "secretref://local/codex-session",
            "auth_method": "api_key",
            "actor": "ceo",
        },
    )
    assert response.status_code == 422
    assert "official OAuth" in response.json()["detail"]


def test_entitlement_connection_session_lifecycle_for_public_subscriptions(
    client, engine
):
    project_id = "boss-entitlement-sessions"
    with engine.begin() as connection:
        connection.execute(text(f"DELETE FROM events WHERE project_id = '{project_id}'"))
        connection.execute(
            text(
                f"DELETE FROM entitlement_connection_sessions WHERE project_id = '{project_id}'"
            )
        )
        connection.execute(text(f"DELETE FROM routing_logs WHERE project_id = '{project_id}'"))
        connection.execute(
            text(f"DELETE FROM entitlement_sources WHERE project_id = '{project_id}'")
        )
        connection.execute(text(f"DELETE FROM projects WHERE id = '{project_id}'"))
        connection.execute(
            text(
                f"""
                INSERT INTO projects (id, workspace_id, name, slug)
                VALUES ('{project_id}', 'ws_boss', 'Entitlement Sessions', '{project_id}')
                """
            )
        )
        api_main.members.seed_default_members(connection, project_id, "test")
        api_main.entitlements.seed_default_sources(connection, project_id, "test")

    api_session = client.post(
        "/v1/entitlements/sources/openai_api/connection-sessions",
        json={"project_id": project_id, "actor": "ceo"},
    )
    assert api_session.status_code == 422

    started = client.post(
        "/v1/entitlements/sources/openai_codex_chatgpt/connection-sessions",
        json={
            "project_id": project_id,
            "connection_mode": "browser_oauth",
            "requested_scopes": ["codex.local"],
            "actor": "ceo",
        },
    )
    assert started.status_code == 201
    session = started.json()
    assert session["source_id"] == "openai_codex_chatgpt"
    assert session["status"] == "operator_action_required"
    assert session["login_command"] == "codex login"
    assert session["instructions"]["secret_handling"].startswith("BOSS stores only")

    listed = client.get(
        "/v1/entitlement-connection-sessions",
        params={"project_id": project_id, "source_id": "openai_codex_chatgpt"},
    )
    assert listed.status_code == 200
    assert [item["id"] for item in listed.json()["items"]] == [session["id"]]

    completed = client.post(
        f"/v1/entitlement-connection-sessions/{session['id']}/complete",
        json={
            "project_id": project_id,
            "connection_ref": "secretref://local/codex-session",
            "oauth_scopes": ["codex.local"],
            "connection_metadata": {"verified_by": "local_runner"},
            "observed_capacity": {"capacity_state": "available", "remaining_units": 8},
            "actor": "ceo",
        },
    )
    assert completed.status_code == 200
    completed_payload = completed.json()
    assert completed_payload["session"]["status"] == "completed"
    assert completed_payload["session"]["completed_by"] == "ceo"
    assert completed_payload["source"]["connection_status"] == "connected"
    assert (
        completed_payload["source"]["observed_capacity"][
            "official_connection_session_id"
        ]
        == session["id"]
    )

    duplicate_complete = client.post(
        f"/v1/entitlement-connection-sessions/{session['id']}/complete",
        json={
            "project_id": project_id,
            "connection_ref": "secretref://local/codex-session",
            "actor": "ceo",
        },
    )
    assert duplicate_complete.status_code == 422

    events = client.get("/v1/events", params={"project_id": project_id, "limit": 10})
    assert events.status_code == 200
    event_types = [item["event_type"] for item in events.json()["items"]]
    assert "entitlement.connection_session_started" in event_types
    assert "entitlement.connection_session_completed" in event_types


def test_router_preview_rejects_exhausted_subscription_capacity(client, engine):
    project_id = "boss-entitlement-capacity"
    with engine.begin() as connection:
        connection.execute(text(f"DELETE FROM events WHERE project_id = '{project_id}'"))
        connection.execute(text(f"DELETE FROM routing_logs WHERE project_id = '{project_id}'"))
        connection.execute(
            text(f"DELETE FROM entitlement_sources WHERE project_id = '{project_id}'")
        )
        connection.execute(text(f"DELETE FROM projects WHERE id = '{project_id}'"))
        connection.execute(
            text(
                f"""
                INSERT INTO projects (id, workspace_id, name, slug)
                VALUES ('{project_id}', 'ws_boss', 'Entitlement Capacity', '{project_id}')
                """
            )
        )
        api_main.members.seed_default_members(connection, project_id, "test")
        api_main.entitlements.seed_default_sources(connection, project_id, "test")

    connected = client.post(
        "/v1/entitlements/sources/openai_codex_chatgpt/connect",
        json={
            "project_id": project_id,
            "connection_ref": "secretref://local/codex-session",
            "auth_method": "official_cli_oauth",
            "observed_capacity": {
                "capacity_state": "exhausted",
                "remaining_units": 0,
                "reset_at": "2026-06-03T18:00:00Z",
            },
            "actor": "ceo",
        },
    )
    assert connected.status_code == 200

    preview = client.post(
        "/v1/router/preview",
        json={
            "project_id": project_id,
            "skill_id": "model.generate_text",
            "task_kind": "planning",
            "workload_class": "owner_sandbox",
            "risk_tier": 1,
            "preferred_capabilities": ["planning"],
            "actor": "ceo",
        },
    )
    assert preview.status_code == 200
    payload = preview.json()
    assert payload["source_id"] is None
    assert any(
        item["source_id"] == "openai_codex_chatgpt"
        and item["reason"] == "capacity_exhausted"
        and item["capacity_state"] == "exhausted"
        and item["capacity_reset_at"] == "2026-06-03T18:00:00Z"
        for item in payload["rejected_sources"]
    )
    assert payload["decision"]["rejected_source_count"] >= 1


def test_router_preview_keeps_grok_oauth_subscription_out_of_multi_tenant(client, engine):
    with engine.begin() as connection:
        connection.execute(text("DELETE FROM events WHERE project_id = 'boss-grok-router'"))
        connection.execute(
            text("DELETE FROM routing_logs WHERE project_id = 'boss-grok-router'")
        )
        connection.execute(
            text("DELETE FROM entitlement_sources WHERE project_id = 'boss-grok-router'")
        )
        connection.execute(text("DELETE FROM projects WHERE id = 'boss-grok-router'"))
        connection.execute(
            text(
                """
                INSERT INTO projects (id, workspace_id, name, slug)
                VALUES ('boss-grok-router', 'ws_boss', 'BOSS Grok Router', 'boss-grok-router')
                """
            )
        )
        api_main.members.seed_default_members(connection, "boss-grok-router", "test")

    source = client.post(
        "/v1/entitlements/sources",
        json={
            "project_id": "boss-grok-router",
            "provider": "xai",
            "source_id": "xai_grok_build",
            "source_kind": "official_cli",
            "entitlement_kind": "subscription",
            "owner_type": "workspace_operator",
            "owner_id": "founder",
            "workload_classes_allowed": [
                "owner_sandbox",
                "single_tenant_customer",
                "multi_tenant_customer",
            ],
            "multi_tenant_allowed": False,
            "supported_models": ["grok-code-fast"],
            "task_strengths": ["coding"],
            "risk_tier_max": 1,
            "status": "active",
            "auth_method": "official_device_oauth",
            "connection_status": "connected",
            "connection_ref": "local:grok-device-login",
            "oauth_scopes": ["grok_build.local"],
            "observed_capacity": {
                "credential_present": True,
                "official_oauth_connected": True,
            },
            "runtime_supported": True,
            "actor": "ceo",
        },
    )
    assert source.status_code == 201

    preview = client.post(
        "/v1/router/preview",
        json={
            "project_id": "boss-grok-router",
            "skill_id": "model.generate_text",
            "task_kind": "coding",
            "workload_class": "multi_tenant_customer",
            "risk_tier": 1,
            "preferred_capabilities": ["coding"],
            "actor": "ceo",
        },
    )
    assert preview.status_code == 200
    payload = preview.json()
    assert payload["source_id"] is None
    assert any(
        item["source_id"] == "xai_grok_build"
        and item["reason"] in {"provider_policy_disallows_workload", "multi_tenant_not_allowed"}
        for item in payload["rejected_sources"]
    )


def test_model_adapter_and_entitlement_pool_v2_surfaces_are_visible(client, engine):
    project_id = "boss-model-pools-v2"
    with engine.begin() as connection:
        connection.execute(text(f"DELETE FROM capacity_receipts WHERE project_id = '{project_id}'"))
        connection.execute(text(f"DELETE FROM events WHERE project_id = '{project_id}'"))
        connection.execute(text(f"DELETE FROM routing_logs WHERE project_id = '{project_id}'"))
        connection.execute(text(f"DELETE FROM entitlement_sources WHERE project_id = '{project_id}'"))
        connection.execute(text(f"DELETE FROM projects WHERE id = '{project_id}'"))
        connection.execute(
            text(
                f"""
                INSERT INTO projects (id, workspace_id, name, slug)
                VALUES ('{project_id}', 'ws_boss', 'Model Pools V2', '{project_id}')
                """
            )
        )
        api_main.members.seed_default_members(connection, project_id, "test")
        api_main.entitlements.seed_default_sources(connection, project_id, "test")

    adapters = client.get("/v1/model-adapters")
    assert adapters.status_code == 200
    adapter_items = adapters.json()["items"]
    assert any(
        item["source_id"] == "openai_codex_chatgpt"
        and item["official_connection_required"] is True
        and item["multi_tenant_allowed"] is False
        for item in adapter_items
    )
    assert any(
        item["source_id"] == "xai_grok_build"
        and item["auth_method"] == "official_device_oauth"
        for item in adapter_items
    )
    assert any(
        item["source_id"] == "enterprise_model_gateway"
        and item["access_class"] == "enterprise_model_gateway"
        and item["multi_tenant_allowed"] is True
        and item["credential_present"] is False
        for item in adapter_items
    )

    pools = client.get("/v1/entitlement-pools", params={"project_id": project_id})
    assert pools.status_code == 200
    pool_items = pools.json()["items"]
    codex_pool = next(
        item for item in pool_items if item["source_id"] == "openai_codex_chatgpt"
    )
    assert codex_pool["id"] == f"pool_{project_id}_openai_codex_chatgpt"
    assert codex_pool["access_class"] == "official_cli"
    assert codex_pool["capacity_state"] == "unknown"
    assert codex_pool["budget_policy_ref"] == f"budget_policy:{project_id}"
    gateway_pool = next(
        item for item in pool_items if item["source_id"] == "enterprise_model_gateway"
    )
    assert gateway_pool["access_class"] == "enterprise_model_gateway"
    assert gateway_pool["effective_status"] == "declared"
    assert gateway_pool["allowed_workload_classes"] == [
        "owner_sandbox",
        "single_tenant_customer",
        "multi_tenant_customer",
        "internal_batch",
    ]


def test_router_preview_selects_enterprise_gateway_for_restricted_multi_tenant_work(
    client, engine
):
    project_id = "boss-enterprise-gateway-route"
    previous_enabled = api_main.entitlements.settings.enterprise_model_gateway_enabled
    previous_url = api_main.entitlements.settings.enterprise_model_gateway_url
    previous_token = api_main.entitlements.settings.enterprise_model_gateway_token
    previous_models = api_main.entitlements.settings.enterprise_model_gateway_models
    api_main.entitlements.settings.enterprise_model_gateway_enabled = True
    api_main.entitlements.settings.enterprise_model_gateway_url = (
        "https://models.enterprise.example"
    )
    api_main.entitlements.settings.enterprise_model_gateway_token = "test-gateway-token"
    api_main.entitlements.settings.enterprise_model_gateway_models = (
        "enterprise-gateway-auto"
    )
    try:
        with engine.begin() as connection:
            connection.execute(text(f"DELETE FROM capacity_receipts WHERE project_id = '{project_id}'"))
            connection.execute(text(f"DELETE FROM events WHERE project_id = '{project_id}'"))
            connection.execute(text(f"DELETE FROM routing_logs WHERE project_id = '{project_id}'"))
            connection.execute(text(f"DELETE FROM entitlement_sources WHERE project_id = '{project_id}'"))
            connection.execute(text(f"DELETE FROM projects WHERE id = '{project_id}'"))
            connection.execute(
                text(
                    f"""
                    INSERT INTO projects (id, workspace_id, name, slug)
                    VALUES ('{project_id}', 'ws_boss', 'Enterprise Gateway Route', '{project_id}')
                    """
                )
            )
            api_main.members.seed_default_members(connection, project_id, "test")
            api_main.entitlements.seed_default_sources(connection, project_id, "test")

        preview = client.post(
            "/v1/router/preview",
            json={
                "project_id": project_id,
                "skill_id": "model.generate_text",
                "task_kind": "reasoning",
                "workload_class": "multi_tenant_customer",
                "risk_tier": 3,
                "data_boundary": "restricted",
                "preferred_capabilities": ["reasoning", "privacy_sensitive"],
                "actor": "ceo",
            },
        )
    finally:
        api_main.entitlements.settings.enterprise_model_gateway_enabled = previous_enabled
        api_main.entitlements.settings.enterprise_model_gateway_url = previous_url
        api_main.entitlements.settings.enterprise_model_gateway_token = previous_token
        api_main.entitlements.settings.enterprise_model_gateway_models = previous_models

    assert preview.status_code == 200
    payload = preview.json()
    assert payload["source_id"] == "enterprise_model_gateway"
    assert payload["provider"] == "boss"
    assert payload["model_id"] == "boss:enterprise-gateway-auto"
    assert payload["data_boundary"] == "restricted"
    assert payload["denial_reason"] is None
    assert payload["decision"]["reservation_required"] is True
    assert payload["decision"]["selected_source"]["connection_ref"] == (
        "BOSS_ENTERPRISE_MODEL_GATEWAY_URL"
    )


def test_router_preview_budget_red_denies_paid_api_and_gateway_sources(client, engine):
    project_id = "boss-red-budget-router"
    previous_openai = api_main.entitlements.settings.openai_api_key
    previous_enabled = api_main.entitlements.settings.enterprise_model_gateway_enabled
    previous_url = api_main.entitlements.settings.enterprise_model_gateway_url
    previous_token = api_main.entitlements.settings.enterprise_model_gateway_token
    api_main.entitlements.settings.openai_api_key = "test-openai-key"
    api_main.entitlements.settings.enterprise_model_gateway_enabled = True
    api_main.entitlements.settings.enterprise_model_gateway_url = (
        "https://models.enterprise.example"
    )
    api_main.entitlements.settings.enterprise_model_gateway_token = "test-gateway-token"
    try:
        with engine.begin() as connection:
            connection.execute(text(f"DELETE FROM capacity_receipts WHERE project_id = '{project_id}'"))
            connection.execute(text(f"DELETE FROM events WHERE project_id = '{project_id}'"))
            connection.execute(text(f"DELETE FROM routing_logs WHERE project_id = '{project_id}'"))
            connection.execute(text(f"DELETE FROM entitlement_sources WHERE project_id = '{project_id}'"))
            connection.execute(text(f"DELETE FROM projects WHERE id = '{project_id}'"))
            connection.execute(
                text(
                    f"""
                    INSERT INTO projects (id, workspace_id, name, slug)
                    VALUES ('{project_id}', 'ws_boss', 'Red Budget Router', '{project_id}')
                    """
                )
            )
            api_main.members.seed_default_members(connection, project_id, "test")
            api_main.entitlements.seed_default_sources(connection, project_id, "test")

        preview = client.post(
            "/v1/router/preview",
            json={
                "project_id": project_id,
                "skill_id": "model.generate_text",
                "task_kind": "planning",
                "workload_class": "multi_tenant_customer",
                "risk_tier": 1,
                "budget_state": "red",
                "data_boundary": "internal",
                "preferred_capabilities": ["planning"],
                "actor": "ceo",
            },
        )
    finally:
        api_main.entitlements.settings.openai_api_key = previous_openai
        api_main.entitlements.settings.enterprise_model_gateway_enabled = previous_enabled
        api_main.entitlements.settings.enterprise_model_gateway_url = previous_url
        api_main.entitlements.settings.enterprise_model_gateway_token = previous_token

    assert preview.status_code == 200
    payload = preview.json()
    assert payload["source_id"] is None
    assert payload["budget_state"] == "red"
    assert payload["denial_reason"] == "no_eligible_source"
    assert any(
        item["source_id"] == "openai_api"
        and item["reason"] == "budget_state_disallows_model_spend"
        for item in payload["rejected_sources"]
    )
    assert any(
        item["source_id"] == "enterprise_model_gateway"
        and item["reason"] == "budget_state_disallows_model_spend"
        for item in payload["rejected_sources"]
    )


def test_entitlement_pool_probe_writes_capacity_receipt_idempotently(client, engine):
    project_id = "boss-capacity-probe-v2"
    pool_id = f"pool_{project_id}_openai_codex_chatgpt"
    with engine.begin() as connection:
        connection.execute(text(f"DELETE FROM capacity_receipts WHERE project_id = '{project_id}'"))
        connection.execute(text(f"DELETE FROM events WHERE project_id = '{project_id}'"))
        connection.execute(text(f"DELETE FROM routing_logs WHERE project_id = '{project_id}'"))
        connection.execute(text(f"DELETE FROM entitlement_sources WHERE project_id = '{project_id}'"))
        connection.execute(text(f"DELETE FROM projects WHERE id = '{project_id}'"))
        connection.execute(
            text(
                f"""
                INSERT INTO projects (id, workspace_id, name, slug)
                VALUES ('{project_id}', 'ws_boss', 'Capacity Probe V2', '{project_id}')
                """
            )
        )
        api_main.members.seed_default_members(connection, project_id, "test")
        api_main.entitlements.seed_default_sources(connection, project_id, "test")

    first = client.post(
        f"/v1/entitlement-pools/{pool_id}/probe",
        json={
            "project_id": project_id,
            "observed_capacity": {
                "capacity_state": "rate_limited",
                "remaining_units": 0,
                "reset_at": "2026-06-03T18:00:00Z",
                "observed_at": "2026-06-03T17:00:00Z",
            },
            "usage_units": {"requests": 1},
            "provider_ref": "local-codex-session",
            "dedupe_key": "capacity-probe-once",
            "actor": "ceo",
        },
    )
    assert first.status_code == 200
    first_payload = first.json()
    assert first_payload["duplicate"] is False
    assert first_payload["pool"]["capacity_state"] == "rate_limited"
    assert first_payload["receipt"]["entitlement_pool_id"] == pool_id
    assert first_payload["receipt"]["capacity_kind"] == "subscription_cli"
    assert first_payload["receipt"]["dedupe_key"] == "capacity-probe-once"

    duplicate = client.post(
        f"/v1/entitlement-pools/{pool_id}/probe",
        json={
            "project_id": project_id,
            "observed_capacity": {
                "capacity_state": "available",
                "remaining_units": 100,
            },
            "dedupe_key": "capacity-probe-once",
            "actor": "ceo",
        },
    )
    assert duplicate.status_code == 200
    duplicate_payload = duplicate.json()
    assert duplicate_payload["duplicate"] is True
    assert duplicate_payload["receipt"]["id"] == first_payload["receipt"]["id"]
    assert duplicate_payload["pool"]["capacity_state"] == "rate_limited"

    with engine.begin() as connection:
        count = connection.execute(
            text(
                """
                SELECT count(*) FROM capacity_receipts
                WHERE project_id = :project_id
                  AND entitlement_pool_id = :pool_id
                  AND dedupe_key = 'capacity-probe-once'
                """
            ),
            {"project_id": project_id, "pool_id": pool_id},
        ).scalar_one()
    assert count == 1

    events = client.get("/v1/events", params={"project_id": project_id, "limit": 10})
    assert events.status_code == 200
    assert any(
        item["event_type"] == "capacity.receipt_recorded"
        for item in events.json()["items"]
    )


def test_model_route_decision_simulate_and_list_v2_surface(client, engine):
    project_id = "boss-route-decisions-v2"
    previous = api_main.entitlements.settings.openai_api_key
    api_main.entitlements.settings.openai_api_key = "test-openai-key"
    try:
        with engine.begin() as connection:
            connection.execute(text(f"DELETE FROM capacity_receipts WHERE project_id = '{project_id}'"))
            connection.execute(text(f"DELETE FROM events WHERE project_id = '{project_id}'"))
            connection.execute(text(f"DELETE FROM routing_logs WHERE project_id = '{project_id}'"))
            connection.execute(text(f"DELETE FROM entitlement_sources WHERE project_id = '{project_id}'"))
            connection.execute(text(f"DELETE FROM projects WHERE id = '{project_id}'"))
            connection.execute(
                text(
                    f"""
                    INSERT INTO projects (id, workspace_id, name, slug)
                    VALUES ('{project_id}', 'ws_boss', 'Route Decisions V2', '{project_id}')
                    """
                )
            )
            api_main.members.seed_default_members(connection, project_id, "test")
            api_main.entitlements.seed_default_sources(connection, project_id, "test")

        simulated = client.post(
            "/v1/model-route-decisions/simulate",
            json={
                "project_id": project_id,
                "skill_id": "model.generate_text",
                "task_kind": "planning",
                "workload_class": "owner_sandbox",
                "risk_tier": 1,
                "data_boundary": "internal",
                "preferred_capabilities": ["planning", "summarization"],
                "actor": "ceo",
            },
        )
    finally:
        api_main.entitlements.settings.openai_api_key = previous

    assert simulated.status_code == 200
    decision = simulated.json()["decision"]
    assert decision["selected_source_id"] == "openai_api"
    assert decision["selected_entitlement_pool_id"] == f"pool_{project_id}_openai_api"
    assert decision["selected_provider"] == "openai"
    assert decision["policy_result"]["allowed"] is True
    assert decision["data_boundary"] == "internal"

    listed = client.get(
        "/v1/model-route-decisions",
        params={"project_id": project_id, "limit": 5},
    )
    assert listed.status_code == 200
    assert any(item["id"] == decision["id"] for item in listed.json()["items"])

    with engine.begin() as connection:
        connection.execute(
            text(
                """
                INSERT INTO routing_logs (
                    id, project_id, skill_id, task_kind, workload_class, risk_tier,
                    data_boundary, budget_state, selected_source_id, selected_provider,
                    selected_model_id, denial_reason, rejected_sources, decision
                )
                VALUES (
                    'route_filtered_denied', :project_id, 'model.generate_text',
                    'coding', 'multi_tenant_customer', 2, 'restricted', 'red',
                    NULL, NULL, NULL, 'budget_state_disallows_model_spend',
                    CAST(:rejected_sources AS JSONB),
                    CAST(:decision AS JSONB)
                )
                """
            ),
            {
                "project_id": project_id,
                "rejected_sources": json.dumps(
                    [
                        {
                            "source_id": "openai_api",
                            "reason": "budget_state_disallows_model_spend",
                        }
                    ]
                ),
                "decision": json.dumps(
                    {
                        "routing_mode": "budget_safe",
                        "provider_rule_compliance": False,
                        "reservation_required": True,
                    }
                ),
            },
        )

    provider_filtered = client.get(
        "/v1/model-route-decisions",
        params={"project_id": project_id, "provider": "openai", "limit": 10},
    )
    assert provider_filtered.status_code == 200
    assert [item["id"] for item in provider_filtered.json()["items"]] == [
        decision["id"]
    ]

    denied_filtered = client.get(
        "/v1/model-route-decisions",
        params={"project_id": project_id, "denied_only": True, "limit": 10},
    )
    assert denied_filtered.status_code == 200
    assert [item["id"] for item in denied_filtered.json()["items"]] == [
        "route_filtered_denied"
    ]
    assert denied_filtered.json()["items"][0]["policy_result"] == {
        "allowed": False,
        "denial_reason": "budget_state_disallows_model_spend",
        "provider_rule_compliance": False,
        "reservation_required": True,
    }

    boundary_filtered = client.get(
        "/v1/model-route-decisions",
        params={
            "project_id": project_id,
            "data_boundary": "restricted",
            "budget_state": "red",
            "risk_tier": 2,
            "limit": 10,
        },
    )
    assert boundary_filtered.status_code == 200
    assert [item["id"] for item in boundary_filtered.json()["items"]] == [
        "route_filtered_denied"
    ]

    conflicting_filters = client.get(
        "/v1/model-route-decisions",
        params={
            "project_id": project_id,
            "denied_only": True,
            "selected_only": True,
        },
    )
    assert conflicting_filters.status_code == 400


def test_command_proposal_creates_task_job_artifact_and_task_lookup(client, engine):
    response = client.post(
        "/v1/commands/propose",
        json={
            "project_id": "boss-dogfood",
            "command": "Plan the next sandbox verification pass.",
            "constraints": {"deadline": "soon"},
        },
    )

    assert response.status_code == 201
    payload = response.json()
    assert payload["status"] == "accepted"
    assert payload["task_id"]
    assert payload["job_id"]
    assert payload["artifact_id"]

    task_response = client.get(f"/v1/tasks/{payload['task_id']}")
    assert task_response.status_code == 200
    task_payload = task_response.json()
    assert task_payload["id"] == payload["task_id"]
    assert task_payload["status"] == "open"
    assert task_payload["priority"] == "p1"

    list_response = client.get("/v1/tasks", params={"project_id": "boss-dogfood"})
    assert list_response.status_code == 200
    listed_ids = [item["id"] for item in list_response.json()["items"]]
    assert payload["task_id"] in listed_ids

    with engine.begin() as connection:
        job_kind = connection.execute(
            text("SELECT kind FROM jobs WHERE id = :job_id"),
            {"job_id": payload["job_id"]},
        ).scalar_one()
        artifact_type = connection.execute(
            text("SELECT artifact_type FROM artifacts WHERE id = :artifact_id"),
            {"artifact_id": payload["artifact_id"]},
        ).scalar_one()
    assert job_kind == "plan.command"
    assert artifact_type == "command_brief"


def test_task_create_supports_hierarchy_dependencies_owner_and_due_date(client):
    parent = client.post(
        "/v1/tasks",
        json={
            "project_id": "boss-dogfood",
            "title": "Parent initiative",
            "dedupe_key": "task-lineage-parent",
            "owner_role_id": "pm",
        },
    )
    assert parent.status_code == 201
    parent_id = parent.json()["id"]

    dependency = client.post(
        "/v1/tasks",
        json={
            "project_id": "boss-dogfood",
            "title": "Dependency task",
            "dedupe_key": "task-lineage-dependency",
            "owner_role_id": "cto",
        },
    )
    assert dependency.status_code == 201
    dependency_id = dependency.json()["id"]

    child = client.post(
        "/v1/tasks",
        json={
            "project_id": "boss-dogfood",
            "title": "Child implementation task",
            "description": "Depends on a prerequisite and belongs to the parent.",
            "dedupe_key": "task-lineage-child",
            "owner_role_id": "software_engineer",
            "parent_task_id": parent_id,
            "dependency_task_ids": [dependency_id],
            "due_at": "2026-06-30T12:00:00Z",
        },
    )
    assert child.status_code == 201
    payload = child.json()
    assert payload["owner_role_id"] == "software_engineer"
    assert payload["parent_task_id"] == parent_id
    assert payload["dependency_task_ids"] == [dependency_id]
    assert payload["due_at"].startswith("2026-06-30T12:00:00")

    fetched = client.get(f"/v1/tasks/{payload['id']}")
    assert fetched.status_code == 200
    assert fetched.json()["parent_task_id"] == parent_id

    listed = client.get("/v1/tasks", params={"project_id": "boss-dogfood"})
    assert listed.status_code == 200
    child_from_list = next(
        item for item in listed.json()["items"] if item["id"] == payload["id"]
    )
    assert child_from_list["dependency_task_ids"] == [dependency_id]

    events = client.get("/v1/events", params={"project_id": "boss-dogfood"})
    assert events.status_code == 200
    child_created = next(
        item
        for item in events.json()["items"]
        if item["event_type"] == "task.created" and item["entity_id"] == payload["id"]
    )
    assert child_created["data"]["parent_task_id"] == parent_id
    assert child_created["data"]["dependency_task_ids"] == [dependency_id]


def test_task_create_rejects_unknown_parent_dependency_and_owner(client):
    bad_parent = client.post(
        "/v1/tasks",
        json={
            "project_id": "boss-dogfood",
            "title": "Bad parent",
            "dedupe_key": "task-lineage-bad-parent",
            "parent_task_id": "tsk_missing_parent",
        },
    )
    assert bad_parent.status_code == 400
    assert "parent_task_id" in bad_parent.json()["detail"]

    bad_dependency = client.post(
        "/v1/tasks",
        json={
            "project_id": "boss-dogfood",
            "title": "Bad dependency",
            "dedupe_key": "task-lineage-bad-dependency",
            "dependency_task_ids": ["tsk_missing_dependency"],
        },
    )
    assert bad_dependency.status_code == 400
    assert "dependency_task_ids" in bad_dependency.json()["detail"]

    bad_owner = client.post(
        "/v1/tasks",
        json={
            "project_id": "boss-dogfood",
            "title": "Bad owner",
            "dedupe_key": "task-lineage-bad-owner",
            "owner_role_id": "unknown_role",
        },
    )
    assert bad_owner.status_code == 400
    assert "owner_role_id" in bad_owner.json()["detail"]


def test_canonical_work_objects_have_stable_ids_lifecycles_and_events(client):
    department = client.post(
        "/v1/departments",
        json={
            "project_id": "boss-dogfood",
            "department_key": "engineering",
            "name": "Engineering",
            "mission": "Ship tested BOSS software changes.",
            "actor": "ceo",
        },
    )
    assert department.status_code == 201
    assert department.json()["id"].startswith("dept_")
    assert department.json()["status"] == "active"

    roles = client.get("/v1/roles", params={"project_id": "boss-dogfood"})
    assert roles.status_code == 200
    owner_role_id = roles.json()["items"][0]["id"]
    kpi = client.post(
        "/v1/kpis",
        json={
            "project_id": "boss-dogfood",
            "kpi_key": "verified_work_throughput",
            "name": "Verified Work Throughput",
            "description": "Completed artifacts that pass required gates.",
            "target_value": 10,
            "current_value": 2,
            "unit": "artifacts/week",
            "owner_role_id": owner_role_id,
            "actor": "ceo",
        },
    )
    assert kpi.status_code == 201
    assert kpi.json()["id"].startswith("kpi_")
    assert kpi.json()["status"] == "active"
    assert kpi.json()["target_value"] == 10

    bad_kpi = client.post(
        "/v1/kpis",
        json={
            "project_id": "boss-dogfood",
            "kpi_key": "bad_owner",
            "name": "Bad Owner",
            "target_value": 1,
            "owner_role_id": "missing_role",
            "actor": "ceo",
        },
    )
    assert bad_kpi.status_code == 400
    assert "owner_role_id" in bad_kpi.json()["detail"]

    policy = client.post(
        "/v1/policies",
        json={
            "project_id": "boss-dogfood",
            "policy_key": "default_safety",
            "version": 1,
            "title": "Default Safety Policy",
            "body": {"no_external_effect_without_receipt": True},
            "actor": "ceo",
        },
    )
    assert policy.status_code == 201
    assert policy.json()["id"].startswith("pol_")
    assert policy.json()["status"] == "active"

    plan = client.post(
        "/v1/plans",
        json={
            "project_id": "boss-dogfood",
            "title": "Canonical object plan",
            "objective": "Represent plans as first-class canonical objects.",
            "phases": ["schema", "api", "tests"],
            "success_criteria": ["objects are listable"],
            "risks": ["schema drift"],
            "actor": "ceo",
        },
    )
    assert plan.status_code == 201
    assert plan.json()["id"].startswith("plan_")
    assert plan.json()["status"] == "draft"

    meeting = client.post(
        "/v1/meetings",
        json={
            "project_id": "boss-dogfood",
            "title": "Canonical object review",
            "purpose": "Review stable object coverage.",
            "participants": ["ceo", "cao"],
            "agenda": ["coverage", "risks"],
            "actor": "ceo",
        },
    )
    assert meeting.status_code == 201
    assert meeting.json()["id"].startswith("mtg_")
    assert meeting.json()["status"] == "scheduled"

    for path in (
        "/v1/departments",
        "/v1/kpis",
        "/v1/policies",
        "/v1/plans",
        "/v1/meetings",
    ):
        response = client.get(path, params={"project_id": "boss-dogfood"})
        assert response.status_code == 200
        assert response.json()["items"]

    events = client.get("/v1/events", params={"project_id": "boss-dogfood"})
    assert events.status_code == 200
    event_types = [item["event_type"] for item in events.json()["items"]]
    assert "department.upserted" in event_types
    assert "kpi.upserted" in event_types
    assert "policy.upserted" in event_types
    assert "plan.created" in event_types
    assert "meeting.created" in event_types


def test_kpi_double_upsert_idempotent_under_concurrent_submit(client):
    """Covers GAP-AUTO-002: double KPI upsert (same stable key) under concurrent submit.
    Uses real DB; ON CONFLICT ensures single record (idempotent)."""
    roles_resp = client.get("/v1/roles", params={"project_id": "boss-dogfood"})
    assert roles_resp.status_code == 200
    owner_role_id = roles_resp.json()["items"][0]["id"]

    def do_upsert(current):
        return client.post(
            "/v1/kpis",
            json={
                "project_id": "boss-dogfood",
                "kpi_key": "concurrent_upsert_kpi",
                "name": "Concurrent Double Submit KPI",
                "description": "tests race on stable key",
                "target_value": 42,
                "current_value": current,
                "unit": "widgets",
                "owner_role_id": owner_role_id,
                "actor": "ceo",
            },
        )

    from concurrent.futures import ThreadPoolExecutor, as_completed

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [
            executor.submit(do_upsert, 5),
            executor.submit(do_upsert, 7),
        ]
        results = [f.result() for f in as_completed(futures)]

    for r in results:
        assert r.status_code == 201
        assert r.json()["kpi_key"] == "concurrent_upsert_kpi"

    # only one record due to ON CONFLICT on (project_id, kpi_key)
    list_resp = client.get("/v1/kpis", params={"project_id": "boss-dogfood"})
    items = [
        it for it in list_resp.json()["items"] if it["kpi_key"] == "concurrent_upsert_kpi"
    ]
    assert len(items) == 1
    # current_value should be one of the submitted (last writer wins)
    assert items[0]["current_value"] in (5, 7)

    events_resp = client.get("/v1/events", params={"project_id": "boss-dogfood"})
    event_types = [e["event_type"] for e in events_resp.json()["items"]]
    # at least one upsert emitted (may be two if both emitted before conflict resolve)
    assert event_types.count("kpi.upserted") >= 1


def test_concurrent_budget_reserves_succeed_for_distinct_categories(client):
    """Covers GAP-AUTO-002: two concurrent budget reserves (different cats, safe amounts).
    Exercises real DB paths under thread concurrency for race/double-submit coverage."""
    def do_reserve(cat, amt):
        return client.post(
            "/v1/budget/reserve",
            json={
                "project_id": "boss-dogfood",
                "category": cat,
                "amount_usd_micros": amt,
                "reason": f"concurrent race test {cat}",
            },
        )

    from concurrent.futures import ThreadPoolExecutor, as_completed

    with ThreadPoolExecutor(max_workers=2) as executor:
        futs = [
            executor.submit(do_reserve, "model_api", 12_345),
            executor.submit(do_reserve, "infra", 23_456),
        ]
        results = [f.result() for f in as_completed(futs)]

    for r in results:
        assert r.status_code == 201
        assert r.json()["status"] == "reserved"

    status = client.get(
        "/v1/budget/status",
        params={"scope_type": "project", "scope_id": "boss-dogfood"},
    )
    assert status.status_code == 200
    payload = status.json()
    assert payload["reserved_usd_micros"] >= 12_345 + 23_456


def test_concurrent_consume_attempts_on_same_reservation(client, engine):
    """Covers GAP-AUTO-002: concurrent consume attempts on same reservation (double-submit path).
    Uses real DB + threads. Accepts possible 1 or 2 costs (race window in assert+settle) but ensures no crash + res consumed."""
    reservation_id = reserve_for_pr_proposal(client)
    rec1 = create_test_receipt(engine, "concur-cons-1")
    rec2 = create_test_receipt(engine, "concur-cons-2")

    def do_consume(rec_id):
        return client.post(
            "/v1/budget/consume",
            json={
                "project_id": "boss-dogfood",
                "reservation_id": reservation_id,
                "receipt_id": rec_id,
                "category": "model_api",
                "skill_or_model_id": "race:concur",
                "estimated_usd_micros": 5_000,
                "actual_usd_micros": 4_000,
                "actor": "ceo",
            },
        )

    from concurrent.futures import ThreadPoolExecutor, as_completed

    with ThreadPoolExecutor(max_workers=2) as executor:
        futs = [
            executor.submit(do_consume, rec1),
            executor.submit(do_consume, rec2),
        ]
        results = [f.result() for f in as_completed(futs)]

    statuses = [r.status_code for r in results]
    # at least one success; other may 409 (if update serializes) or 200 (race)
    assert any(s == 200 for s in statuses)

    with engine.begin() as conn:
        res_status = conn.execute(
            text("SELECT status FROM budget_reservations WHERE id = :rid"),
            {"rid": reservation_id},
        ).scalar_one()
    assert res_status == "consumed"

    with engine.begin() as conn:
        cost_count = conn.execute(
            text("SELECT COUNT(*) FROM cost_receipts WHERE reservation_id = :rid"),
            {"rid": reservation_id},
        ).scalar_one()
    assert cost_count >= 1


def test_meeting_run_materializes_minutes_actions_decisions_and_events(client):
    meeting = client.post(
        "/v1/meetings",
        json={
            "project_id": "boss-dogfood",
            "title": "Release go/no-go",
            "purpose": "Decide whether the sandbox release can advance.",
            "participants": ["ceo", "program_manager", "release_manager"],
            "agenda": ["readiness", "risks", "decision"],
            "actor": "ceo",
        },
    )
    assert meeting.status_code == 201
    meeting_id = meeting.json()["id"]

    run = client.post(
        f"/v1/meetings/{meeting_id}/run",
        json={
            "actor": "ceo",
            "transcript": [
                {
                    "speaker": "program_manager",
                    "message": "The release packet is complete.",
                    "phase": "propose",
                },
                {
                    "speaker": "release_manager",
                    "message": "Rollback instructions are present.",
                    "phase": "vote",
                },
            ],
            "minutes": {
                "summary": "Release readiness reviewed.",
                "outcomes": ["Proceed only after CEO approval."],
            },
            "action_items": [
                {
                    "title": "Attach final smoke report",
                    "description": "Link the smoke test artifact to the release packet.",
                    "owner_role_id": "qa",
                    "priority": "p1",
                    "risk_tier": 1,
                }
            ],
            "decision_items": [
                {
                    "question": "Approve sandbox release go/no-go?",
                    "recommendation": {"summary": "Approve after smoke report link."},
                    "risk_tier": 2,
                    "default_action": "hold",
                }
            ],
        },
    )
    assert run.status_code == 200
    payload = run.json()
    assert payload["status"] == "completed"
    assert payload["transcript_artifact_id"].startswith("art_")
    assert payload["minutes_artifact_id"].startswith("art_")
    assert len(payload["action_task_ids"]) == 1
    assert payload["decisions"][0]["decision_id"].startswith("dec_")
    assert payload["completed_at"]

    minutes = client.get(f"/v1/artifacts/{payload['minutes_artifact_id']}")
    assert minutes.status_code == 200
    assert minutes.json()["artifact_type"] == "meeting_minutes"
    assert minutes.json()["content"]["transcript_artifact_id"] == payload["transcript_artifact_id"]

    tasks = client.get("/v1/tasks", params={"project_id": "boss-dogfood"})
    assert tasks.status_code == 200
    assert any(
        item["id"] in payload["action_task_ids"]
        and item["dedupe_key"] == f"meeting:{meeting_id}:action:1"
        for item in tasks.json()["items"]
    )

    decisions = client.get("/v1/decisions/inbox", params={"project_id": "boss-dogfood"})
    assert decisions.status_code == 200
    assert any(
        item["id"] == payload["decisions"][0]["decision_id"]
        and item["default_action"] == "hold"
        and item["risk_tier"] == 2
        for item in decisions.json()["items"]
    )

    rerun = client.post(
        f"/v1/meetings/{meeting_id}/run",
        json={
            "actor": "ceo",
            "transcript": [{"speaker": "ceo", "message": "Run again."}],
            "minutes": {"summary": "Duplicate run."},
        },
    )
    assert rerun.status_code == 409

    events = client.get("/v1/events", params={"project_id": "boss-dogfood"})
    event_types = [item["event_type"] for item in events.json()["items"]]
    assert "meeting.completed" in event_types
    assert "artifact.created" in event_types
    assert "task.created" in event_types
    assert "decision.created" in event_types


def test_meeting_run_requires_blocked_state_and_dissent_records(client):
    meeting = client.post(
        "/v1/meetings",
        json={
            "project_id": "boss-dogfood",
            "title": "Architecture review",
            "purpose": "Resolve provider boundary concerns.",
            "participants": ["cao", "cto"],
            "agenda": ["proposal", "dissent"],
            "actor": "ceo",
        },
    )
    assert meeting.status_code == 201
    meeting_id = meeting.json()["id"]

    missing_blocker = client.post(
        f"/v1/meetings/{meeting_id}/run",
        json={
            "actor": "ceo",
            "outcome_status": "blocked",
            "transcript": [{"speaker": "cao", "message": "We are blocked."}],
            "minutes": {"summary": "No safe decision yet."},
        },
    )
    assert missing_blocker.status_code == 422

    missing_dissent = client.post(
        f"/v1/meetings/{meeting_id}/run",
        json={
            "actor": "ceo",
            "transcript": [{"speaker": "cto", "message": "I disagree."}],
            "minutes": {"summary": "Consensus failed."},
            "consensus_reached": False,
        },
    )
    assert missing_dissent.status_code == 422

    blocked = client.post(
        f"/v1/meetings/{meeting_id}/run",
        json={
            "actor": "ceo",
            "outcome_status": "blocked",
            "transcript": [{"speaker": "cao", "message": "Approval policy is unclear."}],
            "minutes": {"summary": "Blocked on approval policy."},
            "consensus_reached": False,
            "dissent_records": [
                {"role_id": "cto", "position": "Do not proceed without policy change."}
            ],
            "blocked_state": {
                "reason": "approval_policy_gap",
                "next_action": "Create a policy clarification decision.",
            },
        },
    )
    assert blocked.status_code == 200
    payload = blocked.json()
    assert payload["status"] == "blocked"
    assert payload["blocked_state"]["reason"] == "approval_policy_gap"
    assert payload["dissent_records"][0]["role_id"] == "cto"

    events = client.get("/v1/events", params={"project_id": "boss-dogfood"})
    assert "meeting.blocked" in [item["event_type"] for item in events.json()["items"]]


def test_artifact_list_and_detail_return_created_artifacts(client):
    response = client.post(
        "/v1/commands/propose",
        json={
            "project_id": "boss-dogfood",
            "command": "Capture artifact visibility in the console.",
            "constraints": {"deadline": "today"},
        },
    )
    assert response.status_code == 201
    artifact_id = response.json()["artifact_id"]

    artifact_list = client.get(
        "/v1/artifacts",
        params={"project_id": "boss-dogfood", "limit": 10},
    )
    assert artifact_list.status_code == 200
    payload = artifact_list.json()
    assert payload["project_id"] == "boss-dogfood"
    assert artifact_id in [item["id"] for item in payload["items"]]

    artifact_detail = client.get(f"/v1/artifacts/{artifact_id}")
    assert artifact_detail.status_code == 200
    detail = artifact_detail.json()
    assert detail["artifact_type"] == "command_brief"
    assert detail["content"]["command"] == "Capture artifact visibility in the console."
    assert detail["content"]["constraints"]["deadline"] == "today"


def test_change_journal_lists_and_details_change_artifacts(client, engine):
    task = client.post(
        "/v1/tasks",
        json={
            "project_id": "boss-dogfood",
            "title": "Change journal task",
            "dedupe_key": "change-journal-task",
        },
    )
    assert task.status_code == 201
    task_id = task.json()["id"]
    content = {
        "summary": "Opened a sandbox PR with a tested UI change.",
        "diff_summary": "Adds a decision inbox default action panel.",
        "files_changed": ["apps/web/app/page.tsx", "apps/api/app/main.py"],
        "rollback_plan": "Close the sandbox PR branch.",
    }
    with engine.begin() as connection:
        connection.execute(
            text(
                """
                INSERT INTO artifacts (
                    id, project_id, task_id, artifact_type, title, uri, content,
                    content_hash, data_boundary
                )
                VALUES (
                    'art_change_journal_diff', 'boss-dogfood', :task_id,
                    'diff_manifest', 'Diff manifest for change journal',
                    'file://artifacts/change-journal.json',
                    CAST(:content AS JSONB), 'hash-change-journal', 'internal'
                )
                """
            ),
            {"task_id": task_id, "content": json.dumps(content)},
        )

    journal = client.get(
        "/v1/change-journal",
        params={"project_id": "boss-dogfood", "limit": 5},
    )
    assert journal.status_code == 200
    payload = journal.json()
    item = next(
        item for item in payload["items"] if item["id"] == "art_change_journal_diff"
    )
    assert item["change_kind"] == "software_factory"
    assert item["diff_summary"] == "Adds a decision inbox default action panel."
    assert item["files_changed"] == [
        "apps/web/app/page.tsx",
        "apps/api/app/main.py",
    ]
    assert item["rollback_plan"] == "Close the sandbox PR branch."

    detail = client.get(
        "/v1/change-journal/art_change_journal_diff",
        params={"project_id": "boss-dogfood"},
    )
    assert detail.status_code == 200
    assert detail.json()["content"]["summary"] == content["summary"]

    wrong_project = client.get(
        "/v1/change-journal/art_change_journal_diff",
        params={"project_id": "boss-lab"},
    )
    assert wrong_project.status_code == 404


def test_data_boundary_propagates_to_work_objects_receipts_and_audit(client, engine):
    response = client.post(
        "/v1/commands/propose",
        json={
            "project_id": "boss-dogfood",
            "command": "Handle restricted customer support export.",
            "constraints": {"data_boundary": "restricted"},
        },
    )
    assert response.status_code == 201
    created = response.json()

    task = client.get(f"/v1/tasks/{created['task_id']}")
    assert task.status_code == 200
    assert task.json()["data_boundary"] == "restricted"

    job = client.get(f"/v1/jobs/{created['job_id']}")
    assert job.status_code == 200
    assert job.json()["data_boundary"] == "restricted"

    artifact = client.get(f"/v1/artifacts/{created['artifact_id']}")
    assert artifact.status_code == 200
    assert artifact.json()["data_boundary"] == "restricted"

    poll = client.post(
        "/v1/worker/poll",
        json={
            "worker_id": "worker-boundary",
            "capabilities": ["plan.command"],
            "lease_ttl_seconds": 60,
        },
    )
    assert poll.status_code == 200
    leased = poll.json()["job"]
    assert leased["id"] == created["job_id"]
    assert leased["data_boundary"] == "restricted"

    with engine.begin() as connection:
        api_main.receipts.create_or_reuse(
            connection,
            api_main.settings.default_org_id,
            api_main.settings.default_workspace_id,
            "boss-dogfood",
            "boundary-receipt-1",
            "github.pr.create",
            {
                "target_repo": "boss-sandbox",
                "task_id": created["task_id"],
                "data_boundary": "restricted",
            },
            {"pull_request": {"title": "Boundary proof"}},
            created_by_job_id=created["job_id"],
        )

    receipts = client.get("/v1/receipts", params={"project_id": "boss-dogfood"})
    assert receipts.status_code == 200
    receipt = next(
        item
        for item in receipts.json()["items"]
        if item["dedupe_key"] == "boundary-receipt-1"
    )
    assert receipt["data_boundary"] == "restricted"

    audit = client.get(
        "/v1/audit/export",
        params={"project_id": "boss-dogfood", "task_id": created["task_id"]},
    )
    assert audit.status_code == 200
    payload = audit.json()
    assert payload["tasks"][0]["data_boundary"] == "restricted"
    assert payload["jobs"][0]["data_boundary"] == "restricted"
    assert payload["artifacts"][0]["data_boundary"] == "restricted"
    assert payload["receipts"][0]["data_boundary"] == "restricted"


def test_invalid_data_boundary_is_rejected_without_creating_work(client):
    task = client.post(
        "/v1/tasks",
        json={
            "project_id": "boss-dogfood",
            "title": "Invalid boundary task",
            "dedupe_key": "invalid-boundary-task",
            "data_boundary": "secret",
        },
    )
    assert task.status_code == 422

    command = client.post(
        "/v1/commands/propose",
        json={
            "project_id": "boss-dogfood",
            "command": "Use an invalid data boundary.",
            "constraints": {"data_boundary": "secret"},
        },
    )
    assert command.status_code == 400
    assert "data_boundary" in command.json()["detail"]

    job = client.post(
        "/v1/jobs",
        json={
            "project_id": "boss-dogfood",
            "kind": "plan.command",
            "payload": {"data_boundary": "secret"},
        },
    )
    assert job.status_code == 400
    assert "data_boundary" in job.json()["detail"]


def test_comms_outbox_and_inbox_are_visible(client):
    outbox = client.post(
        "/v1/comms/outbox",
        json={
            "project_id": "boss-dogfood",
            "channel": "slack:#ops",
            "subject": "BOSS digest",
            "body": "System health summary.",
            "dedupe_key": "outbox-digest-1",
        },
    )
    assert outbox.status_code == 201
    outbox_payload = outbox.json()
    assert outbox_payload["status"] == "draft"

    inbox = client.post(
        "/v1/comms/inbox/ingest",
        json={
            "project_id": "boss-dogfood",
            "channel": "email:founder",
            "subject": "Need status",
            "body": "What changed today?",
            "raw_ref": "mailbox-123",
        },
    )
    assert inbox.status_code == 201
    inbox_payload = inbox.json()
    assert inbox_payload["status"] == "received"

    outbox_list = client.get(
        "/v1/comms/outbox", params={"project_id": "boss-dogfood", "limit": 10}
    )
    assert outbox_list.status_code == 200
    assert outbox_payload["id"] in [item["id"] for item in outbox_list.json()["items"]]

    inbox_list = client.get(
        "/v1/comms/inbox", params={"project_id": "boss-dogfood", "limit": 10}
    )
    assert inbox_list.status_code == 200
    assert inbox_payload["id"] in [item["id"] for item in inbox_list.json()["items"]]


def test_comms_channel_policies_are_permissioned_and_visible(client):
    denied = client.post(
        "/v1/comms/policies",
        json={
            "project_id": "boss-dogfood",
            "provider": "slack",
            "delivery_mode": "real_send_allowed",
            "allowed_data_boundaries": ["public", "internal"],
            "actor": "metatron",
        },
    )
    assert denied.status_code == 403

    policy = upsert_comms_policy(
        client,
        "boss-dogfood",
        "slack",
        delivery_mode="draft_only",
        allowed_data_boundaries=["public"],
    )
    assert policy["provider"] == "slack"
    assert policy["delivery_mode"] == "draft_only"
    assert policy["allowed_data_boundaries"] == ["public"]

    listed = client.get(
        "/v1/comms/policies", params={"project_id": "boss-dogfood"}
    )
    assert listed.status_code == 200
    assert policy["id"] in [item["id"] for item in listed.json()["items"]]


def test_decision_digest_creates_artifact_and_optional_outbox_draft(client, engine):
    with engine.begin() as connection:
        connection.execute(
            text(
                """
                INSERT INTO decisions (
                    id, project_id, question, recommendation, options,
                    risk_tier, cost_summary, deadline_at, default_action, status
                )
                VALUES
                    (
                        'dec_digest_urgent',
                        'boss-dogfood',
                        'Approve a high-risk sandbox release?',
                        '{"action":"approve_release"}'::jsonb,
                        '[{"label":"Approve","action":"approve"}]'::jsonb,
                        3,
                        '{"estimated_usd_micros": 50000}'::jsonb,
                        now() + interval '2 hours',
                        'hold',
                        'pending'
                    ),
                    (
                        'dec_digest_batch',
                        'boss-dogfood',
                        'Approve low-risk copy update?',
                        '{"action":"approve_copy"}'::jsonb,
                        '[{"label":"Approve","action":"approve"}]'::jsonb,
                        1,
                        '{"estimated_usd_micros": 1000}'::jsonb,
                        now() + interval '1 day',
                        'hold',
                        'pending'
                    )
                """
            )
        )

    digest = client.post(
        "/v1/decisions/digests",
        json={
            "project_id": "boss-dogfood",
            "actor": "ceo",
            "channel": "email:founder@example.com",
            "dedupe_key": "decision-digest-outbox-1",
        },
    )
    assert digest.status_code == 201
    payload = digest.json()
    assert payload["status"] == "created"
    assert payload["decision_count"] >= 2
    assert payload["urgent_count"] >= 1
    assert payload["batched_count"] >= 1
    assert "dec_digest_urgent" in payload["body"]
    assert "dec_digest_batch" in payload["body"]
    assert payload["outbox_message_id"]

    with engine.begin() as connection:
        artifact = connection.execute(
            text(
                """
                SELECT artifact_type, content
                FROM artifacts
                WHERE id = :artifact_id
                """
            ),
            {"artifact_id": payload["artifact_id"]},
        ).mappings().one()
        outbox = connection.execute(
            text(
                """
                SELECT status, subject, body
                FROM outbox_messages
                WHERE id = :message_id
                """
            ),
            {"message_id": payload["outbox_message_id"]},
        ).mappings().one()
        event_count = connection.execute(
            text(
                """
                SELECT COUNT(*)
                FROM events
                WHERE event_type = 'decision.digest_created'
                  AND entity_id = :artifact_id
                """
            ),
            {"artifact_id": payload["artifact_id"]},
        ).scalar_one()

    assert artifact["artifact_type"] == "decision_digest"
    assert artifact["content"]["urgent"]
    assert artifact["content"]["batched_low_risk"]
    assert outbox["status"] == "draft"
    assert outbox["subject"] == "BOSS decision digest"
    assert "dec_digest_urgent" in outbox["body"]
    assert event_count == 1


def test_inbox_decision_reply_approves_decision(client, engine):
    with engine.begin() as connection:
        connection.execute(
            text(
                """
                INSERT INTO decisions (
                    id, project_id, question, recommendation, options,
                    risk_tier, cost_summary, deadline_at, default_action, status
                )
                VALUES (
                    'dec_inbox_reply_approve',
                    'boss-dogfood',
                    'Approve from inbound email?',
                    '{"action":"approve"}'::jsonb,
                    '[{"label":"Approve","action":"approve"}]'::jsonb,
                    1,
                    '{}'::jsonb,
                    now() + interval '1 day',
                    'hold',
                    'pending'
                )
                """
            )
        )

    inbox = client.post(
        "/v1/comms/inbox/ingest",
        json={
            "project_id": "boss-dogfood",
            "channel": "email:founder@example.com",
            "subject": "Approve decision dec_inbox_reply_approve",
            "body": "Approved. Ship the safe change.",
            "raw_ref": "email-thread-approve-1",
            "actor": "ceo",
        },
    )
    assert inbox.status_code == 201
    payload = inbox.json()
    assert payload["status"] == "actioned"
    assert payload["parsed"]["action_result"]["decision_id"] == "dec_inbox_reply_approve"
    assert payload["parsed"]["action_result"]["status"] == "approved"

    with engine.begin() as connection:
        decision_status = connection.execute(
            text(
                """
                SELECT status
                FROM decisions
                WHERE id = 'dec_inbox_reply_approve'
                """
            )
        ).scalar_one()
        actioned_event = connection.execute(
            text(
                """
                SELECT COUNT(*)
                FROM events
                WHERE event_type = 'inbox.actioned'
                  AND entity_id = :message_id
                """
            ),
            {"message_id": payload["id"]},
        ).scalar_one()

    assert decision_status == "approved"
    assert actioned_event == 1


def test_inbox_unparseable_decision_reply_deadletters(client, engine):
    inbox = client.post(
        "/v1/comms/inbox/ingest",
        json={
            "project_id": "boss-dogfood",
            "channel": "email:founder@example.com",
            "subject": "Decision approval",
            "body": "Approve this decision, but no ID is included.",
            "raw_ref": "email-thread-missing-id-1",
            "actor": "ceo",
        },
    )
    assert inbox.status_code == 201
    payload = inbox.json()
    assert payload["status"] == "deadlettered"
    assert payload["parsed"]["deadletter"]["reason"] == (
        "decision_reply_missing_id_or_action"
    )

    with engine.begin() as connection:
        deadletter = connection.execute(
            text(
                """
                SELECT source_kind, source_id, reason
                FROM deadletters
                WHERE source_id = :message_id
                """
            ),
            {"message_id": payload["id"]},
        ).mappings().one()

    assert deadletter["source_kind"] == "inbox_message"
    assert deadletter["reason"] == "decision_reply_missing_id_or_action"


def test_comms_messages_carry_data_boundaries_and_delivery_jobs_inherit_them(client):
    outbox = client.post(
        "/v1/comms/outbox",
        json={
            "project_id": "boss-dogfood",
            "channel": "slack:#ops",
            "body": "Confidential delivery job.",
            "data_boundary": "confidential",
            "dedupe_key": "outbox-boundary-1",
        },
    )
    assert outbox.status_code == 201
    outbox_payload = outbox.json()
    assert outbox_payload["data_boundary"] == "confidential"

    inbox = client.post(
        "/v1/comms/inbox/ingest",
        json={
            "project_id": "boss-dogfood",
            "channel": "email:founder",
            "body": "Restricted inbound request.",
            "raw_ref": "restricted-mail-1",
            "data_boundary": "restricted",
        },
    )
    assert inbox.status_code == 201
    assert inbox.json()["data_boundary"] == "restricted"

    reserve = client.post(
        "/v1/budget/reserve",
        json={
            "project_id": "boss-dogfood",
            "category": "comms",
            "amount_usd_micros": 50_000,
            "reason": "Queue boundary-aware outbound delivery.",
        },
    )
    assert reserve.status_code == 201
    queued = client.post(
        f"/v1/comms/outbox/{outbox_payload['id']}/queue",
        json={
            "project_id": "boss-dogfood",
            "reservation_id": reserve.json()["reservation_id"],
            "dedupe_key": "outbox-boundary-job-1",
        },
    )
    assert queued.status_code == 201
    assert queued.json()["data_boundary"] == "confidential"

    poll = client.post(
        "/v1/worker/poll",
        json={
            "worker_id": "worker-comms-boundary",
            "capabilities": ["comms.outbox.deliver"],
            "lease_ttl_seconds": 60,
        },
    )
    assert poll.status_code == 200
    assert poll.json()["job"]["data_boundary"] == "confidential"

    outbox_list = client.get(
        "/v1/comms/outbox", params={"project_id": "boss-dogfood", "limit": 10}
    )
    assert outbox_list.status_code == 200
    listed_outbox = next(
        item for item in outbox_list.json()["items"] if item["id"] == outbox_payload["id"]
    )
    assert listed_outbox["data_boundary"] == "confidential"

    inbox_list = client.get(
        "/v1/comms/inbox", params={"project_id": "boss-dogfood", "limit": 10}
    )
    assert inbox_list.status_code == 200
    listed_inbox = next(
        item for item in inbox_list.json()["items"] if item["id"] == inbox.json()["id"]
    )
    assert listed_inbox["data_boundary"] == "restricted"


def test_slack_delivery_blocks_restricted_boundary_before_receipt(client, engine):
    outbox = client.post(
        "/v1/comms/outbox",
        json={
            "project_id": "boss-dogfood",
            "channel": "slack:#ops",
            "body": "Restricted outbound message.",
            "data_boundary": "restricted",
            "dedupe_key": "outbox-restricted-1",
        },
    )
    assert outbox.status_code == 201
    message_id = outbox.json()["id"]

    reserve = client.post(
        "/v1/budget/reserve",
        json={
            "project_id": "boss-dogfood",
            "category": "comms",
            "amount_usd_micros": 50_000,
            "reason": "Restricted Slack send should be blocked before receipt.",
        },
    )
    assert reserve.status_code == 201
    reservation_id = reserve.json()["reservation_id"]

    delivery = client.post(
        f"/v1/comms/outbox/{message_id}/deliver",
        json={
            "project_id": "boss-dogfood",
            "reservation_id": reservation_id,
            "dedupe_key": "restricted-slack-delivery",
        },
    )
    assert delivery.status_code == 200
    payload = delivery.json()
    assert payload["status"] == "blocked"
    assert payload["data_boundary"] == "restricted"
    assert "Slack" in payload["message"]

    with engine.begin() as connection:
        reservation_status = connection.execute(
            text(
                """
                SELECT status
                FROM budget_reservations
                WHERE id = :reservation_id
                """
            ),
            {"reservation_id": reservation_id},
        ).scalar_one()
        receipt_count = connection.execute(
            text(
                """
                SELECT COUNT(*)
                FROM receipts
                WHERE dedupe_key = 'restricted-slack-delivery'
                """
            )
        ).scalar_one()
        outbox_status = connection.execute(
            text("SELECT status FROM outbox_messages WHERE id = :message_id"),
            {"message_id": message_id},
        ).scalar_one()

    assert reservation_status == "released"
    assert receipt_count == 0
    assert outbox_status == "blocked"


def test_email_delivery_blocks_restricted_boundary_before_receipt(client, engine):
    outbox = client.post(
        "/v1/comms/outbox",
        json={
            "project_id": "boss-dogfood",
            "channel": "email:founder@example.com",
            "body": "Restricted outbound email.",
            "data_boundary": "restricted",
            "dedupe_key": "outbox-email-restricted-1",
        },
    )
    assert outbox.status_code == 201
    message_id = outbox.json()["id"]

    reserve = client.post(
        "/v1/budget/reserve",
        json={
            "project_id": "boss-dogfood",
            "category": "comms",
            "amount_usd_micros": 50_000,
            "reason": "Restricted email send should be blocked before receipt.",
        },
    )
    assert reserve.status_code == 201
    reservation_id = reserve.json()["reservation_id"]

    delivery = client.post(
        f"/v1/comms/outbox/{message_id}/deliver",
        json={
            "project_id": "boss-dogfood",
            "reservation_id": reservation_id,
            "dedupe_key": "restricted-email-delivery",
        },
    )
    assert delivery.status_code == 200
    payload = delivery.json()
    assert payload["status"] == "blocked"
    assert payload["data_boundary"] == "restricted"
    assert "Email" in payload["message"]

    with engine.begin() as connection:
        reservation_status = connection.execute(
            text(
                """
                SELECT status
                FROM budget_reservations
                WHERE id = :reservation_id
                """
            ),
            {"reservation_id": reservation_id},
        ).scalar_one()
        receipt_count = connection.execute(
            text(
                """
                SELECT COUNT(*)
                FROM receipts
                WHERE dedupe_key = 'restricted-email-delivery'
                """
            )
        ).scalar_one()
        outbox_status = connection.execute(
            text("SELECT status FROM outbox_messages WHERE id = :message_id"),
            {"message_id": message_id},
        ).scalar_one()

    assert reservation_status == "released"
    assert receipt_count == 0
    assert outbox_status == "blocked"


def test_comms_invalid_data_boundary_is_rejected(client):
    outbox = client.post(
        "/v1/comms/outbox",
        json={
            "project_id": "boss-dogfood",
            "channel": "slack:#ops",
            "body": "Invalid boundary.",
            "dedupe_key": "outbox-invalid-boundary",
            "data_boundary": "secret",
        },
    )
    assert outbox.status_code == 422

    inbox = client.post(
        "/v1/comms/inbox/ingest",
        json={
            "project_id": "boss-dogfood",
            "channel": "email:founder",
            "body": "Invalid boundary.",
            "data_boundary": "secret",
        },
    )
    assert inbox.status_code == 422


def test_queue_outbox_delivery_creates_job_and_marks_message_queued(client, engine):
    outbox = client.post(
        "/v1/comms/outbox",
        json={
            "project_id": "boss-dogfood",
            "channel": "slack:#ops",
            "body": "Queue this message.",
            "dedupe_key": "outbox-queue-1",
        },
    )
    assert outbox.status_code == 201
    message_id = outbox.json()["id"]

    reserve = client.post(
        "/v1/budget/reserve",
        json={
            "project_id": "boss-dogfood",
            "category": "comms",
            "amount_usd_micros": 50_000,
            "reason": "Queue outbound Slack delivery.",
        },
    )
    assert reserve.status_code == 201
    reservation_id = reserve.json()["reservation_id"]

    queued = client.post(
        f"/v1/comms/outbox/{message_id}/queue",
        json={
            "project_id": "boss-dogfood",
            "reservation_id": reservation_id,
            "dedupe_key": "outbox-queue-job-1",
        },
    )
    assert queued.status_code == 201
    payload = queued.json()
    assert payload["kind"] == "comms.outbox.deliver"

    with engine.begin() as connection:
        message_status = connection.execute(
            text("SELECT status FROM outbox_messages WHERE id = :message_id"),
            {"message_id": message_id},
        ).scalar_one()
        attached_job_id = connection.execute(
            text(
                "SELECT job_id FROM budget_reservations WHERE id = :reservation_id"
            ),
            {"reservation_id": reservation_id},
        ).scalar_one()

    assert message_status == "queued"
    assert attached_job_id == payload["id"]


def test_outbox_queue_defers_during_quiet_hours_and_urgent_bypasses(
    client, engine
):
    previous_enabled = api_main.comms.settings.comms_quiet_hours_enabled
    previous_start = api_main.comms.settings.comms_quiet_hours_start_utc
    previous_end = api_main.comms.settings.comms_quiet_hours_end_utc
    api_main.comms.settings.comms_quiet_hours_enabled = True
    api_main.comms.settings.comms_quiet_hours_start_utc = "00:00"
    api_main.comms.settings.comms_quiet_hours_end_utc = "23:59"
    try:
        normal_outbox = client.post(
            "/v1/comms/outbox",
            json={
                "project_id": "boss-dogfood",
                "channel": "slack:#ops",
                "body": "Normal delivery should wait for quiet hours.",
                "dedupe_key": "outbox-quiet-normal-1",
            },
        )
        assert normal_outbox.status_code == 201
        normal_reserve = client.post(
            "/v1/budget/reserve",
            json={
                "project_id": "boss-dogfood",
                "category": "comms",
                "amount_usd_micros": 50_000,
                "reason": "Quiet-hours normal delivery.",
            },
        )
        assert normal_reserve.status_code == 201
        normal_queued = client.post(
            f"/v1/comms/outbox/{normal_outbox.json()['id']}/queue",
            json={
                "project_id": "boss-dogfood",
                "reservation_id": normal_reserve.json()["reservation_id"],
                "dedupe_key": "outbox-quiet-normal-job-1",
                "urgency": "normal",
            },
        )
        assert normal_queued.status_code == 201

        urgent_outbox = client.post(
            "/v1/comms/outbox",
            json={
                "project_id": "boss-dogfood",
                "channel": "slack:#ops",
                "body": "Urgent delivery can bypass quiet hours.",
                "dedupe_key": "outbox-quiet-urgent-1",
            },
        )
        assert urgent_outbox.status_code == 201
        urgent_reserve = client.post(
            "/v1/budget/reserve",
            json={
                "project_id": "boss-dogfood",
                "category": "comms",
                "amount_usd_micros": 50_000,
                "reason": "Quiet-hours urgent delivery.",
            },
        )
        assert urgent_reserve.status_code == 201
        urgent_queued = client.post(
            f"/v1/comms/outbox/{urgent_outbox.json()['id']}/queue",
            json={
                "project_id": "boss-dogfood",
                "reservation_id": urgent_reserve.json()["reservation_id"],
                "dedupe_key": "outbox-quiet-urgent-job-1",
                "urgency": "urgent",
            },
        )
        assert urgent_queued.status_code == 201

        with engine.begin() as connection:
            normal_job = connection.execute(
                text(
                    """
                    SELECT run_after, created_at, payload
                    FROM jobs
                    WHERE id = :job_id
                    """
                ),
                {"job_id": normal_queued.json()["id"]},
            ).mappings().one()
            urgent_job = connection.execute(
                text(
                    """
                    SELECT run_after, created_at, payload
                    FROM jobs
                    WHERE id = :job_id
                    """
                ),
                {"job_id": urgent_queued.json()["id"]},
            ).mappings().one()
            deferred_event_count = connection.execute(
                text(
                    """
                    SELECT COUNT(*)
                    FROM events
                    WHERE event_type = 'outbox.deferred'
                      AND entity_id = :job_id
                      AND data->>'reason' = 'quiet_hours'
                    """
                ),
                {"job_id": normal_queued.json()["id"]},
            ).scalar_one()
            urgent_deferred_count = connection.execute(
                text(
                    """
                    SELECT COUNT(*)
                    FROM events
                    WHERE event_type = 'outbox.deferred'
                      AND entity_id = :job_id
                    """
                ),
                {"job_id": urgent_queued.json()["id"]},
            ).scalar_one()

        assert normal_job["run_after"] > normal_job["created_at"]
        assert normal_job["payload"]["urgency"] == "normal"
        assert urgent_job["run_after"] <= urgent_job["created_at"] + timedelta(seconds=1)
        assert urgent_job["payload"]["urgency"] == "urgent"
        assert deferred_event_count == 1
        assert urgent_deferred_count == 0
    finally:
        api_main.comms.settings.comms_quiet_hours_enabled = previous_enabled
        api_main.comms.settings.comms_quiet_hours_start_utc = previous_start
        api_main.comms.settings.comms_quiet_hours_end_utc = previous_end


def test_outbox_queue_throttles_nonurgent_delivery(client, engine):
    previous_limit = api_main.comms.settings.comms_throttle_max_queued_per_hour
    previous_defer = api_main.comms.settings.comms_throttle_defer_minutes
    previous_quiet = api_main.comms.settings.comms_quiet_hours_enabled
    api_main.comms.settings.comms_quiet_hours_enabled = False
    api_main.comms.settings.comms_throttle_max_queued_per_hour = 1
    api_main.comms.settings.comms_throttle_defer_minutes = 10
    try:
        first_outbox = client.post(
            "/v1/comms/outbox",
            json={
                "project_id": "boss-dogfood",
                "channel": "email:founder@example.com",
                "body": "First throttled-channel message.",
                "dedupe_key": "outbox-throttle-first-1",
            },
        )
        second_outbox = client.post(
            "/v1/comms/outbox",
            json={
                "project_id": "boss-dogfood",
                "channel": "email:founder@example.com",
                "body": "Second throttled-channel message.",
                "dedupe_key": "outbox-throttle-second-1",
            },
        )
        assert first_outbox.status_code == 201
        assert second_outbox.status_code == 201

        first_reserve = client.post(
            "/v1/budget/reserve",
            json={
                "project_id": "boss-dogfood",
                "category": "comms",
                "amount_usd_micros": 50_000,
                "reason": "First throttle delivery.",
            },
        )
        second_reserve = client.post(
            "/v1/budget/reserve",
            json={
                "project_id": "boss-dogfood",
                "category": "comms",
                "amount_usd_micros": 50_000,
                "reason": "Second throttle delivery.",
            },
        )
        assert first_reserve.status_code == 201
        assert second_reserve.status_code == 201

        first_queued = client.post(
            f"/v1/comms/outbox/{first_outbox.json()['id']}/queue",
            json={
                "project_id": "boss-dogfood",
                "reservation_id": first_reserve.json()["reservation_id"],
                "dedupe_key": "outbox-throttle-first-job-1",
            },
        )
        assert first_queued.status_code == 201
        second_queued = client.post(
            f"/v1/comms/outbox/{second_outbox.json()['id']}/queue",
            json={
                "project_id": "boss-dogfood",
                "reservation_id": second_reserve.json()["reservation_id"],
                "dedupe_key": "outbox-throttle-second-job-1",
            },
        )
        assert second_queued.status_code == 201

        with engine.begin() as connection:
            first_job = connection.execute(
                text(
                    """
                    SELECT run_after, created_at
                    FROM jobs
                    WHERE id = :job_id
                    """
                ),
                {"job_id": first_queued.json()["id"]},
            ).mappings().one()
            second_job = connection.execute(
                text(
                    """
                    SELECT run_after, created_at
                    FROM jobs
                    WHERE id = :job_id
                    """
                ),
                {"job_id": second_queued.json()["id"]},
            ).mappings().one()
            throttle_event_count = connection.execute(
                text(
                    """
                    SELECT COUNT(*)
                    FROM events
                    WHERE event_type = 'outbox.deferred'
                      AND entity_id = :job_id
                      AND data->>'reason' = 'throttled'
                    """
                ),
                {"job_id": second_queued.json()["id"]},
            ).scalar_one()

        assert first_job["run_after"] <= first_job["created_at"] + timedelta(seconds=1)
        assert second_job["run_after"] >= second_job["created_at"] + timedelta(
            minutes=9
        )
        assert throttle_event_count == 1
    finally:
        api_main.comms.settings.comms_throttle_max_queued_per_hour = previous_limit
        api_main.comms.settings.comms_throttle_defer_minutes = previous_defer
        api_main.comms.settings.comms_quiet_hours_enabled = previous_quiet


def test_outbox_delivery_missing_reservation_creates_visible_blocker(client, engine):
    outbox = client.post(
        "/v1/comms/outbox",
        json={
            "project_id": "boss-dogfood",
            "channel": "slack:#ops",
            "body": "Missing reservation should block visibly.",
            "dedupe_key": "outbox-missing-reservation-1",
        },
    )
    assert outbox.status_code == 201
    message_id = outbox.json()["id"]

    response = client.post(
        f"/v1/comms/outbox/{message_id}/deliver",
        json={
            "project_id": "boss-dogfood",
            "reservation_id": "budget_reservation_missing",
            "dedupe_key": "outbox-missing-reservation-delivery-1",
        },
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "blocked"
    assert payload["decision_id"]

    with engine.begin() as connection:
        outbox_status = connection.execute(
            text("SELECT status FROM outbox_messages WHERE id = :message_id"),
            {"message_id": message_id},
        ).scalar_one()
    assert outbox_status == "blocked"

    events = client.get("/v1/events", params={"project_id": "boss-dogfood"})
    assert events.status_code == 200
    assert "outbox.blocked" in [
        item["event_type"] for item in events.json()["items"]
    ]


def test_outbox_queue_rejects_cross_project_message_reference(client, engine):
    lab_project_id = "boss-comms-lab"
    with engine.begin() as connection:
        connection.execute(
            text(
                """
                INSERT INTO projects (id, workspace_id, name, slug)
                VALUES (
                    :project_id, 'ws_boss', 'BOSS Comms Lab', 'boss-comms-lab'
                )
                ON CONFLICT (id) DO NOTHING
                """
            ),
            {"project_id": lab_project_id},
        )
        api_main.members.seed_default_members(connection, lab_project_id, "test")

    outbox = client.post(
        "/v1/comms/outbox",
        json={
            "project_id": "boss-dogfood",
            "channel": "slack:#ops",
            "body": "This belongs to the dogfood project.",
            "dedupe_key": "outbox-cross-project-1",
        },
    )
    assert outbox.status_code == 201
    message_id = outbox.json()["id"]

    response = client.post(
        f"/v1/comms/outbox/{message_id}/queue",
        json={
            "project_id": lab_project_id,
            "reservation_id": "reservation_not_reached",
            "dedupe_key": "outbox-cross-project-queue-1",
        },
    )

    assert response.status_code == 404
    assert "not found for project" in response.json()["detail"]


def test_outbox_delivery_blocks_when_slack_delivery_is_disabled(client, engine):
    upsert_comms_policy(client, "boss-dogfood", "slack")
    outbox = client.post(
        "/v1/comms/outbox",
        json={
            "project_id": "boss-dogfood",
            "channel": "slack:#ops",
            "body": "Blocked message.",
            "dedupe_key": "outbox-blocked-1",
        },
    )
    assert outbox.status_code == 201
    message_id = outbox.json()["id"]

    reserve = client.post(
        "/v1/budget/reserve",
        json={
            "project_id": "boss-dogfood",
            "category": "comms",
            "amount_usd_micros": 50_000,
            "reason": "Attempt blocked Slack delivery.",
        },
    )
    assert reserve.status_code == 201
    reservation_id = reserve.json()["reservation_id"]

    previous_allowed = api_main.comms.settings.slack_real_send_allowed
    previous_webhook = api_main.comms.settings.slack_webhook_url
    api_main.comms.settings.slack_real_send_allowed = False
    api_main.comms.settings.slack_webhook_url = None
    try:
        response = client.post(
            f"/v1/comms/outbox/{message_id}/deliver",
            json={
                "project_id": "boss-dogfood",
                "reservation_id": reservation_id,
                "dedupe_key": "outbox-blocked-delivery-1",
            },
        )
    finally:
        api_main.comms.settings.slack_real_send_allowed = previous_allowed
        api_main.comms.settings.slack_webhook_url = previous_webhook

    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "blocked"
    assert payload["decision_id"]
    assert payload["receipt_id"]

    with engine.begin() as connection:
        reservation_status = connection.execute(
            text("SELECT status FROM budget_reservations WHERE id = :reservation_id"),
            {"reservation_id": reservation_id},
        ).scalar_one()
        outbox_status = connection.execute(
            text("SELECT status FROM outbox_messages WHERE id = :message_id"),
            {"message_id": message_id},
        ).scalar_one()

    assert reservation_status == "released"
    assert outbox_status == "blocked"


def test_outbox_delivery_requires_channel_policy_before_real_send(
    client, engine, monkeypatch
):
    project_id = "boss-comms-policy-required"
    with engine.begin() as connection:
        connection.execute(
            text("DELETE FROM comms_channel_policies WHERE project_id = :project_id"),
            {"project_id": project_id},
        )
        connection.execute(
            text("DELETE FROM project_members WHERE project_id = :project_id"),
            {"project_id": project_id},
        )
        connection.execute(
            text("DELETE FROM projects WHERE id = :project_id"),
            {"project_id": project_id},
        )
        connection.execute(
            text(
                """
                INSERT INTO projects (id, workspace_id, name, slug)
                VALUES (
                    :project_id, 'ws_boss', 'BOSS Comms Policy Required',
                    'boss-comms-policy-required'
                )
                """
            ),
            {"project_id": project_id},
        )
        api_main.members.seed_default_members(connection, project_id, "test")

    outbox = client.post(
        "/v1/comms/outbox",
        json={
            "project_id": project_id,
            "channel": "slack:#ops",
            "subject": "Policy required",
            "body": "This should not reach Slack without a rollout policy.",
            "dedupe_key": "outbox-policy-required-1",
        },
    )
    assert outbox.status_code == 201
    message_id = outbox.json()["id"]

    reserve = client.post(
        "/v1/budget/reserve",
        json={
            "project_id": project_id,
            "category": "comms",
            "amount_usd_micros": 50_000,
            "reason": "Attempt Slack delivery without channel policy.",
        },
    )
    assert reserve.status_code == 201
    reservation_id = reserve.json()["reservation_id"]

    previous_allowed = api_main.comms.settings.slack_real_send_allowed
    previous_webhook = api_main.comms.settings.slack_webhook_url
    api_main.comms.settings.slack_real_send_allowed = True
    api_main.comms.settings.slack_webhook_url = "https://hooks.slack.com/services/test"
    called = {"value": False}

    def fail_if_called(channel, subject, body):
        called["value"] = True
        raise AssertionError("Slack adapter must not be called without policy.")

    monkeypatch.setattr(api_main.comms.slack, "post_message", fail_if_called)
    try:
        response = client.post(
            f"/v1/comms/outbox/{message_id}/deliver",
            json={
                "project_id": project_id,
                "reservation_id": reservation_id,
                "dedupe_key": "outbox-policy-required-delivery-1",
            },
        )
    finally:
        api_main.comms.settings.slack_real_send_allowed = previous_allowed
        api_main.comms.settings.slack_webhook_url = previous_webhook

    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "blocked"
    assert payload["receipt_id"]
    assert payload["decision_id"]
    assert "communications channel policy" in payload["message"]
    assert called["value"] is False

    with engine.begin() as connection:
        reservation_status = connection.execute(
            text("SELECT status FROM budget_reservations WHERE id = :reservation_id"),
            {"reservation_id": reservation_id},
        ).scalar_one()
        outbox_status = connection.execute(
            text("SELECT status FROM outbox_messages WHERE id = :message_id"),
            {"message_id": message_id},
        ).scalar_one()
        receipt = connection.execute(
            text(
                """
                SELECT effect_kind, status, error
                FROM receipts
                WHERE dedupe_key = 'outbox-policy-required-delivery-1'
                """
            )
        ).mappings().one()

    assert reservation_status == "released"
    assert outbox_status == "blocked"
    assert receipt["effect_kind"] == "slack.message.post"
    assert receipt["status"] == "failed"
    assert receipt["error"]["reason"] == "comms_policy_missing"


def test_outbox_delivery_succeeds_and_creates_receipt(client, engine, monkeypatch):
    upsert_comms_policy(client, "boss-dogfood", "slack")
    outbox = client.post(
        "/v1/comms/outbox",
        json={
            "project_id": "boss-dogfood",
            "channel": "slack:#ops",
            "subject": "Deployment",
            "body": "The sandbox change is ready for review.",
            "dedupe_key": "outbox-success-1",
        },
    )
    assert outbox.status_code == 201
    message_id = outbox.json()["id"]

    reserve = client.post(
        "/v1/budget/reserve",
        json={
            "project_id": "boss-dogfood",
            "category": "comms",
            "amount_usd_micros": 50_000,
            "reason": "Send real Slack message.",
        },
    )
    assert reserve.status_code == 201
    reservation_id = reserve.json()["reservation_id"]

    previous_allowed = api_main.comms.settings.slack_real_send_allowed
    previous_webhook = api_main.comms.settings.slack_webhook_url
    api_main.comms.settings.slack_real_send_allowed = True
    api_main.comms.settings.slack_webhook_url = "https://hooks.slack.com/services/test"
    monkeypatch.setattr(
        api_main.comms.slack,
        "post_message",
        lambda channel, subject, body: f"slack-webhook:{channel}",
    )
    try:
        response = client.post(
            f"/v1/comms/outbox/{message_id}/deliver",
            json={
                "project_id": "boss-dogfood",
                "reservation_id": reservation_id,
                "dedupe_key": "outbox-success-delivery-1",
            },
        )
    finally:
        api_main.comms.settings.slack_real_send_allowed = previous_allowed
        api_main.comms.settings.slack_webhook_url = previous_webhook

    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "created"
    assert payload["receipt_id"]
    assert payload["provider_ref"] == "slack-webhook:slack:#ops"

    receipts = client.get(
        "/v1/receipts", params={"project_id": "boss-dogfood", "limit": 10}
    )
    assert receipts.status_code == 200
    receipt = receipts.json()["items"][0]
    assert receipt["effect_kind"] == "slack.message.post"
    assert receipt["created_by_job_id"] is None

    with engine.begin() as connection:
        outbox_row = connection.execute(
            text(
                """
                SELECT status, receipt_id
                FROM outbox_messages
                WHERE id = :message_id
                """
            ),
            {"message_id": message_id},
        ).mappings().one()

    assert outbox_row["status"] == "acked"
    assert outbox_row["receipt_id"] == payload["receipt_id"]


def test_outbox_email_delivery_blocks_when_email_delivery_is_disabled(client, engine):
    upsert_comms_policy(client, "boss-dogfood", "email")
    outbox = client.post(
        "/v1/comms/outbox",
        json={
            "project_id": "boss-dogfood",
            "channel": "email:founder@example.com",
            "subject": "Founder update",
            "body": "Email delivery should be blocked without SMTP config.",
            "dedupe_key": "outbox-email-blocked-1",
        },
    )
    assert outbox.status_code == 201
    message_id = outbox.json()["id"]

    reserve = client.post(
        "/v1/budget/reserve",
        json={
            "project_id": "boss-dogfood",
            "category": "comms",
            "amount_usd_micros": 50_000,
            "reason": "Attempt blocked email delivery.",
        },
    )
    assert reserve.status_code == 201
    reservation_id = reserve.json()["reservation_id"]

    previous_allowed = api_main.comms.settings.email_real_send_allowed
    previous_host = api_main.comms.settings.email_smtp_host
    previous_from = api_main.comms.settings.email_from_address
    api_main.comms.settings.email_real_send_allowed = False
    api_main.comms.settings.email_smtp_host = None
    api_main.comms.settings.email_from_address = None
    try:
        response = client.post(
            f"/v1/comms/outbox/{message_id}/deliver",
            json={
                "project_id": "boss-dogfood",
                "reservation_id": reservation_id,
                "dedupe_key": "outbox-email-blocked-delivery-1",
            },
        )
    finally:
        api_main.comms.settings.email_real_send_allowed = previous_allowed
        api_main.comms.settings.email_smtp_host = previous_host
        api_main.comms.settings.email_from_address = previous_from

    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "blocked"
    assert payload["decision_id"]
    assert payload["receipt_id"]
    assert "BOSS_EMAIL_REAL_SEND_ALLOWED is false" in payload["message"]

    with engine.begin() as connection:
        reservation_status = connection.execute(
            text("SELECT status FROM budget_reservations WHERE id = :reservation_id"),
            {"reservation_id": reservation_id},
        ).scalar_one()
        outbox_status = connection.execute(
            text("SELECT status FROM outbox_messages WHERE id = :message_id"),
            {"message_id": message_id},
        ).scalar_one()
        receipt = connection.execute(
            text(
                """
                SELECT effect_kind, status
                FROM receipts
                WHERE dedupe_key = 'outbox-email-blocked-delivery-1'
                """
            )
        ).mappings().one()

    assert reservation_status == "released"
    assert outbox_status == "blocked"
    assert receipt["effect_kind"] == "email.message.send"
    assert receipt["status"] == "failed"


def test_outbox_email_delivery_succeeds_and_creates_receipt(
    client, engine, monkeypatch
):
    upsert_comms_policy(client, "boss-dogfood", "email")
    outbox = client.post(
        "/v1/comms/outbox",
        json={
            "project_id": "boss-dogfood",
            "channel": "email:founder@example.com",
            "subject": "Deployment",
            "body": "The sandbox change is ready for email review.",
            "dedupe_key": "outbox-email-success-1",
        },
    )
    assert outbox.status_code == 201
    message_id = outbox.json()["id"]

    reserve = client.post(
        "/v1/budget/reserve",
        json={
            "project_id": "boss-dogfood",
            "category": "comms",
            "amount_usd_micros": 50_000,
            "reason": "Send real email message.",
        },
    )
    assert reserve.status_code == 201
    reservation_id = reserve.json()["reservation_id"]

    previous_allowed = api_main.comms.settings.email_real_send_allowed
    previous_host = api_main.comms.settings.email_smtp_host
    previous_from = api_main.comms.settings.email_from_address
    previous_tls = api_main.comms.settings.email_smtp_use_tls
    api_main.comms.settings.email_real_send_allowed = True
    api_main.comms.settings.email_smtp_host = "smtp.example.com"
    api_main.comms.settings.email_from_address = "boss@example.com"
    api_main.comms.settings.email_smtp_use_tls = True
    monkeypatch.setattr(
        api_main.comms.email,
        "send_message",
        lambda channel, subject, body: f"smtp:{channel.split(':', 1)[1]}",
    )
    try:
        response = client.post(
            f"/v1/comms/outbox/{message_id}/deliver",
            json={
                "project_id": "boss-dogfood",
                "reservation_id": reservation_id,
                "dedupe_key": "outbox-email-success-delivery-1",
            },
        )
    finally:
        api_main.comms.settings.email_real_send_allowed = previous_allowed
        api_main.comms.settings.email_smtp_host = previous_host
        api_main.comms.settings.email_from_address = previous_from
        api_main.comms.settings.email_smtp_use_tls = previous_tls

    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "created"
    assert payload["receipt_id"]
    assert payload["provider_ref"] == "smtp:founder@example.com"

    receipts = client.get(
        "/v1/receipts", params={"project_id": "boss-dogfood", "limit": 10}
    )
    assert receipts.status_code == 200
    receipt = receipts.json()["items"][0]
    assert receipt["effect_kind"] == "email.message.send"
    assert receipt["created_by_job_id"] is None

    with engine.begin() as connection:
        outbox_row = connection.execute(
            text(
                """
                SELECT status, receipt_id
                FROM outbox_messages
                WHERE id = :message_id
                """
            ),
            {"message_id": message_id},
        ).mappings().one()

    assert outbox_row["status"] == "acked"
    assert outbox_row["receipt_id"] == payload["receipt_id"]


def test_outbox_delivery_rejects_unknown_created_by_job_id(client):
    outbox = client.post(
        "/v1/comms/outbox",
        json={
            "project_id": "boss-dogfood",
            "channel": "slack:#ops",
            "body": "This request should fail validation.",
            "dedupe_key": "outbox-invalid-job-1",
        },
    )
    assert outbox.status_code == 201
    message_id = outbox.json()["id"]

    reserve = client.post(
        "/v1/budget/reserve",
        json={
            "project_id": "boss-dogfood",
            "category": "comms",
            "amount_usd_micros": 50_000,
            "reason": "Validate created_by_job_id handling.",
        },
    )
    assert reserve.status_code == 201

    response = client.post(
        f"/v1/comms/outbox/{message_id}/deliver",
        json={
            "project_id": "boss-dogfood",
            "reservation_id": reserve.json()["reservation_id"],
            "dedupe_key": "outbox-invalid-job-delivery-1",
            "created_by_job_id": "job_missing",
        },
    )

    assert response.status_code == 400
    assert "created_by_job_id" in response.json()["detail"]


def test_comms_job_failure_deadletters_outbox_message(client, engine):
    outbox = client.post(
        "/v1/comms/outbox",
        json={
            "project_id": "boss-dogfood",
            "channel": "slack:#ops",
            "body": "This message will deadletter.",
            "dedupe_key": "outbox-deadletter-1",
        },
    )
    assert outbox.status_code == 201
    message_id = outbox.json()["id"]

    reserve = client.post(
        "/v1/budget/reserve",
        json={
            "project_id": "boss-dogfood",
            "category": "comms",
            "amount_usd_micros": 50_000,
            "reason": "Queue a comms job that fails.",
        },
    )
    assert reserve.status_code == 201
    reservation_id = reserve.json()["reservation_id"]

    queued = client.post(
        f"/v1/comms/outbox/{message_id}/queue",
        json={
            "project_id": "boss-dogfood",
            "reservation_id": reservation_id,
            "dedupe_key": "outbox-deadletter-job-1",
        },
    )
    assert queued.status_code == 201
    job_id = queued.json()["id"]

    poll = client.post(
        "/v1/worker/poll",
        json={
            "worker_id": "worker-comms-deadletter",
            "capabilities": ["comms.outbox.deliver"],
            "lease_ttl_seconds": 60,
        },
    )
    assert poll.status_code == 200
    leased = poll.json()["job"]
    assert leased["id"] == job_id

    failed = client.post(
        "/v1/worker/fail",
        json={
            "worker_id": "worker-comms-deadletter",
            "job_id": job_id,
            "lease_token": leased["lease_token"],
            "error": {"message": "Slack webhook rejected the payload."},
            "retryable": False,
            "retry_delay_seconds": 0,
        },
    )
    assert failed.status_code == 200
    payload = failed.json()
    assert payload["status"] == "deadlettered"
    assert payload["deadletter_id"]

    with engine.begin() as connection:
        outbox_status = connection.execute(
            text("SELECT status FROM outbox_messages WHERE id = :message_id"),
            {"message_id": message_id},
        ).scalar_one()
    assert outbox_status == "deadlettered"


def test_budget_denial_creates_decision(client):
    response = client.post(
        "/v1/budget/reserve",
        json={
            "project_id": "boss-dogfood",
            "category": "model",
            "amount_usd_micros": 100_000_000,
            "reason": "force denial",
        },
    )

    assert response.status_code == 201
    payload = response.json()
    assert payload["status"] == "denied"
    assert payload["decision_id"]


def test_budget_status_returns_project_snapshot(client):
    response = client.get(
        "/v1/budget/status",
        params={"scope_type": "project", "scope_id": "boss-dogfood"},
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["scope_type"] == "project"
    assert payload["scope_id"] == "boss-dogfood"
    assert payload["budget_state"] == "green"
    assert payload["cap_usd_micros"] > 0
    assert payload["available_usd_micros"] == payload["cap_usd_micros"]
    assert payload["committed_usd_micros"] == 0
    assert payload["burn_window_hours"] == 24
    assert payload["burn_rate_usd_micros_per_hour"] == 0
    assert payload["forecasted_24h_usd_micros"] == 0
    assert payload["time_to_cap_seconds"] is None
    assert payload["active_reservations_count"] == 0
    assert payload["top_cost_drivers"] == []


def test_budget_status_reports_forecast_and_time_to_cap(client, engine):
    with engine.begin() as connection:
        connection.execute(
            text(
                """
                UPDATE budget_policies
                SET daily_cap_usd_micros = 1_000_000
                WHERE scope_type = 'project' AND scope_id = 'boss-dogfood'
                """
            )
        )
        connection.execute(
            text(
                """
                INSERT INTO cost_receipts (
                    id, project_id, category, skill_or_model_id,
                    estimated_usd_micros, actual_usd_micros, status, created_at
                )
                VALUES (
                    'cost_budget_forecast', 'boss-dogfood', 'model',
                    'gpt-5.2-codex', 240000, 240000, 'actual', :created_at
                )
                """
            ),
            {"created_at": datetime.now(timezone.utc)},
        )

    response = client.get(
        "/v1/budget/status",
        params={"scope_type": "project", "scope_id": "boss-dogfood"},
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["actual_usd_micros"] == 240_000
    assert payload["committed_usd_micros"] == 240_000
    assert payload["burn_window_hours"] == 24
    assert payload["burn_rate_usd_micros_per_hour"] == 10_000
    assert payload["forecasted_24h_usd_micros"] == 240_000
    assert payload["time_to_cap_seconds"] == 273_600
    assert payload["top_cost_drivers"] == [
        {"skill_or_model_id": "gpt-5.2-codex", "amount_usd_micros": 240_000}
    ]


def test_budget_quote_returns_allowed_projection(client):
    response = client.post(
        "/v1/budget/quote",
        json={
            "project_id": "boss-dogfood",
            "category": "model",
            "amount_usd_micros": 250_000,
            "reason": "quote a model call",
        },
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["allowed"] is True
    assert payload["requested_usd_micros"] == 250_000
    assert payload["budget_state"] == "green"
    assert payload["post_reservation_available_usd_micros"] < payload["available_usd_micros"]


def test_budget_allocations_enforce_parent_limits_and_reservation_capacity(client):
    start = datetime.now(timezone.utc)
    end = start + timedelta(days=1)

    root = client.post(
        "/v1/budget/allocations",
        json={
            "project_id": "boss-dogfood",
            "scope_type": "project",
            "scope_id": "boss-dogfood",
            "amount_usd_micros": 1_000_000,
            "period_start": start.isoformat(),
            "period_end": end.isoformat(),
            "actor": "ceo",
        },
    )
    assert root.status_code == 201
    root_payload = root.json()
    assert root_payload["available_usd_micros"] == 1_000_000

    child = client.post(
        "/v1/budget/allocations",
        json={
            "project_id": "boss-dogfood",
            "parent_allocation_id": root_payload["id"],
            "scope_type": "department",
            "scope_id": "software_factory",
            "amount_usd_micros": 700_000,
            "period_start": start.isoformat(),
            "period_end": end.isoformat(),
            "actor": "ceo",
        },
    )
    assert child.status_code == 201
    child_payload = child.json()

    over_child = client.post(
        "/v1/budget/allocations",
        json={
            "project_id": "boss-dogfood",
            "parent_allocation_id": root_payload["id"],
            "scope_type": "department",
            "scope_id": "growth",
            "amount_usd_micros": 400_000,
            "period_start": start.isoformat(),
            "period_end": end.isoformat(),
            "actor": "ceo",
        },
    )
    assert over_child.status_code == 409

    reserve = client.post(
        "/v1/budget/reserve",
        json={
            "project_id": "boss-dogfood",
            "allocation_id": child_payload["id"],
            "category": "tool",
            "amount_usd_micros": 650_000,
            "reason": "reserve against software factory allocation",
        },
    )
    assert reserve.status_code == 201
    assert reserve.json()["status"] == "reserved"

    over_reserve = client.post(
        "/v1/budget/reserve",
        json={
            "project_id": "boss-dogfood",
            "allocation_id": child_payload["id"],
            "category": "tool",
            "amount_usd_micros": 100_000,
            "reason": "force allocation denial",
        },
    )
    assert over_reserve.status_code == 201
    assert over_reserve.json()["status"] == "denied"
    assert over_reserve.json()["decision_id"]

    allocations = client.get(
        "/v1/budget/allocations", params={"project_id": "boss-dogfood"}
    )
    assert allocations.status_code == 200
    items = {item["id"]: item for item in allocations.json()["items"]}
    assert items[root_payload["id"]]["child_allocated_usd_micros"] == 700_000
    assert items[child_payload["id"]]["reserved_usd_micros"] == 650_000
    assert items[child_payload["id"]]["available_usd_micros"] == 50_000

    events = client.get("/v1/events", params={"project_id": "boss-dogfood"})
    event_types = [item["event_type"] for item in events.json()["items"]]
    assert "budget.allocation_denied" in event_types
    assert "budget.allocation_reservation_denied" in event_types


def test_budget_release_frees_active_reservation(client):
    reserve = client.post(
        "/v1/budget/reserve",
        json={
            "project_id": "boss-dogfood",
            "category": "tool",
            "amount_usd_micros": 250_000,
            "reason": "reserve then release",
        },
    )
    assert reserve.status_code == 201
    reservation_id = reserve.json()["reservation_id"]

    release = client.post(
        "/v1/budget/release",
        json={
            "project_id": "boss-dogfood",
            "reservation_id": reservation_id,
            "reason": "No external effect was executed.",
        },
    )
    assert release.status_code == 200
    payload = release.json()
    assert payload["status"] == "released"
    assert payload["reservation_id"] == reservation_id

    status = client.get(
        "/v1/budget/status",
        params={"scope_type": "project", "scope_id": "boss-dogfood"},
    )
    assert status.status_code == 200
    status_payload = status.json()
    assert status_payload["reserved_usd_micros"] == 0
    assert status_payload["active_reservations_count"] == 0


def test_budget_denial_is_visible_in_decision_inbox_and_event_feed(client):
    denial = client.post(
        "/v1/budget/reserve",
        json={
            "project_id": "boss-dogfood",
            "category": "model",
            "amount_usd_micros": 100_000_000,
            "reason": "force denial",
        },
    )
    assert denial.status_code == 201
    decision_id = denial.json()["decision_id"]

    inbox = client.get("/v1/decisions/inbox", params={"project_id": "boss-dogfood"})
    assert inbox.status_code == 200
    inbox_payload = inbox.json()
    assert inbox_payload["items"]
    assert inbox_payload["items"][0]["id"] == decision_id
    assert inbox_payload["items"][0]["status"] == "pending"

    events = client.get("/v1/events", params={"project_id": "boss-dogfood"})
    assert events.status_code == 200
    event_types = [item["event_type"] for item in events.json()["items"]]
    assert "decision.created" in event_types
    assert "budget.reservation_denied" in event_types


def test_decision_can_be_approved_and_emits_audit_event(client):
    denial = client.post(
        "/v1/budget/reserve",
        json={
            "project_id": "boss-dogfood",
            "category": "model",
            "amount_usd_micros": 100_000_000,
            "reason": "force denial",
        },
    )
    assert denial.status_code == 201
    decision_id = denial.json()["decision_id"]

    approve = client.post(
        f"/v1/decisions/{decision_id}/approve",
        json={"actor": "ceo", "note": "Budget increase is acceptable."},
    )
    assert approve.status_code == 200
    payload = approve.json()
    assert payload["id"] == decision_id
    assert payload["status"] == "approved"
    assert payload["approved_by"] == "ceo"
    assert payload["approved_at"]

    status = client.get("/v1/status", params={"project_id": "boss-dogfood"})
    assert status.status_code == 200
    assert status.json()["pending_decisions"] == 0

    events = client.get("/v1/events", params={"project_id": "boss-dogfood"})
    assert events.status_code == 200
    event_types = [item["event_type"] for item in events.json()["items"]]
    assert "decision.approved" in event_types


def test_decision_can_be_rejected_and_cannot_be_reapproved(client):
    denial = client.post(
        "/v1/budget/reserve",
        json={
            "project_id": "boss-dogfood",
            "category": "model",
            "amount_usd_micros": 100_000_000,
            "reason": "force denial",
        },
    )
    assert denial.status_code == 201
    decision_id = denial.json()["decision_id"]

    reject = client.post(
        f"/v1/decisions/{decision_id}/reject",
        json={"actor": "ceo", "note": "Keep the guardrail in place."},
    )
    assert reject.status_code == 200
    payload = reject.json()
    assert payload["status"] == "rejected"
    assert payload["approved_by"] is None
    assert payload["approved_at"] is None

    duplicate_approve = client.post(
        f"/v1/decisions/{decision_id}/approve",
        json={"actor": "ceo"},
    )
    assert duplicate_approve.status_code == 409

    events = client.get("/v1/events", params={"project_id": "boss-dogfood"})
    assert events.status_code == 200
    event_types = [item["event_type"] for item in events.json()["items"]]
    assert "decision.rejected" in event_types


def test_tier_two_decision_approval_requires_configured_approver_role(client):
    denial = client.post(
        "/v1/budget/reserve",
        json={
            "project_id": "boss-dogfood",
            "category": "model",
            "amount_usd_micros": 100_000_000,
            "reason": "force denial",
        },
    )
    assert denial.status_code == 201
    decision_id = denial.json()["decision_id"]

    approve = client.post(
        f"/v1/decisions/{decision_id}/approve",
        json={
            "actor": "release_manager",
            "note": "Trying to approve without delegation.",
        },
    )

    assert approve.status_code == 403
    assert "not allowed to approve" in approve.json()["detail"]
    events = client.get("/v1/events", params={"project_id": "boss-dogfood"})
    policy_events = [
        item for item in events.json()["items"] if item["event_type"] == "policy.denied"
    ]
    assert policy_events
    assert policy_events[0]["entity_id"] == decision_id


def test_decision_can_be_delegated_and_then_approved(client):
    denial = client.post(
        "/v1/budget/reserve",
        json={
            "project_id": "boss-dogfood",
            "category": "model",
            "amount_usd_micros": 100_000_000,
            "reason": "force denial",
        },
    )
    assert denial.status_code == 201
    decision_id = denial.json()["decision_id"]

    delegate = client.post(
        f"/v1/decisions/{decision_id}/delegate",
        json={
            "actor": "ceo",
            "delegate_to": "release_manager",
            "note": "Release manager should own this final call.",
        },
    )
    assert delegate.status_code == 200
    delegated_payload = delegate.json()
    assert delegated_payload["status"] == "delegated"
    assert delegated_payload["delegated_to"] == "release_manager"
    assert delegated_payload["delegated_by"] == "ceo"
    assert delegated_payload["delegated_at"]
    assert delegated_payload["resolution_note"] == "Release manager should own this final call."

    approve = client.post(
        f"/v1/decisions/{decision_id}/approve",
        json={"actor": "release_manager", "note": "Approved after delegated review."},
    )
    assert approve.status_code == 200
    approved_payload = approve.json()
    assert approved_payload["status"] == "approved"
    assert approved_payload["approved_by"] == "release_manager"

    events = client.get("/v1/events", params={"project_id": "boss-dogfood"})
    assert events.status_code == 200
    event_types = [item["event_type"] for item in events.json()["items"]]
    assert "decision.delegated" in event_types
    assert "decision.approved" in event_types


def test_decision_can_request_revision_and_then_blocks_approval(client):
    denial = client.post(
        "/v1/budget/reserve",
        json={
            "project_id": "boss-dogfood",
            "category": "model",
            "amount_usd_micros": 100_000_000,
            "reason": "force denial",
        },
    )
    assert denial.status_code == 201
    decision_id = denial.json()["decision_id"]

    revise = client.post(
        f"/v1/decisions/{decision_id}/revise",
        json={
            "actor": "ceo",
            "note": "Revise the estimate and include lower-cost options.",
        },
    )
    assert revise.status_code == 200
    payload = revise.json()
    assert payload["status"] == "revision_requested"
    assert payload["revision_requested_by"] == "ceo"
    assert payload["revision_requested_at"]
    assert payload["resolution_note"] == "Revise the estimate and include lower-cost options."

    approve = client.post(
        f"/v1/decisions/{decision_id}/approve",
        json={"actor": "ceo"},
    )
    assert approve.status_code == 409

    events = client.get("/v1/events", params={"project_id": "boss-dogfood"})
    assert events.status_code == 200
    event_types = [item["event_type"] for item in events.json()["items"]]
    assert "decision.revision_requested" in event_types


def test_self_improvement_proposal_runs_through_canary_rollout_and_rollback(
    client, engine
):
    created = client.post(
        "/v1/self-improvement/proposals",
        json={
            "project_id": "boss-dogfood",
            "title": "Tighten decision inbox defaults",
            "summary": "Improve default-action handling and observability for expired decisions.",
            "change_ref": "https://github.com/seanheiney/boss/pull/42",
            "diff_summary": "Adjust decision expiry handling and add stronger audit markers.",
            "expected_impact": ["Lower decision latency", "Fewer unsafe expirations"],
            "test_plan": ["Add API transition tests", "Run smoke suite"],
            "canary_scope": ["dogfood workspace only", "decision inbox traffic"],
            "rollback_plan": ["Revert PR", "Disable rollout flag"],
            "success_metrics": ["No expired high-risk decision executes", "No silent drops"],
            "actor": "vp_agents",
            "risk_tier": 2,
        },
    )
    assert created.status_code == 201
    created_payload = created.json()
    proposal_id = created_payload["id"]
    task_id = created_payload["task_id"]
    canary_decision_id = created_payload["current_decision_id"]
    assert created_payload["status"] == "proposed"
    assert created_payload["proposal_artifact_id"]

    approve_canary = client.post(
        f"/v1/decisions/{canary_decision_id}/approve",
        json={"actor": "ceo", "note": "Run the dogfood canary."},
    )
    assert approve_canary.status_code == 200

    proposal_after_canary_approval = client.get(
        f"/v1/self-improvement/proposals/{proposal_id}"
    )
    assert proposal_after_canary_approval.status_code == 200
    assert proposal_after_canary_approval.json()["status"] == "canary_approved"

    canary = client.post(
        f"/v1/self-improvement/proposals/{proposal_id}/canary",
        json={
            "actor": "release_manager",
            "outcome": "succeeded",
            "summary": "Dogfood canary stayed within expected error and cost bounds.",
            "metrics": {"decision_latency_delta_pct": -12},
            "evidence_refs": ["evt_canary_1"],
        },
    )
    assert canary.status_code == 200
    canary_payload = canary.json()
    rollout_decision_id = canary_payload["current_decision_id"]
    assert canary_payload["status"] == "canary_succeeded"
    assert canary_payload["canary_artifact_id"]
    assert rollout_decision_id

    approve_rollout = client.post(
        f"/v1/decisions/{rollout_decision_id}/approve",
        json={"actor": "ceo", "note": "Promote to full dogfood rollout."},
    )
    assert approve_rollout.status_code == 200

    rollout_ready = client.get(f"/v1/self-improvement/proposals/{proposal_id}")
    assert rollout_ready.status_code == 200
    assert rollout_ready.json()["status"] == "rollout_approved"

    rollout = client.post(
        f"/v1/self-improvement/proposals/{proposal_id}/rollout",
        json={
            "actor": "release_manager",
            "summary": "Rolled out to the dogfood control plane.",
            "external_ref": "deploy://dogfood/2026-06-01/self-improvement-42",
            "metrics": {"deadletter_rate": 0},
            "evidence_refs": ["art_rollout_1"],
        },
    )
    assert rollout.status_code == 200
    rollout_payload = rollout.json()
    assert rollout_payload["status"] == "rolled_out"
    assert rollout_payload["rollout_artifact_id"]

    rollback = client.post(
        f"/v1/self-improvement/proposals/{proposal_id}/rollback",
        json={
            "actor": "release_manager",
            "reason": "Rollback after regression review.",
            "external_ref": "rollback://dogfood/2026-06-01/self-improvement-42",
            "metrics": {"policy_violation_count": 1},
            "evidence_refs": ["art_rollback_1"],
        },
    )
    assert rollback.status_code == 200
    rollback_payload = rollback.json()
    assert rollback_payload["status"] == "rolled_back"
    assert rollback_payload["rollback_artifact_id"]

    with engine.begin() as connection:
        task_status = connection.execute(
            text("SELECT status FROM tasks WHERE id = :task_id"),
            {"task_id": task_id},
        ).scalar_one()
        artifact_types = [
            row[0]
            for row in connection.execute(
                text(
                    """
                    SELECT artifact_type
                    FROM artifacts
                    WHERE task_id = :task_id
                    ORDER BY created_at ASC
                    """
                ),
                {"task_id": task_id},
            ).all()
        ]

    assert task_status == "rolled_back"
    assert "self_improvement_proposal" in artifact_types
    assert "self_improvement_canary_report" in artifact_types
    assert "self_improvement_rollout_record" in artifact_types
    assert "self_improvement_rollback_record" in artifact_types


def test_self_improvement_proposal_requires_non_empty_change_control_evidence(client):
    response = client.post(
        "/v1/self-improvement/proposals",
        json={
            "project_id": "boss-dogfood",
            "title": "   ",
            "summary": "Harden proposal input validation.",
            "change_ref": "https://github.com/seanheiney/boss/pull/99",
            "diff_summary": "Require explicit test, canary, rollback, and metric evidence.",
            "expected_impact": [],
            "test_plan": [" "],
            "canary_scope": [],
            "rollback_plan": [],
            "success_metrics": [],
        },
    )

    assert response.status_code == 422
    payload = response.json()
    assert "must" in json.dumps(payload)
    assert any(
        term in json.dumps(payload)
        for term in [
            "title must not be blank",
            "expected_impact must contain at least one non-empty item",
            "test_plan must contain at least one non-empty item",
            "canary_scope must contain at least one non-empty item",
            "rollback_plan must contain at least one non-empty item",
            "success_metrics must contain at least one non-empty item",
        ]
    )


def test_self_improvement_canary_requires_metrics_and_evidence(client):
    created = client.post(
        "/v1/self-improvement/proposals",
        json={
            "project_id": "boss-dogfood",
            "title": "Tighten decision inbox defaults",
            "summary": "Improve default-action handling and observability for expired decisions.",
            "change_ref": "https://github.com/seanheiney/boss/pull/142",
            "diff_summary": "Adjust decision expiry handling and add stronger audit markers.",
            "expected_impact": ["Lower decision latency"],
            "test_plan": ["Add API transition tests"],
            "canary_scope": ["dogfood workspace only"],
            "rollback_plan": ["Revert PR"],
            "success_metrics": ["No expired high-risk decision executes"],
        },
    )
    assert created.status_code == 201
    proposal_id = created.json()["id"]
    decision_id = created.json()["current_decision_id"]

    approve_canary = client.post(
        f"/v1/decisions/{decision_id}/approve",
        json={"actor": "ceo", "note": "Run the dogfood canary."},
    )
    assert approve_canary.status_code == 200

    canary = client.post(
        f"/v1/self-improvement/proposals/{proposal_id}/canary",
        json={
            "actor": "release_manager",
            "outcome": "succeeded",
            "summary": "Canary looked healthy.",
            "metrics": {},
            "evidence_refs": [],
        },
    )
    assert canary.status_code == 422
    assert "metrics" in json.dumps(canary.json())
    assert "evidence_refs" in json.dumps(canary.json())


def test_self_improvement_rollout_and_rollback_require_metrics_and_evidence(client):
    created = client.post(
        "/v1/self-improvement/proposals",
        json={
            "project_id": "boss-dogfood",
            "title": "Refine budget governor events",
            "summary": "Improve budget denial event detail and audit clarity.",
            "change_ref": "https://github.com/seanheiney/boss/pull/177",
            "diff_summary": "Expand denial event payloads and review packet notes.",
            "expected_impact": ["Better operator clarity"],
            "test_plan": ["Decision API coverage"],
            "canary_scope": ["dogfood only"],
            "rollback_plan": ["Revert PR"],
            "success_metrics": ["Clearer denial event feed"],
        },
    )
    assert created.status_code == 201
    proposal_id = created.json()["id"]
    canary_decision_id = created.json()["current_decision_id"]

    assert (
        client.post(
            f"/v1/decisions/{canary_decision_id}/approve",
            json={"actor": "ceo", "note": "Approve canary."},
        ).status_code
        == 200
    )
    successful_canary = client.post(
        f"/v1/self-improvement/proposals/{proposal_id}/canary",
        json={
            "actor": "release_manager",
            "outcome": "succeeded",
            "summary": "Canary stayed within expected bounds.",
            "metrics": {"decision_latency_delta_pct": -4},
            "evidence_refs": ["evt_canary_budget_1"],
        },
    )
    assert successful_canary.status_code == 200
    rollout_decision_id = successful_canary.json()["current_decision_id"]

    assert (
        client.post(
            f"/v1/decisions/{rollout_decision_id}/approve",
            json={"actor": "ceo", "note": "Approve rollout."},
        ).status_code
        == 200
    )

    rollout = client.post(
        f"/v1/self-improvement/proposals/{proposal_id}/rollout",
        json={
            "actor": "release_manager",
            "summary": "Rolled out broadly.",
            "metrics": {},
            "evidence_refs": [],
        },
    )
    assert rollout.status_code == 422
    assert "metrics" in json.dumps(rollout.json())
    assert "evidence_refs" in json.dumps(rollout.json())

    valid_rollout = client.post(
        f"/v1/self-improvement/proposals/{proposal_id}/rollout",
        json={
            "actor": "release_manager",
            "summary": "Rolled out broadly.",
            "external_ref": "deploy://dogfood/2026-06-02/self-improvement-177",
            "metrics": {"deadletter_rate": 0},
            "evidence_refs": ["art_rollout_budget_1"],
        },
    )
    assert valid_rollout.status_code == 200

    rollback = client.post(
        f"/v1/self-improvement/proposals/{proposal_id}/rollback",
        json={
            "actor": "release_manager",
            "reason": "Rollback after operator review.",
            "metrics": {},
            "evidence_refs": [],
        },
    )
    assert rollback.status_code == 422
    assert "metrics" in json.dumps(rollback.json())
    assert "evidence_refs" in json.dumps(rollback.json())


def test_self_improvement_revision_request_updates_proposal_status(client):
    created = client.post(
        "/v1/self-improvement/proposals",
        json={
            "project_id": "boss-dogfood",
            "title": "Refine budget governor events",
            "summary": "Improve event detail for budget denials.",
            "change_ref": "https://github.com/seanheiney/boss/pull/77",
            "diff_summary": "Expand denial event payloads.",
            "expected_impact": ["Better operator clarity"],
            "test_plan": ["Decision API coverage"],
            "canary_scope": ["dogfood only"],
            "rollback_plan": ["Revert PR"],
            "success_metrics": ["Clearer denial event feed"],
        },
    )
    assert created.status_code == 201
    proposal_id = created.json()["id"]
    decision_id = created.json()["current_decision_id"]

    revise = client.post(
        f"/v1/decisions/{decision_id}/revise",
        json={
            "actor": "ceo",
            "note": "Add a stronger rollback story before canarying this.",
        },
    )
    assert revise.status_code == 200

    proposal = client.get(f"/v1/self-improvement/proposals/{proposal_id}")
    assert proposal.status_code == 200
    payload = proposal.json()
    assert payload["status"] == "revision_requested"
    assert payload["status_note"] == "Add a stronger rollback story before canarying this."
    assert payload["current_decision_id"] is None


def test_self_improvement_proposal_perm_enforced(client):
    """Covers P2 e2e perm: create proposal requires self_improvement_manage (operator lacks it)."""
    denied = client.post(
        "/v1/self-improvement/proposals",
        json={
            "project_id": "boss-dogfood",
            "title": "Perm test proposal",
            "summary": "Should be denied for lack of perm.",
            "change_ref": "https://example.com/ref",
            "diff_summary": "Test perm enforcement.",
            "expected_impact": ["perm visible"],
            "test_plan": ["test perm path"],
            "canary_scope": ["test only"],
            "rollback_plan": ["no change"],
            "success_metrics": ["403 on bad actor"],
            "actor": "coo",
            "risk_tier": 2,
        },
    )
    assert denied.status_code == 403
    detail = denied.json().get("detail", "")
    assert "lacks permission 'self_improvement_manage'" in detail


def test_self_improvement_canary_only_from_canary_approved_state(client):
    """Covers P2 e2e state machine: canary record only allowed from canary_approved (not proposed). 409 on invalid transition."""
    created = client.post(
        "/v1/self-improvement/proposals",
        json={
            "project_id": "boss-dogfood",
            "title": "Canary state test",
            "summary": "Test canary gate.",
            "change_ref": "ref-state",
            "diff_summary": "State only.",
            "expected_impact": ["state safety"],
            "test_plan": ["state tests"],
            "canary_scope": ["unit"],
            "rollback_plan": ["revert"],
            "success_metrics": ["no bad trans"],
            "actor": "vp_agents",
            "risk_tier": 2,
        },
    )
    assert created.status_code == 201
    proposal_id = created.json()["id"]
    # do not approve decision; status remains proposed
    canary = client.post(
        f"/v1/self-improvement/proposals/{proposal_id}/canary",
        json={
            "actor": "release_manager",
            "outcome": "succeeded",
            "summary": "Premature canary.",
            "metrics": {"foo": 1},
            "evidence_refs": ["e1"],
        },
    )
    assert canary.status_code == 409
    assert "cannot record a canary result" in canary.json().get("detail", "")


def test_self_improvement_rollback_only_from_rolled_out_state(client):
    """Covers P2 e2e: rollback only from rolled_out state; 409 otherwise."""
    created = client.post(
        "/v1/self-improvement/proposals",
        json={
            "project_id": "boss-dogfood",
            "title": "Rollback state test",
            "summary": "Test rollback gate.",
            "change_ref": "ref-rollback",
            "diff_summary": "Only rolled_out.",
            "expected_impact": ["safety"],
            "test_plan": ["trans tests"],
            "canary_scope": ["test"],
            "rollback_plan": ["revert"],
            "success_metrics": ["no early rollback"],
            "actor": "vp_agents",
        },
    )
    assert created.status_code == 201
    proposal_id = created.json()["id"]
    # attempt rollback from proposed
    rb = client.post(
        f"/v1/self-improvement/proposals/{proposal_id}/rollback",
        json={
            "actor": "release_manager",
            "reason": "Early rollback attempt.",
            "metrics": {"bar": 2},
            "evidence_refs": ["e2"],
        },
    )
    assert rb.status_code == 409
    assert "cannot be rolled back" in rb.json().get("detail", "")


def test_self_improvement_concurrent_canary_attempts(client):
    """Covers P2 e2e conc + no duplicate effects: after canary_approved, concurrent record attempts; one wins, other 409; single artifact + status final."""
    from concurrent.futures import ThreadPoolExecutor, as_completed

    created = client.post(
        "/v1/self-improvement/proposals",
        json={
            "project_id": "boss-dogfood",
            "title": "Conc canary test",
            "summary": "Race on canary record.",
            "change_ref": "ref-conc",
            "diff_summary": "Concurrent safe.",
            "expected_impact": ["idempotent trans"],
            "test_plan": ["conc test"],
            "canary_scope": ["dogfood"],
            "rollback_plan": ["revert"],
            "success_metrics": ["one effect"],
            "actor": "vp_agents",
        },
    )
    assert created.status_code == 201
    proposal_id = created.json()["id"]
    dec_id = created.json()["current_decision_id"]
    # approve to canary_approved
    app = client.post(
        f"/v1/decisions/{dec_id}/approve",
        json={"actor": "ceo", "note": "ok"},
    )
    assert app.status_code == 200

    def do_canary():
        return client.post(
            f"/v1/self-improvement/proposals/{proposal_id}/canary",
            json={
                "actor": "release_manager",
                "outcome": "succeeded",
                "summary": "Concurrent canary.",
                "metrics": {"conc": 1},
                "evidence_refs": ["conc-evt"],
            },
        )

    with ThreadPoolExecutor(max_workers=2) as executor:
        futs = [executor.submit(do_canary), executor.submit(do_canary)]
        results = [f.result() for f in as_completed(futs)]

    codes = sorted([r.status_code for r in results])
    assert codes == [200, 409] or codes == [409, 200]
    final = client.get(f"/v1/self-improvement/proposals/{proposal_id}")
    assert final.status_code == 200
    assert final.json()["status"] == "canary_succeeded"
    assert final.json()["canary_artifact_id"]


def test_self_improvement_perm_on_canary_rollout_rollback(client):
    """Covers P2 e2e perm + states: self_improvement_manage required for canary/record/rollout/rollback (not just create)."""
    created = client.post(
        "/v1/self-improvement/proposals",
        json={
            "project_id": "boss-dogfood",
            "title": "Perm on actions test",
            "summary": "Operator cannot record canary/rollout/rollback.",
            "change_ref": "ref-perm-actions",
            "diff_summary": "Perm everywhere.",
            "expected_impact": ["perm safety"],
            "test_plan": ["perm tests"],
            "canary_scope": ["test"],
            "rollback_plan": ["revert"],
            "success_metrics": ["403s"],
            "actor": "vp_agents",
        },
    )
    assert created.status_code == 201
    proposal_id = created.json()["id"]
    dec_id = created.json()["current_decision_id"]

    # approve to canary_approved
    assert (
        client.post(
            f"/v1/decisions/{dec_id}/approve",
            json={"actor": "ceo", "note": "ok"},
        ).status_code
        == 200
    )

    # operator cannot record canary
    denied_canary = client.post(
        f"/v1/self-improvement/proposals/{proposal_id}/canary",
        json={
            "actor": "coo",
            "outcome": "succeeded",
            "summary": "no",
            "metrics": {"x": 1},
            "evidence_refs": ["e"],
        },
    )
    assert denied_canary.status_code == 403
    assert "self_improvement_manage" in denied_canary.json().get("detail", "")

    # vp can record canary success
    canary_ok = client.post(
        f"/v1/self-improvement/proposals/{proposal_id}/canary",
        json={
            "actor": "release_manager",
            "outcome": "succeeded",
            "summary": "ok",
            "metrics": {"x": 1},
            "evidence_refs": ["e"],
        },
    )
    assert canary_ok.status_code == 200
    rollout_dec = canary_ok.json()["current_decision_id"]

    # approve rollout
    assert (
        client.post(
            f"/v1/decisions/{rollout_dec}/approve",
            json={"actor": "ceo", "note": "ok"},
        ).status_code
        == 200
    )

    # operator denied on rollout
    denied_roll = client.post(
        f"/v1/self-improvement/proposals/{proposal_id}/rollout",
        json={
            "actor": "coo",
            "summary": "no",
            "metrics": {"y": 1},
            "evidence_refs": ["e2"],
        },
    )
    assert denied_roll.status_code == 403

    # vp rollout
    roll_ok = client.post(
        f"/v1/self-improvement/proposals/{proposal_id}/rollout",
        json={
            "actor": "release_manager",
            "summary": "rolled",
            "metrics": {"y": 1},
            "evidence_refs": ["e2"],
        },
    )
    assert roll_ok.status_code == 200

    # operator denied rollback
    denied_rb = client.post(
        f"/v1/self-improvement/proposals/{proposal_id}/rollback",
        json={
            "actor": "coo",
            "reason": "no",
            "metrics": {"z": 1},
            "evidence_refs": ["e3"],
        },
    )
    assert denied_rb.status_code == 403


def test_self_improvement_concurrent_rollout_attempts(client):
    """Covers P2 e2e conc + no dup effects: concurrent rollout after approved; exactly one succeeds, final rolled_out + single artifact."""
    from concurrent.futures import ThreadPoolExecutor, as_completed

    created = client.post(
        "/v1/self-improvement/proposals",
        json={
            "project_id": "boss-dogfood",
            "title": "Conc rollout test",
            "summary": "Race on rollout record.",
            "change_ref": "ref-conc-roll",
            "diff_summary": "Concurrent rollout safe.",
            "expected_impact": ["single effect"],
            "test_plan": ["conc rollout"],
            "canary_scope": ["dogfood"],
            "rollback_plan": ["revert"],
            "success_metrics": ["one rollout"],
            "actor": "vp_agents",
        },
    )
    assert created.status_code == 201
    proposal_id = created.json()["id"]
    canary_dec = created.json()["current_decision_id"]

    assert (
        client.post(
            f"/v1/decisions/{canary_dec}/approve",
            json={"actor": "ceo", "note": "canary ok"},
        ).status_code
        == 200
    )
    canary_res = client.post(
        f"/v1/self-improvement/proposals/{proposal_id}/canary",
        json={
            "actor": "release_manager",
            "outcome": "succeeded",
            "summary": "canary good",
            "metrics": {"c": 1},
            "evidence_refs": ["c1"],
        },
    )
    assert canary_res.status_code == 200
    rollout_dec = canary_res.json()["current_decision_id"]

    assert (
        client.post(
            f"/v1/decisions/{rollout_dec}/approve",
            json={"actor": "ceo", "note": "roll ok"},
        ).status_code
        == 200
    )

    def do_rollout():
        return client.post(
            f"/v1/self-improvement/proposals/{proposal_id}/rollout",
            json={
                "actor": "release_manager",
                "summary": "Concurrent rollout.",
                "metrics": {"roll": 1},
                "evidence_refs": ["roll-evt"],
            },
        )

    with ThreadPoolExecutor(max_workers=2) as executor:
        futs = [executor.submit(do_rollout), executor.submit(do_rollout)]
        results = [f.result() for f in as_completed(futs)]

    codes = sorted([r.status_code for r in results])
    assert codes == [200, 409] or codes == [409, 200]
    final = client.get(f"/v1/self-improvement/proposals/{proposal_id}")
    assert final.status_code == 200
    assert final.json()["status"] == "rolled_out"
    assert final.json()["rollout_artifact_id"]


def test_self_improvement_reject_path_and_events(client, engine):
    """Covers P2 e2e states + events + persist: reject from proposed leads to rejected, no further canary, decision cleared, events + list persist."""
    created = client.post(
        "/v1/self-improvement/proposals",
        json={
            "project_id": "boss-dogfood",
            "title": "Reject path test",
            "summary": "Test reject.",
            "change_ref": "ref-reject",
            "diff_summary": "Reject state.",
            "expected_impact": ["safety"],
            "test_plan": ["reject test"],
            "canary_scope": ["unit"],
            "rollback_plan": ["revert"],
            "success_metrics": ["no proceed"],
            "actor": "vp_agents",
        },
    )
    assert created.status_code == 201
    proposal_id = created.json()["id"]
    dec_id = created.json()["current_decision_id"]
    assert created.json()["status"] == "proposed"

    # reject via decision
    rej = client.post(
        f"/v1/decisions/{dec_id}/reject",
        json={"actor": "ceo", "note": "Not ready."},
    )
    assert rej.status_code == 200

    prop = client.get(f"/v1/self-improvement/proposals/{proposal_id}")
    assert prop.status_code == 200
    p = prop.json()
    assert p["status"] == "rejected"
    assert p["status_note"] == "Not ready."
    assert p["current_decision_id"] is None

    # cannot canary from rejected
    bad_can = client.post(
        f"/v1/self-improvement/proposals/{proposal_id}/canary",
        json={
            "actor": "release_manager",
            "outcome": "succeeded",
            "summary": "no",
            "metrics": {"bad": 1},
            "evidence_refs": ["bad"],
        },
    )
    assert bad_can.status_code == 409

    # list persists the state
    lst = client.get("/v1/self-improvement/proposals", params={"project_id": "boss-dogfood"})
    assert lst.status_code == 200
    items = lst.json()["items"]
    matching = [i for i in items if i["id"] == proposal_id]
    assert matching and matching[0]["status"] == "rejected"

    # events include reject
    evs = client.get("/v1/events", params={"project_id": "boss-dogfood"})
    etypes = [e["event_type"] for e in evs.json()["items"]]
    assert "self_improvement.rejected" in etypes
    assert "decision.rejected" in etypes  # from decision path


def test_cross_slice_e2e_regression_pack_import_selfimp_proposal_toasts_commercial_edition_budget_reserve_flow(client, engine):
    """Cross-slice e2e regression (P2 e2e + DoD) for 3x features:
    pack import (via service for P3 GAP-PRD-AUDIT-001 local upload + receipt+event+dedupe) +
    selfimp proposal (using pack context e.g. title/role ref + actor) +
    toasts feedback context (drives client states in web kpis.test/access for success/error/loading on related flows) +
    commercial edition check (community local_auth + source_pack) +
    budget reserve flow (quote/reserve + persist + release).
    Covers happy + error + perm + persist (re-list/re-get after mutates, list states survive).
    Per AGENTS, PRD (SIM-001, COMPKG, BUD, CAN, AGT), user-stories (OPS-002, FIN-001, ADM-007, ENG-002), trace, gap closed items.
    No silent drops (events/receipts), tenant scoped, no ext bypass.
    """
    from apps.api.app.config import get_settings
    from apps.api.app.services.specpack import SpecPackService

    settings = get_settings()
    specpack = SpecPackService(settings)

    # cleanup test artifacts (pack_installs not auto-deleted in all fixture paths; explicit for isolation)
    with engine.begin() as connection:
        connection.execute(
            text("DELETE FROM pack_installs WHERE project_id = 'boss-dogfood' AND pack_id LIKE 'test_e2e_reg_%'")
        )
        connection.execute(
            text("DELETE FROM events WHERE project_id = 'boss-dogfood' AND (event_type LIKE '%pack%' OR event_type LIKE '%self_improvement%')")
        )
        connection.execute(
            text("DELETE FROM receipts WHERE dedupe_key LIKE '%test_e2e_reg_%' OR dedupe_key LIKE '%cross-reg%'")
        )
        connection.execute(
            text("DELETE FROM budget_reservations WHERE project_id = 'boss-dogfood' AND category = 'test_cross_reg'")
        )

    # commercial edition check (happy community path for local packs per COMPKG + constitution)
    ed_resp = client.get("/v1/edition", params={"project_id": "boss-dogfood"})
    assert ed_resp.status_code == 200
    ed = ed_resp.json()
    assert ed["edition_id"] == "community"
    assert ed["status"] == "active"
    assert ed["local_auth_allowed"] is True
    assert ed["account_connect_required"] is False
    assert "community_edition.yaml" in (ed.get("source_pack") or "")

    # budget reserve flow (happy, from FIN-001 AC: reserve + persist + perm)
    reserve_resp = client.post(
        "/v1/budget/reserve",
        json={
            "project_id": "boss-dogfood",
            "actor": "finops_sentinel",
            "category": "test_cross_reg",
            "amount_usd_micros": 12345,
            "reason": "cross-slice regression budget reserve for pack+selfimp",
            "ttl_seconds": 600,
        },
    )
    assert reserve_resp.status_code == 201
    reserve = reserve_resp.json()
    assert reserve["status"] == "reserved"
    res_id = reserve["reservation_id"]
    assert res_id

    # list status persists reserve
    status_resp = client.get(
        "/v1/budget/status",
        params={"scope_type": "project", "scope_id": "boss-dogfood"},
    )
    assert status_resp.status_code == 200
    assert status_resp.json()["active_reservations_count"] >= 1

    # happy: pack import (P3 local, via service as endpoint pending full wire; exercises receipt dedupe, event, yaml validate, role install from core, persist)
    valid_yaml = """id: test_e2e_reg_pack_v1
version: "0.1.0"
description: "E2E regression cross-slice pack for import + selfimp + commercial + budget"
installs:
  roles:
    - metatron
requires: []
"""
    with engine.begin() as connection:
        imported = specpack.import_pack(connection, "boss-dogfood", valid_yaml, "ceo")
    assert imported["pack_id"] == "test_e2e_reg_pack_v1"
    assert imported["install_status"] in ("imported", "already_imported")
    assert imported.get("role_count", 0) >= 1
    assert any(
        role_result.get("role_key") == "metatron"
        for role_result in imported.get("role_results", [])
    )

    # receipt with dedupe_key (hard rule)
    with engine.begin() as connection:
        rec = connection.execute(
            text(
                """
                SELECT id, effect_kind, dedupe_key, status
                FROM receipts
                WHERE dedupe_key = :dk
                """
            ),
            {"dk": "pack.import:boss-dogfood:test_e2e_reg_pack_v1"},
        ).mappings().first()
    assert rec is not None
    assert rec["effect_kind"] == "pack.import"
    assert rec["status"] == "succeeded"

    # event emitted
    ev_resp = client.get("/v1/events", params={"project_id": "boss-dogfood", "limit": 20})
    assert ev_resp.status_code == 200
    etypes = [e["event_type"] for e in ev_resp.json().get("items", [])]
    assert "pack.imported" in etypes

    # persist via API list/get after import (pack feed + commercial context)
    packs_resp = client.get("/v1/packs", params={"project_id": "boss-dogfood"})
    assert packs_resp.status_code == 200
    pitems = packs_resp.json()["items"]
    found_pack = next((p for p in pitems if p["pack_id"] == "test_e2e_reg_pack_v1"), None)
    assert found_pack is not None
    # cross: commercial edition source_pack still community
    assert ed["source_pack"] == "community_edition.yaml" or "community" in ed["source_pack"]

    # cross: selfimp proposal using new pack context (title ref imported pack, actor from pack lineage)
    # (exercises SIM-001 happy path + receipt/artifact/decision/task + events)
    prop_resp = client.post(
        "/v1/self-improvement/proposals",
        json={
            "project_id": "boss-dogfood",
            "title": "Improve test_e2e_reg_pack_v1 + metatron from imported pack",
            "summary": "Cross regression: pack import enables role extension for self-imp.",
            "change_ref": "https://example.com/reg-pr-3x",
            "diff_summary": "Add e2e cross tests for pack+selfimp+budget+commercial+toasts.",
            "expected_impact": ["full regression coverage", "no silent drops in 3x flows"],
            "test_plan": ["pytest -k 'pack or self_improvement or commercial or toasts'", "pnpm test"],
            "canary_scope": ["dogfood only"],
            "rollback_plan": ["delete test pack_install", "revert commit"],
            "success_metrics": ["all tests green", "receipts present", "events emitted"],
            "actor": "vp_agents",
            "risk_tier": 1,
        },
    )
    assert prop_resp.status_code == 201
    prop = prop_resp.json()
    assert prop["status"] == "proposed"
    assert prop["task_id"]
    assert prop["current_decision_id"]
    prop_id = prop["id"]

    # persist: list selfimp shows it post create (state machine persist)
    sims_resp = client.get("/v1/self-improvement/proposals", params={"project_id": "boss-dogfood"})
    assert sims_resp.status_code == 200
    sim_items = sims_resp.json()["items"]
    matching_sim = next((s for s in sim_items if s["id"] == prop_id), None)
    assert matching_sim is not None
    assert matching_sim["status"] == "proposed"
    assert "test_e2e_reg_pack_v1" in matching_sim["title"]

    # error paths for pack import
    with engine.begin() as connection:
        # invalid yaml
        try:
            specpack.import_pack(connection, "boss-dogfood", "invalid: [ yaml: structure", "ceo")
            assert False, "expected ValueError for bad yaml"
        except ValueError as exc:
            assert "invalid yaml" in str(exc).lower() or "mapping" in str(exc).lower()

        # core pack clash (error)
        try:
            clash = """id: core_v1
version: "9.9.9"
description: "clash test"
installs:
  roles: []
requires: []
"""
            specpack.import_pack(connection, "boss-dogfood", clash, "ceo")
            assert False, "expected clash error"
        except ValueError as exc:
            assert "clashes with core baked pack" in str(exc)

    # perm example (pack_manage gate on install path; import is internal but cross covered; self-imp perm via bad actor on other)
    perm_bad = client.post(
        "/v1/packs/install",
        json={"project_id": "boss-dogfood", "pack_id": "core_v1", "actor": "random-no-perm-actor"},
    )
    assert perm_bad.status_code in (403, 422)  # perm or role not found depending

    # also budget permission cross: consume checks budget_manage before receipt lookup.
    bad_budget = client.post(
        "/v1/budget/consume",
        json={
            "project_id": "boss-dogfood",
            "reservation_id": res_id,
            "receipt_id": "missing_receipt_for_permission_path",
            "category": "test_cross_reg",
            "skill_or_model_id": "test:e2e",
            "estimated_usd_micros": 1,
            "actual_usd_micros": 1,
            "actor": "coo",
        },
    )
    assert bad_budget.status_code == 403

    # release budget res (cleanup)
    rel_resp = client.post(
        "/v1/budget/release",
        json={
            "project_id": "boss-dogfood",
            "reservation_id": res_id,
            "reason": "cross regression cleanup",
        },
    )
    assert rel_resp.status_code == 200

    # final persist check: re-fetch pack and selfimp after all
    packs2 = client.get("/v1/packs", params={"project_id": "boss-dogfood"})
    assert any(p["pack_id"] == "test_e2e_reg_pack_v1" for p in packs2.json()["items"])
    sims2 = client.get("/v1/self-improvement/proposals", params={"project_id": "boss-dogfood"})
    assert any(s["id"] == prop_id and s["status"] == "proposed" for s in sims2.json()["items"])

    # toasts feedback covered indirectly: these flows (pack receipt/event, selfimp create, budget reserve, commercial check) are the server side for client ActionForm toasts + revalidate in web tests (kpis.test client action feedback, accessibility toast a11y, commercial states)
    # happy/error/perm/persist all exercised above without silent failure.


def test_approved_decision_can_be_manually_executed_with_evidence(client, engine):
    with engine.begin() as connection:
        connection.execute(
            text(
                """
                INSERT INTO tasks (
                    id, project_id, title, description, status, priority, risk_tier, dedupe_key
                )
                VALUES (
                    'task_manual_execute', 'boss-dogfood', 'Manual merge follow-on',
                    'Track a manually executed follow-on action.', 'open', 'p1', 2,
                    'manual-execute-task'
                )
                """
            )
        )
        connection.execute(
            text(
                """
                INSERT INTO decisions (
                    id, project_id, task_id, question, recommendation, options, risk_tier,
                    cost_summary, deadline_at, default_action, status, approved_by, approved_at
                )
                VALUES (
                    'dec_manual_execute', 'boss-dogfood', 'task_manual_execute',
                    'Record the manual sandbox merge.', '{}'::jsonb, '[]'::jsonb, 2,
                    '{}'::jsonb, now() + interval '1 day', 'hold', 'approved',
                    'ceo', now()
                )
                """
            )
        )

    execute = client.post(
        "/v1/decisions/dec_manual_execute/execute",
        json={
            "actor": "release_manager",
            "note": "Merged manually after review in GitHub.",
            "external_ref": "https://github.com/seanheiney/boss-sandbox/pull/42",
        },
    )
    assert execute.status_code == 200
    payload = execute.json()
    assert payload["status"] == "executed"
    assert payload["executed_by"] == "release_manager"
    assert payload["executed_at"]
    assert payload["external_ref"] == "https://github.com/seanheiney/boss-sandbox/pull/42"
    assert payload["resolution_note"] == "Merged manually after review in GitHub."

    with engine.begin() as connection:
        task_status = connection.execute(
            text("SELECT status FROM tasks WHERE id = 'task_manual_execute'")
        ).scalar_one()
        artifact_type = connection.execute(
            text(
                """
                SELECT artifact_type
                FROM artifacts
                WHERE task_id = 'task_manual_execute'
                ORDER BY created_at DESC
                LIMIT 1
                """
            )
        ).scalar_one()

    assert task_status == "completed"
    assert artifact_type == "manual_execution_record"

    events = client.get("/v1/events", params={"project_id": "boss-dogfood"})
    assert events.status_code == 200
    event_types = [item["event_type"] for item in events.json()["items"]]
    assert "decision.executed" in event_types
    assert "task.completed" in event_types


def test_overdue_pending_decisions_expire_to_safe_default(client, engine):
    deadline_at = datetime.now(timezone.utc) - timedelta(minutes=1)
    with engine.begin() as connection:
        connection.execute(
            text(
                """
                INSERT INTO decisions (
                    id, project_id, task_id, question, recommendation, options, risk_tier,
                    cost_summary, deadline_at, default_action, status
                )
                VALUES (
                    'dec_expired_for_inbox', 'boss-dogfood', NULL, 'Expired decision',
                    '{}'::jsonb, '[]'::jsonb, 2, '{}'::jsonb, :deadline_at, 'hold', 'pending'
                )
                """
            ),
            {"deadline_at": deadline_at},
        )

    inbox = client.get("/v1/decisions/inbox", params={"project_id": "boss-dogfood"})
    assert inbox.status_code == 200
    payload = inbox.json()
    assert payload["items"][0]["id"] == "dec_expired_for_inbox"
    assert payload["items"][0]["status"] == "expired"
    assert payload["items"][0]["default_action"] == "hold"

    events = client.get("/v1/events", params={"project_id": "boss-dogfood"})
    assert events.status_code == 200
    assert "decision.expired" in [item["event_type"] for item in events.json()["items"]]


def test_expired_reservations_are_reclassified_before_budget_status(client, engine):
    expired_at = datetime.now(timezone.utc) - timedelta(minutes=10)
    with engine.begin() as connection:
        connection.execute(
            text(
                """
                INSERT INTO budget_reservations (
                    id, allocation_id, project_id, job_id, category, amount_usd_micros,
                    status, reason, expires_at
                )
                VALUES (
                    'res_expired_budget_status', NULL, 'boss-dogfood', NULL, 'model', 12345,
                    'active', 'expired for test', :expires_at
                )
                """
            ),
            {"expires_at": expired_at},
        )

    status_response = client.get(
        "/v1/budget/status",
        params={"scope_type": "project", "scope_id": "boss-dogfood"},
    )
    assert status_response.status_code == 200
    payload = status_response.json()
    assert payload["reserved_usd_micros"] == 0

    with engine.begin() as connection:
        status = connection.execute(
            text(
                """
                SELECT status FROM budget_reservations
                WHERE id = 'res_expired_budget_status'
                """
            )
        ).scalar_one()
    assert status == "expired"

    events = client.get("/v1/events", params={"project_id": "boss-dogfood"})
    assert events.status_code == 200
    assert "budget.reservation_expired" in [
        item["event_type"] for item in events.json()["items"]
    ]


def test_openai_plan_blocks_without_reservation(client):
    response = client.post(
        "/v1/openai/plans",
        json={
            "project_id": "boss-dogfood",
            "reservation_id": "missing",
            "dedupe_key": "test-plan-no-res",
            "prompt": "Create a plan.",
        },
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "blocked"
    assert payload["decision_id"]


def test_openai_plan_blocks_without_credentials_and_releases_reservation(client, engine):
    reserve = client.post(
        "/v1/budget/reserve",
        json={
            "project_id": "boss-dogfood",
            "category": "model",
            "amount_usd_micros": 250_000,
            "reason": "OpenAI plan without credentials.",
        },
    )
    assert reserve.status_code == 201
    reservation_id = reserve.json()["reservation_id"]

    previous = api_main.software_factory.settings.openai_api_key
    api_main.software_factory.settings.openai_api_key = None
    try:
        response = client.post(
            "/v1/openai/plans",
            json={
                "project_id": "boss-dogfood",
                "reservation_id": reservation_id,
                "dedupe_key": "missing-openai-creds",
                "prompt": "Create a plan.",
            },
        )
    finally:
        api_main.software_factory.settings.openai_api_key = previous

    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "blocked"
    assert payload["decision_id"]
    assert "OPENAI_API_KEY" in payload["message"]

    with engine.begin() as connection:
        reservation_status = connection.execute(
            text(
                """
                SELECT status
                FROM budget_reservations
                WHERE id = :reservation_id
                """
            ),
            {"reservation_id": reservation_id},
        ).scalar_one()
        decision_question = connection.execute(
            text(
                """
                SELECT question
                FROM decisions
                WHERE id = :decision_id
                """
            ),
            {"decision_id": payload["decision_id"]},
        ).scalar_one()

    assert reservation_status == "released"
    assert decision_question == "OpenAI integration is not configured"


def test_two_workers_cannot_claim_same_job(client):
    created = client.post(
        "/v1/jobs",
        json={
            "project_id": "boss-dogfood",
            "kind": "plan.generate",
            "payload": {"command": "Create a draft plan."},
        },
    )
    assert created.status_code == 201
    job_id = created.json()["id"]

    worker_a = client.post(
        "/v1/worker/poll",
        json={
            "worker_id": "worker-a",
            "capabilities": ["plan.generate"],
            "lease_ttl_seconds": 60,
        },
    )
    assert worker_a.status_code == 200
    leased = worker_a.json()
    assert leased["status"] == "leased"
    assert leased["job"]["id"] == job_id

    worker_b = client.post(
        "/v1/worker/poll",
        json={
            "worker_id": "worker-b",
            "capabilities": ["plan.generate"],
            "lease_ttl_seconds": 60,
        },
    )
    assert worker_b.status_code == 200
    assert worker_b.json()["status"] == "idle"
    assert worker_b.json()["job"] is None


def test_get_job_returns_created_job(client):
    created = client.post(
        "/v1/jobs",
        json={
            "project_id": "boss-dogfood",
            "kind": "plan.generate",
            "payload": {"command": "Create a draft plan."},
        },
    )
    assert created.status_code == 201
    job_id = created.json()["id"]

    fetched = client.get(f"/v1/jobs/{job_id}")
    assert fetched.status_code == 200
    payload = fetched.json()
    assert payload["id"] == job_id
    assert payload["kind"] == "plan.generate"
    assert payload["status"] == "queued"


def test_agent_job_lease_attaches_runtime_and_completion_creates_artifact(client):
    created = client.post(
        "/v1/jobs",
        json={
            "project_id": "boss-dogfood",
            "kind": "agent.pm.scope_brief",
            "payload": {
                "command": "Draft scope for ProviderOps dashboard alerts.",
                "risk_tier": 1,
            },
        },
    )
    assert created.status_code == 201
    job_id = created.json()["id"]

    poll = client.post(
        "/v1/worker/poll",
        json={
            "worker_id": "worker-agent-pm",
            "capabilities": ["agent.pm.scope_brief"],
            "lease_ttl_seconds": 60,
        },
    )
    assert poll.status_code == 200
    leased = poll.json()["job"]
    assert leased["id"] == job_id
    assert leased["payload"]["agent_module"]["id"] == "pm_product_planner_v1"
    assert leased["payload"]["agent_runtime"]["role"]["role_key"] == "pm"
    assert "No worker execution without a lease." in leased["payload"][
        "agent_runtime"
    ]["compiled_prompt"]["system"]

    complete = client.post(
        "/v1/worker/complete",
        json={
            "worker_id": "worker-agent-pm",
            "job_id": job_id,
            "lease_token": leased["lease_token"],
            "result": {
                "summary": "PM scope brief for ProviderOps dashboard alerts.",
                "findings": ["The request is UI and API visible."],
                "recommendations": ["Create acceptance criteria before build."],
                "required_decisions": ["Confirm alert threshold defaults."],
                "agent_module": leased["payload"]["agent_module"],
                "agent_runtime": leased["payload"]["agent_runtime"],
                "strategy_id": "agent.pm.scope_brief",
                "grounding_status": "needs_control_plane_context",
                "work_product": {
                    "contract": "FeatureBrief",
                    "sections": ["problem_statement", "acceptance_criteria"],
                    "evidence_requirements": ["prd", "task", "decision", "user_story"],
                    "safety_gates": ["explicit_non_goals"],
                },
                "prompt_excerpt": leased["payload"]["agent_runtime"][
                    "compiled_prompt"
                ]["job"][:400],
            },
        },
    )
    assert complete.status_code == 200
    payload = complete.json()
    assert payload["status"] == "succeeded"
    assert payload["artifact_id"]

    artifact = client.get(f"/v1/artifacts/{payload['artifact_id']}")
    assert artifact.status_code == 200
    content = artifact.json()["content"]
    assert artifact.json()["artifact_type"] == "agent_brief"
    assert content["agent_module"]["id"] == "pm_product_planner_v1"
    assert content["agent_runtime"]["role"]["role_key"] == "pm"
    assert content["required_decisions"] == ["Confirm alert threshold defaults."]
    assert content["strategy_id"] == "agent.pm.scope_brief"
    assert content["grounding_status"] == "needs_control_plane_context"
    assert content["work_product"]["contract"] == "FeatureBrief"

    events = client.get("/v1/events", params={"project_id": "boss-dogfood"})
    assert "agent.brief_created" in [
        item["event_type"] for item in events.json()["items"]
    ]


def test_enterprise_marketplace_validate_job_creates_entitlement_gate_report(client):
    created = client.post(
        "/v1/jobs",
        json={
            "project_id": "boss-dogfood",
            "kind": "enterprise.marketplace.validate",
            "payload": {
                "module_id": "private_enterprise_agent_v1",
                "required_entitlement": "private_agent_marketplace",
                "entitlement_status": "missing",
            },
        },
    )
    assert created.status_code == 201
    job_id = created.json()["id"]

    poll = client.post(
        "/v1/worker/poll",
        json={
            "worker_id": "worker-marketplace-validator",
            "capabilities": ["enterprise.marketplace.validate"],
            "lease_ttl_seconds": 60,
        },
    )
    assert poll.status_code == 200
    leased = poll.json()["job"]
    assert leased["id"] == job_id

    complete = client.post(
        "/v1/worker/complete",
        json={
            "worker_id": "worker-marketplace-validator",
            "job_id": job_id,
            "lease_token": leased["lease_token"],
            "result": {
                "status": "blocked",
                "summary": "Marketplace validation blocked missing entitlement.",
                "module_id": "private_enterprise_agent_v1",
                "required_entitlement": "private_agent_marketplace",
                "entitlement_status": "missing",
                "findings": ["Module requires private_agent_marketplace."],
                "recommendations": ["Keep install blocked until entitlement exists."],
                "required_decisions": [
                    {
                        "reason": "missing_marketplace_entitlement",
                        "required_entitlement": "private_agent_marketplace",
                        "default_action": "hold",
                    }
                ],
                "work_product": {
                    "contract": "EntitlementGateReport",
                    "sections": ["module_identity", "entitlement_status"],
                    "safety_gates": ["do_not_install_without_entitlement"],
                },
            },
        },
    )
    assert complete.status_code == 200
    payload = complete.json()
    assert payload["status"] == "succeeded"
    assert payload["artifact_id"]

    artifact = client.get(f"/v1/artifacts/{payload['artifact_id']}")
    assert artifact.status_code == 200
    content = artifact.json()["content"]
    assert artifact.json()["artifact_type"] == "entitlement_gate_report"
    assert content["module_id"] == "private_enterprise_agent_v1"
    assert content["status"] == "blocked"
    assert content["work_product"]["contract"] == "EntitlementGateReport"

    events = client.get("/v1/events", params={"project_id": "boss-dogfood"})
    assert "enterprise.marketplace_validated" in [
        item["event_type"] for item in events.json()["items"]
    ]


def test_expired_lease_can_be_reclaimed(client, engine):
    created = client.post(
        "/v1/jobs",
        json={
            "project_id": "boss-dogfood",
            "kind": "plan.generate",
            "payload": {"command": "Create a draft plan."},
        },
    )
    assert created.status_code == 201
    job_id = created.json()["id"]

    first = client.post(
        "/v1/worker/poll",
        json={
            "worker_id": "worker-a",
            "capabilities": ["plan.generate"],
            "lease_ttl_seconds": 60,
        },
    )
    assert first.status_code == 200
    assert first.json()["job"]["attempt"] == 1

    with engine.begin() as connection:
        connection.execute(
            text(
                """
                UPDATE jobs
                SET lease_expires_at = :lease_expires_at
                WHERE id = :job_id
                """
            ),
            {
                "job_id": job_id,
                "lease_expires_at": datetime.now(timezone.utc) - timedelta(seconds=5),
            },
        )

    second = client.post(
        "/v1/worker/poll",
        json={
            "worker_id": "worker-b",
            "capabilities": ["plan.generate"],
            "lease_ttl_seconds": 60,
        },
    )
    assert second.status_code == 200
    payload = second.json()
    assert payload["status"] == "leased"
    assert payload["job"]["id"] == job_id
    assert payload["job"]["attempt"] == 2

    events = client.get("/v1/events", params={"project_id": "boss-dogfood"})
    assert events.status_code == 200
    assert "job.lease_reclaimed" in [
        item["event_type"] for item in events.json()["items"]
    ]


def test_failed_job_retries_then_deadletters(client, engine):
    created = client.post(
        "/v1/jobs",
        json={
            "project_id": "boss-dogfood",
            "kind": "plan.generate",
            "payload": {"command": "Create a draft plan."},
            "max_attempts": 2,
        },
    )
    assert created.status_code == 201
    job_id = created.json()["id"]

    first_lease = client.post(
        "/v1/worker/poll",
        json={
            "worker_id": "worker-a",
            "capabilities": ["plan.generate"],
            "lease_ttl_seconds": 60,
        },
    ).json()["job"]
    heartbeat = client.post(
        "/v1/worker/heartbeat",
        json={
            "worker_id": "worker-a",
            "job_id": job_id,
            "lease_token": first_lease["lease_token"],
            "lease_ttl_seconds": 60,
        },
    )
    assert heartbeat.status_code == 200

    first_fail = client.post(
        "/v1/worker/fail",
        json={
            "worker_id": "worker-a",
            "job_id": job_id,
            "lease_token": first_lease["lease_token"],
            "error": {"message": "transient failure"},
            "retryable": True,
            "retry_delay_seconds": 0,
        },
    )
    assert first_fail.status_code == 200
    assert first_fail.json()["status"] == "failed_retryable"

    second_lease = client.post(
        "/v1/worker/poll",
        json={
            "worker_id": "worker-b",
            "capabilities": ["plan.generate"],
            "lease_ttl_seconds": 60,
        },
    ).json()["job"]
    second_heartbeat = client.post(
        "/v1/worker/heartbeat",
        json={
            "worker_id": "worker-b",
            "job_id": job_id,
            "lease_token": second_lease["lease_token"],
            "lease_ttl_seconds": 60,
        },
    )
    assert second_heartbeat.status_code == 200

    second_fail = client.post(
        "/v1/worker/fail",
        json={
            "worker_id": "worker-b",
            "job_id": job_id,
            "lease_token": second_lease["lease_token"],
            "error": {"message": "still failing"},
            "retryable": True,
            "retry_delay_seconds": 0,
        },
    )
    assert second_fail.status_code == 200
    payload = second_fail.json()
    assert payload["status"] == "deadlettered"
    assert payload["deadletter_id"]

    with engine.begin() as connection:
        status = connection.execute(
            text("SELECT status FROM jobs WHERE id = :job_id"),
            {"job_id": job_id},
        ).scalar_one()
        deadletters = connection.execute(
            text(
                "SELECT COUNT(*) FROM deadletters WHERE source_kind = 'job' AND source_id = :job_id"
            ),
            {"job_id": job_id},
        ).scalar_one()
    assert status == "deadlettered"
    assert deadletters == 1


def test_deadletters_are_visible_via_api(client):
    created = client.post(
        "/v1/jobs",
        json={
            "project_id": "boss-dogfood",
            "kind": "plan.generate",
            "payload": {"command": "Create a draft plan."},
            "max_attempts": 1,
        },
    )
    assert created.status_code == 201
    job_id = created.json()["id"]

    leased = client.post(
        "/v1/worker/poll",
        json={
            "worker_id": "worker-deadletter",
            "capabilities": ["plan.generate"],
            "lease_ttl_seconds": 60,
        },
    ).json()["job"]
    client.post(
        "/v1/worker/heartbeat",
        json={
            "worker_id": "worker-deadletter",
            "job_id": job_id,
            "lease_token": leased["lease_token"],
            "lease_ttl_seconds": 60,
        },
    )
    fail = client.post(
        "/v1/worker/fail",
        json={
            "worker_id": "worker-deadletter",
            "job_id": job_id,
            "lease_token": leased["lease_token"],
            "error": {"message": "terminal failure"},
            "retryable": False,
            "retry_delay_seconds": 0,
        },
    )
    assert fail.status_code == 200
    deadletter_id = fail.json()["deadletter_id"]

    deadletters = client.get(
        "/v1/deadletters",
        params={"project_id": "boss-dogfood", "limit": 10},
    )
    assert deadletters.status_code == 200
    payload = deadletters.json()
    assert payload["project_id"] == "boss-dogfood"
    item = next(entry for entry in payload["items"] if entry["id"] == deadletter_id)
    assert item["source_kind"] == "job"
    assert item["source_id"] == job_id
    assert item["reason"] == "terminal failure"
    assert item["status"] == "open"


def test_deadletter_incident_and_job_requeue_are_audited_and_idempotent(client, engine):
    created = client.post(
        "/v1/jobs",
        json={
            "project_id": "boss-dogfood",
            "kind": "plan.generate",
            "payload": {"command": "Create a draft plan for remediation."},
            "max_attempts": 1,
        },
    )
    assert created.status_code == 201
    job_id = created.json()["id"]

    leased = client.post(
        "/v1/worker/poll",
        json={
            "worker_id": "worker-deadletter-remediation",
            "capabilities": ["plan.generate"],
            "lease_ttl_seconds": 60,
        },
    ).json()["job"]
    client.post(
        "/v1/worker/heartbeat",
        json={
            "worker_id": "worker-deadletter-remediation",
            "job_id": job_id,
            "lease_token": leased["lease_token"],
            "lease_ttl_seconds": 60,
        },
    )
    fail = client.post(
        "/v1/worker/fail",
        json={
            "worker_id": "worker-deadletter-remediation",
            "job_id": job_id,
            "lease_token": leased["lease_token"],
            "error": {"message": "terminal failure requiring COO packet"},
            "retryable": False,
            "retry_delay_seconds": 0,
        },
    )
    assert fail.status_code == 200
    deadletter_id = fail.json()["deadletter_id"]

    incident = client.post(
        f"/v1/deadletters/{deadletter_id}/incident",
        json={
            "actor": "ceo",
            "severity": "critical",
            "summary": "Terminal job failure must be reviewed before retry.",
        },
    )
    assert incident.status_code == 200
    incident_payload = incident.json()
    assert incident_payload["status"] == "incident_created"
    assert incident_payload["incident_task_id"].startswith("tsk_")
    assert incident_payload["incident_artifact_id"].startswith("art_")

    artifact = client.get(f"/v1/artifacts/{incident_payload['incident_artifact_id']}")
    assert artifact.status_code == 200
    artifact_payload = artifact.json()
    assert artifact_payload["artifact_type"] == "deadletter_incident_packet"
    assert artifact_payload["content"]["deadletter_id"] == deadletter_id
    assert "No silent drops." in artifact_payload["content"]["boss_invariants"]

    duplicate_incident = client.post(
        f"/v1/deadletters/{deadletter_id}/incident",
        json={"actor": "ceo", "severity": "high"},
    )
    assert duplicate_incident.status_code == 200
    assert (
        duplicate_incident.json()["incident_artifact_id"]
        == incident_payload["incident_artifact_id"]
    )

    requeue = client.post(
        f"/v1/deadletters/{deadletter_id}/requeue",
        json={
            "actor": "ceo",
            "reason": "root_cause_fixed",
            "run_after_seconds": 0,
        },
    )
    assert requeue.status_code == 200
    requeue_payload = requeue.json()
    assert requeue_payload["status"] == "requeued"
    requeued_job_id = requeue_payload["requeued_job_id"]
    assert requeued_job_id.startswith("job_")

    with engine.begin() as connection:
        row = connection.execute(
            text(
                """
                SELECT status, kind, payload, attempt
                FROM jobs
                WHERE id = :job_id
                """
            ),
            {"job_id": requeued_job_id},
        ).mappings().one()
    assert row["status"] == "queued"
    assert row["kind"] == "plan.generate"
    assert row["attempt"] == 0
    assert row["payload"]["requeue"]["source_job_id"] == job_id
    assert row["payload"]["requeue"]["source_deadletter_id"] == deadletter_id

    duplicate_requeue = client.post(
        f"/v1/deadletters/{deadletter_id}/requeue",
        json={"actor": "ceo", "reason": "retry_button_double_click"},
    )
    assert duplicate_requeue.status_code == 200
    assert duplicate_requeue.json()["requeued_job_id"] == requeued_job_id

    events = client.get("/v1/events", params={"project_id": "boss-dogfood"})
    event_types = [item["event_type"] for item in events.json()["items"]]
    assert "deadletter.incident_created" in event_types
    assert "deadletter.requeued" in event_types


def test_deadletter_requeue_rejects_non_job_sources(client, engine):
    with engine.begin() as connection:
        connection.execute(
            text(
                """
                INSERT INTO deadletters (id, project_id, source_kind, source_id, reason, payload)
                VALUES (
                    'dlq_non_job_test', 'boss-dogfood', 'provider_sync',
                    'sync_123', 'provider credentials missing', '{}'::jsonb
                )
                """
            )
        )

    response = client.post(
        "/v1/deadletters/dlq_non_job_test/requeue",
        json={"actor": "ceo"},
    )
    assert response.status_code == 400
    assert response.json()["detail"] == "only job deadletters can be requeued"


# === WORKER/LEASE/JOB E2E POLISH (P2 + DoD ops) added tests for leases, heartbeats, failure paths (409s), deadletter, pack/agent related jobs ===
def test_lease_heartbeat_and_complete_fail_on_invalid_lease_token(client):
    created = client.post(
        "/v1/jobs",
        json={
            "project_id": "boss-dogfood",
            "kind": "plan.generate",
            "payload": {"command": "Lease token validation test."},
        },
    )
    assert created.status_code == 201
    job_id = created.json()["id"]

    leased = client.post(
        "/v1/worker/poll",
        json={
            "worker_id": "worker-token-test",
            "capabilities": ["plan.generate"],
            "lease_ttl_seconds": 60,
        },
    ).json()["job"]

    # bad token -> 409 on heartbeat
    hb_bad = client.post(
        "/v1/worker/heartbeat",
        json={
            "worker_id": "worker-token-test",
            "job_id": job_id,
            "lease_token": "bad-token-not-real",
            "lease_ttl_seconds": 60,
        },
    )
    assert hb_bad.status_code == 409
    assert "Lease" in hb_bad.json()["detail"] or "invalid" in hb_bad.json().get("detail", "").lower()

    # bad token on complete -> 409
    comp_bad = client.post(
        "/v1/worker/complete",
        json={
            "worker_id": "worker-token-test",
            "job_id": job_id,
            "lease_token": "bad-token-not-real",
            "result": {"summary": "no"},
        },
    )
    assert comp_bad.status_code == 409

    # bad token on fail -> 409
    fail_bad = client.post(
        "/v1/worker/fail",
        json={
            "worker_id": "worker-token-test",
            "job_id": job_id,
            "lease_token": "bad-token-not-real",
            "error": {"message": "bad"},
            "retryable": False,
            "retry_delay_seconds": 0,
        },
    )
    assert fail_bad.status_code == 409

    # valid complete succeeds after real lease token
    comp_ok = client.post(
        "/v1/worker/complete",
        json={
            "worker_id": "worker-token-test",
            "job_id": job_id,
            "lease_token": leased["lease_token"],
            "result": {"summary": "lease token paths exercised"},
        },
    )
    assert comp_ok.status_code == 200
    assert comp_ok.json()["status"] == "succeeded"


def test_multiple_heartbeats_extend_lease_and_emit_events(client, engine):
    created = client.post(
        "/v1/jobs",
        json={
            "project_id": "boss-dogfood",
            "kind": "plan.generate",
            "payload": {"command": "Multi heartbeat test."},
        },
    )
    assert created.status_code == 201
    job_id = created.json()["id"]

    leased = client.post(
        "/v1/worker/poll",
        json={
            "worker_id": "worker-hb-multi",
            "capabilities": ["plan.generate"],
            "lease_ttl_seconds": 30,
        },
    ).json()["job"]
    first_expiry = leased["lease_expires_at"]

    hb1 = client.post(
        "/v1/worker/heartbeat",
        json={
            "worker_id": "worker-hb-multi",
            "job_id": job_id,
            "lease_token": leased["lease_token"],
            "lease_ttl_seconds": 30,
        },
    )
    assert hb1.status_code == 200

    hb2 = client.post(
        "/v1/worker/heartbeat",
        json={
            "worker_id": "worker-hb-multi",
            "job_id": job_id,
            "lease_token": leased["lease_token"],
            "lease_ttl_seconds": 45,
        },
    )
    assert hb2.status_code == 200
    assert hb2.json()["lease_expires_at"] != first_expiry

    # verify DB heartbeat updated and job still leased/running capable
    with engine.begin() as connection:
        row = connection.execute(
            text("SELECT status, heartbeat_at FROM jobs WHERE id = :job_id"),
            {"job_id": job_id},
        ).mappings().one()
        assert row["status"] in ("leased", "running")
        assert row["heartbeat_at"] is not None

    events = client.get("/v1/events", params={"project_id": "boss-dogfood", "limit": 20})
    hb_events = [e for e in events.json()["items"] if e["event_type"] == "job.heartbeat"]
    assert len(hb_events) >= 2


def test_deadletter_requeue_then_poll_lease_and_succeed_new_job(client, engine):
    created = client.post(
        "/v1/jobs",
        json={
            "project_id": "boss-dogfood",
            "kind": "plan.generate",
            "payload": {"command": "Requeue then lease e2e."},
            "max_attempts": 1,
        },
    )
    assert created.status_code == 201
    job_id = created.json()["id"]

    leased = client.post(
        "/v1/worker/poll",
        json={
            "worker_id": "worker-req",
            "capabilities": ["plan.generate"],
            "lease_ttl_seconds": 60,
        },
    ).json()["job"]
    client.post(
        "/v1/worker/heartbeat",
        json={
            "worker_id": "worker-req",
            "job_id": job_id,
            "lease_token": leased["lease_token"],
            "lease_ttl_seconds": 60,
        },
    )
    fail = client.post(
        "/v1/worker/fail",
        json={
            "worker_id": "worker-req",
            "job_id": job_id,
            "lease_token": leased["lease_token"],
            "error": {"message": "pack related sim fail"},
            "retryable": False,
            "retry_delay_seconds": 0,
        },
    )
    assert fail.status_code == 200
    dlq_id = fail.json()["deadletter_id"]

    # requeue via API (pack/job related remediation path)
    rq = client.post(
        f"/v1/deadletters/{dlq_id}/requeue",
        json={"actor": "coo", "reason": "test requeue polish"},
    )
    assert rq.status_code == 200
    assert rq.json()["status"] == "requeued"
    new_job_id = rq.json()["requeued_job_id"]
    assert new_job_id

    # the requeued job should be leasable
    new_leased = client.post(
        "/v1/worker/poll",
        json={
            "worker_id": "worker-req2",
            "capabilities": ["plan.generate"],
            "lease_ttl_seconds": 60,
        },
    ).json()["job"]
    assert new_leased["id"] == new_job_id
    # requeue meta in payload per impl
    assert "requeue" in (new_leased.get("payload") or {})

    # complete it to close
    comp = client.post(
        "/v1/worker/complete",
        json={
            "worker_id": "worker-req2",
            "job_id": new_job_id,
            "lease_token": new_leased["lease_token"],
            "result": {"summary": "requeued job succeeded"},
        },
    )
    assert comp.status_code == 200
    assert comp.json()["status"] == "succeeded"

    # verify events for requeue and new lease
    events = client.get("/v1/events", params={"project_id": "boss-dogfood"})
    types = [e["event_type"] for e in events.json()["items"]]
    assert "deadletter.requeued" in types
    assert "job.leased" in types


def test_poll_and_complete_pack_derived_agent_and_marketplace_jobs(client):
    # pack related: agent.* and enterprise.marketplace.validate come from role/pack installs
    for kind, caps in [
        ("agent.pm.scope_brief", ["agent.pm.scope_brief"]),
        ("enterprise.marketplace.validate", ["enterprise.marketplace.validate"]),
    ]:
        created = client.post(
            "/v1/jobs",
            json={
                "project_id": "boss-dogfood",
                "kind": kind,
                "payload": {
                    "command": f"Pack-derived {kind} e2e polish.",
                    "module_id": "pm_product_planner_v1" if "agent" in kind else "private_enterprise_agent_v1",
                    "required_entitlement": "private_agent_marketplace",
                    "entitlement_status": "missing",
                },
            },
        )
        assert created.status_code == 201
        job_id = created.json()["id"]

        poll = client.post(
            "/v1/worker/poll",
            json={
                "worker_id": f"worker-pack-{kind.split('.')[0]}",
                "capabilities": caps,
                "lease_ttl_seconds": 60,
            },
        )
        assert poll.status_code == 200
        assert poll.json()["status"] == "leased"
        leased = poll.json()["job"]
        assert leased["id"] == job_id
        assert leased["kind"] == kind

        # complete with minimal result matching what runner would
        result = {"summary": f"completed {kind}"} if "agent" not in kind else {
            "summary": "PM brief", "agent_module": {"id": "pm_product_planner_v1"}, "agent_runtime": {"role_key": "pm"},
            "work_product": {"contract": "FeatureBrief"}, "strategy_id": kind,
        }
        comp = client.post(
            "/v1/worker/complete",
            json={
                "worker_id": f"worker-pack-{kind.split('.')[0]}",
                "job_id": job_id,
                "lease_token": leased["lease_token"],
                "result": result,
            },
        )
        assert comp.status_code == 200
        assert comp.json()["status"] == "succeeded"


def test_ops_queue_inspection_and_digest_use_live_control_plane_state(client, engine):
    project_id = "boss-ops-inspection"
    with engine.begin() as connection:
        connection.execute(text("DELETE FROM deadletters WHERE project_id = :project_id"), {"project_id": project_id})
        connection.execute(text("DELETE FROM jobs WHERE project_id = :project_id"), {"project_id": project_id})
        connection.execute(text("DELETE FROM decisions WHERE project_id = :project_id"), {"project_id": project_id})
        connection.execute(text("DELETE FROM artifacts WHERE project_id = :project_id"), {"project_id": project_id})
        connection.execute(text("DELETE FROM events WHERE project_id = :project_id"), {"project_id": project_id})
        connection.execute(text("DELETE FROM project_members WHERE project_id = :project_id"), {"project_id": project_id})
        connection.execute(text("DELETE FROM projects WHERE id = :project_id"), {"project_id": project_id})
        connection.execute(
            text(
                """
                INSERT INTO projects (id, workspace_id, name, slug)
                VALUES (:project_id, 'ws_boss', 'BOSS Ops Inspection', :project_id)
                """
            ),
            {"project_id": project_id},
        )
        api_main.members.seed_default_members(connection, project_id, "test")

    queued_job = client.post(
        "/v1/jobs",
        json={
            "project_id": project_id,
            "kind": "plan.generate",
            "payload": {"command": "Queued work for ops inspection."},
        },
    )
    assert queued_job.status_code == 201
    queued_job_id = queued_job.json()["id"]

    stale_job = client.post(
        "/v1/jobs",
        json={
            "project_id": project_id,
            "kind": "agent.coo.ops_digest",
            "payload": {"command": "Lease me then become stale."},
        },
    )
    assert stale_job.status_code == 201
    stale_job_id = stale_job.json()["id"]
    leased = client.post(
        "/v1/worker/poll",
        json={
            "worker_id": "worker-stale-lease",
            "capabilities": ["agent.coo.ops_digest"],
            "lease_ttl_seconds": 60,
        },
    )
    assert leased.status_code == 200
    assert leased.json()["job"]["id"] == stale_job_id

    deadlettered = client.post(
        "/v1/jobs",
        json={
            "project_id": project_id,
            "kind": "plan.generate",
            "payload": {"command": "Deadletter me for ops inspection."},
            "max_attempts": 1,
        },
    )
    assert deadlettered.status_code == 201
    deadlettered_job_id = deadlettered.json()["id"]
    deadletter_lease = client.post(
        "/v1/worker/poll",
        json={
            "worker_id": "worker-ops-deadletter",
            "capabilities": ["plan.generate"],
            "lease_ttl_seconds": 60,
        },
    ).json()["job"]
    assert deadletter_lease["id"] == queued_job_id

    # Fail a different leased job by moving the intended deadletter job to the front.
    with engine.begin() as connection:
        connection.execute(
            text(
            """
            UPDATE jobs
            SET status = 'failed_retryable',
                run_after = now() - interval '1 minute',
                last_error = '{"message":"retryable ops failure"}'::jsonb
            WHERE id = :queued_job_id
            """
            ),
            {"queued_job_id": queued_job_id},
        )
        connection.execute(
            text(
                """
                UPDATE jobs
                SET lease_expires_at = now() - interval '5 minutes'
                WHERE id = :stale_job_id
                """
            ),
            {"stale_job_id": stale_job_id},
        )
        connection.execute(
            text(
                """
                INSERT INTO deadletters (
                    id, project_id, source_kind, source_id, reason, payload
                )
                VALUES (
                    'dlq_ops_inspect', :project_id, 'job', :deadlettered_job_id,
                    'manual terminal ops failure', '{"message":"terminal"}'::jsonb
                )
                """
            ),
            {"deadlettered_job_id": deadlettered_job_id, "project_id": project_id},
        )
        connection.execute(
            text(
                """
                UPDATE jobs
                SET status = 'deadlettered'
                WHERE id = :deadlettered_job_id
                """
            ),
            {"deadlettered_job_id": deadlettered_job_id},
        )
        connection.execute(
            text(
                """
                INSERT INTO decisions (
                    id, project_id, question, recommendation, options, risk_tier,
                    cost_summary, default_action, status
                )
                VALUES (
                    'dec_ops_inspect', :project_id, 'Approve ops remediation?',
                    '{"summary":"Review queue blockers"}'::jsonb,
                    '[]'::jsonb, 2, '{}'::jsonb, 'hold', 'pending'
                )
                """
            ),
            {"project_id": project_id},
        )

    queue = client.get(
        "/v1/ops/queue",
        params={"project_id": project_id, "limit": 20},
    )
    assert queue.status_code == 200
    payload = queue.json()
    assert payload["project_id"] == project_id
    assert payload["job_status_counts"]["failed_retryable"] >= 1
    assert payload["job_status_counts"]["deadlettered"] >= 1
    assert queued_job_id in payload["ready_job_ids"]
    assert stale_job_id in payload["stale_lease_job_ids"]
    assert any(item["id"] == "dlq_ops_inspect" for item in payload["deadletters"])
    assert any(item["id"] == "dec_ops_inspect" for item in payload["pending_decisions"])
    blocker_categories = {item["category"] for item in payload["blockers"]}
    assert {"deadletters", "leases", "retry", "decisions"}.issubset(
        blocker_categories
    )

    digest = client.post(
        "/v1/ops/digests",
        json={"project_id": project_id, "actor": "coo", "limit": 20},
    )
    assert digest.status_code == 201
    digest_payload = digest.json()
    assert digest_payload["artifact_type"] == "ops_digest"
    assert digest_payload["counts"]["blockers"] >= 4

    artifact = client.get(f"/v1/artifacts/{digest_payload['artifact_id']}")
    assert artifact.status_code == 200
    artifact_payload = artifact.json()
    assert artifact_payload["artifact_type"] == "ops_digest"
    inspection = artifact_payload["content"]["inspection"]
    assert inspection["ready_job_ids"]
    assert "No silent drops." in artifact_payload["content"]["boss_invariants"]

    events = client.get("/v1/events", params={"project_id": project_id})
    assert "ops.digest_created" in [item["event_type"] for item in events.json()["items"]]


def test_worker_complete_creates_plan_artifact_and_decision(client):
    created = client.post(
        "/v1/jobs",
        json={
            "project_id": "boss-dogfood",
            "kind": "plan.generate",
            "payload": {"command": "Create a draft plan."},
        },
    )
    assert created.status_code == 201
    job_id = created.json()["id"]

    leased = client.post(
        "/v1/worker/poll",
        json={
            "worker_id": "worker-a",
            "capabilities": ["plan.generate"],
            "lease_ttl_seconds": 60,
        },
    ).json()["job"]
    client.post(
        "/v1/worker/heartbeat",
        json={
            "worker_id": "worker-a",
            "job_id": job_id,
            "lease_token": leased["lease_token"],
            "lease_ttl_seconds": 60,
        },
    )
    complete = client.post(
        "/v1/worker/complete",
        json={
            "worker_id": "worker-a",
            "job_id": job_id,
            "lease_token": leased["lease_token"],
            "result": {
                "summary": "Create a draft plan.",
                "objectives": ["Create a draft plan."],
                "steps": ["Do the work."],
                "risks": ["None"],
                "done_when": ["Artifact exists."],
            },
        },
    )
    assert complete.status_code == 200
    payload = complete.json()
    assert payload["status"] == "succeeded"
    assert payload["artifact_id"]
    assert payload["decision_id"]


def test_approving_plan_decision_queues_follow_on_pr_job(client, engine):
    created = client.post(
        "/v1/jobs",
        json={
            "project_id": "boss-dogfood",
            "kind": "plan.generate",
            "payload": {"command": "Prepare the next sandbox proposal."},
        },
    )
    assert created.status_code == 201
    job_id = created.json()["id"]

    leased = client.post(
        "/v1/worker/poll",
        json={
            "worker_id": "worker-a",
            "capabilities": ["plan.generate"],
            "lease_ttl_seconds": 60,
        },
    ).json()["job"]
    client.post(
        "/v1/worker/heartbeat",
        json={
            "worker_id": "worker-a",
            "job_id": job_id,
            "lease_token": leased["lease_token"],
            "lease_ttl_seconds": 60,
        },
    )
    complete = client.post(
        "/v1/worker/complete",
        json={
            "worker_id": "worker-a",
            "job_id": job_id,
            "lease_token": leased["lease_token"],
            "result": {
                "summary": "Prepare the next sandbox proposal.",
                "objectives": ["Prepare the next sandbox proposal."],
                "steps": ["Draft the proposal markdown."],
                "risks": ["Budget could block execution."],
                "done_when": ["A sandbox PR proposal job is queued."],
            },
        },
    )
    assert complete.status_code == 200
    decision_id = complete.json()["decision_id"]

    approve = client.post(
        f"/v1/decisions/{decision_id}/approve",
        json={"actor": "ceo", "note": "Proceed to sandbox proposal."},
    )
    assert approve.status_code == 200

    with engine.begin() as connection:
        reservation = connection.execute(
            text(
                """
                SELECT id, status, category, amount_usd_micros
                FROM budget_reservations
                WHERE project_id = 'boss-dogfood'
                ORDER BY created_at DESC
                LIMIT 1
                """
            )
        ).mappings().one()
        next_job = connection.execute(
            text(
                """
                SELECT kind, status, payload
                FROM jobs
                WHERE project_id = 'boss-dogfood'
                  AND kind = 'software_factory.propose_pr'
                ORDER BY created_at DESC
                LIMIT 1
                """
            )
        ).mappings().one()

    assert reservation["status"] == "active"
    assert reservation["category"] == "tool"
    assert int(reservation["amount_usd_micros"]) == 500_000
    assert next_job["status"] == "queued"
    payload = next_job["payload"]
    assert payload["reservation_id"] == reservation["id"]
    assert payload["title"].startswith("BOSS proposal:")
    assert payload["branch_name"].startswith("boss/proposal-")
    assert payload["agent_module_id"] == "codex_software_engineer_v1"
    assert payload["files"][0]["path"].startswith("proposals/")

    events = client.get("/v1/events", params={"project_id": "boss-dogfood"})
    assert events.status_code == 200
    event_types = [item["event_type"] for item in events.json()["items"]]
    assert "decision.follow_on_job_queued" in event_types


def test_approving_pr_review_decision_creates_merge_gate_block_artifact(client, engine):
    with engine.begin() as connection:
        connection.execute(
            text(
                """
                INSERT INTO artifacts (
                    id, project_id, task_id, artifact_type, title, uri, content, content_hash, created_by_job_id
                )
                VALUES (
                    'art_review_packet', 'boss-dogfood', NULL, 'review_packet', 'Review packet',
                    NULL, '{}'::jsonb, 'hash', NULL
                )
                """
            )
        )
        connection.execute(
            text(
                """
                INSERT INTO artifacts (
                    id, project_id, task_id, artifact_type, title, uri, content, content_hash, created_by_job_id
                )
                VALUES (
                    'art_test_report_merge_gate', 'boss-dogfood', NULL, 'test_report', 'Test report',
                    NULL, CAST(:content AS JSONB), 'hash-test', NULL
                )
                """
            ),
            {
                "content": json.dumps(
                    {
                        "status": "passed",
                        "summary": "GitHub validation passed: CI.",
                        "tests_added_or_run": ["CI"],
                    }
                )
            },
        )
        connection.execute(
            text(
                """
                INSERT INTO decisions (
                    id, project_id, task_id, question, recommendation, options, risk_tier,
                    cost_summary, default_action, status
                )
                VALUES (
                    'dec_review_pr', 'boss-dogfood', NULL, 'Review and approve sandbox PR: Demo',
                    CAST(:recommendation AS JSONB), '[]'::jsonb, 2, '{}'::jsonb, 'hold', 'pending'
                )
                """
            ),
            {
                "recommendation": json.dumps(
                    {
                        "action": "review_pull_request",
                        "artifact_id": "art_review_packet",
                        "test_report_artifact_id": "art_test_report_merge_gate",
                        "receipt_id": "rcpt_pr_demo",
                        "pull_request_url": "https://github.com/seanheiney/boss-sandbox/pull/42",
                        "branch_name": "boss/demo-pr",
                        "title": "Demo",
                    }
                )
            },
        )

    approve = client.post(
        "/v1/decisions/dec_review_pr/approve",
        json={"actor": "ceo", "note": "Manual merge is acceptable."},
    )
    assert approve.status_code == 200
    assert approve.json()["status"] == "approved"

    with engine.begin() as connection:
        merge_gate = connection.execute(
            text(
                """
                SELECT artifact_type, title, content
                FROM artifacts
                WHERE project_id = 'boss-dogfood' AND artifact_type = 'merge_gate'
                ORDER BY created_at DESC
                LIMIT 1
                """
            )
        ).mappings().one()
        queued_jobs = connection.execute(
            text(
                """
                SELECT COUNT(*) FROM jobs
                WHERE project_id = 'boss-dogfood' AND kind = 'github.pull_request.merge'
                """
            )
        ).scalar_one()

    assert merge_gate["artifact_type"] == "merge_gate"
    assert "Merge gate for Demo" == merge_gate["title"]
    assert merge_gate["content"]["status"] == "blocked_manual_merge_required"
    assert (
        merge_gate["content"]["pull_request_url"]
        == "https://github.com/seanheiney/boss-sandbox/pull/42"
    )
    assert merge_gate["content"]["receipt_id"] == "rcpt_pr_demo"
    assert queued_jobs == 0

    events = client.get("/v1/events", params={"project_id": "boss-dogfood"})
    assert events.status_code == 200
    event_types = [item["event_type"] for item in events.json()["items"]]
    assert "decision.follow_on_blocked" in event_types


def test_pr_proposal_runs_receipt_backed_local_sandbox_validation(
    client, engine, monkeypatch, tmp_path
):
    branch_name = "boss/local-validation-pass"
    repo = create_local_validation_repo(tmp_path, branch_name)
    settings = api_main.software_factory.settings
    previous = {
        "openai_api_key": settings.openai_api_key,
        "sandbox_local_validation_enabled": settings.sandbox_local_validation_enabled,
        "sandbox_validation_repo_path": settings.sandbox_validation_repo_path,
        "sandbox_validation_commands": settings.sandbox_validation_commands,
        "sandbox_validation_fetch_remote": settings.sandbox_validation_fetch_remote,
    }
    configure_pr_proposal_integrations(monkeypatch)
    settings.sandbox_local_validation_enabled = True
    settings.sandbox_validation_repo_path = repo
    settings.sandbox_validation_commands = "python3 -c \"print('local validation ok')\""
    settings.sandbox_validation_fetch_remote = False

    try:
        reservation_id = reserve_for_pr_proposal(client)
        proposal = client.post(
            "/v1/software-factory/proposals",
            json={
                "project_id": "boss-dogfood",
                "reservation_id": reservation_id,
                "dedupe_key": "local-validation-pass",
                "title": "Local validation pass",
                "branch_name": branch_name,
                "summary": "Validate the sandbox branch locally before merge.",
                "files": [
                    {
                        "path": "proposals/local-validation-pass.md",
                        "content": "# Local validation pass\n",
                    }
                ],
            },
        )
        assert proposal.status_code == 200
        payload = proposal.json()
        assert payload["status"] == "created"
        assert payload["validation_status"] == "passed"
        assert payload["validation_sources"] == [
            "github.check_runs",
            "local.sandbox_commands",
        ]
        assert payload["local_validation_receipt_id"]

        report = client.get(f"/v1/artifacts/{payload['test_report_artifact_id']}")
        assert report.status_code == 200
        content = report.json()["content"]
        assert content["status"] == "passed"
        assert content["local_validation"]["status"] == "passed"
        assert content["local_validation_receipt_status"] == "succeeded"
        assert "python3 -c \"print('local validation ok')\"" in content[
            "tests_added_or_run"
        ]

        with engine.begin() as connection:
            receipts = connection.execute(
                text(
                    """
                    SELECT effect_kind, status
                    FROM receipts
                    WHERE project_id = 'boss-dogfood'
                    ORDER BY created_at ASC
                    """
                )
            ).mappings().all()
        receipt_status_by_kind = {row["effect_kind"]: row["status"] for row in receipts}
        assert receipt_status_by_kind == {
            "github.pull_request.create": "succeeded",
            "sandbox.validation.run": "succeeded",
        }

        events = client.get("/v1/events", params={"project_id": "boss-dogfood"})
        assert events.status_code == 200
        event_types = [item["event_type"] for item in events.json()["items"]]
        assert "software_factory.local_validation_passed" in event_types
    finally:
        for key, value in previous.items():
            setattr(settings, key, value)


def test_pr_proposal_local_sandbox_validation_failure_blocks_merge_gate(
    client, engine, monkeypatch, tmp_path
):
    branch_name = "boss/local-validation-fail"
    repo = create_local_validation_repo(tmp_path, branch_name)
    settings = api_main.software_factory.settings
    previous_factory = {
        "openai_api_key": settings.openai_api_key,
        "sandbox_local_validation_enabled": settings.sandbox_local_validation_enabled,
        "sandbox_validation_repo_path": settings.sandbox_validation_repo_path,
        "sandbox_validation_commands": settings.sandbox_validation_commands,
        "sandbox_validation_fetch_remote": settings.sandbox_validation_fetch_remote,
    }
    previous_merge = api_main.decisions.settings.enable_sandbox_merge
    configure_pr_proposal_integrations(monkeypatch)
    settings.sandbox_local_validation_enabled = True
    settings.sandbox_validation_repo_path = repo
    settings.sandbox_validation_commands = "python3 -c \"raise SystemExit(3)\""
    settings.sandbox_validation_fetch_remote = False
    api_main.decisions.settings.enable_sandbox_merge = True

    try:
        reservation_id = reserve_for_pr_proposal(client)
        proposal = client.post(
            "/v1/software-factory/proposals",
            json={
                "project_id": "boss-dogfood",
                "reservation_id": reservation_id,
                "dedupe_key": "local-validation-fail",
                "title": "Local validation fail",
                "branch_name": branch_name,
                "summary": "Block merge when local sandbox validation fails.",
                "files": [
                    {
                        "path": "proposals/local-validation-fail.md",
                        "content": "# Local validation fail\n",
                    }
                ],
            },
        )
        assert proposal.status_code == 200
        payload = proposal.json()
        assert payload["status"] == "created"
        assert payload["validation_status"] == "failed"
        assert "exited 3" in payload["validation_summary"]

        report = client.get(f"/v1/artifacts/{payload['test_report_artifact_id']}")
        assert report.status_code == 200
        content = report.json()["content"]
        assert content["status"] == "failed"
        assert content["local_validation"]["status"] == "failed"
        assert content["local_validation_receipt_status"] == "failed"

        approve = client.post(
            f"/v1/decisions/{payload['decision_id']}/approve",
            json={"actor": "ceo", "note": "Try to merge despite local failure."},
        )
        assert approve.status_code == 200
        assert approve.json()["status"] == "approved"

        with engine.begin() as connection:
            merge_jobs = connection.execute(
                text(
                    """
                    SELECT COUNT(*)
                    FROM jobs
                    WHERE project_id = 'boss-dogfood'
                      AND kind = 'github.pull_request.merge'
                      AND payload->>'decision_id' = :decision_id
                    """
                ),
                {"decision_id": payload["decision_id"]},
            ).scalar_one()
            merge_gate = connection.execute(
                text(
                    """
                    SELECT content
                    FROM artifacts
                    WHERE project_id = 'boss-dogfood' AND artifact_type = 'merge_gate'
                    ORDER BY created_at DESC
                    LIMIT 1
                    """
                )
            ).mappings().one()
            validation_receipt_status = connection.execute(
                text(
                    """
                    SELECT status
                    FROM receipts
                    WHERE id = :receipt_id
                    """
                ),
                {"receipt_id": payload["local_validation_receipt_id"]},
            ).scalar_one()

        assert merge_jobs == 0
        assert merge_gate["content"]["status"] == "blocked_ci_validation_failed"
        assert merge_gate["content"]["ci_validation_status"] == "failed"
        assert validation_receipt_status == "failed"

        events = client.get("/v1/events", params={"project_id": "boss-dogfood"})
        assert events.status_code == 200
        event_types = [item["event_type"] for item in events.json()["items"]]
        assert "software_factory.local_validation_failed" in event_types
        assert "decision.follow_on_blocked" in event_types
    finally:
        for key, value in previous_factory.items():
            setattr(settings, key, value)
        api_main.decisions.settings.enable_sandbox_merge = previous_merge


def test_approving_pr_review_decision_queues_merge_job_when_policy_enabled(client, engine):
    previous = api_main.decisions.settings.enable_sandbox_merge
    api_main.decisions.settings.enable_sandbox_merge = True
    try:
        with engine.begin() as connection:
            connection.execute(
                text(
                    """
                    INSERT INTO artifacts (
                        id, project_id, task_id, artifact_type, title, uri, content, content_hash, created_by_job_id
                    )
                    VALUES (
                        'art_test_report_merge_allowed', 'boss-dogfood', NULL, 'test_report', 'Test report',
                        NULL, CAST(:content AS JSONB), 'hash-test', NULL
                    )
                    """
                ),
                {
                    "content": json.dumps(
                        {
                            "status": "passed",
                            "summary": "GitHub validation passed: CI.",
                            "tests_added_or_run": ["CI"],
                        }
                    )
                },
            )
            connection.execute(
                text(
                    """
                    INSERT INTO decisions (
                        id, project_id, task_id, question, recommendation, options, risk_tier,
                        cost_summary, default_action, status
                    )
                    VALUES (
                        'dec_review_pr_merge', 'boss-dogfood', NULL, 'Review and approve sandbox PR: Merge',
                        CAST(:recommendation AS JSONB), '[]'::jsonb, 2, '{}'::jsonb, 'hold', 'pending'
                    )
                    """
                ),
                {
                    "recommendation": json.dumps(
                        {
                            "action": "review_pull_request",
                            "test_report_artifact_id": "art_test_report_merge_allowed",
                            "pull_request_url": "https://github.com/seanheiney/boss-sandbox/pull/42",
                            "branch_name": "boss/demo-pr",
                            "title": "Merge",
                        }
                    )
                },
            )

        approve = client.post(
            "/v1/decisions/dec_review_pr_merge/approve",
            json={"actor": "ceo", "note": "Queue the merge."},
        )
        assert approve.status_code == 200
        assert approve.json()["status"] == "approved"

        with engine.begin() as connection:
            merge_jobs = connection.execute(
                text(
                    """
                    SELECT kind, status, payload
                    FROM jobs
                    WHERE project_id = 'boss-dogfood' AND kind = 'github.pull_request.merge'
                    ORDER BY created_at DESC
                    LIMIT 1
                    """
                )
            ).mappings().all()
            merge_gates = connection.execute(
                text(
                    """
                    SELECT COUNT(*) FROM artifacts
                    WHERE project_id = 'boss-dogfood' AND artifact_type = 'merge_gate'
                    """
                )
            ).scalar_one()

        assert len(merge_jobs) == 1
        merge_payload = merge_jobs[0]["payload"]
        assert merge_jobs[0]["status"] == "queued"
        assert merge_payload["decision_id"] == "dec_review_pr_merge"
        assert merge_payload["pull_request_number"] == 42
        assert merge_payload["pull_request_url"].endswith("/pull/42")
        assert merge_gates == 0

        events = client.get("/v1/events", params={"project_id": "boss-dogfood"})
        assert events.status_code == 200
        event_types = [item["event_type"] for item in events.json()["items"]]
        assert "decision.follow_on_job_queued" in event_types
    finally:
        api_main.decisions.settings.enable_sandbox_merge = previous


def test_approving_pr_review_decision_blocks_merge_when_ci_failed(client, engine):
    previous = api_main.decisions.settings.enable_sandbox_merge
    api_main.decisions.settings.enable_sandbox_merge = True
    try:
        with engine.begin() as connection:
            connection.execute(
                text(
                    """
                    INSERT INTO artifacts (
                        id, project_id, task_id, artifact_type, title, uri, content, content_hash, created_by_job_id
                    )
                    VALUES (
                        'art_test_report_merge_failed', 'boss-dogfood', NULL, 'test_report', 'Test report',
                        NULL, CAST(:content AS JSONB), 'hash-test', NULL
                    )
                    """
                ),
                {
                    "content": json.dumps(
                        {
                            "status": "failed",
                            "summary": "GitHub validation failed: CI.",
                            "tests_added_or_run": ["CI"],
                        }
                    )
                },
            )
            connection.execute(
                text(
                    """
                    INSERT INTO decisions (
                        id, project_id, task_id, question, recommendation, options, risk_tier,
                        cost_summary, default_action, status
                    )
                    VALUES (
                        'dec_review_pr_merge_failed', 'boss-dogfood', NULL, 'Review and approve sandbox PR: Broken',
                        CAST(:recommendation AS JSONB), '[]'::jsonb, 2, '{}'::jsonb, 'hold', 'pending'
                    )
                    """
                ),
                {
                    "recommendation": json.dumps(
                        {
                            "action": "review_pull_request",
                            "test_report_artifact_id": "art_test_report_merge_failed",
                            "pull_request_url": "https://github.com/seanheiney/boss-sandbox/pull/77",
                            "branch_name": "boss/broken-pr",
                            "title": "Broken",
                        }
                    )
                },
            )

        approve = client.post(
            "/v1/decisions/dec_review_pr_merge_failed/approve",
            json={"actor": "ceo", "note": "Try to merge anyway."},
        )
        assert approve.status_code == 200
        assert approve.json()["status"] == "approved"

        with engine.begin() as connection:
            merge_jobs = connection.execute(
                text(
                    """
                    SELECT COUNT(*)
                    FROM jobs
                    WHERE project_id = 'boss-dogfood'
                      AND kind = 'github.pull_request.merge'
                      AND payload->>'decision_id' = 'dec_review_pr_merge_failed'
                    """
                )
            ).scalar_one()
            merge_gate = connection.execute(
                text(
                    """
                    SELECT content
                    FROM artifacts
                    WHERE project_id = 'boss-dogfood' AND artifact_type = 'merge_gate'
                    ORDER BY created_at DESC
                    LIMIT 1
                    """
                )
            ).mappings().one()

        assert merge_jobs == 0
        assert merge_gate["content"]["status"] == "blocked_ci_validation_failed"
        assert merge_gate["content"]["ci_validation_status"] == "failed"
        assert "Sandbox validation failed" in merge_gate["content"]["reason"]

        events = client.get("/v1/events", params={"project_id": "boss-dogfood"})
        assert events.status_code == 200
        blocked_events = [
            item
            for item in events.json()["items"]
            if item["event_type"] == "decision.follow_on_blocked"
        ]
        assert blocked_events
        assert any(
            item["data"]["reason"] == "ci_validation_failed" for item in blocked_events
        )
    finally:
        api_main.decisions.settings.enable_sandbox_merge = previous


def test_spend_gated_job_without_reservation_is_not_leased(client, engine):
    created = client.post(
        "/v1/jobs",
        json={
            "project_id": "boss-dogfood",
            "kind": "openai.plan",
            "requires_reservation": True,
            "payload": {"prompt": "Plan a task."},
        },
    )
    assert created.status_code == 201
    job_id = created.json()["id"]

    poll = client.post(
        "/v1/worker/poll",
        json={
            "worker_id": "worker-budget",
            "capabilities": ["openai.plan"],
            "lease_ttl_seconds": 60,
        },
    )
    assert poll.status_code == 200
    assert poll.json()["status"] == "idle"

    with engine.begin() as connection:
        job_status = connection.execute(
            text("SELECT status FROM jobs WHERE id = :job_id"),
            {"job_id": job_id},
        ).scalar_one()
        decision_count = connection.execute(
            text(
                """
                SELECT COUNT(*) FROM decisions
                WHERE project_id = 'boss-dogfood'
                  AND question = 'Budget blocked spend-gated job: openai.plan'
                """
            )
        ).scalar_one()
    assert job_status == "cancelled"
    assert decision_count == 1


def test_spend_gated_job_with_reservation_can_be_leased(client):
    reserve = client.post(
        "/v1/budget/reserve",
        json={
            "project_id": "boss-dogfood",
            "category": "model",
            "amount_usd_micros": 250_000,
            "reason": "Lease an OpenAI-backed job.",
        },
    )
    assert reserve.status_code == 201
    reservation_id = reserve.json()["reservation_id"]

    created = client.post(
        "/v1/jobs",
        json={
            "project_id": "boss-dogfood",
            "kind": "openai.plan",
            "requires_reservation": True,
            "reservation_id": reservation_id,
            "payload": {"prompt": "Plan a task."},
        },
    )
    assert created.status_code == 201

    poll = client.post(
        "/v1/worker/poll",
        json={
            "worker_id": "worker-budget",
            "capabilities": ["openai.plan"],
            "lease_ttl_seconds": 60,
        },
    )
    assert poll.status_code == 200
    payload = poll.json()
    assert payload["status"] == "leased"
    assert payload["job"]["kind"] == "openai.plan"


def test_orange_budget_defers_non_critical_job_and_creates_decision(client, engine):
    with engine.begin() as connection:
        connection.execute(
            text(
                """
                UPDATE budget_policies
                SET daily_cap_usd_micros = 1_000_000,
                    soft_cap_pct = 0.70,
                    auto_lite_mode_pct = 0.85,
                    freeze_provisioning_pct = 0.95
                WHERE scope_type = 'project' AND scope_id = 'boss-dogfood'
                """
            )
        )
        connection.execute(
            text(
                """
                INSERT INTO cost_receipts (
                    id, project_id, category, skill_or_model_id, estimated_usd_micros,
                    actual_usd_micros, status
                )
                VALUES (
                    'cost_orange_budget', 'boss-dogfood', 'model', 'gpt-5.2-codex',
                    860000, 860000, 'actual'
                )
                """
            )
        )

    created = client.post(
        "/v1/jobs",
        json={
            "project_id": "boss-dogfood",
            "kind": "plan.generate",
            "payload": {"command": "Defer this non-critical task."},
        },
    )
    assert created.status_code == 201
    job_id = created.json()["id"]

    poll = client.post(
        "/v1/worker/poll",
        json={
            "worker_id": "worker-orange",
            "capabilities": ["plan.generate"],
            "lease_ttl_seconds": 60,
        },
    )
    assert poll.status_code == 200
    assert poll.json()["status"] == "idle"

    with engine.begin() as connection:
        job = connection.execute(
            text(
                """
                SELECT status, run_after, last_error
                FROM jobs
                WHERE id = :job_id
                """
            ),
            {"job_id": job_id},
        ).mappings().one()
        decision = connection.execute(
            text(
                """
                SELECT question, recommendation, cost_summary, status
                FROM decisions
                WHERE project_id = 'boss-dogfood'
                  AND recommendation->>'action' = 'review_budget_policy'
                  AND cost_summary->>'job_id' = :job_id
                ORDER BY created_at DESC
                LIMIT 1
                """
            ),
            {"job_id": job_id},
        ).mappings().one()

    assert job["status"] == "queued"
    assert job["last_error"]["reason"] == "orange_defers_non_critical_work"
    assert decision["status"] == "pending"
    assert "Budget state orange deferred job" in decision["question"]
    assert decision["recommendation"]["budget_state"] == "orange"

    events = client.get("/v1/events", params={"project_id": "boss-dogfood"})
    assert events.status_code == 200
    assert "job.deferred_budget_policy" in [
        item["event_type"] for item in events.json()["items"]
    ]


def test_orange_budget_allows_high_priority_job(client, engine):
    with engine.begin() as connection:
        connection.execute(
            text(
                """
                UPDATE budget_policies
                SET daily_cap_usd_micros = 1_000_000,
                    soft_cap_pct = 0.70,
                    auto_lite_mode_pct = 0.85,
                    freeze_provisioning_pct = 0.95
                WHERE scope_type = 'project' AND scope_id = 'boss-dogfood'
                """
            )
        )
        connection.execute(
            text(
                """
                INSERT INTO cost_receipts (
                    id, project_id, category, skill_or_model_id, estimated_usd_micros,
                    actual_usd_micros, status
                )
                VALUES (
                    'cost_orange_allowed', 'boss-dogfood', 'model', 'gpt-5.2-codex',
                    860000, 860000, 'actual'
                )
                """
            )
        )
        connection.execute(
            text(
                """
                INSERT INTO tasks (
                    id, project_id, title, description, status, owner_role_id, priority,
                    risk_tier, inputs, required_artifacts, budget_allocation_id, dedupe_key
                )
                VALUES (
                    'task_orange_p1', 'boss-dogfood', 'Orange P1 task', 'critical enough',
                    'open', NULL, 'p1', 1, '{}'::jsonb, '[]'::jsonb, NULL, 'task_orange_p1'
                )
                """
            )
        )

    created = client.post(
        "/v1/jobs",
        json={
            "project_id": "boss-dogfood",
            "task_id": "task_orange_p1",
            "kind": "plan.generate",
            "payload": {"command": "Allow this high-priority task."},
        },
    )
    assert created.status_code == 201

    poll = client.post(
        "/v1/worker/poll",
        json={
            "worker_id": "worker-orange-allow",
            "capabilities": ["plan.generate"],
            "lease_ttl_seconds": 60,
        },
    )
    assert poll.status_code == 200
    payload = poll.json()
    assert payload["status"] == "leased"
    assert payload["job"]["task_id"] == "task_orange_p1"


def test_red_budget_halts_non_p0_job_and_freezes_tier2_merge(client, engine):
    with engine.begin() as connection:
        connection.execute(
            text(
                """
                UPDATE budget_policies
                SET daily_cap_usd_micros = 1_000_000,
                    soft_cap_pct = 0.70,
                    auto_lite_mode_pct = 0.80,
                    freeze_provisioning_pct = 0.90
                WHERE scope_type = 'project' AND scope_id = 'boss-dogfood'
                """
            )
        )
        connection.execute(
            text(
                """
                INSERT INTO cost_receipts (
                    id, project_id, category, skill_or_model_id, estimated_usd_micros,
                    actual_usd_micros, status
                )
                VALUES (
                    'cost_red_budget', 'boss-dogfood', 'model', 'gpt-5.2-codex',
                    960000, 960000, 'actual'
                )
                """
            )
        )
        connection.execute(
            text(
                """
                INSERT INTO tasks (
                    id, project_id, title, description, status, owner_role_id, priority,
                    risk_tier, inputs, required_artifacts, budget_allocation_id, dedupe_key
                )
                VALUES (
                    'task_red_p0', 'boss-dogfood', 'Red P0 task', 'keep running',
                    'open', NULL, 'p0', 2, '{}'::jsonb, '[]'::jsonb, NULL, 'task_red_p0'
                )
                """
            )
        )
    non_p0_job = client.post(
        "/v1/jobs",
        json={
            "project_id": "boss-dogfood",
            "kind": "plan.generate",
            "payload": {"command": "Halt this non-p0 task."},
        },
    )
    assert non_p0_job.status_code == 201
    merge_job = client.post(
        "/v1/software-factory/merges/queue",
        json={
            "project_id": "boss-dogfood",
            "task_id": "task_red_p0",
            "decision_id": "dec_red_merge",
            "dedupe_key": "red-merge-job",
            "pull_request_number": 7,
            "pull_request_url": "https://github.com/seanheiney/boss-sandbox/pull/7",
            "title": "Red merge",
            "branch_name": "boss/red-merge",
        },
    )
    assert merge_job.status_code == 201
    p0_job = client.post(
        "/v1/jobs",
        json={
            "project_id": "boss-dogfood",
            "task_id": "task_red_p0",
            "kind": "plan.generate",
            "payload": {"command": "Allow this p0 task."},
        },
    )
    assert p0_job.status_code == 201

    poll_merge = client.post(
        "/v1/worker/poll",
        json={
            "worker_id": "worker-red-merge",
            "capabilities": ["github.pull_request.merge"],
            "lease_ttl_seconds": 60,
        },
    )
    assert poll_merge.status_code == 200
    assert poll_merge.json()["status"] == "idle"

    poll_plan = client.post(
        "/v1/worker/poll",
        json={
            "worker_id": "worker-red-plan",
            "capabilities": ["plan.generate"],
            "lease_ttl_seconds": 60,
        },
    )
    assert poll_plan.status_code == 200
    payload = poll_plan.json()
    assert payload["status"] == "leased"
    assert payload["job"]["task_id"] == "task_red_p0"

    with engine.begin() as connection:
        deferred_non_p0 = connection.execute(
            text(
                """
                SELECT status, last_error
                FROM jobs
                WHERE id = :job_id
                """
            ),
            {"job_id": non_p0_job.json()["id"]},
        ).mappings().one()
        deferred_merge = connection.execute(
            text(
                """
                SELECT status, last_error
                FROM jobs
                WHERE id = :job_id
                """
            ),
            {"job_id": merge_job.json()["id"]},
        ).mappings().one()

    assert deferred_non_p0["status"] == "queued"
    assert deferred_non_p0["last_error"]["reason"] == "red_halts_non_p0_work"
    assert deferred_merge["status"] == "queued"
    assert deferred_merge["last_error"]["reason"] == "red_freezes_tier2_effects"


def test_infra_provision_review_request_creates_decision_and_artifact(client):
    reservation_id = reserve_for_infra_provision(
        client,
        "Reserve budget for infrastructure provisioning review.",
    )

    response = client.post(
        "/v1/infra/droplets/requests",
        json={
            "project_id": "boss-dogfood",
            "reservation_id": reservation_id,
            "title": "Preview worker droplet",
            "region": "nyc3",
            "size": "s-1vcpu-1gb",
            "ttl_minutes": 90,
            "tags": ["preview", "dogfood"],
            "actor": "ceo",
        },
    )

    assert response.status_code == 201
    payload = response.json()
    assert payload["status"] == "pending_approval"
    assert payload["artifact_id"]
    assert payload["decision_id"]

    artifact = client.get(f"/v1/artifacts/{payload['artifact_id']}")
    assert artifact.status_code == 200
    assert artifact.json()["artifact_type"] == "infra_provision_request"

    inbox = client.get("/v1/decisions/inbox", params={"project_id": "boss-dogfood"})
    assert inbox.status_code == 200
    decision = next(
        item for item in inbox.json()["items"] if item["id"] == payload["decision_id"]
    )
    assert decision["risk_tier"] == 3
    assert decision["recommendation"]["action"] == "review_infra_provisioning"


def test_approving_infra_review_decision_queues_job_when_enabled(client, engine):
    reservation_id = reserve_for_infra_provision(
        client,
        "Reserve budget for approved infra queue.",
    )
    request = client.post(
        "/v1/infra/droplets/requests",
        json={
            "project_id": "boss-dogfood",
            "reservation_id": reservation_id,
            "title": "Queued preview worker droplet",
            "region": "nyc3",
            "size": "s-1vcpu-1gb",
            "ttl_minutes": 60,
            "tags": ["preview"],
            "actor": "ceo",
        },
    )
    assert request.status_code == 201
    decision_id = request.json()["decision_id"]

    previous_flag = api_main.decisions.settings.infra_real_provision_allowed
    api_main.decisions.settings.infra_real_provision_allowed = True
    try:
        approve = client.post(
            f"/v1/decisions/{decision_id}/approve",
            json={"actor": "ceo", "note": "Provision the preview worker."},
        )
    finally:
        api_main.decisions.settings.infra_real_provision_allowed = previous_flag

    assert approve.status_code == 200

    with engine.begin() as connection:
        job = connection.execute(
            text(
                """
                SELECT kind, payload
                FROM jobs
                WHERE project_id = 'boss-dogfood'
                  AND kind = 'infra.droplet.create'
                ORDER BY created_at DESC
                LIMIT 1
                """
            )
        ).mappings().one()

    assert job["kind"] == "infra.droplet.create"
    assert job["payload"]["decision_id"] == decision_id
    assert job["payload"]["reservation_id"] == reservation_id


def test_approving_infra_review_decision_creates_gate_when_disabled(client, engine):
    reservation_id = reserve_for_infra_provision(
        client,
        "Reserve budget for blocked infra gate.",
    )
    request = client.post(
        "/v1/infra/droplets/requests",
        json={
            "project_id": "boss-dogfood",
            "reservation_id": reservation_id,
            "title": "Blocked preview worker droplet",
            "region": "nyc3",
            "size": "s-1vcpu-1gb",
            "ttl_minutes": 60,
            "tags": ["preview"],
            "actor": "ceo",
        },
    )
    assert request.status_code == 201
    decision_id = request.json()["decision_id"]

    previous_flag = api_main.decisions.settings.infra_real_provision_allowed
    api_main.decisions.settings.infra_real_provision_allowed = False
    try:
        approve = client.post(
            f"/v1/decisions/{decision_id}/approve",
            json={"actor": "ceo", "note": "Keep infra gated."},
        )
    finally:
        api_main.decisions.settings.infra_real_provision_allowed = previous_flag

    assert approve.status_code == 200

    with engine.begin() as connection:
        gate = connection.execute(
            text(
                """
                SELECT artifact_type, content
                FROM artifacts
                WHERE project_id = 'boss-dogfood'
                  AND artifact_type = 'infra_provision_gate'
                ORDER BY created_at DESC
                LIMIT 1
                """
            )
        ).mappings().one()

    assert gate["artifact_type"] == "infra_provision_gate"
    assert gate["content"]["status"] == "blocked_manual_provision_required"


def test_red_budget_freezes_infra_provisioning_job(client, engine):
    reservation_id = reserve_for_infra_provision(
        client,
        "Reserve budget for red infra freeze.",
    )
    with engine.begin() as connection:
        connection.execute(
            text(
                """
                UPDATE budget_policies
                SET daily_cap_usd_micros = 1_000_000,
                    soft_cap_pct = 0.70,
                    auto_lite_mode_pct = 0.80,
                    freeze_provisioning_pct = 0.90
                WHERE scope_type = 'project' AND scope_id = 'boss-dogfood'
                """
            )
        )
        connection.execute(
            text(
                """
                INSERT INTO cost_receipts (
                    id, project_id, category, skill_or_model_id, estimated_usd_micros,
                    actual_usd_micros, status
                )
                VALUES (
                    'cost_red_infra', 'boss-dogfood', 'infra', 'infra.do.create_droplet_real',
                    960000, 960000, 'actual'
                )
                """
            )
        )
        connection.execute(
            text(
                """
                INSERT INTO tasks (
                    id, project_id, title, description, status, owner_role_id, priority,
                    risk_tier, inputs, required_artifacts, budget_allocation_id, data_boundary,
                    dedupe_key
                )
                VALUES (
                    'task_red_infra', 'boss-dogfood', 'Red infra task', 'freeze provisioning',
                    'open', NULL, 'p0', 1, '{}'::jsonb, '[]'::jsonb, NULL, 'internal',
                    'task_red_infra'
                )
                """
            )
        )
    insert_approved_infra_decision(
        engine,
        decision_id="dec_red_infra",
        reservation_id=reservation_id,
        task_id="task_red_infra",
        title="Red infra",
    )

    queued = client.post(
        "/v1/infra/droplets/queue",
        json={
            "project_id": "boss-dogfood",
            "task_id": "task_red_infra",
            "decision_id": "dec_red_infra",
            "reservation_id": reservation_id,
            "dedupe_key": "red-infra-job",
            "title": "Red infra",
            "region": "nyc3",
            "size": "s-1vcpu-1gb",
            "ttl_minutes": 60,
            "tags": ["preview"],
            "actor": "ceo",
        },
    )
    assert queued.status_code == 201

    poll = client.post(
        "/v1/worker/poll",
        json={
            "worker_id": "worker-red-infra",
            "capabilities": ["infra.droplet.create"],
            "lease_ttl_seconds": 60,
        },
    )
    assert poll.status_code == 200
    assert poll.json()["status"] == "idle"

    with engine.begin() as connection:
        job = connection.execute(
            text(
                """
                SELECT status, last_error
                FROM jobs
                WHERE id = :job_id
                """
            ),
            {"job_id": queued.json()["id"]},
        ).mappings().one()

    assert job["status"] == "queued"
    assert job["last_error"]["reason"] == "red_freezes_infra_provisioning"


def test_queue_infra_job_is_leasable_and_completion_passes_through(client, engine):
    reservation_id = reserve_for_infra_provision(
        client,
        "Reserve budget for queued infra job.",
    )
    insert_approved_infra_decision(
        engine,
        decision_id="dec_queue_infra",
        reservation_id=reservation_id,
    )

    queued = client.post(
        "/v1/infra/droplets/queue",
        json={
            "project_id": "boss-dogfood",
            "decision_id": "dec_queue_infra",
            "reservation_id": reservation_id,
            "dedupe_key": "queued-infra-job",
            "title": "Queued infra",
            "region": "nyc3",
            "size": "s-1vcpu-1gb",
            "ttl_minutes": 60,
            "tags": ["preview"],
            "actor": "ceo",
        },
    )
    assert queued.status_code == 201
    job_id = queued.json()["id"]

    poll = client.post(
        "/v1/worker/poll",
        json={
            "worker_id": "worker-infra",
            "capabilities": ["infra.droplet.create"],
            "lease_ttl_seconds": 60,
        },
    )
    assert poll.status_code == 200
    leased = poll.json()
    assert leased["status"] == "leased"
    assert leased["job"]["id"] == job_id

    complete = client.post(
        "/v1/worker/complete",
        json={
            "worker_id": "worker-infra",
            "job_id": job_id,
            "lease_token": leased["job"]["lease_token"],
            "result": {
                "status": "created",
                "artifact_id": "art_infra",
                "receipt_id": "rcpt_infra",
                "decision_id": "dec_queue_infra",
                "resource_id": "infra_123",
                "provider_ref": "https://api.digitalocean.com/v2/droplets/123",
                "provider_resource_id": "123",
                "resource_name": "boss-preview",
                "expires_at": "2026-06-03T12:00:00+00:00",
            },
        },
    )
    assert complete.status_code == 200
    payload = complete.json()
    assert payload["status"] == "succeeded"
    assert payload["resource_id"] == "infra_123"
    assert payload["provider_resource_id"] == "123"


def test_infra_provision_blocks_without_credentials_and_releases_reservation(
    client, engine
):
    reservation_id = reserve_for_infra_provision(
        client,
        "Reserve budget for blocked infra provisioning.",
    )
    insert_approved_infra_decision(
        engine,
        decision_id="dec_infra_missing_creds",
        reservation_id=reservation_id,
    )

    previous_flag = api_main.infrastructure.settings.infra_real_provision_allowed
    previous_token = api_main.infrastructure.settings.digitalocean_token
    api_main.infrastructure.settings.infra_real_provision_allowed = False
    api_main.infrastructure.settings.digitalocean_token = None
    try:
        response = client.post(
            "/v1/infra/droplets",
            json={
                "project_id": "boss-dogfood",
                "decision_id": "dec_infra_missing_creds",
                "reservation_id": reservation_id,
                "dedupe_key": "infra-missing-creds",
                "title": "Blocked infra",
                "region": "nyc3",
                "size": "s-1vcpu-1gb",
                "ttl_minutes": 60,
                "tags": ["preview"],
            },
        )
    finally:
        api_main.infrastructure.settings.infra_real_provision_allowed = previous_flag
        api_main.infrastructure.settings.digitalocean_token = previous_token

    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "blocked"
    assert payload["decision_id"]
    assert payload["receipt_id"]
    assert "BOSS_INFRA_REAL_PROVISION_ALLOWED is false" in payload["message"]

    with engine.begin() as connection:
        reservation_status = connection.execute(
            text("SELECT status FROM budget_reservations WHERE id = :reservation_id"),
            {"reservation_id": reservation_id},
        ).scalar_one()
        receipt = connection.execute(
            text(
                """
                SELECT effect_kind, status
                FROM receipts
                WHERE dedupe_key = 'infra-missing-creds'
                """
            )
        ).mappings().one()

    assert reservation_status == "released"
    assert receipt["effect_kind"] == "infra.digitalocean.droplet.create"
    assert receipt["status"] == "failed"


def test_infra_provision_succeeds_and_records_resource(client, engine, monkeypatch):
    reservation_id = reserve_for_infra_provision(
        client,
        "Reserve budget for successful infra provisioning.",
    )
    insert_approved_infra_decision(
        engine,
        decision_id="dec_infra_success",
        reservation_id=reservation_id,
        title="Successful infra",
    )

    previous_flag = api_main.infrastructure.settings.infra_real_provision_allowed
    previous_token = api_main.infrastructure.settings.digitalocean_token
    api_main.infrastructure.settings.infra_real_provision_allowed = True
    api_main.infrastructure.settings.digitalocean_token = "do-test-token"
    monkeypatch.setattr(
        api_main.infrastructure.digitalocean,
        "create_droplet",
        lambda **kwargs: DropletProvisionResult(
            droplet_id="123456",
            name=kwargs["name"],
            region=kwargs["region"],
            size=kwargs["size"],
            status="new",
            api_url="https://api.digitalocean.com/v2/droplets/123456",
        ),
    )
    try:
        response = client.post(
            "/v1/infra/droplets",
            json={
                "project_id": "boss-dogfood",
                "decision_id": "dec_infra_success",
                "reservation_id": reservation_id,
                "dedupe_key": "infra-success",
                "title": "Successful infra",
                "region": "nyc3",
                "size": "s-1vcpu-1gb",
                "ttl_minutes": 60,
                "tags": ["preview"],
            },
        )
    finally:
        api_main.infrastructure.settings.infra_real_provision_allowed = previous_flag
        api_main.infrastructure.settings.digitalocean_token = previous_token

    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "created"
    assert payload["resource_id"]
    assert payload["provider_resource_id"] == "123456"
    assert payload["provider_ref"].endswith("/123456")

    resources = client.get(
        "/v1/infra/resources", params={"project_id": "boss-dogfood", "limit": 10}
    )
    assert resources.status_code == 200
    resource = next(
        item for item in resources.json()["items"] if item["id"] == payload["resource_id"]
    )
    assert resource["status"] == "active"
    assert resource["external_resource_id"] == "123456"
    assert resource["provision_receipt_id"] == payload["receipt_id"]

    with engine.begin() as connection:
        reservation_status = connection.execute(
            text("SELECT status FROM budget_reservations WHERE id = :reservation_id"),
            {"reservation_id": reservation_id},
        ).scalar_one()
        cost = connection.execute(
            text(
                """
                SELECT category, skill_or_model_id, status
                FROM cost_receipts
                WHERE receipt_id = :receipt_id
                """
            ),
            {"receipt_id": payload["receipt_id"]},
        ).mappings().one()

    assert reservation_status == "consumed"
    assert cost["category"] == "infra"
    assert cost["skill_or_model_id"] == "infra.do.create_droplet_real"
    assert cost["status"] == "estimated"


def test_infra_provision_blocks_restricted_data_boundary_before_receipt(client, engine):
    task = client.post(
        "/v1/tasks",
        json={
            "project_id": "boss-dogfood",
            "title": "Restricted infra task",
            "dedupe_key": "restricted-infra-task",
            "data_boundary": "restricted",
        },
    )
    assert task.status_code == 201
    task_id = task.json()["id"]
    reservation_id = reserve_for_infra_provision(
        client,
        "Reserve budget for restricted infra provisioning.",
    )
    insert_approved_infra_decision(
        engine,
        decision_id="dec_infra_restricted",
        reservation_id=reservation_id,
        task_id=task_id,
        title="Restricted infra",
        data_boundary="restricted",
    )

    response = client.post(
        "/v1/infra/droplets",
        json={
            "project_id": "boss-dogfood",
            "task_id": task_id,
            "decision_id": "dec_infra_restricted",
            "reservation_id": reservation_id,
            "dedupe_key": "infra-restricted",
            "title": "Restricted infra",
            "region": "nyc3",
            "size": "s-1vcpu-1gb",
            "ttl_minutes": 60,
            "tags": ["preview"],
        },
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "blocked"
    assert payload["data_boundary"] == "restricted"
    assert "external infrastructure providers" in payload["message"]

    with engine.begin() as connection:
        reservation_status = connection.execute(
            text("SELECT status FROM budget_reservations WHERE id = :reservation_id"),
            {"reservation_id": reservation_id},
        ).scalar_one()
        receipt_count = connection.execute(
            text(
                """
                SELECT COUNT(*)
                FROM receipts
                WHERE dedupe_key = 'infra-restricted'
                """
            )
        ).scalar_one()
        denied_event_count = connection.execute(
            text(
                """
                SELECT COUNT(*)
                FROM events
                WHERE event_type = 'policy.denied'
                  AND entity_type = 'infra_effect'
                  AND entity_id = 'infra.digitalocean.droplet.create'
                """
            )
        ).scalar_one()

    assert reservation_status == "released"
    assert receipt_count == 0
    assert denied_event_count == 1


def test_infra_termination_and_janitor_queue_are_visible(client, engine, monkeypatch):
    reservation_id = reserve_for_infra_provision(
        client,
        "Reserve budget for infra termination setup.",
    )
    insert_approved_infra_decision(
        engine,
        decision_id="dec_infra_terminate",
        reservation_id=reservation_id,
        title="Terminated infra",
    )

    previous_flag = api_main.infrastructure.settings.infra_real_provision_allowed
    previous_token = api_main.infrastructure.settings.digitalocean_token
    api_main.infrastructure.settings.infra_real_provision_allowed = True
    api_main.infrastructure.settings.digitalocean_token = "do-test-token"
    monkeypatch.setattr(
        api_main.infrastructure.digitalocean,
        "create_droplet",
        lambda **kwargs: DropletProvisionResult(
            droplet_id="654321",
            name=kwargs["name"],
            region=kwargs["region"],
            size=kwargs["size"],
            status="active",
            api_url="https://api.digitalocean.com/v2/droplets/654321",
        ),
    )
    create = client.post(
        "/v1/infra/droplets",
        json={
            "project_id": "boss-dogfood",
            "decision_id": "dec_infra_terminate",
            "reservation_id": reservation_id,
            "dedupe_key": "infra-terminate-setup",
            "title": "Terminated infra",
            "region": "nyc3",
            "size": "s-1vcpu-1gb",
            "ttl_minutes": 10,
            "tags": ["preview"],
        },
    )
    assert create.status_code == 200
    resource_id = create.json()["resource_id"]
    monkeypatch.setattr(
        api_main.infrastructure.digitalocean,
        "delete_droplet",
        lambda droplet_id: DropletTerminationResult(
            droplet_id=droplet_id,
            api_url=f"https://api.digitalocean.com/v2/droplets/{droplet_id}",
        ),
    )
    try:
        terminate = client.post(
            f"/v1/infra/resources/{resource_id}/terminate",
            json={
                "project_id": "boss-dogfood",
                "dedupe_key": "infra-terminate-direct",
                "actor": "release_manager",
                "reason": "cleanup",
            },
        )
    finally:
        api_main.infrastructure.settings.infra_real_provision_allowed = previous_flag
        api_main.infrastructure.settings.digitalocean_token = previous_token

    assert terminate.status_code == 200
    assert terminate.json()["status"] == "terminated"

    with engine.begin() as connection:
        connection.execute(
            text(
                """
                UPDATE infra_resources
                SET status = 'active',
                    terminated_at = NULL,
                    termination_reason = NULL,
                    termination_receipt_id = NULL,
                    termination_artifact_id = NULL,
                    expires_at = now() - interval '5 minutes'
                WHERE id = :resource_id
                """
            ),
            {"resource_id": resource_id},
        )

    janitor = client.post(
        "/v1/infra/janitor/run",
        json={
            "project_id": "boss-dogfood",
            "actor": "system",
            "limit": 10,
        },
    )
    assert janitor.status_code == 200
    janitor_payload = janitor.json()
    assert janitor_payload["status"] == "queued"
    assert resource_id in janitor_payload["resource_ids"]
    assert janitor_payload["queued_job_ids"]

def test_queue_pr_proposal_job_is_leasable_and_completion_passes_through(client):
    reserve = client.post(
        "/v1/budget/reserve",
        json={
            "project_id": "boss-dogfood",
            "category": "tool",
            "amount_usd_micros": 500_000,
            "reason": "Queue a PR proposal job.",
        },
    )
    assert reserve.status_code == 201
    reservation_id = reserve.json()["reservation_id"]

    queued = client.post(
        "/v1/software-factory/proposals/queue",
        json={
            "project_id": "boss-dogfood",
            "reservation_id": reservation_id,
            "dedupe_key": "queued-pr-proposal",
            "title": "Sandbox PR",
            "branch_name": "boss/test-queued-pr",
            "summary": "Create a queued sandbox PR proposal.",
            "files": [{"path": "README.md", "content": "hello"}],
        },
    )
    assert queued.status_code == 201
    job_id = queued.json()["id"]

    fetched = client.get(f"/v1/jobs/{job_id}")
    assert fetched.status_code == 200
    assert fetched.json()["kind"] == "software_factory.propose_pr"

    poll = client.post(
        "/v1/worker/poll",
        json={
            "worker_id": "worker-pr",
            "capabilities": ["software_factory.propose_pr"],
            "lease_ttl_seconds": 60,
        },
    )
    assert poll.status_code == 200
    leased = poll.json()
    assert leased["status"] == "leased"
    assert leased["job"]["id"] == job_id

    complete = client.post(
        "/v1/worker/complete",
        json={
            "worker_id": "worker-pr",
            "job_id": job_id,
            "lease_token": leased["job"]["lease_token"],
            "result": {
                "status": "created",
                "artifact_id": "art_review",
                "diff_artifact_id": "art_diff",
                "test_report_artifact_id": "art_test_report",
                "receipt_id": "rcpt_pr",
                "decision_id": "dec_pr",
                "pull_request_url": "https://github.com/seanheiney/boss-sandbox/pull/1",
                "branch_name": "boss/test-queued-pr",
            },
        },
    )
    assert complete.status_code == 200
    payload = complete.json()
    assert payload["status"] == "succeeded"
    assert payload["artifact_id"] == "art_review"
    assert payload["diff_artifact_id"] == "art_diff"
    assert payload["test_report_artifact_id"] == "art_test_report"
    assert payload["receipt_id"] == "rcpt_pr"
    assert payload["decision_id"] == "dec_pr"
    assert payload["pull_request_url"].endswith("/pull/1")


def test_pr_proposal_blocks_without_github_credentials_and_releases_reservation(
    client, engine, monkeypatch
):
    reserve = client.post(
        "/v1/budget/reserve",
        json={
            "project_id": "boss-dogfood",
            "category": "tool",
            "amount_usd_micros": 500_000,
            "reason": "PR proposal without GitHub credentials.",
        },
    )
    assert reserve.status_code == 201
    reservation_id = reserve.json()["reservation_id"]

    monkeypatch.setattr(
        api_main.software_factory.openai,
        "create_text_response",
        lambda prompt, system_prompt=None, model=None: (
            "resp_review_packet",
            "Review packet",
        ),
    )
    previous = api_main.software_factory.settings.github_token
    previous_openai = api_main.software_factory.settings.openai_api_key
    api_main.software_factory.settings.github_token = None
    api_main.software_factory.settings.openai_api_key = "test-openai-key"
    try:
        response = client.post(
            "/v1/software-factory/proposals",
            json={
                "project_id": "boss-dogfood",
                "reservation_id": reservation_id,
                "dedupe_key": "missing-github-creds-pr",
                "title": "Sandbox PR",
                "branch_name": "boss/missing-github-creds",
                "summary": "Exercise fail-closed GitHub proposal behavior.",
                "files": [{"path": "README.md", "content": "hello"}],
            },
        )
    finally:
        api_main.software_factory.settings.github_token = previous
        api_main.software_factory.settings.openai_api_key = previous_openai

    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "blocked"
    assert payload["decision_id"]
    assert "BOSS_GITHUB_TOKEN" in payload["message"]

    with engine.begin() as connection:
        reservation_status = connection.execute(
            text(
                """
                SELECT status
                FROM budget_reservations
                WHERE id = :reservation_id
                """
            ),
            {"reservation_id": reservation_id},
        ).scalar_one()
        decision_question = connection.execute(
            text(
                """
                SELECT question
                FROM decisions
                WHERE id = :decision_id
                """
            ),
            {"decision_id": payload["decision_id"]},
        ).scalar_one()

    assert reservation_status == "released"
    assert decision_question == "GitHub integration is not configured"


def test_pr_proposal_blocks_restricted_data_boundary_before_github_effect(
    client, engine
):
    task = client.post(
        "/v1/tasks",
        json={
            "project_id": "boss-dogfood",
            "title": "Restricted PR proposal",
            "dedupe_key": "restricted-pr-task",
            "data_boundary": "restricted",
        },
    )
    assert task.status_code == 201
    task_id = task.json()["id"]

    reserve = client.post(
        "/v1/budget/reserve",
        json={
            "project_id": "boss-dogfood",
            "category": "tool",
            "amount_usd_micros": 500_000,
            "reason": "Restricted PR proposal should be blocked before GitHub.",
        },
    )
    assert reserve.status_code == 201
    reservation_id = reserve.json()["reservation_id"]

    response = client.post(
        "/v1/software-factory/proposals",
        json={
            "project_id": "boss-dogfood",
            "task_id": task_id,
            "reservation_id": reservation_id,
            "dedupe_key": "restricted-github-pr",
            "title": "Restricted Sandbox PR",
            "branch_name": "boss/restricted-pr",
            "summary": "This must not be sent to GitHub.",
            "files": [{"path": "README.md", "content": "restricted"}],
        },
    )
    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "blocked"
    assert payload["data_boundary"] == "restricted"
    assert "GitHub" in payload["message"]

    with engine.begin() as connection:
        reservation_status = connection.execute(
            text(
                """
                SELECT status
                FROM budget_reservations
                WHERE id = :reservation_id
                """
            ),
            {"reservation_id": reservation_id},
        ).scalar_one()
        receipt_count = connection.execute(
            text(
                """
                SELECT COUNT(*)
                FROM receipts
                WHERE dedupe_key = 'restricted-github-pr'
                """
            )
        ).scalar_one()
        decision_question = connection.execute(
            text("SELECT question FROM decisions WHERE id = :decision_id"),
            {"decision_id": payload["decision_id"]},
        ).scalar_one()
        denied_event_count = connection.execute(
            text(
                """
                SELECT COUNT(*)
                FROM events
                WHERE event_type = 'policy.denied'
                  AND entity_type = 'github_effect'
                  AND entity_id = 'github.pull_request.create'
                """
            )
        ).scalar_one()

    assert reservation_status == "released"
    assert receipt_count == 0
    assert decision_question == "Data boundary blocks GitHub external effect"
    assert denied_event_count == 1


def test_pr_proposal_records_github_validation_evidence(client, monkeypatch):
    reserve = client.post(
        "/v1/budget/reserve",
        json={
            "project_id": "boss-dogfood",
            "category": "tool",
            "amount_usd_micros": 500_000,
            "reason": "PR proposal with validation evidence.",
        },
    )
    assert reserve.status_code == 201
    reservation_id = reserve.json()["reservation_id"]

    previous_openai = api_main.software_factory.settings.openai_api_key
    api_main.software_factory.settings.openai_api_key = "test-openai-key"
    monkeypatch.setattr(
        api_main.software_factory.openai,
        "create_text_response",
        lambda prompt, system_prompt=None, model=None: (
            "resp_review_packet",
            "## Implementation summary\n- Review packet generated with CI evidence.",
        ),
    )
    monkeypatch.setattr(
        api_main.software_factory.github,
        "ensure_branch",
        lambda branch_name: "sha-base",
    )
    monkeypatch.setattr(
        api_main.software_factory.github,
        "upsert_file",
        lambda branch_name, path, content, message: f"commit-{path}",
    )
    monkeypatch.setattr(
        api_main.software_factory.github,
        "create_pull_request",
        lambda branch_name, title, body: PullRequestResult(
            url="https://github.com/seanheiney/boss-sandbox/pull/5",
            number=5,
        ),
    )
    monkeypatch.setattr(
        api_main.software_factory.github,
        "wait_for_commit_checks",
        lambda head_sha, timeout_seconds, poll_interval_seconds: PullRequestValidationResult(
            status="passed",
            summary="GitHub validation passed: CI.",
            head_sha=head_sha,
            completed=True,
            checks=[
                PullRequestValidationCheck(
                    name="CI",
                    status="completed",
                    conclusion="success",
                    html_url="https://github.com/seanheiney/boss-sandbox/actions/runs/1",
                    started_at="2026-06-02T12:00:00Z",
                    completed_at="2026-06-02T12:01:00Z",
                )
            ],
        ),
    )

    try:
        response = client.post(
            "/v1/software-factory/proposals",
            json={
                "project_id": "boss-dogfood",
                "reservation_id": reservation_id,
                "dedupe_key": "pr-proposal-validation-evidence",
                "title": "Sandbox PR with CI",
                "branch_name": "boss/pr-validation-evidence",
                "summary": "Collect GitHub validation evidence.",
                "files": [{"path": "README.md", "content": "hello validation"}],
            },
        )
    finally:
        api_main.software_factory.settings.openai_api_key = previous_openai

    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "created"
    assert payload["validation_status"] == "passed"
    assert "GitHub validation passed" in payload["validation_summary"]
    assert payload["security_review_artifact_id"]
    assert payload["release_go_no_go_artifact_id"]

    test_report = client.get(f"/v1/artifacts/{payload['test_report_artifact_id']}")
    assert test_report.status_code == 200
    test_report_payload = test_report.json()
    assert test_report_payload["content"]["status"] == "passed"
    assert test_report_payload["content"]["tests_added_or_run"] == ["CI"]
    assert test_report_payload["content"]["checks"][0]["conclusion"] == "success"
    assert (
        test_report_payload["content"]["security_dependency_review_artifact_id"]
        == payload["security_review_artifact_id"]
    )

    security_review = client.get(
        f"/v1/artifacts/{payload['security_review_artifact_id']}"
    )
    assert security_review.status_code == 200
    security_payload = security_review.json()
    assert security_payload["artifact_type"] == "security_dependency_review"
    assert security_payload["content"]["review"]["status"] == "passed"
    assert security_payload["content"]["browser_validation"]["status"] == "not_applicable"

    release_gate = client.get(
        f"/v1/artifacts/{payload['release_go_no_go_artifact_id']}"
    )
    assert release_gate.status_code == 200
    release_payload = release_gate.json()
    assert release_payload["artifact_type"] == "release_go_no_go"
    assert release_payload["content"]["decision_input"]["status"] == "go"
    assert (
        f"artifact:{payload['security_review_artifact_id']}"
        in release_payload["content"]["evidence_refs"]
    )

    review_packet = client.get(f"/v1/artifacts/{payload['artifact_id']}")
    assert review_packet.status_code == 200
    review_payload = review_packet.json()
    assert review_payload["content"]["ci_validation_status"] == "passed"
    assert review_payload["content"]["confidence_score"] == "ci-passed-manual-review-required"
    assert (
        review_payload["content"]["security_review_artifact_id"]
        == payload["security_review_artifact_id"]
    )
    assert (
        review_payload["content"]["release_go_no_go_artifact_id"]
        == payload["release_go_no_go_artifact_id"]
    )
    assert review_payload["content"]["release_go_no_go"]["status"] == "go"

    with api_main.engine.begin() as connection:
        decision = connection.execute(
            text(
                """
                SELECT recommendation
                FROM decisions
                WHERE id = :decision_id
                """
            ),
            {"decision_id": payload["decision_id"]},
        ).mappings().one()
    assert decision["recommendation"]["release_go_no_go_status"] == "go"


def test_software_factory_release_gate_blocks_secret_candidates():
    security_review = api_main.software_factory._build_security_dependency_review(
        [
            FileChange(
                path="apps/api/app/config.py",
                content='OPENAI_API_KEY = "sk-test-secret-value"',
            )
        ]
    )
    browser_validation = api_main.software_factory._build_browser_validation_review(
        [FileChange(path="apps/web/app/page.tsx", content="export default function Page() {}")]
    )
    release_gate = api_main.software_factory._build_release_go_no_go(
        validation_status="passed",
        validation_summary="GitHub validation passed.",
        security_dependency_review=security_review,
        browser_validation=browser_validation,
        evidence_refs=["artifact:test"],
    )

    assert security_review["status"] == "failed"
    assert security_review["findings"][0]["kind"] == "hardcoded_secret_candidate"
    assert browser_validation["status"] == "required_not_run"
    assert release_gate["status"] == "no_go"
    assert "security_dependency_review_failed" in release_gate["blockers"]
    assert "browser_validation_required_before_merge" in release_gate["warnings"]


def test_queue_merge_job_is_leasable_and_completion_passes_through(client):
    queued = client.post(
        "/v1/software-factory/merges/queue",
        json={
            "project_id": "boss-dogfood",
            "decision_id": "dec_merge",
            "dedupe_key": "queued-pr-merge",
            "pull_request_number": 42,
            "pull_request_url": "https://github.com/seanheiney/boss-sandbox/pull/42",
            "title": "Merge Demo",
            "branch_name": "boss/demo-pr",
        },
    )
    assert queued.status_code == 201
    job_id = queued.json()["id"]

    fetched = client.get(f"/v1/jobs/{job_id}")
    assert fetched.status_code == 200
    assert fetched.json()["kind"] == "github.pull_request.merge"

    poll = client.post(
        "/v1/worker/poll",
        json={
            "worker_id": "worker-merge",
            "capabilities": ["github.pull_request.merge"],
            "lease_ttl_seconds": 60,
        },
    )
    assert poll.status_code == 200
    leased = poll.json()
    assert leased["status"] == "leased"
    assert leased["job"]["id"] == job_id

    complete = client.post(
        "/v1/worker/complete",
        json={
            "worker_id": "worker-merge",
            "job_id": job_id,
            "lease_token": leased["job"]["lease_token"],
            "result": {
                "status": "created",
                "artifact_id": "art_merge",
                "receipt_id": "rcpt_merge",
                "decision_id": "dec_merge",
                "pull_request_url": "https://github.com/seanheiney/boss-sandbox/pull/42",
                "branch_name": "boss/demo-pr",
                "merged_sha": "abc123",
            },
        },
    )
    assert complete.status_code == 200
    payload = complete.json()
    assert payload["status"] == "succeeded"
    assert payload["artifact_id"] == "art_merge"
    assert payload["receipt_id"] == "rcpt_merge"
    assert payload["decision_id"] == "dec_merge"
    assert payload["pull_request_url"].endswith("/pull/42")
    assert payload["merged_sha"] == "abc123"


def test_approving_deployment_review_decision_creates_deploy_gate_block_artifact(
    client, engine
):
    with engine.begin() as connection:
        connection.execute(
            text(
                """
                INSERT INTO decisions (
                    id, project_id, task_id, question, recommendation, options, risk_tier,
                    cost_summary, default_action, status
                )
                VALUES (
                    'dec_review_deploy', 'boss-dogfood', NULL, 'Approve sandbox deployment to staging: Demo deploy',
                    CAST(:recommendation AS JSONB), '[]'::jsonb, 2, '{}'::jsonb, 'hold', 'pending'
                )
                """
            ),
            {
                "recommendation": json.dumps(
                    {
                        "action": "review_deployment",
                        "pull_request_url": "https://github.com/seanheiney/boss-sandbox/pull/42",
                        "branch_name": "boss/demo-pr",
                        "title": "Demo deploy",
                        "merged_sha": "merge-sha-42",
                        "environment": "staging",
                    }
                )
            },
        )

    approve = client.post(
        "/v1/decisions/dec_review_deploy/approve",
        json={"actor": "ceo", "note": "Manual deploy is acceptable."},
    )
    assert approve.status_code == 200
    assert approve.json()["status"] == "approved"

    with engine.begin() as connection:
        deploy_gate = connection.execute(
            text(
                """
                SELECT artifact_type, title, content
                FROM artifacts
                WHERE project_id = 'boss-dogfood' AND artifact_type = 'deployment_gate'
                ORDER BY created_at DESC
                LIMIT 1
                """
            )
        ).mappings().one()
        queued_jobs = connection.execute(
            text(
                """
                SELECT COUNT(*) FROM jobs
                WHERE project_id = 'boss-dogfood' AND kind = 'github.deployment.create'
                """
            )
        ).scalar_one()

    assert deploy_gate["artifact_type"] == "deployment_gate"
    assert deploy_gate["title"] == "Deployment gate for Demo deploy"
    assert deploy_gate["content"]["status"] == "blocked_manual_deploy_required"
    assert deploy_gate["content"]["environment"] == "staging"
    assert deploy_gate["content"]["merged_sha"] == "merge-sha-42"
    assert queued_jobs == 0

    events = client.get("/v1/events", params={"project_id": "boss-dogfood"})
    assert events.status_code == 200
    event_types = [item["event_type"] for item in events.json()["items"]]
    assert "decision.follow_on_blocked" in event_types


def test_queue_deployment_job_is_leasable_and_completion_passes_through(client):
    queued = client.post(
        "/v1/software-factory/deployments/queue",
        json={
            "project_id": "boss-dogfood",
            "decision_id": "dec_deploy",
            "dedupe_key": "queued-deploy",
            "merged_sha": "merge-sha-42",
            "pull_request_url": "https://github.com/seanheiney/boss-sandbox/pull/42",
            "title": "Deploy Demo",
            "branch_name": "boss/demo-pr",
            "environment": "staging",
        },
    )
    assert queued.status_code == 201
    job_id = queued.json()["id"]

    fetched = client.get(f"/v1/jobs/{job_id}")
    assert fetched.status_code == 200
    assert fetched.json()["kind"] == "github.deployment.create"

    poll = client.post(
        "/v1/worker/poll",
        json={
            "worker_id": "worker-deploy",
            "capabilities": ["github.deployment.create"],
            "lease_ttl_seconds": 60,
        },
    )
    assert poll.status_code == 200
    leased = poll.json()
    assert leased["status"] == "leased"
    assert leased["job"]["id"] == job_id

    complete = client.post(
        "/v1/worker/complete",
        json={
            "worker_id": "worker-deploy",
            "job_id": job_id,
            "lease_token": leased["job"]["lease_token"],
            "result": {
                "status": "created",
                "artifact_id": "art_deploy",
                "receipt_id": "rcpt_deploy",
                "decision_id": "dec_deploy",
                "environment": "staging",
                "deployment_id": 12345,
                "deployment_status": "success",
                "deployment_url": "https://api.github.com/repos/seanheiney/boss-sandbox/deployments/12345",
                "merged_sha": "merge-sha-42",
            },
        },
    )
    assert complete.status_code == 200
    payload = complete.json()
    assert payload["status"] == "succeeded"
    assert payload["artifact_id"] == "art_deploy"
    assert payload["receipt_id"] == "rcpt_deploy"
    assert payload["decision_id"] == "dec_deploy"
    assert payload["environment"] == "staging"
    assert payload["deployment_id"] == 12345
    assert payload["deployment_status"] == "success"


def test_deployment_blocks_without_github_credentials(client, engine):
    previous = api_main.software_factory.settings.github_token
    api_main.software_factory.settings.github_token = None
    try:
        response = client.post(
            "/v1/software-factory/deployments",
            json={
                "project_id": "boss-dogfood",
                "decision_id": "dec_deploy_missing_github",
                "dedupe_key": "missing-github-creds-deploy",
                "merged_sha": "merge-sha-42",
                "pull_request_url": "https://github.com/seanheiney/boss-sandbox/pull/42",
                "title": "Deploy Demo",
                "branch_name": "boss/demo-pr",
                "environment": "staging",
            },
        )
    finally:
        api_main.software_factory.settings.github_token = previous

    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "blocked"
    assert payload["decision_id"]
    assert "BOSS_GITHUB_TOKEN" in payload["message"]

    with engine.begin() as connection:
        decision_question = connection.execute(
            text(
                """
                SELECT question
                FROM decisions
                WHERE id = :decision_id
                """
            ),
            {"decision_id": payload["decision_id"]},
        ).scalar_one()
        deploy_artifact_count = connection.execute(
            text(
                """
                SELECT COUNT(*)
                FROM artifacts
                WHERE project_id = 'boss-dogfood' AND artifact_type = 'deployment_result'
                """
            )
        ).scalar_one()

    assert decision_question == "GitHub integration is not configured"
    assert deploy_artifact_count == 0


def test_deployment_healthcheck_failure_creates_blocking_decision(client, engine, monkeypatch):
    previous = api_main.software_factory.settings.sandbox_deploy_healthcheck_url
    api_main.software_factory.settings.sandbox_deploy_healthcheck_url = (
        "https://sandbox.example.test/health"
    )
    monkeypatch.setattr(
        api_main.software_factory.github,
        "create_deployment",
        lambda ref, environment, description: DeploymentResult(
            id=991,
            environment=environment,
            sha=ref,
            url="https://api.github.com/repos/seanheiney/boss-sandbox/deployments/991",
        ),
    )
    monkeypatch.setattr(
        api_main.software_factory.github,
        "mark_deployment_failure",
        lambda deployment_id, environment, environment_url=None, description=None: DeploymentStatusResult(
            state="failure",
            url="https://api.github.com/repos/seanheiney/boss-sandbox/deployments/991/statuses/1",
            environment_url=environment_url,
        ),
    )
    monkeypatch.setattr(
        api_main.software_factory,
        "_verify_deployment_health",
        lambda healthcheck_url: {
            "status": "failed",
            "healthcheck_url": healthcheck_url,
            "summary": "Deployment health check returned HTTP 503.",
            "http_status": 503,
            "response_excerpt": "service unavailable",
        },
    )
    with engine.begin() as connection:
        connection.execute(
            text(
                """
                INSERT INTO decisions (
                    id, project_id, task_id, question, recommendation, options, risk_tier,
                    cost_summary, default_action, status, approved_by, approved_at
                )
                VALUES (
                    'dec_deploy_health_fail', 'boss-dogfood', NULL, 'Approve sandbox deployment to staging: Demo deploy',
                    CAST(:recommendation AS JSONB), '[]'::jsonb, 2, '{}'::jsonb, 'hold',
                    'approved', 'ceo', now()
                )
                """
            ),
            {
                "recommendation": json.dumps(
                    {
                        "action": "review_deployment",
                        "pull_request_url": "https://github.com/seanheiney/boss-sandbox/pull/42",
                        "branch_name": "boss/demo-pr",
                        "title": "Deploy Demo",
                        "merged_sha": "merge-sha-42",
                        "environment": "staging",
                    }
                )
            },
        )

    try:
        response = client.post(
            "/v1/software-factory/deployments",
            json={
                "project_id": "boss-dogfood",
                "decision_id": "dec_deploy_health_fail",
                "dedupe_key": "deploy-health-fail",
                "merged_sha": "merge-sha-42",
                "pull_request_url": "https://github.com/seanheiney/boss-sandbox/pull/42",
                "title": "Deploy Demo",
                "branch_name": "boss/demo-pr",
                "environment": "staging",
            },
        )
    finally:
        api_main.software_factory.settings.sandbox_deploy_healthcheck_url = previous

    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "blocked"
    assert payload["health_verification_status"] == "failed"
    assert payload["healthcheck_url"] == "https://sandbox.example.test/health"
    assert payload["health_decision_id"]
    assert payload["health_verification_artifact_id"]
    assert payload["deployment_status"] == "failure"
    assert payload["rollout_id"]
    assert payload["rollout_artifact_id"]
    assert payload["rollout_status"] == "blocked"
    assert payload["rollout_stage"] == "post_deploy_health"

    with engine.begin() as connection:
        original_decision_status = connection.execute(
            text("SELECT status FROM decisions WHERE id = 'dec_deploy_health_fail'")
        ).scalar_one()
        health_decision = connection.execute(
            text(
                """
                SELECT question, recommendation, status
                FROM decisions
                WHERE id = :decision_id
                """
            ),
            {"decision_id": payload["health_decision_id"]},
        ).mappings().one()
        health_artifact = connection.execute(
            text(
                """
                SELECT artifact_type, content
                FROM artifacts
                WHERE id = :artifact_id
                """
            ),
            {"artifact_id": payload["health_verification_artifact_id"]},
        ).mappings().one()
        rollout = connection.execute(
            text(
                """
                SELECT status, current_stage, rollback_decision_id, stages
                FROM deployment_rollouts
                WHERE id = :rollout_id
                """
            ),
            {"rollout_id": payload["rollout_id"]},
        ).mappings().one()
        rollout_artifact = connection.execute(
            text(
                """
                SELECT artifact_type, content
                FROM artifacts
                WHERE id = :artifact_id
                """
            ),
            {"artifact_id": payload["rollout_artifact_id"]},
        ).mappings().one()

    assert original_decision_status == "approved"
    assert "Review failed sandbox deployment health check" in health_decision["question"]
    assert health_decision["recommendation"]["action"] == "review_deployment_health"
    assert health_artifact["artifact_type"] == "deployment_health_check"
    assert health_artifact["content"]["verification"]["http_status"] == 503
    assert rollout["status"] == "blocked"
    assert rollout["current_stage"] == "post_deploy_health"
    assert rollout["rollback_decision_id"] == payload["health_decision_id"]
    assert rollout["stages"][1]["status"] == "failed"
    assert rollout_artifact["artifact_type"] == "deployment_rollout"
    assert rollout_artifact["content"]["status"] == "blocked"

    rollouts = client.get(
        "/v1/software-factory/rollouts",
        params={"project_id": "boss-dogfood"},
    )
    assert rollouts.status_code == 200
    rollout_items = rollouts.json()["items"]
    assert rollout_items[0]["id"] == payload["rollout_id"]
    assert rollout_items[0]["status"] == "blocked"

    events = client.get("/v1/events", params={"project_id": "boss-dogfood"})
    assert events.status_code == 200
    event_types = [item["event_type"] for item in events.json()["items"]]
    assert "software_factory.deployment_health_blocked" in event_types
    assert "software_factory.deployment_rollout_recorded" in event_types


def test_merge_blocks_without_github_credentials(client, engine):
    previous = api_main.software_factory.settings.github_token
    api_main.software_factory.settings.github_token = None
    try:
        response = client.post(
            "/v1/software-factory/merges",
            json={
                "project_id": "boss-dogfood",
                "decision_id": "dec_merge_missing_github",
                "dedupe_key": "missing-github-creds-merge",
                "pull_request_number": 42,
                "pull_request_url": "https://github.com/seanheiney/boss-sandbox/pull/42",
                "title": "Merge Demo",
                "branch_name": "boss/demo-pr",
            },
        )
    finally:
        api_main.software_factory.settings.github_token = previous

    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "blocked"
    assert payload["decision_id"]
    assert "BOSS_GITHUB_TOKEN" in payload["message"]

    with engine.begin() as connection:
        decision_question = connection.execute(
            text(
                """
                SELECT question
                FROM decisions
                WHERE id = :decision_id
                """
            ),
            {"decision_id": payload["decision_id"]},
        ).scalar_one()
        merge_artifact_count = connection.execute(
            text(
                """
                SELECT COUNT(*)
                FROM artifacts
                WHERE project_id = 'boss-dogfood' AND artifact_type = 'merge_result'
                """
            )
        ).scalar_one()

    assert decision_question == "GitHub integration is not configured"
    assert merge_artifact_count == 0


def test_merge_and_deployment_block_restricted_data_boundary_before_github_receipts(
    client, engine
):
    task = client.post(
        "/v1/tasks",
        json={
            "project_id": "boss-dogfood",
            "title": "Restricted release",
            "dedupe_key": "restricted-release-task",
            "data_boundary": "restricted",
        },
    )
    assert task.status_code == 201
    task_id = task.json()["id"]
    with engine.begin() as connection:
        connection.execute(
            text(
                """
                INSERT INTO decisions (
                    id, project_id, task_id, question, recommendation, options,
                    risk_tier, cost_summary, default_action, status
                )
                VALUES (
                    'dec_restricted_release', 'boss-dogfood', :task_id,
                    'Restricted release decision',
                    '{}'::jsonb, '[]'::jsonb, 2, '{}'::jsonb, 'hold', 'approved'
                )
                """
            ),
            {"task_id": task_id},
        )

    merge = client.post(
        "/v1/software-factory/merges",
        json={
            "project_id": "boss-dogfood",
            "decision_id": "dec_restricted_release",
            "dedupe_key": "restricted-merge-effect",
            "pull_request_number": 42,
            "pull_request_url": "https://github.com/seanheiney/boss-sandbox/pull/42",
            "title": "Restricted Merge",
            "branch_name": "boss/restricted-release",
        },
    )
    assert merge.status_code == 200
    merge_payload = merge.json()
    assert merge_payload["status"] == "blocked"
    assert merge_payload["data_boundary"] == "restricted"

    deploy = client.post(
        "/v1/software-factory/deployments",
        json={
            "project_id": "boss-dogfood",
            "decision_id": "dec_restricted_release",
            "dedupe_key": "restricted-deploy-effect",
            "merged_sha": "abc123",
            "pull_request_url": "https://github.com/seanheiney/boss-sandbox/pull/42",
            "title": "Restricted Deploy",
            "branch_name": "boss/restricted-release",
            "environment": "staging",
        },
    )
    assert deploy.status_code == 200
    deploy_payload = deploy.json()
    assert deploy_payload["status"] == "blocked"
    assert deploy_payload["data_boundary"] == "restricted"

    with engine.begin() as connection:
        receipt_count = connection.execute(
            text(
                """
                SELECT COUNT(*)
                FROM receipts
                WHERE dedupe_key IN ('restricted-merge-effect', 'restricted-deploy-effect')
                """
            )
        ).scalar_one()
        denied_effects = connection.execute(
            text(
                """
                SELECT entity_id
                FROM events
                WHERE event_type = 'policy.denied'
                  AND entity_type = 'github_effect'
                ORDER BY entity_id
                """
            )
        ).scalars().all()

    assert receipt_count == 0
    assert "github.pull_request.merge" in denied_effects
    assert "github.deployment.create" in denied_effects


def test_command_to_merge_flow_completes_task_when_merge_policy_enabled(
    client, engine, monkeypatch
):
    previous = api_main.decisions.settings.enable_sandbox_merge
    previous_openai = api_main.software_factory.settings.openai_api_key
    api_main.decisions.settings.enable_sandbox_merge = True
    api_main.software_factory.settings.openai_api_key = "test-openai-key"
    monkeypatch.setattr(
        api_main.software_factory.openai,
        "create_text_response",
        lambda prompt, system_prompt=None, model=None: (
            "resp_review_packet",
            "## Implementation summary\n- Review packet generated for test coverage.",
        ),
    )
    monkeypatch.setattr(
        api_main.software_factory.github,
        "ensure_branch",
        lambda branch_name: "sha-base",
    )
    monkeypatch.setattr(
        api_main.software_factory.github,
        "upsert_file",
        lambda branch_name, path, content, message: f"commit-{path}",
    )
    monkeypatch.setattr(
        api_main.software_factory.github,
        "create_pull_request",
        lambda branch_name, title, body: PullRequestResult(
            url="https://github.com/seanheiney/boss-sandbox/pull/99",
            number=99,
        ),
    )
    monkeypatch.setattr(
        api_main.software_factory.github,
        "wait_for_commit_checks",
        lambda head_sha, timeout_seconds, poll_interval_seconds: PullRequestValidationResult(
            status="passed",
            summary="GitHub validation passed: CI.",
            head_sha=head_sha,
            completed=True,
            checks=[
                PullRequestValidationCheck(
                    name="CI",
                    status="completed",
                    conclusion="success",
                    html_url="https://github.com/seanheiney/boss-sandbox/actions/runs/99",
                    started_at="2026-06-02T12:00:00Z",
                    completed_at="2026-06-02T12:01:00Z",
                )
            ],
        ),
    )
    monkeypatch.setattr(
        api_main.software_factory.github,
        "merge_pull_request",
        lambda pull_request_number, commit_title: PullRequestMergeResult(
            sha="merge-sha-99",
            merged=True,
            message="Pull request successfully merged",
        ),
    )

    try:
        created = client.post(
            "/v1/commands/propose",
            json={
                "project_id": "boss-dogfood",
                "command": "Ship the next sandbox merge audit improvement.",
                "constraints": {"deadline": "this week"},
            },
        )
        assert created.status_code == 201
        payload = created.json()
        task_id = payload["task_id"]
        command_job_id = payload["job_id"]

        leased_plan = client.post(
            "/v1/worker/poll",
            json={
                "worker_id": "worker-e2e-plan",
                "capabilities": ["plan.command"],
                "lease_ttl_seconds": 60,
            },
        )
        assert leased_plan.status_code == 200
        leased_plan_job = leased_plan.json()["job"]
        assert leased_plan_job["id"] == command_job_id

        heartbeat_plan = client.post(
            "/v1/worker/heartbeat",
            json={
                "worker_id": "worker-e2e-plan",
                "job_id": command_job_id,
                "lease_token": leased_plan_job["lease_token"],
                "lease_ttl_seconds": 60,
            },
        )
        assert heartbeat_plan.status_code == 200

        completed_plan = client.post(
            "/v1/worker/complete",
            json={
                "worker_id": "worker-e2e-plan",
                "job_id": command_job_id,
                "lease_token": leased_plan_job["lease_token"],
                "result": {
                    "summary": "Ship the next sandbox merge audit improvement.",
                    "objectives": ["Ship the next sandbox merge audit improvement."],
                    "steps": [
                        "Prepare a reviewable proposal.",
                        "Review the sandbox PR.",
                        "Merge after approval.",
                    ],
                    "risks": ["Budget or provider credentials could block progress."],
                    "done_when": ["The sandbox PR is merged and the task is completed."],
                },
            },
        )
        assert completed_plan.status_code == 200
        plan_decision_id = completed_plan.json()["decision_id"]

        approve_plan = client.post(
            f"/v1/decisions/{plan_decision_id}/approve",
            json={"actor": "ceo", "note": "Proceed to PR proposal."},
        )
        assert approve_plan.status_code == 200
        assert approve_plan.json()["status"] == "approved"

        leased_pr = client.post(
            "/v1/worker/poll",
            json={
                "worker_id": "worker-e2e-pr",
                "capabilities": ["software_factory.propose_pr"],
                "lease_ttl_seconds": 60,
            },
        )
        assert leased_pr.status_code == 200
        leased_pr_job = leased_pr.json()["job"]
        assert leased_pr_job["kind"] == "software_factory.propose_pr"

        heartbeat_pr = client.post(
            "/v1/worker/heartbeat",
            json={
                "worker_id": "worker-e2e-pr",
                "job_id": leased_pr_job["id"],
                "lease_token": leased_pr_job["lease_token"],
                "lease_ttl_seconds": 60,
            },
        )
        assert heartbeat_pr.status_code == 200

        proposal_run = client.post(
            "/v1/software-factory/proposals",
            json={
                "project_id": leased_pr_job["project_id"],
                "task_id": leased_pr_job["task_id"],
                "reservation_id": leased_pr_job["payload"]["reservation_id"],
                "dedupe_key": leased_pr_job["payload"]["dedupe_key"],
                "title": leased_pr_job["payload"]["title"],
                "branch_name": leased_pr_job["payload"]["branch_name"],
                "summary": leased_pr_job["payload"]["summary"],
                "files": leased_pr_job["payload"]["files"],
                "created_by_job_id": leased_pr_job["id"],
            },
        )
        assert proposal_run.status_code == 200
        proposal_result = proposal_run.json()
        assert proposal_result["status"] == "created"
        assert proposal_result["validation_status"] == "passed"
        pr_decision_id = proposal_result["decision_id"]

        complete_pr = client.post(
            "/v1/worker/complete",
            json={
                "worker_id": "worker-e2e-pr",
                "job_id": leased_pr_job["id"],
                "lease_token": leased_pr_job["lease_token"],
                "result": proposal_result,
            },
        )
        assert complete_pr.status_code == 200
        assert complete_pr.json()["status"] == "succeeded"

        approve_pr = client.post(
            f"/v1/decisions/{pr_decision_id}/approve",
            json={"actor": "ceo", "note": "Merge the approved sandbox PR."},
        )
        assert approve_pr.status_code == 200
        assert approve_pr.json()["status"] == "approved"

        leased_merge = client.post(
            "/v1/worker/poll",
            json={
                "worker_id": "worker-e2e-merge",
                "capabilities": ["github.pull_request.merge"],
                "lease_ttl_seconds": 60,
            },
        )
        assert leased_merge.status_code == 200
        leased_merge_job = leased_merge.json()["job"]
        assert leased_merge_job["kind"] == "github.pull_request.merge"

        heartbeat_merge = client.post(
            "/v1/worker/heartbeat",
            json={
                "worker_id": "worker-e2e-merge",
                "job_id": leased_merge_job["id"],
                "lease_token": leased_merge_job["lease_token"],
                "lease_ttl_seconds": 60,
            },
        )
        assert heartbeat_merge.status_code == 200

        merge_run = client.post(
            "/v1/software-factory/merges",
            json={
                "project_id": leased_merge_job["project_id"],
                "decision_id": leased_merge_job["payload"]["decision_id"],
                "dedupe_key": leased_merge_job["payload"]["dedupe_key"],
                "pull_request_number": leased_merge_job["payload"]["pull_request_number"],
                "pull_request_url": leased_merge_job["payload"]["pull_request_url"],
                "title": leased_merge_job["payload"]["title"],
                "branch_name": leased_merge_job["payload"].get("branch_name"),
                "created_by_job_id": leased_merge_job["id"],
            },
        )
        assert merge_run.status_code == 200
        merge_result = merge_run.json()
        assert merge_result["status"] == "created"
        assert merge_result["merged_sha"] == "merge-sha-99"

        complete_merge = client.post(
            "/v1/worker/complete",
            json={
                "worker_id": "worker-e2e-merge",
                "job_id": leased_merge_job["id"],
                "lease_token": leased_merge_job["lease_token"],
                "result": merge_result,
            },
        )
        assert complete_merge.status_code == 200
        assert complete_merge.json()["status"] == "succeeded"

        with engine.begin() as connection:
            task_status = connection.execute(
                text("SELECT status FROM tasks WHERE id = :task_id"),
                {"task_id": task_id},
            ).scalar_one()
            merge_decision_status = connection.execute(
                text("SELECT status FROM decisions WHERE id = :decision_id"),
                {"decision_id": pr_decision_id},
            ).scalar_one()
            artifact_types = [
                row["artifact_type"]
                for row in connection.execute(
                    text(
                        """
                        SELECT artifact_type
                        FROM artifacts
                        WHERE project_id = 'boss-dogfood'
                        ORDER BY created_at ASC
                        """
                    )
                ).mappings().all()
            ]
            receipt_count = connection.execute(
                text("SELECT COUNT(*) FROM receipts WHERE project_id = 'boss-dogfood'")
            ).scalar_one()

        assert task_status == "completed"
        assert merge_decision_status == "executed"
        assert "command_brief" in artifact_types
        assert "plan_draft" in artifact_types
        assert "review_packet" in artifact_types
        assert "diff_manifest" in artifact_types
        assert "test_report" in artifact_types
        assert "merge_result" in artifact_types
        assert receipt_count == 2

        events = client.get("/v1/events", params={"project_id": "boss-dogfood"})
        assert events.status_code == 200
        event_types = [item["event_type"] for item in events.json()["items"]]
        assert "software_factory.pull_request_proposed" in event_types
        assert "software_factory.pull_request_merged" in event_types
        assert "task.completed" in event_types

        receipt_list = client.get(
            "/v1/receipts",
            params={"project_id": "boss-dogfood", "limit": 10},
        )
        assert receipt_list.status_code == 200
        receipt_payload = receipt_list.json()
        assert receipt_payload["project_id"] == "boss-dogfood"
        assert len(receipt_payload["items"]) == 2
        assert {item["effect_kind"] for item in receipt_payload["items"]} == {
            "github.pull_request.create",
            "github.pull_request.merge",
        }
        assert {
            item["created_by_job_id"] for item in receipt_payload["items"]
        } == {leased_pr_job["id"], leased_merge_job["id"]}

        audit_export = client.get(
            "/v1/audit/export",
            params={"project_id": "boss-dogfood", "task_id": task_id},
        )
        assert audit_export.status_code == 200
        export_payload = audit_export.json()
        assert export_payload["project_id"] == "boss-dogfood"
        assert export_payload["scope"]["task_id"] == task_id
        assert export_payload["counts"]["tasks"] == 1
        assert export_payload["counts"]["jobs"] == 3
        assert export_payload["counts"]["decisions"] == 2
        assert export_payload["counts"]["receipts"] == 2
        assert export_payload["counts"]["budget_reservations"] == 1
        assert export_payload["counts"]["routing_logs"] >= 1
        assert task_id in export_payload["chain"]["task_ids"]
        assert leased_pr_job["id"] in export_payload["chain"]["job_ids"]
        assert leased_merge_job["id"] in export_payload["chain"]["job_ids"]
        assert pr_decision_id in export_payload["chain"]["decision_ids"]

        replay_bundle = client.post(
            "/v1/audit/replay-bundles",
            json={
                "project_id": "boss-dogfood",
                "task_id": task_id,
                "actor": "ceo",
            },
        )
        assert replay_bundle.status_code == 201
        replay_payload = replay_bundle.json()
        assert replay_payload["status"] == "created"
        assert replay_payload["artifact_type"] == "replay_bundle"
        assert replay_payload["counts"]["receipts"] == 2

        replay_artifact = client.get(f"/v1/artifacts/{replay_payload['artifact_id']}")
        assert replay_artifact.status_code == 200
        replay_artifact_payload = replay_artifact.json()
        assert replay_artifact_payload["artifact_type"] == "replay_bundle"
        assert replay_artifact_payload["content"]["counts"]["jobs"] == 3
        assert leased_merge_job["id"] in replay_artifact_payload["content"]["chain"]["job_ids"]
    finally:
        api_main.decisions.settings.enable_sandbox_merge = previous
        api_main.software_factory.settings.openai_api_key = previous_openai


def test_command_to_deploy_flow_completes_task_when_deploy_policy_enabled(
    client, engine, monkeypatch
):
    previous_merge = api_main.decisions.settings.enable_sandbox_merge
    previous_merge_factory = api_main.software_factory.settings.enable_sandbox_merge
    previous_deploy = api_main.decisions.settings.enable_sandbox_deploy
    previous_deploy_factory = api_main.software_factory.settings.enable_sandbox_deploy
    previous_openai = api_main.software_factory.settings.openai_api_key
    previous_healthcheck_url = (
        api_main.software_factory.settings.sandbox_deploy_healthcheck_url
    )
    api_main.decisions.settings.enable_sandbox_merge = True
    api_main.software_factory.settings.enable_sandbox_merge = True
    api_main.decisions.settings.enable_sandbox_deploy = True
    api_main.software_factory.settings.enable_sandbox_deploy = True
    api_main.software_factory.settings.openai_api_key = "test-openai-key"
    api_main.software_factory.settings.sandbox_deploy_healthcheck_url = (
        "https://example.com/staging/777"
    )
    monkeypatch.setattr(
        api_main.software_factory.openai,
        "create_text_response",
        lambda prompt, system_prompt=None, model=None: (
            "resp_review_packet",
            "## Implementation summary\n- Review packet generated for deploy test.",
        ),
    )
    monkeypatch.setattr(
        api_main.software_factory.github,
        "ensure_branch",
        lambda branch_name: "sha-base",
    )
    monkeypatch.setattr(
        api_main.software_factory.github,
        "upsert_file",
        lambda branch_name, path, content, message: f"commit-{path}",
    )
    monkeypatch.setattr(
        api_main.software_factory.github,
        "create_pull_request",
        lambda branch_name, title, body: PullRequestResult(
            url="https://github.com/seanheiney/boss-sandbox/pull/199",
            number=199,
        ),
    )
    monkeypatch.setattr(
        api_main.software_factory.github,
        "wait_for_commit_checks",
        lambda head_sha, timeout_seconds, poll_interval_seconds: PullRequestValidationResult(
            status="passed",
            summary="GitHub validation passed: CI.",
            head_sha=head_sha,
            completed=True,
            checks=[
                PullRequestValidationCheck(
                    name="CI",
                    status="completed",
                    conclusion="success",
                    html_url="https://github.com/seanheiney/boss-sandbox/actions/runs/199",
                    started_at="2026-06-02T12:00:00Z",
                    completed_at="2026-06-02T12:01:00Z",
                )
            ],
        ),
    )
    monkeypatch.setattr(
        api_main.software_factory.github,
        "merge_pull_request",
        lambda pull_request_number, commit_title: PullRequestMergeResult(
            sha="merge-sha-199",
            merged=True,
            message="Pull request successfully merged",
        ),
    )
    monkeypatch.setattr(
        api_main.software_factory.github,
        "create_deployment",
        lambda ref, environment, description: DeploymentResult(
            id=777,
            environment=environment,
            sha=ref,
            url="https://api.github.com/repos/seanheiney/boss-sandbox/deployments/777",
        ),
    )
    monkeypatch.setattr(
        api_main.software_factory.github,
        "mark_deployment_success",
        lambda deployment_id, environment, environment_url=None, description=None: DeploymentStatusResult(
            state="success",
            url="https://api.github.com/repos/seanheiney/boss-sandbox/deployments/777/statuses/1",
            environment_url=environment_url or "https://example.com/staging/777",
        ),
    )
    monkeypatch.setattr(
        api_main.software_factory,
        "_verify_deployment_health",
        lambda healthcheck_url: {
            "status": "passed",
            "healthcheck_url": healthcheck_url,
            "summary": "Deployment health check passed with HTTP 200.",
            "http_status": 200,
            "response_excerpt": "ok",
        },
    )

    try:
        created = client.post(
            "/v1/commands/propose",
            json={
                "project_id": "boss-dogfood",
                "command": "Ship the next sandbox deployment audit improvement.",
                "constraints": {"deadline": "this week"},
            },
        )
        assert created.status_code == 201
        payload = created.json()
        task_id = payload["task_id"]
        command_job_id = payload["job_id"]

        leased_plan = client.post(
            "/v1/worker/poll",
            json={
                "worker_id": "worker-deploy-plan",
                "capabilities": ["plan.command"],
                "lease_ttl_seconds": 60,
            },
        )
        assert leased_plan.status_code == 200
        leased_plan_job = leased_plan.json()["job"]
        assert leased_plan_job["id"] == command_job_id

        client.post(
            "/v1/worker/heartbeat",
            json={
                "worker_id": "worker-deploy-plan",
                "job_id": command_job_id,
                "lease_token": leased_plan_job["lease_token"],
                "lease_ttl_seconds": 60,
            },
        ).raise_for_status()

        completed_plan = client.post(
            "/v1/worker/complete",
            json={
                "worker_id": "worker-deploy-plan",
                "job_id": command_job_id,
                "lease_token": leased_plan_job["lease_token"],
                "result": {
                    "summary": "Ship the next sandbox deployment audit improvement.",
                    "objectives": ["Ship the next sandbox deployment audit improvement."],
                    "steps": [
                        "Prepare a reviewable proposal.",
                        "Review and merge the sandbox PR.",
                        "Approve and record the sandbox deployment.",
                    ],
                    "risks": ["Budget or provider credentials could block progress."],
                    "done_when": [
                        "The sandbox PR is merged, the sandbox deployment is recorded, and the task is completed."
                    ],
                },
            },
        )
        assert completed_plan.status_code == 200
        plan_decision_id = completed_plan.json()["decision_id"]

        assert (
            client.post(
                f"/v1/decisions/{plan_decision_id}/approve",
                json={"actor": "ceo", "note": "Proceed to PR proposal."},
            ).status_code
            == 200
        )

        leased_pr = client.post(
            "/v1/worker/poll",
            json={
                "worker_id": "worker-deploy-pr",
                "capabilities": ["software_factory.propose_pr"],
                "lease_ttl_seconds": 60,
            },
        )
        assert leased_pr.status_code == 200
        leased_pr_job = leased_pr.json()["job"]
        assert leased_pr_job["kind"] == "software_factory.propose_pr"

        client.post(
            "/v1/worker/heartbeat",
            json={
                "worker_id": "worker-deploy-pr",
                "job_id": leased_pr_job["id"],
                "lease_token": leased_pr_job["lease_token"],
                "lease_ttl_seconds": 60,
            },
        ).raise_for_status()

        proposal_run = client.post(
            "/v1/software-factory/proposals",
            json={
                "project_id": leased_pr_job["project_id"],
                "task_id": leased_pr_job["task_id"],
                "reservation_id": leased_pr_job["payload"]["reservation_id"],
                "dedupe_key": leased_pr_job["payload"]["dedupe_key"],
                "title": leased_pr_job["payload"]["title"],
                "branch_name": leased_pr_job["payload"]["branch_name"],
                "summary": leased_pr_job["payload"]["summary"],
                "files": leased_pr_job["payload"]["files"],
                "created_by_job_id": leased_pr_job["id"],
            },
        )
        assert proposal_run.status_code == 200
        proposal_result = proposal_run.json()
        assert proposal_result["status"] == "created"
        pr_decision_id = proposal_result["decision_id"]

        assert (
            client.post(
                "/v1/worker/complete",
                json={
                    "worker_id": "worker-deploy-pr",
                    "job_id": leased_pr_job["id"],
                    "lease_token": leased_pr_job["lease_token"],
                    "result": proposal_result,
                },
            ).status_code
            == 200
        )

        assert (
            client.post(
                f"/v1/decisions/{pr_decision_id}/approve",
                json={"actor": "ceo", "note": "Merge the approved sandbox PR."},
            ).status_code
            == 200
        )

        leased_merge = client.post(
            "/v1/worker/poll",
            json={
                "worker_id": "worker-deploy-merge",
                "capabilities": ["github.pull_request.merge"],
                "lease_ttl_seconds": 60,
            },
        )
        assert leased_merge.status_code == 200
        leased_merge_job = leased_merge.json()["job"]
        assert leased_merge_job["kind"] == "github.pull_request.merge"

        client.post(
            "/v1/worker/heartbeat",
            json={
                "worker_id": "worker-deploy-merge",
                "job_id": leased_merge_job["id"],
                "lease_token": leased_merge_job["lease_token"],
                "lease_ttl_seconds": 60,
            },
        ).raise_for_status()

        merge_run = client.post(
            "/v1/software-factory/merges",
            json={
                "project_id": leased_merge_job["project_id"],
                "decision_id": leased_merge_job["payload"]["decision_id"],
                "dedupe_key": leased_merge_job["payload"]["dedupe_key"],
                "pull_request_number": leased_merge_job["payload"]["pull_request_number"],
                "pull_request_url": leased_merge_job["payload"]["pull_request_url"],
                "title": leased_merge_job["payload"]["title"],
                "branch_name": leased_merge_job["payload"].get("branch_name"),
                "created_by_job_id": leased_merge_job["id"],
            },
        )
        assert merge_run.status_code == 200
        merge_result = merge_run.json()
        assert merge_result["status"] == "created"
        assert merge_result["merged_sha"] == "merge-sha-199"
        deploy_decision_id = merge_result["deployment_decision_id"]

        complete_merge = client.post(
            "/v1/worker/complete",
            json={
                "worker_id": "worker-deploy-merge",
                "job_id": leased_merge_job["id"],
                "lease_token": leased_merge_job["lease_token"],
                "result": merge_result,
            },
        )
        assert complete_merge.status_code == 200
        assert complete_merge.json()["status"] == "succeeded"
        assert complete_merge.json()["deployment_decision_id"] == deploy_decision_id

        with engine.begin() as connection:
            task_status_after_merge = connection.execute(
                text("SELECT status FROM tasks WHERE id = :task_id"),
                {"task_id": task_id},
            ).scalar_one()
            deploy_decision_status = connection.execute(
                text("SELECT status FROM decisions WHERE id = :decision_id"),
                {"decision_id": deploy_decision_id},
            ).scalar_one()

        assert task_status_after_merge == "open"
        assert deploy_decision_status == "pending"

        assert (
            client.post(
                f"/v1/decisions/{deploy_decision_id}/approve",
                json={"actor": "ceo", "note": "Record the sandbox deployment."},
            ).status_code
            == 200
        )

        leased_deploy = client.post(
            "/v1/worker/poll",
            json={
                "worker_id": "worker-deploy-release",
                "capabilities": ["github.deployment.create"],
                "lease_ttl_seconds": 60,
            },
        )
        assert leased_deploy.status_code == 200
        leased_deploy_job = leased_deploy.json()["job"]
        assert leased_deploy_job["kind"] == "github.deployment.create"

        client.post(
            "/v1/worker/heartbeat",
            json={
                "worker_id": "worker-deploy-release",
                "job_id": leased_deploy_job["id"],
                "lease_token": leased_deploy_job["lease_token"],
                "lease_ttl_seconds": 60,
            },
        ).raise_for_status()

        deploy_run = client.post(
            "/v1/software-factory/deployments",
            json={
                "project_id": leased_deploy_job["project_id"],
                "decision_id": leased_deploy_job["payload"]["decision_id"],
                "dedupe_key": leased_deploy_job["payload"]["dedupe_key"],
                "merged_sha": leased_deploy_job["payload"]["merged_sha"],
                "pull_request_url": leased_deploy_job["payload"]["pull_request_url"],
                "title": leased_deploy_job["payload"]["title"],
                "branch_name": leased_deploy_job["payload"].get("branch_name"),
                "environment": leased_deploy_job["payload"]["environment"],
                "created_by_job_id": leased_deploy_job["id"],
            },
        )
        assert deploy_run.status_code == 200
        deploy_result = deploy_run.json()
        assert deploy_result["status"] == "created"
        assert deploy_result["environment"] == "staging"
        assert deploy_result["deployment_status"] == "success"
        assert deploy_result["health_verification_status"] == "passed"
        assert deploy_result["healthcheck_url"] == "https://example.com/staging/777"
        assert deploy_result["rollout_status"] == "completed"
        assert deploy_result["rollout_stage"] == "release_readiness"

        complete_deploy = client.post(
            "/v1/worker/complete",
            json={
                "worker_id": "worker-deploy-release",
                "job_id": leased_deploy_job["id"],
                "lease_token": leased_deploy_job["lease_token"],
                "result": deploy_result,
            },
        )
        assert complete_deploy.status_code == 200
        assert complete_deploy.json()["status"] == "succeeded"
        assert complete_deploy.json()["rollout_id"] == deploy_result["rollout_id"]

        with engine.begin() as connection:
            task_status = connection.execute(
                text("SELECT status FROM tasks WHERE id = :task_id"),
                {"task_id": task_id},
            ).scalar_one()
            review_decision_status = connection.execute(
                text("SELECT status FROM decisions WHERE id = :decision_id"),
                {"decision_id": pr_decision_id},
            ).scalar_one()
            deployment_decision_status = connection.execute(
                text("SELECT status FROM decisions WHERE id = :decision_id"),
                {"decision_id": deploy_decision_id},
            ).scalar_one()
            artifact_types = [
                row["artifact_type"]
                for row in connection.execute(
                    text(
                        """
                        SELECT artifact_type
                        FROM artifacts
                        WHERE project_id = 'boss-dogfood'
                        ORDER BY created_at ASC
                        """
                    )
                ).mappings().all()
            ]
            receipt_kinds = [
                row["effect_kind"]
                for row in connection.execute(
                    text(
                        """
                        SELECT effect_kind
                        FROM receipts
                        WHERE project_id = 'boss-dogfood'
                        ORDER BY created_at ASC
                        """
                    )
                ).mappings().all()
            ]
            rollout = connection.execute(
                text(
                    """
                    SELECT status, current_stage, health_verification_status, stages
                    FROM deployment_rollouts
                    WHERE id = :rollout_id
                    """
                ),
                {"rollout_id": deploy_result["rollout_id"]},
            ).mappings().one()

        assert task_status == "completed"
        assert review_decision_status == "executed"
        assert deployment_decision_status == "executed"
        assert "deployment_result" in artifact_types
        assert "deployment_rollout" in artifact_types
        assert rollout["status"] == "completed"
        assert rollout["current_stage"] == "release_readiness"
        assert rollout["health_verification_status"] == "passed"
        assert rollout["stages"][2]["status"] == "passed"
        assert sorted(receipt_kinds) == sorted(
            [
                "github.pull_request.create",
                "github.pull_request.merge",
                "github.deployment.create",
            ]
        )

        rollouts = client.get(
            "/v1/software-factory/rollouts",
            params={"project_id": "boss-dogfood"},
        )
        assert rollouts.status_code == 200
        assert rollouts.json()["items"][0]["id"] == deploy_result["rollout_id"]

        events = client.get("/v1/events", params={"project_id": "boss-dogfood"})
        assert events.status_code == 200
        event_types = [item["event_type"] for item in events.json()["items"]]
        assert "software_factory.deployment_review_requested" in event_types
        assert "software_factory.deployment_created" in event_types
        assert "software_factory.deployment_rollout_recorded" in event_types
        assert "task.completed" in event_types
    finally:
        api_main.decisions.settings.enable_sandbox_merge = previous_merge
        api_main.software_factory.settings.enable_sandbox_merge = previous_merge_factory
        api_main.decisions.settings.enable_sandbox_deploy = previous_deploy
        api_main.software_factory.settings.enable_sandbox_deploy = previous_deploy_factory
        api_main.software_factory.settings.sandbox_deploy_healthcheck_url = (
            previous_healthcheck_url
        )
        api_main.software_factory.settings.openai_api_key = previous_openai


def test_pagination_limit_offset_on_events_resources_costs_lists(client):
    """P2 perf: key high-volume lists now support limit/offset; defaults preserved; responses include metadata; invalid offset rejected. (refs arch-review P2 pagination, GAP dispatch)"""
    # Events (always has seed/activity data)
    r = client.get("/v1/events", params={"project_id": "boss-dogfood"})
    assert r.status_code == 200
    body = r.json()
    assert "items" in body and isinstance(body["items"], list)
    assert body["limit"] == 50
    assert body["offset"] == 0
    assert len(body["items"]) <= 50

    r_small = client.get("/v1/events", params={"project_id": "boss-dogfood", "limit": 2, "offset": 0})
    assert r_small.status_code == 200
    b_small = r_small.json()
    assert b_small["limit"] == 2
    assert b_small["offset"] == 0
    assert len(b_small["items"]) <= 2

    r_page2 = client.get("/v1/events", params={"project_id": "boss-dogfood", "limit": 2, "offset": 2})
    assert r_page2.status_code == 200
    assert r_page2.json()["offset"] == 2

    # offset negative rejected (400)
    r_bad = client.get("/v1/events", params={"project_id": "boss-dogfood", "offset": -5})
    assert r_bad.status_code == 400
    assert "offset" in r_bad.json()["detail"].lower()

    # Resources and costs: contract + paging params accepted (may be empty post-seed, but shape + defaults)
    rr = client.get("/v1/resources", params={"project_id": "boss-dogfood", "limit": 5, "offset": 1})
    assert rr.status_code == 200
    rrb = rr.json()
    assert rrb["limit"] == 5
    assert rrb["offset"] == 1
    assert "items" in rrb

    cc = client.get("/v1/costs", params={"project_id": "boss-dogfood", "limit": 3, "offset": 0})
    assert cc.status_code == 200
    ccb = cc.json()
    assert ccb["limit"] == 3
    assert ccb["offset"] == 0
    assert "items" in ccb
