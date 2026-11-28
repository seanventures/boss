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
        why = build_checkout.problem(c, tree, owner=OWNER, repo=REPO, run_id="crun_new",
                                     base_branch="main", root_run="crun_new")
    assert why and f"boss/code/{LINEAGE}" in why and "second app from scratch" in why


def test_a_clone_of_the_apps_branch_or_a_first_build_is_never_refused(eng, project, tmp_path):  # noqa: F811
    bare = _bare_with_app_on_a_branch(tmp_path)
    on_branch = _clone(bare, f"boss/code/{LINEAGE}", tmp_path / "on-branch")
    empty = _clone(bare, "main", tmp_path / "empty")
    with eng.begin() as c:
        # the app's own branch holds the app: builds on its content
        _earlier_build(c, project)
        assert build_checkout.problem(c, on_branch, owner=OWNER, repo=REPO, run_id="crun_new",
                                      base_branch="x") is None
        assert "cmd/gw/main.go" in build_checkout.app_files(on_branch)
    # a genuinely new app (nothing built in THIS repository before): an empty repository is where it starts
    with eng.begin() as c:
        assert build_checkout.problem(c, empty, owner=OWNER, repo="another-repo",
                                      run_id="crun_new", base_branch="main") is None
        # the earlier build's branch is gone, or it never completed: it is not an app to build on
        assert build_checkout.problem(c, empty, owner=OWNER, repo=REPO, run_id="crun_new",
                                      base_branch="main", branch_exists=lambda b: False) is None
        c.execute(text("UPDATE code_task_runs SET status = 'failed' WHERE id = :i"), {"i": LINEAGE})
        assert build_checkout.problem(c, empty, owner=OWNER, repo=REPO, run_id="crun_new",
                                      base_branch="main") is None
        c.execute(text("UPDATE code_task_runs SET status = 'completed' WHERE id = :i"), {"i": LINEAGE})
        # a retry of the refused run (same repository, its own lineage) is still refused, and
        # it is refused wherever it runs: the repository is the key, not the project
        assert build_checkout.problem(c, empty, owner=OWNER, repo=REPO, run_id="crun_retry",
                                      root_run="crun_new", base_branch="main")
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
    # a retry or a fresh_build retry is checked too: only a continuation (_is_repair) is exempt
    assert "not use_microvm and not _is_repair and not fctx.get(\"seed_workspace_from\")" in source
    assert "fresh_build\") and root_run" not in source


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
    (tmp_path / "package.json").unlink()
    # a micro-VM's tree is elsewhere; a command naming its own directory runs there
    assert governed._command_cannot_run("npm install", tmp_path, ".", use_microvm=True) is False
    assert governed._command_cannot_run("npm --prefix app test", tmp_path, ".") is False


def test_the_recorded_command_is_the_last_verify_not_the_plan_made_on_the_empty_tree():
    import inspect

    source = inspect.getsource(governed._run_governed_code_task)
    assert '"command": last_verify_command or test_cmd' in source
    assert "last_verify_command = (plan.description if plan.runs_steps" in source


# ── C. build -> config_deploy ships THE BUILD'S commit ───────────────────────────────────────

