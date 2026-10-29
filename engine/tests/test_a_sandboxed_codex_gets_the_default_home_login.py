"""A sandboxed codex run starts signed in when the install never set CODEX_HOME (2026-10-28).

`_seed_cli_logins` copied Codex's login only from $CODEX_HOME. boss-1 and the laptop never set it:
Codex keeps its login in its default ~/.codex, so nothing was copied and every build's codex ran
signed out, falling back to an API key it did not have ("401 Unauthorized,
wss://api.openai.com/v1/responses"), while Setup's own check, run as the control plane, said
working."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from boss_engine.services import sandbox_identity


@dataclass
class _Ident:
    home: Path


def _no_claude(monkeypatch):
    monkeypatch.setattr(sandbox_identity, "_seed_claude_login", lambda ident: None)


def test_the_default_codex_home_is_copied_when_codex_home_is_unset(tmp_path, monkeypatch):
    _no_claude(monkeypatch)
    service_home = tmp_path / "boss"
    (service_home / ".codex").mkdir(parents=True)
    (service_home / ".codex" / "auth.json").write_text('{"auth_mode": "chatgpt"}')
    monkeypatch.setenv("HOME", str(service_home))
    monkeypatch.delenv("CODEX_HOME", raising=False)
    monkeypatch.delenv("GROK_HOME", raising=False)
    run_home = tmp_path / "run-home"
    run_home.mkdir()

    sandbox_identity._seed_cli_logins(_Ident(run_home))

    assert (run_home / ".codex" / "auth.json").read_text() == '{"auth_mode": "chatgpt"}'


def test_codex_home_still_wins_when_it_is_set(tmp_path, monkeypatch):
    _no_claude(monkeypatch)
    monkeypatch.setenv("HOME", str(tmp_path / "boss"))
    custom = tmp_path / "custom-codex"
    custom.mkdir()
    (custom / "auth.json").write_text('{"from": "CODEX_HOME"}')
    monkeypatch.setenv("CODEX_HOME", str(custom))
    run_home = tmp_path / "run-home"
    run_home.mkdir()

    sandbox_identity._seed_cli_logins(_Ident(run_home))

    assert (run_home / ".codex" / "auth.json").read_text() == '{"from": "CODEX_HOME"}'


def test_no_codex_login_anywhere_copies_nothing(tmp_path, monkeypatch):
    _no_claude(monkeypatch)
    monkeypatch.setenv("HOME", str(tmp_path / "empty-home"))
    monkeypatch.delenv("CODEX_HOME", raising=False)
    monkeypatch.delenv("GROK_HOME", raising=False)
    run_home = tmp_path / "run-home"
    run_home.mkdir()

    sandbox_identity._seed_cli_logins(_Ident(run_home))

    assert not (run_home / ".codex").exists()


def test_the_codex_copy_never_carries_the_refresh_token(tmp_path, monkeypatch):
    """boss-1, 2026-10-29: every run's codex held the one refresh token; a refresh in any copy
    rotated it and OpenAI revoked the whole login (login_expired, access token still valid)."""
    import json

    _no_claude(monkeypatch)
    service_home = tmp_path / "boss"
    (service_home / ".codex").mkdir(parents=True)
    src = service_home / ".codex" / "auth.json"
    src.write_text(json.dumps({"auth_mode": "chatgpt", "last_refresh": "x",
                               "tokens": {"access_token": "ACCESS", "id_token": "ID",
                                          "refresh_token": "REFRESH_MARKER", "account_id": "a"}}))
    before = src.read_text()
    monkeypatch.setenv("HOME", str(service_home))
    monkeypatch.delenv("CODEX_HOME", raising=False)
    run_home = tmp_path / "run-home"
    run_home.mkdir()

    sandbox_identity._seed_cli_logins(_Ident(run_home))

    copied = json.loads((run_home / ".codex" / "auth.json").read_text())
    assert copied["tokens"]["access_token"] == "ACCESS"
    assert "refresh_token" not in copied["tokens"]
    assert "REFRESH_MARKER" not in (run_home / ".codex" / "auth.json").read_text()
    assert src.read_text() == before                     # the control plane's login is untouched
