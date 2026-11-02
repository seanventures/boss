"""KG P7a: a regulation document and its ontology pack, over HTTP.

GET/POST /v1/knowledge/packs      the project's active ontology pack
GET/POST /v1/knowledge/documents  a regulation document and its clauses

Breaks covered: a document added before any pack is set; the pack/document list not answering
the project that was just set up.
"""
from __future__ import annotations

import uuid

from sqlalchemy import text

HOME = "boss-dogfood"
ACTOR = "ceo"


def _new_project(engine, prefix: str) -> str:
    """A fresh project in boss-dogfood's workspace, with ACTOR holding knowledge:manage there
    (the HOME project's own members are left untouched, so other tests are unaffected)."""
    from apps.api.app import main as api_main

    project_id = f"{prefix}-{uuid.uuid4().hex[:10]}"
    with engine.begin() as connection:
        workspace_id = connection.execute(
            text("SELECT workspace_id FROM projects WHERE id = :id"), {"id": HOME}
        ).scalar_one()
        connection.execute(
            text("INSERT INTO projects (id, workspace_id, name, slug) "
                 "VALUES (:id, :workspace_id, :id, :id)"),
            {"id": project_id, "workspace_id": workspace_id},
        )
        api_main.members.upsert_member(
            connection, project_id=project_id, member_id=ACTOR, display_name="CEO",
            member_type="human", role_keys=[], permissions=["knowledge:manage", "knowledge:read"],
            actor=ACTOR,
        )
    return project_id


def test_set_pack_api_sets_and_changes_the_project_pack(client, engine):  # P1 -> criterion 1
    project_id = _new_project(engine, "pack-api")
    url = "/v1/knowledge/packs"
    first = client.post(url, json={"project_id": project_id, "actor": ACTOR,
                                   "step_up": "confirmed", "pack_id": "banking-regulatory"})
    assert first.status_code == 200, first.text
    assert first.json()["pack_id"] == "banking-regulatory"

    changed = client.post(url, json={"project_id": project_id, "actor": ACTOR,
                                     "step_up": "confirmed", "pack_id": "banking-dq"})
    assert changed.status_code == 200, changed.text
    assert changed.json()["pack_id"] == "banking-dq"

    refused = client.post(url, json={"project_id": project_id, "actor": ACTOR,
                                     "step_up": "confirmed", "pack_id": "not-a-real-pack"})
    assert refused.status_code == 422, refused.text


def test_add_document_is_refused_without_a_pack_then_succeeds_after_one_is_set(client, engine):
    # P1 -> criterion 2
    project_id = _new_project(engine, "doc-api")
    doc_url = "/v1/knowledge/documents"
    body = {"project_id": project_id, "actor": ACTOR, "step_up": "confirmed",
            "title": "Reg Z", "source_url": "https://example.test/regz",
            "citation": "12 CFR 1026",
            "clauses": [{"section": "1026.36(c)(1)(i)", "text": "A loan originator shall not."}]}

    refused = client.post(doc_url, json=body)
    assert refused.status_code == 422, refused.text
    assert "pack" in refused.json()["detail"].lower()

    client.post("/v1/knowledge/packs", json={"project_id": project_id, "actor": ACTOR,
                                             "step_up": "confirmed", "pack_id": "banking-regulatory"})
    added = client.post(doc_url, json=body)
    assert added.status_code == 200, added.text
    added_body = added.json()
    assert added_body["document_id"] and len(added_body["clause_ids"]) == 1

    missing_text = client.post(doc_url, json={**body, "clauses": [{"section": "1026.36(c)(1)(ii)"}]})
    assert missing_text.status_code == 422, missing_text.text


