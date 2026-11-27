"""BOSSAPP-1437: preview DB migration detection & sandboxed repair."""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from boss_engine.config import Settings
from boss_engine.services import feedback_agent, infra_view, preview_service


def test_classify_start_failure_detects_migration_needed_before_boot_crash():
    for msg in (
        'psycopg.errors.UndefinedTable: relation "items" does not exist',
        'PrismaClientKnownRequestError: P2021 The table `public.items` does not exist',
        'alembic.util.exc.CommandError: Target database is not up to date.',
        'PendingMigrationError: Migrations are pending',
        'sqlite3.OperationalError: no such table: items',
        'You have 3 unapplied migration(s)',
    ):
        kind, advice = preview_service._classify_start_failure(msg, exited=True)
        assert kind == "migration_needed", (msg, kind)
        assert "migration" in advice.lower()

    # Non-DB "does not exist" must NOT be misclassified as migration_needed.
    kind, _ = preview_service._classify_start_failure(
        "FileNotFoundError: config.yaml does not exist", exited=True)
    assert kind == "boot_crash"


def test_infra_view_has_migration_needed_word():
    w = infra_view.word("failure_kind", "migration_needed")
    assert w == {"word": "Needs DB migration", "tone": "amber"}
    assert len(w["word"].split()) <= 4


def _setup_preview_harness(monkeypatch, tmp_path: Path, *, app_files: dict[str, str]):
    preview_root = tmp_path / "previews"
    preview_root.mkdir()
    monkeypatch.setattr(preview_service, "PREVIEW_ROOT", preview_root)

    run_ws = tmp_path / "crun_mig_1"
    run_ws.mkdir()
    for rel, content in app_files.items():
        target = run_ws / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")

    inserted: list[dict] = []
    emitted: list[str] = []

    class FakeConn:
        def execute(self, stmt, params=None):
            sql_str = str(stmt)
            if "INSERT INTO app_previews" in sql_str and params:
                inserted.append(dict(params))
            res = MagicMock()
            res.mappings.return_value.first.return_value = None
            return res

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    monkeypatch.setattr(preview_service, "begin", lambda _eng: FakeConn())
    monkeypatch.setattr(
        preview_service, "load_code_task_run",
        lambda _c, rid: {
            "id": rid, "project_id": "proj_1",
            "pull_request_url": "",
            "spec": {"owner": "acme", "repo": "mig-app"},
            "state": {"tests_passed": True, "impl_files": list(app_files)},
            "finalize_ctx": {"work_branch": "boss/code/crun_mig_1"},
        },
    )
    monkeypatch.setattr(preview_service, "find_local_run_workspace", lambda _rid: run_ws)
    monkeypatch.setattr(preview_service, "_preview_sandbox", lambda _pid: None)
    monkeypatch.setattr(preview_service, "_release_preview_sandbox", lambda _pid: None)
    monkeypatch.setattr(preview_service, "provision_preview_database", lambda *a, **k: None)
    monkeypatch.setattr(preview_service, "keep_preview_data", lambda *a, **k: [])
    monkeypatch.setattr(preview_service, "record_demo_logins", lambda *a, **k: [])
    monkeypatch.setattr(preview_service, "_record_repo_manifests", lambda *a, **k: None)
    monkeypatch.setattr(preview_service, "emit_event", lambda _c, _p, et, *a, **k: emitted.append(et))
    return inserted, emitted


def test_start_preview_repairs_cold_db_via_migration_and_launches_healthy(monkeypatch, tmp_path):
    db_file = tmp_path / "preview.sqlite"
    app_files = {
        "server.py": f"""
import http.server, os, sqlite3, sys
conn = sqlite3.connect({str(db_file)!r})
try:
    conn.execute("SELECT id FROM items LIMIT 1").fetchall()
except sqlite3.OperationalError as exc:
    print(f'psycopg.errors.UndefinedTable: relation "items" does not exist ({{exc}})', flush=True)
    sys.exit(1)

class H(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"ok")
    def log_message(self, *a):
        pass

http.server.HTTPServer(("127.0.0.1", int(os.environ["PORT"])), H).serve_forever()
""",
        "package.json": json.dumps({
            "name": "mig-app",
            "scripts": {
                "db:migrate": (
                    f"python3 -c \"import sqlite3; c=sqlite3.connect({str(db_file)!r}); "
                    "c.execute('CREATE TABLE IF NOT EXISTS items (id INTEGER PRIMARY KEY)'); c.commit()\""
                ),
            },
        }),
        "boss.preview.command": "python3 server.py",
    }
    inserted, emitted = _setup_preview_harness(monkeypatch, tmp_path, app_files=app_files)

    result = preview_service.start_preview(
        MagicMock(), Settings(), "crun_mig_1", ready_timeout=10.0)
    try:
        assert result["status"] == "running", result
        assert "app_preview.migration_repaired" in emitted
        assert any(row.get("port") == result["port"] for row in inserted)
    finally:
        if result.get("pid"):
            preview_service._kill(result["pid"])

    with sqlite3.connect(db_file) as c:
        tables = [r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()]
    assert "items" in tables


def test_start_preview_without_migration_command_fails_as_migration_needed(monkeypatch, tmp_path):
    app_files = {
        "server.py": """
import sys
print('psycopg.errors.UndefinedTable: relation "items" does not exist', flush=True)
sys.exit(1)
""",
        "boss.preview.command": "python3 server.py",
    }
    inserted, _emitted = _setup_preview_harness(monkeypatch, tmp_path, app_files=app_files)

    result = preview_service.start_preview(
        MagicMock(), Settings(), "crun_mig_1", ready_timeout=10.0)
    assert result["status"] == "failed"
    assert result["failure_kind"] == "migration_needed"
    assert inserted and inserted[-1]["fk"] == "migration_needed"


def test_start_preview_broken_migration_is_bounded_to_one_attempt(monkeypatch, tmp_path):
    app_files = {
        "server.py": """
import sys
print('psycopg.errors.UndefinedTable: relation "items" does not exist', flush=True)
sys.exit(1)
""",
        "package.json": json.dumps({"scripts": {"db:migrate": "echo migration failed >&2; exit 2"}}),
        "boss.preview.command": "python3 server.py",
    }
    _setup_preview_harness(monkeypatch, tmp_path, app_files=app_files)

    calls = 0
    real_run = preview_service._run_preview_migration

    def counted(*args, **kwargs):
        nonlocal calls
        calls += 1
        return real_run(*args, **kwargs)

    monkeypatch.setattr(preview_service, "_run_preview_migration", counted)

    result = preview_service.start_preview(
        MagicMock(), Settings(), "crun_mig_1", ready_timeout=10.0)
    assert calls == 1
    assert result["status"] == "failed"
    assert result["failure_kind"] == "migration_needed"


def test_submit_preview_feedback_names_failure_kind_when_not_running(monkeypatch):
    class FakeConn:
        def execute(self, stmt, params=None):
            res = MagicMock()
            res.mappings.return_value.first.return_value = {
                "run_id": "crun_1", "status": "failed", "failure_kind": "migration_needed",
            }
            return res

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    monkeypatch.setattr(feedback_agent, "begin", lambda _eng: FakeConn())
    with pytest.raises(ValueError, match=r"preview is not running \(migration_needed\)"):
        feedback_agent.submit_preview_feedback(MagicMock(), "prev_1", {"message": "fix"})
