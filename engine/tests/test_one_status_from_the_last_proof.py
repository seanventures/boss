"""One status, from the last real proof, on every surface (BOSSAPP-769, audit P0-3/P0-4/P1-6/P1-7/P2-10).

THE LIVE BUG. Codex's last proof failed `401 invalid_refresh_token`. The Models row said Login
expired; the Sources list on the same page said Active · Available; `boss connect --summary`
said "Codex connected"; GET /v1/setup/coding-agents listed it as connected. Four surfaces, four
judges. Now ONE function decides (`setup_view.source_state`: last real proof first, then the
credential) and every surface reads it. These tests hold each surface to it, plus:

- honest detection: installed-but-signed-out is `needs_sign_in`, never `not_on_server`; a
  legacy stored Claude token is `legacy_token`, never "signed in"; no fix text sends anyone to
  a refused import (setup-token, auth.json, store_subscription_token);
- proofs that prove: exit 0 is not enough without `BOSS_OK`, and a CLI whose own sign-in failed
  before any model call is a known rejection — nothing held, the pill says Login expired.
"""
from __future__ import annotations

import json
import os
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path

import pytest
from sqlalchemy import create_engine, text

from boss_engine.config import Settings
from boss_engine.services import cli_harness, setup_pills, setup_service, setup_view
from boss_engine.services.entitlements import EntitlementBrokerService
from boss_engine.services.setup_service import with_recorded_connections

ROOT = Path(__file__).resolve().parents[2]
NOW = datetime(2026, 10, 24, 12, 0, tzinfo=timezone.utc)
REFUSED_IMPORTS = ("setup-token", "auth.json", "store_subscription_token")


class _Settings:
    github_token = ""
    target_github_owner = ""
    target_github_repo = ""
    openai_api_key = ""
    openai_base_url = ""
    cli_harnesses_json = ""


@pytest.fixture()
def connection(entitlement_tables, setup_tables):
    engine = create_engine("sqlite+pysqlite:///:memory:")
    with engine.begin() as conn:
        setup_tables(conn)
        entitlement_tables(conn)
    with engine.begin() as conn:
        yield conn


def _codex(conn, *, metadata=None, capacity=None, status="declared"):
    """The live Codex row: connected subscription, last proof refused."""
    conn.execute(text(
        "INSERT INTO entitlement_sources (project_id, source_id, provider, auth_method,"
        " connection_status, status, credential_secret_ref_id, connection_metadata,"
        " observed_capacity, updated_at, created_at, cost_mode)"
        " VALUES ('p', 'openai_codex_chatgpt', 'openai', 'official_cli_oauth', 'connected',"
        " :status, NULL, :m, :c, :at, :at, 'subscription')"),
        {"m": json.dumps(metadata or {}), "c": json.dumps(capacity or {}), "at": NOW,
         "status": status})


LIVE_CODEX = {"metadata": {"last_check": {"ok": False, "state": "failed",
                                          "at": "2026-10-24T05:21:30+00:00"}},
              "capacity": {"credential_state": "rejected", "credential_present": True}}


def _models(conn):
    return {m["source_id"]: m for m in setup_view.build_basics(conn, _Settings(), "p")["models"]}


# ── P0-3: one status, every surface ──────────────────────────────────────────────────────────