@pytest.fixture()
def built(eng, project, tmp_path, monkeypatch):  # noqa: F811
    root = tmp_path / "repos"
    monkeypatch.setenv("BOSS_LOCAL_REPO_ROOT", str(root))
    bare = local_repo.init_bare(root / project / f"{REPO}.git")
    local_repo.commit_files(bare, "main", {"tenants/lumen-pay.yaml": "tenant: lumen-pay\nversion: 0\n"},
                            "default branch")
    branch = f"boss/code/{LINEAGE}"
    local_repo.ensure_branch(bare, branch, "main")
    local_repo.commit_files(bare, branch, {"tenants/lumen-pay.yaml": "tenant: lumen-pay\nversion: 3\n",
                                           "tenants/art_note.yaml": "art_looks_like_an_artifact_id\n"},
                            "the build's change")
    shipped = local_repo.branch_sha(bare, branch)
    owner = f"local_{project}"
    with eng.begin() as c:
        c.execute(text(
            "INSERT INTO code_task_runs (id, project_id, status, title, spec, state, finalize_ctx, "
            "pull_request_url, pull_request_number) VALUES (:i, :p, 'completed', 't', "
            "CAST(:s AS JSONB), '{}'::jsonb, CAST(:f AS JSONB), 'http://boss-local/pull/3', 3)"),
            {"i": LINEAGE, "p": project, "s": json.dumps({"owner": owner, "repo": REPO}),
             "f": json.dumps({"final_head_sha": shipped})})
    # A LATER RUN MOVES THE LINEAGE BRANCH: the build's shipped commit is not its tip any more
    local_repo.commit_files(bare, branch, {"tenants/lumen-pay.yaml": "tenant: lumen-pay\nversion: 99\n"},
                            "a later run's change")
    yield {"sha": shipped, "tip": local_repo.branch_sha(bare, branch),
           "main_sha": local_repo.branch_sha(bare, "main")}
    with eng.begin() as c:
        c.execute(text("DELETE FROM code_task_runs WHERE id = :i"), {"i": LINEAGE})


def _staging(eng, settings, project, dirs, gateway, *, approval_free=True):  # noqa: F811
    gateway["state"].update(healthy=True)
    with eng.begin() as c:
        _target(c, settings, project, "staging", dirs["staging"], gateway)
        if approval_free:
            set_approval_policy(c, project_id=project, environment="staging",
                                requires_approval=False, actor="usr_owner")


def test_a_config_deploy_after_a_build_deploys_the_commit_that_run_shipped(
        eng, project, gateway, dirs, built):  # noqa: F811
    settings = _fresh_settings()
    _staging(eng, settings, project, dirs, gateway)
    planned = config_deploy.deploy_files(
        eng, settings, project_id=project, actor=PERSON, app_name="Northgate Gateway",
        environment="staging", code_task_run_id=LINEAGE, source_paths=["tenants/"])
    assert planned["status"] == "planned", planned
    assert planned["source"]["branch"] == f"boss/code/{LINEAGE}"
    assert planned["source"]["head_sha"] == built["sha"] != built["tip"]
    assert planned["source"]["pull_request_url"] == "http://boss-local/pull/3"
    assert _apply(eng, settings, project, planned["deployment_run_id"])["status"] == "succeeded"
    # THE RUN'S commit: not the default branch's (0), not the moved tip (99)
    assert (dirs["staging"] / "lumen-pay.yaml").read_text() == "tenant: lumen-pay\nversion: 3\n"
    # a file whose text looks like an artifact id is shipped as text
    assert (dirs["staging"] / "art_note.yaml").read_text() == "art_looks_like_an_artifact_id\n"
    with eng.begin() as c:
        packet = c.execute(text("SELECT release_packet FROM deployment_runs WHERE id = :i"),
                           {"i": planned["deployment_run_id"]}).scalar_one()
    assert packet["build"]["head_sha"] == built["sha"]


def test_the_approval_card_shows_the_build_its_commit_and_pr(eng, project, gateway, dirs, built):  # noqa: F811
    settings = _fresh_settings()
    _staging(eng, settings, project, dirs, gateway, approval_free=False)
    planned = config_deploy.deploy_files(
        eng, settings, project_id=project, actor=PERSON, app_name="Northgate Gateway",
        environment="staging", code_task_run_id=LINEAGE, source_paths=["tenants/lumen-pay.yaml"])
    assert planned["status"] == "planned"
    with eng.begin() as c:
        question, rec = c.execute(text(
            "SELECT d.question, d.recommendation FROM decisions d JOIN deployment_runs r "
            "ON r.decision_id = d.id WHERE r.id = :i"), {"i": planned["deployment_run_id"]}).one()
    assert LINEAGE in question and built["sha"][:12] in question and "pull/3" in question
    rec = rec if isinstance(rec, dict) else json.loads(rec)
    assert rec["build"]["head_sha"] == built["sha"]


