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


def test_a_versioned_key_of_an_existing_workflow_is_refused_naming_the_real_key(eng, project):
    _draft(eng, project, workflow_key="api_onboarding", name="API onboarding",
           steps=[{"id": "validate", "agent": True, "instructions": "lint it"}])
    out = _draft(eng, project, workflow_key="api_onboarding_v2", name="API onboarding v2",
                 steps=v2_steps(), inputs=INPUTS)
    assert out["ok"] is False and "'api_onboarding'" in out["error"]
    assert "workflow_key='api_onboarding'" in out["error"]


def test_version_of_names_an_existing_workflow_or_is_refused(eng, project):
    _draft(eng, project, workflow_key="api_onboarding", name="API onboarding",
           steps=[{"id": "validate", "agent": True, "instructions": "lint it"}])
    ok = _draft(eng, project, version_of="api_onboarding", name="API onboarding",
                steps=v2_steps(), inputs=INPUTS)
    assert (ok.get("status"), ok["workflow_key"], ok["version"]) == (
        "published", "api_onboarding", 2), ok
    gone = _draft(eng, project, version_of="no_such_one", name="x", steps=v2_steps())
    assert gone["ok"] is False and "not a workflow in this project" in gone["error"]
    clash = _draft(eng, project, version_of="api_onboarding", workflow_key="other_one",
                   name="x", steps=v2_steps())
    assert clash["ok"] is False and "different workflows" in clash["error"]


def test_a_badly_spelled_key_is_refused_not_renamed(eng, project):
    out = _draft(eng, project, workflow_key="API-Onboarding Flow!", name="x",
                 steps=[{"id": "a", "agent": True, "instructions": "go"}])
    assert out["ok"] is False and "lower_snake_case" in out["error"]
    with eng.begin() as c:
        assert c.execute(text("SELECT count(*) FROM workflow_definitions WHERE project_id=:p"),
                         {"p": project}).scalar() == 0


def test_a_name_derived_key_that_matches_an_existing_workflow_is_refused(eng, project):
    steps = [{"id": "a", "agent": True, "instructions": "go"}]
    made = _draft(eng, project, name="Tenant intake", steps=steps)
    assert made["workflow_key"] == "tenant_intake"
    again = _draft(eng, project, name="Tenant intake", steps=steps)
    assert again["ok"] is False and "'tenant_intake'" in again["error"]


def test_a_button_for_a_draft_must_name_its_key():
    import types

    from boss_engine.tools.builtin import build_default_registry

    handler = build_default_registry().get("propose_action").handler
    out = handler(types.SimpleNamespace(settings=None), project_id="p", actor="u",
                  args={"tool": "draft_workflow", "label": "x",
                        "args": {"name": "x", "steps": [{"id": "a", "agent": True}]}})
    assert out["ok"] is False and "workflow_key" in out["error"]


def test_instructions_alone_do_not_make_an_agent_step_and_unknown_fields_are_refused():
    out = _spec_from_steps([{"id": "validate", "instructions": "lint it"}], {})
    assert out["ok"] is False and "validate" in out["error"]
    out = _spec_from_steps([{"id": "t", "capability": "contract_test", "cases": CASES}], {})
    assert out["ok"] is False and "cases" in out["error"] and "`input`" in out["error"]
    out = _spec_from_steps([{"id": "a", "capability": "no_such_capability"}], {})
    assert out.get("ok") is not False  # unknown capability words are refused at validation,
    # with the full list (see the next test)


def test_an_unknown_capability_is_refused_with_every_valid_one(eng, project):
    out = _draft(eng, project, workflow_key="bad_cap", name="x",
                 steps=[{"id": "a", "capability": "no_such_capability"}])
    assert out["ok"] is False and "Valid capabilities:" in out["error"]
    assert "config_deploy" in out["error"] and "smoke_test" in out["error"]


def test_the_id_guess_applies_only_to_an_exact_capability_name():
    assert _spec_from_steps([{"id": "smoke_test", "input": {"url": "https://x.example"}}],
                            {})["steps"][0]["binding"]["capability"] == "smoke_test"
    assert _spec_from_steps([{"id": "Smoke_Test"}], {})["ok"] is False