@pytest.mark.parametrize("row", [
    LIVE_CODEX,
    {"metadata": {"last_check": {"ok": False, "state": "login_expired",
                                 "at": "2026-10-24T05:21:30+00:00"}}},
])
def test_a_refused_login_is_red_on_every_surface(connection, row):
    _codex(connection, **row)

    # The Models row.
    pill = _models(connection)["openai_codex_chatgpt"]["pill"]
    assert (pill["state"], pill["tone"], pill["label"]) == ("login_expired", "red",
                                                             "Login expired")
    assert pill["action"] == "sign_in"
    assert "boss connect codex" in pill["tip"]

    # The Sources row's pill: the SAME pill.
    sources = setup_view.source_pills(connection, _Settings(), "p")
    assert sources["openai_codex_chatgpt"] == {
        "state": "login_expired", "label": "Login expired", "tone": "red",
        "checked_at": _models(connection)["openai_codex_chatgpt"]["checked_at"]}

    # The Sources row's effective status: never "active" over a refused login.
    broker = EntitlementBrokerService(Settings())
    source = {"source_id": "openai_codex_chatgpt", "status": "declared",
              "cost_mode": "subscription", "provider": "openai",
              "entitlement_kind": "subscription", "source_kind": "official_cli",
              "auth_method": "official_cli_oauth", "connection_status": "connected",
              "runtime_supported": True, "connection_metadata": row["metadata"],
              "observed_capacity": {"credential_present": True, **row.get("capacity", {})}}
    assert broker._effective_status(source) == "login_rejected"

    # GET /v1/setup/coding-agents and the agent's detect_coding_agents (both join the record).
    agents = [{"name": "codex", "ready": True, "next_step": None,
               "entitlement_source_id": "openai_codex_chatgpt"}]
    [codex] = with_recorded_connections(agents, connection, "p", settings=_Settings())
    assert codex["connected"] is False and codex["ready"] is False
    assert codex["pill"]["state"] == "login_expired"
    assert "boss connect codex" in codex["next_step"]


def test_a_working_proof_stays_connected_and_active(connection):
    _codex(connection, metadata={"last_check": {"ok": True, "state": "working",
                                                "at": "2026-10-24T05:21:30+00:00"}},
           capacity={"credential_state": "ok"})
    agents = [{"name": "codex", "ready": True, "next_step": None,
               "entitlement_source_id": "openai_codex_chatgpt"}]
    [codex] = with_recorded_connections(agents, connection, "p", settings=_Settings())
    assert codex["connected"] is True and codex["ready"] is True
    assert codex["pill"]["state"] == "working"
    source = {"source_id": "openai_codex_chatgpt", "status": "declared",
              "cost_mode": "subscription", "provider": "openai",
              "entitlement_kind": "subscription", "source_kind": "official_cli",
              "auth_method": "official_cli_oauth", "connection_status": "connected",
              "runtime_supported": True,
              "connection_metadata": {"last_check": {"ok": True, "state": "working"}},
              "observed_capacity": {"credential_present": True, "credential_state": "ok"}}
    assert EntitlementBrokerService(Settings())._effective_status(source) == "active"


def test_a_session_completed_without_a_proof_is_not_checked(connection):
    """Sources › Sign in › Complete session marks the source connected with any typed ref. It
    answered nothing, so it is Not checked — never Working."""
    _codex(connection)
    pill = _models(connection)["openai_codex_chatgpt"]["pill"]
    assert pill["state"] == "not_checked"
    assert setup_view.source_pills(connection, _Settings(), "p")[
        "openai_codex_chatgpt"]["state"] == "not_checked"


def test_a_source_with_no_models_row_has_no_pill(connection):
    connection.execute(text(
        "INSERT INTO entitlement_sources (project_id, source_id, provider, connection_status,"
        " status, cost_mode, connection_metadata, observed_capacity, updated_at, created_at)"
        " VALUES ('p', 'xai_grok_build', 'xai', 'not_connected', 'declared', 'subscription',"
        " '{}', '{}', :at, :at)"), {"at": NOW})
    assert "xai_grok_build" not in setup_view.source_pills(connection, _Settings(), "p")


