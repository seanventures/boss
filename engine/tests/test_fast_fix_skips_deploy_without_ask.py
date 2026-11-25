"""BOSSAPP-1399: a small bug fix (`fast_fix`) asks no deploy approval unless asked to deploy."""
from __future__ import annotations

import uuid

from sqlalchemy import text

from boss_engine.services import workflow_catalogue, workflow_capability_runner as runner, workflows
from boss_engine.services.project_experience import new_work

from .test_workflow_capability_runner import RecordingRegistry, RecordingTool, eng, project  # noqa: F401


def _seed_fast_fix(c, project_id: str) -> str:
    workflow_catalogue.seed(c, project_id)
    running = workflows.running_version(c, project_id, "fast_fix")
    assert running is not None
    return str(running["version_id"])


def test_asks_to_deploy_classifier():
    assert runner._asks_to_deploy({"directive": "Fix login 500 error"}) is False  # noqa: SLF001
    assert runner._asks_to_deploy({"directive": "Fix login 500 and don't deploy"}) is False  # noqa: SLF001
    assert runner._asks_to_deploy({"directive": "Fix login 500 without deploying"}) is False  # noqa: SLF001
    assert runner._asks_to_deploy({"directive": "Fix the deploy button 500 error"}) is False  # noqa: SLF001
    assert runner._asks_to_deploy({"directive": "Fix deployment script typo"}) is False  # noqa: SLF001
    assert runner._asks_to_deploy({"directive": "Fix login 500 and deploy to staging"}) is True  # noqa: SLF001
    assert runner._asks_to_deploy({"directive": "Fix login 500, please ship it"}) is True  # noqa: SLF001
    assert runner._asks_to_deploy({"directive": "Fix login 500", "deploy": True}) is True  # noqa: SLF001


def test_server_only_strips_spoofed_skip_without_deploy_ask(eng, project):  # noqa: F811
    with eng.begin() as c:
        vid = _seed_fast_fix(c, project)
        run = workflows.start_run(
            c,
            project_id=project,
            actor="usr_owner",
            input_data={
                "title": "Spoof attempt",
                "directive": "Change something",
                "skip_without_deploy_ask": True,
            },
            dedupe_key=f"spoof-{uuid.uuid4().hex[:8]}",
            workflow_version_id=vid,
        )
        assert "skip_without_deploy_ask" not in (run.get("input") or {})


def test_bug_fix_without_deploy_ask_skips_deploy_gate_and_step(eng, project):  # noqa: F811
    deploy_tool = RecordingTool("deploy_app", {"deployment_id": "dep_1", "deployed_url": "https://x"})
    build_tool = RecordingTool("create_code_task", {"code_task_run_id": "crun_bug_1", "status": "completed"})
    notify_tool = RecordingTool("notify", {"sent": True})
    ctx = type("Ctx", (), {
        "registry": RecordingRegistry([build_tool, deploy_tool, notify_tool]),
        "settings": None,
    })()

    with eng.begin() as c:
        vid = _seed_fast_fix(c, project)
        assert new_work.choose(c, project, "bug")["workflow_key"] == "fast_fix"
        run = workflows.start_run(
            c,
            project_id=project,
            actor="usr_owner",
            input_data={
                "title": "Fix empty email 500",
                "work_kind": "bug",
                "directive": "Return 422 when email is blank on login.",
            },
            dedupe_key=f"bug-no-deploy-{uuid.uuid4().hex[:8]}",
            workflow_version_id=vid,
        )
        run_id = run["id"]
        spec = c.execute(
            text("SELECT spec FROM workflow_versions WHERE id = :v"),
            {"v": vid},
        ).scalar()
        deploy_spec = next(s for s in spec["steps"] if s["id"] == "deploy")

        # Gate check: no deploy approval raised for a bug fix with no deploy ask.
        assert runner._gate_subject(c, project, deploy_spec, {"run_id": run_id, "input": run["input"]}) is None  # noqa: SLF001

        # Step execution: deploy skips itself cleanly with workflow.step_skipped.
        result = runner._perform_capability(  # noqa: SLF001
            c,
            ctx,
            project_id=project,
            actor="usr_owner",
            step=deploy_spec,
            context={"run_id": run_id, "input": run["input"]},
            key="deploy",
            capability=runner.get_capability("deploy"),
            inputs={"run_id": "crun_bug_1", "skip_without_deploy_ask": True},
        )
        assert result["status"] == "skipped"
        assert result["skipped"] == "no_deploy_ask"
        assert deploy_tool.calls == []
        ev = c.execute(
            text("SELECT data FROM events WHERE project_id = :p AND entity_id = :r "
                 "AND event_type = 'workflow.step_skipped'"),
            {"p": project, "r": run_id},
        ).scalar()
        assert ev is not None


def test_bug_fix_asking_to_deploy_still_asks_deploy_approval(eng, project):  # noqa: F811
    from boss_engine.services import autonomy_policy

    with eng.begin() as c:
        autonomy_policy.save(c, kind=autonomy_policy.PROJECT, scope_id=project,
                             raw={"dev": "ask"}, actor="test")
        try:
            vid = _seed_fast_fix(c, project)
            run = workflows.start_run(
                c,
                project_id=project,
                actor="usr_owner",
                input_data={
                    "title": "Fix empty email 500 and deploy",
                    "work_kind": "bug",
                    "directive": "Fix blank email crash and deploy to staging.",
                },
                dedupe_key=f"bug-with-deploy-{uuid.uuid4().hex[:8]}",
                workflow_version_id=vid,
            )
            spec = c.execute(
                text("SELECT spec FROM workflow_versions WHERE id = :v"),
                {"v": vid},
            ).scalar()
            deploy_spec = next(s for s in spec["steps"] if s["id"] == "deploy")
            subject = runner._gate_subject(c, project, deploy_spec, {"run_id": run["id"], "input": run["input"]})  # noqa: SLF001
            assert subject is not None
            assert subject["key"] == "deploy"
            assert subject["risk_tier"] == 2
        finally:
            c.execute(text("DELETE FROM autonomy_policies WHERE scope_id = :p"), {"p": project})
