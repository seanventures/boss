"""A deploy that fails for the APP's reason is fixed by BOSS and shipped again — automatically.

The laptop's HelpDesk run (wfr_fb4fb5ded8c84a4dab12, 2026-10-16): the build passed, its deploy
planned helpdesk-ai/boss.deploy.yaml (`kind: commands`, `npm install`, `npm start &`) onto a host
with Docker and no Node, and the step failed at `install 1` with `npm: command not found`. The
build had succeeded so it could not be retried, and the deploy pins the build's commit: the
operator had no way to get a fix in short of a new run.

Now (deploy_repair): the preflight refuses the plan before anything is written, the deploy step
starts ONE repair build of the build's lineage told what failed and what the machine has, waits
on it, and plans a new deployment of the pull request's new head under that deployment's own
approval. The cap stops it; a failure on the host's side and a person's "no" never auto-fix;
the build is told the machine's facts up front; the step's Ask carries the deployment.

The host is a fake that answers like a Docker machine without Node; GitHub is a fake; the
repair build is a stub that records its brief. Everything BOSS decides is real.
"""
from __future__ import annotations

import subprocess
from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy import text

from apps.api.app import main as api_main
from boss_engine.services import deploy_repair, workflows
from boss_engine.services import workflow_capability_runner as runner

from apps.api.tests.test_a_stack_deploys_only_through_an_approved_changeset import (  # noqa: F401 - fixture
    HOME,
    _approve_and_execute,
    _build,
    stack,
)

NPM_MANIFEST = """
version: 1
name: helpdesk-fix
kind: commands
install: ["npm install --no-audit --no-fund --loglevel=error", "npm start &"]
health: [{url: "http://127.0.0.1:8000/", timeout_seconds: 30}]
open: {url: "{target.public_url}"}
"""
FIXED_MANIFEST = """
version: 1
name: helpdesk-fix
install: ["./start.sh"]
health: [{url: "http://127.0.0.1:8000/", timeout_seconds: 30}]
open: {url: "{target.public_url}"}
"""
SCRIPT_MANIFEST = FIXED_MANIFEST
FIXED_HEAD = "f" * 40


@pytest.fixture()
def docker_host(stack):
    """The stack fixture's host, answering the preflight as a machine with Docker and no Node,
    and failing the install with `npm: command not found` when told to."""
    host = stack["host"]
    answer = host.stack_exec
    host.install_fails_with = None

    def stack_exec(**kwargs):
        command = kwargs["command"]
        if "BOSS_SSH_OK" in command:
            out = ("BOSS_SSH_OK\nDIR_CREATABLE\nSUDO_OK\nDOCKER_PRESENT\nDOCKER_USABLE\n"
                   "TAR_OK\nCURL_OK\nBOSS_HAS docker\nBOSS_LACKS node\nBOSS_LACKS npm\n"
                   "BOSS_VERSION docker Docker version 27.1.1, build 6312585\n")
            host.calls.append(kwargs)
            return subprocess.CompletedProcess([], 0, out, "")
        if host.install_fails_with and "bash -lc" in command:
            host.calls.append(kwargs)
            code, said = host.install_fails_with
            return subprocess.CompletedProcess([], code, "", said)
        return answer(**kwargs)

    host.stack_exec = stack_exec
    stack["github"].read_file = lambda owner, repo, path, commit: (
        stack["manifest"].get(commit, stack["manifest"]["*"]) if path == "boss.deploy.yaml"
        else None)
    stack["manifest"] = {"*": NPM_MANIFEST}
    return stack


@pytest.fixture()
def repairs(engine, monkeypatch):
    """retry_code_task as a stub: records the brief, makes the repair attempt's row."""
    from boss_engine.services import code_task_service

    started: list[dict] = []

    def retry(engine_, settings, jobs, run_id, *, fix_note="", **_):
        new_id = f"crun_{uuid4().hex[:10]}"
        with engine.begin() as c:
            c.execute(text(
                "INSERT INTO code_task_runs (id, project_id, status, title, spec, "
                "pull_request_number, finalize_ctx) SELECT :n, project_id, 'running', title, "
                "spec, pull_request_number, jsonb_build_object('parent_run_id', CAST(:r AS TEXT),"
                " 'root_run_id', CAST(:r AS TEXT)) FROM code_task_runs WHERE id = :r"),
                {"n": new_id, "r": run_id})
        started.append({"from": run_id, "run_id": new_id, "note": fix_note})
        return {"status": "queued", "run_id": new_id}

    monkeypatch.setattr(code_task_service, "retry_code_task", retry)
    return started


