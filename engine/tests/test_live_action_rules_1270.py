"""BOSSAPP-1270: a builder's sentence is a saved rule for one live app action.

The sentence is parsed once, when it is recorded. Later evaluation reads the saved
effect, action pattern and predicate. It does not read the sentence again and it
does not call a model. No matching allow means the action does not run. A block
does not run. An allow runs, then writes one decision row. The same dedupe key
does not run a second time.
"""
from __future__ import annotations

import inspect
import os
import uuid

import pytest
from sqlalchemy import create_engine, text

from boss_engine.services.live_action_rules import (
    LiveActionFailed,
    LiveActionRuleError,
    attempt,
    list_rules,
    parse_description,
    record_rule,
)

TEST_DB = os.environ["DATABASE_URL"]


@pytest.fixture(scope="module")
def eng():
    return create_engine(TEST_DB, future=True)


@pytest.fixture()
def project(eng):
    pid = f"p_{uuid.uuid4().hex[:10]}"
    oid = f"org_{uuid.uuid4().hex[:10]}"
    wid = f"ws_{uuid.uuid4().hex[:10]}"
    with eng.begin() as c:
        c.execute(text("INSERT INTO orgs (id, name) VALUES (:i, 'live rules')"), {"i": oid})
        c.execute(text(
            "INSERT INTO workspaces (id, org_id, name) VALUES (:i, :o, 'live')"),
            {"i": wid, "o": oid})
        c.execute(text(
            "INSERT INTO projects (id, workspace_id, name, slug) VALUES (:i, :w, 'Live', :s)"),
            {"i": pid, "w": wid, "s": pid})
    yield pid
    with eng.begin() as c:
        c.execute(text("DELETE FROM live_action_decisions WHERE project_id = :p"), {"p": pid})
        c.execute(text("DELETE FROM live_action_rules WHERE project_id = :p"), {"p": pid})
        c.execute(text("DELETE FROM projects WHERE id = :p"), {"p": pid})
        c.execute(text("DELETE FROM workspaces WHERE id = :w"), {"w": wid})
        c.execute(text("DELETE FROM orgs WHERE id = :o"), {"o": oid})


def _decisions(c, pid):
    return c.execute(text(
        "SELECT id, rule_id, effect, action, actor, dedupe_key FROM live_action_decisions "
        "WHERE project_id = :p ORDER BY created_at, id"), {"p": pid}).mappings().all()


def test_the_same_sentence_always_parses_the_same_way():
    parsed = parse_description("  Block   refund   when   amount   is   over   50  ")
    assert parsed == parse_description("block refund when amount is over 50")
    assert parsed == {
        "effect": "block",
        "action_pattern": "refund",
        "predicate": {"field": "amount", "op": "gt", "value": 50},
    }
    assert parse_description("allow checkout") == {
        "effect": "allow", "action_pattern": "checkout", "predicate": {},
    }
    assert parse_description("allow publish when actor is Ada")["predicate"] == {
        "field": "actor", "op": "eq", "value": "Ada",
    }
    assert parse_description("block refund*")["action_pattern"] == "refund*"
    assert parse_description("allow *")["action_pattern"] == "*"


@pytest.mark.parametrize("sentence", [
    "",
    "   ",
    "refund",
    "deny refund",
    "allow",
    "ask the model whether to allow refund",
    "block refund when",
    "block refund when amount is over",
])
def test_a_sentence_that_is_not_a_rule_is_refused(sentence):
    with pytest.raises(LiveActionRuleError):
        parse_description(sentence)


def test_evaluation_source_does_not_call_a_model():
    import boss_engine.services.live_action_rules as rules

    source = inspect.getsource(rules)
    for banned in ("openai", "anthropic", "litellm", "model_gateway", "chat.completions"):
        assert banned not in source


def test_a_described_rule_is_stored(eng, project):
    with eng.begin() as c:
        rule = record_rule(
            c, project, "block refund when amount is over 50",
            actor="ada", app_key="Storefront")
        assert rule["effect"] == "block"
        assert rule["action_pattern"] == "refund"
        assert rule["predicate"] == {"field": "amount", "op": "gt", "value": 50}
        assert rule["app_key"] == "storefront"
        assert rule["description"] == "block refund when amount is over 50"
        listed = list_rules(c, project, app_key="storefront")
        assert [row["id"] for row in listed] == [rule["id"]]
        assert _decisions(c, project) == []


