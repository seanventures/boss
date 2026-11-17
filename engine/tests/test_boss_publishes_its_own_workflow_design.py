"""BOSS publishes the workflow it designs (BOSSAPP-1356).

Ask BOSS designed api_onboarding v2 from a plain requirement and could not publish it in five
rounds: "step 1 has no capability", "needs 'cases' / 'url', which is not set", and a key refusal
when it tried a new key to get a new version. `draft_workflow` must say everything the console's
builder can draw: agent steps with instructions, literal inputs, {from_step, field} bindings that
rename a field, a deployment target on deploy / promote steps, and a published version of an
EXISTING key. Real Postgres (boss_test); no model.
"""
from __future__ import annotations

import os
import uuid

import pytest
from sqlalchemy import create_engine, text

from boss_engine.services.workflow_drafting import _spec_from_steps
from boss_engine.services.workflows import validate_workflow_spec

TEST_DB = os.environ.get(
    "BOSS_TEST_DATABASE_URL", "postgresql+psycopg://boss:boss@localhost:5432/boss_test")

STAGING, PROD = "deptgt_feace220c5af403f9c79", "deptgt_0dd5f2002efa4c6cae56"

CASES = [{"name": "health", "method": "GET", "path": "/health", "expect_status": 200}]


def v2_steps():
    """BOSS's design, in the shapes a model writes (a mix of them on purpose)."""
    return [
        {"id": "validate", "agent": True,
         "instructions": "Run `gwlint validate` on the tenant YAML and write a report of every "
                         "violation with its suggested fix."},
        {"id": "fix_pr", "agent_work": True,
         "instructions": "Open a PR applying `gwlint --fix`, re-validate until clean."},
        {"id": "staging", "capability": "config_deploy", "deployment_target": STAGING},
        {"id": "contract_test", "capability": "contract_test",
         "input": {"cases": CASES,
                   "base_url": {"from_step": "staging", "field": "deployed_url"}}},
        {"id": "release_approval", "capability": "release_approval"},
        {"id": "production", "capability": "promote_config",
         "inputs": {"target_id": PROD}},
        {"id": "smoke_check", "capability": "smoke_test",
         "input": {"url": {"from_step": "staging", "field": "deployed_url"}}},
    ]


def _by_id(spec):
    return {s["id"]: s for s in spec["steps"]}


def test_a_literal_default_and_a_renamed_binding_are_in_the_spec():
    spec = _spec_from_steps(v2_steps(), {"inputs": ["tenant_name", "tenant_yaml_path"]})
    assert spec.get("ok") is not False, spec
    steps = _by_id(spec)
    assert steps["contract_test"]["binding"]["inputs"]["cases"] == CASES
    assert steps["smoke_check"]["binding"]["inputs"]["url"] == {
        "from_step": "staging", "field": "deployed_url"}
    assert steps["staging"]["binding"]["inputs"]["target_id"] == STAGING
    assert steps["production"]["binding"]["inputs"]["target_id"] == PROD
    # promote_config's required deployment_id still comes from the staging step it follows.
    assert steps["production"]["binding"]["inputs"]["deployment_id"] == {
        "from_step": "staging", "field": "deployment_id"}
    validate_workflow_spec(spec)


def test_operator_inputs_listed_but_read_only_in_prose_still_make_the_start_form():
    spec = _spec_from_steps(v2_steps(), {"inputs": ["tenant_name", "tenant_yaml_path"]})
    schema = spec["input_schema"]
    assert {"tenant_name", "tenant_yaml_path"} <= set(schema["properties"])
    assert {"tenant_name", "tenant_yaml_path"} <= set(schema["required"])


def test_an_agent_step_with_instructions_round_trips_in_every_spelling():
    for step in ({"id": "a", "agent": True, "instructions": "do x"},
                 {"id": "a", "agent_work": True, "instructions": "do x"},
                 {"id": "a", "agent_work": {"instructions": "do x"}},
                 {"id": "a", "binding": {"agent_work": True, "inputs": {"instructions": "do x"}}},
                 {"id": "a", "kind": "agent", "input": {"instructions": "do x"}}):
        spec = _spec_from_steps([step], {})
        assert spec.get("ok") is not False, (step, spec)
        binding = spec["steps"][0]["binding"]
        assert binding["agent_work"] is True and binding["inputs"]["instructions"] == "do x", step
        validate_workflow_spec(spec)


def test_a_step_whose_id_is_its_capability_needs_no_second_word():
    spec = _spec_from_steps([{"id": "smoke_test", "input": {"url": "https://x.example/h"}}], {})
    assert spec["steps"][0]["binding"]["capability"] == "smoke_test"


def test_a_from_step_naming_a_later_step_is_refused_with_the_three_ways_to_set_it():
    out = _spec_from_steps(
        [{"id": "smoke", "capability": "smoke_test",
          "input": {"url": {"from_step": "deploy", "field": "deployed_url"}}},
         {"id": "deploy", "capability": "config_deploy"}], {})
    assert out["ok"] is False
    assert "step 'smoke' input 'url'" in out["error"] and "literal" in out["error"]


# -- against the database ------------------------------------------------------------------


@pytest.fixture(scope="module")
def eng():
    engine = create_engine(TEST_DB, future=True)
    try:
        with engine.begin() as c:
            if c.execute(text("SELECT workspace_id FROM projects LIMIT 1")).scalar() is None:
                pytest.skip("boss_test has no workspace")
    except Exception as error:                      # pragma: no cover
        pytest.skip(f"boss_test unavailable: {error}")
    return engine


