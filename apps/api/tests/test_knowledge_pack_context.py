"""BOSS-356 — knowledge pack-context HTTP API over ToolchainRegistryService.pack_context."""
from __future__ import annotations

from boss_engine.services.knowledge_context import (
    DEFAULT_TOKEN_BUDGET,
    pack_knowledge_context,
)


PROJECT_ID = "boss-dogfood"


def test_knowledge_status_reports_pack_context_availability(client):
    response = client.get(
        "/v1/knowledge/status",
        params={"project_id": PROJECT_ID},
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["pack_context_available"] is True
    assert body["default_token_budget"] == DEFAULT_TOKEN_BUDGET
    assert body["min_token_budget"] == 128
    assert body["redaction_enabled"] is True
    assert body["backend"] == "toolchain.context.pack"
    assert body["project_id"] == PROJECT_ID


def test_knowledge_pack_context_packs_messages_and_items(client):
    response = client.post(
        "/v1/knowledge/pack-context",
        json={
            "project_id": PROJECT_ID,
            "actor": "ceo",
            "token_budget": 400,
            "messages": [
                {
                    "role": "system",
                    "content": "Never expose secrets to model providers.",
                },
                {
                    "role": "user",
                    "content": "Summarize the open decisions for this project.",
                },
            ],
            "items": [
                {
                    "id": "proof-1",
                    "text": "Acceptance tests passed for budget reservations.",
                    "authority": "proof",
                    "priority": 80,
                },
                {
                    "id": "noise",
                    "text": "Optional background material. " * 40,
                    "authority": "reference",
                    "priority": 1,
                },
            ],
        },
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["budget_status"] in {
        "within_budget",
        "trimmed_non_authoritative",
    }
    assert body["token_budget"] == 400
    assert body["default_token_budget"] == DEFAULT_TOKEN_BUDGET
    assert "Never expose secrets" in body["context"]
    assert "Acceptance tests passed" in body["context"]
    assert "proof-1" in body["included_ids"]
    # System message is treated as policy/authoritative.
    assert any(item_id.startswith("system") for item_id in body["included_ids"])
    assert isinstance(body["redaction_summary"], dict)
    assert body["tools_attached"] == []


def test_knowledge_pack_context_never_returns_secrets(client):
    secret = "sk-abcdefghijklmnopqrstuvwxyz012345"
    bearer = "Bearer abcdefghijklmnopQRSTUVWX"
    aws_key = "AKIAIOSFODNN7EXAMPLE"

    response = client.post(
        "/v1/knowledge/pack-context",
        json={
            "project_id": PROJECT_ID,
            "actor": "ceo",
            "token_budget": 512,
            "messages": [
                {
                    "role": "user",
                    "content": (
                        f"Use api key {secret} and {bearer} and aws {aws_key} "
                        "to call the upstream."
                    ),
                }
            ],
            "items": [
                {
                    "id": "note",
                    "text": f"Stored credential: {secret}",
                    "authority": "reference",
                }
            ],
        },
    )
    assert response.status_code == 200, response.text
    body = response.json()
    packed = body["context"]
    assert secret not in packed
    assert bearer not in packed
    assert aws_key not in packed
    assert "[SECRET]" in packed or "[BEARER]" in packed or "[AWS_KEY]" in packed
    assert body["redaction_summary"]
    # Labels are reported for audit; raw values never appear.
    assert "SECRET" in body["redaction_summary"] or "AWS_KEY" in body["redaction_summary"]


def test_knowledge_pack_context_attaches_optional_tool_ids(client):
    response = client.post(
        "/v1/knowledge/pack-context",
        json={
            "project_id": PROJECT_ID,
            "actor": "ceo",
            "token_budget": 2000,
            "items": [
                {
                    "id": "task",
                    "text": "Prepare a bounded context pack for the coding agent.",
                    "authority": "fact",
                }
            ],
            "tool_ids": ["list_tasks", "not_a_real_tool"],
        },
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert "task" in body["included_ids"] or "tool:list_tasks" in body["included_ids"]
    # Unknown tools are soft-attached as reference stubs, not hard failures.
    assert "tool:not_a_real_tool" in body["included_ids"] or (
        "tool:not_a_real_tool" in body["omitted_ids"]
    )
    # Known tools appear in tools_attached when catalog resolves them.
    assert isinstance(body["tools_attached"], list)


def test_knowledge_pack_context_requires_actor_permission(client):
    denied = client.post(
        "/v1/knowledge/pack-context",
        json={
            "project_id": PROJECT_ID,
            "actor": "not-a-member",
            "token_budget": 256,
            "items": [{"id": "a", "text": "hello world"}],
        },
    )
    assert denied.status_code == 403, denied.text


def test_knowledge_pack_context_rejects_empty_and_invalid_budget(client):
    empty = client.post(
        "/v1/knowledge/pack-context",
        json={
            "project_id": PROJECT_ID,
            "actor": "ceo",
            "messages": [],
            "items": [],
            "chunks": [],
        },
    )
    assert empty.status_code == 422, empty.text

    low_budget = client.post(
        "/v1/knowledge/pack-context",
        json={
            "project_id": PROJECT_ID,
            "actor": "ceo",
            "token_budget": 10,
            "items": [{"id": "a", "text": "hello world"}],
        },
    )
    assert low_budget.status_code == 422, low_budget.text


def test_pack_knowledge_context_service_defaults_budget_and_dedupes():
    result = pack_knowledge_context(
        items=[
            {"id": "a", "text": "Policy: budgets before spend.", "authority": "policy"},
            {"id": "b", "text": "Policy: budgets before spend.", "authority": "reference"},
            {"id": "c", "text": "Optional fluff " * 20, "authority": "reference", "priority": 1},
        ],
        token_budget=None,
    )
    assert result["token_budget"] == DEFAULT_TOKEN_BUDGET
    assert result["duplicate_ids"] == ["b"]
    assert "Policy: budgets before spend." in result["context"]
    assert result["redaction_summary"] == {}
