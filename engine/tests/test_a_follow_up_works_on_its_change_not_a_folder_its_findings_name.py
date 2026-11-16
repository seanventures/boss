"""A follow-up on a change branch works on that branch's change, never on a folder its findings
name (BOSSAPP-1307, AR-17).

THE INCIDENT (northgate-gateway, local repository, 2026-11-16). PR #2 (`boss/code/crun_24f39976`)
opened with checks failing, and BOSS started the follow-up crun_8abe60c6 on that branch. The
clone held the whole Go module, yet `go vet` printed "no packages to vet", the tests failed and
all 30 must-haves read "missing (not present)": 53 minutes and $6.34 on nothing. Its state says
why: `verify_cwd = "schema"`. A follow-up's instructions are its FINDINGS, and one of them read
"there is no `schema/` directory at all" — which `brief_app_dir` took for the brief naming the
app's folder. Every gate, the test budget and the acceptance check then looked inside `schema/`.
The follow-up also started under a new app id (its title named a different "app"), so nothing
the app had recorded applied to it.

Fixed as a class: (1) a follow-up's findings are never read as its brief (app_scope); (2) the
checks-failing / deferred follow-up inherits its parent's app id; (3) before any agent runs, a
continuation on its change branch whose workspace — or app folder — holds none of the change's
files stops, in words: "the workspace has none of the change's files".
"""
from __future__ import annotations

import json
import os
import subprocess
import uuid
from pathlib import Path

import pytest
from sqlalchemy import text

from boss_engine.services import governed_code_task_service as governed
from boss_engine.services import local_repo
from boss_engine.services import verification_target as vt

from .test_identity_agents_workflow_consent import env  # noqa: F401  (full-schema fixture)

# The real finding from crun_9a9a535cd72e486e9c17's consistency check, as it reached the
# follow-up's instructions.
FINDINGS = (
    "The parent change's pull request opened with these checks still FAILING after its repair "
    "rounds (Review passed, Consistent, Must-haves met), so it is a draft and is not merged until "
    "they pass or a person decides. Make every failing check pass and fix every open finding "
    "below on top of the shipped branch, security first.\n\n"
    "FAILING CHECKS AND OPEN FINDINGS (from run crun_9a9a535cd72e486e9c17):\n"
    "- [FINDING] (consistency_check) 3. **Agreed-spec change list vs. delivered files**: the "
    "spec's \"Change list\" explicitly promises `schema/tenant.schema.json` and PRD-mandated "
    "domain metrics — none exist in the change (`grep` across all `.go` files finds zero "
    "occurrences of any of these metric names, and there is no `schema/` directory at all).\n\n"
    "Fix ONLY these. Do not refactor beyond them and do not weaken existing tests or checks.")
FOLLOWUP = {"deferred_from_run": "crun_parent", "is_followup": True,
            "root_run_id": "crun_root", "parent_run_id": "crun_parent"}
GO_FILES = {
    "go.mod": "module northgate\n\ngo 1.22\n",
    "cmd/northgate-gateway/main.go": "package main\n\nfunc main() {}\n",
    "internal/config/watcher.go": "package config\n",
}


# ── 1. The findings are not the brief ──────────────────────────────────────────────────────────

def test_the_premise_a_finding_reads_like_a_brief_naming_a_folder():
    assert vt.brief_app_dir(FINDINGS) == "schema"


def test_a_follow_ups_findings_never_name_its_app_folder():
    assert vt.app_scope({"instructions": FINDINGS}, FOLLOWUP) is None
    # A retry of a findings follow-up carries the mark (code_task_service.retry_code_task).
    assert vt.app_scope({"instructions": FINDINGS},
                        {"is_followup": True, "parent_run_id": "crun_x",
                         "instructions_are_findings": True}) is None
    # A pinned app is named by BOSS, not by the text: it still wins on a follow-up.
    assert vt.app_scope({"instructions": FINDINGS, "app_root": "bench-app"}, FOLLOWUP) == "bench-app"


def test_an_operators_follow_up_is_a_brief_and_keeps_the_folder_it_names():
    """start_followup_task: a person asking for a change writes a brief, not findings."""
    ask = "Add an admin dashboard in the `apps/admin/` folder."
    operator = {"root_run_id": "crun_a", "parent_run_id": "crun_a", "is_followup": True,
                "requested_by": "operator", "app_id": "app_x"}
    assert vt.app_scope({"instructions": ask}, operator) == "apps/admin"
    # ...and a retry of it (is_followup carried, no findings mark) still does.
    assert vt.app_scope({"instructions": ask},
                        {"is_followup": True, "parent_run_id": "crun_b"}) == "apps/admin"


