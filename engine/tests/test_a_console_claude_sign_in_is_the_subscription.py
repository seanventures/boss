"""The Claude subscription signed in INSIDE BOSS is the subscription (2026-10-25).

The console's Sign in (and `boss connect claude`) runs `claude auth login` as the service
user; the login lives in the CLI's own config dir. BOSS only counted a STORED token
(the retired setup-token import) as the subscription, so with the login in place and the
Anthropic API key out of credit, every Claude call went to the key: "Credit balance is too
low", and the CLI itself warned that ANTHROPIC_API_KEY "takes precedence over your claude.ai
login". The CLI's own login now resolves as the subscription and the child gets no key.
"""
import os

from boss_engine.config import Settings
from boss_engine.services import claude_credential as cc


def _login(tmp_path, monkeypatch, *, present=True, mode=0o600):
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path))
    monkeypatch.delenv(cc.TOKEN_NAME, raising=False)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-out-of-credit")
    monkeypatch.setattr(cc, "_store_lookup", lambda _s, _n: "")
    if present:
        path = tmp_path / ".credentials.json"
        path.write_text('{"claudeAiOauth": {"subscriptionType": "max"}}')
        os.chmod(path, mode)


def test_the_cli_login_is_the_subscription_and_no_key_reaches_the_child(tmp_path, monkeypatch):
    _login(tmp_path, monkeypatch)
    cred = cc.resolve_claude_credential(Settings())
    assert cred.kind == cc.KIND_CLI_LOGIN
    assert cred.billing == "subscription"
    env = cred.apply({"ANTHROPIC_API_KEY": "sk-ant-out-of-credit", "PATH": "/bin"})
    assert "ANTHROPIC_API_KEY" not in env and cc.TOKEN_NAME not in env
    # Bound settings (a child with no connection) keep the answer.
    again = cc.resolve_claude_credential(cred.bind(Settings()))
    assert again.kind == cc.KIND_CLI_LOGIN


def test_no_login_falls_through_to_the_key(tmp_path, monkeypatch):
    _login(tmp_path, monkeypatch, present=False)
    assert cc.resolve_claude_credential(Settings()).kind == cc.KIND_ENV_KEY


def test_a_shared_readable_file_is_not_a_login(tmp_path, monkeypatch):
    _login(tmp_path, monkeypatch, mode=0o644)
    assert cc.resolve_claude_credential(Settings()).kind == cc.KIND_ENV_KEY


def test_api_key_first_still_puts_the_key_first(tmp_path, monkeypatch):
    _login(tmp_path, monkeypatch)
    settings = Settings().model_copy(update={cc.ORDER_KNOB: cc.API_KEY_FIRST})
    assert cc.resolve_claude_credential(settings).kind == cc.KIND_ENV_KEY
