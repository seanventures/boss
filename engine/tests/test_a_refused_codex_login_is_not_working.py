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


def test_a_401_on_a_run_flips_setup_and_raises_one_card(conn):
    _source(conn, "openai_codex_chatgpt", "openai_codex_chatgpt")
    _source(conn, "anthropic_claude_code", "anthropic_claude_code")
    row = _row(conn, "openai_codex_chatgpt")
    assert setup_service.last_check_broken(row["connection_metadata"],
                                           row["observed_capacity"]) is False

    card = harness_refusal.on_auth_refused(conn, conn.project_id, "crun_1", "codex", Settings())

    row = _row(conn, "openai_codex_chatgpt")
    assert setup_service.last_check_broken(row["connection_metadata"],
                                           row["observed_capacity"]) is True
    assert card
    rec = _cards(conn)[0]
    assert rec["action"] == "sign_in_to_subscription_again"
    assert rec["fallback_engine"] == "claude_code"
    found = setup_service.connected_engines(conn, Settings(), conn.project_id)
    assert found["codex"]["working"] is False
    assert harness_refusal.on_auth_refused(conn, conn.project_id, "crun_2", "codex",
                                           Settings()) is None
    assert len(_cards(conn)) == 1


def test_the_keepalive_and_the_run_share_one_card_per_dead_login(conn):
    from boss_engine.services import subscription_keepalive

    _source(conn, "openai_codex_chatgpt", "openai_codex_chatgpt")
    subscription_keepalive._raise_login_card(conn, conn.project_id, "openai_codex_chatgpt",
                                             "codex")
    assert harness_refusal.on_auth_refused(conn, conn.project_id, "crun_1", "codex",
                                           Settings()) is None
    assert len(_cards(conn)) == 1


def test_the_card_action_is_registered_and_worded():
    from boss_engine.services import decision_actions, decision_words

    assert harness_refusal.CARD_ACTION in decision_actions.KNOWN_ACTIONS
    assert harness_refusal.CARD_ACTION in decision_words._BY_ACTION


# -- the real hook: harness_failures.trip, provider vs app 401 ----------------------------------

class _Db:
    def __init__(self, c):
        self.c = c

    def begin(self):
        from contextlib import nullcontext
        return nullcontext(self.c)

    connect = begin


@pytest.fixture
def hook(conn, monkeypatch):
    from boss_engine.services import llm_telemetry

    llm_telemetry.set_llm_context(db_engine=_Db(conn), project_id=conn.project_id)
    yield conn
    llm_telemetry.set_llm_context()


PROVIDER_401 = ("2026-11-17T02:00:27Z ERROR codex_api::endpoint::responses_websocket: failed to "
                "connect to websocket: HTTP error: 401 Unauthorized, url: "
                "wss://api.openai.com/v1/responses")


def _verdict(message, *, where="engine", source_id="", transport=""):
    from boss_engine.services.harness_failures import HarnessVerdict

    return HarnessVerdict("codex", "auth_refused", message, "reconnect", where, source_id,
                          transport)


def _broken(c, sid):
    r = _row(c, sid)
    return setup_service.last_check_broken(r["connection_metadata"], r["observed_capacity"])


def test_a_provider_401_through_trip_flips_the_source(hook):
    from boss_engine.services import harness_failures

    _source(hook, "openai_codex_chatgpt", "openai_codex_chatgpt")
    harness_failures.trip(_verdict("x", transport=PROVIDER_401))
    assert _broken(hook, "openai_codex_chatgpt") is True
    assert len(_cards(hook)) == 1


def test_an_apps_own_401_in_a_summary_does_not_flip_the_source(hook):
    from boss_engine.services import harness_failures

    _source(hook, "openai_codex_chatgpt", "openai_codex_chatgpt")
    harness_failures.trip(_verdict("the test failed: expected 200, got 401 Unauthorized"))
    assert _broken(hook, "openai_codex_chatgpt") is False
    assert _cards(hook) == []


def test_a_refused_pool_account_does_not_flip_the_default_source(hook):
    from boss_engine.services import harness_failures

    _source(hook, "openai_codex_chatgpt", "openai_codex_chatgpt")
    harness_failures.trip(_verdict("x", source_id="pool:abc", transport=PROVIDER_401))
    assert _broken(hook, "openai_codex_chatgpt") is False
    assert _cards(hook)[0]["source_id"] == "pool:abc"


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


# -- failure_stands edges, and the lock ---------------------------------------------------------

def _state(home, at):
    body = {"state": "refresh_failed", "error": "http_401"}
    if at is not None:
        body["at"] = at
    (home / codex_login.STATE_NAME).write_text(json.dumps(body))


def test_a_failure_with_no_timestamp_stands(tmp_path, monkeypatch):
    home = _home(tmp_path, monkeypatch, last_refresh=_iso(60))
    _state(home, None)
    assert codex_login.failure_stands() is True


def test_a_naive_timestamp_is_read_as_utc(tmp_path, monkeypatch):
    home = _home(tmp_path, monkeypatch, last_refresh=_iso(3600))
    naive_after = (datetime.now(timezone.utc) - timedelta(seconds=60)).replace(tzinfo=None)
    _state(home, naive_after.isoformat())
    assert codex_login.failure_stands() is True      # failed after the login was issued
    naive_before = (datetime.now(timezone.utc) - timedelta(days=3)).replace(tzinfo=None)
    _state(home, naive_before.isoformat())
    assert codex_login.failure_stands() is False


