"""The Workflow Library (design PR #287, BOSSAPP-1296): a template installs into a project as an
editable copy, the copy is changed and run, and sharing runs the publish-time checks.

    "build and customise reusable AI workflows" — the preview team's first priority.

"API gateway onboarding" is "Onboard a config file" shipped as a template (PackTemplate): the
standard stays seeded everywhere as before; the template installs under its own key as the
project's own copy, follows the standard's steps while unmodified, and keeps a team's changes.

The run goes through the real runner `tick` on the real database. The build's code task is played
by a scripted stand-in (no model, no repository): it is started by the build step, then finishes
the way the pipeline records a scripted-engine build — and the run walks on to review and waits at
the merge. Staging → contract tests → production are the standard's own steps, driven end to end
by the config onboarding chain test.
"""
from __future__ import annotations

import copy
import uuid

import pytest
from sqlalchemy import text

from boss_engine.services import workflow_capability_runner as runner
from boss_engine.services import workflow_catalogue as catalogue
from boss_engine.services import workflow_marketplace as market
from boss_engine.services import workflows

from .test_workflow_capability_runner import Ctx, RecordingRegistry, RecordingTool, _editor
from .test_workflow_marketplace import _cleanup, _new_project, eng  # noqa: F401

PACK = "api-gateway-onboarding"
KEY = "api_gateway_onboarding"
SOURCE = "onboard_config_file"
TARGET = "config/tenants/lumen-pay.yaml"


@pytest.fixture()
def project(eng):  # noqa: F811
    with eng.begin() as c:
        pid = _new_project(c, name="library test")
        c.exec_driver_sql("ALTER TABLE workflow_packs ADD COLUMN IF NOT EXISTS tags JSONB "
                          "NOT NULL DEFAULT '[]'::jsonb")
    yield pid
    with eng.begin() as c:
        for table in ("code_task_runs", "decisions", "deadletters", "quality_gates",
                      "role_skills", "roles"):
            try:
                with c.begin_nested():
                    c.execute(text(f"DELETE FROM {table} WHERE project_id=:p"), {"p": pid})
            except Exception:
                pass
    _cleanup(eng, [pid])


def _row(c, project, key=KEY):
    row = c.execute(text("SELECT * FROM workflow_definitions WHERE project_id=:p "
                         "AND workflow_key=:k"), {"p": project, "k": key}).mappings().first()
    return dict(row) if row else None


def _active_spec(c, row):
    return c.execute(text("SELECT spec FROM workflow_versions WHERE id=:v"),
                     {"v": row["active_version_id"]}).scalar()


def _source():
    return next(e for e in catalogue.CATALOGUE if e["key"] == SOURCE)


# ── the template in the Library ─────────────────────────────────────────────────────────────

def test_the_template_is_listed_with_its_tags_steps_and_inputs(eng, project):  # noqa: F811
    with eng.begin() as c:
        catalogue.seed(c, project)
        pack = {p["id"]: p for p in market.list_packs(c, project)}[PACK]
    assert pack["source"] == "boss" and pack["publisher"] == "BOSS"
    assert pack["tags"] == ["gateway", "onboarding", "config"]
    assert pack["installed"] is False and pack["installed_in"] == []
    (wf,) = pack["workflows"]
    assert (wf["key"], wf["name"]) == (KEY, "API gateway onboarding")
    assert wf["steps"][:3] == ["Fix the file", "Adversarial review", "Merge"]
    assert wf["step_count"] >= 5
    # The standard's start form, required fields first.
    assert {"Start from a file", "Where the fixed file goes"} <= set(wf["inputs"][:4])
    assert wf["objective"].startswith("Take the file the run was given")


