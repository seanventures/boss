"""An agent is born with a passport: an id that never changes, a birth, a sponsor, a version.

Agent World (agent-world spec §4.1, §5.3). A `roles` row keyed by a reusable string could not
say which agent did yesterday's work, who made it or who answers for it. Now a create through
the real API births the agent — an agt_ id, born_by/sponsor = the actor, born_from, `agent.born`
and `agent.sealed` v1 — and every real change to what it is or holds seals a new version, once
per request, while an unchanged save seals nothing. Identity fields cannot be written.
"""
from __future__ import annotations

from uuid import uuid4

from sqlalchemy import text

from boss_engine.services.agent_identity import AGENT_ID, IDENTITY_FIXED

PROJECT = "boss-dogfood"


def _key(prefix: str = "born") -> str:
    return f"{prefix}_{uuid4().hex[:8]}"


def _events(engine, agent_id: str, kind: str) -> list[dict]:
    with engine.connect() as c:
        return [dict(r) for r in c.execute(text(
            "SELECT actor, data FROM events WHERE project_id = :p AND entity_type = 'agent' "
            "AND entity_id = :a AND event_type = :k ORDER BY created_at"),
            {"p": PROJECT, "a": agent_id, "k": kind}).mappings().all()]


def _passport(client, key: str) -> dict:
    r = client.get(f"/v1/agents/{key}/passport", params={"project_id": PROJECT})
    assert r.status_code == 200, r.text
    return r.json()


def _patch(client, key: str, **body):
    return client.patch(f"/v1/agents/{key}",
                        json={"project_id": PROJECT, "actor": "ceo", **body})


def test_an_agent_made_from_an_archetype_is_born_with_a_passport(client, engine):
    key = _key()
    made = client.post("/v1/agents", json={
        "project_id": PROJECT, "actor": "ceo", "name": "Founder Lens", "role_key": key,
        "born_from": {"kind": "archetype", "archetype": "visionary_founder"}})
    assert made.status_code == 201, made.text
    body = made.json()
    assert AGENT_ID.match(body["agent_id"])
    assert body["version"] == 1 and body["lifecycle"] == "active"
    # The archetype's voice and remit came with it; the operator's name stays theirs.
    assert body["persona"]["callsign"] == "Nova"
    assert body["persona"]["archetype"] == "visionary_founder"
    assert body["name"] == "Founder Lens"
    assert "hundred times" in body["mission"]

    passport = _passport(client, key)
    assert passport["agent_id"] == body["agent_id"]
    assert passport["born"]["by"] == "ceo"
    assert passport["born"]["from"] == {"kind": "archetype", "archetype": "visionary_founder"}
    assert passport["sponsor"] == "ceo"
    assert passport["version"] == 1 and len(passport["fingerprint"]) == 64
    assert passport["identity"] == {"mode": "local", "tokens": False}
    assert set(passport["vitals"]) == {"trust", "runs", "clean_rate", "cost_7d", "panels", "apps"}
    assert passport["presence"]["state"] == "idle"
    # Its own face: seeded by its own id, not the shipped Nova's.
    assert passport["avatar"] == {"kit": "boss-v1", "seed": body["agent_id"]}

    born = _events(engine, body["agent_id"], "agent.born")
    sealed = _events(engine, body["agent_id"], "agent.sealed")
    assert len(born) == 1 and born[0]["actor"] == "ceo"
    assert len(sealed) == 1, "one seal for the birth, however many grants came with it"
    assert sealed[0]["data"]["version"] == 1
    assert sealed[0]["data"]["change_kind"] == "born"
    assert sealed[0]["data"]["fingerprint"] == passport["fingerprint"]


def test_a_voice_change_is_a_new_version_and_an_unchanged_save_is_not(client, engine):
    key = _key()
    made = client.post("/v1/agents", json={
        "project_id": PROJECT, "actor": "ceo", "name": "Release Auditor", "role_key": key,
        "persona": {"callsign": "Audra", "tagline": "Prove it shipped."}})
    assert made.status_code == 201, made.text
    agent_id = made.json()["agent_id"]

    voice = {"callsign": "Audra", "tagline": "Prove it shipped, then prove it again.",
             "voice": {"blunt": 3, "thorough": 2, "bold": 1, "casual": 1}}
    first = _patch(client, key, persona=voice)
    assert first.status_code == 200, first.text
    assert first.json()["version"] == 2 and first.json()["warnings"] == []
    assert first.json()["persona"]["tagline"] == "Prove it shipped, then prove it again."

    again = _patch(client, key, persona=voice)
    assert again.status_code == 200
    assert again.json()["version"] == 2, "an identical save must not move the version"
    assert again.json()["fingerprint"] == first.json()["fingerprint"]
    assert [e["data"]["version"] for e in _events(engine, agent_id, "agent.sealed")] == [1, 2]
    assert _events(engine, agent_id, "agent.sealed")[-1]["data"]["change_kind"] == "persona"


