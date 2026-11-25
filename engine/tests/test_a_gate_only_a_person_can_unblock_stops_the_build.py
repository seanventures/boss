"""A gate only a person can unblock stops the build, says how to unblock it, and stops nothing else.

PR #135 marked a toolchain_missing gate plan as a person's to fix (GatePlan.needs_human): a go.mod
app while Go is off on this install. Nothing read it, so a build that met it ran its agents and
then its repair rounds against a gate no code change can make run. Now:

  * when BOSS knows the app's folder (named, recorded, or a seeded attempt's files) the build
    stops right after the gate plan is resolved — before build services, agents or any repair —
    as needs_human / stack_unsupported (no automatic retry, no partial PR), and the run's error
    is the plan's words: which toolchain, `boss toolchains enable go`, `boss update`;
  * otherwise the starting plan (made at the root with no change to place it) never stops a
    build: a Go-root repo whose task builds the Node app in web/ goes on, and every verify,
    which plans with the run's changed files, stops there instead;
  * a Node / Python app the BASE tree holds is gated as that app even with a go.mod beside it
    (a Hugo site), with Go on or off; a go.mod an agent ADDS to a folder with no such app in
    the base tree still stops the build (no repair flips the stack);
  * an unreadable toolchain manifest is said as that, not as "Go is not turned on".
"""
from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from boss_engine.services import governed_code_task_service as g
from boss_engine.services import toolchains, verification_target as vt

from .test_a_rules_403_opens_an_inbox_decision import _Conn, _decisions

GO_MOD = "module example.com/x\n\ngo 1.27\n"


def _manifest(tmp_path, monkeypatch, data) -> Path:
    manifest = tmp_path / "manifest.json"
    manifest.write_text(data if isinstance(data, str) else json.dumps(data))
    monkeypatch.setenv(toolchains.MANIFEST_ENV, str(manifest))
    monkeypatch.setenv(toolchains.CACHE_ROOT_ENV, str(tmp_path / "cache"))
    return manifest


@pytest.fixture
def go_on(tmp_path, monkeypatch):
    return _manifest(tmp_path, monkeypatch, {"go": {"go": "1.27.4"}})


@pytest.fixture
def go_off(tmp_path, monkeypatch):
    return _manifest(tmp_path, monkeypatch, {})


def _tree(tmp_path: Path, files: dict[str, str]) -> Path:
    repo = tmp_path / "repo"
    for rel, text in files.items():
        (repo / rel).parent.mkdir(parents=True, exist_ok=True)
        (repo / rel).write_text(text)
    return repo


PKG = json.dumps({"name": "web", "scripts": {"test": "vitest"}})
GO_SVC = {"svc/go.mod": GO_MOD, "svc/main.go": "package main\n\nfunc main() {}\n",
          "README.md": "x\n"}
GO_ROOT_WEB = {"go.mod": GO_MOD, "main.go": "package main\n\nfunc main() {}\n",
               "web/package.json": PKG, "web/src/app.ts": "export {}\n"}
HUGO = {"go.mod": GO_MOD, "package.json": PKG, "src/app.ts": "export {}\n"}


def _plan(repo: Path, **kw):
    return vt.resolve_gate_plan({}, "add a route", repo,
                                pinned=kw.pop("pinned", None) or vt.pin_verify_manifest(repo), **kw)


# ---- the plan --------------------------------------------------------------------------------------

def test_a_go_app_with_go_off_stops_with_the_enable_hint(tmp_path, go_off):
    plan = _plan(_tree(tmp_path, GO_SVC), app_id="svc")
    assert plan.needs_human
    with pytest.raises(g.StackUnsupportedError) as stopped:
        g.stop_if_the_gate_needs_a_person(plan)
    words = str(stopped.value)
    assert words.startswith("This project needs Go (it has a go.mod)")
    assert "`boss toolchains enable go`" in words and "`boss update`" in words


def test_with_go_on_it_is_the_go_gate(tmp_path, go_on):
    plan = _plan(_tree(tmp_path, GO_SVC), app_id="svc")
    g.stop_if_the_gate_needs_a_person(plan)  # does not raise
    assert plan.source == "toolchain" and plan.commands == list(toolchains.GO_GATE)


def test_a_node_project_is_unaffected(tmp_path, go_off):
    plan = _plan(_tree(tmp_path, {"package.json": PKG}))
    assert not plan.needs_human and plan.source != "toolchain_missing"


