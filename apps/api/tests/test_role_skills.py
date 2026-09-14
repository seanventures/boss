"""Giving a skill to an agent.

A skill attaches at two independent ends: a workflow step RUNS it
(`workflow_custom_steps.skill_id`) and an agent HOLDS it (`role_skills`). Only the first
existed, so the seventeen skills that ship with BOSS could be listed and never given to
anybody — reported as "we need to assign those 17 skills to the Agents out there".

Independent on purpose: a step is "this happens at this point in the path", an agent holding a
skill is "this member of the workforce can do this when its own work calls for it". Both,
either or neither must be expressible, which is what these tests pin.
"""


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


# ---------------------------------------------------------------------------
# Assignment is the grant
# ---------------------------------------------------------------------------
#
# Skills declare the callers that existed when they were written. Without this, an operator who
# assigns one to an agent outside that list gets a saved assignment that is denied the first
# time it runs — a feature that looks like it works and does not.
#
# Same argument `workflow_custom_steps` already makes for the workflow runner, and the grant has
# the same four properties: explicit, scoped, recorded, revocable. What widens is WHO may call.


def _invoke(client_engine, skill_id, caller, project_id=PROJECT):
    """Run the runtime's permission gate for real, as the agent loop would."""
    from apps.api.app.services.skill_runtime import SkillRuntimeService

    with client_engine.begin() as connection:
        return SkillRuntimeService(api_main.settings).invoke(
            connection, project_id, skill_id, caller, {}, mock=True,
        )


def test_an_agent_outside_the_declared_callers_is_denied_without_an_assignment(client, engine):
    # security_reviewer is a real agent and is NOT among github.merge_pr_real's allowed callers.
    result = _invoke(engine, "github.merge_pr_real", "security_reviewer")
    assert result["status"] == "denied"
    assert "not in allowed_callers" in result["reason"]


def test_assigning_the_skill_is_what_permits_it(client, engine):
    _assign(client, "security_reviewer", "github.merge_pr_real")
    result = _invoke(engine, "github.merge_pr_real", "security_reviewer")
    assert result["status"] != "denied", result.get("reason")


def test_unassigning_takes_the_permission_back(client, engine):
    _assign(client, "security_reviewer", "github.merge_pr_real")
    assert _invoke(engine, "github.merge_pr_real", "security_reviewer")["status"] != "denied"

    client.delete(
        "/v1/role-skills",
        params={"project_id": PROJECT, "role_key": "security_reviewer",
                "skill_id": "github.merge_pr_real", "actor": "ceo"},
    )
    # Read at the moment of the call, not cached: revoking must bite on the NEXT run, not on
    # the run after a process restart.
    after = _invoke(engine, "github.merge_pr_real", "security_reviewer")
    assert after["status"] == "denied"


def test_the_grant_does_not_widen_anything_except_who_may_call(client, engine):
    """A tier-3 merge still needs its approvals no matter which agent holds it."""
    _assign(client, "security_reviewer", "github.merge_pr_real")
    spec = client.get("/v1/skills", params={"project_id": PROJECT}).json()["items"]
    merge = next(s for s in spec if s["id"] == "github.merge_pr_real")
    # The SPEC is untouched by assignment — the risk tier and the approval requirement are
    # still exactly what the skill declares.
    assert merge["risk_tier"] == 3
    assert merge["policy"]["requires_approval"] == ["ceo", "cto"]
    assert "security_reviewer" not in (merge["permissions"]["allowed_callers"])


def test_an_agent_that_was_never_assigned_anything_is_still_denied(client, engine):
    """The gate must not open for every agent once the table has any row in it at all."""
    _assign(client, "security_reviewer", "github.merge_pr_real")
    result = _invoke(engine, "github.merge_pr_real", "design_critic")
    assert result["status"] == "denied"
