"""A gate only a person can unblock stops the build at once, and says how to unblock it.

PR #135 marked a toolchain_missing gate plan as a person's to fix (GatePlan.needs_human): a go.mod
tree while Go is off on this install. Nothing read it, so a build that met it ran the agents and
then its repair rounds against a gate no code change can make run. The build now stops right
after the gate plan is resolved — before build services, agents or any repair round — as
needs_human / stack_unsupported (no automatic retry), and the card carries the plan's own words:
which toolchain, `boss toolchains enable go`, `boss update`. A tree that turns into such a stack
mid-build stops at its next verify the same way. Go on, or a Node project, is unaffected.
"""
from __future__ import annotations

import inspect
import json
from pathlib import Path

import pytest

from boss_engine.services import governed_code_task_service as g
from boss_engine.services import toolchains, verification_target as vt

from .test_a_rules_403_opens_an_inbox_decision import _Conn, _decisions


def _manifest(tmp_path, monkeypatch, data: dict) -> Path:
    manifest = tmp_path / "manifest.json"
    manifest.write_text(json.dumps(data))
    monkeypatch.setenv(toolchains.MANIFEST_ENV, str(manifest))
    monkeypatch.setenv(toolchains.CACHE_ROOT_ENV, str(tmp_path / "cache"))
    return manifest


@pytest.fixture
def go_on(tmp_path, monkeypatch):
    return _manifest(tmp_path, monkeypatch, {"go": {"go": "1.27.4"}})


@pytest.fixture
def go_off(tmp_path, monkeypatch):
    return _manifest(tmp_path, monkeypatch, {})


def _go_tree(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "go.mod").write_text("module example.com/x\n\ngo 1.27\n")
    (repo / "main.go").write_text("package main\n\nfunc main() {}\n")
    return repo


def _node_tree(tmp_path: Path) -> Path:
    repo = tmp_path / "web"
    repo.mkdir()
    (repo / "package.json").write_text(json.dumps({"name": "web", "scripts": {"test": "vitest"}}))
    return repo


def _plan(repo: Path):
    return vt.resolve_gate_plan({}, "add a route", repo)


# ---- the plan decides -----------------------------------------------------------------------------

def test_a_go_tree_with_go_off_stops_with_the_enable_hint(tmp_path, go_off):
    plan = _plan(_go_tree(tmp_path))
    assert plan.needs_human
    with pytest.raises(g.StackUnsupportedError) as stopped:
        g.stop_if_the_gate_needs_a_person(plan)
    words = str(stopped.value)
    assert words.startswith("This project needs Go (it has a go.mod)")
    assert "`boss toolchains enable go`" in words and "`boss update`" in words


def test_with_go_on_the_build_proceeds_to_the_go_gate(tmp_path, go_on):
    plan = _plan(_go_tree(tmp_path))
    g.stop_if_the_gate_needs_a_person(plan)  # does not raise
    assert plan.source == "toolchain" and not plan.needs_human
    assert plan.commands == list(toolchains.GO_GATE)


def test_a_node_project_is_unaffected(tmp_path, go_off):
    plan = _plan(_node_tree(tmp_path))
    g.stop_if_the_gate_needs_a_person(plan)  # does not raise
    assert not plan.needs_human and plan.source != "toolchain_missing"


# ---- the build ends needs_human / stack_unsupported, with the hint on its card ------------------

def _run_the_build(monkeypatch, acquire):
    """The real pipeline entry on a fake connection (test_a_rules_403_opens_an_inbox_decision's
    harness); `acquire` is the first thing the build does with a sandbox."""
    from boss_engine.config import Settings
    from boss_engine.ports.composition import EnginePorts

    run = {"id": "crun_go", "project_id": "proj_1", "task_id": "task_1", "status": "running",
           "phase": "bootstrap", "title": "Add a route",
           "spec": {"title": "Add a route", "instructions": "Add GET /health.", "owner": "acme",
                    "repo": "svc", "acceptance_criteria": []},
           "state": {}, "usage": {}, "finalize_ctx": {}, "reservation_id": None,
           "pull_request_url": None, "pull_request_number": None, "decision_id": None,
           "error": None}
    conn = _Conn()
    settings = Settings(_env_file=None, github_token="tok", github_app_id=None,
                        github_app_private_key=None, github_app_private_key_path=None,
                        github_app_installation_id=None)
    monkeypatch.setattr(g, "load_code_task_run", lambda _c, _run_id: run)
    monkeypatch.setattr(g, "_engine_ports", lambda: EnginePorts.oss_defaults(settings))
    monkeypatch.setattr(g.LocalSubprocessProvider, "acquire", acquire)
    monkeypatch.setattr("boss_engine.identity.runs.start_code_task_run", lambda *_a, **_k: None)
    monkeypatch.setattr("boss_engine.services.github_app_adapter.repo_token",
                        lambda *_a, **_k: "tok")
    return g._run_governed_code_task(conn, settings, object(), run["id"]), conn  # noqa: SLF001


def test_the_build_ends_needs_human_stack_unsupported_and_is_not_retried(tmp_path, go_off,
                                                                         monkeypatch):
    plan = _plan(_go_tree(tmp_path))

    def acquire(self, *, task_id, tenant_id=None):
        g.stop_if_the_gate_needs_a_person(plan)
        raise AssertionError("the gate plan should have stopped the build")

    result, conn = _run_the_build(monkeypatch, acquire)
    assert result["failure_class"] == "needs_human"
    assert result["reason_code"] == "stack_unsupported"
    assert result.get("auto_retry_run_id") is None and result.get("auto_retry") is None
    stored = [str(v) for s, p in conn.calls if "UPDATE code_task_runs" in s for v in p.values()]
    hint = [v for v in stored if "boss toolchains enable go" in v]
    assert hint, "the run's error (its card) must carry the enable hint"
    assert any("boss update" in v for v in hint)
    assert _decisions(conn) == []  # no capacity / rules card in its place


# ---- where it runs: before any service, agent or repair round, and at every later verify --------

def test_the_stop_runs_before_build_services_agents_and_repair_rounds():
    src = inspect.getsource(g._run_governed_code_task)
    stop = src.index("stop_if_the_gate_needs_a_person(_gate_plan0)")
    # After the last resolution of the starting plan (a seeded repair re-plans it) …
    assert src.rindex("_gate_plan0 = verification_target.resolve_gate_plan(") < stop
    # … and before anything that spends: build services, the first verify, any repair.
    assert stop < src.index("_services_lease = _provision_build_services(")
    assert stop < src.index("test_result = _run_verify()")
    verify_once = src[src.index("def _run_verify_once("):]
    verify_once = verify_once[:verify_once.index("\n        def ", 1)]
    assert verify_once.index("stop_if_the_gate_needs_a_person(plan)") < \
        verify_once.index("_run_manifest_verify(plan")
