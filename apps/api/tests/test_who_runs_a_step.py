"""Who runs a workflow step, through the API the console and the examples use.

THE INCIDENTS. A step could only name an agent from a hard-coded set of 29 role keys, so an
agent made with POST /v1/roles — or the shipped `docs_writer` — was refused at save as "not a
role this workforce has", while the builder's picker offered five roles no project has. When
no agent was named, an agent step's model pick never ran (`adapter.complete()` does not exist),
and the run said nothing about who did the work. These drive the real endpoints: the palette,
save, start, the capability tick, and the runs listing the console reads.
"""
from __future__ import annotations

PROJECT = "boss-dogfood"


def _roles(client) -> list[str]:
    body = client.get("/v1/roles", params={"project_id": PROJECT}).json()
    return sorted(r["role_key"] for r in body["items"])


def _a_skill(client) -> str:
    items = client.get("/v1/skills", params={"project_id": PROJECT}).json()["items"]
    assert items, "no built-in skills registered; the fixture changed"
    return items[0]["id"]


def _new_agent(client, role_key: str, *skills: str) -> None:
    made = client.post("/v1/roles", json={"project_id": PROJECT, "role_key": role_key,
                                          "display_name": role_key.replace("_", " ").title()})
    assert made.status_code == 201, made.text
    for skill in skills:
        granted = client.post("/v1/role-skills", json={
            "project_id": PROJECT, "role_key": role_key, "skill_id": skill, "actor": "ceo"})
        assert granted.status_code == 201, granted.text


def _workflow(client, name: str) -> str:
    made = client.post("/v1/delivery-workflows/create",
                       json={"project_id": PROJECT, "name": name, "actor": "ceo"})
    assert made.status_code == 200, made.text
    return made.json()["definition"]["id"]


def _save(client, definition_id: str, steps: list[dict], **spec):
    return client.post("/v1/delivery-workflows/save", json={
        "project_id": PROJECT, "definition_id": definition_id, "actor": "ceo",
        "spec": {"entry_step": steps[0]["id"], "steps": steps, **spec}})


def _agent_step(**extra) -> dict:
    return {"id": "work", "kind": "capability", "next": [], "approval": {"mode": "auto"},
            "binding": {"agent_work": True,
                        "inputs": {"instructions": {"from_input": "instructions"}},
                        **extra.pop("binding", {})},
            **extra}


def test_the_builder_is_offered_this_projects_agents_and_a_person(client):
    payload = client.get("/v1/delivery-workflows/capabilities",
                         params={"project_id": PROJECT}).json()
    assert payload["roles"][-1] == "human"
    assert sorted(payload["roles"][:-1]) == [r for r in _roles(client) if r != "human"]
    # Each with what it holds, so the picker can say "3 skills" and the scope can be offered.
    assert {"role_key", "name", "holds"} <= set(payload["agents"][0])
    # The constant's phantom roles are not offered unless this project has them.
    for phantom in ("engineering_captain", "chief_architect", "vp_product"):
        assert phantom in payload["roles"] or phantom not in _roles(client)


def test_an_agent_made_through_the_api_can_be_put_on_a_step(client):
    skill = _a_skill(client)
    _new_agent(client, "brand_new_agent", skill)
    payload = client.get("/v1/delivery-workflows/capabilities",
                         params={"project_id": PROJECT}).json()
    assert "brand_new_agent" in payload["roles"]

    definition_id = _workflow(client, "Named agent")
    saved = _save(client, definition_id, [_agent_step(captain="brand_new_agent")])
    assert saved.status_code == 200, saved.text


def test_an_agent_the_project_does_not_have_is_refused_with_the_ones_it_has(client):
    definition_id = _workflow(client, "Ghost agent")
    saved = _save(client, definition_id, [_agent_step(captain="nobody_by_this_name")])
    assert saved.status_code == 422
    detail = saved.json()["detail"]
    assert "which this project does not have" in detail
    assert any(role in detail for role in _roles(client))