def test_a_go_root_repo_plans_its_node_app_in_web_as_npm(tmp_path, go_off):
    repo = _tree(tmp_path, GO_ROOT_WEB)
    plan = _plan(repo, changed=["web/src/app.ts"])
    assert not plan.needs_human
    assert plan.commands and plan.commands[0].startswith("npm ")
    assert plan.cwd == repo / "web"


@pytest.mark.parametrize("fixture", ["go_off", "go_on"])
def test_a_node_app_with_a_stray_go_mod_is_gated_as_node_either_way(tmp_path, fixture, request):
    request.getfixturevalue(fixture)
    plan = _plan(_tree(tmp_path, HUGO))
    assert not plan.needs_human
    assert plan.commands and plan.commands[0].startswith("npm ")


def test_a_go_mod_an_agent_adds_stops_the_build_even_with_a_package_json(tmp_path, go_off):
    """The fix round's tree: svc/ held nothing in the base tree; an agent wrote a go.mod (and a
    package.json, which would flip the gate to npm if the current tree decided)."""
    repo = _tree(tmp_path, {"README.md": "x\n", "svc/.keep": ""})
    base = vt.pin_verify_manifest(repo)
    (repo / "svc" / "go.mod").write_text(GO_MOD)
    (repo / "svc" / "main.go").write_text("package main\n")
    (repo / "svc" / "package.json").write_text(PKG)
    plan = _plan(repo, pinned=base, changed=["svc/go.mod", "svc/main.go", "svc/package.json"])
    assert plan.needs_human and plan.source == "toolchain_missing"
    with pytest.raises(g.StackUnsupportedError):
        g.stop_if_the_gate_needs_a_person(plan)


@pytest.mark.parametrize("content,why", [(None, "No such file"), ("{not json", "not valid JSON"),
                                         ("[]", "not a JSON object")])
def test_an_unreadable_toolchain_manifest_is_said_as_that(tmp_path, monkeypatch, content, why):
    path = tmp_path / "manifest.json"
    if content is not None:
        path.write_text(content)
    monkeypatch.setenv(toolchains.MANIFEST_ENV, str(path))
    plan = _plan(_tree(tmp_path, GO_SVC), app_id="svc")
    assert plan.needs_human
    assert "could not read its toolchain manifest" in plan.error and why in plan.error
    assert "is not turned on for this BOSS" not in plan.error
    assert "`boss update`" in plan.error and "`boss toolchains enable go`" in plan.error


def test_a_readable_manifest_has_no_problem(go_off):
    assert toolchains.manifest_problem() is None


# ---- the real pipeline -----------------------------------------------------------------------------

class _Sandbox:
    def __init__(self, path: Path):
        self.path = path

    def provision(self, *a, **k):
        return None

    def diff(self):
        return []

    def teardown(self):
        return None


class _ReachedBuildServices(RuntimeError):
    pass


def _run_the_build(monkeypatch, repo: Path, instructions: str):
    """The real pipeline entry on a fake connection (the rules-403 test's harness) and a sandbox
    whose checkout is `repo`. A build the gate plan does not stop runs on to build services,
    where this harness ends it with a marker."""
    from boss_engine.config import Settings
    from boss_engine.ports.composition import EnginePorts

    run = {"id": "crun_go", "project_id": "proj_1", "task_id": "task_1", "status": "running",
           "phase": "bootstrap", "title": "Add a route",
           "spec": {"title": "Add a route", "instructions": instructions, "owner": "acme",
                    "repo": "svc", "acceptance_criteria": []},
           "state": {}, "usage": {}, "finalize_ctx": {}, "reservation_id": None,
           "pull_request_url": None, "pull_request_number": None, "decision_id": None,
           "error": None}
    conn = _Conn()
    settings = Settings(_env_file=None, github_token="tok", github_app_id=None,
                        github_app_private_key=None, github_app_private_key_path=None,
                        github_app_installation_id=None)
    rescued: list = []

    def services(*_a, **_k):
        raise _ReachedBuildServices("reached build services")

    monkeypatch.setattr(g, "load_code_task_run", lambda _c, _run_id: run)
    monkeypatch.setattr(g, "_engine_ports", lambda: EnginePorts.oss_defaults(settings))
    monkeypatch.setattr(g.LocalSubprocessProvider, "acquire",
                        lambda self, *, task_id, tenant_id=None: _Sandbox(repo))
    monkeypatch.setattr(g, "_provision_build_services", services)
    monkeypatch.setattr(g.SoftwareFactoryService, "propose_pull_request",
                        lambda *a, **k: rescued.append(k) or {"status": "skipped"})
    monkeypatch.setattr("boss_engine.identity.runs.start_code_task_run", lambda *_a, **_k: None)
    monkeypatch.setattr("boss_engine.services.github_app_adapter.repo_token",
                        lambda *_a, **_k: "tok")
    result = g._run_governed_code_task(conn, settings, object(), run["id"])  # noqa: SLF001
    return result, conn, rescued


