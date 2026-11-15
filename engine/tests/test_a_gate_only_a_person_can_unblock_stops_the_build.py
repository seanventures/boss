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

import inspect
import json
from pathlib import Path

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

def test_every_verify_and_every_exit_toward_a_pr_honours_the_stop():
    src = inspect.getsource(g._run_governed_code_task)
    run_verify = src[src.index("def _run_verify("):src.index("def _run_verify_once(")]
    assert "_halt_if_needs_person()" in run_verify  # sticky: once met, every verify re-raises
    once = src[src.index("def _run_verify_once("):]
    once = once[:once.index("\n        def ", 1)]
    assert once.index("_halt_if_needs_person(plan)") < once.index("_run_manifest_verify(plan")
    # The broad handlers around a verify re-raise it (red-first, quality, convergence, secrets).
    assert src.count("except StackUnsupportedError:\n") >= 4
    # A panel seat's error is recorded, not raised: the regression seat says failed, and the
    # panel re-raises after its seats (both panel runs).
    assert 'panel["full"] = False' in src
    assert src.count("_halt_if_needs_person()") >= 5
    # Before the gates are judged and before the PR opens.
    gates = src.index("        gates = {\n")
    assert src.rindex("_halt_if_needs_person()", 0, gates) > gates - 60
    pr = src.index('_abort_if_cancelled("before opening the PR"')
    assert src.index("_halt_if_needs_person()", pr) < src.index("sf.propose_pull_request(", pr)
    # No partial PR for it.
    exit_ = src[src.index("if isinstance(exc, StackUnsupportedError):\n            _partial_pr"):]
    assert exit_.index("_partial_pr = None") < exit_.index("_rescue_pr(reason)")
