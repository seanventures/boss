"""A sandboxed claude_code run on the cli_login credential starts signed in (2026-10-26).

`_seed_cli_logins` copied Codex's and Grok's logins into the run's HOME but never Claude's, so
every build on the Claude subscription failed "Not logged in · Please run /login" while the
control plane itself reported loggedIn: true. The copy must never carry the refresh token: a run
that refreshed would rotate it and sign the control plane out."""
from __future__ import annotations

import json
import os
import stat
from dataclasses import dataclass
from pathlib import Path

from boss_engine.services import claude_credential, sandbox_identity


@dataclass
class _Ident:
    home: Path


def _login(tmp_path: Path, monkeypatch) -> Path:
    conf = tmp_path / "service-claude"
    conf.mkdir(mode=0o700)
    src = conf / ".credentials.json"
    src.write_text(json.dumps({"claudeAiOauth": {
        "accessToken": "ACCESS_MARKER", "refreshToken": "REFRESH_MARKER",
        "expiresAt": 1, "scopes": ["user:inference"], "subscriptionType": "max"}}))
    os.chmod(src, 0o600)
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(conf))
    return src


def test_the_run_gets_the_access_token_and_never_the_refresh_token(tmp_path, monkeypatch):
    src = _login(tmp_path, monkeypatch)
    before = src.read_text()
    home = tmp_path / "run-home"
    home.mkdir()
    sandbox_identity._seed_claude_login(_Ident(home))

    copied = home / ".claude" / ".credentials.json"
    oauth = json.loads(copied.read_text())["claudeAiOauth"]
    assert oauth["accessToken"] == "ACCESS_MARKER"
    assert "refreshToken" not in oauth
    assert "REFRESH_MARKER" not in copied.read_text()
    assert stat.S_IMODE(copied.stat().st_mode) == 0o644
    assert src.read_text() == before                      # the control plane's login is untouched


def test_no_cli_login_means_nothing_is_copied(tmp_path, monkeypatch):
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "absent"))
    home = tmp_path / "run-home"
    home.mkdir()
    sandbox_identity._seed_claude_login(_Ident(home))
    assert not (home / ".claude").exists()


def test_a_login_that_is_not_private_is_not_trusted(tmp_path, monkeypatch):
    src = _login(tmp_path, monkeypatch)
    os.chmod(src, 0o644)                                  # cli_login_present refuses it
    assert not claude_credential.cli_login_present()
    home = tmp_path / "run-home"
    home.mkdir()
    sandbox_identity._seed_claude_login(_Ident(home))
    assert not (home / ".claude" / ".credentials.json").exists()


def test_a_symlinked_destination_is_not_followed(tmp_path, monkeypatch):
    _login(tmp_path, monkeypatch)
    home = tmp_path / "run-home"
    (home / ".claude").mkdir(parents=True)
    outside = tmp_path / "outside.json"
    outside.write_text("{}")
    (home / ".claude" / ".credentials.json").symlink_to(outside)
    try:
        sandbox_identity._seed_claude_login(_Ident(home))
    except OSError:
        pass
    assert outside.read_text() == "{}"