def test_a_person_cannot_be_given_a_step_that_is_not_a_gate(client):
    definition_id = _workflow(client, "Human worker")
    saved = _save(client, definition_id, [_agent_step(captain="human")])
    assert saved.status_code == 422
    assert "only an approval gate waits for a person" in saved.json()["detail"]


def test_a_step_cannot_scope_an_agent_to_a_skill_it_does_not_hold(client):
    skills = [s["id"] for s in client.get(
        "/v1/skills", params={"project_id": PROJECT}).json()["items"]][:2]
    _new_agent(client, "narrow_agent", skills[0])
    definition_id = _workflow(client, "Over-scoped")
    saved = _save(client, definition_id,
                  [_agent_step(captain="narrow_agent", binding={"skills": [skills[1]]})])
    assert saved.status_code == 422
    assert f"which narrow_agent does not hold" in saved.json()["detail"]


def test_a_step_left_to_boss_runs_as_the_agent_holding_its_skills_and_the_run_says_so(
        client, monkeypatch):
    """R4 end to end: no agent named, the step scoped to one skill, exactly one agent holds it.
    Started, ticked, and read back the way the console reads a run."""
    from boss_engine.services import agent_loop

    class _Model:
        def create_chat_completion(self, messages, tools=None, **_):
            return ({"content": "done", "tool_calls": None,
                     "raw": {"role": "assistant", "content": "done"}},
                    {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0})

    monkeypatch.setattr(agent_loop, "make_chat_adapter", lambda _s: _Model())

    skills = [s["id"] for s in client.get(
        "/v1/skills", params={"project_id": PROJECT}).json()["items"]]
    # A skill nobody on the seeded project holds, so exactly one agent will.
    held = {a["skill_id"] for a in client.get(
        "/v1/role-skills", params={"project_id": PROJECT}).json()["assignments"]}
    skill = next(s for s in skills if s not in held)
    _new_agent(client, "only_holder_agent", skill)

    definition_id = _workflow(client, "Left to BOSS")
    saved = _save(client, definition_id, [_agent_step(binding={"skills": [skill]})],
                  input_schema={"required": ["instructions"],
                                "properties": {"instructions": {"title": "What to do"}}})
    assert saved.status_code == 200, saved.text
    listed = client.get("/v1/delivery-workflows", params={"project_id": PROJECT}).json()
    workflow_key = next(w["workflow_key"] for w in listed["items"] if w["id"] == definition_id)

    started = client.post("/v1/delivery-workflows/start", json={
        "project_id": PROJECT, "actor": "ceo", "workflow_key": workflow_key,
        "input": {"instructions": "list what is there"}})
    assert started.status_code == 200, started.text
    ticked = client.post("/v1/delivery-workflows/run-capabilities",
                         json={"project_id": PROJECT})
    assert ticked.status_code == 200, ticked.text

    runs = client.get("/v1/delivery-workflows/runs", params={"project_id": PROJECT}).json()
    step = next(s for r in runs["runs"] for s in r["steps"]
                if s["step_id"] == "work" and (s["output"] or {}).get("ran_as"))
    assert step["status"] == "succeeded", step.get("error")
    ran_as = step["output"]["ran_as"]
    assert (ran_as["agent"], ran_as["how"]) == ("only_holder_agent", "only_holder")
    assert skill in ran_as["why"]
    assert ran_as["skills_offered"] == [skill]


# ── Versions published before the step-agent rules ───────────────────────────────────────────