@pytest.fixture()
def project(eng):
    pid = f"p_{uuid.uuid4().hex[:10]}"
    with eng.begin() as c:
        wid = c.execute(text("SELECT workspace_id FROM projects LIMIT 1")).scalar()
        c.execute(text("INSERT INTO projects (id, workspace_id, name, slug) "
                       "VALUES (:i, :w, 'drafting', :s)"), {"i": pid, "w": wid, "s": pid})
        # An agent that holds a skill: what makes an agent step ownable.
        c.execute(text("INSERT INTO roles (id, project_id, role_key, display_name) "
                       "VALUES (:i, :p, 'editor', 'editor')"),
                  {"i": f"role_{uuid.uuid4().hex[:8]}", "p": pid})
        c.execute(text("INSERT INTO role_skills (id, project_id, role_key, skill_id, source) "
                       "VALUES (:i, :p, 'editor', 'drive.list_videos', 'builtin')"),
                  {"i": f"rs_{uuid.uuid4().hex[:8]}", "p": pid})
    yield pid
    with eng.begin() as c:
        for table in ("workflow_custom_steps", "workflow_mappings", "workflow_versions",
                      "workflow_definitions", "events", "role_skills", "roles"):
            with c.begin_nested():
                c.execute(text(f"DELETE FROM {table} WHERE project_id=:p"), {"p": pid})
        c.execute(text("DELETE FROM projects WHERE id=:p"), {"p": pid})


class _Ctx:
    def __init__(self, engine):
        self._engine = engine

    def begin(self):
        return self._engine.begin()


def _draft(eng, project, **args):
    from boss_engine.tools.builtin import build_default_registry

    tool = {s.name: s for s in build_default_registry().specs()}["draft_workflow"]
    return tool.handler(_Ctx(eng), project_id=project, actor="ceo", args=args)


def _describe(eng, project, **args):
    from boss_engine.tools.builtin import build_default_registry

    tool = {s.name: s for s in build_default_registry().specs()}["describe_workflow"]
    return tool.handler(_Ctx(eng), project_id=project, actor="ceo", args=args)


INPUTS = ["tenant_name", "tenant_yaml_path"]


def test_boss_publishes_its_v2_design_as_the_next_version_of_the_same_key(eng, project):
    first = _draft(eng, project, workflow_key="api_onboarding", name="API onboarding",
                   steps=[{"id": "validate", "agent": True, "instructions": "lint it"}])
    assert first.get("status") == "published" and first["version"] == 1, first

    second = _draft(eng, project, workflow_key="api_onboarding", name="API onboarding",
                    steps=v2_steps(), inputs=INPUTS)
    assert second.get("status") == "published", second
    assert second["version"] == 2 and second["workflow_key"] == "api_onboarding"
    assert second["definition_id"] == first["definition_id"]


def test_asking_for_a_versioned_key_publishes_the_next_version_of_the_base(eng, project):
    _draft(eng, project, workflow_key="api_onboarding", name="API onboarding",
           steps=[{"id": "validate", "agent": True, "instructions": "lint it"}])
    out = _draft(eng, project, workflow_key="api_onboarding_v2", name="API onboarding v2",
                 steps=v2_steps(), inputs=INPUTS)
    assert out.get("status") == "published", out
    assert (out["workflow_key"], out["version"]) == ("api_onboarding", 2)


def test_a_key_spelled_loosely_is_tidied_not_refused(eng, project):
    out = _draft(eng, project, workflow_key="API-Onboarding Flow!", name="x",
                 steps=[{"id": "a", "agent": True, "instructions": "go"}])
    assert out.get("status") == "published", out
    assert out["workflow_key"] == "api_onboarding_flow"
    no_key = _draft(eng, project, name="Tenant intake",
                    steps=[{"id": "a", "agent": True, "instructions": "go"}])
    assert no_key["workflow_key"] == "tenant_intake"


def test_a_missing_input_says_the_step_the_input_and_how_to_set_it(eng, project):
    out = _draft(eng, project, workflow_key="needs_cases", name="x",
                 steps=[{"id": "contract_test", "capability": "contract_test"}])
    assert out["ok"] is False
    error = out["error"]
    assert "step 'contract_test'" in error and "'cases'" in error
    assert "from_step" in error and "literal" in error and "operator" in error


def test_describe_workflow_reports_exactly_the_stored_schema(eng, project):
    out = _draft(eng, project, workflow_key="api_onboarding", name="API onboarding",
                 steps=v2_steps(), inputs=INPUTS,
                 input_details={"tenant_name": {"title": "Tenant"},
                                "tenant_yaml_path": {"required": False}})
    assert out.get("status") == "published", out
    with eng.begin() as c:
        stored = c.execute(text(
            "SELECT v.spec->'input_schema' FROM workflow_versions v WHERE "
            "v.project_id=:p AND v.version=1"), {"p": project}).scalar()
    described = _describe(eng, project, workflow_key="api_onboarding", version=1)
    assert described["ok"] is True
    assert [i["name"] for i in described["inputs"]] == list(stored["properties"])
    assert described["you_must_supply"] == stored["required"] == ["tenant_name"]
    assert {i["name"]: i["required"] for i in described["inputs"]} == {
        "tenant_name": True, "tenant_yaml_path": False}
