"""Subscription discovery must not touch or transfer a VM credential by default."""
from __future__ import annotations

import importlib.util
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


def test_claude_setup_token_import_cannot_run(monkeypatch) -> None:
    token = "sk-ant-oat01-session-token-material"
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", token)
    monkeypatch.setattr(module, "send_to_boss", lambda *args, **kwargs: pytest.fail("credential sent"))
    monkeypatch.setattr(module, "discover", lambda: pytest.fail("import must stop before discovery"))
    with pytest.raises(RuntimeError, match="clearance") as raised:
        module.claude_credential()
    assert token not in str(raised.value)
    monkeypatch.setattr(module.sys, "argv", ["coding_subscriptions.py", "--connect-claude"])
    with pytest.raises(RuntimeError, match="disabled") as raised:
        module.main()
    assert token not in str(raised.value)