def _deploy_run(engine, build_run_id: str, **extra) -> str:
    key = f"fixship_{uuid4().hex[:8]}"
    with engine.begin() as c:
        definition = workflows.create_definition(
            c, project_id=HOME, workflow_key=key, name=key, description=None, actor="test",
            namespace="product")
        version = workflows.publish_version(
            c, project_id=HOME, definition_id=definition["id"], actor="test",
            spec={"entry_step": "deploy", "steps": [{
                "id": "deploy", "kind": "capability", "next": [],
                "binding": {"capability": "deploy",
                            "inputs": {"run_id": {"from_input": "build_run_id"}}},
                "approval": {"approver_role": "release_manager"},
            }]})
        run = workflows.start_run(
            c, project_id=HOME, actor="ceo", input_data={"build_run_id": build_run_id, **extra},
            dedupe_key=f"dk_{uuid4().hex[:10]}", workflow_version_id=version["id"])
    return run["id"]


def _tick(engine):
    with engine.begin() as c:
        return runner.tick(c, SimpleNamespace(engine=engine, settings=api_main.settings,
                                              begin=engine.begin, jobs=object()),
                           project_id=HOME)


def _step(engine, run_id: str) -> dict:
    with engine.begin() as c:
        return dict(c.execute(
            text("SELECT id, status, decision_id, output, last_error FROM workflow_step_runs "
                 "WHERE workflow_run_id = :r AND step_id = 'deploy' "
                 "ORDER BY created_at DESC LIMIT 1"), {"r": run_id}).mappings().one())


def _events(engine, run_id: str, kind: str) -> list[dict]:
    with engine.begin() as c:
        return [dict(r) for r in c.execute(
            text("SELECT data FROM events WHERE entity_id = :r AND event_type = :k"),
            {"r": run_id, "k": kind}).mappings().all()]


def _finish(engine, run_id: str, status: str = "completed") -> None:
    with engine.begin() as c:
        c.execute(text("UPDATE code_task_runs SET status = :s WHERE id = :i"),
                  {"s": status, "i": run_id})


def _deployments(engine, target_id: str) -> list[dict]:
    with engine.begin() as c:
        return [dict(r) for r in c.execute(
            text("SELECT id, status, commit_sha, code_task_run_id, decision_id FROM "
                 "stack_deployments WHERE target_id = :t ORDER BY created_at"),
            {"t": target_id}).mappings().all()]


def test_a_missing_runtime_is_refused_before_the_host_is_touched_then_fixed_and_redeployed(
        client, engine, docker_host, repairs):
    stack = docker_host
    build = _build(engine, '{"owner": "acme", "repo": "shop"}')
    run_id = _deploy_run(engine, build)
    _tick(engine)

    # REFUSED IN PREFLIGHT: nothing planned, nothing written to the machine.
    assert stack["host"].writes() == []
    assert _deployments(engine, stack["target"]["id"]) == []

    # ONE repair build of the build's lineage, told what failed and what the machine has.
    assert len(repairs) == 1 and repairs[0]["from"] == build
    note = repairs[0]["note"]
    for said in ("has no `npm`", "Docker", "has no node, npm", "kind: compose",
                 "npm install --no-audit", "automatic fix 1 of 4"):
        assert said in note, said

    step = _step(engine, run_id)
    assert step["status"] == "waiting", step
    handle = step["output"]["await"]
    assert handle == {"kind": "code_task", "id": repairs[0]["run_id"],
                      "then": deploy_repair.THEN_REDEPLOY, "attempt": 1}
    # Said by the deploy step's own agent (step_recovery), with the build it started.
    assert step["output"]["note"].endswith(
        f": Deploy failed: `npm` missing on 203.0.113.40 → fixing the app "
        f"(build {repairs[0]['run_id']}) → redeploying")
    assert [f["build_run_id"] for f in step["output"]["deploy_fixes"]] == [repairs[0]["run_id"]]
    assert _events(engine, run_id, deploy_repair.FIX_EVENT)
    # The host's facts were kept for the next build and the deploy agent.
    with engine.begin() as c:
        pf = c.execute(text("SELECT last_preflight FROM deployment_hosts WHERE id = :h"),
                       {"h": stack["target"]["host_id"]}).scalar()
    assert pf["facts"]["tools"] == {"docker": True, "node": False, "npm": False}

    # The repair pushes a manifest the machine can run; the PR's head moves.
    stack["manifest"] = {"*": SCRIPT_MANIFEST}
    stack["github"].refs["pull/7"] = FIXED_HEAD
    _finish(engine, repairs[0]["run_id"])
    _tick(engine)
    _tick(engine)

    assert _events(engine, run_id, deploy_repair.REDEPLOY_EVENT)
    planned = _deployments(engine, stack["target"]["id"])
    assert len(planned) == 1, planned
    assert planned[0]["commit_sha"] == FIXED_HEAD
    assert planned[0]["code_task_run_id"] == repairs[0]["run_id"]
    step = _step(engine, run_id)
    assert step["status"] == "waiting" and step["output"]["await"]["kind"] == "stack_deployment"
    assert step["output"]["deploy_fixes"][0]["build_run_id"] == repairs[0]["run_id"]

    # The new commit is approved as any deployment is — by its own decision.
    deployment = client.get(f"/v1/deployments/stack/{planned[0]['id']}").json()["deployment"]
    assert deployment["decision_id"]
    done, _ = _approve_and_execute(client, engine, deployment)
    assert done["deployment"]["status"] == "healthy", done
    _tick(engine)
    assert _step(engine, run_id)["status"] == "succeeded"
    assert len(repairs) == 1, "exactly one repair build"


