"""BOSSAPP-1347: Codex's ChatGPT login 'silently stopped working'.

Laptop, 2026-11-16/17: a refresh_failed verdict recorded on 7 Nov stayed in the control plane's
state file forever, so after the operator signed in afresh every run's sandbox was started
WITHOUT the login (sandbox_identity refuses to seed a login flagged dead) and codex failed 401 on
wss://api.openai.com/v1/responses, while the Setup proof (which reads the control plane's own
login) passed and Setup kept showing Working. Pinned here:
  1. a failure belongs to one login: a newer login supersedes it, and runs get seeded;
  2. a refresh written during a run reaches the store, newest wins, and a stale copy never
     overwrites a newer store copy;
  3. a run's 401 flips the Setup row to not working and raises a Reconnect / fallback card.
No network, no real token: markers only.
"""
from __future__ import annotations

import json
import os
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.exc import SQLAlchemyError

from boss_engine.config import Settings
from boss_engine.services import cli_harness, codex_login, harness_refusal, sandbox_identity
from boss_engine.services import setup_service


def _iso(seconds_ago: float) -> str:
    return (datetime.now(timezone.utc) - timedelta(seconds=seconds_ago)).isoformat()


def _home(tmp_path: Path, monkeypatch, *, last_refresh: str, refresh: str = "RT") -> Path:
    home = tmp_path / "codex-home"
    home.mkdir(mode=0o700)
    (home / "auth.json").write_text(json.dumps({
        "auth_mode": "chatgpt", "last_refresh": last_refresh,
        "tokens": {"access_token": "AT", "refresh_token": refresh, "id_token": "ID"}}))
    monkeypatch.setenv("CODEX_HOME", str(home))
    monkeypatch.setattr(sandbox_identity, "current", lambda: None)
    return home


# -- 1. a failure belongs to one login ---------------------------------------------------------

def test_a_failure_recorded_before_a_new_sign_in_no_longer_stands(tmp_path, monkeypatch):
    home = _home(tmp_path, monkeypatch, last_refresh=_iso(3600))
    (home / codex_login.STATE_NAME).write_text(json.dumps(
        {"state": "refresh_failed", "error": "http_401", "at": _iso(10 * 86400)}))
    assert codex_login.failure_stands() is False
    assert codex_login.status()["state"] == codex_login.OK


def test_a_failure_after_the_login_still_stands(tmp_path, monkeypatch):
    home = _home(tmp_path, monkeypatch, last_refresh=_iso(86400))
    codex_login.mark_revoked("invalidated oauth token")
    assert codex_login.failure_stands() is True
    assert codex_login.status()["state"] == codex_login.REFRESH_FAILED


def test_a_run_is_seeded_after_the_operator_signs_in_again(tmp_path, monkeypatch):
    home = _home(tmp_path, monkeypatch, last_refresh=_iso(60))
    (home / codex_login.STATE_NAME).write_text(json.dumps(
        {"state": "refresh_failed", "error": "http_401", "at": _iso(10 * 86400)}))
    run_home = tmp_path / "run"
    run_home.mkdir()
    ident = SimpleNamespace(home=run_home, key="k")
    sandbox_identity._seed_codex_login(ident)
    seeded = json.loads((run_home / ".codex" / "auth.json").read_text())
    assert seeded["tokens"]["access_token"] == "AT", "the run got the current login"
    assert seeded["tokens"]["refresh_token"] == sandbox_identity.RUN_REFRESH_TOKEN, \
        "and never the real refresh token"


def test_a_login_flagged_dead_after_it_was_issued_is_still_not_handed_out(tmp_path, monkeypatch):
    _home(tmp_path, monkeypatch, last_refresh=_iso(3600))
    codex_login.mark_revoked("refresh token was revoked")
    run_home = tmp_path / "run"
    run_home.mkdir()
    sandbox_identity._seed_codex_login(SimpleNamespace(home=run_home, key="k"))
    assert not (run_home / ".codex" / "auth.json").exists()


# -- 2. write-back: newest wins ----------------------------------------------------------------

STORE_KEY = "BOSS_SUBSCRIPTION_CODEX_AUTH_JSON"


class _Store:
    def __init__(self, values):
        self.values = dict(values)

    def get(self, key):
        return self.values[key]

    def put(self, key, value):
        self.values[key] = value


def _doc(last_refresh: str, access: str) -> str:
    return json.dumps({"last_refresh": last_refresh, "tokens": {"access_token": access}})