def test_every_surface_reads_the_one_function():
    """Structural: the decision lives once. The Models row, the Sources pill, the effective
    status and the coding-agent list all reach `source_state`; no copy of its rules remains."""
    import inspect

    from boss_engine.services import entitlements

    # A connected subscription with no proof was a second judge of "Working".
    assert 'kind == "subscription" and connected' not in inspect.getsource(setup_view)
    assert "source_state(" in inspect.getsource(setup_view.source_entry)
    assert "source_entry(" in inspect.getsource(setup_view.source_pills)
    assert "source_entry(" in inspect.getsource(setup_view._models)
    assert "source_state(" in inspect.getsource(
        entitlements.EntitlementBrokerService._credential_refused)
    assert "source_pills(" in inspect.getsource(with_recorded_connections)
    from boss_engine.tools import builtin

    assert "with_recorded_connections(" in inspect.getsource(builtin)
    main = (ROOT / "apps" / "api" / "app" / "main.py").read_text()
    route = main[main.index("def list_entitlement_sources("):][:1200]
    assert "source_pills(" in route


# ── P2-10: boss connect --summary tells the truth ────────────────────────────────────────────

def _model_status():
    sys.path.insert(0, str(ROOT / "scripts" / "install"))
    try:
        import model_status
    finally:
        sys.path.pop(0)
    return model_status


#: This install's GET /v1/setup, trimmed (2026-10-24): a console-stored Anthropic key Working,
#: Codex's login refused, Build on the Claude Agent SDK.
LIVE_VIEW = {
    "models": [
        {"source_id": "openai_codex_chatgpt", "provider": "codex", "label": "Codex",
         "kind": "subscription", "model": "codex", "source": "connected", "base_url": None,
         "pill": {"state": "login_expired", "tone": "red", "label": "Login expired",
                  "tip": setup_pills.LOGIN_FIX["codex"], "action": "sign_in"}},
        {"source_id": "setup-anthropic", "provider": "anthropic", "label": "Anthropic",
         "kind": "metered", "model": "claude-sonnet-4-5", "source": "connected",
         "base_url": None,
         "pill": {"state": "working", "tone": "green", "label": "Working",
                  "tip": "Answered a test question 8h ago."}},
    ],
    "detected": [
        {"provider": "claude_code", "kind": "subscription", "found": "needs_sign_in",
         "legacy_token": True, "fix": cli_harness.LEGACY_TOKEN_FIX},
        {"provider": "grok", "kind": "subscription", "found": "needs_sign_in",
         "legacy_token": False},
    ],
    "jobs": [
        {"job": "build", "label": "Build",
         "resolved": {"engine": "claude_agent_sdk", "model": "claude-sonnet-5",
                      "source_id": "setup-anthropic", "label": "Anthropic · claude-sonnet-5"}},
        {"job": "review", "label": "Review",
         "resolved": {"engine": "claude_code", "model": "sonnet",
                      "source_id": "setup-anthropic"}},
    ],
}


def test_the_summary_reads_the_setup_view(monkeypatch, tmp_path):
    ms = _model_status()
    monkeypatch.setattr(ms, "api_get", lambda _u, _k, path: LIVE_VIEW
                        if path.startswith("/v1/setup?") else None)
    rows = dict(ms.rows("http://api", "host-key", tmp_path / "missing.env", {}))
    assert rows["Anthropic (console key)"].startswith("Working")
    assert rows["Codex (subscription)"].startswith("Login expired")
    assert "boss connect codex" in rows["Codex (subscription)"]
    assert rows["Default"].startswith("Build → Claude Agent SDK")
    assert "on setup-anthropic" in rows["Default"]
    assert "legacy Claude token" in rows["Claude Code"]
    assert "boss connect grok" in rows["Grok Build"]
    assert not any("connected" == text for text in rows.values())
    printed = " ".join(rows.values())
    assert not any(bad in printed for bad in REFUSED_IMPORTS)


def test_an_expired_login_is_not_a_usable_hosted_model(monkeypatch, tmp_path):
    ms = _model_status()
    only_codex = {**LIVE_VIEW, "models": LIVE_VIEW["models"][:1]}
    monkeypatch.setattr(ms, "api_get", lambda _u, _k, path: only_codex
                        if path.startswith("/v1/setup?") else None)
    assert ms.hosted_connected("http://api", "host-key", tmp_path / "missing.env") is False
    monkeypatch.setattr(ms, "api_get", lambda _u, _k, path: LIVE_VIEW
                        if path.startswith("/v1/setup?") else None)
    assert ms.hosted_connected("http://api", "host-key", tmp_path / "missing.env") is True