def test_equal_timestamps_stand(tmp_path, monkeypatch):
    stamp = "2026-11-17T03:03:00+00:00"
    home = _home(tmp_path, monkeypatch, last_refresh=stamp)
    _state(home, stamp)
    assert codex_login.failure_stands() is True


def test_mark_revoked_takes_the_refreshers_lock(tmp_path, monkeypatch):
    _home(tmp_path, monkeypatch, last_refresh=_iso(60))
    taken = []
    real = codex_login._flock

    def spy(home, kind, timeout):
        taken.append(kind)
        return real(home, kind, timeout)

    monkeypatch.setattr(codex_login, "_flock", spy)
    codex_login.mark_revoked("invalidated oauth token")
    assert taken and codex_login.failure_stands() is True


# -- provider transport vs an app's words -------------------------------------------------------

@pytest.mark.parametrize("summary", [
    'Tests fail: POST /login -> 401 {"detail":"Failed to authenticate user"}',
    "test_dashboard_redirects FAILED - user not logged in, got 401",
    "expected 200, got 401 Unauthorized"])
def test_an_apps_own_401_words_are_not_a_provider_refusal(summary):
    assert harness_refusal.is_provider_refusal(_verdict(summary)) is False
    # even when the app's words sit in the CLI's stdout-side message, only stderr is read
    assert harness_refusal.is_provider_refusal(_verdict(summary, transport="")) is False


def test_a_long_summary_then_a_real_provider_401_in_stderr_is_a_refusal():
    summary = "I ran the suite and the checkout tests all passed. " * 12
    assert len(summary) > 300
    assert harness_refusal.is_provider_refusal(_verdict(summary, transport=PROVIDER_401)) is True


def test_the_claude_clis_login_line_is_a_refusal():
    assert harness_refusal.is_provider_refusal(
        _verdict("x", transport="OAuth token has expired. Please run /login")) is True


def test_generic_auth_words_in_stderr_alone_are_not_enough():
    assert harness_refusal.is_provider_refusal(
        _verdict("x", transport="authentication_error: failed to authenticate")) is False


# -- repeat refusals, open statuses, other surfaces ---------------------------------------------

def test_a_refusal_after_a_reconnect_and_retry_of_the_same_run_flips_setup_again(hook):
    from boss_engine.services import harness_failures, model_failures

    _source(hook, "openai_codex_chatgpt", "openai_codex_chatgpt")
    harness_failures.trip(_verdict("x", transport=PROVIDER_401))
    assert _broken(hook, "openai_codex_chatgpt") is True
    # the operator reconnects (a clean mark) and the card is answered
    model_failures.mark_login(hook, hook.project_id, "openai_codex_chatgpt", rejected=False)
    hook.execute(text("UPDATE decisions SET status = 'approved' WHERE project_id = :p"),
                 {"p": hook.project_id})
    assert _broken(hook, "openai_codex_chatgpt") is False
    harness_failures.trip(_verdict("x", transport=PROVIDER_401))   # same run, retried
    assert _broken(hook, "openai_codex_chatgpt") is True
    assert len(_cards(hook)) == 2


def test_a_delegated_card_still_counts_as_open(hook):
    from boss_engine.services import harness_failures

    _source(hook, "openai_codex_chatgpt", "openai_codex_chatgpt")
    harness_failures.trip(_verdict("x", transport=PROVIDER_401))
    hook.execute(text("UPDATE decisions SET status = 'delegated' WHERE project_id = :p"),
                 {"p": hook.project_id})
    harness_failures.trip(_verdict("x", transport=PROVIDER_401))
    assert len(_cards(hook)) == 1


def test_a_discovery_run_with_no_project_in_the_context_still_flips_its_source(conn):
    """A discovery (no code_task_runs row, no llm-context project) resolves its project from the
    discovery run; a run nothing knows is skipped, never guessed."""
    from boss_engine.services import harness_failures, llm_telemetry

    _source(conn, "openai_codex_chatgpt", "openai_codex_chatgpt")
    rid = f"disc_{uuid.uuid4().hex[:8]}"
    conn.execute(text("INSERT INTO discovery_runs (id, project_id, title) VALUES (:i, :p, 't')"),
                 {"i": rid, "p": conn.project_id})
    llm_telemetry.set_llm_context(db_engine=_Db(conn), project_id=None, run_id=rid)
    try:
        harness_failures.trip(_verdict("x", transport=PROVIDER_401))
        assert _broken(conn, "openai_codex_chatgpt") is True
    finally:
        llm_telemetry.set_llm_context()


def test_a_bare_provider_url_401_in_a_log_is_not_a_refusal():
    line = 'HTTP Request: POST https://api.openai.com/v1/chat/completions "HTTP/1.1 401 Unauthorized"'
    assert harness_refusal.is_provider_refusal(_verdict("x", transport=line)) is False


def test_the_code_agent_engine_error_path_flips_claude_codes_row(hook):
    """code_agent / agent_sdk_engine hand the CLI's own final error to note_engine_error (a
    refused login is only ever classified on that path; the live stderr stream never sees it)."""
    from boss_engine.services import harness_failures

    _source(hook, "anthropic_claude_code", "anthropic_claude_code")
    harness_failures.note_engine_error("Invalid API key \u00b7 Please run /login",
                                       engine="claude_code")
    assert _broken(hook, "anthropic_claude_code") is True
    assert len(_cards(hook)) == 1


def test_the_engine_error_path_flips_codexs_row(hook):
    from boss_engine.services import harness_failures

    _source(hook, "openai_codex_chatgpt", "openai_codex_chatgpt")
    harness_failures.note_engine_error(PROVIDER_401, engine="codex")
    assert _broken(hook, "openai_codex_chatgpt") is True