NAMED = "Add GET /health to the service in the `svc/` folder of this repository."


def _error_text(conn) -> str:
    return "\n".join(str(v) for s, p in conn.calls if "UPDATE code_task_runs" in s
                     for v in p.values())


def test_the_build_ends_needs_human_before_build_services_and_is_not_retried(
        tmp_path, go_off, monkeypatch):
    result, conn, rescued = _run_the_build(monkeypatch, _tree(tmp_path, GO_SVC), NAMED)
    assert result["failure_class"] == "needs_human"
    assert result["reason_code"] == "stack_unsupported"
    assert result.get("auto_retry_run_id") is None and result.get("auto_retry") is None
    error = _error_text(conn)
    assert "reached build services" not in error
    assert "`boss toolchains enable go`" in error and "`boss update`" in error
    assert rescued == []  # no partial PR of code no gate could run
    assert _decisions(conn) == []


def test_with_go_on_the_build_goes_on(tmp_path, go_on, monkeypatch):
    result, conn, _ = _run_the_build(monkeypatch, _tree(tmp_path, GO_SVC), NAMED)
    assert result["reason_code"] != "stack_unsupported"
    assert "reached build services" in _error_text(conn)


def test_a_go_root_repo_building_its_web_app_is_not_stopped_at_start(tmp_path, go_off,
                                                                     monkeypatch):
    result, conn, _ = _run_the_build(monkeypatch, _tree(tmp_path, GO_ROOT_WEB),
                                     "Add a dark-mode toggle to the settings page.")
    assert result["reason_code"] != "stack_unsupported"
    assert "reached build services" in _error_text(conn)


def test_a_node_project_builds_on(tmp_path, go_off, monkeypatch):
    result, conn, _ = _run_the_build(monkeypatch, _tree(tmp_path, HUGO),
                                     "Add a dark-mode toggle to the settings page.")
    assert result["reason_code"] != "stack_unsupported"
    assert "reached build services" in _error_text(conn)


# ---- mid-build: every verify, and nothing may swallow it -------------------------------------------

