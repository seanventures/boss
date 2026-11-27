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
        if "FROM deployment_targets" in sql or "FROM deployment_hosts" in sql:
            return _Result(rows=[])
        if "FROM app_logs" in sql:
            assert "project_id = :pid" in sql
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


@pytest.mark.parametrize("edition", ["community", "oss", "local", "unknown", "enterprise_self_hosted"])
def test_direct_http_probe_is_exposed_and_enforces_ssrf_guard(edition, monkeypatch):
    monkeypatch.setattr(urllib.request, "urlopen", _no_external)
    result = build_default_registry().invoke(
        "http_probe", _Ctx(edition, _Connection()), project_id="project-a",
        actor="member", args={"url": "http://169.254.169.254/latest/meta-data/"},
    )
    assert result["ok"] is False
    assert "metadata or link-local" in result["error"]
    assert "http_probe" in build_discuss_read_registry(edition=edition).names()


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
    # The binding check, then (for a source tool's refusal) where else the org has it bound.
    assert 1 <= len(conn.queries) <= 2
    assert all("FROM github_repository_bindings" in sql for sql, _ in conn.queries)


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


@pytest.mark.parametrize("edition", ["community", "oss", "local", "unknown", "enterprise_self_hosted"])
def test_logs_are_project_scoped_and_identical_across_editions(edition, monkeypatch):
    # Build first: the registry's lazy imports (pydantic, zoneinfo) stat files themselves, and
    # whether an earlier test already imported them must not decide this one.
    registry = build_default_registry()
    monkeypatch.setattr(builtin._os.path, "isfile", lambda _p: False)
    stamp = datetime.now(timezone.utc)
    conn = _Connection(logs=[{
        "created_at": stamp, "level": "INFO", "logger": "api",
        "message": "own project event", "project_id": "project-a", "run_id": None,
    }])
    # The call acts in ONE project — the one the identity gate decided (H1): an explicit
    # project_id argument names it, and the handler reads that project's rows.
    result = registry.invoke(
        "read_logs", _Ctx(edition, conn), project_id="project-z", actor="member",
        args={"service": "api", "project_id": "project-a"},
    )
    assert result["ok"] is True
    assert result["source"] == "app_logs (BOSS's own capture)"
    assert "own project event" in result["log"][0]
    assert len(conn.queries) == 1


@pytest.mark.parametrize("edition", ["community", "enterprise_self_hosted"])
def test_http_probe_executes_from_chat_registry_on_project_host_and_blocks_ssrf(edition):
    import http.server
    import threading

    class _Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            body = b'{"status":"healthy"}'
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_args):
            return

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        port = server.server_address[1]

        class _HostConn:
            def begin_nested(self):
                return contextmanager(lambda: (yield self))()

            def execute(self, statement, _params=None):
                sql = str(statement)
                if "FROM deployment_hosts" in sql:
                    return _Result(rows=[("127.0.0.1",)])
                if "FROM deployment_targets" in sql:
                    return _Result(rows=[])
                raise AssertionError(f"unexpected query: {sql}")

        chat_reg = build_discuss_read_registry(edition=edition)
        assert "http_probe" in chat_reg.names()
        assert "chat" in chat_reg.get("http_probe").exposure.surfaces

        ok = chat_reg.invoke(
            "http_probe", _Ctx(edition, _HostConn()), project_id="project-a",
            actor="member", args={"url": f"http://127.0.0.1:{port}/healthz"},
        )
        assert ok["ok"] is True, ok
        assert ok["status"] == 200
        assert "healthy" in ok["body_excerpt"]

        blocked = chat_reg.invoke(
            "http_probe", _Ctx(edition, _HostConn()), project_id="project-a",
            actor="member", args={"url": "http://169.254.169.254/latest/meta-data/"},
        )
        assert blocked["ok"] is False
        assert "metadata or link-local" in blocked["error"]
    finally:
        server.shutdown()