def _unheld_skill_nobody_may_call(client, engine) -> str:
    """A catalogue skill no agent on the project holds and whose own policy names no project
    agent, `*` or the workflow — the exact shape the builder's old default produced."""
    from sqlalchemy import text

    from boss_engine.services import step_agents

    held = {a["skill_id"] for a in client.get(
        "/v1/role-skills", params={"project_id": PROJECT}).json()["assignments"]}
    roles = set(_roles(client))
    with engine.begin() as c:
        for item in client.get("/v1/skills", params={"project_id": PROJECT}).json()["items"]:
            skill = item["id"]
            if skill in held:
                continue
            callers = step_agents._allowed_callers(c, PROJECT, skill)
            if callers and not (callers & (roles | {"*", "workflow_runner"})):
                # Registered at tier 0, as the engine tests do, so the step binds at a known tier.
                c.execute(text(
                    "INSERT INTO project_skills (id, project_id, skill_id, enabled, config, "
                    "provenance) VALUES (:i, :p, :s, true, CAST(:c AS JSONB), "
                    "'{\"trusted\": true}'::jsonb) ON CONFLICT DO NOTHING"),
                    {"i": f"ps_{skill.replace('.', '_')[:20]}", "p": PROJECT, "s": skill,
                     "c": '{"risk_tier": 0}'})
                return skill
    raise AssertionError("no catalogue skill fits; the fixture changed")


def _old_version(client, engine, name: str, skill: str) -> tuple[str, str]:
    """A custom step naming nobody, published the way main published it: no agent-rules stamp.
    Written straight to the table, because publishing it today is (rightly) refused."""
    import json
    import uuid

    from sqlalchemy import text

    made = client.post("/v1/delivery-workflows/custom-steps", json={
        "project_id": PROJECT, "actor": "ceo", "label": f"{name} step",
        "binding_kind": "skill", "skill_id": skill, "step_key": f"{name}_step"})
    assert made.status_code == 200, made.text
    definition_id = _workflow(client, name)
    spec = {"entry_step": "do", "input_schema": {}, "output_schema": {}, "steps": [
        {"id": "do", "kind": "capability", "next": [], "max_attempts": 1,
         "approval": {"mode": "manual"},
         "binding": {"custom_step": f"{name}_step", "inputs": {}}}]}
    version_id = f"wfv_{uuid.uuid4().hex[:12]}"
    with engine.begin() as c:
        c.execute(text(
            "INSERT INTO workflow_versions (id, project_id, workflow_definition_id, version, "
            "status, spec, content_hash, created_by) VALUES (:i, :p, :d, 1, 'published', "
            "CAST(:s AS JSONB), :h, 'ceo')"),
            {"i": version_id, "p": PROJECT, "d": definition_id, "s": json.dumps(spec),
             "h": uuid.uuid4().hex})
        c.execute(text("UPDATE workflow_definitions SET active_version_id=:v, status='active' "
                       "WHERE id=:d"), {"v": version_id, "d": definition_id})
    return definition_id, version_id


def _tick_through_the_gate(client, engine, run_id: str, step_id: str) -> dict:
    from sqlalchemy import text

    client.post("/v1/delivery-workflows/run-capabilities", json={"project_id": PROJECT})
    with engine.begin() as c:
        decision = c.execute(text(
            "SELECT decision_id FROM workflow_step_runs WHERE workflow_run_id=:r AND step_id=:s"),
            {"r": run_id, "s": step_id}).scalar()
        if decision:
            c.execute(text("UPDATE decisions SET status='approved' WHERE id=:d"), {"d": decision})
    for _ in range(2):
        ticked = client.post("/v1/delivery-workflows/run-capabilities",
                             json={"project_id": PROJECT})
        assert ticked.status_code == 200, ticked.text
    with engine.begin() as c:
        row = c.execute(text(
            "SELECT status, output, last_error FROM workflow_step_runs "
            "WHERE workflow_run_id=:r AND step_id=:s"), {"r": run_id, "s": step_id}).mappings().first()
    return dict(row)


