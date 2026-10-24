"""Rules review over HTTP (BOSSAPP-768): /v1/knowledge/view/rules and its review actions.

Breaks covered: the review queue not answering in the KgRulesResponse shape; an approval without
step-up confirmation, or by someone without knowledge:review; the reviewer not recorded as the
request's actor; an edit that does not return the version it superseded.
"""
from __future__ import annotations

from sqlalchemy import text

from apps.api.app.db import begin
from boss_engine.services.knowledge import rules
from boss_engine.services.knowledge.code_index import index_files, symbol_key
from boss_engine.services.knowledge.store import PostgresKnowledgeStore

PROJECT_ID = "boss-dogfood"
ACTOR = "ceo"
PATH = "rules_api/limits.py"
SOURCE = "def check_limit(amount, limit):\n    if amount > limit:\n        raise ValueError('over')\n"
SYMBOL = symbol_key(None, PATH, "check_limit")


def _proposed(engine, statement: str = "Amounts above the limit are refused.") -> str:
    with begin(engine) as connection:
        index_files(connection, PostgresKnowledgeStore(), project_id=PROJECT_ID,
                    files=[{"path": PATH, "language": "python", "source": SOURCE}])
        return rules.propose_rule(
            connection, project_id=PROJECT_ID, title="Limit", statement=statement, category="limit",
            scope={}, evidence=[{"symbol": SYMBOL, "lines": [2, 3]}], rationale="", confidence=0.8,
            actor_ref="chief_architect", scan_id="scan_api")["node"]["id"]


def test_the_queue_lists_proposed_rules_with_counts(client, engine):
    rid = _proposed(engine)
    response = client.get("/v1/knowledge/view/rules",
                          params={"project_id": PROJECT_ID, "actor": ACTOR, "status": "proposed",
                                  "scan_id": "scan_api"})
    assert response.status_code == 200, response.text
    body = response.json()
    assert [r["node"]["id"] for r in body["items"]] == [rid]
    assert body["counts"]["proposed"] == 1 and body["scan_id"] == "scan_api"
    item = body["items"][0]
    assert item["evidence"][0]["symbol_key"] == SYMBOL and item["evidence"][0]["lines"] == [2, 3]
    assert client.get("/v1/knowledge/view/rules", params={"project_id": PROJECT_ID, "actor": ACTOR,
                                                          "status": "maybe"}).status_code == 422


def test_approve_needs_step_up_and_records_the_reviewer(client, engine):
    rid = _proposed(engine)
    url = f"/v1/knowledge/view/rules/{rid}/approve"
    refused = client.post(url, json={"project_id": PROJECT_ID, "actor": ACTOR})
    assert refused.status_code == 403 and "step_up" in refused.json()["detail"]
    approved = client.post(url, json={"project_id": PROJECT_ID, "actor": ACTOR, "step_up": "confirmed",
                                      "note": "Policy 4.2"})
    assert approved.status_code == 200, approved.text
    rule = approved.json()["rule"]
    assert rule["status"] == "approved" and rule["review"]["by"] == ACTOR
    with begin(engine) as connection:
        assert connection.execute(text(
            "SELECT actor_ref FROM kg_audit WHERE entity_id = :i AND action = 'knowledge.rule.approve'"),
            {"i": rid}).scalar_one() == ACTOR
    again = client.post(url, json={"project_id": PROJECT_ID, "actor": ACTOR, "step_up": "confirmed"})
    assert again.status_code == 422 and "is approved" in again.json()["detail"]


def test_review_needs_knowledge_review(client, engine):
    rid = _proposed(engine)
    with begin(engine) as connection:
        holder = connection.execute(text(
            "SELECT member_id FROM project_members WHERE project_id = :p AND status = 'active' "
            "AND NOT (permissions ? 'knowledge:review') AND member_type <> 'system' ORDER BY member_id LIMIT 1"),
            {"p": PROJECT_ID}).scalar()
    assert holder, "a member without knowledge:review"
    response = client.post(f"/v1/knowledge/view/rules/{rid}/reject",
                           json={"project_id": PROJECT_ID, "actor": holder, "step_up": "confirmed"})
    assert response.status_code == 403, response.text


def test_edit_returns_the_new_version_and_the_one_it_superseded(client, engine):
    rid = _proposed(engine)
    response = client.post(f"/v1/knowledge/view/rules/{rid}/edit", json={
        "project_id": PROJECT_ID, "actor": ACTOR, "step_up": "confirmed",
        "statement": "Amounts above the product's limit are refused at disbursement.",
        "category": "validation"})
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["rule"]["version"] == 2 and body["rule"]["previous_id"] == rid
    assert body["previous"]["status"] == "superseded" and body["rule"]["status"] == "approved"
    reject = client.post(f"/v1/knowledge/view/rules/{body['rule']['node']['id']}/reject",
                         json={"project_id": PROJECT_ID, "actor": ACTOR, "step_up": "confirmed",
                               "note": "Retired"})
    assert reject.status_code == 200 and reject.json()["rule"]["status"] == "rejected"
