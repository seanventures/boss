"""AR-54 + BOSSAPP-1377: a workflow's build never starts a second app from scratch, and a custom
workflow chains build -> config deploy -> authenticated contract test.

THE REAL ROW (northgate-gateway, crun_671a765b, workflow run wfr_7b661509, step validate_and_fix):
spec repo=northgate-gateway owner=local_northgate-gateway base_branch=None. The clone worked, onto
`main` — a README and a .gitignore, because the app BOSS built earlier lives on unmerged
`boss/code/<root>` branches. The agent found no app, wrote cmd/gwlint and a go.mod from scratch
and the step succeeded; the record named `npm install && npm run build` (the plan made on the
empty tree) with ok:true for a Go module. Builds started from the build page pass the lineage
branch and were never affected.
"""
from __future__ import annotations

import json
import subprocess
import uuid
from pathlib import Path

import pytest
from sqlalchemy import text

from boss_engine.config import Settings
from boss_engine.services import build_checkout, config_deploy, local_repo
from boss_engine.services import governed_code_task_service as governed
from boss_engine.services import verification_target as vt
from boss_engine.services.deploy_policy import set_approval_policy

from .test_config_is_deployed_through_a_governed_changeset import (  # noqa: F401
    PERSON, _apply, _fresh_settings, _target, dirs, gateway, tidy)
from .test_workflow_capability_runner import eng, project  # noqa: F401

OWNER = "local_northgate-gateway"
REPO = "northgate-gateway"
LINEAGE = "crun_24f399769d9f4f5d8fa1"
GO_FILES = {"go.mod": "module northgate\n\ngo 1.23\n",
            "cmd/gw/main.go": "package main\n\nfunc main() {}\n",
            "tenants/lumen-pay.yaml": "tenant: lumen-pay\nversion: 3\n"}


def _git(*args: str, cwd: Path) -> str:
    return subprocess.run(["git", *args], cwd=str(cwd), check=True, capture_output=True,
                          text=True).stdout


def _bare_with_app_on_a_branch(tmp_path: Path) -> Path:
    bare = local_repo.init_bare(tmp_path / "northgate-gateway.git")
    local_repo.commit_files(bare, "main", {"README.md": "# northgate\n", ".gitignore": "bin/\n"},
                            "Initial commit (BOSS)")
    branch = f"boss/code/{LINEAGE}"
    local_repo.ensure_branch(bare, branch, "main")
    local_repo.commit_files(bare, branch, dict(GO_FILES), "the app")
    return bare


def _clone(bare: Path, branch: str, into: Path) -> Path:
    into.mkdir(parents=True)
    _git("clone", "--depth", "1", "--branch", branch, local_repo.file_url(bare), ".", cwd=into)
    return into


def _earlier_build(c, project_id: str, *, files=("go.mod",), root=LINEAGE):
    c.execute(text(
        "INSERT INTO code_task_runs (id, project_id, status, title, spec, state, finalize_ctx) "
        "VALUES (:i, :p, 'completed', 't', CAST(:s AS JSONB), CAST(:st AS JSONB), "
        "CAST(:f AS JSONB))"),
        {"i": root, "p": project_id, "s": json.dumps({"owner": OWNER, "repo": REPO}),
         "st": json.dumps({"impl_files": list(files)}), "f": "{}"})


# ── A. a build never proceeds on a checkout with no app when the repo has one ────────────────

def test_a_workflow_build_on_the_mainline_of_a_repo_with_an_unmerged_app_stops_with_the_branch(
        eng, project, tmp_path):  # noqa: F811
    bare = _bare_with_app_on_a_branch(tmp_path)
    tree = _clone(bare, "main", tmp_path / "wf-build")
    assert build_checkout.app_files(tree) == []          # README + .gitignore only
    with eng.begin() as c:
        _earlier_build(c, project)
        why = build_checkout.problem(c, tree, project_id=project, owner=OWNER, repo=REPO,
                                     run_id="crun_new", base_branch="main")
    assert why and f"boss/code/{LINEAGE}" in why and "second app from scratch" in why