def test_the_standard_stays_seeded_and_the_template_is_not(eng, project):  # noqa: F811
    with eng.begin() as c:
        catalogue.seed(c, project)
        assert _row(c, project, SOURCE)["is_catalogue"] is True
        assert _row(c, project) is None
    assert SOURCE not in market.PACK_OF_KEY and KEY not in market.PACK_OF_KEY
    assert market.pack_of_key(KEY) == PACK and market.pack_of_key(SOURCE) is None


def test_the_template_is_the_standards_steps_under_its_own_name():
    (entry,) = market._entries(market.SHIPPED_PACKS[PACK])
    spec, standard = catalogue.entry_spec(entry), catalogue.entry_spec(_source())
    assert spec["workflow_name"] == "API gateway onboarding"
    assert spec["steps"] == standard["steps"]
    assert spec["input_schema"] == standard["input_schema"]
    assert spec.get("checks_must_pass") is True
    # The standard's own entry is not touched by deriving the template.
    assert _source()["key"] == SOURCE and _source()["name"] == "Onboard a config file"


# ── Add to project, and Edit a copy ─────────────────────────────────────────────────────────

def test_add_to_project_installs_an_editable_copy_beside_the_standard(eng, project):  # noqa: F811
    with eng.begin() as c:
        catalogue.seed(c, project)
        standard_before = _row(c, project, SOURCE)
        result = market.install(c, project, PACK, "ceo")
        assert [i["key"] for i in result["installed"]] == [KEY]
        row = _row(c, project)
        assert row["is_catalogue"] is False and row["marketplace_pack_id"] == PACK
        assert row["active_version_id"] and not row["marketplace_waiting"]
        assert _row(c, project, SOURCE)["active_version_id"] == standard_before["active_version_id"]
        listed = {p["id"]: p for p in market.list_packs(c, project)}[PACK]
        assert listed["installed"] is True and project in listed["installed_in"]
        state = market.install_state(c, row)
        assert state["source"] == "boss" and state["modified"] is False


def test_an_edited_copy_keeps_its_change_and_is_offered_the_standards_next_steps(
        eng, project, monkeypatch):  # noqa: F811
    with eng.begin() as c:
        catalogue.seed(c, project)
        market.install(c, project, PACK, "ceo")
        row = _row(c, project)
        spec = copy.deepcopy({k: v for k, v in _active_spec(c, row).items()
                              if k != workflows.AGENT_RULES_KEY})
        staging = next(s for s in spec["steps"] if s["id"] == "staging")
        staging["title"] = "Northgate staging"
        workflows.publish_version(c, project_id=project, definition_id=row["id"], spec=spec,
                                  actor="ceo")
        row = _row(c, project)
        assert market.install_state(c, row)["modified"] is True

        # The standard changes (a later BOSS ships a new step title): the edited copy is
        # OFFERED it, never overwritten.
        changed = []
        for entry in catalogue.CATALOGUE:
            if entry["key"] == SOURCE:
                entry = copy.deepcopy(entry)
                entry["steps"][-1]["title"] = "Contract tests on staging"
            changed.append(entry)
        monkeypatch.setattr(catalogue, "CATALOGUE", changed)
        market.sync_shipped(c, project_ids=[project])
        row = _row(c, project)
        live = _active_spec(c, row)
        assert next(s for s in live["steps"] if s["id"] == "staging")["title"] == "Northgate staging"
        assert market.install_state(c, row)["update"] is not None


def test_an_unmodified_copy_follows_the_standard(eng, project, monkeypatch):  # noqa: F811
    with eng.begin() as c:
        catalogue.seed(c, project)
        market.install(c, project, PACK, "ceo")
        changed = []
        for entry in catalogue.CATALOGUE:
            if entry["key"] == SOURCE:
                entry = copy.deepcopy(entry)
                entry["steps"][-1]["title"] = "Contract tests on staging"
            changed.append(entry)
        monkeypatch.setattr(catalogue, "CATALOGUE", changed)
        assert market.sync_shipped(c, project_ids=[project]) == {project: [KEY]}
        titles = [s.get("title") for s in _active_spec(c, _row(c, project))["steps"]]
        assert "Contract tests on staging" in titles