def test_an_install_that_fails_with_the_apps_error_is_fixed_with_its_log(
        client, engine, docker_host, repairs):
    stack = docker_host
    stack["manifest"] = {"*": SCRIPT_MANIFEST}
    stack["host"].install_fails_with = (127, "bash: line 1: npm: command not found\n")
    build = _build(engine, '{"owner": "acme", "repo": "shop"}')
    run_id = _deploy_run(engine, build)
    _tick(engine)
    deployment_id = _step(engine, run_id)["output"]["stack_deployment_id"]
    deployment = client.get(f"/v1/deployments/stack/{deployment_id}").json()["deployment"]
    done, _ = _approve_and_execute(client, engine, deployment)
    assert done["deployment"]["status"] == "failed"
    assert done["deployment"]["error"]["cause"] == "app"
    assert done["deployment"]["error"]["step"] == "install: install 1"

    _tick(engine)
    assert len(repairs) == 1
    note = repairs[0]["note"]
    assert "npm: command not found" in note and "`./start.sh`" in note
    step = _step(engine, run_id)
    assert step["status"] == "waiting", step
    assert step["output"]["await"]["then"] == deploy_repair.THEN_REDEPLOY
    assert step["output"]["deploy_fixes"][0]["deployment_id"] == deployment_id

    # THE STEP'S ASK carries the deployment: steps, the failing command, the log, the machine,
    # the fix — so the deploy agent answers from the record.
    from boss_engine.services import turn_subject

    with engine.begin() as c:
        evidence = turn_subject.resolve_subject(
            c, HOME, turn_subject.SubjectRef("workflow_step", run_id, "deploy"))
    said = evidence.render("why did the deploy fail?")
    for part in ("deployment failure", "install: install 1 ran `./start.sh`",
                 "npm: command not found", "has Docker", "has no node, npm",
                 "automatic fixes", repairs[0]["run_id"], deployment_id):
        assert part in said, part
    assert "KEYMATERIAL" not in said

    # The deploy agent's own read tool shows the same deployment in full.
    from boss_engine.tools.builtin import build_default_registry

    tool = {s.name: s for s in build_default_registry().specs()}["list_deployments"]
    read = tool.handler(SimpleNamespace(begin=engine.begin), project_id=HOME, actor="ceo",
                        args={"deployment_id": deployment_id})
    assert read["deployment"]["step"] == "install: install 1"
    assert "npm: command not found" in read["deployment"]["log_tail"]
    listed = tool.handler(SimpleNamespace(begin=engine.begin), project_id=HOME, actor="ceo",
                          args={})
    assert deployment_id in {d["id"] for d in listed["stack_deployments"]}


def test_the_cap_stops_the_repairs_and_the_step_fails_with_every_cause(
        client, engine, docker_host, repairs, monkeypatch):
    stack = docker_host
    monkeypatch.setattr(deploy_repair, "fix_cap", lambda *a, **k: 1)
    build = _build(engine, '{"owner": "acme", "repo": "shop"}')
    run_id = _deploy_run(engine, build)
    _tick(engine)
    assert len(repairs) == 1
    # The repair did not fix it: the manifest still runs npm.
    stack["github"].refs["pull/7"] = FIXED_HEAD
    _finish(engine, repairs[0]["run_id"])
    _tick(engine)
    _tick(engine)

    assert len(repairs) == 1, "the cap is one repair"
    step = _step(engine, run_id)
    # Settled for a person (Try again after a change, or Skip): a retry is refused the same way.
    assert step["status"] == "deadlettered", step
    assert "has no `npm`" in step["last_error"]
    assert "did not fix it by itself" in step["output"]["error"]
    assert "after 1 automatic fix" in step["output"]["error"]