def test_a_clone_of_the_apps_branch_or_a_first_build_is_never_refused(eng, project, tmp_path):  # noqa: F811
    bare = _bare_with_app_on_a_branch(tmp_path)
    on_branch = _clone(bare, f"boss/code/{LINEAGE}", tmp_path / "on-branch")
    empty = _clone(bare, "main", tmp_path / "empty")
    with eng.begin() as c:
        # the app's own branch holds the app: builds on its content
        _earlier_build(c, project)
        assert build_checkout.problem(c, on_branch, project_id=project, owner=OWNER, repo=REPO,
                                      run_id="crun_new", base_branch="x") is None
        assert "cmd/gw/main.go" in build_checkout.app_files(on_branch)
    other = f"p_{uuid.uuid4().hex[:10]}"
    # a genuinely new app (nothing built here before): an empty repository is where it starts
    with eng.begin() as c:
        assert build_checkout.problem(c, empty, project_id=other, owner=OWNER, repo=REPO,
                                      run_id="crun_new", base_branch="main") is None
    # not a git checkout: nothing to prove
    assert build_checkout.app_files(tmp_path / "nowhere") is None


def test_the_pipeline_checks_the_checkout_after_the_clone_and_before_any_model_call():
    import inspect

    source = inspect.getsource(governed._run_governed_code_task)
    guard = source.index("_build_checkout.problem(")
    assert source.index("sandbox.provision(owner, repo") < guard
    assert guard < source.index("stop_if_the_gate_needs_a_person(_gate_plan0)")
    assert guard < source.index("_provision_build_services(")
    assert 'reason_code="app_missing"' in source


def test_a_clone_failure_fails_the_provision_with_the_error_never_a_scratch_build(tmp_path):
    """The sandbox's provision raises when the clone fails; the pipeline lets it propagate (no
    broad except around it for a local repository) so the run fails with git's own words."""
    import inspect

    from boss_engine.services.sandbox import EphemeralSandbox

    source = inspect.getsource(EphemeralSandbox._provision)
    assert "git init" not in source and "init" not in source.split("clone", 1)[0].split("def ")[-1]
    pipeline = inspect.getsource(governed._run_governed_code_task)
    clone_block = pipeline[pipeline.index("sandbox.provision(owner, repo"):][:900]
    assert "raise" in clone_block      # re-raised unless GitHub is down (an outage only)


# ── B. the gate recorded is the stack's real command, and a command that cannot run is not ok ─

@pytest.fixture
def go_on(tmp_path, monkeypatch):
    from boss_engine.services import toolchains

    manifest = tmp_path / "toolchains.json"
    manifest.write_text(json.dumps({"go": {"go": "1.27.4"}}))
    monkeypatch.setenv(toolchains.MANIFEST_ENV, str(manifest))


def test_a_go_repo_is_gated_by_go_commands_never_the_npm_default(tmp_path, go_on):
    bare = _bare_with_app_on_a_branch(tmp_path)
    tree = _clone(bare, f"boss/code/{LINEAGE}", tmp_path / "go")
    plan = vt.resolve_gate_plan({"owner": OWNER, "repo": REPO, "instructions": "run gwlint"},
                                "Validate", tree)
    assert plan.commands and all(c.startswith("go ") for c in plan.commands), plan.commands
    assert not any("npm" in c for c in plan.commands)


def test_an_npm_command_with_no_package_json_where_it_ran_is_never_ok(tmp_path):
    (tmp_path / "go.mod").write_text("module x\n")
    assert governed._command_cannot_run("npm install && npm run build", tmp_path, ".") is True
    (tmp_path / "package.json").write_text("{}")
    assert governed._command_cannot_run("npm install && npm run build", tmp_path, ".") is False
    assert governed._command_cannot_run("go vet ./... && go test ./...", tmp_path / "none", ".") is False


def test_the_recorded_command_is_the_last_verify_not_the_plan_made_on_the_empty_tree():
    import inspect

    source = inspect.getsource(governed._run_governed_code_task)
    assert '"command": last_verify_command or test_cmd' in source
    assert "last_verify_command = (plan.description if plan.runs_steps" in source


