"""An approved PRD either creates build work or records why it did not. Never "started" alone.

Observed on boss-1, 2026-11-01: dec_8b573b40 ("Approve publishing the PRD", boss-platform) was
approved at 22:43:52. At 22:43:53 the events said `code_task.started_from_prd`
{mode: decomposed_build}, and nothing existed behind it: no code_task_runs row, no job, no
failure anywhere. Two faults, one class:

  1. The event was emitted on the approval thread BEFORE the build thread ran, and the thread
     swallowed every exception and ignored `started: False`.
  2. The PRD (BOSS's own platform, which mentions "UI") matched the web-app heuristic, so the
     launch tried to seed a fresh Vite scaffold through the install's default GitHub target —
     which on boss-1 names no repository — and raised.

The contract: `code_task.started_from_prd` carries a run_id and is written only after that run
exists; any failure writes `code_task.not_started` with the reason, and the reason is put on
the decision itself. An existing codebase is never scaffolded over.
"""
from __future__ import annotations

import inspect
from contextlib import contextmanager
from types import SimpleNamespace

import httpx
import pytest

from boss_engine.services import decisions, prd_decomposition as pd


class _Conn:
    def __init__(self, log):
        self.log = log

    def execute(self, statement, params=None):
        self.log.append(("sql", str(statement), params))
        return SimpleNamespace(scalar=lambda: None)


@pytest.fixture
def harness(monkeypatch):
    log: list = []

    @contextmanager
    def fake_begin(_engine):
        yield _Conn(log)

    monkeypatch.setattr(decisions, "begin", fake_begin)
    monkeypatch.setattr(decisions, "emit_event",
                        lambda _c, _p, kind, _a, _et, _eid, data: log.append(("event", kind, data)))
    monkeypatch.setattr(pd, "project_github", lambda *a, **k: "gh")
    monkeypatch.setattr(pd, "repository_has_code", lambda gh: True)
    return log


def _launch(directive="Add a ranking stage to the engine"):
    return decisions.launch_prd_build(
        object(), SimpleNamespace(), object(), project_id="p", decision_id="dec_1",
        task_id="tsk_1", actor="op", title="T", directive=directive, owner="o", repo="r")


def _events(log):
    return [(e[1], e[2]) for e in log if e[0] == "event"]


class TestStartedOnlyWithARunBehindIt:
    def test_a_started_build_names_the_run_that_exists(self, harness, monkeypatch):
        def start(*_a, **_k):
            harness.append(("run_created", "crun_1"))
            return {"started": True, "first_run_id": "crun_1", "total": 3}

        monkeypatch.setattr(pd, "start_decomposed_build", start)
        _launch()
        kinds = [e[1] if e[0] == "event" else e[0] for e in harness]
        assert kinds.index("run_created") < kinds.index("code_task.started_from_prd")
        (kind, data), = _events(harness)
        assert kind == "code_task.started_from_prd" and data["run_id"] == "crun_1"

    def test_a_launch_that_raises_is_a_visible_failure(self, harness, monkeypatch):
        def boom(*_a, **_k):
            raise RuntimeError("GitHub repository is not configured")

        monkeypatch.setattr(pd, "start_decomposed_build", boom)
        result = _launch()
        assert result["started"] is False
        (kind, data), = _events(harness)
        assert kind == "code_task.not_started"
        assert data["reason"] == "build_launch_failed"
        assert "GitHub repository is not configured" in data["detail"]
        notes = [e for e in harness if e[0] == "sql" and "UPDATE decisions" in e[1]]
        assert notes and "did not start" in notes[0][2]["why"]

    def test_a_decomposition_with_no_tasks_is_a_visible_failure(self, harness, monkeypatch):
        monkeypatch.setattr(pd, "start_decomposed_build",
                            lambda *a, **k: {"started": False,
                                             "reason": "decomposition_produced_no_tasks"})
        _launch()
        (kind, data), = _events(harness)
        assert kind == "code_task.not_started"
        assert data["reason"] == "decomposition_produced_no_tasks"

    def test_started_without_a_run_id_is_not_believed(self, harness, monkeypatch):
        monkeypatch.setattr(pd, "start_decomposed_build", lambda *a, **k: {"started": True})
        _launch()
        assert [k for k, _ in _events(harness)] == ["code_task.not_started"]

    def test_the_approval_thread_no_longer_announces_the_build(self):
        body = inspect.getsource(decisions.DecisionService._start_code_task_after_prd)
        assert '"code_task.started_from_prd"' not in body
        assert "launch_prd_build" in body