def test_the_summary_usable_states_are_the_views():
    assert _model_status().USABLE_STATES == setup_view.USABLE_MODEL
    assert _model_status().USABLE_LOCAL_STATES == setup_view.USABLE_LOCAL_MODEL


# ── P0-4 (engine): no surface offers a route the backend refuses ─────────────────────────────

def test_subscriptions_have_no_credential_fields():
    for provider in ("codex", "claude_code", "grok"):
        entry = setup_view.CATALOGUE_BY_PROVIDER[provider]
        assert entry["fields"] == [], provider
        assert not any(bad in str(entry.get("command") or "") for bad in REFUSED_IMPORTS)


def test_no_fix_offers_a_refused_import():
    texts = [*setup_pills.LOGIN_FIX.values(), setup_pills.DEFAULT_LOGIN_FIX,
             *(e["tip"] for e in setup_pills.MODEL.values()), cli_harness.LEGACY_TOKEN_FIX]
    for spec in cli_harness.BUILTIN_CLI_HARNESSES.values():
        texts += [spec.token_hint, spec.signin_hint, cli_harness._sign_in_step(spec)]
    # A failed model call's own advice too (it told people to paste a `claude setup-token`).
    from boss_engine.services import model_failures

    texts.append(model_failures.explain("401 invalid_refresh_token: not logged in",
                                        source_id="codex_subscription")["action"])
    for said in texts:
        assert not any(bad in said.lower() for bad in REFUSED_IMPORTS), said
    for provider in ("codex", "claude_code", "grok"):
        assert setup_pills.LOGIN_FIX[provider].startswith(
            f"Run `boss connect {setup_pills.CONNECT_NAME[provider]}` (signs in inside BOSS)")


def test_an_expired_subscription_offers_sign_in_not_replace():
    shown = setup_pills.pill("model", "login_expired",
                             provider_fix=setup_pills.LOGIN_FIX["codex"])
    assert shown["action"] == "sign_in"


# ── P1-6: honest detection ───────────────────────────────────────────────────────────────────

def test_installed_but_signed_out_needs_sign_in(monkeypatch):
    monkeypatch.setattr(setup_view, "_cli_detection", lambda _s: {
        "grok": {"installed": True, "authenticated": False},
        "codex": {"installed": True, "authenticated": False},
        "claude_code": {"installed": True, "authenticated": False},
        "cursor": {"installed": False, "authenticated": None},
    })
    found = {d["provider"]: d["found"] for d in setup_view._detected(_Settings())}
    assert found["grok"] == "needs_sign_in"
    assert found["codex"] == "needs_sign_in"
    assert found["claude_code"] == "needs_sign_in"
    # Cursor is not offered (P1-8): its CLI is not in the image, so detection does not list it.
    assert "cursor" not in found
    assert not {"needs_token", "needs_file"} & set(found.values())


def _claude_probe(monkeypatch, *, stored: bool):
    """A signed-out claude CLI in the container; BOSS's store holding (or not) a legacy token."""
    spec = cli_harness.BUILTIN_CLI_HARNESSES["claude_code"]
    monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN", raising=False)
    monkeypatch.setattr(cli_harness.shutil, "which", lambda _b: "/usr/bin/claude")
    seen = {}

    def probe(_binary, args, timeout=20, extra_env=None, exclude_env=()):
        if args == spec.version_args:
            return 0, "2.1.273 (Claude Code)"
        seen["env"] = dict(extra_env or {})
        return 0, json.dumps({"loggedIn": bool(extra_env), "authMethod":
                              "oauth_token" if extra_env else "none"})

    monkeypatch.setattr(cli_harness, "_run_probe", probe)
    monkeypatch.setattr(cli_harness, "stored_credential",
                        lambda _s, _n, **_k: "legacy-token-value" if stored else None)
    return spec, seen


