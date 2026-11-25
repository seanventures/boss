"""A sandboxed claude_code run on the cli_login credential starts signed in (2026-10-26) —
without the login ever entering the run (BOSSAPP-1480).

`_seed_cli_logins` once copied Codex's and Grok's logins into the run's HOME but never Claude's,
so every build on the Claude subscription failed "Not logged in". The fix then copied the access
token in (never the refresh token). Now nothing is copied: the session gets a run token, and the
model proxy reads the control plane's CURRENT access token on each request."""
from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass
from pathlib import Path

from boss_engine.services import claude_credential, model_proxy, sandbox_identity
from boss_engine.services import subscription_shield


@dataclass
class _Ident:
    home: Path
    key: str = "run-x"


def _login(tmp_path: Path, monkeypatch) -> Path:
    conf = tmp_path / "service-claude"
    conf.mkdir(mode=0o700)
    src = conf / ".credentials.json"
    src.write_text(json.dumps({"claudeAiOauth": {
        "accessToken": "ACCESS_MARKER", "refreshToken": "REFRESH_MARKER",
        "expiresAt": int((time.time() + 8 * 3600) * 1000), "scopes": ["user:inference"],
        "subscriptionType": "max"}}))
    os.chmod(src, 0o600)
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(conf))
    return src


def test_the_run_gets_a_run_token_and_the_proxy_the_access_token(tmp_path, monkeypatch):
    src = _login(tmp_path, monkeypatch)
    before = src.read_text()
    proxy = model_proxy.ModelProxy()
    try:
        env = subscription_shield.claude({}, cli_login=True, proxy=proxy)
        token = env["CLAUDE_CODE_OAUTH_TOKEN"]
        assert token.startswith(model_proxy.TOKEN_PREFIX)
        assert env["ANTHROPIC_BASE_URL"] == proxy.base_url("claude_subscription")
        assert model_proxy.held_for(token, proxy) == "ACCESS_MARKER"   # never the refresh token
        assert "MARKER" not in json.dumps(env)
    finally:
        proxy.stop()
    assert src.read_text() == before                      # the control plane's login is untouched


def test_nothing_is_copied_into_the_run(tmp_path, monkeypatch):
    _login(tmp_path, monkeypatch)
    monkeypatch.setattr(sandbox_identity, "_refresh_logins_ahead", lambda ident: None)
    home = tmp_path / "run-home"
    home.mkdir()
    sandbox_identity._seed_cli_logins(_Ident(home))
    assert not (home / ".claude" / ".credentials.json").exists()
    assert not any("MARKER" in p.read_text(errors="ignore")
                   for p in home.rglob("*") if p.is_file())


def test_a_login_that_is_not_private_is_not_trusted(tmp_path, monkeypatch):
    src = _login(tmp_path, monkeypatch)
    os.chmod(src, 0o644)                                  # cli_login_present refuses it
    assert not claude_credential.cli_login_present()