def test_a_build_with_no_recorded_commit_or_unfinished_is_refused(
        eng, project, gateway, dirs, built):  # noqa: F811
    settings = _fresh_settings()
    _staging(eng, settings, project, dirs, gateway)

    def deploy(**kw):
        return config_deploy.deploy_files(eng, settings, project_id=project, actor=PERSON,
                                          app_name="Northgate Gateway", environment="staging",
                                          code_task_run_id=LINEAGE, **kw)

    assert "source_paths" in deploy()["reason"]
    with eng.begin() as c:
        c.execute(text("UPDATE code_task_runs SET finalize_ctx = '{}'::jsonb WHERE id = :i"), {"i": LINEAGE})
    assert "no recorded commit" in deploy(source_paths=["tenants/"])["reason"]
    with eng.begin() as c:
        c.execute(text("UPDATE code_task_runs SET status = 'failed' WHERE id = :i"), {"i": LINEAGE})
    assert "not a successful build" in deploy(source_paths=["tenants/"])["reason"]


def test_source_paths_are_confined_to_the_repository(eng, project, gateway, dirs, built):  # noqa: F811
    settings = _fresh_settings()
    _staging(eng, settings, project, dirs, gateway)
    for bad in (["../../etc/passwd"], ["/etc/passwd"], {"x.yaml": "a/../../b"}, {"../x": "tenants/lumen-pay.yaml"}):
        out = config_deploy.deploy_files(eng, settings, project_id=project, actor=PERSON,
                                         app_name="Northgate Gateway", environment="staging",
                                         code_task_run_id=LINEAGE, source_paths=bad)
        assert out["status"] == "failed", (bad, out)
    missing = config_deploy.deploy_files(eng, settings, project_id=project, actor=PERSON,
                                         app_name="Northgate Gateway", environment="staging",
                                         code_task_run_id=LINEAGE, source_paths=["nope.yaml"])
    assert "not in commit" in missing["reason"]


def test_a_build_with_failing_or_advisory_gates_needs_a_person_for_staging_and_never_ships_to_production(
        eng, project, gateway, dirs, built):  # noqa: F811
    settings = _fresh_settings()
    _staging(eng, settings, project, dirs, gateway)         # staging's policy: no approval asked
    with eng.begin() as c:
        c.execute(text("UPDATE code_task_runs SET finalize_ctx = finalize_ctx || "
                       "'{\"advisory_failing_gates\": [\"code_review_passed\"]}'::jsonb WHERE id = :i"),
                  {"i": LINEAGE})
        production = _target(c, settings, project, "production", dirs["production"], gateway)
    staged = config_deploy.deploy_files(
        eng, settings, project_id=project, actor=PERSON, app_name="Northgate Gateway",
        environment="staging", code_task_run_id=LINEAGE, source_paths=["tenants/lumen-pay.yaml"])
    assert staged["status"] == "planned" and "needs_a_person" in staged["source"]
    assert "code_review_passed" in staged["source"]["needs_a_person"]
    assert staged["approval"] is None                      # the policy did not answer for a person
    prod = config_deploy.deploy_files(
        eng, settings, project_id=project, actor=PERSON, app_name="Northgate Gateway",
        environment="production", code_task_run_id=LINEAGE, source_paths=["tenants/lumen-pay.yaml"])
    assert prod["status"] == "failed" and "production" in prod["reason"] and "code_review_passed" in prod["reason"]


def test_the_github_contents_call_quotes_the_path_and_ref():
    import inspect

    from boss_engine.services.github_adapter import GitHubAdapter

    source = inspect.getsource(GitHubAdapter.read_text_file)
    assert "quote(path, safe='/')" in source and "quote(branch_name, safe='')" in source