def test_a_workflow_published_before_the_agent_rules_still_starts_and_runs(
        client, engine, monkeypatch):
    """THE INCIDENT the review caught: on main a custom step that named nobody ran as the
    workflow, with the skill's callers widened for it — the builder's own default. The new rule
    refused it at publish, at start AND mid-run, so upgrading killed workflows that worked. A
    version without the agent-rules stamp keeps its old behaviour end to end, and the run says
    exactly what to do to move it to an agent."""
    from boss_engine.services import agent_loop

    seen: dict = {}

    def fake_invoke(connection, settings, project_id, skill_id, caller, args, *, actor,
                    skill_spec=None):
        seen.update(caller=caller, actor=actor, spec=skill_spec)
        return {"status": "succeeded", "result": {"ok": True}}

    monkeypatch.setattr(agent_loop, "_invoke_agent_skill", fake_invoke)
    skill = _unheld_skill_nobody_may_call(client, engine)
    definition_id, _ = _old_version(client, engine, "legacy_flow", skill)
    listed = client.get("/v1/delivery-workflows", params={"project_id": PROJECT}).json()
    workflow_key = next(w["workflow_key"] for w in listed["items"] if w["id"] == definition_id)

    started = client.post("/v1/delivery-workflows/start", json={
        "project_id": PROJECT, "actor": "ceo", "workflow_key": workflow_key, "input": {}})
    assert started.status_code == 200, started.text
    run_id = started.json().get("run", started.json()).get("id")
    row = _tick_through_the_gate(client, engine, run_id, "do")

    assert row["status"] == "succeeded", row["last_error"]
    ran_as = row["output"]["ran_as"]
    assert (ran_as["agent"], ran_as["how"]) == ("workflow_runner", "legacy_workflow_runner")
    assert f"Give {skill} to an agent" in ran_as["why"]
    # It ran exactly as it did on main: as the workflow, on the grant the version was made with.
    assert seen["caller"] == "workflow_runner"
    assert seen["spec"] is not None
    assert "workflow_runner" in seen["spec"]["permissions"]["allowed_callers"]


def test_the_same_workflow_published_again_is_refused_with_what_to_do(client, engine):
    """Publishing is where the new rule is met — the operator is in the builder and can act."""
    skill = _unheld_skill_nobody_may_call(client, engine)
    definition_id, _ = _old_version(client, engine, "legacy_again", skill)
    saved = _save(client, definition_id, [
        {"id": "do", "kind": "capability", "next": [], "approval": {"mode": "manual"},
         "binding": {"custom_step": "legacy_again_step", "inputs": {}}}])
    assert saved.status_code == 422
    assert f"Give {skill} to an agent on the Agents page" in saved.json()["detail"]


def test_publish_and_run_agree_on_what_an_agent_holds(client, engine):
    """THE MISMATCH: publish read raw role_skills, the runtime dropped every grant not in the
    skill catalogue. An agent step scoped to an AUTHORED skill published cleanly and then failed
    at run time with "<agent> does not hold X" — false; it held it. Both now read one function,
    so the step is refused at publish with the sentence the runtime would give."""
    from boss_engine.config import Settings
    from boss_engine.services import agent_steps

    authored = client.post("/v1/skill-definitions", json={
        "project_id": PROJECT, "actor": "ceo", "skill_key": "house_style_check",
        "label": "House style check", "behaviour_kind": "builtin",
        "config": {"executor_id": "house_style"}})
    assert authored.status_code == 201, authored.text
    _new_agent(client, "style_agent", "house_style_check")

    definition_id = _workflow(client, "Authored scope")
    saved = _save(client, definition_id, [
        _agent_step(captain="style_agent", binding={"skills": ["house_style_check"]})])
    assert saved.status_code == 422
    detail = saved.json()["detail"]
    assert "style_agent holds house_style_check" in detail
    assert "does not hold" not in detail

    with engine.begin() as c:
        try:
            agent_steps.run(c, Settings(), PROJECT, instructions="check the style",
                            role_key="style_agent", skills=["house_style_check"])
        except agent_steps.AgentStepError as error:
            at_run = str(error)
        else:  # pragma: no cover
            raise AssertionError("the runtime ran a step publish refused")
    assert at_run in detail, "publish and run gave different reasons"