@pytest.fixture
def store(monkeypatch):
    monkeypatch.delenv("CODEX_AUTH_JSON", raising=False)
    s = _Store({})
    monkeypatch.setattr("boss_engine.services.secret_backends.default_secret_backend",
                        lambda settings: "database")
    monkeypatch.setattr("boss_engine.services.secret_backends.backend_for",
                        lambda kind, settings: s)
    return s


def _sync(tmp_path, monkeypatch, file_doc: str):
    home = tmp_path / "h"
    home.mkdir(exist_ok=True)
    (home / "auth.json").write_text(file_doc)
    spec = cli_harness.load_specs(Settings())["codex"]
    return cli_harness.sync_rotated_credential(spec, Settings(), cli_home=str(home))


def test_a_token_refreshed_during_a_run_is_written_back_to_the_store(tmp_path, monkeypatch, store):
    store.values[STORE_KEY] = _doc("2026-11-16T14:25:00Z", "old")
    out = _sync(tmp_path, monkeypatch, _doc("2026-11-17T03:03:00Z", "new"))
    assert out["state"] == "wrote"
    assert json.loads(store.values[STORE_KEY])["tokens"]["access_token"] == "new"


def test_a_stale_copy_never_overwrites_a_newer_store_copy(tmp_path, monkeypatch, store):
    store.values[STORE_KEY] = _doc("2026-11-17T03:03:00Z", "newer")
    out = _sync(tmp_path, monkeypatch, _doc("2026-11-16T14:25:00Z", "stale"))
    assert out["state"] == "store_newer"
    assert json.loads(store.values[STORE_KEY])["tokens"]["access_token"] == "newer"


def test_the_control_plane_refresh_syncs_the_store(tmp_path, monkeypatch):
    home = _home(tmp_path, monkeypatch, last_refresh=_iso(8 * 86400))
    synced = []
    monkeypatch.setattr(codex_login, "_store_sync_after_refresh", lambda: synced.append(1) or True)
    out = codex_login.refresh_if_due(post=lambda url, body: (200, {
        "access_token": "AT2", "refresh_token": "RT2", "expires_in": 3600}))
    assert out["result"] == "refreshed" and synced == [1]


# -- 3. a 401 flips Setup and offers a way on --------------------------------------------------

@pytest.fixture(scope="module")
def pg_engine():
    url = os.environ.get("DATABASE_URL") or os.environ.get("BOSS_TEST_DATABASE_URL") or ""
    name = url.rsplit("/", 1)[-1].split("?", 1)[0].lower()
    if not url or not any(m in name for m in ("test", "ci", "scratch", "tmp", "boss_run")):
        pytest.skip("a disposable database is required")
    engine = create_engine(url, future=True)
    try:
        with engine.connect() as c:
            c.execute(text("SELECT 1"))
    except SQLAlchemyError as exc:
        pytest.skip(f"postgres is not reachable: {exc.__class__.__name__}")
    from boss_engine.db import ensure_schema
    ensure_schema(engine)
    return engine


@pytest.fixture
def conn(pg_engine):
    with pg_engine.connect() as c:
        tx = c.begin()
        org, ws, proj = (f"{p}_{uuid.uuid4().hex[:8]}" for p in ("org", "ws", "proj"))
        c.execute(text("INSERT INTO orgs (id, name) VALUES (:o, 'T')"), {"o": org})
        c.execute(text("INSERT INTO workspaces (id, org_id, name) VALUES (:w, :o, 'T')"),
                  {"w": ws, "o": org})
        c.execute(text("INSERT INTO projects (id, workspace_id, name, slug) "
                       "VALUES (:p, :w, 'T', :p)"), {"p": proj, "w": ws})
        c.project_id = proj  # type: ignore[attr-defined]
        try:
            yield c
        finally:
            tx.rollback()


def _source(c, source_id: str, provider: str):
    c.execute(text(
        "INSERT INTO entitlement_sources (id, project_id, provider, source_id, source_kind,"
        " entitlement_kind, owner_type, owner_id, auth_method, connection_status)"
        " VALUES (:id, :p, :prov, :sid, 'subscription', 'quota', 'project', :p,"
        " 'oauth_subscription', 'connected')"),
        {"id": f"ent_{uuid.uuid4().hex[:8]}", "p": c.project_id, "prov": provider, "sid": source_id})


