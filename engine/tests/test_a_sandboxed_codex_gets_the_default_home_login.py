"""A sandboxed codex run starts signed in when the install never set CODEX_HOME (2026-10-28) —
through the model proxy, never with a copy of the login (BOSSAPP-1480).

`_seed_cli_logins` once copied Codex's login only from $CODEX_HOME. boss-1 and the laptop never
set it: Codex keeps its login in its default ~/.codex, so nothing was copied and every build's
codex ran signed out. Now nothing is copied at all; the session's login (a run token) stands for
whichever login the control plane holds — $CODEX_HOME when set, else ~/.codex."""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

import pytest

from boss_engine.config import Settings
from boss_engine.services import model_proxy, sandbox_identity, subscription_shield
from boss_engine.services.cli_harness import BUILTIN_CLI_HARNESSES, GenericCliCodeAgent


@dataclass
class _Ident:
    home: Path
    key: str = "run-x"


def _doc(access: str) -> str:
    return json.dumps({"auth_mode": "chatgpt", "last_refresh": "2099-01-01T00:00:00Z",
                       "tokens": {"access_token": access, "refresh_token": "R1",
                                  "account_id": "acct-1"}})


@pytest.fixture()
def run(tmp_path, monkeypatch):
    for name in ("CODEX_HOME", "GROK_HOME", "OPENAI_API_KEY", "CODEX_API_KEY"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(subscription_shield, "_codex_before_start", lambda: None)
    monkeypatch.setattr(subscription_shield, "_codex_login_dead", lambda: False)
    run_home = tmp_path / "run-home"
    run_home.mkdir()
    monkeypatch.setattr(sandbox_identity, "current", lambda: _Ident(run_home))
    proxy = model_proxy.ModelProxy()
    model_proxy.set_proxy(proxy)
    yield run_home, proxy
    model_proxy.set_proxy(None)
    proxy.stop()


def _session_login(run_home: Path) -> tuple[dict, dict]:
    env = GenericCliCodeAgent(Settings(), BUILTIN_CLI_HARNESSES["codex"])._env()
    path = Path(env.get("CODEX_HOME") or "/nonexistent") / "auth.json"
    return env, (json.loads(path.read_text()) if path.exists() else {})


def test_the_default_codex_home_login_is_used_when_codex_home_is_unset(tmp_path, monkeypatch,
                                                                       run):
    run_home, proxy = run
    service_home = tmp_path / "boss"
    (service_home / ".codex").mkdir(parents=True)
    (service_home / ".codex" / "auth.json").write_text(_doc("FROM_DEFAULT_HOME"))
    monkeypatch.setenv("HOME", str(service_home))
    env, login = _session_login(run_home)
    assert Path(env["CODEX_HOME"]).parent == run_home
    assert model_proxy.held_for(login["tokens"]["access_token"], proxy) == "FROM_DEFAULT_HOME"
    assert "FROM_DEFAULT_HOME" not in json.dumps(login)


def test_codex_home_still_wins_when_it_is_set(tmp_path, monkeypatch, run):
    run_home, proxy = run
    monkeypatch.setenv("HOME", str(tmp_path / "boss"))
    custom = tmp_path / "custom-codex"
    custom.mkdir()
    (custom / "auth.json").write_text(_doc("FROM_CODEX_HOME"))
    monkeypatch.setenv("CODEX_HOME", str(custom))
    _env, login = _session_login(run_home)
    assert model_proxy.held_for(login["tokens"]["access_token"], proxy) == "FROM_CODEX_HOME"


def test_no_codex_login_anywhere_places_none(tmp_path, monkeypatch, run):
    run_home, _proxy = run
    monkeypatch.setenv("HOME", str(tmp_path / "empty-home"))
    _env, login = _session_login(run_home)
    assert login == {}
    sandbox_identity._seed_cli_logins(_Ident(run_home))
    assert not (run_home / ".codex" / "auth.json").exists()


def test_the_run_never_gets_the_real_refresh_token_or_access_token(tmp_path, monkeypatch, run):
    run_home, _proxy = run
    service_home = tmp_path / "boss"
    (service_home / ".codex").mkdir(parents=True)
    (service_home / ".codex" / "auth.json").write_text(_doc("A1"))
    monkeypatch.setenv("HOME", str(service_home))
    _env, login = _session_login(run_home)
    assert login["auth_mode"] == "chatgpt"
    assert login["tokens"]["access_token"].startswith(model_proxy.TOKEN_PREFIX)
    assert login["tokens"]["refresh_token"] == sandbox_identity.RUN_REFRESH_TOKEN
