"""Upload a document, over HTTP (rulebook source G3, G7, G6, G9).

POST /v1/knowledge/documents/uploads                    the file (base64): a PREVIEW, nothing written
POST /v1/knowledge/documents/uploads/{id}/edit|confirm  a person changes the split, then confirms
GET/POST /v1/knowledge/samples/dq-rulebook              the NorthPeak rulebook through the same path
GET /v1/knowledge/view/search/compare, /view/names      words vs meaning; citation names

Breaks covered: a write before the confirmation; a missing step-up accepted; a broken file
answered with a 500; the DQ sample not staged as a preview; a raw id where a name belongs.
"""
from __future__ import annotations

import base64
import uuid
from pathlib import Path

from sqlalchemy import text

HOME = "boss-dogfood"
ACTOR = "ceo"
PDF = Path(__file__).resolve().parents[3] / "tools" / "dq_sample" / "NorthPeak_Loan_DQ_Rulebook_v3.2.pdf"


def _new_project(engine, prefix: str) -> str:
    from apps.api.app import main as api_main

    project_id = f"{prefix}-{uuid.uuid4().hex[:10]}"
    with engine.begin() as connection:
        workspace_id = connection.execute(
            text("SELECT workspace_id FROM projects WHERE id = :id"), {"id": HOME}).scalar_one()
        connection.execute(text("INSERT INTO projects (id, workspace_id, name, slug) "
                                "VALUES (:id, :workspace_id, :id, :id)"),
                           {"id": project_id, "workspace_id": workspace_id})
        api_main.members.upsert_member(
            connection, project_id=project_id, member_id=ACTOR, display_name="CEO", member_type="human",
            role_keys=[], permissions=["knowledge:manage", "knowledge:read"], actor=ACTOR)
    return project_id


def _clauses(engine, project_id: str) -> int:
    with engine.connect() as connection:
        return int(connection.execute(text(
            "SELECT count(*) FROM kg_nodes WHERE project_id = :p AND kind = 'clause'"), {"p": project_id}).scalar())


def test_an_upload_is_a_preview_until_a_person_confirms_it(client, engine):
    project_id = _new_project(engine, "upload-api")
    client.post("/v1/knowledge/packs", json={"project_id": project_id, "actor": ACTOR, "step_up": "confirmed",
                                             "pack_id": "banking-regulatory"})
    body = {"project_id": project_id, "actor": ACTOR, "filename": "policy.txt",
            "content_base64": base64.b64encode(b"1. Scope\nAll loans.\n2. Rule\nNo nulls.\n3. Owner\nOps.").decode()}
    assert client.post("/v1/knowledge/documents/uploads", json=body).status_code == 422, "step-up is required"
    staged = client.post("/v1/knowledge/documents/uploads", json={**body, "step_up": "confirmed"})
    assert staged.status_code == 200, staged.text
    preview = staged.json()
    assert [s["title"] for s in preview["sections"]] == ["1. Scope", "2. Rule", "3. Owner"]
    assert preview["status"] == "pending" and _clauses(engine, project_id) == 0
    listed = client.get("/v1/knowledge/documents/uploads", params={"project_id": project_id, "actor": ACTOR})
    assert [u["upload_id"] for u in listed.json()["uploads"]] == [preview["upload_id"]]
    lines = client.get(f"/v1/knowledge/documents/uploads/{preview['upload_id']}/sections/s1",
                       params={"project_id": project_id, "actor": ACTOR})
    assert lines.json()["lines"] == ["1. Scope", "All loans."]

    edited = client.post(f"/v1/knowledge/documents/uploads/{preview['upload_id']}/edit",
                         json={"project_id": project_id, "actor": ACTOR, "step_up": "confirmed",
                               "edits": [{"op": "merge", "key": "s2"}]})
    assert edited.status_code == 200 and edited.json()["counts"]["sections"] == 2
    bad = client.post(f"/v1/knowledge/documents/uploads/{preview['upload_id']}/edit",
                      json={"project_id": project_id, "actor": ACTOR, "step_up": "confirmed",
                            "edits": [{"op": "rewrite", "key": "s1"}]})
    assert bad.status_code == 422 and "Unknown edit" in bad.json()["detail"]

    done = client.post(f"/v1/knowledge/documents/uploads/{preview['upload_id']}/confirm",
                       json={"project_id": project_id, "actor": ACTOR, "step_up": "confirmed"})
    assert done.status_code == 200, done.text
    assert done.json()["sections"] == 2 and _clauses(engine, project_id) == 2
    docs = client.get("/v1/knowledge/documents", params={"project_id": project_id, "actor": ACTOR}).json()["documents"]
    assert docs[0]["filename"] == "policy.txt" and [c["kind"] for c in docs[0]["clauses"]] == ["heading", "heading"]