# ── the installed copy runs ─────────────────────────────────────────────────────────────────

def _steps(c, run_id):
    rows = c.execute(text("SELECT step_id, status, output, last_error FROM workflow_step_runs "
                          "WHERE workflow_run_id=:r ORDER BY created_at"),
                     {"r": run_id}).mappings().all()
    return {r["step_id"]: dict(r) for r in rows}


def _scripted_build(c, project, run_id, pr):
    """The build's code task finishing as a scripted-engine build is recorded: completed, with
    its pull request, on the engine that plays no model."""
    c.execute(text(
        "INSERT INTO code_task_runs (id, project_id, status, phase, title, pull_request_url, "
        "spec, finalize_ctx) VALUES (:id, :p, 'completed', 'done', 'Lumen Pay', :pr, "
        "CAST(:spec AS JSONB), '{}'::jsonb) ON CONFLICT (id) DO UPDATE SET status = "
        "EXCLUDED.status, pull_request_url = EXCLUDED.pull_request_url"),
        {"id": run_id, "p": project, "pr": pr,
         "spec": '{"model_config": {"engine": "scripted"}}'})


def test_the_installed_copy_runs_from_its_file_to_the_merge(eng, project, monkeypatch):  # noqa: F811
    crun = f"crun_{uuid.uuid4().hex[:10]}"
    build = RecordingTool("create_code_task", {"code_task_run_id": crun, "status": "started"})
    monkeypatch.setattr(runner, "_tool_registry", lambda: RecordingRegistry([build]))
    with eng.begin() as c:
        catalogue.seed(c, project)
        market.install(c, project, PACK, "ceo")
        row = _row(c, project)
        run = workflows.start_run(
            c, project_id=project, actor="ceo", dedupe_key=f"dk_{uuid.uuid4().hex[:10]}",
            workflow_version_id=row["active_version_id"],
            input_data={"title": "Lumen Pay tenant onboarding",
                        "directive": catalogue.ONBOARD_FILE_BRIEF,
                        "start_from": "lumen-pay.yaml",
                        "start_from_content": "id: lumen-pay\nroutes: []\n",
                        "target_path": TARGET})
        runner.tick(c, Ctx(eng), project_id=project)          # the build starts and waits
        steps = _steps(c, run["id"])
        assert steps["build"]["status"] == "waiting", steps["build"]
        assert build.calls, "the build step never started its code task"
        args = build.calls[0]["args"]
        assert args["remediate"]["output"] == TARGET and args["remediate"]["start_from"] is True

        _scripted_build(c, project, crun, "https://github.com/northgate/gateway/pull/12")
        for _ in range(4):
            runner.tick(c, Ctx(eng), project_id=project)
            workflows.advance_run(c, project, run["id"])
        steps = _steps(c, run["id"])
    assert steps["build"]["status"] == "succeeded", steps["build"]
    assert steps["build"]["output"]["code_task_run_id"] == crun
    assert steps["review"]["status"] == "succeeded", steps["review"]
    # The merge waits on the change's merge card: nothing ships until a person merges.
    assert steps["merge"]["status"] == "waiting", steps["merge"]
    assert "staging" not in steps or steps["staging"]["status"] == "pending"


# ── Share to Library runs the publish-time checks ────────────────────────────────────────────

def _agent_workflow(c, project, key):
    _editor(c, project)
    d = workflows.create_definition(c, project_id=project, workflow_key=key, name="Trim review",
                                    description="Trim and review.", actor="ceo",
                                    namespace=workflows.PRODUCT_NAMESPACE)
    workflows.publish_version(c, project_id=project, definition_id=d["id"], actor="ceo", spec={
        "entry_step": "edit", "steps": [{
            "id": "edit", "kind": "capability", "captain": "video_editor",
            "binding": {"agent_work": True, "inputs": {"instructions": "trim it"}},
            "next": []}]})
    return d


