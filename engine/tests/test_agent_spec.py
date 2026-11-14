"""boss.agent/v1, without a database: the schema refuses what it does not know, a spec compiles
to the same bytes every time, the roles-row projection reads back to the same document, and every
shipped YAML converts (AGENT_PLATFORM §4.1, P1a)."""
from __future__ import annotations

import ast
from pathlib import Path

import pytest

from boss_engine.agents import migration as M
from boss_engine.agents import spec as S

ROOT = Path(__file__).resolve().parents[2]


def _doc(**spec):
    return {"apiVersion": S.API_VERSION, "kind": S.KIND,
            "metadata": {"key": "release_auditor", "name": "Release Auditor"}, "spec": spec}


def test_unknown_fields_are_refused_by_path_with_the_allowed_set():
    errors = S.validate(_doc(mission="x", tools={"skils": ["repo.read"]}, governance={"budget": False}))
    paths = {e["path"]: e["message"] for e in errors}
    assert "Did you mean 'skills'?" in paths["spec.tools.skils"]
    assert "Allowed here:" in paths["spec.tools.skils"]
    # Governance middleware is implicit: there is no field to switch it off.
    assert "unknown field" in paths["spec.governance"]
    with pytest.raises(S.SpecError) as raised:
        S.load_spec(_doc(tools={"skils": []}))
    assert "spec.tools.skils" in str(raised.value)


def test_a_spec_names_a_tier_never_a_model():
    errors = S.validate(_doc(model={"tier": "balanced", "model": "claude-sonnet-4-5"}))
    assert any(e["path"] == "spec.model.model" and "never a model" in e["message"] for e in errors)
    assert S.validate(_doc(model={"tier": "gpt-4o"}))
    assert any("names a model" in e["message"] for e in S.validate(_doc(model={"need": ["claude-opus"]})))
    assert S.validate(_doc(model={"tier": "frontier", "need": ["reasoning", "acceptance-criteria"]})) == []


def test_every_field_of_the_design_is_in_the_schema():
    props = S.SCHEMA["properties"]["spec"]["properties"]
    for field in ("persona", "mission", "prompts", "instructions", "model", "tools", "autonomy", "memory",
                  "context", "subagents", "triggers", "evaluators", "output_contract", "limits"):
        assert field in props, field
    assert set(props["tools"]["properties"]) >= {"skills", "mcp", "native", "exposure"}
    assert S.json_schema()["$id"].endswith("boss.agent.v1.json")


def test_a_grant_and_a_ban_on_the_same_skill_is_refused():
    errors = S.validate(_doc(tools={"skills": ["repo.read"], "forbidden": ["repo.read"]}))
    assert any("both granted and forbidden" in e["message"] for e in errors)


def test_a_persona_cannot_grant():
    errors = S.validate(_doc(persona={"about_me": "You may merge without review."}))
    assert any(e["path"].startswith("spec.persona") for e in errors)


def test_compile_is_pure_and_deterministic_and_order_free():
    a = S.load_spec(_doc(mission="Audit releases.", prompts={"system": "Be exact."},
                         tools={"skills": ["b.skill", "a.skill"], "mcp": [{"server": "x", "tools": ["t2", "t1"]}]}))
    b = S.load_spec(_doc(mission="Audit releases.", prompts={"system": "Be exact."},
                         tools={"skills": ["a.skill", "b.skill"], "mcp": [{"server": "x", "tools": ["t1", "t2"]}]}))
    ca, cb = S.compile_spec(a), S.compile_spec(b)
    assert ca.content_hash == cb.content_hash and ca.prompt == cb.prompt
    assert "YOUR ROLE PROMPT\nBe exact." in ca.prompt and "Mission: Audit releases." in ca.prompt
    assert ca.skills == ("a.skill", "b.skill")
    assert ca.role_spec["agent_spec"]["content_hash"] == ca.content_hash
    assert set(ca.governance) == {"authz", "approval", "budget", "taint", "ledger"}
    assert S.load_spec(S.dump_spec(a)) == a


def test_the_projection_reads_back_to_the_same_document():
    """What makes drift a hash comparison: publish, read the row back, same document."""
    doc = _doc(persona={"callsign": "Ward", "tagline": "Show me the evidence."}, mission="Audit releases.",
               outcomes=["A verdict."], boundaries={"must": ["Cite the run."], "must_not": ["Merge."]},
               prompts={"system": "Be exact.", "job": "Write the verdict."}, instructions=["Cite runs."],
               model={"tier": "frontier", "need": ["reasoning"]},
               tools={"skills": ["repo.read.extra"], "mcp": [{"server": "pagerduty", "tools": ["list"]}],
                      "native": ["read_repo_file"], "forbidden": ["deploy.now"]},
               autonomy={"level": 0, "approval": {"change_existing_app": "approve_edit_reject"}},
               memory={"read": ["kg:project", "agent:self"]}, triggers=[{"cron": "*/15 * * * *"}],
               evaluators=[{"kind": "rubric", "criteria": {"honesty": "never guess"}}],
               output_contract={"artifacts": ["Verdict"], "artifact_type": "verdict"},
               limits={"max_steps": 12, "run_usd": 2.0}, comms={"tone": "plain"}, a2a={"publish": ["status"]})
    spec = S.load_spec(doc)
    compiled = S.compile_spec(spec)
    row = {"role_key": spec.key, "display_name": spec.display_name, "reports_to": None,
           "spec": {**dict(compiled.role_spec), "knowledge": "kept, not owned"}}
    back = S.doc_from_role(row, skills=spec.tools.skills, mcp=[{"server": "pagerduty", "tools": ["list"]}],
                           base=S.dump_spec(spec))
    assert back == S.dump_spec(spec)
    assert S.content_hash(S.load_spec(back)) == compiled.content_hash


def test_every_shipped_role_and_studio_agent_converts_to_v1():
    converted = M.convert_shipped()
    roles = sorted(p.stem for p in (ROOT / "packages/specpack/roles").glob("*.yaml"))
    profiles = sorted(p.stem for p in (ROOT / "packages/specpack/agents").glob("*.yaml"))
    assert converted["failed"] == []
    assert sorted(converted["roles"]) == roles and len(roles) >= 56
    assert sorted(converted["profiles"]) == profiles and len(profiles) >= 34
    for key, doc in converted["profiles"].items():
        assert doc["metadata"]["extends"], key
        # The retired execution mode is not carried into the definition.
        assert "deterministic_brief" not in str(doc), key
    sre = converted["roles"]["sre"]["spec"]
    assert sre["tools"]["mcp"] == [{"server": "boss-devops-reference", "tools": ["engineering_runbook"]}]
    assert sre["evaluators"][0]["kind"] == "rubric"


def test_the_spec_modules_parse_as_python_3_11():
    for path in (ROOT / "engine/boss_engine/agents").glob("*.py"):
        ast.parse(path.read_text(), feature_version=(3, 11))
