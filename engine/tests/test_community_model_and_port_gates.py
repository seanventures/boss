"""Community setup and read tools stop ungoverned model/network effects."""
from __future__ import annotations

import socket

import pytest

from boss_engine.services import setup_service
from boss_engine.tools import build_default_registry, build_discuss_read_registry


class _Settings:
    def __init__(self, edition):
        self.boss_edition = edition


class _Ctx:
    def __init__(self, edition):
        self.settings = _Settings(edition)
        self.began = False

    def begin(self):
        self.began = True
        raise AssertionError("the gate must run before database or external work")


class _Engine:
    def begin(self):
        from contextlib import nullcontext
        return nullcontext(_Conn())


class _Conn:
    """A request connection whose engine the governed proof commits through."""
    engine = _Engine()

    def execute(self, *_a, **_k):
        class _R:
            rowcount = 0
        return _R()


def _signed_in_codex(monkeypatch):
    from boss_engine.services import cli_harness

    monkeypatch.setattr(cli_harness, "detect", lambda _spec: {
        "installed": True, "authenticated": True, "version": "1.0", "auth_detail": "signed in",
    })


@pytest.mark.parametrize("edition", ["community", "oss", "local", "unknown", "enterprise"])
def test_community_proves_a_connection_only_through_the_governed_path(edition, monkeypatch):
    """A connection is PROVED with one live turn before it is called connected — a wrong Claude
    credential "connected" unproved on boss-1 and failed later inside a workflow with a 401. In
    EVERY edition that turn spends only through governed_proof (reservation and receipt first,
    a $0 gate for a subscription); the ungoverned direct proof is never called."""
    _signed_in_codex(monkeypatch)
    monkeypatch.setattr(setup_service, "prove_coding_agent",
                        lambda *_a, **_k: pytest.fail("ungoverned live proof in Community"))
    seen = []
    monkeypatch.setattr(setup_service, "governed_proof",
                        lambda engine, settings, project, provider, actor, prove=None,
                        amount=None, intent="": seen.append(
                            (engine, provider, actor, amount, intent)) or {"ok": True, "replied": "BOSS_OK"})

    class _Broker:
        def __init__(self, _settings):
            pass

        def upsert_source(self, *_a, **_k):
            return None

    from boss_engine.services import entitlements
    monkeypatch.setattr(entitlements, "EntitlementBrokerService", _Broker)
    result = setup_service.connect_coding_agent(
        _Conn(), _Settings(edition), "project-a", "codex", actor="member")
    assert result["connected"] is True and result["proof"]["ok"] is True
    assert seen == [(_Conn.engine, "codex", "member", 0, "")]


def test_a_rejected_login_is_not_connected_and_says_what_to_do(monkeypatch):
    _signed_in_codex(monkeypatch)
    monkeypatch.setattr(setup_service, "governed_proof", lambda *_a, **_k: {
        "ok": False, "error": "Failed to authenticate. API Error: 401 Invalid bearer token"})
    with pytest.raises(setup_service.SetupError) as refused:
        setup_service.connect_coding_agent(
            _Conn(), _Settings("community"), "project-a", "codex", actor="member")
    message = str(refused.value)
    assert "login was rejected" in message and "not connected" in message


def test_the_agent_tool_proves_by_default(monkeypatch):
    seen = []
    monkeypatch.setattr(setup_service, "connect_coding_agent",
                        lambda c, s, p, provider, *, actor, prove, **_k: seen.append(prove) or {"ok": True})

    class _OkCtx(_Ctx):
        def begin(self):
            from contextlib import nullcontext
            return nullcontext(None)

    result = build_default_registry().invoke(
        "connect_coding_agent", _OkCtx("community"), project_id="project-a", actor="member",
        args={"provider": "codex"})
    assert result.get("ok") is not False and seen == [True]


def test_explicit_no_proof_keeps_safe_subscription_registration(monkeypatch):
    from boss_engine.services import cli_harness

    class _Spec:
        require_local_session_file = False
        entitlement_source_id = None
        binary = "safe-agent"

    monkeypatch.setattr(cli_harness, "load_specs", lambda _settings: {"safe": _Spec()})
    monkeypatch.setattr(cli_harness, "detect", lambda _spec: {
        "installed": True, "authenticated": True, "version": "1.0", "auth_detail": "signed in",
    })
    monkeypatch.setattr(setup_service, "prove_coding_agent", lambda *_a, **_k: (
        pytest.fail("prove=false must not run a model")))
    result = setup_service.connect_coding_agent(
        None, _Settings("community"), "project-a", "safe", actor="member", prove=False,
    )
    assert result["ok"] is True
    assert result["proof"] == {"ok": True, "skipped": True}


def test_claude_cli_owned_login_records_metadata_without_credential_or_model(monkeypatch):
    from boss_engine.services import cli_harness, entitlements

    records = []
    monkeypatch.setattr(cli_harness, "detect", lambda _spec: {
        "installed": True, "authenticated": True, "version": "2.1.197",
        "auth_detail": "Signed in with the Claude Code CLI.", "auth_method": "claude_ai",
    })
    monkeypatch.setattr(cli_harness.GenericCliCodeAgent, "ensure_authorised",
                        lambda *_args: pytest.fail("local login must not restore a token"))
    monkeypatch.setattr(setup_service, "prove_coding_agent",
                        lambda *_args, **_kwargs: pytest.fail("no unmetered model turn"))

    class _Broker:
        def __init__(self, _settings):
            pass

        def upsert_source(self, _connection, payload, *, actor):
            records.append((payload, actor))

    monkeypatch.setattr(entitlements, "EntitlementBrokerService", _Broker)
    result = setup_service.connect_coding_agent(
        None, _Settings("community"), "project-a", "claude_code",
        actor="ceo", prove=False,
    )

    assert result["connected"] is True
    assert result["entitlement_recorded"] is True
    assert result["proof"] == {"ok": True, "skipped": True}
    payload, actor = records[0]
    assert actor == "ceo"
    assert payload["source_id"] == "anthropic_claude_code"
    assert payload["auth_method"] == "official_cli_oauth"
    assert "credential_secret_ref_id" not in payload
    assert "token" not in payload


@pytest.mark.parametrize("edition", ["community", "oss", "local", "unknown"])
def test_host_port_discovery_stops_before_db_or_network(edition, monkeypatch):
    def no_probe(*_args, **_kwargs):
        raise AssertionError("host probe must not run")

    monkeypatch.setattr(socket, "socket", no_probe)
    ctx = _Ctx(edition)
    result = build_default_registry().invoke(
        "find_free_port", ctx, project_id="project-a", actor="member",
        args={"host": "169.254.169.254", "start": 3000},
    )
    assert result["ok"] is False
    assert "Community host-port probes" in result["error"]
    assert ctx.began is False
    assert "find_free_port" not in build_discuss_read_registry(edition=edition).names()