def test_no_matching_allow_does_not_run(eng, project):
    calls = []

    def executor(action, payload):
        calls.append((action, payload))
        return {"status": "ran"}

    with eng.begin() as c:
        out = attempt(c, project, app_key="storefront", action="checkout", actor="ada",
                      payload={}, executor=executor)
        assert out["decision"] == "block" and out["ran"] is False
        assert out["decision_id"] is None and out["rule_id"] is None
        assert calls == []
        assert _decisions(c, project) == []


def test_a_block_does_not_run_and_an_allow_records_the_decision(eng, project):
    calls = []

    def executor(action, payload):
        calls.append((action, dict(payload)))
        return {"status": "ran", "action": action}

    with eng.begin() as c:
        blocked = record_rule(
            c, project, "block refund when amount is over 50",
            actor="ada", app_key="storefront", position=10)
        allowed = record_rule(
            c, project, "allow refund", actor="ada", app_key="storefront", position=20)
        record_rule(c, project, "allow checkout", actor="ada", app_key="storefront")
        high = attempt(c, project, app_key="storefront", action="refund", actor="ada",
                       payload={"amount": 80}, executor=executor)
        low = attempt(c, project, app_key="storefront", action="refund", actor="ada",
                      payload={"amount": 40}, executor=executor)
        other = attempt(c, project, app_key="storefront", action="checkout", actor="ada",
                        payload={}, executor=executor)
        assert high["decision"] == "block" and high["ran"] is False
        assert high["rule_id"] == blocked["id"] and high["decision_id"] is None
        assert low["decision"] == "allow" and low["ran"] is True
        assert low["rule_id"] == allowed["id"] and low["result"]["action"] == "refund"
        assert other["ran"] is True and other["decision_id"]
        assert calls == [("refund", {"amount": 40}), ("checkout", {})]
        rows = _decisions(c, project)
        assert [row["action"] for row in rows] == ["refund", "checkout"]
        assert rows[0]["effect"] == "allow" and rows[0]["rule_id"] == allowed["id"]
        assert rows[0]["id"] == low["decision_id"]


def test_saved_columns_decide_not_the_sentence(eng, project):
    calls = []

    def executor(action, payload):
        calls.append(action)
        return {"status": "ran"}

    with eng.begin() as c:
        rule = record_rule(c, project, "block refund", actor="ada", app_key="storefront")
        c.execute(text(
            "UPDATE live_action_rules SET description = 'allow refund' WHERE id = :i"),
            {"i": rule["id"]})
        blocked = attempt(c, project, app_key="storefront", action="refund", actor="ada",
                          executor=executor)
        assert blocked["ran"] is False and blocked["rule_id"] == rule["id"]
        c.execute(text(
            "UPDATE live_action_rules SET effect = 'allow' WHERE id = :i"),
            {"i": rule["id"]})
        allowed = attempt(c, project, app_key="storefront", action="refund", actor="ada",
                          executor=executor)
        assert allowed["ran"] is True and allowed["rule_id"] == rule["id"]
        assert calls == ["refund"]


def test_a_narrow_predicate_uses_the_attempt_actor(eng, project):
    calls = []

    def executor(action, payload):
        calls.append(action)
        return {"status": "ran"}

    with eng.begin() as c:
        record_rule(c, project, "allow publish when actor is ada",
                    actor="ada", app_key="storefront", position=10)
        record_rule(c, project, "block publish", actor="ada", app_key="storefront", position=20)
        ada = attempt(c, project, app_key="storefront", action="Publish", actor="Ada",
                      executor=executor)
        bob = attempt(c, project, app_key="storefront", action="publish", actor="bob",
                      executor=executor)
        named = attempt(c, project, app_key="storefront", action="publish", actor="ada",
                        payload={"actor": "bob"}, executor=executor)
        assert ada["ran"] is True
        assert bob["ran"] is False and named["ran"] is False
        assert calls == ["publish"]


def test_a_glob_blocks_only_the_actions_it_names(eng, project):
    calls = []

    def executor(action, payload):
        calls.append(action)
        return {"status": "ran"}

    with eng.begin() as c:
        record_rule(c, project, "allow *", actor="ada", app_key="storefront", position=50)
        record_rule(c, project, "block refund*", actor="ada", app_key="storefront", position=10)
        refund = attempt(c, project, app_key="storefront", action="refund_full", actor="ada",
                         executor=executor)
        charge = attempt(c, project, app_key="storefront", action="charge", actor="ada",
                         executor=executor)
        assert refund["ran"] is False and charge["ran"] is True
        assert calls == ["charge"]


