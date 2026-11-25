"""Dev autonomy over the real API (BOSSAPP-1441): the policy is readable and settable, a project
is only ever stricter than its org, "is this production?" is one answer, and "Decided for you"
lists what BOSS decided with its undo."""
from __future__ import annotations

import json
import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from apps.api.app.main import app

PROJECT = "boss-dogfood"


@pytest.fixture
def client() -> TestClient:
    return TestClient(app, raise_server_exceptions=False)


@pytest.fixture(autouse=True)
def clean(engine):
    def wipe():
        with engine.begin() as c:
            c.execute(text("DELETE FROM autonomy_policies"))
    wipe()
    yield
    wipe()


def test_the_policy_reads_with_its_one_line(client):
    got = client.get("/v1/autonomy/policy", params={"project_id": PROJECT})
    assert got.status_code == 200, got.text
    body = got.json()
    assert body["policy"]["dev"] == "act" and body["policy"]["prod"] == "ask"
    assert body["policy"]["line"] == "Development: agents act · Production: you decide"


def test_a_project_is_only_ever_stricter_than_its_org(client):
    org = client.put("/v1/autonomy/org-policy", json={
        "project_id": PROJECT, "policy": {"dev": "act_capped"}, "actor": "ceo"})
    assert org.status_code == 200, org.text
    looser = client.put("/v1/autonomy/policy", json={
        "project_id": PROJECT, "policy": {"dev": "act"}, "actor": "ceo"})
    assert looser.status_code == 422 and looser.json()["detail"]["error"] == "stricter_only"
    prod = client.put("/v1/autonomy/policy", json={
        "project_id": PROJECT, "policy": {"prod": "act"}, "actor": "ceo"})
    assert prod.status_code == 422
    stricter = client.put("/v1/autonomy/policy", json={
        "project_id": PROJECT, "policy": {"dev": "ask"}, "actor": "ceo"})
    assert stricter.status_code == 200, stricter.text
    assert stricter.json()["policy"]["dev"] == "ask"


def test_is_this_production_is_one_answer(client):
    remote = client.get("/v1/autonomy/resolve", params={"project_id": PROJECT,
                                                        "environment": "eu1"})
    assert remote.status_code == 200
    staging = client.get("/v1/autonomy/resolve", params={"project_id": PROJECT,
                                                         "environment": "staging"})
    install = client.get("/v1/autonomy/resolve", params={"project_id": PROJECT,
                                                         "install": "true"})
    assert remote.json()["verdict"]["prod"] is True
    assert remote.json()["verdict"]["known"] is False
    assert staging.json()["verdict"]["prod"] is False
    assert install.json()["verdict"]["source"] == "install_tier"


def test_decided_for_you_lists_it_and_undo_says_when_there_is_none(client, engine):
    from boss_engine.config import get_settings
    from boss_engine.services import autonomy_gate
    from boss_engine.services.core import create_blocking_decision

    with engine.begin() as c:
        decision_id = create_blocking_decision(
            c, PROJECT, None, f"api autonomy {uuid.uuid4().hex[:8]}", {"action": "decide"}, {},
            "test")
        assert autonomy_gate.decide(c, get_settings(), decision_id)["decided"] is True
    listed = client.get("/v1/autonomy/decided", params={"project_id": PROJECT})
    assert listed.status_code == 200, listed.text
    item = next(i for i in listed.json()["items"] if i["decision_id"] == decision_id)
    assert item["words"].endswith("under dev autonomy") and item["undo"] is None
    undo = client.post(f"/v1/autonomy/decided/{decision_id}/undo",
                       json={"project_id": PROJECT, "actor": "ceo"})
    assert undo.status_code == 409
    reviewed = client.post(f"/v1/autonomy/decided/{decision_id}/reviewed",
                           json={"project_id": PROJECT, "actor": "ceo"})
    assert reviewed.status_code == 200, reviewed.text
    with engine.begin() as c:
        rec = c.execute(text("SELECT recommendation FROM decisions WHERE id = :d"),
                        {"d": decision_id}).scalar()
    rec = rec if isinstance(rec, dict) else json.loads(rec)
    assert rec["autonomy"]["human_reviewed"] is True