def test_only_from_step_and_from_input_are_bindings():
    spec = _spec_from_steps(
        [{"id": "a", "agent": True, "instructions": "go"},
         {"id": "n", "capability": "notify",
          "input": {"message": {"from": "a.x", "note": 1}, "channel": {"step": "a"}}}], {})
    assert spec["steps"][1]["binding"]["inputs"]["message"] == {"from": "a.x", "note": 1}
    assert spec["steps"][1]["binding"]["inputs"]["channel"] == {"step": "a"}


def test_conflicting_or_duplicate_settings_are_refused_not_overwritten():
    out = _spec_from_steps([{"id": "d", "capability": "config_deploy",
                             "deployment_target": STAGING, "input": {"target_id": PROD}}], {})
    assert out["ok"] is False and "target" in out["error"]
    out = _spec_from_steps([{"id": "s", "capability": "smoke_test",
                             "input": {"url": "https://a.example"},
                             "inputs": {"url": "https://b.example"}}], {})
    assert out["ok"] is False and "'url' twice" in out["error"]
    same = _spec_from_steps([{"id": "d", "capability": "config_deploy",
                              "deployment_target": STAGING, "input": {"target_id": STAGING}}], {})
    assert same.get("ok") is not False


def test_duplicate_explicit_step_ids_are_refused_and_from_step_ignores_case():
    out = _spec_from_steps([{"id": "a", "agent": True}, {"id": "A", "agent": True}], {})
    assert out["ok"] is False and "unique" in out["error"]
    spec = _spec_from_steps(
        [{"id": "staging", "capability": "config_deploy"},
         {"id": "smoke", "capability": "smoke_test",
          "input": {"url": {"from_step": "Staging", "field": "deployed_url"}}}], {})
    assert spec["steps"][1]["binding"]["inputs"]["url"]["from_step"] == "staging"


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


# -- the console's one-shot Describe path ----------------------------------------------------


def _describe_with(eng, project, monkeypatch, replies):
    import json

    from boss_engine.services import workflow_drafting as wd

    prompts: list[str] = []
    queue = list(replies)
    monkeypatch.setattr(wd, "model_connected", lambda *a, **k: True)

    def ask(_c, _s, _p, prompt):
        prompts.append(prompt)
        return json.dumps(queue.pop(0))

    monkeypatch.setattr(wd, "_ask_model", ask)
    return wd.draft_from_description(eng, None, project, "lint then notify"), prompts


def test_describe_drops_commentary_keys_silently(eng, project, monkeypatch):
    out, prompts = _describe_with(eng, project, monkeypatch, [{
        "name": "Lint", "steps": [{"id": "lint", "agent": True, "instructions": "lint it",
                                   "why": "because", "rationale": "x", "note": "y"}]}])
    assert out["spec"]["steps"][0]["binding"]["agent_work"] is True and len(prompts) == 1


def test_describe_retries_once_with_the_error_then_succeeds(eng, project, monkeypatch):
    out, prompts = _describe_with(eng, project, monkeypatch, [
        {"name": "Lint", "steps": [{"id": "lint", "instructions": "lint it"}]},
        {"name": "Lint", "steps": [{"id": "lint", "agent": True, "instructions": "lint it"}]}])
    assert len(prompts) == 2 and "was refused" in prompts[1] and "lint" in prompts[1]
    assert out["spec"]["steps"][0]["binding"]["agent_work"] is True


def test_describe_surfaces_the_error_after_the_one_retry(eng, project, monkeypatch):
    from boss_engine.services.workflow_drafting import DraftError

    bad = {"name": "Lint", "steps": [{"id": "lint", "instructions": "lint it"}]}
    with pytest.raises(DraftError):
        _describe_with(eng, project, monkeypatch, [bad, bad])


