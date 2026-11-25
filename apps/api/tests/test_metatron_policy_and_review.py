"""Wave B tests: signed policy bundle, JWKS, rule example validation, grants & reviewer."""
from __future__ import annotations

from fastapi.testclient import TestClient
from sqlalchemy import text

from apps.api.tests.identity_fixtures import IdentityWorld
from apps.api.tests.test_agent_voice_stream_intent import connect_owned
from boss_engine.identity import jwks
from boss_engine.services import model_routing
from boss_engine.services.model_routing import ModelChoice


class _ReviewerAdapter:
    _boss_choice = ModelChoice("ollama", "owned reviewer", "approved_local_model_host")

    def __init__(self, action: str = "approve", confidence: float = 0.95):
        self.action = action
        self.confidence = confidence
        self.calls = 0

    def create_chat_completion(self, messages, tools=None, tool_choice="auto"):
        self.calls += 1
        return (
            {
                "content": (
                    f'{{"action": "{self.action}", "confidence": {self.confidence}, '
                    f'"reason": "Covered by active grant", "evidence": ["grant"]}}'
                ),
                "tool_calls": None,
            },
            {
                "model": "owned-reviewer",
                "cost_usd_micros": 500,
                "prompt_tokens": 20,
                "completion_tokens": 12,
            },
        )


def test_policy_bundle_jws_verification_etag_and_rule_examples_validation(
    client: TestClient, engine, monkeypatch
):
    monkeypatch.setenv("BOSS_AUTHZ_MODE", "enforce")
    world = IdentityWorld.create(engine)
    admin = world.person("pol-admin")
    viewer = world.person("pol-viewer")
    world.bind(admin, "admin", project=world.project_a)
    world.bind(viewer, "viewer", project=world.project_a)

    # 1. Invalid rule examples fail with 422 on publish.
    with world.acting_as(world.ctx(admin)):
        bad_pub = client.post(
            "/v1/tool-policies",
            json={
                "project_id": world.project_a,
                "actor": admin.id,
                "scope": "project",
                "effect": "allow",
                "selector": {"server": "metatron", "tool": "ssh_run"},
                "conditions": [{"path": "$.argv[0]", "op": "equals", "value": "sar"}],
                "examples": {
                    "match": ["uptime"],  # does NOT match argv[0] == "sar"
                    "not_match": ["sar -u 1 5"],  # DOES match
                },
            },
        )
        assert bad_pub.status_code == 422, bad_pub.text

        # 2. Valid rule examples + hard:true succeed on publish.
        good_pub = client.post(
            "/v1/tool-policies",
            json={
                "project_id": world.project_a,
                "actor": admin.id,
                "scope": "project",
                "priority": 10,
                "effect": "ask",
                "selector": {"server": "metatron", "tool": "ssh_run"},
                "conditions": [{"path": "$.argv[0]", "op": "equals", "value": "systemctl"}],
                "hard": True,
                "note": "Hard prompt for systemctl",
                "examples": {
                    "match": ["systemctl status nginx"],
                    "not_match": ["sar -u 1 5"],
                },
            },
        )
        assert good_pub.status_code == 201, good_pub.text
        created_id = good_pub.json()["policy"]["id"]

    # 3. GET /v1/metatron/policy returns signed Ed25519 JWS + ETag; /policy/keys verifies it.
    keys_resp = client.get("/v1/metatron/policy/keys")
    assert keys_resp.status_code == 200
    key_map = {k["kid"]: jwks.public_key_from_jwk(k) for k in keys_resp.json()["keys"]}
    assert key_map

    with world.acting_as(world.ctx(viewer)):
        pol_resp = client.get("/v1/metatron/policy", params={"project_id": world.project_a})
        assert pol_resp.status_code == 200, pol_resp.text
        etag = pol_resp.headers.get("etag")
        assert etag
        payload = pol_resp.json()
        assert payload["alg"] == "EdDSA"
        assert payload["typ"] == "boss-policy+jws"
        header, claims = jwks.verify(
            payload["jws"],
            typ="boss-policy+jws",
            key_for_kid=lambda kid: key_map.get(kid),
        )
        assert header["kid"] == payload["kid"]
        assert claims["project_id"] == world.project_a
        rule_entry = next(r for r in claims["rules"] if r["id"] == created_id)
        assert rule_entry["hard"] is True
        assert rule_entry["decision"] == "prompt"
        assert rule_entry["examples"]["match"] == ["systemctl status nginx"]
        assert claims["host_diagnostics"]["version"] == 1

        # ETag If-None-Match -> 304 Not Modified
        cached = client.get(
            "/v1/metatron/policy",
            params={"project_id": world.project_a},
            headers={"If-None-Match": etag},
        )
        assert cached.status_code == 304

        # Cross-project read is forbidden
        cross = client.get("/v1/metatron/policy", params={"project_id": world.project_b})
        assert cross.status_code == 403