def test_a_voice_change_and_a_grant_in_one_save_seal_once(client, engine):
    skill = client.get("/v1/skills", params={"project_id": PROJECT}).json()["items"][0]["id"]
    key = _key()
    made = client.post("/v1/agents", json={"project_id": PROJECT, "actor": "ceo",
                                           "name": "Batch Agent", "role_key": key})
    agent_id = made.json()["agent_id"]
    both = _patch(client, key, persona={"tagline": "One save, one version."},
                  grant_skills=[skill])
    assert both.status_code == 200, both.text
    assert both.json()["version"] == 2
    sealed = _events(engine, agent_id, "agent.sealed")
    assert [e["data"]["version"] for e in sealed] == [1, 2]
    assert sealed[-1]["data"]["change_kind"] == "persona"  # the most significant change
    assert f"skill:{skill}" in sealed[-1]["data"]["canonical"]["grants"]

    # A grant on its own (the Skills page, one at a time) is a version too.
    other = client.get("/v1/skills", params={"project_id": PROJECT}).json()["items"][1]["id"]
    granted = client.post("/v1/role-skills", json={"project_id": PROJECT, "role_key": key,
                                                   "skill_id": other, "actor": "ceo"})
    assert granted.status_code == 201, granted.text
    assert _passport(client, key)["version"] == 3
    assert _events(engine, agent_id, "agent.sealed")[-1]["data"]["change_kind"] == "grant"


def test_identity_fields_cannot_be_written(client):
    key = _key()
    made = client.post("/v1/agents", json={"project_id": PROJECT, "actor": "ceo",
                                           "name": "Fixed Identity", "role_key": key})
    original = made.json()["agent_id"]
    for field, value in (("agent_id", "agt_aaaaaaaaaa"), ("sponsor", "someone"),
                         ("lifecycle", "retired"), ("fingerprint", "0" * 64)):
        refused = _patch(client, key, **{field: value})
        assert refused.status_code == 422, (field, refused.text)
        assert refused.json()["detail"] == {"field": field, "message": IDENTITY_FIXED}
    assert _passport(client, key)["agent_id"] == original


def test_every_shipped_agent_already_has_an_identity(client):
    items = client.get("/v1/agents/identities", params={"project_id": PROJECT}).json()["items"]
    by_key = {i["role_key"]: i for i in items}
    for key in ("qa", "pm", "engineering_captain", "backend_engineer", "frontend_engineer",
                "operations_engineer", "sre", "chief_architect"):
        assert AGENT_ID.match(by_key[key]["agent_id"] or ""), key
        assert by_key[key]["callsign"], f"{key} shipped without a callsign"
    assert all(i["agent_id"] for i in items), "every role row has an agent_id after backfill"
    # Shipped agents are the pack's: no sponsor, so their owners answer for them.
    assert _passport(client, "qa")["born"]["by"] == "spec-pack"
    assert _passport(client, "qa")["sponsor"] is None


def test_a_role_registered_by_hand_is_born_too(client):
    key = _key("registered")
    made = client.post("/v1/roles", json={"project_id": PROJECT, "role_key": key,
                                          "display_name": "Hand Made", "actor": "ceo"})
    assert made.status_code == 201, made.text
    passport = _passport(client, key)
    assert AGENT_ID.match(passport["agent_id"])
    assert passport["born"]["by"] == "ceo" and passport["sponsor"] == "ceo"


def test_a_template_birth_starts_from_the_shipped_role_and_the_operator_wins(client):
    key = _key()
    made = client.post("/v1/agents", json={
        "project_id": PROJECT, "actor": "ceo", "name": "Second QA", "role_key": key,
        "purpose": "Checks the mobile build only.",
        "born_from": {"kind": "template", "template_key": "qa"},
        "persona": {"callsign": "Vex"}})
    assert made.status_code == 201, made.text
    body = made.json()
    assert body["mission"] == "Checks the mobile build only."   # what they wrote wins
    assert body["persona"]["callsign"] == "Vex"
    assert body["persona"]["tagline"] == "Show me the test."     # the rest came from qa
    assert body["boundaries"]["must_not"] == ["Rubber-stamp untested work."]
    assert _passport(client, key)["born"]["from"] == {"kind": "template", "template_key": "qa"}