def test_a_stored_legacy_claude_token_is_not_a_sign_in(monkeypatch):
    spec, seen = _claude_probe(monkeypatch, stored=True)
    found = cli_harness.detect(spec, Settings())
    assert found["authenticated"] is False and found["ready"] is False
    assert found["legacy_token"] is True and found["token_present"] is True
    assert "anthropic_claude_code/disconnect" in found["legacy_token_fix"]
    assert "boss connect claude" in found["next_step"]
    assert seen["env"] == {}, "the stored token must not sign the probe in"
    assert "legacy-token-value" not in json.dumps(found)

    with pytest.raises(setup_service.SetupError, match="legacy Claude token"):
        setup_service.connect_coding_agent(None, Settings(), "p", "claude_code",
                                           prove_with=lambda: pytest.fail("proved a token"))


def test_with_the_legacy_token_removed_claude_can_connect(monkeypatch):
    spec, _seen = _claude_probe(monkeypatch, stored=False)
    found = cli_harness.detect(spec, Settings())
    assert found["legacy_token"] is False and found["token_present"] is False
    assert found["legacy_token_fix"] is None


def test_the_legacy_refusal_is_one_pill(monkeypatch):
    from boss_engine.services import setup_actions

    def refuse(*_a, **_k):
        raise setup_service.SetupError("the claude_code CLI is not signed in inside BOSS: BOSS "
                                       "holds a legacy Claude token from the retired import.")

    monkeypatch.setattr(setup_service, "connect_coding_agent", refuse)
    monkeypatch.setattr(setup_actions, "_subscription_spec",
                        lambda _s, _p: cli_harness.BUILTIN_CLI_HARNESSES["claude_code"])
    with pytest.raises(setup_actions.SetupActionError) as caught:
        setup_actions.connect_model(None, Settings(), "p", provider="claude_code",
                                    kind="subscription")
    assert caught.value.detail["state"] == "legacy_token"
    assert "Disconnect Claude Code" in caught.value.detail["fix"]


# ── P1-7: proofs prove ───────────────────────────────────────────────────────────────────────

def _stub_turn(monkeypatch, *, summary="", exit_code=0, raises=None):
    def run_role(self, prompt, cwd, **_k):
        if raises is not None:
            raise raises
        return {"summary": summary, "exit_code": exit_code}

    monkeypatch.setattr(cli_harness.GenericCliCodeAgent, "run_role", run_role)


REFRESH_401 = ("ERROR codex_login::auth::manager: Failed to refresh token status=401 "
               "Unauthorized detail=TokenErrorDetail { error_code: Some(\"invalid_refresh_token\") }")


def test_exit_zero_without_boss_ok_is_not_working(monkeypatch):
    _stub_turn(monkeypatch, summary="error: something went wrong", exit_code=0)
    proof = setup_service.prove_coding_agent(Settings(), "codex")
    assert proof["ok"] is False and "BOSS_OK" in proof["error"]
    _stub_turn(monkeypatch, summary="BOSS_OK", exit_code=0)
    assert setup_service.prove_coding_agent(Settings(), "codex")["ok"] is True


@pytest.mark.parametrize("how", ["exit", "harness_failure", "unavailable"])
def test_a_cli_auth_failure_is_a_known_rejection(monkeypatch, how):
    from boss_engine.services.harness_failures import HarnessFailure, HarnessVerdict

    if how == "exit":
        _stub_turn(monkeypatch, summary=REFRESH_401, exit_code=1)
    elif how == "harness_failure":
        _stub_turn(monkeypatch, raises=HarnessFailure(HarnessVerdict(
            engine="codex", kind="login", message=REFRESH_401, fix="sign in", where="engine")))
    else:
        _stub_turn(monkeypatch, raises=cli_harness.CliHarnessUnavailable(
            "grok exited 1: You are not authenticated"))
    proof = setup_service.prove_coding_agent(Settings(), "codex")
    assert proof["ok"] is False and proof["status"] == 401
    assert proof["state"] == "login_expired"