def test_the_same_dedupe_key_does_not_run_twice(eng, project):
    calls = []

    def executor(action, payload):
        calls.append(action)
        return {"status": "ran", "n": len(calls)}

    with eng.begin() as c:
        record_rule(c, project, "allow checkout", actor="ada", app_key="storefront")
        first = attempt(c, project, app_key="storefront", action="checkout", actor="ada",
                        dedupe_key="order-1", executor=executor)
        second = attempt(c, project, app_key="storefront", action="checkout", actor="ada",
                         dedupe_key="order-1", executor=executor)
        assert first["ran"] is True and first["repeated"] is False
        assert second["ran"] is True and second["repeated"] is True
        assert second["decision_id"] == first["decision_id"]
        assert second["result"]["n"] == 1
        assert calls == ["checkout"]
        assert len(_decisions(c, project)) == 1


def test_a_failed_action_is_visible_and_not_recorded(eng, project):
    def executor(action, payload):
        raise RuntimeError("ledger down")

    with eng.begin() as c:
        record_rule(c, project, "allow checkout", actor="ada", app_key="storefront")
        with pytest.raises(LiveActionFailed, match="ledger down"):
            attempt(c, project, app_key="storefront", action="checkout", actor="ada",
                    executor=executor)
        assert _decisions(c, project) == []


def test_an_unparseable_sentence_is_not_stored(eng, project):
    with eng.begin() as c:
        with pytest.raises(LiveActionRuleError):
            record_rule(c, project, "refunds are sometimes fine", actor="ada", app_key="storefront")
        assert list_rules(c, project) == []


def test_rules_for_another_app_do_not_apply(eng, project):
    calls = []

    def executor(action, payload):
        calls.append(action)
        return {"status": "ran"}

    with eng.begin() as c:
        record_rule(c, project, "allow checkout", actor="ada", app_key="storefront")
        out = attempt(c, project, app_key="billing", action="checkout", actor="ada",
                      executor=executor)
        assert out["ran"] is False and calls == []


def test_http_records_a_rule_and_runs_only_an_allow(eng, project, monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from apps.api.app.routers import gap_1270 as gap

    monkeypatch.setattr(gap, "admit_reader", lambda *args, **kwargs: None)
    monkeypatch.setattr(gap, "admit_writer", lambda connection, project_id, actor: actor or "ada")
    app = FastAPI()
    app.include_router(gap.router)
    client = TestClient(app)
    bad = client.post("/v1/live-action-rules", json={
        "project_id": project, "app_key": "storefront", "actor": "ada",
        "description": "please decide",
    })
    assert bad.status_code == 422
    recorded = client.post("/v1/live-action-rules", json={
        "project_id": project, "app_key": "storefront", "actor": "ada",
        "description": "block refund when amount is over 50",
    })
    assert recorded.status_code == 201
    rule = recorded.json()["rule"]
    assert rule["effect"] == "block" and rule["action_pattern"] == "refund"
    listed = client.get("/v1/live-action-rules", params={
        "project_id": project, "app_key": "storefront"})
    assert listed.status_code == 200
    assert [row["id"] for row in listed.json()["rules"]] == [rule["id"]]
    client.post("/v1/live-action-rules", json={
        "project_id": project, "app_key": "storefront", "actor": "ada",
        "description": "allow checkout",
    })
    blocked = client.post("/v1/live-action-rules/attempt", json={
        "project_id": project, "app_key": "storefront", "actor": "ada",
        "action": "refund", "payload": {"amount": 80},
    })
    assert blocked.status_code == 200
    assert blocked.json()["ran"] is False and blocked.json()["decision_id"] is None
    allowed = client.post("/v1/live-action-rules/attempt", json={
        "project_id": project, "app_key": "storefront", "actor": "ada",
        "action": "checkout", "payload": {}, "dedupe_key": "http-1",
    })
    body = allowed.json()
    assert allowed.status_code == 200 and body["ran"] is True and body["decision_id"]
    again = client.post("/v1/live-action-rules/attempt", json={
        "project_id": project, "app_key": "storefront", "actor": "ada",
        "action": "checkout", "payload": {}, "dedupe_key": "http-1",
    })
    assert again.json()["repeated"] is True
    assert again.json()["decision_id"] == body["decision_id"]
    with eng.begin() as c:
        rows = _decisions(c, project)
    assert [row["action"] for row in rows] == ["checkout"]