def test_an_unknown_blueprint_is_refused_and_nothing_is_made(client, engine):
    key = _key()
    for born_from in ({"kind": "archetype", "archetype": "famous_ceo"},
                      {"kind": "template", "template_key": "no_such_role"},
                      {"kind": "clone"}):
        refused = client.post("/v1/agents", json={"project_id": PROJECT, "actor": "ceo",
                                                  "name": "Nope", "role_key": key,
                                                  "born_from": born_from})
        assert refused.status_code == 422, refused.text
    with engine.connect() as c:
        assert c.execute(text("SELECT count(*) FROM roles WHERE project_id = :p AND "
                              "role_key = :k"), {"p": PROJECT, "k": key}).scalar() == 0


def test_the_card_is_an_a2a_card_that_leaks_nothing_internal(client):
    card = client.get("/v1/agents/qa/card", params={"project_id": PROJECT})
    assert card.status_code == 200, card.text
    body = card.json()
    assert body["protocolVersion"] == "1.0"
    assert body["name"] == "Vera · QA Lead"
    assert body["description"].startswith("Show me the test.")
    assert body["x-boss"]["agent_id"] == _passport(client, "qa")["agent_id"]
    assert body["x-boss"]["uri"].endswith(f"/agents/{body['x-boss']['agent_id']}")
    assert body["x-boss"]["signed"] is False and body["x-boss"]["ai_agent"] is True
    flat = str(body).lower()
    for leak in ("mcp_server", "web-intel", "repository", "cost"):
        assert leak not in flat, f"the card leaks {leak}"


def test_identities_carry_an_etag_and_answer_304_when_nothing_changed(client):
    first = client.get("/v1/agents/identities", params={"project_id": PROJECT})
    assert first.status_code == 200 and first.headers.get("etag")
    assert first.json()["aliases"]["qa lead"] == "qa"
    assert first.json()["aliases"]["vera"] == "qa"
    again = client.get("/v1/agents/identities", params={"project_id": PROJECT},
                       headers={"If-None-Match": first.headers["etag"]})
    assert again.status_code == 304


# ── Every project's agents, not only the default one's ──────────────────────────────────────


def _project_with_pre_identity_agents(engine) -> tuple[str, list[str]]:
    """Another project on this install with roles from before Agent World: no agent_id."""
    from uuid import uuid4

    other = f"aw-other-{uuid4().hex[:8]}"
    keys = [f"legacy_{uuid4().hex[:6]}", "qa"]
    with engine.begin() as c:
        workspace = c.execute(text("SELECT workspace_id FROM projects WHERE id = :p"),
                              {"p": PROJECT}).scalar_one()
        c.execute(text("INSERT INTO projects (id, workspace_id, name, slug) "
                       "VALUES (:id, :w, 'Other', :id)"), {"id": other, "w": workspace})
        for key in keys:
            c.execute(text("INSERT INTO roles (id, project_id, role_key, display_name, spec) "
                           "VALUES (:id, :p, :k, :k, '{}'::jsonb)"),
                      {"id": f"role_{uuid4().hex[:16]}", "p": other, "k": key})
    return other, keys


def test_the_resolver_list_never_shows_an_agent_without_its_id(client, engine):
    """identities() backfills as passport does, so a face never changes on first click."""
    other, keys = _project_with_pre_identity_agents(engine)
    items = {i["role_key"]: i for i in client.get(
        "/v1/agents/identities", params={"project_id": other}).json()["items"]}
    for key in keys:
        assert AGENT_ID.match(items[key]["agent_id"] or ""), key
        passport = client.get(f"/v1/agents/{key}/passport",
                              params={"project_id": other}).json()
        assert passport["agent_id"] == items[key]["agent_id"]
        assert passport["avatar"] == items[key]["avatar"], "one agent, one face"


def test_startup_backfills_every_project_on_the_install(engine):
    from apps.api.app import main as api_main

    other, keys = _project_with_pre_identity_agents(engine)
    assert api_main._backfill_agent_identities() >= len(keys)
    with engine.connect() as c:
        missing = c.execute(text(
            "SELECT count(*) FROM roles WHERE agent_id IS NULL OR fingerprint IS NULL")).scalar()
    assert missing == 0, "every role row on every project has an agent_id after backfill"
    with engine.connect() as c:
        born = c.execute(text("SELECT count(*) FROM events WHERE project_id = :p "
                              "AND event_type = 'agent.born'"), {"p": other}).scalar()
    assert born == 0, "quiet: no birth dated at the upgrade"