class TestAnExistingCodebaseIsNeverScaffolded:
    def test_a_ui_mentioning_prd_on_a_codebase_is_decomposed_unseeded(self, harness, monkeypatch):
        seen = {}
        monkeypatch.setattr(pd, "start_web_app_build",
                            lambda *a, **k: pytest.fail("scaffolded an existing codebase"))

        def decomposed(*_a, **k):
            seen.update(k)
            return {"started": True, "first_run_id": "crun_2", "total": 2}

        monkeypatch.setattr(pd, "start_decomposed_build", decomposed)
        _launch("Make the reviewer's UI show evidence on the dashboard")
        assert seen["seed_scaffold"] is False and seen["github"] == "gh"

    def test_a_new_web_app_on_an_empty_repo_is_scaffolded(self, harness, monkeypatch):
        monkeypatch.setattr(pd, "repository_has_code", lambda gh: False)
        monkeypatch.setattr(pd, "start_web_app_build",
                            lambda *a, **k: {"started": True, "run_id": "crun_3"})
        _launch("Build a React web app for expense approvals")
        (kind, data), = _events(harness)
        assert data["mode"] == "web_app_build" and data["run_id"] == "crun_3"


class _Gh:
    repository_full_name = "o/r"

    def __init__(self, tree=None, status=None):
        self.tree, self.status = tree, status

    def default_branch_sha(self):
        if self.status:
            request = httpx.Request("GET", "https://api.github.com/x")
            raise httpx.HTTPStatusError("x", request=request,
                                        response=httpx.Response(self.status, request=request))
        return "main", "sha"

    def _request(self, _method, _path):
        return {"tree": [{"path": p} for p in self.tree]}


class TestRepositoryHasCode:
    def test_an_empty_repository_has_none(self):
        assert pd.repository_has_code(_Gh(status=409)) is False

    def test_a_fresh_repository_with_a_readme_has_none(self):
        assert pd.repository_has_code(_Gh(tree=["README.md", "LICENSE", ".gitignore"])) is False

    def test_a_codebase_has_code(self):
        assert pd.repository_has_code(_Gh(tree=["README.md", "engine", "package.json"])) is True

    def test_an_auth_failure_is_raised_not_guessed(self):
        with pytest.raises(httpx.HTTPStatusError):
            pd.repository_has_code(_Gh(status=401))


def test_a_scaffold_is_never_seeded_through_the_install_default():
    with pytest.raises(ValueError):
        pd.seed_web_scaffold(SimpleNamespace(), work_branch="boss/code/x", name="app")


class TestTheBuildActsForTheApprover:
    """The launch thread enqueues a code.task job, which under enforce needs a principal. A bare
    Thread starts with an empty context: PrincipalRequired, swallowed (dec_8b573b40)."""

    def test_the_thread_sees_the_approvers_identity(self):
        from boss_engine.identity import lineage

        seen = []
        token = lineage._ACTING.set("usr_approver")
        try:
            decisions._spawn_as_this_request(lambda: seen.append(lineage._ACTING.get())).join(5)
        finally:
            lineage._ACTING.reset(token)
        assert seen == ["usr_approver"]

    def test_the_prd_launch_uses_it(self):
        body = inspect.getsource(decisions.DecisionService._start_code_task_after_prd)
        assert "_spawn_as_this_request(_launch_build)" in body
        assert "threading.Thread(" not in body
