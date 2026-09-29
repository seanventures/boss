"""Subscription discovery must not touch or transfer a VM credential by default."""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts/install/coding_subscriptions.py"
SPEC = importlib.util.spec_from_file_location("coding_subscriptions", SCRIPT)
assert SPEC and SPEC.loader
module = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(module)


def test_discovery_is_read_only_and_does_not_claim_grok_auth(monkeypatch, capsys) -> None:
    calls = []
    monkeypatch.setattr(module.shutil, "which", lambda name: f"/bin/{name}")

    def status(argv, timeout=15):
        calls.append(argv)
        if argv[0] == "codex":
            return 0, "Not logged in"
        return 0, '{"loggedIn": false}'

    monkeypatch.setattr(module, "checked_output", status)
    found = module.discover()
    output = capsys.readouterr().out

    assert calls == [["codex", "login", "status"], ["claude", "auth", "status"]]
    assert found == {"claude": False, "codex": False, "grok": True}
    assert "Grok login: check inside the BOSS API container" in output
    assert "Not logged in" not in output


def test_codex_auth_requires_private_vm_user_file(monkeypatch, tmp_path: Path) -> None:
    home = tmp_path / "codex"
    home.mkdir()
    auth = home / "auth.json"
    auth.write_text('{"tokens":{"access_token":"private"}}')
    monkeypatch.setenv("CODEX_HOME", str(home))
    monkeypatch.setattr(module, "checked_output", lambda argv: (0, "Logged in using ChatGPT"))

    auth.chmod(0o644)
    with pytest.raises(RuntimeError, match="mode 0600"):
        module.codex_credential()

    auth.chmod(0o600)
    assert module.codex_credential() == '{"tokens":{"access_token":"private"}}'


def test_connect_requires_explicit_flag_and_does_not_send_on_discovery(monkeypatch) -> None:
    monkeypatch.setattr(module, "discover", lambda: {"claude": True, "codex": True, "grok": True})
    monkeypatch.setattr(module, "send_to_boss", lambda *args, **kwargs: pytest.fail("credential sent"))
    monkeypatch.setattr(module, "connect", lambda *args, **kwargs: pytest.fail("proof called"))
    monkeypatch.setattr(module.sys, "argv", ["coding_subscriptions.py", "--api-url", "http://127.0.0.1:9"])
    assert module.main() == 0


def test_claude_setup_token_import_stores_the_supplied_token(monkeypatch, capsys) -> None:
    token = "test-claude-token"
    sent = {}

    def capture(*args, **kwargs):
        sent["args"] = args
        sent["kwargs"] = kwargs
        return {"ok": True, "backend": "local_vault"}

    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", token)
    monkeypatch.setenv("BOSS_API_KEY", "test-api-key")
    monkeypatch.setattr(module, "send_to_boss", capture)
    monkeypatch.setattr(module, "discover", lambda: {"claude": False, "codex": False, "grok": False})
    monkeypatch.setattr(module, "connect", lambda *args, **kwargs: sent.setdefault("connected", args[2]))
    monkeypatch.setattr(module.sys, "argv", ["coding_subscriptions.py", "--connect-claude",
                                             "--accept-database-fallback"])

    assert module.main() == 0
    assert sent["args"][3] == "claude_code"
    assert sent["args"][4] == token
    assert sent["kwargs"]["accept_database_fallback"] is False
    assert sent["connected"] == "claude_code"
    output = capsys.readouterr()
    assert token not in output.out + output.err


def test_a_missing_claude_setup_token_fails_clearly(monkeypatch) -> None:
    monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN", raising=False)
    monkeypatch.setattr(module.shutil, "which", lambda _name: None)
    monkeypatch.setattr(module, "send_to_boss", lambda *args, **kwargs: pytest.fail("credential sent"))
    monkeypatch.setattr(module, "discover", lambda: {"claude": False, "codex": False, "grok": False})
    monkeypatch.setenv("BOSS_API_KEY", "test-api-key")
    monkeypatch.setattr(module.sys, "argv", ["coding_subscriptions.py", "--connect-claude"])

    with pytest.raises(RuntimeError, match="Nothing was stored") as raised:
        module.main()

    assert "test-claude-token" not in str(raised.value)


def test_cli_login_instructions_do_not_start_a_model_call(monkeypatch, capsys) -> None:
    monkeypatch.setattr(module, "discover", lambda: {"claude": False, "codex": False, "grok": False})
    monkeypatch.setattr(module.urllib.request, "urlopen",
                        lambda *args, **kwargs: pytest.fail("status or model request"))
    monkeypatch.setattr(module, "connect", lambda *args, **kwargs: pytest.fail("connect"))
    monkeypatch.setattr(module, "send_to_boss", lambda *args, **kwargs: pytest.fail("credential sent"))
    monkeypatch.setattr(module.subprocess, "run",
                        lambda *args, **kwargs: pytest.fail("cli was executed"))
    monkeypatch.setattr(module.sys, "argv", ["coding_subscriptions.py"])

    assert module.main() == 0
    output = capsys.readouterr().out
    assert "claude auth login" in output
    assert "--connect-claude-local" in output
    assert "grok login --device-auth" in output
    assert "--connect-grok-local" in output
    assert "codex login --device-auth" in output
    assert "--connect-codex" in output


def test_grok_local_login_reads_status_and_does_not_upload_a_session(monkeypatch) -> None:
    calls = []

    def answer(request, timeout):
        calls.append((request.get_method(), request.full_url, request.data, timeout))
        if request.get_method() == "GET":
            body = {"agents": [{"name": "grok", "installed": True, "authenticated": True}]}
        else:
            body = {"connected": True, "entitlement_recorded": True,
                    "proof": {"ok": True, "skipped": True}}
        class _Response:
            def __enter__(self):
                return self
            def __exit__(self, *_args):
                return False
            def read(self):
                return json.dumps(body).encode()
        return _Response()

    monkeypatch.setattr(module.urllib.request, "urlopen", answer)
    monkeypatch.setattr(module, "discover", lambda: pytest.fail("host discovery"))
    monkeypatch.setattr(module, "send_to_boss", lambda *args, **kwargs: pytest.fail("upload"))
    monkeypatch.setenv("BOSS_API_KEY", "test-api-key")
    monkeypatch.setattr(module.sys, "argv", ["coding_subscriptions.py", "--connect-grok-local"])

    assert module.main() == 0
    assert calls[0][0] == "GET"
    assert calls[0][2] is None
    assert json.loads(calls[1][2])["prove"] is False
    assert json.loads(calls[1][2])["provider"] == "grok"


def test_a_missing_grok_login_is_a_visible_error(monkeypatch) -> None:
    class _Response:
        def __enter__(self):
            return self
        def __exit__(self, *_args):
            return False
        def read(self):
            return b'{"agents":[{"name":"grok","installed":true,"authenticated":false}]}'

    monkeypatch.setattr(module.urllib.request, "urlopen", lambda *_args, **_kwargs: _Response())
    monkeypatch.setattr(module, "connect", lambda *args, **kwargs: pytest.fail("recorded"))
    monkeypatch.setenv("BOSS_API_KEY", "test-api-key")
    monkeypatch.setattr(module.sys, "argv", ["coding_subscriptions.py", "--connect-grok-local"])

    with pytest.raises(RuntimeError, match="Nothing was recorded") as raised:
        module.main()

    assert "grok login --device-auth" in str(raised.value)
