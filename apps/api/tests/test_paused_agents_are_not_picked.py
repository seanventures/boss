"""A paused agent is never picked, a step naming one fails fast by name, and only its sponsor
(or an owner) may pause, resume or retire it.

Agent World lifecycle (agent-world spec §5.3, §10). Pausing is how an operator takes an agent
out of rotation without deleting its history, so it has to bite where work is assigned —
`step_agents.resolve`, the one rule every step kind goes through — and it has to say who is
paused rather than "nobody holds this skill", which would send the operator to grant something
the agent already holds.
"""
from __future__ import annotations

from uuid import uuid4

import pytest

from boss_engine.services import step_agents

PROJECT = "boss-dogfood"


def _unheld_skill(client) -> str:
    skills = [s["id"] for s in client.get("/v1/skills", params={"project_id": PROJECT})
              .json()["items"]]
    held = {a["skill_id"] for a in client.get("/v1/role-skills", params={"project_id": PROJECT})
            .json()["assignments"]}
    return next(s for s in skills if s not in held)


def _only_holder(client) -> tuple[str, str]:
    skill = _unheld_skill(client)
    key = f"holder_{uuid4().hex[:8]}"
    made = client.post("/v1/agents", json={
        "project_id": PROJECT, "actor": "ceo", "name": "Only Holder", "role_key": key,
        "skills": [skill], "persona": {"callsign": "Solo"}})
    assert made.status_code == 201, made.text
    return key, skill


def _lifecycle(client, key: str, state: str, actor: str = "ceo"):
    return client.post(f"/v1/agents/{key}/lifecycle", json={
        "project_id": PROJECT, "actor": actor, "state": state, "reason": "test"})


def _resolve(engine, step: dict) -> dict:
    with engine.begin() as c:
        return step_agents.resolve(c, None, PROJECT, step, actor="ceo")


def _scoped(skill: str) -> dict:
    return {"id": "work", "kind": "capability",
            "binding": {"agent_work": True, "skills": [skill], "inputs": {}}}


def test_auto_skips_a_paused_agent_and_says_who_is_paused(client, engine):
    key, skill = _only_holder(client)
    assert _resolve(engine, _scoped(skill))["agent"] == key

    paused = _lifecycle(client, key, "paused")
    assert paused.status_code == 200, paused.text
    assert paused.json()["lifecycle"] == "paused"
    assert paused.json()["presence"]["state"] == "paused"
    with pytest.raises(step_agents.StepAgentError) as refused:
        _resolve(engine, _scoped(skill))
    assert str(refused.value).startswith("Solo is paused.")

    assert _lifecycle(client, key, "active").status_code == 200
    assert _resolve(engine, _scoped(skill))["agent"] == key, "resumed: picked again"


def test_a_step_naming_a_paused_agent_fails_fast_by_name(client, engine):
    key, _ = _only_holder(client)
    built_in = {"id": "cost", "kind": "capability", "captain": key,
                "binding": {"capability": "cost_check", "inputs": {}}}
    assert _resolve(engine, built_in)["how"] == "named"
    _lifecycle(client, key, "paused")
    with pytest.raises(step_agents.StepAgentError, match=r"^Solo is paused\."):
        _resolve(engine, built_in)
    _lifecycle(client, key, "retired")
    with pytest.raises(step_agents.StepAgentError, match=r"^Solo is retired\."):
        _resolve(engine, built_in)


def test_with_two_holders_the_paused_one_is_not_in_the_pool(client, engine):
    key, skill = _only_holder(client)
    other = f"holder_{uuid4().hex[:8]}"
    assert client.post("/v1/agents", json={"project_id": PROJECT, "actor": "ceo",
                                           "name": "Second Holder", "role_key": other,
                                           "skills": [skill]}).status_code == 201
    _lifecycle(client, key, "paused")
    picked = _resolve(engine, _scoped(skill))
    assert picked["agent"] == other and picked["how"] == "only_holder"
    assert picked["considered"] == [other]


def test_only_the_sponsor_or_an_owner_may_change_an_agents_lifecycle(client):
    key, _ = _only_holder(client)  # sponsored by ceo, who made it
    # vp_agents may review roles but is neither the sponsor nor an owner (a CEO member).
    refused = _lifecycle(client, key, "retired", actor="vp_agents")
    assert refused.status_code == 403
    assert refused.json()["detail"] == "Only the sponsor can retire this agent"
    assert _lifecycle(client, key, "paused", actor="vp_agents").json()["detail"] == (
        "Only the sponsor can pause this agent")
    retired = _lifecycle(client, key, "retired")
    assert retired.status_code == 200 and retired.json()["lifecycle"] == "retired"
    listed = {a["role_key"]: a for a in client.get(
        "/v1/agents", params={"project_id": PROJECT}).json()["items"]}
    assert listed[key]["lifecycle"] == "retired"


def test_a_shipped_agent_nobody_sponsors_is_its_owners_and_only_theirs(client, engine):
    """No sponsor: only an owner — a HUMAN member with the ceo role — may pause, resume or
    retire it. role_review alone is not ownership: vp_agents may review roles, and
    `executive_console`, a system member, holds role_review too — a principal that is not a
    person could otherwise retire agents."""
    for reviewer in ("vp_agents", "executive_console"):
        refused = _lifecycle(client, "docs_writer", "retired", actor=reviewer)
        assert refused.status_code == 403, (reviewer, refused.text)
        assert refused.json()["detail"] == "Only an owner can retire this agent"
    try:
        paused = _lifecycle(client, "docs_writer", "paused")
        assert paused.status_code == 200, paused.text
        assert paused.json()["sponsor"] is None
    finally:
        assert _lifecycle(client, "docs_writer", "active").status_code == 200


def test_a_ceo_role_on_something_other_than_a_person_is_not_an_owner(client, engine):
    """The owner check asks for a human, as require_autonomy_operator does."""
    from sqlalchemy import text

    with engine.begin() as c:
        c.execute(text("UPDATE project_members SET role_keys = role_keys || '[\"ceo\"]'::jsonb "
                       "WHERE project_id = :p AND member_id = 'executive_console'"),
                  {"p": PROJECT})
        kind = c.execute(text("SELECT member_type FROM project_members WHERE project_id = :p "
                              "AND member_id = 'executive_console'"), {"p": PROJECT}).scalar()
    assert kind != "human"
    refused = _lifecycle(client, "docs_writer", "paused", actor="executive_console")
    assert refused.status_code == 403, refused.text


def test_a_lifecycle_change_is_a_version(client):
    key, _ = _only_holder(client)
    before = client.get(f"/v1/agents/{key}/passport", params={"project_id": PROJECT}).json()
    after = _lifecycle(client, key, "paused").json()
    assert after["version"] == before["version"] + 1
    assert _lifecycle(client, key, "paused").json()["version"] == after["version"]