URL = os.environ.get("BOSS_CONNECT_TEST_DATABASE_URL", "")


@pytest.fixture()
def scope():
    if not URL:
        pytest.skip("requires an owned disposable Postgres")
    engine = create_engine(URL, connect_args={"options": "-c statement_timeout=8000"})
    project = "onestatus_" + uuid.uuid4().hex[:12]
    with engine.begin() as c:
        c.execute(text("INSERT INTO orgs(id,name) VALUES(:p,:p)"), {"p": project})
        c.execute(text("INSERT INTO workspaces(id,org_id,name) VALUES(:p,:p,:p)"), {"p": project})
        c.execute(text("INSERT INTO projects(id,workspace_id,name,slug) VALUES(:p,:p,:p,:p)"),
                  {"p": project})
        c.execute(text(
            "INSERT INTO budget_policies(id,scope_type,scope_id,daily_cap_usd_micros,"
            "monthly_cap_usd_micros,burst_cap_usd_micros_per_min) "
            "VALUES(:p,'project',:p,1000000,1000000,0)"), {"p": project})
    yield engine, project, Settings()
    engine.dispose()


def test_a_refresh_401_holds_nothing_and_says_login_expired(scope, monkeypatch):
    """The live Codex proof: the CLI's own token refresh was refused before any model call.
    Its reservation is released, nothing is held for review, and every surface says Login
    expired."""
    from boss_engine.services import setup_actions
    from boss_engine.services.connection_spend import held_proofs

    e, p, s = scope
    monkeypatch.setattr(cli_harness, "detect", lambda *_a, **_k: {
        "installed": True, "authenticated": True, "ready": True, "version": "owned-fixture"})
    with e.begin() as c:
        setup_actions.connect_model(c, s, p, provider="codex", kind="subscription",
                                    actor="usr_owned_owner", intent="owned-connect",
                                    prove_with=lambda: {"ok": True, "cost_usd_micros": 0})
    _stub_turn(monkeypatch, summary=REFRESH_401, exit_code=1)
    with e.begin() as c:
        proof = setup_actions.check_model(c, s, p, "openai_codex_chatgpt",
                                          actor="usr_owned_owner")
    assert proof["ok"] is False and proof["state"] == "login_expired"
    assert not proof.get("reconciliation_required")
    with e.begin() as c:
        assert held_proofs(c, p) == []
        assert c.execute(text(
            "SELECT count(*) FROM budget_reservations WHERE project_id=:p AND status='active'"),
            {"p": p}).scalar_one() == 0
        pills = setup_view.source_pills(c, s, p)
        assert pills["openai_codex_chatgpt"]["state"] == "login_expired"
        [row] = [i for i in EntitlementBrokerService(s).list_sources(c, p)
                 if i["source_id"] == "openai_codex_chatgpt"]
        assert row["effective_status"] == "login_rejected"
        assert row["observed_capacity"]["capacity_state"] == "unavailable"


def test_an_exit_zero_error_is_not_working(scope, monkeypatch):
    from boss_engine.services import setup_actions

    e, p, s = scope
    monkeypatch.setattr(cli_harness, "detect", lambda *_a, **_k: {
        "installed": True, "authenticated": True, "ready": True, "version": "owned-fixture"})
    with e.begin() as c:
        setup_actions.connect_model(c, s, p, provider="codex", kind="subscription",
                                    actor="usr_owned_owner", intent="owned-connect-2",
                                    prove_with=lambda: {"ok": True, "cost_usd_micros": 0})
    _stub_turn(monkeypatch, summary="error: the model is unavailable", exit_code=0)
    with e.begin() as c:
        proof = setup_actions.check_model(c, s, p, "openai_codex_chatgpt",
                                          actor="usr_owned_owner")
    assert proof["ok"] is False
    with e.begin() as c:
        assert setup_view.source_pills(c, s, p)["openai_codex_chatgpt"]["state"] != "working"


