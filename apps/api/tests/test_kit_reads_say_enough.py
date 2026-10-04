"""The Library and the drawer get what they need from the API (tester round 1, K13/K22).

`/v1/skills` says whether a skill can run here (the provider cost syncs have no runner, so the
Library showed them Ready); each Kit `used_in` entry says which part of the ability the step
uses (`via`), so a Library row counts its own steps; each ability's MCP server carries its URL
for the Review popover.
"""
from __future__ import annotations

import json

from sqlalchemy import text

PROJECT = "boss-dogfood"


def test_a_skill_nothing_can_run_is_not_usable(client):
    items = {i["id"]: i for i in client.get(
        "/v1/skills", params={"project_id": PROJECT}).json()["items"]}
    cost = items["anthropic.usage_cost.sync"]
    assert cost["usable"] is False and cost["reason"]
    assert items["repo.read"]["usable"] is True and items["repo.read"]["reason"] is None


def test_used_in_names_what_the_step_uses_and_servers_carry_their_url(client, engine):
    kit = client.get("/v1/kit", params={"project_id": PROJECT, "check": "none"}).json()
    research = next(a for a in kit["abilities"] if a["key"] == "web_research")
    assert all(m["url"] for m in research["mcp"]), research["mcp"]
    from boss_engine.services import kit as kit_service
    from boss_engine.config import get_settings

    spec = {"steps": [{"id": "s1", "title": "Draft", "binding": {
        "skills": ["github.create_pr_real"], "mcp": ["web-intel"]}}]}
    with engine.begin() as c:
        c.execute(text("""INSERT INTO workflow_definitions (id, project_id, workflow_key, name, created_by)
                          VALUES ('wfd_via', :p, 'wf_via', 'Via test', 't')
                          ON CONFLICT DO NOTHING"""), {"p": PROJECT})
        c.execute(text("""INSERT INTO workflow_versions (id, project_id, workflow_definition_id,
                                                         version, spec, content_hash, created_by)
                          VALUES ('wfv_via', :p, 'wfd_via', 1, CAST(:s AS JSONB), 'h', 't')
                          ON CONFLICT DO NOTHING"""), {"p": PROJECT, "s": json.dumps(spec)})
        c.execute(text("UPDATE workflow_definitions SET active_version_id = 'wfv_via' "
                       "WHERE id = 'wfd_via'"))
        abilities = kit_service.definitions(get_settings())["abilities"]
        used = kit_service.used_in(c, get_settings(), PROJECT, abilities)
        c.execute(text("UPDATE workflow_definitions SET active_version_id = NULL "
                       "WHERE id = 'wfd_via'"))
        c.execute(text("DELETE FROM workflow_versions WHERE id = 'wfv_via'"))
        c.execute(text("DELETE FROM workflow_definitions WHERE id = 'wfd_via'"))
    assert [u["via"] for u in used["open_prs"] if u["workflow_key"] == "wf_via"] == [
        "skill:github.create_pr_real"]
    assert {u["via"] for u in used["web_research"] if u["workflow_key"] == "wf_via"} == {
        "mcp:web-intel"}