def test_the_findings_mark_is_set_by_the_spawner_and_carried_by_every_retry():
    import inspect

    from boss_engine.services import code_task_service as cts

    assert '"instructions_are_findings": True' in inspect.getsource(
        governed._spawn_deferred_findings_task)
    retry = inspect.getsource(cts.retry_code_task)
    assert '"instructions_are_findings": True' in retry
    assert 'fctx_prev.get("deferred_from_run")' in retry


def test_a_brief_that_names_its_folder_still_scopes_a_fresh_build_and_its_retry():
    brief = "Build HelpDesk AI in the `helpdesk-ai/` folder of this repository."
    assert vt.app_scope({"instructions": brief}, {}) == "helpdesk-ai"
    assert vt.app_scope({"instructions": brief},
                        {"parent_run_id": "crun_a", "root_run_id": "crun_a"}) == "helpdesk-ai"


def _go_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    for rel, body in {**GO_FILES, "schema/README.md": "# schemas\n"}.items():
        (repo / rel).parent.mkdir(parents=True, exist_ok=True)
        (repo / rel).write_text(body)
    return repo


#: What start_code_task stamps on a follow-up of a Go lineage (BOSSAPP-1230).
GO_LINEAGE = {"required_stack": [{"language": "go", "from": "lineage"}], "test_commands": []}


@pytest.fixture
def go_on(tmp_path, monkeypatch):
    from boss_engine.services import toolchains

    manifest = tmp_path / "toolchains.json"
    manifest.write_text(json.dumps({"go": {"go": "1.27.4"}}))
    monkeypatch.setenv(toolchains.MANIFEST_ENV, str(manifest))


def test_the_gate_plan_of_a_go_root_follow_up_runs_at_the_repository_root(tmp_path, go_on):
    repo = _go_repo(tmp_path)
    spec = {"instructions": FINDINGS, **GO_LINEAGE}
    before = vt.resolve_gate_plan(spec, "Follow-up: checks failing — KAN-9", repo)
    assert Path(before.cwd).resolve() == (repo / "schema").resolve(), (
        "the premise (crun_8abe60c6's verify_cwd): read as a brief, the findings put the Go "
        "gate in schema/, where `go vet ./...` finds no packages")
    plan = vt.resolve_gate_plan(spec, "Follow-up: checks failing — KAN-9", repo,
                                finalize_ctx=FOLLOWUP)
    assert Path(plan.cwd).resolve() == repo.resolve()
    assert plan.commands == before.commands


# ── 2. The guard: none of the change's files, before anything is spent ─────────────────────────

def test_an_empty_workspace_has_none_of_the_changes_files(tmp_path):
    message = governed.workspace_missing_the_change(
        list(GO_FILES), tmp_path, branch="boss/code/crun_24f399769d9f4f5d8fa1")
    assert message and "the workspace has none of the change's files" in message
    assert "boss/code/crun_24f399769d9f4f5d8fa1" in message
    assert "go.mod" in message


def test_an_app_folder_without_the_change_is_refused_too(tmp_path):
    repo = _go_repo(tmp_path)
    message = governed.workspace_missing_the_change(list(GO_FILES), repo, scope="schema")
    assert message and "the workspace has none of the change's files" in message
    assert "`schema/`" in message


def test_a_workspace_holding_the_change_passes(tmp_path):
    repo = _go_repo(tmp_path)
    assert governed.workspace_missing_the_change(list(GO_FILES), repo) is None
    assert governed.workspace_missing_the_change(
        list(GO_FILES) + ["schema/tenant.schema.json"], repo, scope="cmd") is None
    # Nothing expected (a first attempt): nothing to compare.
    assert governed.workspace_missing_the_change([], tmp_path / "empty") is None


def test_the_pipeline_asks_before_any_agent_runs():
    """The guard sits before the first gate decision and the build services — the first
    places money can be spent — and fails the run with its own reason code."""
    import inspect

    source = inspect.getsource(governed._run_governed_code_task)
    guard = source.index("workspace_missing_the_change(")
    assert guard < source.index("stop_if_the_gate_needs_a_person(_gate_plan0)")
    assert guard < source.index("_provision_build_services(")
    assert 'reason_code="workspace_missing_the_change"' in source


# ── 3. A local repository: the follow-up starts from the change branch's tree ──────────────────

def _git(*args: str, cwd: Path) -> str:
    return subprocess.run(["git", *args], cwd=str(cwd), check=True, capture_output=True,
                          text=True).stdout


