"""boss.agent/v1 versions on a real database (AGENT_PLATFORM P1a acceptance).

(c) An agent created through the API from a spec with a skill USES that skill in a workflow
    agent step — the grant comes from the spec, nobody edits a row.
(d) Editing the prompt makes version N+1; rolling back to N restores the compiled prompt (and
    the live work prompt) byte for byte.
    The boot migration is idempotent across two boots, keeps every grant, and grants never
    exceed the Passport ceiling.
"""
from __future__ import annotations

import json
from uuid import uuid4

import yaml
from sqlalchemy import text

PROJECT = "boss-dogfood"
SKILL = "tickets.read"  # a read bundle, risk 0, not in the baseline


def _key() -> str:
    return f"spec_{uuid4().hex[:8]}"


def _doc(key: str, **spec) -> dict:
    return {"apiVersion": "boss.agent/v1", "kind": "Agent",
            "metadata": {"key": key, "name": "Spec Agent " + key[-4:]},
            "spec": {"mission": "Pack context for the next step.",
                     "prompts": {"system": "You pack context exactly."}, **spec}}


def _create(client, doc: dict):
    return client.post("/v1/agent-specs", json={"project_id": PROJECT, "actor": "ceo", "spec": doc})


def _skills(engine, key: str) -> set[str]:
    with engine.begin() as c:
        return set(c.execute(text("SELECT skill_id FROM role_skills WHERE project_id = :p AND role_key = :r"),
                             {"p": PROJECT, "r": key}).scalars())


class _UsesTheSkill:
    """A model that calls the one skill tool it was offered, then reports."""

    def __init__(self, needle: str) -> None:
        self.needle = needle
        self.calls = 0
        self.offered: list[str] = []

    def create_chat_completion(self, messages, tools=None, **kwargs):
        self.calls += 1
        names = [str((t.get("function") or {}).get("name") or "") for t in (tools or [])]
        if self.calls == 1:
            self.offered = names
            name = next((n for n in names if self.needle in n), None)
            assert name, f"no {self.needle} tool offered: {names}"
            body = json.dumps({"query": "release"})
            return ({"content": None, "tool_calls": [{"id": "c1", "name": name, "arguments": body}],
                     "raw": {"role": "assistant", "content": None, "tool_calls": [
                         {"id": "c1", "type": "function", "function": {"name": name, "arguments": body}}]}},
                    {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2})
        return ({"role": "assistant", "content": "Packed.", "raw": {"role": "assistant", "content": "Packed."},
                 "tool_calls": []}, {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2})


def test_c_an_agent_created_from_a_spec_uses_its_skill_in_an_agent_step(client, engine, monkeypatch):
    from boss_engine.config import Settings
    from boss_engine.services import agent_loop, agent_steps

    key = _key()
    made = _create(client, _doc(key, tools={"skills": [SKILL]}))
    assert made.status_code == 201, made.text
    assert made.json()["version"] == 1 and made.json()["active"] is True
    assert SKILL in _skills(engine, key), "publishing the spec IS the grant"

    model = _UsesTheSkill("ticket")
    monkeypatch.setattr(agent_loop, "project_chat_adapter", lambda *_a, **_k: model)
    # One test transaction holds the skill call's budget lock; the model's next turn reserves in
    # that same transaction (the governed adapter's own fallback for a connection it cannot
    # leave), instead of on a fresh connection that would wait behind it.
    from boss_engine.services import model_routing

    monkeypatch.setattr(model_routing.GovernedChatAdapter, "_open",
                        lambda self: self._reserve(self._connection))
    with engine.begin() as c:
        out = agent_steps.run(c, Settings(), PROJECT, instructions="Read the open tickets.", role_key=key,
                              actor="test")
    assert SKILL in out["skills_offered"]
    assert SKILL in out["skills_used"], {k: out.get(k) for k in ("skills_used", "summary", "status")}
    assert model.calls == 2, "the agent called the tool and then answered"


