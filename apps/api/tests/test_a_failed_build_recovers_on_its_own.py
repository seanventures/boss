"""A failed BUILD step is recovered by its own agent too — and BOSS's own bugs are written down.

The build failures of 2026-10-16, each of which went to the operator:
  * the template gate said the app "needs Java" because BOSS's planner read the SHARED
    REPOSITORY ROOT, not the app's folder                     → BOSS's own: wait_for_boss;
  * the secret scan blocked a committed literal with no repair turn → the app's: fix_app with a
    precise note (remove the literal, read the environment);
  * the app exited at start because BOSS's start check gave it no environment → BOSS's own;
  * anything else that looks transient                             → retry, once.
A BOSS-side cause becomes ONE recommendation for BOSS's developers (deduped by cause across
runs), on the step and as an event; the step resumes by itself when BOSS's build changes.

The build itself is a stub (rows in code_task_runs); the model is a stub behind BOSS's router.
"""
from __future__ import annotations

import json
from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy import text

from apps.api.app import main as api_main
from boss_engine.services import boss_recommendations, step_recovery, workflows
from boss_engine.services import workflow_capability_runner as runner

from apps.api.tests.test_a_deploy_that_fails_for_the_app_is_fixed_and_redeployed import (  # noqa: F401 - fixture
    repairs,
)
from apps.api.tests.test_a_failed_deploy_recovers_on_its_own import StubModel, model  # noqa: F401

HOME = "boss-dogfood"
JAVA = ("template gate held: the app needs Java (pom.xml found at the repository root), and this "
        "build has no Java toolchain")
SECRET = ("blocked: no_committed_secrets failed — the secret scan found a committed secret "
          "literal at auth/demo.mjs:58 (generic-password)")
NO_ENV = ("the app exited at start: the start check had no env — DATABASE_URL was not set when "
          "BOSS started it")


@pytest.fixture()
def builds(monkeypatch, engine):
    """The build capability's create_code_task, as a row this test finishes by hand."""
    started: list[str] = []

    def create_code_task(ctx, tool_name, project_id, actor, args):
        run_id = f"crun_{uuid4().hex[:10]}"
        with engine.begin() as c:
            c.execute(text("INSERT INTO code_task_runs (id, project_id, status, title, spec) "
                           "VALUES (:i, :p, 'running', 'HelpDesk', "
                           "'{\"owner\": \"acme\", \"repo\": \"shop\"}')"),
                      {"i": run_id, "p": HOME})
        started.append(run_id)
        return {"run_id": run_id}

    monkeypatch.setattr(runner, "_invoke_tool", create_code_task)
    return started


def _build_run(engine) -> str:
    key = f"buildrec_{uuid4().hex[:8]}"
    with engine.begin() as c:
        definition = workflows.create_definition(
            c, project_id=HOME, workflow_key=key, name=key, description=None, actor="test",
            namespace="product")
        version = workflows.publish_version(
            c, project_id=HOME, definition_id=definition["id"], actor="test",
            spec={"entry_step": "build", "steps": [{
                "id": "build", "kind": "capability", "next": [],
                "binding": {"capability": "build",
                            "inputs": {"title": "HelpDesk", "instructions": "Build it."}}}]})
        run = workflows.start_run(c, project_id=HOME, actor="ceo", input_data={},
                                  dedupe_key=f"dk_{uuid4().hex[:10]}",
                                  workflow_version_id=version["id"])
    return run["id"]


def _tick(engine):
    with engine.begin() as c:
        return runner.tick(c, SimpleNamespace(engine=engine, settings=api_main.settings,
                                              begin=engine.begin, jobs=object()),
                           project_id=HOME)


def _step(engine, run_id: str) -> dict:
    with engine.begin() as c:
        return dict(c.execute(
            text("SELECT status, output, last_error FROM workflow_step_runs WHERE "
                 "workflow_run_id = :r AND step_id = 'build' ORDER BY created_at DESC LIMIT 1"),
            {"r": run_id}).mappings().one())


def _fail(engine, run_id: str, error: str, status: str = "failed") -> None:
    with engine.begin() as c:
        c.execute(text("UPDATE code_task_runs SET status = :s, error = :e WHERE id = :i"),
                  {"s": status, "e": error, "i": run_id})


def _decisions(engine, run_id: str) -> list[dict]:
    return [d for d in ((_step(engine, run_id)["output"] or {}).get("recovery") or {})
            .get("decisions") or [] if d.get("action") in step_recovery.ACTIONS]


BOSS_FIX = {"action": "wait_for_boss", "cause": "BOSS's planner read the repository root",
            "reasoning": "The app's own folder has no pom.xml; the root belongs to another app.",
            "evidence": ["pom.xml found at the repository root"],
            "boss_fix": {"title": "Template gate plans from the shared repository root",
                         "root_cause": "the toolchain planner reads the repo root, not the app",
                         "proposed_change": "plan toolchains from the app's own folder",
                         "files": ["engine/boss_engine/templates/gate.py"]}}