def _row(c, source_id):
    return c.execute(text("SELECT observed_capacity, connection_metadata FROM entitlement_sources"
                          " WHERE project_id=:p AND source_id=:s"),
                     {"p": c.project_id, "s": source_id}).mappings().one()


def _cards(c):
    return c.execute(text("SELECT recommendation FROM decisions WHERE project_id=:p"),
                     {"p": c.project_id}).scalars().all()


def test_a_401_on_a_run_flips_setup_and_offers_reconnect_or_a_fallback(conn):
    _source(conn, "openai_codex_chatgpt", "openai_codex_chatgpt")
    _source(conn, "anthropic_claude_code", "anthropic_claude_code")
    assert setup_service.last_check_broken(
        _row(conn, "openai_codex_chatgpt")["connection_metadata"],
        _row(conn, "openai_codex_chatgpt")["observed_capacity"]) is False

    card = harness_refusal.on_auth_refused(conn, conn.project_id, "crun_1", "codex", Settings())

    row = _row(conn, "openai_codex_chatgpt")
    assert setup_service.last_check_broken(row["connection_metadata"],
                                           row["observed_capacity"]) is True
    assert card
    rec = _cards(conn)[0]
    assert rec["action"] == harness_refusal.CARD_ACTION
    ids = [o["id"] for o in rec["options"]]
    assert ids == ["reconnect", "use_fallback"]
    assert rec["fallback_engine"] == "claude_code"
    # Setup's engine list now offers codex as not working.
    found = setup_service.connected_engines(conn, Settings(), conn.project_id)
    assert found["codex"]["working"] is False
    # A second refusal does not stack a second card.
    assert harness_refusal.on_auth_refused(conn, conn.project_id, "crun_2", "codex",
                                           Settings()) is None
    assert len(_cards(conn)) == 1


def test_without_a_fallback_the_card_offers_reconnect_only(conn):
    _source(conn, "openai_codex_chatgpt", "openai_codex_chatgpt")
    harness_refusal.on_auth_refused(conn, conn.project_id, "crun_1", "codex", Settings())
    assert [o["id"] for o in _cards(conn)[0]["options"]] == ["reconnect"]


# -- the same class for Claude Code, and for a named account -----------------------------------

def test_a_401_on_a_claude_code_run_flips_its_row_and_offers_codex(conn):
    _source(conn, "anthropic_claude_code", "anthropic_claude_code")
    _source(conn, "openai_codex_chatgpt", "openai_codex_chatgpt")
    assert harness_refusal.on_auth_refused(conn, conn.project_id, "crun_1", "claude_code",
                                           Settings())
    row = _row(conn, "anthropic_claude_code")
    assert setup_service.last_check_broken(row["connection_metadata"],
                                           row["observed_capacity"]) is True
    other = _row(conn, "openai_codex_chatgpt")
    assert setup_service.last_check_broken(other["connection_metadata"],
                                           other["observed_capacity"]) is False
    assert _cards(conn)[0]["fallback_engine"] == "codex"


def test_a_refusal_is_recorded_on_the_named_account_only(conn):
    _source(conn, "openai_codex_chatgpt", "openai_codex_chatgpt")
    _source(conn, "openai_codex_chatgpt_acct2", "openai_codex_chatgpt")
    harness_refusal.on_auth_refused(conn, conn.project_id, "crun_1", "codex", Settings(),
                                    source_id="openai_codex_chatgpt_acct2")
    two = _row(conn, "openai_codex_chatgpt_acct2")
    one = _row(conn, "openai_codex_chatgpt")
    assert setup_service.last_check_broken(two["connection_metadata"], two["observed_capacity"])
    assert not setup_service.last_check_broken(one["connection_metadata"], one["observed_capacity"])


def test_a_claude_failure_belongs_to_one_login_too(tmp_path, monkeypatch):
    """claude_login binds refresh_failed to the login's expiry: a new sign-in supersedes it."""
    from boss_engine.services import claude_login

    home = tmp_path / "claude"
    home.mkdir()
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(home))
    import time
    exp = int((time.time() + 7200) * 1000)
    (home / ".credentials.json").write_text(json.dumps(
        {"claudeAiOauth": {"accessToken": "A", "refreshToken": "R", "expiresAt": exp}}))
    (home / claude_login.STATE_NAME).write_text(json.dumps(
        {"state": "refresh_failed", "error": "http_401", "for_expires_at": exp - 99999}))
    assert claude_login.status()["state"] != claude_login.REFRESH_FAILED