def test_d_a_prompt_edit_is_version_n_plus_1_and_rollback_restores_the_prompt_bytes(client, engine):
    from boss_engine.services import agent_steps

    key = _key()
    assert _create(client, _doc(key, tools={"skills": [SKILL]})).status_code == 201
    v1 = client.get(f"/v1/agents/{key}/spec", params={"project_id": PROJECT}).json()
    with engine.begin() as c:
        brief_v1 = agent_steps.agent_brief(c, PROJECT, key)
    assert "You pack context exactly." in v1["compiled_prompt"] and "You pack context exactly." in brief_v1

    edited = _doc(key, tools={"skills": [SKILL]})
    edited["spec"]["prompts"]["system"] = "You pack context, and you say what you dropped."
    saved = client.put(f"/v1/agents/{key}/spec", json={"project_id": PROJECT, "actor": "ceo", "spec": edited})
    assert saved.status_code == 200, saved.text
    assert saved.json()["version"] == 2 and saved.json()["active"]
    with engine.begin() as c:
        assert "say what you dropped" in agent_steps.agent_brief(c, PROJECT, key)

    diff = client.get(f"/v1/agents/{key}/spec/diff", params={"project_id": PROJECT, "from_version": 1,
                                                             "to_version": 2}).json()
    assert [ch["path"] for ch in diff["changes"]] == ["spec.prompts.system"]
    assert "say what you dropped" in diff["prompt_diff"]

    back = client.post(f"/v1/agents/{key}/spec/rollback",
                       json={"project_id": PROJECT, "actor": "ceo", "to_version": 1})
    assert back.status_code == 200, back.text
    now = client.get(f"/v1/agents/{key}/spec", params={"project_id": PROJECT}).json()
    assert now["version"] == 1
    assert now["compiled_prompt"].encode() == v1["compiled_prompt"].encode()
    with engine.begin() as c:
        brief_back = agent_steps.agent_brief(c, PROJECT, key)
    # The live prompt names the identity version, which only ever goes up (a rollback is a new
    # seal); everything the version says is byte-identical.
    import re

    unsealed = lambda s: re.sub(r"\(agt_[^)]*\)", "(agent)", s)  # noqa: E731
    assert unsealed(brief_back).encode() == unsealed(brief_v1).encode()
    versions = client.get(f"/v1/agents/{key}/spec/versions", params={"project_id": PROJECT}).json()["items"]
    assert [v["version"] for v in versions] == [2, 1]


def test_a_spec_dropping_a_skill_revokes_it_and_keeps_what_others_granted(client, engine):
    key = _key()
    assert _create(client, _doc(key, tools={"skills": [SKILL]})).status_code == 201
    with engine.begin() as c:  # granted elsewhere (the Skills page), not by the spec
        from boss_engine.services.skill_definitions import assign_skill_to_role

        assign_skill_to_role(c, project_id=PROJECT, actor="ceo", role_key=key, skill_id="docs.draft")
    saved = client.put(f"/v1/agents/{key}/spec", json={"project_id": PROJECT, "actor": "ceo",
                                                       "spec": _doc(key)})
    assert saved.status_code == 200, saved.text
    held = _skills(engine, key)
    assert SKILL not in held and "docs.draft" in held
    assert {"boss.fundamentals", "repo.read"} <= held, "the baseline is never revoked"


def test_grants_never_exceed_the_passport_ceiling(client, engine):
    key = _key()
    refused = _create(client, _doc(key, tools={"native": ["run_shell"]}))
    assert refused.status_code == 403, refused.text
    assert "can never be given to an agent" in refused.json()["detail"]["message"]
    with engine.begin() as c:
        assert c.execute(text("SELECT 1 FROM roles WHERE project_id = :p AND role_key = :r"),
                         {"p": PROJECT, "r": key}).first() is None, "a refused create leaves nothing"
    unknown = _create(client, _doc(_key(), tools={"native": ["teleport"]}))
    assert unknown.status_code == 403 and "no native tool" in unknown.json()["detail"]["message"]
    # A native tool inside the baseline ceiling is fine.
    ok = _create(client, _doc(_key(), tools={"native": ["read_repo_file"]}))
    assert ok.status_code == 201, ok.text
    assert ok.json()["report"]["required_permissions"] == ["repository:read"]


def test_an_unknown_field_is_refused_with_its_path(client):
    bad = _create(client, _doc(_key(), tools={"skils": [SKILL]}))
    assert bad.status_code == 422
    assert bad.json()["detail"]["errors"][0]["path"] == "spec.tools.skils"


