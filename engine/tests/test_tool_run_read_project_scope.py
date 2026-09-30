"""Community agent reads must not resolve another project's run ID."""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone

import pytest

from boss_engine.tools import build_discuss_read_registry
from boss_engine.tools import builtin


class _Result:
    def __init__(self, row=None, rows=None):
        self.row = row
        self.rows = [] if rows is None else rows

    def mappings(self):
        return self

    def scalars(self):
        return self

    def first(self):
        return self.row

    def all(self):
        return self.rows


class _ForeignRunConnection:
    def __init__(self):
        self.queries = []

    def execute(self, statement, params=None):
        sql = str(statement)
        self.queries.append((sql, params))
        assert "FROM code_task_runs" in sql
        assert "project_id" in sql
        assert (params or {}).get("p") == "project-a"
        return _Result()  # The run exists, but only in project B.


class _Ctx:
    def __init__(self, connection):
        self.connection = connection
        self.settings = object()

    @contextmanager
    def begin(self):
        yield self.connection


@pytest.mark.parametrize(
    ("tool", "args"),
    [
        ("read_run_turns", {"run_id": "crun_foreign"}),
        ("diagnose_run", {"run_id": "crun_foreign"}),
        ("search_code", {"run_id": "crun_foreign", "pattern": "secret"}),
        ("list_source", {"run_id": "crun_foreign"}),
        ("read_code", {"run_id": "crun_foreign", "file": "secret.txt"}),
        ("git_history", {"run_id": "crun_foreign"}),
    ],
)
def test_foreign_run_is_rejected_before_transcript_or_source_access(tool, args, monkeypatch):
    def no_source(*_args, **_kwargs):
        raise AssertionError("foreign run must not reach a repository or network")

    monkeypatch.setattr(builtin, "_repo_root", no_source)
    monkeypatch.setattr(builtin._subprocess, "run", no_source)
    conn = _ForeignRunConnection()
    result = build_discuss_read_registry(edition="community").invoke(
        tool, _Ctx(conn), project_id="project-a", actor="member", args=args,
    )

    assert result["ok"] is False
    expected_error = "no code task run 'crun_foreign'" if tool in {
        "search_code", "list_source", "read_code", "git_history"
    } else "no code task run crun_foreign"
    assert result["error"] == expected_error
    assert len(conn.queries) == 1


class _OwnRunConnection:
    def __init__(self):
        self.queries = []

    def execute(self, statement, params=None):
        sql = str(statement)
        self.queries.append((sql, params))
        if "FROM code_task_runs WHERE id = :r AND project_id = :p" in sql:
            assert params == {"r": "crun_mine", "p": "project-a"}
            if "SELECT status, phase" in sql:
                return _Result({"status": "completed", "phase": "done"})
            return _Result({
                "id": "crun_mine", "project_id": "project-a", "title": "Own app",
                "status": "completed", "phase": "done", "error": None,
                "minutes": 10, "created_at": datetime.now(timezone.utc),
                "updated_at": datetime.now(timezone.utc),
            })
        if "FROM agent_turns" in sql and "AS turns" in sql:
            stamp = datetime.now(timezone.utc)
            return _Result({
                "turns": 1, "first_turn": stamp, "last_turn": stamp,
                "tool_calls": 0, "tool_results": 0, "failures": 0,
                "silent_seconds": 0,
            })
        if "FROM agent_turns" in sql and "AS n" in sql:
            return _Result({"n": 0})
        if "FROM agent_turns" in sql:
            return _Result(rows=[{
                "turn_index": 1, "direction": "completion", "stage": "implement",
                "role_key": "engineer", "tool_name": None, "latency_ms": 5,
                "ok": True, "error": None, "truncated": False,
                "content": "this project's own work", "created_at": datetime.now(timezone.utc),
            }])
        if "FROM code_task_runs" in sql and "status = 'completed'" in sql:
            return _Result(rows=[])
        raise AssertionError(f"unexpected query: {sql}")


def test_same_project_transcript_remains_readable():
    result = build_discuss_read_registry(edition="community").invoke(
        "read_run_turns", _Ctx(_OwnRunConnection()), project_id="project-a",
        actor="member", args={"run_id": "crun_mine"},
    )
    assert result["ok"] is True
    assert result["recent_turns"][0]["content"] == "this project's own work"


def test_same_project_diagnosis_remains_readable():
    result = build_discuss_read_registry(edition="community").invoke(
        "diagnose_run", _Ctx(_OwnRunConnection()), project_id="project-a",
        actor="member", args={"run_id": "crun_mine", "project_id": "project-b"},
    )
    assert result["ok"] is True
    assert result["run_id"] == "crun_mine"
    assert result["project_id"] == "project-a"
