"""A reconnect ends with the store holding the LIVE credential, out loud (BOSSAPP-1026).

On a live install the operator did exactly what the error said — reconnected the Codex
subscription in Repo and models — and the secret store's version did not move: the sign-in
rewrote the CLI's own auth.json and nobody stored it. Every later restore-from-store then
signed the working login out again, and nothing anywhere said so. These tests pin the other
contract: Connect stores the credential it just proved, reports the store generation it wrote,
and FAILS in words when the store did not take it — never settles silently.

No model, no store, no network: the proof is stubbed, the store is a fake, CODEX_HOME is a
temporary directory (never the developer's own login).
"""
from __future__ import annotations

import json

import pytest

from boss_engine.config import Settings
from boss_engine.services import cli_harness, setup_service

STORE_KEY = "BOSS_SUBSCRIPTION_CODEX_AUTH_JSON"

LIVE = {"tokens": {"access_token": "live.secret.value"},
        "last_refresh": "2026-11-12T04:20:00Z"}
STALE = {"tokens": {"access_token": "stale.secret.value"},
         "last_refresh": "2026-10-01T00:00:00Z"}


class _Store:
    def __init__(self, values=None):
        self.values = dict(values or {})
        self.versions = {key: 1 for key in self.values}

    def get(self, key):
        if key not in self.values:
            raise KeyError(key)
        return self.values[key]

    def put(self, key, value):
        self.values[key] = value
        self.versions[key] = self.versions.get(key, 0) + 1

    def describe(self, key):
        if key not in self.values:
            return {}
        return {"version": self.versions[key], "updated_at": "2026-11-12T04:21:00Z"}


@pytest.fixture
def connect(tmp_path, monkeypatch):
    """connect_coding_agent for codex with a signed-in CLI, a passing proof and a fake store;
    returns (call, store)."""
    home = tmp_path / "codex-home"
    home.mkdir()
    (home / "auth.json").write_text(json.dumps(LIVE))
    monkeypatch.setenv("CODEX_HOME", str(home))
    monkeypatch.delenv("CODEX_AUTH_JSON", raising=False)
    store = _Store({STORE_KEY: json.dumps(STALE)})
    monkeypatch.setattr("boss_engine.services.secret_backends.default_secret_backend",
                        lambda settings: "database")
    monkeypatch.setattr("boss_engine.services.secret_backends.backend_for",
                        lambda kind, settings: store)
    monkeypatch.setattr(cli_harness, "detect", lambda _spec: {
        "installed": True, "authenticated": True, "version": "codex 0.158.0",
        "auth_detail": "Logged in using ChatGPT",
    })
    monkeypatch.setattr(setup_service, "governed_proof",
                        lambda *_a, **_k: {"ok": True, "replied": "BOSS_OK"})

    from types import SimpleNamespace

    monkeypatch.setattr("boss_engine.services.entitlements.EntitlementBrokerService",
                        lambda _settings: SimpleNamespace(
                            upsert_source=lambda _conn, _payload, **_kw: None))

    class _Engine:
        def begin(self):
            from contextlib import nullcontext

            return nullcontext(_Conn())

    class _Conn:
        engine = _Engine()

        def execute(self, *_a, **_k):
            raise AssertionError("no SQL in this test")

    def call():
        return setup_service.connect_coding_agent(_Conn(), Settings(), "project", "codex")

    return call, store


def test_a_reconnect_stores_the_proven_credential_and_reports_the_generation(connect) -> None:
    call, store = connect

    out = call()

    assert out["ok"] is True
    assert json.loads(store.values[STORE_KEY]) == LIVE, "the store holds the LIVE generation"
    receipt = out["credential_store"]
    assert receipt["state"] == "wrote"
    assert receipt["store_version"] == 2, "the reconnect reports the store version it wrote"
    assert receipt["store_updated_at"]
    assert "live.secret.value" not in repr(out) and "stale.secret.value" not in repr(out)


def test_a_reconnect_the_store_refuses_fails_in_words(connect, monkeypatch) -> None:
    """Never a silent no-op: that was the live failure, verbatim."""
    call, store = connect

    def refuse(_key, _value):
        raise RuntimeError("store is down")

    monkeypatch.setattr(store, "put", refuse)

    with pytest.raises(setup_service.SetupError) as refused:
        call()
    message = str(refused.value)
    assert "could not keep its credential current" in message
    assert "nothing was connected" in message and "Connect again" in message
    assert "live.secret.value" not in message and "stale.secret.value" not in message


def test_a_reconnect_whose_write_does_not_take_fails_too(connect, monkeypatch) -> None:
    """A store that accepts the put and still serves the old generation left the version
    unchanged — exactly the silent settle this fix forbids."""
    call, store = connect
    monkeypatch.setattr(store, "put", lambda _key, _value: None)

    with pytest.raises(setup_service.SetupError, match="could not keep its credential current"):
        call()


def test_a_reconnect_shadowed_by_an_environment_variable_says_so(connect, monkeypatch) -> None:
    """With the credential pinned in the environment, every restore uses that value whatever
    the operator reconnects — the one case where only words can fix the install."""
    call, _store = connect
    monkeypatch.setenv("CODEX_AUTH_JSON", json.dumps(STALE))

    with pytest.raises(setup_service.SetupError, match="CODEX_AUTH_JSON"):
        call()


def test_an_unchanged_store_that_already_holds_the_live_generation_is_fine(connect) -> None:
    """"Must change the version" means "must hold the live credential" — a store already
    current is a healthy reconnect, not a silent one."""
    call, store = connect
    store.values[STORE_KEY] = json.dumps(LIVE)

    out = call()

    assert out["ok"] is True
    assert out["credential_store"]["state"] == "already_current"
    assert store.versions[STORE_KEY] == 1


def test_the_console_reconnect_keeps_the_loud_words(monkeypatch) -> None:
    """The console's Connect goes through `_connect_subscription`, which maps SetupErrors onto
    pills — and a generic "failed" pill would hide exactly the silence this fix removes. The
    store-sync refusal travels with its own words."""
    from boss_engine.services import setup_actions

    said = ("codex is signed in and answered its proof, but BOSS could not keep its "
            "credential current in the secret store: the store is down. Fix that, then "
            "press Connect again.")

    def refuse(*_a, **_k):
        raise setup_service.SetupError(said)

    monkeypatch.setattr(setup_service, "connect_coding_agent", refuse)

    with pytest.raises(setup_actions.SetupActionError) as refused:
        setup_actions._connect_subscription(None, Settings(), "project", "codex",
                                            credential="", accept=False, actor="ceo")

    assert "could not keep its credential current" in refused.value.detail["fix"]
