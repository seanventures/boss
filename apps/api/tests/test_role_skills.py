"""Giving a skill to an agent.

A skill attaches at two independent ends: a workflow step RUNS it
(`workflow_custom_steps.skill_id`) and an agent HOLDS it (`role_skills`). Only the first
existed, so the seventeen skills that ship with BOSS could be listed and never given to
anybody — reported as "we need to assign those 17 skills to the Agents out there".

Independent on purpose: a step is "this happens at this point in the path", an agent holding a
skill is "this member of the workforce can do this when its own work calls for it". Both,
either or neither must be expressible, which is what these tests pin.
"""

import pytest

from apps.api.app import main as api_main


PROJECT = "boss-dogfood"


def _assign(client, role_key, skill_id, actor="ceo"):
    return client.post(
        "/v1/role-skills",
        json={"project_id": PROJECT, "role_key": role_key,
              "skill_id": skill_id, "actor": actor},
    )


def _a_builtin_skill(client) -> str:
    items = client.get("/v1/skills", params={"project_id": PROJECT}).json()["items"]
    assert items, "no built-in skills registered; the fixture changed"
    return items[0]["id"]


def test_a_builtin_skill_can_be_given_to_an_agent(client):
    skill = _a_builtin_skill(client)
    r = _assign(client, "deployment_manager", skill)
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["assigned"] is True
    # The source is recorded, not inferred from the shape of the key: an authored skill and a
    # shipped one live in different tables and a reader should not have to guess which.
    assert body["source"] == "builtin"

    listed = client.get("/v1/role-skills", params={"project_id": PROJECT}).json()
    assert {"role_key": "deployment_manager", "skill_id": skill} in [
        {"role_key": a["role_key"], "skill_id": a["skill_id"]}
        for a in listed["assignments"]
    ]


def test_assigning_twice_is_not_an_error(client):
    """The console assigns from a list it may be holding a stale copy of."""
    skill = _a_builtin_skill(client)
    assert _assign(client, "security_reviewer", skill).status_code == 201
    assert _assign(client, "security_reviewer", skill).status_code == 201
    rows = [
        a for a in client.get("/v1/role-skills", params={"project_id": PROJECT}).json()["assignments"]
        if a["role_key"] == "security_reviewer" and a["skill_id"] == skill
    ]
    assert len(rows) == 1


def test_unassigning_leaves_the_skill_alone(client):
    """Taking a skill off an agent must not delete the skill or touch any workflow step."""
    skill = _a_builtin_skill(client)
    _assign(client, "deployment_manager", skill)
    before = len(client.get("/v1/skills", params={"project_id": PROJECT}).json()["items"])

    r = client.delete(
        "/v1/role-skills",
        params={"project_id": PROJECT, "role_key": "deployment_manager",
                "skill_id": skill, "actor": "ceo"},
    )
    assert r.status_code == 200, r.text
    assert r.json()["assigned"] is False
    after = len(client.get("/v1/skills", params={"project_id": PROJECT}).json()["items"])
    assert after == before


def test_an_unknown_agent_is_told_which_agents_exist(client):
    """"Unknown role" sends the reader hunting for a typo. The list says more."""
    r = _assign(client, "chief_vibes_officer", _a_builtin_skill(client))
    assert r.status_code == 422
    detail = r.json()["detail"]
    assert "chief_vibes_officer" in detail
    assert "Available:" in detail
    assert "deployment_manager" in detail


def test_an_unknown_skill_is_refused(client):
    r = _assign(client, "deployment_manager", "skill_that_does_not_exist")
    assert r.status_code == 422
    assert "skill_that_does_not_exist" in r.json()["detail"]


def test_assignment_needs_the_same_permission_as_authoring(client):
    """Deciding who can do what with a skill is the same act as writing it."""
    r = _assign(client, "deployment_manager", _a_builtin_skill(client), actor="not-a-member")
    assert r.status_code == 403


def test_the_two_ends_are_independent(client):
    """An agent holding a skill says nothing about any workflow step, and vice versa."""
    skill = _a_builtin_skill(client)
    _assign(client, "deployment_manager", skill)
    steps = client.get(
        "/v1/delivery-workflows/custom-steps", params={"project_id": PROJECT}
    ).json().get("steps", [])
    # Nothing was created on the workflow side by assigning to an agent.
    assert all(s.get("skill_id") != skill for s in steps)