def test_the_nearest_preceding_build_step_wins(eng, project, monkeypatch):  # noqa: F811
    from boss_engine.services import workflow_capability_runner as runner

    spec = {"steps": [{"id": "build_a"}, {"id": "build_b"}, {"id": "staging"}]}
    monkeypatch.setattr(runner.workflows, "_run_spec", lambda c, rid: spec)
    context = {"run_id": "wfr_x", "steps": {"build_a": {"code_task_run_id": "crun_a"},
                                            "build_b": {"code_task_run_id": "crun_b"}}}
    with eng.begin() as c:
        assert runner._nearest_build_run_id(c, context, {"id": "staging"}) == "crun_b"
        assert runner._nearest_build_run_id(c, context, {"id": "build_b"}) == "crun_a"


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
    branch = 'key == "config_deploy" and inputs.get("source_paths")'
    assert branch in source
    assert "_nearest_build_run_id(connection, context, step)" in source[source.index(branch):][:700]


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

        cases, shared, error, names = ct.resolve_header_secrets(spec_cases, None, resolver)
        assert error is None and asked == ["sref_x"] and names == frozenset({"authorization"})
        assert json.dumps(spec_cases) == before            # the spec still holds only the reference
        with caplog.at_level("DEBUG"):
            out = ct.run_cases(cases, base_url=base, allowed_private=frozenset({"127.0.0.1"}))
        assert out["ok"] and out["passed"] == 1 and seen["auth"] == "Bearer s3cr3t-token-value"
        assert "s3cr3t-token-value" not in json.dumps(out)
        assert "s3cr3t-token-value" not in caplog.text
        # an unresolvable reference names the header and the ref, never a value
        _c, _h, bad, _n = ct.resolve_header_secrets(
            spec_cases, None, lambda r: (_ for _ in ()).throw(RuntimeError("nope s3cr3t")))
        assert bad and "sref_x" in bad and "Authorization" in bad and "s3cr3t" not in bad
    finally:
        server.shutdown()


def _ref(eng, project, ref_id, *, http_auth=True, kind="api_token"):  # noqa: F811
    with eng.begin() as c:
        c.execute(text(
            "INSERT INTO secret_refs (id, project_id, scope_id, ref_key, secret_kind, created_by, "
            "metadata) VALUES (:i, :p, :p, :k, :kind, 'test', CAST(:m AS JSONB))"),
            {"i": ref_id, "p": project, "k": "X", "kind": kind,
             "m": json.dumps({"http_auth": True} if http_auth else {})})


def _tool(eng, monkeypatch):  # noqa: F811
    from boss_engine.tools.builtin import build_default_registry

    _ = monkeypatch

    class Ctx:
        settings = Settings()
        engine = eng

        def begin(self):
            return eng.begin()

    return {s.name: s for s in build_default_registry().specs()}["contract_test"].handler, Ctx()


def test_the_contract_test_tool_resolves_only_http_auth_refs_through_the_store(eng, project, monkeypatch):  # noqa: F811
    from boss_engine.services import secret_backends

    handler, ctx = _tool(eng, monkeypatch)
    seen = []
    monkeypatch.setattr(secret_backends, "resolve_secret_ref",
                        lambda db, ref, settings, pid=None, **kw: seen.append((ref, pid)) or "tok")
    _ref(eng, project, "sref_ok")
    try:
        out = handler(ctx, project_id=project, actor=PERSON, args={"cases": [{
            "name": "c", "url": "http://127.0.0.1:9/x",
            "headers": {"Authorization": {"secret_ref": "sref_ok", "prefix": "Bearer "}}}]})
        assert seen == [("sref_ok", project)]
        assert out["ok"] is True and "tok" not in json.dumps(out)
    finally:
        with eng.begin() as c:
            c.execute(text("DELETE FROM secret_refs WHERE project_id = :p"), {"p": project})