# ── C. build -> config_deploy ships THE BUILD'S branch and SHA ───────────────────────────────

@pytest.fixture()
def built(eng, project, tmp_path, monkeypatch):  # noqa: F811
    root = tmp_path / "repos"
    monkeypatch.setenv("BOSS_LOCAL_REPO_ROOT", str(root))
    bare = local_repo.init_bare(root / project / f"{REPO}.git")
    local_repo.commit_files(bare, "main", {"tenants/lumen-pay.yaml": "tenant: lumen-pay\nversion: 0\n"},
                            "default branch")
    branch = f"boss/code/{LINEAGE}"
    local_repo.ensure_branch(bare, branch, "main")
    local_repo.commit_files(bare, branch, {"tenants/lumen-pay.yaml": "tenant: lumen-pay\nversion: 3\n"},
                            "the build's change")
    owner = f"local_{project}"
    with eng.begin() as c:
        c.execute(text(
            "INSERT INTO code_task_runs (id, project_id, status, title, spec, state, "
            "pull_request_url) VALUES (:i, :p, 'completed', 't', CAST(:s AS JSONB), "
            "'{}'::jsonb, 'http://boss-local/pull/3')"),
            {"i": LINEAGE, "p": project, "s": json.dumps({"owner": owner, "repo": REPO})})
    yield {"sha": local_repo.branch_sha(bare, branch), "main_sha": local_repo.branch_sha(bare, "main")}
    with eng.begin() as c:
        c.execute(text("DELETE FROM code_task_runs WHERE id = :i"), {"i": LINEAGE})


def test_a_config_deploy_after_a_build_deploys_the_builds_branch_and_sha(
        eng, project, gateway, dirs, built):  # noqa: F811
    settings = _fresh_settings()
    gateway["state"].update(healthy=True)
    with eng.begin() as c:
        _target(c, settings, project, "staging", dirs["staging"], gateway)
        set_approval_policy(c, project_id=project, environment="staging", requires_approval=False,
                            actor="usr_owner")
    planned = config_deploy.deploy_files(
        eng, settings, project_id=project, actor=PERSON, app_name="Northgate Gateway",
        environment="staging", code_task_run_id=LINEAGE, source_paths=["tenants/"])
    assert planned["status"] == "planned", planned
    assert planned["source"]["branch"] == f"boss/code/{LINEAGE}"
    assert planned["source"]["head_sha"] == built["sha"] != built["main_sha"]
    assert planned["source"]["pull_request_url"] == "http://boss-local/pull/3"
    assert _apply(eng, settings, project, planned["deployment_run_id"])["status"] == "succeeded"
    # the BUILD's file, not the default branch's
    assert (dirs["staging"] / "lumen-pay.yaml").read_text() == "tenant: lumen-pay\nversion: 3\n"
    with eng.begin() as c:
        packet = c.execute(text("SELECT release_packet FROM deployment_runs WHERE id = :i"),
                           {"i": planned["deployment_run_id"]}).scalar_one()
    assert packet["source"]["head_sha"] == built["sha"]


def test_a_config_deploy_from_a_build_needs_the_paths_and_a_finished_build(
        eng, project, gateway, dirs, built):  # noqa: F811
    settings = _fresh_settings()
    with eng.begin() as c:
        _target(c, settings, project, "staging", dirs["staging"], gateway)
    no_paths = config_deploy.deploy_files(eng, settings, project_id=project, actor=PERSON,
                                          app_name="Northgate Gateway", environment="staging",
                                          code_task_run_id=LINEAGE)
    assert no_paths["status"] == "failed" and "source_paths" in no_paths["reason"]
    with eng.begin() as c:
        c.execute(text("UPDATE code_task_runs SET status = 'failed' WHERE id = :i"), {"i": LINEAGE})
    failed = config_deploy.deploy_files(eng, settings, project_id=project, actor=PERSON,
                                        app_name="Northgate Gateway", environment="staging",
                                        code_task_run_id=LINEAGE, source_paths=["tenants/"])
    assert failed["status"] == "failed" and "not a successful build" in failed["reason"]


