"""The project wiki through the HTTP surface (BOSSAPP-891)."""
from __future__ import annotations

import uuid

PROJECT = "boss-dogfood"


def _slug() -> str:
    return f"page-{uuid.uuid4().hex[:8]}"


def test_a_person_edits_reads_lists_and_restores(client):
    slug = _slug()
    put = client.put(f"/v1/wiki/pages/{slug}", json={
        "project_id": PROJECT, "actor": "ceo", "title": "Deploys", "body": "See [[runbook]].\n"})
    assert put.status_code == 200, put.text
    assert put.json()["changed"]
    client.put(f"/v1/wiki/pages/{slug}", json={
        "project_id": PROJECT, "actor": "ceo", "title": "Deploys", "body": "Deploy daily.\n"})
    page = client.get(f"/v1/wiki/pages/{slug}", params={"project_id": PROJECT})
    assert page.status_code == 200 and page.json()["body"] == "Deploy daily.\n"
    listed = client.get("/v1/wiki/pages", params={"project_id": PROJECT}).json()["pages"]
    assert any(p["slug"] == slug and p["revision_no"] == 2 for p in listed)
    revs = client.get(f"/v1/wiki/pages/{slug}/revisions", params={"project_id": PROJECT}).json()
    assert [r["revision_no"] for r in revs["revisions"]] == [2, 1]
    restored = client.post(f"/v1/wiki/pages/{slug}/restore",
                           json={"project_id": PROJECT, "actor": "ceo", "revision": 1})
    assert restored.status_code == 200, restored.text
    assert restored.json()["revision"]["body"] == "See [[runbook]].\n"


def test_a_proposal_waits_and_a_missing_page_is_404(client):
    slug = _slug()
    proposed = client.post(f"/v1/wiki/pages/{slug}/propose", json={
        "project_id": PROJECT, "actor": "ceo", "title": "New", "body": "Hello.\n"})
    assert proposed.status_code == 200, proposed.text
    assert proposed.json()["status"] == "pending" and proposed.json()["decision_id"]
    missing = client.get(f"/v1/wiki/pages/{slug}", params={"project_id": PROJECT})
    assert missing.status_code == 404


def test_a_bad_slug_is_refused_in_words(client):
    r = client.put("/v1/wiki/pages/Not_A_Slug", json={
        "project_id": PROJECT, "actor": "ceo", "title": "x", "body": ""})
    assert r.status_code == 422 and "slug" in r.json()["detail"]


def test_maintain_renders_the_graph(client):
    r = client.post("/v1/wiki/maintain", json={"project_id": PROJECT, "actor": "ceo"})
    assert r.status_code == 200, r.text
    assert "knowledge" in (r.json()["written"] + r.json()["unchanged"] + r.json()["proposed"])