def test_a_ref_not_marked_for_http_auth_is_refused_and_the_refusal_is_audited(eng, project, monkeypatch):  # noqa: F811
    from boss_engine.services import secret_backends

    handler, ctx = _tool(eng, monkeypatch)
    monkeypatch.setattr(secret_backends, "resolve_secret_ref",
                        lambda *a, **k: pytest.fail("a provider's key must never be resolved here"))
    llm = f"sref_llm_{uuid.uuid4().hex[:8]}"
    _ref(eng, project, llm, http_auth=False)
    try:
        out = handler(ctx, project_id=project, actor=PERSON, args={"cases": [{
            "name": "c", "url": "http://127.0.0.1:9/x",
            "headers": {"Authorization": {"secret_ref": llm}}}]})
        assert out["ok"] is False and "not marked for HTTP auth" in out["error"]
        with eng.begin() as c:
            rows = c.execute(text("SELECT outcome FROM secret_access_events WHERE secret_ref_id = :r"), {"r": llm}).all()
        assert rows and rows[0][0] == "refused"      # written outside the rolled-back transaction
    finally:
        with eng.begin() as c:
            c.execute(text("DELETE FROM secret_refs WHERE project_id = :p"), {"p": project})


def test_a_secret_header_is_not_sent_to_another_origin_on_a_redirect():
    import http.server
    import threading

    from boss_engine.services import contract_tests as ct

    seen = {}

    class Other(http.server.BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            seen["other"] = dict(self.headers)
            self.send_response(200)
            self.send_header("Content-Length", "0")
            self.end_headers()

        def log_message(self, *a):
            pass

    other = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Other)

    class First(http.server.BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            self.send_response(302)
            self.send_header("Location", f"http://localhost:{other.server_address[1]}/x")
            self.send_header("Content-Length", "0")
            self.end_headers()

        def log_message(self, *a):
            pass

    first = http.server.ThreadingHTTPServer(("127.0.0.1", 0), First)
    for srv in (other, first):
        threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        cases = [{"name": "r", "url": f"http://127.0.0.1:{first.server_address[1]}/a",
                  "headers": {"X-Gateway-Key": {"secret_ref": "sref_k"}}, "expect_status": 200}]
        cases, shared, error, names = ct.resolve_header_secrets(cases, None, lambda r: "very-secret")
        out = ct.run_cases(cases, allowed_private=frozenset({"127.0.0.1", "localhost"}),
                           secret_headers=names)
        assert out["passed"] == 1
        assert not any(k.lower() == "x-gateway-key" for k in seen["other"]), seen["other"]
    finally:
        other.shutdown()
        first.shutdown()


def test_a_finished_run_records_the_commit_it_shipped():
    import inspect

    source = inspect.getsource(governed._run_governed_code_task)
    assert "'final_head_sha'" in source and "_final_head = str(" in source
    assert source.index("'final_head_sha'") < source.index('"phase=\'done\', pull_request_url=:u')


def test_an_earlier_approved_gate_never_pre_approves_a_build_with_failing_gates(
        eng, project, gateway, dirs, built):  # noqa: F811
    """A REAL released gate, no stubs: a config_deploy step's gate, approved by a person on THIS
    workflow run, is exactly the gate that pre-approves this deploy when the build is clean
    (control). Once the build has failing gates, neither that gate named as an argument nor the
    runner's own trusted `released_gate` pre-approves it: a person is asked, and the gate is
    not spent."""
    from boss_engine.services import deploy_policy

    from .test_workflow_capability_runner import _publish as publish_steps, _start as start_run

    settings = _fresh_settings()
    _staging(eng, settings, project, dirs, gateway, approval_free=False)
    earlier = f"dec_gate_{uuid.uuid4().hex[:10]}"
    with eng.begin() as c:
        _, version = publish_steps(c, project, f"gate_{uuid.uuid4().hex[:6]}", [
            {"id": "ship", "kind": "capability", "next": [],
             "binding": {"capability": "config_deploy", "inputs": {}}}], "ship")
        run = start_run(c, project, version["id"], {})
        c.execute(text("INSERT INTO decisions (id, project_id, question, status, approved_by) "
                       "VALUES (:d, :p, 'Ship config?', 'approved', :a)"),
                  {"d": earlier, "p": project, "a": PERSON})
        c.execute(text("UPDATE workflow_step_runs SET decision_id = :d, binding_snapshot = "
                       "COALESCE(binding_snapshot, '{}'::jsonb) || CAST(:s AS JSONB) "
                       "WHERE workflow_run_id = :r"),
                  {"d": earlier, "s": json.dumps({"capability": "config_deploy",
                                                  "released_for": earlier}), "r": run["id"]})
        mine = deploy_policy.released_gate(c, project, earlier, capability="config_deploy",
                                           run_id=run["id"])
    assert mine and mine["approved_by"] == PERSON

    # CONTROL: with a clean build this very gate, as an argument or as the runner's value,
    # would have pre-approved this config deploy (checked in a transaction that is rolled
    # back, so the gate is not claimed).
    with eng.connect() as c:
        for kw in ({"gate_decision_id": earlier}, {"released": mine}):
            with c.begin() as tx:
                ok = deploy_policy.pre_approval(c, project, "staging", gate_capability="config_deploy",
                                                gate_run_id=run["id"], **kw)
                assert ok and ok["source"] == "gate" and ok["decision_id"] == earlier, kw
                tx.rollback()

    with eng.begin() as c:
        c.execute(text("UPDATE code_task_runs SET finalize_ctx = finalize_ctx || "
                       "'{\"failing_gates\": [\"security_clean\"]}'::jsonb WHERE id = :i"), {"i": LINEAGE})
    for kw in ({"approval_decision_id": earlier}, {"released_gate": mine},
               {"approval_decision_id": earlier, "released_gate": mine}):
        asked = config_deploy.deploy_files(
            eng, settings, project_id=project, actor=PERSON, app_name="Northgate Gateway",
            environment="staging", code_task_run_id=LINEAGE, source_paths=["tenants/lumen-pay.yaml"],
            workflow_run_id=run["id"], **kw)
        assert asked["status"] == "planned" and asked["approval"] is None, (kw, asked)
        assert "security_clean" in asked["source"]["needs_a_person"]
        with eng.begin() as c:
            question = c.execute(text("SELECT d.id, d.status FROM decisions d JOIN deployment_runs r "
                                      "ON r.decision_id = d.id WHERE r.id = :i"),
                                 {"i": asked["deployment_run_id"]}).mappings().one()
            # a card is raised for a person, with the reasons — never the earlier gate
            assert question["status"] == "pending" and question["id"] != earlier, kw
            assert c.execute(text("SELECT 1 FROM deployment_gate_uses WHERE decision_id = :d"),
                             {"d": earlier}).first() is None, "the earlier gate was spent"
            c.execute(text("UPDATE deployment_runs SET status = 'cancelled' WHERE id = :i"),
                      {"i": asked["deployment_run_id"]})


def test_the_http_auth_flag_is_set_on_save_and_kept_when_the_value_is_saved_again(eng, project, monkeypatch):  # noqa: F811
    from boss_engine.services import contract_tests as ct
    from boss_engine.services import secrets_service

    settings = Settings()

    class Store:
        def put(self, key, value):
            pass

    monkeypatch.setattr(secrets_service, "backend_for", lambda backend, s: Store())
    name = f"gw_{uuid.uuid4().hex[:6]}"
    try:
        with eng.begin() as c:
            first = secrets_service.put_secret(c, settings, project_id=project, name=name,
                                               value="value-one-1234", backend="database")
            ref = first["secret_ref_id"]
            assert ct.http_auth_problem(c, ref, project)           # not marked: refused
            secrets_service.put_secret(c, settings, project_id=project, name=name,
                                       value="value-one-1234", backend="database", http_auth=True)
            assert ct.http_auth_problem(c, ref, project) is None   # marked: usable as a header
            # the rotated value is saved again WITHOUT the option: the flag stays
            secrets_service.put_secret(c, settings, project_id=project, name=name,
                                       value="rotated-value-5678", backend="database")
            assert ct.http_auth_problem(c, ref, project) is None
            secrets_service.put_secret(c, settings, project_id=project, name=name,
                                       value="rotated-value-5678", backend="database", http_auth=False)
            assert ct.http_auth_problem(c, ref, project)           # explicitly cleared
    finally:
        with eng.begin() as c:
            c.execute(text("DELETE FROM secret_refs WHERE project_id = :p"), {"p": project})