def test_disconnect_removes_the_legacy_token(scope, monkeypatch):
    """The governed removal: Sources › Claude Code › Disconnect forgets the stored token, and
    detection stops reporting it — so `boss connect claude` can proceed."""
    from boss_engine.services.claude_credential import TOKEN_NAME
    from boss_engine.services.secrets_service import put_secret

    e, p, s = scope
    monkeypatch.setenv("BOSS_COMMUNITY_SECRET_STORE", "database")
    monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN", raising=False)
    spec = cli_harness.BUILTIN_CLI_HARNESSES["claude_code"]
    broker = EntitlementBrokerService(s)
    with e.begin() as c:
        put_secret(c, s, project_id=p, name=TOKEN_NAME, value="owned-legacy-" + p,
                   secret_kind="api_token", backend="database",
                   scope_id=cli_harness.SUBSCRIPTION_SECRET_SCOPE, actor="usr_owned_owner")
        broker.upsert_source(c, {
            "project_id": p, "provider": "anthropic", "source_id": "anthropic_claude_code",
            "source_kind": "official_cli", "entitlement_kind": "subscription",
            "owner_type": "workspace_operator", "owner_id": "workspace_operator",
            "multi_tenant_allowed": False, "auth_method": "official_cli_oauth",
            "connection_status": "connected", "cost_mode": "subscription",
            "runtime_supported": True, "observed_capacity": {"credential_present": True}},
            actor="usr_owned_owner")
    assert cli_harness.legacy_token_stored(spec, s) is True
    with e.begin() as c:
        broker.disconnect_source(c, p, "anthropic_claude_code", actor="usr_owned_owner")
    assert cli_harness.legacy_token_stored(spec, s) is False


def test_connect_after_a_fresh_sign_in_retires_the_legacy_token(monkeypatch):
    """Signed in inside BOSS (the CLI's own login: "claude.ai") with a legacy token still
    stored: Connect retires the token, recorded, and goes on to prove the login. It used to
    refuse and send the operator to a per-project Disconnect plus a terminal command."""
    spec = cli_harness.BUILTIN_CLI_HARNESSES["claude_code"]
    monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN", raising=False)
    monkeypatch.setattr(cli_harness.shutil, "which", lambda _b: "/usr/bin/claude")
    monkeypatch.setattr(cli_harness, "_run_probe", lambda _b, args, **_k: (
        (0, "2.1.197 (Claude Code)") if args == spec.version_args else
        (0, json.dumps({"loggedIn": True, "authMethod": "claude.ai"}))))
    stored = {"token": True}
    monkeypatch.setattr(cli_harness, "stored_credential",
                        lambda _s, _n, **_k: "legacy-token-value" if stored["token"] else None)
    retired, events = [], []

    from boss_engine.services import claude_credential, core

    def retire(_c, _s, *, actor):
        retired.append(actor)
        stored["token"] = False
        return 2

    monkeypatch.setattr(claude_credential, "retire_legacy_token", retire)
    monkeypatch.setattr(core, "emit_event", lambda *a, **_k: events.append(a[2]) or "evt")

    class Proved(Exception):
        pass

    def prove():
        raise Proved()

    # Past the legacy gate it reaches the proof (which needs a real database connection).
    with pytest.raises((Proved, setup_service.SetupError)) as caught:
        setup_service.connect_coding_agent(None, Settings(), "p", "claude_code",
                                           actor="operator", prove_with=prove)
    assert "legacy" not in str(caught.value)
    assert retired == ["operator"]
    assert events == ["entitlement.legacy_token_retired"]
