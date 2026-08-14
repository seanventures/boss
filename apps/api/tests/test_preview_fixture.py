"""No-spend preview fixture: View HTML 200 without a sandbox."""

from __future__ import annotations


def test_fixture_preview_view_html_is_200_and_honest(client):
    created = client.post(
        "/v1/previews/fixture",
        json={"project_id": "boss-dogfood", "actor": "ceo", "ttl_minutes": 15},
    )
    assert created.status_code == 201, created.text
    body = created.json()
    assert body["status"] == "running"
    assert body["mode"] == "fixture"
    assert body["fixture"] is True
    assert body["simulated"] is True
    assert "127.0.0.1" not in (body.get("url") or "")
    assert "127.0.0.1" not in (body.get("open_url") or "")
    assert "/v1/previews/" in (body.get("open_url") or body.get("url") or "")
    preview_id = body["id"]

    view = client.get(f"/v1/previews/{preview_id}/web/")
    assert view.status_code == 200, view.text
    html = view.text
    assert "text/html" in (view.headers.get("content-type") or "")
    assert "fixture — not a sandboxed app" in html
    assert "Preview View path is live" in html
    assert "127.0.0.1" not in html

    listed = client.get("/v1/previews", params={"project_id": "boss-dogfood", "limit": 20})
    assert listed.status_code == 200, listed.text
    rows = listed.json().get("previews") or listed.json().get("items") or []
    match = next((row for row in rows if row.get("id") == preview_id), None)
    assert match is not None
    assert match["status"] == "running"
    assert "127.0.0.1" not in (match.get("url") or "")
    assert "127.0.0.1" not in (match.get("open_url") or "")


def test_fixture_preview_requires_actor_and_project(client):
    missing = client.post("/v1/previews/fixture", json={})
    assert missing.status_code == 422
