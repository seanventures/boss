"""Dry run 2, gap 9: a code task started straight through POST /v1/code-tasks got the project's
Standard Product Delivery run wrapped around it (wfr_eaf335b4). Its change (#10) merged at
02:03; at 03:42 the run's `plrr` step extended the merged build, which opened a NEW change,
#13 "Follow-up: Production launch readiness — …", and then started yet another follow-up.

A direct change's outer run (run input `direct_change`, set by the endpoint when no workflow was
picked) ends with that one change: once it is merged or closed, every later step is
`not_applicable` and nothing is built, opened or deployed. No database: the record is faked.
"""
from __future__ import annotations

import contextlib
from types import SimpleNamespace

import pytest

from boss_engine.services import code_task_service
from boss_engine.services import workflow_capability_runner as runner

BUILD = "crun_direct_setup"


class _Rows:
    def __init__(self, row):
        self._row = row

    def mappings(self):
        return self

    def first(self):
        return self._row

    def all(self):
        return [self._row] if self._row else []

    def scalar(self):
        return None


class FakeRecord:
    """The build's row, its merge decision, and BOSS's merge_result artifacts."""

    def __init__(self, *, merged_sha="", decision_status="pending"):
        self.fctx = {"merge": {"sha": merged_sha}} if merged_sha else {}
        self.decision_status = decision_status
        self.writes: list[str] = []

    def begin_nested(self):
        return contextlib.nullcontext()

    def execute(self, statement, params=None):
        sql = str(statement)
        if "FROM code_task_runs" in sql:
            return _Rows({"project_id": "northgate", "pull_request_number": 10,
                          "pull_request_url": "http://boss-local/local_ng/ng/pull/10",
                          "finalize_ctx": self.fctx, "decision_id": "dec_setup"})
        if "FROM decisions" in sql:
            return _Rows({"id": "dec_setup", "status": self.decision_status})
        if "FROM artifacts" in sql:
            return _Rows(None)
        self.writes.append(sql)
        return _Rows(None)


def _context(direct=True):
    return {"run_id": "wfr_direct", "input": {"adopt_code_task_run_id": BUILD,
                                              **({"direct_change": True} if direct else {})}}


def _plrr(connection, context):
    """The step that opened #13: a launch-readiness pass on the build's branch."""
    return runner._perform_capability(
        connection, SimpleNamespace(settings=None), project_id="northgate", actor="boss",
        step={"id": "plrr", "binding": {"capability": "production_launch_readiness"}},
        context=context, key="production_launch_readiness",
        capability=SimpleNamespace(tool=None, label="Production launch readiness"), inputs={})


@pytest.mark.parametrize("record,words", [
    (FakeRecord(merged_sha="68417445"), "is merged"),
    (FakeRecord(decision_status="rejected"), "closed without merging"),
])
def test_a_direct_change_wrapper_ends_when_its_change_is_merged_or_closed(record, words):
    out = _plrr(record, _context())
    assert out["status"] == "not_applicable"
    assert out["direct_change_ended"] is True
    assert words in out["note"] and "nothing more is built, opened or deployed" in out["note"]
    assert record.writes == []                    # nothing started, extended or opened
    # deploy and every other later step end the same way
    assert runner._direct_change_ended(record, "deploy", _context())


def test_the_wrapper_still_runs_while_its_change_is_open_or_when_it_is_not_direct():
    assert runner._direct_change_ended(FakeRecord(), "production_launch_readiness",
                                       _context()) is None
    merged = FakeRecord(merged_sha="68417445")
    # A workflow someone picked (or a panel's project) keeps its full path after the merge.
    assert runner._direct_change_ended(merged, "production_launch_readiness",
                                       _context(direct=False)) is None
    # The adopted build's own step still waits on that build.
    assert runner._direct_change_ended(merged, "build", _context()) is None


def test_the_outer_run_of_a_direct_change_carries_the_mark(monkeypatch):
    seen: dict = {}

    def start_project_run(connection, **kw):
        seen.update(kw["extra_input"])
        return {"id": "wfr_direct"}

    from boss_engine.services import workflows

    monkeypatch.setattr(workflows, "start_project_run", start_project_run)
    record = FakeRecord()
    code_task_service.start_delivery_workflow(
        record, project_id="northgate", task_id=None, run_id=BUILD, title="Setup",
        instructions="Add two files.", finalize_ctx={"direct_change": True})
    assert seen["direct_change"] is True and seen["adopt_code_task_run_id"] == BUILD
    seen.clear()
    code_task_service.start_delivery_workflow(
        record, project_id="northgate", task_id=None, run_id=BUILD, title="Setup",
        instructions="Add two files.", finalize_ctx={})
    assert "direct_change" not in seen


def test_the_endpoint_marks_only_a_change_with_no_workflow_and_no_origin():
    from pathlib import Path

    src = (Path(__file__).resolve().parents[2] / "apps" / "api" / "app" / "main.py").read_text()
    body = src[src.index("def start_code_task_endpoint"):]
    body = body[:body.index("jobs=jobs,")]
    assert '"direct_change": True' in body
    assert 'not payload.get("workflow_version_id")' in body
    assert '.get("origin")' in body