def test_sharing_a_workflow_that_can_no_longer_run_is_refused_with_why(eng, project):  # noqa: F811
    key = f"trim_{uuid.uuid4().hex[:6]}"
    with eng.begin() as c:
        d = _agent_workflow(c, project, key)
        # Since it was saved, its agent lost the only skill it held.
        c.execute(text("DELETE FROM role_skills WHERE project_id=:p AND role_key='video_editor'"),
                  {"p": project})
        with pytest.raises(workflows.WorkflowBindingBlockedError) as refused:
            market.publish(c, project, d["id"], "ceo", tags=["Video"])
        assert "cannot be shared" in str(refused.value)
        assert refused.value.problems
        assert not c.execute(text("SELECT 1 FROM workflow_packs WHERE source_definition_id=:d"),
                             {"d": d["id"]}).first()


def test_sharing_keeps_its_tags(eng, project):  # noqa: F811
    key = f"trim_{uuid.uuid4().hex[:6]}"
    with eng.begin() as c:
        d = _agent_workflow(c, project, key)
        shared = market.publish(c, project, d["id"], "ceo",
                                tags=["Gateway", "gateway", "  On-boarding!! ", ""])
        assert shared["pack"]["tags"] == ["gateway", "on-boarding"]
        listed = {p["id"]: p for p in market.list_packs(c, project)}[shared["pack"]["id"]]
        assert listed["tags"] == ["gateway", "on-boarding"] and listed["source"] == "org"
        # Republished without tags: they stay.
        again = market.publish(c, project, d["id"], "ceo")
        assert again["pack"]["tags"] == ["gateway", "on-boarding"]


def test_a_project_that_cannot_take_a_new_shared_version_is_named_not_dropped(eng, project):  # noqa: F811
    with eng.begin() as c:
        other = _new_project(c, name="library test b")
    try:
        with eng.begin() as c:
            _editor(c, project)
            d = workflows.create_definition(c, project_id=project, workflow_key="tell_team",
                                            name="Tell the team", description=None, actor="ceo",
                                            namespace=workflows.PRODUCT_NAMESPACE)
            workflows.publish_version(c, project_id=project, definition_id=d["id"], actor="ceo",
                                      spec={"entry_step": "tell", "steps": [{
                                          "id": "tell", "kind": "capability", "next": [],
                                          "binding": {"capability": "notify",
                                                      "inputs": {"message": "landed"}}}]})
            pack = market.publish(c, project, d["id"], "ceo")["pack"]
            market.install(c, other, pack["id"], "ceo")
            # v2 adds an agent step: the other project has no agent that holds anything.
            workflows.publish_version(c, project_id=project, definition_id=d["id"], actor="ceo",
                                      spec={"entry_step": "edit", "steps": [
                                          {"id": "edit", "kind": "capability",
                                           "captain": "video_editor",
                                           "binding": {"agent_work": True,
                                                       "inputs": {"instructions": "trim it"}},
                                           "next": ["tell"]},
                                          {"id": "tell", "kind": "capability", "next": [],
                                           "binding": {"capability": "notify",
                                                       "inputs": {"message": "landed"}}}]})
            again = market.publish(c, project, d["id"], "ceo")
            assert [b["project_id"] for b in again["blocked"]] == [other]
            assert again["blocked"][0]["blocked"]
            # It keeps running the version it had.
            assert _row(c, other, "tell_team")["active_version_id"]
            listed = {p["id"]: p for p in market.list_packs(c, project, visible_to={project})}
            assert listed[pack["id"]]["used_count"] == 1
            assert listed[pack["id"]]["installed_in"] == []      # the other project is not visible
    finally:
        with eng.begin() as c:
            c.execute(text("DELETE FROM role_skills WHERE project_id=:p"), {"p": other})
        _cleanup(eng, [other])