def test_yaml_export_and_import_round_trip(client, engine):
    key = _key()
    assert _create(client, _doc(key, tools={"skills": [SKILL]})).status_code == 201
    exported = client.get(f"/v1/agents/{key}/spec/export", params={"project_id": PROJECT}).json()["yaml"]
    doc = yaml.safe_load(exported)
    assert doc["metadata"]["version"] == 1 and doc["spec"]["tools"]["skills"] == [SKILL]
    same = client.post("/v1/agent-specs/import", json={"project_id": PROJECT, "actor": "ceo", "yaml": exported})
    assert same.status_code == 201 and same.json()["duplicate"] is True and same.json()["version"] == 1
    doc["spec"]["mission"] = "Pack context, smaller."
    changed = client.post("/v1/agent-specs/import", json={"project_id": PROJECT, "actor": "ceo",
                                                          "yaml": yaml.safe_dump(doc)})
    assert changed.json()["version"] == 2 and changed.json()["created"] is False
    other = _key()
    doc["metadata"]["key"] = other
    fresh = client.post("/v1/agent-specs/import", json={"project_id": PROJECT, "actor": "ceo",
                                                        "yaml": yaml.safe_dump(doc)})
    assert fresh.status_code == 201 and fresh.json()["created"] is True
    assert SKILL in _skills(engine, other)


def test_a_legacy_edit_is_recorded_as_the_next_version(client, engine):
    key = _key()
    assert _create(client, _doc(key)).status_code == 201
    patched = client.patch(f"/v1/agents/{key}", json={"project_id": PROJECT, "actor": "ceo",
                                                      "mission": "A mission set the old way."})
    assert patched.status_code == 200, patched.text
    now = client.get(f"/v1/agents/{key}/spec", params={"project_id": PROJECT}).json()
    assert now["version"] == 2 and now["source"] == "legacy_edit"
    assert now["spec"]["spec"]["mission"] == "A mission set the old way."
    assert now["spec"]["spec"]["prompts"]["system"] == "You pack context exactly.", "v1-only fields kept"


def test_the_boot_migration_versions_every_agent_once_and_keeps_its_grants(engine):
    from boss_engine.agents import migration, versions

    with engine.begin() as c:
        keys = list(c.execute(text("SELECT role_key FROM roles WHERE project_id = :p"), {"p": PROJECT}).scalars())
        before = {k: versions.holdings(c, PROJECT, k) for k in keys}
    first = migration.sync_all(engine, project_ids=[PROJECT])[PROJECT]
    assert first["agents"]["failed"] == [], first["agents"]["failed"]
    second = migration.sync_all(engine, project_ids=[PROJECT])[PROJECT]
    assert second["agents"]["versioned"] == [] and second["profiles"]["versioned"] == []
    assert second["agents"]["unchanged"] == len(keys)
    with engine.begin() as c:
        after = {k: versions.holdings(c, PROJECT, k) for k in keys}
        for k in keys:
            current = versions.active(c, PROJECT, k)
            assert current is not None, k
            assert set(current["spec"]["spec"].get("tools", {}).get("skills", [])) >= set(before[k][0]) - {
                s for s in before[k][0] if "*" in s}, k
        shipped = c.execute(text("SELECT count(*) FROM agent_spec_versions WHERE project_id = :p "
                                 "AND kind = 'agent' AND source = 'shipped'"), {"p": PROJECT}).scalar()
    assert after == before, "versioning a row never changes what it holds"
    assert shipped >= 1


def test_studio_runs_go_through_the_governed_agent_pipeline(client, engine, monkeypatch):
    """deterministic_brief is retired: a Studio run is an agent step, not a template."""
    from boss_engine.config import get_settings
    from boss_engine.services import agent_loop
    from boss_engine.services.agent_studio import AgentStudioService

    studio = AgentStudioService(get_settings())
    model = _UsesTheSkill("no_such_tool_name_is_offered")
    model.create_chat_completion = lambda messages, tools=None, **kw: (  # type: ignore[method-assign]
        {"role": "assistant", "content": "A governed answer.",
         "raw": {"role": "assistant", "content": "A governed answer."}, "tool_calls": []},
        {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2})
    monkeypatch.setattr(agent_loop, "project_chat_adapter", lambda *_a, **_k: model)
    with engine.begin() as c:
        agent = studio._find_agent_by_key(c, PROJECT, "cfo")  # noqa: SLF001
        assert agent and agent["active_version_id"], "the shipped Studio catalogue is seeded"
        run = studio.start_agent_run(c, project_id=PROJECT, agent_version_id=agent["active_version_id"],
                                     input_data={"objective": "Summarise spend."}, actor="ceo",
                                     dedupe_key=None)
        assert run["status"] == "queued", run.get("error")
        result = studio.execute_run(c, project_id=PROJECT, run_id=run["id"], actor="worker")
    assert result["execution"] == "governed_agent_step"
    assert result["summary"] == "A governed answer."
    assert result["agent_step"]["agent"] == "finops_sentinel"
