"""MCP server DB config resolution & clear error reporting (BOSSAPP-991).

Proves:
  1. MCP resolves the same install config / `.env` DB URL as the API (including Community
     compose's `BOSS_POSTGRES_PORT` + `BOSS_DB_PASSWORD`, as well as `DATABASE_URL`).
  2. Precedence: env `DATABASE_URL` > env `BOSS_DATABASE_URL` > install `.env` > default.
  3. A wrong/unreachable DB fails with a clear message naming the configured source and the
     sanitized target, never leaking the password.
  4. Non-default port `DATABASE_URL` (tested against the test Postgres port 55700 when
     reachable, and an unreachable non-default port) routes cleanly.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
from sqlalchemy import text

from boss_engine.context import EngineContext
from boss_engine.mcp.server import (
    MCPServer,
    format_database_error,
    load_mcp_settings,
    resolve_database_config,
    sanitize_database_url,
)
from boss_engine.tools.registry import ToolRegistry, ToolSpec


def test_resolve_reads_env_database_url_on_non_default_port(tmp_path: Path):
    env = {"DATABASE_URL": "postgresql+psycopg://boss:secret123@127.0.0.1:55700/boss_test"}
    cfg = resolve_database_config(env, cwd=tmp_path, search_defaults=False)
    assert cfg.configured is True
    assert cfg.source == "env:DATABASE_URL"
    assert cfg.url == env["DATABASE_URL"]
    assert cfg.sanitized_target == "boss@127.0.0.1:55700/boss_test"
    assert "secret123" not in cfg.sanitized_target


def test_resolve_reads_install_dotenv_compose_port_and_password(tmp_path: Path):
    install = tmp_path / "install"
    install.mkdir()
    (install / ".env").write_text(
        "# community install\n"
        "BOSS_POSTGRES_PORT=55700\n"
        "BOSS_DB_PASSWORD='p@ss#word'\n"
        "BOSS_AUTHZ_MODE=shadow\n",
        encoding="utf-8",
    )
    cfg = resolve_database_config(
        {"BOSS_INSTALL_DIR": str(install)}, cwd=tmp_path, search_defaults=False
    )
    assert cfg.configured is True
    assert str(install / ".env") in cfg.source
    assert "BOSS_POSTGRES_PORT" in cfg.source
    assert "BOSS_DB_PASSWORD" in cfg.source
    assert ":55700/boss" in cfg.url
    assert "p%40ss%23word" in cfg.url
    assert "p@ss#word" not in cfg.sanitized_target

    settings, loaded_cfg = load_mcp_settings(
        environ={"BOSS_INSTALL_DIR": str(install)}, cwd=tmp_path, search_defaults=False
    )
    assert loaded_cfg.source == cfg.source
    assert settings.database_url == cfg.url


def test_resolve_reads_dotenv_database_url_from_cwd(tmp_path: Path):
    (tmp_path / ".env").write_text(
        "DATABASE_URL=postgresql+psycopg://app:pw@127.0.0.1:55432/customdb\n",
        encoding="utf-8",
    )
    cfg = resolve_database_config({}, cwd=tmp_path, search_defaults=False)
    assert cfg.configured is True
    assert cfg.source == f"cwd_env:{tmp_path / '.env'}:DATABASE_URL"
    assert cfg.sanitized_target == "app@127.0.0.1:55432/customdb"


def test_env_database_url_wins_over_install_dotenv(tmp_path: Path):
    (tmp_path / ".env").write_text(
        "BOSS_POSTGRES_PORT=5439\nBOSS_DB_PASSWORD=from_file\n",
        encoding="utf-8",
    )
    env = {"DATABASE_URL": "postgresql+psycopg://boss:from_env@127.0.0.1:55700/boss_env"}
    cfg = resolve_database_config(env, cwd=tmp_path, search_defaults=False)
    assert cfg.source == "env:DATABASE_URL"
    assert cfg.sanitized_target == "boss@127.0.0.1:55700/boss_env"


def test_unset_config_names_default_source(tmp_path: Path):
    cfg = resolve_database_config({}, cwd=tmp_path, search_defaults=False)
    assert cfg.configured is False
    assert cfg.source == "default:Settings.database_url"


def test_wrong_db_fails_with_clear_message_naming_source_and_scrubbing_secret(tmp_path: Path):
    bad_url = "postgresql+psycopg://boss:super_secret_pw@127.0.0.1:1/nonexistent_db"
    err = format_database_error(
        RuntimeError("connection to server failed for super_secret_pw"),
        database_url=bad_url,
        source="env:DATABASE_URL",
    )
    assert "configured_source=env:DATABASE_URL" in err
    assert "boss@127.0.0.1:1/nonexistent_db" in err
    assert "super_secret_pw" not in err
    assert "[REDACTED]" in err


def test_mcp_tool_call_on_wrong_port_names_configured_source(monkeypatch, tmp_path: Path):
    bad_url = "postgresql+psycopg://boss:topsecretpw@127.0.0.1:1/wrong_db"
    monkeypatch.setenv("DATABASE_URL", bad_url)
    monkeypatch.setenv("BOSS_AUTHZ_MODE", "off")
    ctx = EngineContext.create(cwd=tmp_path, search_defaults=False)
    assert ctx.database_source == "env:DATABASE_URL"
    assert ctx.durable_workspace_configured is True

    db_err = ctx.database_error()
    assert db_err is not None
    assert "configured_source=env:DATABASE_URL" in db_err
    assert "boss@127.0.0.1:1/wrong_db" in db_err
    assert "topsecretpw" not in db_err

    registry = ToolRegistry()

    def _touch_db(c, *, project_id, actor, args):
        with c.begin() as conn:
            conn.execute(text("SELECT 1"))
        return {"ok": True}

    registry.register(ToolSpec(
        name="db_probe",
        kind="read",
        description="Touch the database",
        parameters={"type": "object", "properties": {}},
        handler=_touch_db,
        permission="system:read",
    ))
    server = MCPServer(ctx, project_id="p1", registry=registry)
    resp = server.handle({
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {"name": "db_probe", "arguments": {}},
    })
    assert resp is not None
    result = resp["result"]
    assert result["isError"] is True
    payload = json.loads(result["content"][0]["text"])
    assert payload["ok"] is False
    assert "configured_source=env:DATABASE_URL" in payload["error"]
    assert "boss@127.0.0.1:1/wrong_db" in payload["error"]
    assert "topsecretpw" not in payload["error"]


def test_mcp_server_connects_to_non_default_port_database(monkeypatch, tmp_path: Path):
    url = os.environ.get("BOSS_TEST_DATABASE_URL") or os.environ.get("DATABASE_URL") or ""
    if not url or "5432" in sanitize_database_url(url):
        pytest.skip("requires a reachable Postgres on a non-default port (e.g. 55700)")
    monkeypatch.setenv("DATABASE_URL", url)
    monkeypatch.setenv("BOSS_AUTHZ_MODE", "off")

    ctx = EngineContext.create(cwd=tmp_path, search_defaults=False)
    assert ctx.database_source == "env:DATABASE_URL"
    assert ctx.database_error() is None

    registry = ToolRegistry()

    def _ping_db(c, *, project_id, actor, args):
        with c.begin() as conn:
            val = conn.execute(text("SELECT 1")).scalar()
        return {"ok": True, "ping": int(val)}

    registry.register(ToolSpec(
        name="db_ping",
        kind="read",
        description="Verify DB reachability",
        parameters={"type": "object", "properties": {}},
        handler=_ping_db,
        permission="system:read",
    ))
    server = MCPServer(ctx, project_id="p1", registry=registry)
    resp = server.handle({
        "jsonrpc": "2.0",
        "id": 7,
        "method": "tools/call",
        "params": {"name": "db_ping", "arguments": {}},
    })
    assert resp is not None
    assert resp["result"]["isError"] is False
    payload = json.loads(resp["result"]["content"][0]["text"])
    assert payload["ok"] is True
    assert payload["ping"] == 1
    assert payload["tool"] == "db_ping"
