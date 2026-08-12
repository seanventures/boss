"""E2E: llm_calls list/summary + budget insights over live Postgres.

Covers operator read paths over the canonical llm_calls ledger and the
deterministic budget insights analyzer. No external model spend.
"""

from __future__ import annotations

from uuid import uuid4

from sqlalchemy import text

from apps.api.app.db import begin


PROJECT_ID = "boss-dogfood"


def test_llm_calls_list_empty_or_paginated(client):
    r = client.get("/v1/llm-calls", params={"project_id": PROJECT_ID, "limit": 10})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["project_id"] == PROJECT_ID
    assert body["limit"] == 10
    assert body["offset"] == 0
    assert "items" in body
    assert body["count"] == len(body["items"])
    assert body["total"] >= body["count"]


def test_llm_calls_summary_groups(client):
    r = client.get(
        "/v1/llm-calls/summary",
        params={"project_id": PROJECT_ID, "group_by": "stage"},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["group_by"] == "stage"
    assert body["total_calls"] >= 0
    assert isinstance(body["groups"], list)


def test_llm_calls_summary_bad_group_by(client):
    r = client.get(
        "/v1/llm-calls/summary",
        params={"project_id": PROJECT_ID, "group_by": "nope"},
    )
    assert r.status_code in (422, 400)


def test_budget_insights_shape(client):
    r = client.get("/v1/budget/insights", params={"project_id": PROJECT_ID})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["project_id"] == PROJECT_ID
    assert "total_spend_usd_micros" in body
    assert isinstance(body.get("insights"), list)


def test_llm_calls_list_reflects_insert(client, engine):
    """Insert a row via SQL and confirm list/summary see it (no model spend)."""
    call_id = f"llmc_e2e_{uuid4().hex[:12]}"
    with begin(engine) as conn:
        conn.execute(
            text(
                """
                INSERT INTO llm_calls (
                    id, project_id, code_task_run_id, stage, engine, model, role,
                    prompt_tokens, completion_tokens, total_tokens,
                    cache_read_tokens, cache_write_tokens, cost_usd_micros,
                    price_estimated, latency_ms, retries, turns, ok, error)
                VALUES (
                    :id, :p, NULL, 'implement', 'test-engine', 'test-model', 'software_engineer',
                    10, 5, 15, 0, 0, 1234, false, 42, 0, 1, true, NULL)
                """
            ),
            {"id": call_id, "p": PROJECT_ID},
        )

    listed = client.get(
        "/v1/llm-calls",
        params={
            "project_id": PROJECT_ID,
            "model": "test-model",
            "engine": "test-engine",
            "stage": "implement",
            "ok": "true",
            "limit": 50,
        },
    )
    assert listed.status_code == 200, listed.text
    body = listed.json()
    ids = {item["id"] for item in body["items"]}
    assert call_id in ids
    hit = next(i for i in body["items"] if i["id"] == call_id)
    assert hit["cost_usd_micros"] == 1234
    assert hit["role"] == "software_engineer"

    summary = client.get(
        "/v1/llm-calls/summary",
        params={"project_id": PROJECT_ID, "group_by": "model"},
    )
    assert summary.status_code == 200, summary.text
    groups = summary.json()["groups"]
    keys = {g["group_key"] for g in groups}
    assert "test-model" in keys
    model_group = next(g for g in groups if g["group_key"] == "test-model")
    assert model_group["calls"] >= 1
    assert model_group["cost_usd_micros"] >= 1234