def test_list_documents_returns_what_was_added_with_its_clauses(client, engine):  # P1 -> criterion 3
    project_id = _new_project(engine, "doc-list")
    empty = client.get("/v1/knowledge/documents", params={"project_id": project_id, "actor": ACTOR})
    assert empty.status_code == 200, empty.text
    assert empty.json()["documents"] == []

    client.post("/v1/knowledge/packs", json={"project_id": project_id, "actor": ACTOR,
                                             "step_up": "confirmed", "pack_id": "banking-regulatory"})
    client.post("/v1/knowledge/documents", json={
        "project_id": project_id, "actor": ACTOR, "step_up": "confirmed",
        "title": "Reg Z", "source_url": "https://example.test/regz", "citation": "12 CFR 1026",
        "clauses": [{"section": "1026.36(c)(1)(i)", "text": "A loan originator shall not."},
                    {"section": "1026.36(c)(2)", "text": "A creditor shall maintain records."}]})

    listed = client.get("/v1/knowledge/documents", params={"project_id": project_id, "actor": ACTOR})
    assert listed.status_code == 200, listed.text
    documents = listed.json()["documents"]
    assert len(documents) == 1
    assert documents[0]["title"] == "Reg Z"
    sections = sorted(c["section"] for c in documents[0]["clauses"])
    assert sections == ["1026.36(c)(1)(i)", "1026.36(c)(2)"]


def test_the_aml_sample_loads_through_the_api_and_reports_its_alerts(client, engine):
    """Case study 4: the AML sample is a product action (knowledge:manage, confirmed)."""
    project_id = _new_project(engine, "aml-sample")
    url = "/v1/knowledge/samples/aml"
    before = client.get(url, params={"project_id": project_id, "actor": ACTOR})
    assert before.status_code == 200, before.text
    assert before.json()["loaded"] is False

    unconfirmed = client.post(url, json={"project_id": project_id, "actor": ACTOR})
    assert unconfirmed.status_code == 422, unconfirmed.text

    loaded = client.post(url, json={"project_id": project_id, "actor": ACTOR,
                                    "step_up": "confirmed"})
    assert loaded.status_code == 200, loaded.text
    assert loaded.json()["pack_id"] == "banking-aml"
    after = client.get(url, params={"project_id": project_id, "actor": ACTOR}).json()
    assert after["loaded"] is True and after["pack_id"] == "banking-aml"
    assert [a["alert_id"] for a in after["alerts"]] == ["ALERT-1", "ALERT-2", "ALERT-3"]


def test_the_dq_platform_loads_through_the_api_beside_the_other_packs(client, engine):
    """The DQ case study: the platform is a product action (knowledge:manage, confirmed); it adds
    banking-dq without taking a pack the project already holds, and lists the DQ Monitor's groups by COB."""
    project_id = _new_project(engine, "dq-sample")
    client.post("/v1/knowledge/packs", json={"project_id": project_id, "actor": ACTOR,
                                             "step_up": "confirmed", "pack_id": "banking-aml"})
    url = "/v1/knowledge/samples/dq"
    before = client.get(url, params={"project_id": project_id, "actor": ACTOR})
    assert before.status_code == 200, before.text
    assert before.json()["loaded"] is False
    assert client.post(url, json={"project_id": project_id, "actor": ACTOR}).status_code == 422
    loaded = client.post(url, json={"project_id": project_id, "actor": ACTOR,
                                    "step_up": "confirmed"})
    assert loaded.status_code == 200, loaded.text
    assert loaded.json()["pack_id"] == "banking-dq"
    packs = client.get("/v1/knowledge/packs", params={"project_id": project_id,
                                                       "actor": ACTOR}).json()
    assert {p["pack_id"] for p in packs["packs"]} == {"banking-aml", "banking-dq"}
    after = client.get(url, params={"project_id": project_id, "actor": ACTOR,
                                    "cob": "2026-10-05"}).json()
    assert after["loaded"] is True and after["running_example"] == "EXG-20261005-0003"
    assert [g["group_id"] for g in after["groups"]] == ["EXG-20261005-0003", "EXG-20261005-0005"]