def _local_repo_with_a_change(tmp_path: Path) -> tuple[Path, str]:
    bare = local_repo.init_bare(tmp_path / "northgate-gateway.git")
    local_repo.commit_files(bare, "main", {"README.md": "# northgate\n"}, "init")
    change = "boss/code/crun_24f399769d9f4f5d8fa1"
    local_repo.ensure_branch(bare, change, "main")
    local_repo.commit_files(bare, change, dict(GO_FILES), "the change")
    return bare, change


def _sandbox_clone(bare: Path, branch: str, into: Path) -> Path:
    """The sandbox's own clone of a local repository (sandbox.EphemeralSandbox.provision):
    `git clone --depth 1 --branch <base> file://<bare> .`."""
    into.mkdir(parents=True)
    _git("clone", "--depth", "1", "--branch", branch, local_repo.file_url(bare), ".", cwd=into)
    return into


def test_a_follow_up_on_a_local_change_branch_starts_from_that_branchs_tree(tmp_path, go_on):
    bare, change = _local_repo_with_a_change(tmp_path)
    tree = _sandbox_clone(bare, change, tmp_path / "followup")
    assert all((tree / p).is_file() for p in GO_FILES)
    assert governed.workspace_missing_the_change(list(GO_FILES), tree, branch=change) is None
    # And its gates run where the change is: the repository root, not a folder the findings name.
    plan = vt.resolve_gate_plan({"instructions": FINDINGS, **GO_LINEAGE}, "Follow-up", tree,
                                finalize_ctx=FOLLOWUP)
    assert Path(plan.cwd).resolve() == tree.resolve()


def test_a_clone_without_the_change_stops_before_spending(tmp_path):
    bare, change = _local_repo_with_a_change(tmp_path)
    tree = _sandbox_clone(bare, "main", tmp_path / "wrong")
    message = governed.workspace_missing_the_change(list(GO_FILES), tree, branch=change)
    assert message and "the workspace has none of the change's files" in message


# ── 4. The checks-failing follow-up is the same app ────────────────────────────────────────────

@pytest.fixture
def full_schema(monkeypatch):
    from .test_identity_agents_workflow_consent import URL_ENV

    if os.environ.get(URL_ENV):
        return
    fallback = os.environ.get("BOSS_TEST_DATABASE_URL")
    if fallback:
        monkeypatch.setenv(URL_ENV, fallback)
        return
    message = f"neither {URL_ENV} nor BOSS_TEST_DATABASE_URL is set"
    if os.environ.get("CI"):
        pytest.fail(message)
    pytest.skip(message)


def test_a_checks_failing_follow_up_keeps_its_parents_app_id(full_schema, request):
    from boss_engine.config import get_settings
    from boss_engine.identity import lineage

    from .test_a_resumed_build_runs_as_the_person import _Jobs
    from .test_an_automatic_retry_runs_under_the_persons_grant_or_not_at_all import WORKER_KEY

    env = request.getfixturevalue("env")  # noqa: F811
    parent = f"crun_{uuid.uuid4().hex[:12]}"
    with env.engine.begin() as c:
        c.execute(text(
            "INSERT INTO code_task_runs (id, project_id, task_id, status, phase, title, spec, "
            "state, finalize_ctx) VALUES (:id, :p, NULL, 'completed', 'done', :t, "
            "CAST(:spec AS JSONB), '{}'::jsonb, CAST(:fc AS JSONB))"),
            {"id": parent, "p": env.project,
             "t": "KAN-9 Northgate API Gateway + gwlint · retry 1",
             "spec": json.dumps({"owner": "local_northgate", "repo": "northgate-gateway",
                                 "instructions": "Build the gateway."}),
             "fc": json.dumps({"app_id": "app_115e2fc26700a93031af", "root_run_id": parent})})
    with lineage.acting(WORKER_KEY):
        new = governed._spawn_deferred_findings_task(
            env.engine, get_settings(), _Jobs(), project_id=env.project, task_id=None,
            spec={"owner": "local_northgate", "repo": "northgate-gateway"},
            base_branch=f"boss/code/{parent}",
            findings=[{"severity": "blocker", "text": "Failing gate 'consistent'"}],
            parent_run_id=parent, parent_title="KAN-9 Northgate API Gateway + gwlint · retry 1",
            parent_root=parent, checks_failing={"gates": [{"gate": "consistent", "label": "Consistent"}]})
    assert new
    with env.engine.begin() as c:
        row = c.execute(text("SELECT title, finalize_ctx FROM code_task_runs WHERE id=:i"),
                        {"i": new}).mappings().one()
    assert row["title"].startswith("Follow-up: checks failing")
    assert row["finalize_ctx"]["app_id"] == "app_115e2fc26700a93031af"
    assert row["finalize_ctx"]["is_followup"] is True
    assert row["finalize_ctx"]["instructions_are_findings"] is True