def test_a_broken_file_is_refused_in_words(client, engine):
    project_id = _new_project(engine, "upload-bad")
    client.post("/v1/knowledge/packs", json={"project_id": project_id, "actor": ACTOR, "step_up": "confirmed",
                                             "pack_id": "banking-regulatory"})
    refused = client.post("/v1/knowledge/documents/uploads", json={
        "project_id": project_id, "actor": ACTOR, "step_up": "confirmed", "filename": "x.pdf",
        "content_base64": base64.b64encode(b"%PDF-1.4 not really").decode()})
    assert refused.status_code == 422 and "x.pdf" in refused.json()["detail"]
    garbled = client.post("/v1/knowledge/documents/uploads", json={
        "project_id": project_id, "actor": ACTOR, "step_up": "confirmed", "filename": "x.pdf",
        "content_base64": "%%%not base64"})
    assert garbled.status_code == 422


def test_the_dq_sample_stages_the_shipped_rulebook_as_a_preview(client, engine):
    project_id = _new_project(engine, "dq-sample")
    client.post("/v1/knowledge/packs", json={"project_id": project_id, "actor": ACTOR, "step_up": "confirmed",
                                             "pack_id": "banking-regulatory"})
    status = client.get("/v1/knowledge/samples/dq-rulebook", params={"project_id": project_id, "actor": ACTOR}).json()
    assert status["available"] is True and status["document_id"] is None
    # banking-regulatory alone holds no data-quality kinds: extraction says why.
    assert status["can_extract"] is False and "banking-dq" in status["extract_blocked_reason"]
    staged = client.post("/v1/knowledge/samples/dq-rulebook",
                         json={"project_id": project_id, "actor": ACTOR, "step_up": "confirmed"})
    assert staged.status_code == 200, staged.text
    preview = staged.json()
    assert preview["pages"] == 45 and preview["counts"]["rule_cards"] == 96 and preview["counts"]["sections"] == 136
    assert _clauses(engine, project_id) == 0
    status = client.get("/v1/knowledge/samples/dq-rulebook", params={"project_id": project_id, "actor": ACTOR}).json()
    assert status["pending_upload_id"] == preview["upload_id"]


def test_names_and_the_comparison_answer_without_raw_ids(client, engine):
    project_id = _new_project(engine, "names-api")
    client.post("/v1/knowledge/packs", json={"project_id": project_id, "actor": ACTOR, "step_up": "confirmed",
                                             "pack_id": "banking-regulatory"})
    added = client.post("/v1/knowledge/documents", json={
        "project_id": project_id, "actor": ACTOR, "step_up": "confirmed", "title": "Reg Z", "citation": "12 CFR 1026",
        "clauses": [{"section": "1026.36(c)(1)(ii)", "text": "A partial payment is held until a full payment."}]}).json()
    clause = added["clause_ids"][0]
    names = client.get("/v1/knowledge/view/names", params={"project_id": project_id, "actor": ACTOR,
                                                           "ids": f"{clause},kgn_notreal0000"}).json()["names"]
    assert names == {clause: {"title": "1026.36(c)(1)(ii)", "kind": "clause", "kind_label": "Clause", "layer": "domain"}}
    compared = client.get("/v1/knowledge/view/search/compare", params={"project_id": project_id, "actor": ACTOR,
                                                                       "q": "partial payment"})
    assert compared.status_code == 200, compared.text
    body = compared.json()
    assert [h["node"]["id"] for h in body["words"]] == [clause]
    assert body["meaning"] == [] and body["meaning_available"] is False and body["reason"]
