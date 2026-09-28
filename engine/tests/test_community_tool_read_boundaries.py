"""Community read tools cannot use the server's network or files outside a project."""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
import json
import urllib.request

import pytest

from boss_engine.tools import build_default_registry, build_discuss_read_registry
from boss_engine.tools import builtin


class _Result:
    def __init__(self, row=None, rows=None):
        self.row = row
        self.rows = rows or []

    def mappings(self):
        return self

    def first(self):
        return self.row

    def all(self):
        return self.rows


class _Connection:
    def __init__(self, *, bound=False, run=None, logs=None):
        self.bound = bound
        self.run = run
        self.logs = logs or []
        self.queries = []

    def execute(self, statement, params=None):
        sql = str(statement)
        self.queries.append((sql, params or {}))
        if "FROM github_repository_bindings" in sql:
            assert params["p"] == "project-a"
            return _Result(("binding-a",) if self.bound else None)
        if "FROM code_task_runs" in sql:
            assert "project_id = :p" in sql
            assert params["p"] == "project-a"
            return _Result(self.run)
        if "FROM app_logs" in sql:
            assert "AND project_id = :pid" in sql
            assert "project_id IS NULL" not in sql
            assert params["pid"] == "project-a"
            return _Result(rows=self.logs)
        raise AssertionError(f"unexpected query: {sql}")


class _Ctx:
    def __init__(self, edition, connection):
        self.settings = type("Settings", (), {
            "boss_edition": edition,
            "github_token": "global-token",
            "target_github_owner": "other",
            "target_github_repo": "repo",
        })()
        self.connection = connection

    @contextmanager
    def begin(self):
        yield self.connection


def _no_external(*_args, **_kwargs):
    raise AssertionError("external access must be stopped before this call")


@pytest.mark.parametrize("edition", ["community", "oss", "local", "unknown"])
def test_direct_http_probe_stops_before_network(edition, monkeypatch):
    monkeypatch.setattr(urllib.request, "urlopen", _no_external)
    result = build_default_registry().invoke(
        "http_probe", _Ctx(edition, _Connection()), project_id="project-a",
        actor="member", args={"url": "http://169.254.169.254/latest/meta-data/"},
    )
    assert result["ok"] is False
    assert "Community URL probes" in result["error"]
    assert "http_probe" not in build_discuss_read_registry(edition=edition).names()


@pytest.mark.parametrize("tool,args", [
    ("search_code", {"repo": "other/repo", "pattern": "secret"}),
    ("list_source", {"repo": "other/repo"}),
    ("read_code", {"repo": "other/repo", "file": "secrets.txt"}),
    ("git_history", {"repo": "other/repo"}),
])
def test_explicit_unbound_repository_stops_before_clone(tool, args, monkeypatch):
    monkeypatch.setattr(builtin, "_repo_root", _no_external)
    monkeypatch.setattr(builtin._subprocess, "run", _no_external)
    conn = _Connection(bound=False)
    result = build_default_registry().invoke(
        tool, _Ctx("community", conn), project_id="project-a", actor="member", args=args,
    )
    assert result["ok"] is False
    assert result["error"] == "repository unavailable in this project"
    assert len(conn.queries) == 1


def test_explicit_bound_repository_remains_resolvable():
    ctx = _Ctx("community", _Connection(bound=True))
    assert builtin._resolve_source(
        ctx, {"repo": "owner/allowed", "ref": "main"}, project_id="project-a",
    ) == ("owner/allowed", "main", None)


def test_unbound_pull_request_stops_before_github(monkeypatch):
    monkeypatch.setattr(urllib.request, "urlopen", _no_external)
    conn = _Connection(bound=False)
    result = build_default_registry().invoke(
        "read_pull_request", _Ctx("community", conn), project_id="project-a",
        actor="member", args={"pull_request": "https://github.com/other/repo/pull/7",
                              "with_diff": True},
    )
    assert result["ok"] is False
    assert result["error"] == "repository unavailable in this project"
    assert len(conn.queries) == 1


def test_project_run_pull_request_ignores_foreign_repo_override(monkeypatch):
    calls = []

    class _Response:
        def __init__(self, payload):
            self.payload = payload

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self):
            return json.dumps(self.payload).encode()

    def fake_open(request, timeout):
        calls.append(request.full_url)
        return _Response([] if request.full_url.endswith("/files?per_page=100") else {})

    monkeypatch.setattr(urllib.request, "urlopen", fake_open)
    conn = _Connection(run={"pull_request_number": 7,
                            "pull_request_url": "https://github.com/owner/allowed/pull/7",
                            "owner": "owner", "repo": "allowed"})
    result = build_default_registry().invoke(
        "read_pull_request", _Ctx("community", conn), project_id="project-a",
        actor="member", args={"pull_request": "crun_mine", "repo": "other/repo"},
    )
    assert result["ok"] is True
    assert result["repo"] == "owner/allowed"
    assert calls == [
        "https://api.github.com/repos/owner/allowed/pulls/7",
        "https://api.github.com/repos/owner/allowed/pulls/7/files?per_page=100",
    ]


@pytest.mark.parametrize("edition", ["community", "oss", "local", "unknown"])
def test_community_logs_use_only_project_rows_and_never_open_shared_files(edition, monkeypatch):
    monkeypatch.setattr(builtin._os.path, "isfile", _no_external)
    stamp = datetime.now(timezone.utc)
    conn = _Connection(logs=[{
        "created_at": stamp, "level": "INFO", "logger": "api",
        "message": "own project event", "project_id": "project-a", "run_id": None,
    }])
    result = build_default_registry().invoke(
        "read_logs", _Ctx(edition, conn), project_id="project-a", actor="member",
        args={"service": "api", "project_id": "project-b"},
    )
    assert result["ok"] is True
    assert result["source"] == "app_logs (BOSS's own capture)"
    assert "own project event" in result["log"][0]
    assert len(conn.queries) == 1