def test_a_boss_side_build_failure_is_recorded_and_resumes_after_an_update(
        engine, builds, repairs, model, monkeypatch):
    model({"build:boss": BOSS_FIX})
    run_id = _build_run(engine)
    _tick(engine)
    assert _step(engine, run_id)["status"] == "waiting" and len(builds) == 1
    _fail(engine, builds[0], JAVA)
    _tick(engine)

    step = _step(engine, run_id)
    assert step["status"] == "waiting", step
    assert step["output"]["await"]["kind"] == "recovery"
    assert [d["action"] for d in _decisions(engine, run_id)] == ["wait_for_boss"]
    assert repairs == [], "no build is started for a cause only BOSS can fix"
    with engine.begin() as c:
        recs = boss_recommendations.list_recommendations(c, HOME)
    assert len(recs) == 1 and recs[0]["title"] == BOSS_FIX["boss_fix"]["title"]
    assert recs[0]["refs"][0]["build_id"] == builds[0]
    assert recs[0]["files"] == ["engine/boss_engine/templates/gate.py"]

    # A second run hits the same cause: the same recommendation, counted again.
    other = _build_run(engine)
    _tick(engine)
    _fail(engine, builds[1], JAVA)
    _tick(engine)
    with engine.begin() as c:
        recs = boss_recommendations.list_recommendations(c, HOME)
    assert len(recs) == 1 and recs[0]["occurrences"] == 2
    assert {r["run_id"] for r in recs[0]["refs"]} == {run_id, other}

    # BOSS is updated: the build starts again by itself and the step follows it.
    monkeypatch.setattr(step_recovery, "boss_build", lambda: "1.0.0+updated")
    _tick(engine)
    assert len(repairs) == 2 and {r["from"] for r in repairs} == {builds[0], builds[1]}
    step = _step(engine, run_id)
    assert step["status"] == "waiting"
    assert step["output"]["await"] == {"kind": "code_task", "id": repairs[0]["run_id"]} or \
        step["output"]["await"]["kind"] == "code_task"


def test_a_secret_scan_block_is_fixed_by_the_app(engine, builds, repairs, model):
    model({"build:secret_scan": {
        "action": "fix_app", "cause": "a committed secret literal at auth/demo.mjs:58",
        "reasoning": "The app's own code holds the literal; the fix is in the app.",
        "evidence": ["auth/demo.mjs:58"],
        "note": "Remove the literal password at auth/demo.mjs:58; read DEMO_PASSWORD from "
                "process.env with a non-secret development default."}})
    run_id = _build_run(engine)
    _tick(engine)
    _fail(engine, builds[0], SECRET, status="blocked")
    _tick(engine)

    assert len(repairs) == 1 and repairs[0]["from"] == builds[0]
    assert "auth/demo.mjs:58" in repairs[0]["note"] and "process.env" in repairs[0]["note"]
    step = _step(engine, run_id)
    assert step["status"] == "waiting"
    assert step["output"]["await"] == {"kind": "code_task", "id": repairs[0]["run_id"]}
    assert step["output"]["note"].endswith("fixing the app in a repair build")


def test_with_no_model_boss_triage_still_places_the_build_failures(engine, builds, repairs):
    run_id = _build_run(engine)
    _tick(engine)
    _fail(engine, builds[0], NO_ENV)
    _tick(engine)
    decision = _decisions(engine, run_id)[-1]
    assert decision["action"] == "wait_for_boss" and decision["by"]["engine"] == "coded"

    other = _build_run(engine)
    _tick(engine)
    _fail(engine, builds[1], "worker lost: connection reset by peer while cloning")
    _tick(engine)
    assert [d["action"] for d in _decisions(engine, other)] == ["retry"]
    assert len(repairs) == 1
    # The retry fails the same way: the same action is not taken twice → a person is asked.
    _fail(engine, repairs[0]["run_id"], "worker lost: connection reset by peer while cloning")
    _tick(engine)
    step = _step(engine, other)
    assert step["status"] == "failed", step
    assert [d["action"] for d in _decisions(engine, other)] == ["retry", "ask_person"]
    assert "asks:" in step["last_error"]


def test_the_recommendations_file_is_written_and_served(client, engine, builds, model,
                                                        monkeypatch, tmp_path):
    monkeypatch.setattr(type(api_main.settings), "artifact_root",
                        property(lambda self: tmp_path))
    model({"build:boss": BOSS_FIX})
    run_id = _build_run(engine)
    _tick(engine)
    _fail(engine, builds[0], JAVA)
    _tick(engine)
    written = (tmp_path / "boss-improvements" / "RECOMMENDATIONS.md").read_text()
    assert "Template gate plans from the shared repository root" in written
    assert run_id in written and builds[0] in written and "**Status:** open" in written
    listed = client.get("/v1/boss-recommendations", params={"project_id": HOME}).json()
    assert listed["count"] == 1 and listed["file"] == "RECOMMENDATIONS.md"
    served = client.get("/v1/boss-recommendations.md", params={"project_id": HOME})
    assert served.text == written
    assert "attachment" in served.headers["content-disposition"]
    assert json.dumps(listed).count("KEYMATERIAL") == 0