def test_grants_and_server_side_budgeted_reviewer(
    client: TestClient, engine, monkeypatch
):
    monkeypatch.setenv("BOSS_AUTHZ_MODE", "enforce")
    world = IdentityWorld.create(engine)
    operator = world.person("rev-operator")
    other_op = world.person("rev-other")
    viewer = world.person("rev-viewer")
    world.bind(operator, "operator", project=world.project_a)
    world.bind(other_op, "operator", project=world.project_a)
    world.bind(viewer, "viewer", project=world.project_a)

    connect_owned(engine, monkeypatch, world.project_a, paid=True)
    reviewer_model = _ReviewerAdapter(action="approve")
    monkeypatch.setattr(model_routing, "project_chat_adapter", lambda *_a, **_k: reviewer_model)

    # Viewer cannot create grants or invoke review
    with world.acting_as(world.ctx(viewer)):
        assert (
            client.post(
                "/v1/metatron/grants",
                json={"project_id": world.project_a, "ttl_s": 300},
            ).status_code
            == 403
        )
        assert (
            client.post(
                "/v1/metatron/approvals/review",
                json={
                    "project_id": world.project_a,
                    "session_id": "ses_1",
                    "call": {"tool": "ssh_run", "risk_class": "remote-write"},
                },
            ).status_code
            == 403
        )

    # Without an active grant, reviewer escalates with zero LLM calls
    with world.acting_as(world.ctx(operator)):
        no_grant = client.post(
            "/v1/metatron/approvals/review",
            json={
                "project_id": world.project_a,
                "session_id": "ses_1",
                "call": {"tool": "ssh_run", "argv_redacted": ["systemctl", "restart", "nginx"], "risk_class": "remote-write"},
            },
        )
        assert no_grant.status_code == 200
        assert no_grant.json()["decision"] == "escalate"
        assert no_grant.json()["reason"] == "no_active_grant"
        assert reviewer_model.calls == 0

        # Create TTL-bounded grant
        grant_resp = client.post(
            "/v1/metatron/grants",
            json={
                "project_id": world.project_a,
                "scope": {"hosts": ["web-1"]},
                "allowed_tools": ["ssh_run"],
                "max_risk_class": "remote-write",
                "ttl_s": 300,
            },
        )
        assert grant_resp.status_code == 201, grant_resp.text
        grant_id = grant_resp.json()["grant_id"]

        # Hard risk class (capture / secret-use / app-control) cannot be approved by reviewer even with grant!
        hard_review = client.post(
            "/v1/metatron/approvals/review",
            json={
                "project_id": world.project_a,
                "session_id": "ses_1",
                "call": {"tool": "pcap", "risk_class": "capture"},
            },
        )
        assert hard_review.status_code == 200
        assert hard_review.json()["decision"] == "escalate"
        assert hard_review.json()["hard"] is True
        assert reviewer_model.calls == 0

        # Allowed tool under active grant triggers budgeted AgentService.decide + receipt
        ok_review = client.post(
            "/v1/metatron/approvals/review",
            json={
                "project_id": world.project_a,
                "session_id": "ses_1",
                "turn_id": "mturn_rev_1",
                "call": {
                    "tool": "ssh_run",
                    "argv_redacted": ["service", "nginx", "reload"],
                    "host": "web-1",
                    "risk_class": "remote-write",
                },
            },
        )
        assert ok_review.status_code == 200, ok_review.text
        rev_data = ok_review.json()
        assert rev_data["decision"] == "approve", rev_data
        assert rev_data["reviewer_receipt_id"]
        assert reviewer_model.calls == 1

        with engine.connect() as c:
            rcpt = c.execute(
                text("SELECT status FROM receipts WHERE id = :id"),
                {"id": rev_data["reviewer_receipt_id"]},
            ).scalar()
        assert rcpt == "succeeded"

    # Another principal cannot revoke operator's grant
    with world.acting_as(world.ctx(other_op)):
        assert client.delete(f"/v1/metatron/grants/{grant_id}").status_code == 404

    # Owner revokes grant -> subsequent review escalates
    with world.acting_as(world.ctx(operator)):
        del_resp = client.delete(f"/v1/metatron/grants/{grant_id}")
        assert del_resp.status_code == 200
        after_del = client.post(
            "/v1/metatron/approvals/review",
            json={
                "project_id": world.project_a,
                "session_id": "ses_1",
                "call": {"tool": "ssh_run", "risk_class": "remote-write"},
            },
        )
        assert after_del.json()["decision"] == "escalate"
        assert after_del.json()["reason"] == "no_active_grant"
