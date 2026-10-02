"""The skills and MCP API: create vs update, on/off, tier 0, deletes that check, servers edited.

Each test names the incident from the R1 milestone map that it pins. They run through the HTTP
surface the console and the chat both reach, because every one of these was a route that
answered 200 and did the wrong thing — or a 500 where a sentence belonged.
"""
from __future__ import annotations

import uuid

PROJECT = "boss-dogfood"


def _uid(prefix: str) -> str:
    """A name no other test used. The suite's table reset does not cover skill_definitions or
    mcp_servers, so a fixed name would collide with the previous test's row."""
    return f"{prefix}_{uuid.uuid4().hex[:8]}"


def _save(client, key, **extra):
    body = {"project_id": PROJECT, "actor": "ceo", "skill_key": key, "label": key,
            "behaviour_kind": "builtin", "config": {"executor_id": "model.generate_text"}}
    body.update(extra)
    return client.post("/v1/skill-definitions", json=body)


def _register(client, name=None, url="http://tracker:9/mcp", **extra):
    name = name or _uid("tracker")
    return client.post("/v1/mcp/servers", json={"project_id": PROJECT, "actor": "ceo",
                                                "name": name, "url": url, **extra})


def test_tier_zero_is_saved_as_zero(client):
    """`int(payload.get('risk_tier') or 1)` saved every tier-0 skill as tier 1."""
    key = _uid("read_notes")
    r = _save(client, key, risk_tier=0)
    assert r.status_code == 201, r.text
    assert r.json()["skill"]["risk_tier"] == 0


def test_create_refuses_a_taken_key_with_409(client):
    """Saving silently replaced whatever skill already had the key."""
    key = _uid("read_notes")
    assert _save(client, key, mode="create").status_code == 201
    again = _save(client, key, mode="create", label="Something else")
    assert again.status_code == 409
    assert "Open it with Edit" in again.json()["detail"]
    # An explicit update still changes it, and the original upsert still works for scripts.
    assert _save(client, key, mode="update", label="Renamed").status_code == 201
    assert _save(client, key, label="Upserted").status_code == 201


def test_a_skill_is_turned_off_without_resaving_it(client):
    key = _uid("read_notes")
    _save(client, key)
    r = client.patch(f"/v1/skill-definitions/{key}",
                     json={"project_id": PROJECT, "actor": "ceo", "enabled": False})
    assert r.status_code == 200, r.text
    assert r.json()["skill"]["enabled"] is False
    listed = client.get("/v1/skill-definitions", params={"project_id": PROJECT}).json()
    row = next(s for s in listed["skills"] if s["skill_key"] == key)
    assert row["enabled"] is False
    # The catalogue's view travels with the row, so the page can say whether it can run.
    assert set(row["catalogue"]) >= {"risk_tier", "usable", "reason"}
    assert row["references"] == {"custom_steps": [], "agents": []}


def test_deleting_a_held_skill_asks_first_then_releases_it(client):
    """Deleting left the grant behind: 'no such skill here any more', on the agent forever."""
    key = _uid("read_notes")
    _save(client, key)
    granted = client.post("/v1/role-skills", json={"project_id": PROJECT, "actor": "ceo",
                                                   "role_key": "pm", "skill_id": key})
    assert granted.status_code == 201, granted.text
    refused = client.delete(f"/v1/skill-definitions/{key}",
                            params={"project_id": PROJECT, "actor": "ceo"})
    assert refused.status_code == 409 and "held by pm" in refused.json()["detail"]
    done = client.delete(f"/v1/skill-definitions/{key}",
                         params={"project_id": PROJECT, "actor": "ceo", "release_grants": True})
    assert done.status_code == 200, done.text
    assert done.json()["released_from"] == ["pm"]


def test_an_authored_skill_is_bindable_and_its_step_usable(client):
    """The builder listed project_skills; resolve_risk called authored skills unregistered."""
    key = _uid("read_notes")
    _save(client, key, risk_tier=0, trusted=True)
    bindable = client.get("/v1/delivery-workflows/custom-steps",
                          params={"project_id": PROJECT}).json()["bindable"]["skills"]
    mine = next(s for s in bindable if s["skill_id"] == key)
    assert mine["source"] == "authored" and mine["usable"] is True
    assert any(s["skill_id"] == "model.generate_text" for s in bindable)


def test_a_duplicate_server_name_is_409(client):
    name = _uid("tracker")
    assert _register(client, name).status_code == 201
    dup = _register(client, name.upper())
    assert dup.status_code == 409 and "already registered" in dup.json()["detail"]


def test_a_server_is_edited_in_place(client):
    """Only `enabled` could change; a new URL meant delete-and-re-register, which unbound
    every step on the server."""
    server = _register(client).json()
    r = client.patch(f"/v1/mcp/servers/{server['id']}", params={"project_id": PROJECT},
                     json={"url": "http://tracker.internal:9/mcp", "risk_tier": 0,
                           "description": "Our tracker", "tool_risk_overrides": {"rm": 3},
                           "trusted": True})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["id"] == server["id"] and body["url"] == "http://tracker.internal:9/mcp"
    assert body["risk_tier"] == 0 and body["trusted"] is True
    # The original toggle still works.
    off = client.patch(f"/v1/mcp/servers/{server['id']}",
                       params={"project_id": PROJECT, "enabled": False})
    assert off.status_code == 200 and off.json()["enabled"] is False
    bad = client.patch(f"/v1/mcp/servers/{server['id']}", params={"project_id": PROJECT},
                       json={"url": "not a url"})
    assert bad.status_code == 422 and "absolute HTTP(S) URL" in bad.json()["detail"]


def test_a_server_in_use_is_not_removed(client):
    server = _register(client).json()
    ticket = _uid("file_ticket")
    _save(client, ticket, behaviour_kind="mcp_tool",
          config={"mcp_server_id": server["id"], "tool_name": "create_issue"})
    listed = client.get("/v1/mcp/servers", params={"project_id": PROJECT}).json()["items"]
    row = next(s for s in listed if s["id"] == server["id"])
    assert [s["skill_key"] for s in row["references"]["skills"]] == [ticket]
    refused = client.delete(f"/v1/mcp/servers/{server['id']}", params={"project_id": PROJECT})
    assert refused.status_code == 409 and ticket in refused.json()["detail"]


def test_listing_does_not_wait_on_a_dead_server_unless_asked(client, monkeypatch):
    """The builder probed every server inline with a 6-second timeout, every load."""
    from boss_engine.services.mcp_client import MCPClient

    asked = []

    def refuse(self, url, headers=None):
        asked.append(url)
        raise ConnectionError("refused")

    monkeypatch.setattr(MCPClient, "list_tools", refuse)
    server = _register(client, url="http://dead-tracker:9/mcp").json()

    def mine(items):
        return next(s for s in items if s["id"] == server["id"])

    plain = client.get("/v1/mcp/servers", params={"project_id": PROJECT}).json()["items"]
    assert asked == [] and mine(plain)["reachable"] is None
    checked = client.get("/v1/mcp/servers",
                         params={"project_id": PROJECT, "check": "now"}).json()["items"]
    assert asked.count("http://dead-tracker:9/mcp") == 1
    assert mine(checked)["reachable"] is False
    assert "Unreachable from the BOSS API" in mine(checked)["status_note"]
