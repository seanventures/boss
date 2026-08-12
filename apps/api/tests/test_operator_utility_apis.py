"""HTTP surfaces for previously API-less operator utilities.

Covers pure / dry-run endpoints that wrap existing engine services without
external credentials or provider writes:

  POST /v1/ci-config/generate
  POST /v1/redaction/redact
  GET  /v1/platform-standards
  POST /v1/platform-standards/apply
  POST /v1/telemetry/mine
  POST /v1/verification-target/profile
  GET  /v1/aws/metal/types
  POST /v1/aws/metal/classify
  POST /v1/verifier-debate/run
  POST /v1/design-prompts/product
"""
from __future__ import annotations


def test_ci_config_generate_github_actions(client):
    r = client.post(
        "/v1/ci-config/generate",
        json={"language": "python", "target": "github_actions"},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["target"] == "github_actions"
    assert body["path"] == ".github/workflows/ci.yml"
    assert "jobs:" in body["content"]
    assert "test" in body["commands"]
    assert body["checksum"]
    assert len(body["checksum"]) == 64


def test_ci_config_generate_unsupported_language_422(client):
    r = client.post(
        "/v1/ci-config/generate",
        json={"language": "cobol", "target": "github_actions"},
    )
    assert r.status_code == 422, r.text


def test_redaction_redacts_secrets_and_email(client):
    text = "contact me@example.com with key AKIAIOSFODNN7EXAMPLE and Bearer abcdefghijklmnopqr"
    r = client.post("/v1/redaction/redact", json={"text": text})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["has_pii"] is True
    assert "[EMAIL]" in body["redacted"]
    assert "me@example.com" not in body["redacted"]
    assert "AKIAIOSFODNN7EXAMPLE" not in body["redacted"]
    assert body["summary"]


def test_redaction_clean_text(client):
    r = client.post("/v1/redaction/redact", json={"text": "hello world"})
    assert r.status_code == 200
    body = r.json()
    assert body["redacted"] == "hello world"
    assert body["has_pii"] is False
    assert body["summary"] == {}


def test_platform_standards_catalog_and_apply(client):
    cat = client.get("/v1/platform-standards")
    assert cat.status_code == 200, cat.text
    body = cat.json()
    assert "observability" in body["keys"]
    assert body["count"] >= 1
    assert "http_requests_total" in body["standards"]["observability"]

    apply = client.post(
        "/v1/platform-standards/apply",
        json={"instructions": "Build a FastAPI HTTP service on port 8080"},
    )
    assert apply.status_code == 200, apply.text
    applied = apply.json()
    assert applied["applies"] is True
    assert "PLATFORM STANDARDS" in applied["block"]
    assert "observability" in applied["keys_applied"]

    skip = client.post(
        "/v1/platform-standards/apply",
        json={"instructions": "Add a pure math helper for factorial"},
    )
    assert skip.status_code == 200
    assert skip.json()["applies"] is False
    assert skip.json()["block"] == ""

    forced = client.post(
        "/v1/platform-standards/apply",
        json={"instructions": "pure helper", "force": True},
    )
    assert forced.status_code == 200
    assert forced.json()["applies"] is True
    assert forced.json()["block"]


def test_telemetry_mine_templates_and_error_signal(client):
    events = [
        {
            "ts": float(i),
            "message": f"db connection refused from 10.0.0.{i % 5}",
            "service": "api",
            "level": "error",
        }
        for i in range(12)
    ]
    events.append(
        {
            "ts": 100.0,
            "message": "ok",
            "service": "api",
            "level": "info",
        }
    )
    r = client.post(
        "/v1/telemetry/mine",
        json={"events": events, "top_n": 5},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["events"] == 13
    assert body["distinct_templates"] >= 1
    assert body["top_templates"]
    assert body["signal_count"] >= 1
    kinds = {s["kind"] for s in body["signals"]}
    assert "new_template" in kinds or "error_surge" in kinds


def test_telemetry_mine_requires_events_or_metrics(client):
    r = client.post("/v1/telemetry/mine", json={})
    assert r.status_code == 422


def test_verification_target_profile_web_vs_python(client):
    web = client.post(
        "/v1/verification-target/profile",
        json={
            "title": "Landing page for checkout",
            "spec": {"project_kind": "web", "instructions": "Build a React landing page"},
        },
    )
    assert web.status_code == 200, web.text
    wbody = web.json()
    assert wbody["kind"] == "web"
    assert "npm run build" in wbody["command"]
    assert wbody["test_commands"]

    py = client.post(
        "/v1/verification-target/profile",
        json={
            "title": "Parse CSV invoices",
            "spec": {"project_kind": "python", "instructions": "library only"},
        },
    )
    assert py.status_code == 200, py.text
    assert py.json()["kind"] == "python"
    assert "unittest" in py.json()["command"]


def test_aws_metal_types_and_classify(client):
    types = client.get("/v1/aws/metal/types", params={"region": "us-east-1"})
    assert types.status_code == 200, types.text
    body = types.json()
    assert body["count"] >= 1
    assert any(t.endswith(".metal") for t in body["items"])
    assert body["region"] == "us-east-1"

    yes = client.post(
        "/v1/aws/metal/classify",
        json={"instance_type": "m5.metal"},
    )
    assert yes.status_code == 200
    assert yes.json()["is_bare_metal"] is True

    no = client.post(
        "/v1/aws/metal/classify",
        json={"instance_type": "m5.large"},
    )
    assert no.status_code == 200
    assert no.json()["is_bare_metal"] is False


def test_verifier_debate_generator_concedes(client):
    r = client.post(
        "/v1/verifier-debate/run",
        json={
            "claim": "off-by-one in pagination",
            "max_rounds": 2,
            "defend_turns": [
                {"argument": "you are right", "concede": True},
            ],
            "challenge_turns": [
                {"argument": "see page 0", "withdraw": False},
            ],
        },
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["verdict"] == "refuted"
    assert body["reason"] == "generator_conceded"
    assert body["rounds_used"] == 1
    assert body["transcript"]


def test_verifier_debate_verifier_withdraws(client):
    r = client.post(
        "/v1/verifier-debate/run",
        json={
            "claim": "missing null check",
            "max_rounds": 2,
            "defend_turns": [
                {"argument": "guard is on line 12", "concede": False},
            ],
            "challenge_turns": [
                {"argument": "ok, withdraw", "withdraw": True},
            ],
        },
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["verdict"] == "survived"
    assert body["reason"] == "verifier_withdrew"


def test_design_prompts_product_ui_bearing(client):
    r = client.post(
        "/v1/design-prompts/product",
        json={
            "title": "Marketing landing page",
            "instructions": "Build a responsive React landing page with hero CTA",
            "criteria": ["Mobile first", "On-domain copy"],
            "spec": {"project_kind": "web"},
        },
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ui_bearing"] is True
    assert body["product_design_prompt"]
    assert "product designer" in body["product_design_prompt"].lower()
    assert body["design_impl_clause"]
    assert body["design_review_clause"]

    non_ui = client.post(
        "/v1/design-prompts/product",
        json={
            "title": "Factorial helper",
            "instructions": "Pure math library",
            "spec": {"project_kind": "python"},
        },
    )
    assert non_ui.status_code == 200
    assert non_ui.json()["ui_bearing"] is False
    assert non_ui.json()["product_design_prompt"] is None