def _mid_build_harness(monkeypatch, repo: Path, *, spec_extra: dict | None = None,
                       settings_extra: dict | None = None, use_microvm: bool = False):
    from boss_engine.config import Settings
    from boss_engine.ports.composition import EnginePorts
    from boss_engine.schemas import FileChange

    initial = set(p.relative_to(repo).as_posix() for p in repo.rglob("*") if p.is_file())

    class _DiffSandbox(_Sandbox):
        def diff(self):
            out = []
            for p in sorted(repo.rglob("*")):
                if p.is_file():
                    rel = p.relative_to(repo).as_posix()
                    if rel not in initial:
                        out.append(FileChange(path=rel, content=p.read_text(errors="ignore")))
            return out

        def diff_summary(self):
            return SimpleNamespace(paths=[fc.path for fc in self.diff()])

    run = {
        "id": "crun_mid_go", "project_id": "proj_1", "task_id": "task_1",
        "status": "running", "phase": "bootstrap", "title": "Add a route",
        "spec": {
            "title": "Add a route",
            "instructions": "Add a health endpoint.",
            "owner": "acme", "repo": "svc",
            "acceptance_criteria": [],
            "risk_tier": 0,
            **(spec_extra or {}),
        },
        "state": {}, "usage": {}, "finalize_ctx": {"risk_tier": 0},
        "reservation_id": None, "pull_request_url": None,
        "pull_request_number": None, "decision_id": None, "error": None,
    }
    conn = _Conn()
    settings = Settings(
        _env_file=None, github_token="tok", github_app_id=None,
        github_app_private_key=None, github_app_private_key_path=None,
        github_app_installation_id=None,
        code_agent_engine="scripted", code_review_engine="scripted",
        **(settings_extra or {}),
    )
    rescued: list = []

    monkeypatch.setattr(g, "load_code_task_run", lambda _c, _run_id: run)
    monkeypatch.setattr(g, "_engine_ports", lambda: EnginePorts.oss_defaults(settings))
    monkeypatch.setattr(g.LocalSubprocessProvider, "acquire",
                        lambda self, *, task_id, tenant_id=None: _DiffSandbox(repo))
    monkeypatch.setattr(g, "_provision_build_services", lambda *_a, **_k: None)
    monkeypatch.setattr(g, "infer_risk_tier",
                        lambda s, _p: int((s or {}).get("risk_tier", 0)))
    monkeypatch.setattr("boss_engine.services.typed_decisions.make_decider",
                        lambda *_a, **_k: (_ for _ in ()).throw(RuntimeError("no model")))
    monkeypatch.setattr(g, "_guest_engine_fields",
                        lambda _settings, eng, model, **_kw: {"engine": eng, "model": model})
    monkeypatch.setattr(g.SoftwareFactoryService, "propose_pull_request",
                        lambda *a, **k: rescued.append(k) or {"status": "skipped"})
    monkeypatch.setattr("boss_engine.identity.runs.start_code_task_run", lambda *_a, **_k: None)
    monkeypatch.setattr("boss_engine.services.github_app_adapter.repo_token",
                        lambda *_a, **_k: "tok")
    if use_microvm:
        fake_gh = SimpleNamespace(
            default_branch_sha=lambda: ("main", "sha0"),
            ensure_branch=lambda *_a, **_k: "sha_branch",
            upsert_file=lambda *_a, **_k: "sha_commit",
            commit_files=lambda *_a, **_k: "sha_commit",
            delete_file=lambda *_a, **_k: "sha_commit",
            read_text_file=lambda *_a, **_k: None,
            branch_exists=lambda *_a, **_k: False,
            compare_changed_files=lambda *_a, **_k: [],
        )

        def _bind(self, _c, _p, **_kw):
            self.github = fake_gh
            return {}

        monkeypatch.setattr(g.SoftwareFactoryService, "bind_project_github", _bind)
    else:
        monkeypatch.setattr(g.SoftwareFactoryService, "bind_project_github",
                            lambda self, _c, _p, **_kw: {})
    return run, conn, settings, rescued


def test_a_mid_build_go_mod_write_with_go_off_stops_immediately_without_repair(
        tmp_path, go_off, monkeypatch):
    """Real mid-build flow: base repo has only README.md; the implement agent writes svc/go.mod
    (and even svc/package.json) while Go is off -> _run_verify resolves toolchain_missing ->
    stops immediately as needs_human / stack_unsupported with 1 implement call and no partial PR."""
    repo = _tree(tmp_path, {"README.md": "x\n", "svc/.keep": ""})
    run, conn, settings, rescued = _mid_build_harness(monkeypatch, repo)
    impl_calls: list[str] = []

    class _FakeAgent:
        local_repo = False
        redact = False
        budget_ceiling = None
        command_runner = None

        def run_role(self, prompt, workdir, **_kw):
            impl_calls.append(prompt)
            (Path(workdir) / "svc" / "go.mod").write_text(GO_MOD)
            (Path(workdir) / "svc" / "main.go").write_text("package main\nfunc main() {}\n")
            (Path(workdir) / "svc" / "package.json").write_text(PKG)
            return {"summary": "wrote svc", "cost_usd_micros": 0, "usage": {}, "session_id": "s1"}

    monkeypatch.setattr(g, "make_code_agent", lambda *_a, **_k: _FakeAgent())
    result = g._run_governed_code_task(conn, settings, object(), run["id"])  # noqa: SLF001
    assert result["failure_class"] == "needs_human", result
    assert result["reason_code"] == "stack_unsupported", result
    assert len(impl_calls) == 1
    assert rescued == []
    assert "`boss toolchains enable go`" in _error_text(conn)


