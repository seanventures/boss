"""The models are the ORG's, through the API (org_models; banking case study 2026-10-24).

A model setting written from one project is read by every project of the org (`source: org`);
`scope: project` pins one project and removing the pin hands it back to the org's choice, not to
the install's. Setup's view says whose models they are, and a job pin is removable there.
"""
from __future__ import annotations

from sqlalchemy import text

PROJECT_ID = "boss-dogfood"
OTHER = "om-api-bank"


def _knob(client, project_id, key):
    body = client.get(f"/v1/settings?project_id={project_id}").json()
    return next(k for k in body["knobs"] if k["key"] == key)


def _other_project(engine):
    with engine.begin() as db:
        ws = db.execute(text("SELECT workspace_id FROM projects WHERE id = :p"),
                        {"p": PROJECT_ID}).scalar()
        db.execute(text("INSERT INTO projects (id, workspace_id, name, slug) VALUES "
                        "(:p, :w, 'Bank', :p) ON CONFLICT (id) DO NOTHING"), {"p": OTHER, "w": ws})


def test_a_model_set_in_one_project_is_every_projects_and_a_pin_is_one_projects(client, engine):
    _other_project(engine)
    saved = client.put("/v1/settings/chat_model",
                       json={"project_id": PROJECT_ID, "actor": "ceo", "value": "claude-sonnet-5"})
    assert saved.status_code == 200, saved.text
    assert saved.json()["source"] == "org"
    knob = _knob(client, OTHER, "chat_model")
    assert (knob["value"], knob["source"], knob["scope"]) == ("claude-sonnet-5", "org", "org")

    pinned = client.put("/v1/settings/chat_model", json={
        "project_id": OTHER, "actor": "ceo", "value": "claude-haiku-5", "scope": "project"})
    assert pinned.status_code == 200 and pinned.json()["source"] == "project"
    assert _knob(client, OTHER, "chat_model")["value"] == "claude-haiku-5"
    assert _knob(client, PROJECT_ID, "chat_model")["value"] == "claude-sonnet-5"

    unpinned = client.put("/v1/settings/chat_model", json={
        "project_id": OTHER, "actor": "ceo", "value": None, "scope": "project"})
    assert unpinned.status_code == 200 and unpinned.json()["source"] == "org"
    assert _knob(client, OTHER, "chat_model")["value"] == "claude-sonnet-5"

    bad = client.put("/v1/settings/chat_model", json={
        "project_id": OTHER, "actor": "ceo", "value": "x", "scope": "everywhere"})
    assert bad.status_code == 422


def test_setup_view_says_whose_models_and_a_job_pin_is_removable(client, engine):
    _other_project(engine)
    client.put("/v1/settings/code_review_engine", json={
        "project_id": OTHER, "actor": "ceo", "value": "claude_code", "scope": "project"})
    view = client.get(f"/v1/setup?project_id={OTHER}")
    assert view.status_code == 200, view.text
    body = view.json()
    assert body["models_scope"]["scope"] == "org"
    assert [o["key"] for o in body["models_scope"]["overrides"]] == ["code_review_engine"]
    review = next(j for j in body["jobs"] if j["job"] == "review")
    assert review["scope"] == "project"
    cleared = client.put("/v1/setup/jobs/review", json={
        "project_id": OTHER, "actor": "ceo", "choice": None, "scope": "project"})
    assert cleared.status_code == 200, cleared.text
    assert next(j for j in cleared.json()["jobs"] if j["job"] == "review")["scope"] == "org"
    assert cleared.json()["models_scope"]["overrides"] == []