def test_a_failed_repair_fails_the_step_with_both_causes(engine, docker_host, repairs):
    build = _build(engine, '{"owner": "acme", "repo": "shop"}')
    run_id = _deploy_run(engine, build)
    _tick(engine)
    _finish(engine, repairs[0]["run_id"], "failed")
    _tick(engine)

    step = _step(engine, run_id)
    assert step["status"] == "failed", step
    assert "the deploy failed" in step["last_error"]
    assert f"repair build {repairs[0]['run_id']} did not pass" in step["last_error"]


def test_a_host_side_failure_is_a_persons_and_never_auto_fixed(
        client, engine, docker_host, repairs):
    stack = docker_host
    stack["manifest"] = {"*": SCRIPT_MANIFEST}
    stack["host"].install_fails_with = (
        255, "ssh: connect to host 203.0.113.40 port 22: Connection refused\n")
    run_id = _deploy_run(engine, _build(engine, '{"owner": "acme", "repo": "shop"}'))
    _tick(engine)
    deployment_id = _step(engine, run_id)["output"]["stack_deployment_id"]
    deployment = client.get(f"/v1/deployments/stack/{deployment_id}").json()["deployment"]
    done, _ = _approve_and_execute(client, engine, deployment)
    assert done["deployment"]["error"]["cause"] == "host"
    _tick(engine)

    assert repairs == []
    step = _step(engine, run_id)
    assert step["status"] == "failed" and "could not connect" in step["last_error"]


def test_a_host_the_ssh_user_cannot_write_is_refused_and_never_auto_fixed(
        engine, docker_host, repairs):
    stack = docker_host
    stack["manifest"] = {"*": SCRIPT_MANIFEST}
    answer = stack["host"].stack_exec

    def no_dir(**kwargs):
        done = answer(**kwargs)
        if "BOSS_SSH_OK" in kwargs["command"]:
            return subprocess.CompletedProcess(
                [], 0, done.stdout.replace("DIR_CREATABLE", "DIR_NEEDS_SUDO")
                .replace("SUDO_OK", "SUDO_NO"), "")
        return done

    stack["host"].stack_exec = no_dir
    run_id = _deploy_run(engine, _build(engine, '{"owner": "acme", "repo": "shop"}'))
    _tick(engine)
    assert repairs == []
    step = _step(engine, run_id)
    # A person's to fix on the machine: the step goes back to its ordinary retries, no repair.
    assert step["status"] in ("ready", "failed", "deadlettered"), step
    assert "passwordless sudo" in step["last_error"]
    assert not (step["output"] or {}).get("deploy_fixes")


def test_a_rejected_deployment_is_never_auto_fixed(client, engine, docker_host, repairs):
    docker_host["manifest"] = {"*": SCRIPT_MANIFEST}
    run_id = _deploy_run(engine, _build(engine, '{"owner": "acme", "repo": "shop"}'))
    _tick(engine)
    decision_id = _step(engine, run_id)["output"]["decision_id"]
    assert client.post(f"/v1/decisions/{decision_id}/reject",
                       json={"actor": "ceo", "note": "not now"}).status_code == 200
    _tick(engine)
    assert repairs == []
    step = _step(engine, run_id)
    assert step["status"] == "failed" and "not approved" in step["last_error"]


def test_the_build_is_told_what_the_machine_it_deploys_to_has(engine, docker_host, monkeypatch):
    stack = docker_host
    host_id = stack["target"]["host_id"]
    with engine.begin() as c:
        c.execute(text(
            "UPDATE deployment_hosts SET last_preflight = CAST(:pf AS JSONB) WHERE id = :h"),
            {"h": host_id, "pf": '{"reachable": true, "facts": {"tools": {"docker": true, '
                                 '"node": false, "npm": false}, "versions": {"docker": '
                                 '"Docker version 27.1.1, build 6312585"}}}'})
    run_id = _deploy_run(engine, "crun_not_built_yet", host_id=host_id)

    seen: dict = {}

    def create_code_task(ctx, tool_name, project_id, actor, args):
        seen.update(args)
        return {"run_id": f"crun_{uuid4().hex[:10]}"}

    monkeypatch.setattr(runner, "_invoke_tool", create_code_task)
    from boss_engine.services import workflow_capabilities

    with engine.begin() as c:
        context = runner._run_context(c, run_id)
        assert context["deploys"]
        runner._perform_capability(
            c, SimpleNamespace(engine=engine, settings=api_main.settings, begin=engine.begin),
            project_id=HOME, actor="ceo", step={"id": "build", "kind": "capability"},
            context=context, key="build", capability=workflow_capabilities.get("build"),
            inputs={"title": "HelpDesk", "instructions": "Build a help desk."})
    told = str(seen.get("instructions") or "")
    assert "THE MACHINE THIS APP DEPLOYS TO" in told, told[-800:]
    assert "has Docker (27.1.1)" in told and "has no node, npm" in told
    assert "kind: compose" in told