def test_draft_workflow_accepts_the_builds_run_id_on_a_config_deploy_step():
    from boss_engine.services import workflow_capabilities as caps

    step = {"id": "staging", "kind": "capability", "binding": {
        "capability": "config_deploy",
        "inputs": {"code_task_run_id": {"from_step": "build", "field": "code_task_run_id"},
                   "source_paths": ["tenants/"]}}}
    problems = caps.validate_step_fields(step, None)
    assert not [p for p in problems if "does not take" in p[1]], problems


def test_a_config_deploy_step_after_a_build_takes_the_builds_run_by_itself():
    from boss_engine.services import workflow_capability_runner as runner

    source = __import__("inspect").getsource(runner._perform_capability)
    assert 'key == "config_deploy" and inputs.get("source_paths")' in source
    assert "_build_run_id(context)" in source[source.index('key == "config_deploy"'):][:700]


# ── D. contract_test: header secret references resolve at run time and are never logged ──────

def test_a_header_secret_ref_resolves_at_run_time_and_is_never_in_the_result(caplog):
    import http.server
    import threading

    from boss_engine.services import contract_tests as ct

    seen = {}

    class H(http.server.BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            seen["auth"] = self.headers.get("Authorization")
            self.send_response(200 if seen["auth"] == "Bearer s3cr3t-token-value" else 401)
            self.send_header("Content-Length", "2")
            self.end_headers()
            self.wfile.write(b"ok")

        def log_message(self, *a):
            pass

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        base = f"http://127.0.0.1:{server.server_address[1]}"
        spec_cases = [{"name": "authenticated", "method": "GET", "path": "/v1/accounts",
                       "headers": {"Authorization": {"secret_ref": "sref_x", "prefix": "Bearer "}},
                       "expect_status": 200}]
        before = json.dumps(spec_cases)
        asked = []

        def resolver(ref):
            asked.append(ref)
            return "s3cr3t-token-value"

        cases, shared, error = ct.resolve_header_secrets(spec_cases, None, resolver)
        assert error is None and asked == ["sref_x"]
        assert json.dumps(spec_cases) == before            # the spec still holds only the reference
        with caplog.at_level("DEBUG"):
            out = ct.run_cases(cases, base_url=base, allowed_private=frozenset({"127.0.0.1"}))
        assert out["ok"] and out["passed"] == 1 and seen["auth"] == "Bearer s3cr3t-token-value"
        assert "s3cr3t-token-value" not in json.dumps(out)
        assert "s3cr3t-token-value" not in caplog.text
        # an unresolvable reference names the header and the ref, never a value
        _c, _h, bad = ct.resolve_header_secrets(
            spec_cases, None, lambda r: (_ for _ in ()).throw(RuntimeError("nope s3cr3t")))
        assert bad and "sref_x" in bad and "Authorization" in bad and "s3cr3t" not in bad
    finally:
        server.shutdown()


def test_the_contract_test_tool_resolves_through_the_secret_store(eng, project, monkeypatch):  # noqa: F811
    from boss_engine.services import secret_backends
    from boss_engine.tools.builtin import build_default_registry

    tools = {s.name: s for s in build_default_registry().specs()}
    seen = []
    monkeypatch.setattr("boss_engine.tools.builtin._community_restricted", lambda settings: False)
    monkeypatch.setattr(secret_backends, "resolve_secret_ref",
                        lambda db, ref, settings, pid=None, **kw: seen.append((ref, pid)) or "tok")

    class Ctx:
        settings = Settings()
        engine = eng

        def begin(self):
            return eng.begin()

    out = tools["contract_test"].handler(
        Ctx(), project_id=project, actor=PERSON,
        args={"cases": [{"name": "c", "url": "http://127.0.0.1:9/x",
                         "headers": {"Authorization": {"secret_ref": "sref_9", "prefix": "Bearer "}}}]})
    assert seen == [("sref_9", project)]
    assert out["ok"] is True and "tok" not in json.dumps(out)