def test_conditional_step_inputs_checked_on_publish_and_draft(eng, project):
    """BOSSAPP-1413: config_deploy with code_task_run_id requires source_paths (or source_file),
    fix_pr pairs path <-> content, and build pairs start_from_content -> start_from / target_path."""
    from boss_engine.services import workflows

    # 1) config_deploy v8 (code_task_run_id without source_paths) refused on BOTH paths naming source_paths.
    bad_v8_steps = [
        {"id": "build", "capability": "build",
         "input": {"title": "Make config", "instructions": "make config"}},
        {"id": "deploy", "capability": "config_deploy",
         "deployment_target": STAGING,
         "input": {"code_task_run_id": {"from_step": "build", "field": "run_id"}}},
    ]
    draft_v8 = _draft(eng, project, workflow_key="cfg_cond", name="Cfg Cond", steps=bad_v8_steps)
    assert draft_v8["ok"] is False and "source_paths" in draft_v8["error"], draft_v8

    with eng.begin() as c:
        wf = workflows.create_definition(
            c, project_id=project, workflow_key="cfg_cond_pub",
            name="Cfg Cond Pub", description=None, actor="test", namespace="product",
        )
        with pytest.raises(workflows.WorkflowValidationError, match="source_paths"):
            workflows.publish_version(
                c, project_id=project, definition_id=wf["id"], actor="test",
                spec={
                    "entry_step": "deploy",
                    "steps": [{
                        "id": "deploy",
                        "kind": "capability",
                        "binding": {
                            "capability": "config_deploy",
                            "inputs": {
                                "target_id": STAGING,
                                "code_task_run_id": "crun_123",
                            },
                        },
                        "next": [],
                    }],
                },
            )

    # 2) config_deploy v9 (with source_paths) succeeds on BOTH draft_workflow and publish_version.
    good_v9_steps = [
        {"id": "build", "capability": "build",
         "input": {"title": "Make config", "instructions": "make config"}},
        {"id": "deploy", "capability": "config_deploy",
         "deployment_target": STAGING,
         "input": {"code_task_run_id": {"from_step": "build", "field": "run_id"},
                   "source_paths": ["tenants/lumen.yaml"]}},
    ]
    draft_v9 = _draft(eng, project, workflow_key="cfg_cond", name="Cfg Cond", steps=good_v9_steps)
    assert draft_v9.get("status") == "published", draft_v9

    with eng.begin() as c:
        wf2 = workflows.create_definition(
            c, project_id=project, workflow_key="cfg_cond_pub_ok",
            name="Cfg Cond Pub Ok", description=None, actor="test", namespace="product",
        )
        v9 = workflows.publish_version(
            c, project_id=project, definition_id=wf2["id"], actor="test",
            spec={
                "entry_step": "deploy",
                "steps": [{
                    "id": "deploy",
                    "kind": "capability",
                    "binding": {
                        "capability": "config_deploy",
                        "inputs": {
                            "target_id": STAGING,
                            "code_task_run_id": "crun_123",
                            "source_paths": ["tenants/lumen.yaml"],
                        },
                    },
                    "next": [],
                }],
            },
        )
        assert v9["version"] == 1

    # 3) fix_pr conditional pairing (path <-> content) & build (start_from_content -> start_from/target_path).
    bad_fix = _draft(eng, project, workflow_key="fix_cond", name="Fix Cond", steps=[
        {"id": "fix", "capability": "fix_pr",
         "input": {"pull_request": "crun_1", "title": "Fix", "path": "a.py"}},
    ])
    assert bad_fix["ok"] is False and "content" in bad_fix["error"]

    bad_build = _draft(eng, project, workflow_key="build_cond", name="Build Cond", steps=[
        {"id": "b", "capability": "build",
         "input": {"title": "Make app", "instructions": "Make app", "start_from_content": "print(1)"}},
    ])
    assert bad_build["ok"] is False and "start_from" in bad_build["error"]

    with eng.begin() as c:
        wf3 = workflows.create_definition(
            c, project_id=project, workflow_key="fix_cond_pub",
            name="Fix Cond Pub", description=None, actor="test", namespace="product",
        )
        with pytest.raises(workflows.WorkflowValidationError, match="path"):
            workflows.publish_version(
                c, project_id=project, definition_id=wf3["id"], actor="test",
                spec={
                    "entry_step": "fix",
                    "steps": [{
                        "id": "fix",
                        "kind": "capability",
                        "binding": {
                            "capability": "fix_pr",
                            "inputs": {"pull_request": "crun_1", "title": "Fix", "content": "x = 1"},
                        },
                        "next": [],
                    }],
                },
            )