def test_use_microvm_replans_from_accumulated_and_stops_when_go_off(
        tmp_path, go_off, monkeypatch):
    """In use_microvm, the guest writes svc/go.mod into accumulated while Go is off -> host
    re-plans from accumulated + pinned base manifest and halts before guest verification or PR."""
    from boss_engine.schemas import FileChange

    repo = _tree(tmp_path, {"README.md": "x\n", "svc/.keep": ""})
    run, conn, settings, rescued = _mid_build_harness(
        monkeypatch, repo, spec_extra={"substrate": "microvm"}, use_microvm=True)
    guest_steps: list[tuple[str, list[str]]] = []

    class _FakeVM:
        last_sandbox_task_id = None

        def __init__(self, _settings):
            pass

        def run_code_task(self, guest_spec, tenant_id=None):
            guest_steps.append((guest_spec["task_id"], list(guest_spec["test_commands"])))
            return SimpleNamespace(
                diff=[
                    FileChange(path="svc/go.mod", content=GO_MOD),
                    FileChange(path="svc/main.go", content="package main\nfunc main() {}\n"),
                ],
                deleted=[],
                cost_usd_micros=0,
                summary="wrote go svc",
                usage={},
                turns=1,
                tests_passed=True,
                tests=[],
                error=None,
            )

    monkeypatch.setattr(g, "MicroVMProvider", _FakeVM)
    result = g._run_governed_code_task(conn, settings, object(), run["id"])  # noqa: SLF001
    assert result["failure_class"] == "needs_human", result
    assert result["reason_code"] == "stack_unsupported", result
    assert len(guest_steps) == 1  # only implement ran; no repair / regression / mutation VM
    assert rescued == []
    assert "`boss toolchains enable go`" in _error_text(conn)


def test_use_microvm_replans_verify_commands_before_guest_regression_when_go_on(
        tmp_path, go_on, monkeypatch):
    """In use_microvm with Go on, when implement writes svc/go.mod + svc/main.go, host re-plans
    verify_commands to Go gate before guest regression (run_all_tests)."""
    from boss_engine.schemas import FileChange

    repo = _tree(tmp_path, {"README.md": "x\n", "svc/.keep": ""})
    run, conn, settings, _rescued = _mid_build_harness(
        monkeypatch, repo, spec_extra={"substrate": "microvm", "risk_tier": 1}, use_microvm=True)
    run["finalize_ctx"] = {"risk_tier": 1}
    guest_calls: list[tuple[str, list[str]]] = []

    class _FakeVM:
        last_sandbox_task_id = None

        def __init__(self, _settings):
            pass

        def run_code_task(self, guest_spec, tenant_id=None):
            tid = guest_spec["task_id"]
            guest_calls.append((tid, list(guest_spec["test_commands"])))
            if tid.endswith("-write_tests"):
                return SimpleNamespace(
                    diff=[FileChange(path="svc/main_test.go", content="package main\n")],
                    deleted=[], cost_usd_micros=0, summary="wrote tests", usage={},
                    turns=1, tests_passed=True, tests=[], error=None)
            if tid.endswith("-implement"):
                return SimpleNamespace(
                    diff=[
                        FileChange(path="svc/go.mod", content=GO_MOD),
                        FileChange(path="svc/main.go", content="package main\nfunc main() {}\n"),
                    ],
                    deleted=[], cost_usd_micros=0, summary="wrote go", usage={},
                    turns=1, tests_passed=True, tests=[], error=None)
            return SimpleNamespace(
                diff=[], deleted=[], cost_usd_micros=0, summary="VERDICT: SURVIVED", usage={},
                turns=1, tests_passed=True, tests=[], error=None)

    class _FakeAgent:
        local_repo = False
        redact = False
        budget_ceiling = None
        command_runner = None

        def run_role(self, *_a, **_kw):
            return {"summary": "VERDICT: SURVIVED\nSTATUS: clean\nCONSISTENCY: consistent",
                    "cost_usd_micros": 0, "usage": {}, "session_id": "s1"}

    monkeypatch.setattr(g, "MicroVMProvider", _FakeVM)
    monkeypatch.setattr(g, "make_code_agent", lambda *_a, **_k: _FakeAgent())
    g._run_governed_code_task(conn, settings, object(), run["id"])  # noqa: SLF001
    reg = [cmds for tid, cmds in guest_calls if tid.endswith("-run_all_tests")]
    assert reg == [list(toolchains.GO_GATE)], guest_calls
