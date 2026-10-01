"""The Community installer may record a CLI login without an unmetered model turn."""
from __future__ import annotations

import io
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts" / "install"))
import coding_subscriptions  # noqa: E402


class _Response(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *_args):
        self.close()


@pytest.mark.parametrize("provider", ["codex", "claude_code", "grok"])
def test_community_cli_connection_skips_live_proof_and_reports_unverified(
        monkeypatch, capsys, provider):
    sent = []

    def answer(request, timeout):
        sent.append((request, timeout))
        return _Response(json.dumps({
            "connected": True,
            "entitlement_recorded": True,
            "proof": {"ok": True, "skipped": True},
        }).encode())

    monkeypatch.setattr(coding_subscriptions.urllib.request, "urlopen", answer)
    coding_subscriptions.connect("http://127.0.0.1:8000", "private-api-key", provider)

    request, timeout = sent[0]
    assert timeout == 120
    assert request.full_url.endswith("/v1/setup/coding-agents/connect")
    assert json.loads(request.data) == {
        "project_id": "boss-dogfood", "actor": "ceo", "provider": provider,
        "prove": False,
    }
    output = capsys.readouterr().out
    assert "connection recorded" in output
    assert "unverified" in output
    assert "private-api-key" not in output
    assert "proved" not in output


@pytest.mark.parametrize("reply", [
    {"connected": False, "entitlement_recorded": False,
     "proof": {"ok": True, "skipped": True}},
    {"connected": True, "entitlement_recorded": False,
     "proof": {"ok": True, "skipped": True}},
    {"connected": True, "entitlement_recorded": True,
     "proof": {"ok": True}},
    {"connected": True, "entitlement_recorded": True, "proof": None},
])
def test_installer_refuses_to_claim_an_unrecorded_or_unexpected_connection(monkeypatch, reply):
    monkeypatch.setattr(coding_subscriptions.urllib.request, "urlopen",
                        lambda *_args, **_kwargs: _Response(json.dumps(reply).encode()))
    with pytest.raises(RuntimeError, match="BOSS (did not record|returned an unexpected)"):
        coding_subscriptions.connect("http://127.0.0.1:8000", "private-api-key", "claude_code")


def test_local_claude_status_uses_read_only_api_and_no_credential_bytes(monkeypatch):
    requests = []

    def answer(request, timeout):
        requests.append((request, timeout))
        return _Response(json.dumps({"agents": [{
            "name": "claude_code", "installed": True, "authenticated": True,
            "auth_method": "claude_ai", "token_present": False,
        }]}).encode())

    monkeypatch.setattr(coding_subscriptions.urllib.request, "urlopen", answer)
    coding_subscriptions.claude_local_session_ready(
        "http://127.0.0.1:8000", "private-api-key")

    request, timeout = requests[0]
    assert timeout == 30
    assert request.get_method() == "GET"
    assert request.data is None
    assert request.full_url.endswith("/v1/setup/coding-agents?project_id=boss-dogfood")


@pytest.mark.parametrize("status", [
    {"installed": False, "authenticated": True, "auth_method": "claude_ai",
     "token_present": False},
    {"installed": True, "authenticated": False, "auth_method": "none",
     "token_present": False},
    {"installed": True, "authenticated": True, "auth_method": "api_key",
     "token_present": False},
    {"installed": True, "authenticated": True, "auth_method": "claude_ai",
     "token_present": True},
])
def test_local_claude_refuses_nonlocal_or_legacy_token_status(monkeypatch, status):
    monkeypatch.setattr(coding_subscriptions.urllib.request, "urlopen",
                        lambda *_args, **_kwargs: _Response(json.dumps({
                            "agents": [{"name": "claude_code", **status}],
                        }).encode()))
    with pytest.raises(RuntimeError):
        coding_subscriptions.claude_local_session_ready(
            "http://127.0.0.1:8000", "private-api-key")


def test_local_claude_connect_does_not_read_or_upload_a_token(monkeypatch):
    calls = []
    monkeypatch.setattr(coding_subscriptions, "discover",
                        lambda: pytest.fail("container login must not read host CLI status"))
    monkeypatch.setattr(coding_subscriptions, "claude_local_session_ready",
                        lambda *_args: calls.append("status"))
    monkeypatch.setattr(coding_subscriptions, "connect",
                        lambda *_args: calls.append("connect"))
    monkeypatch.setattr(coding_subscriptions, "claude_credential",
                        lambda: pytest.fail("CLI-owned login must not be read"))
    monkeypatch.setattr(coding_subscriptions, "send_to_boss",
                        lambda *_args, **_kwargs: pytest.fail("no credential upload"))
    monkeypatch.setenv("BOSS_API_KEY", "private-api-key")
    monkeypatch.setattr(coding_subscriptions.sys, "argv",
                        ["coding_subscriptions.py", "--connect-claude-local"])

    assert coding_subscriptions.main() == 0
    assert calls == ["status", "connect"]


@pytest.mark.parametrize("backend", ["infisical", "local_vault"])
def test_a_claude_token_in_any_secret_store_is_recorded_as_connected(monkeypatch, backend):
    """The server stores a Claude token in the install's secret store — Infisical on an install
    made with --secret-store infisical. The installer demanded "local_vault" exactly, so it
    called the stored token a failure and never recorded the connection (found 2026-09-30:
    "BOSS did not store the Claude setup token in the local secret store")."""
    connected = []
    monkeypatch.setenv("BOSS_API_KEY", "k")
    monkeypatch.setattr(coding_subscriptions, "discover", lambda: {"codex": False, "claude_code": True})
    monkeypatch.setattr(coding_subscriptions, "claude_credential", lambda: "tok")
    monkeypatch.setattr(coding_subscriptions, "send_to_boss",
                        lambda *a, **k: {"ok": True, "backend": backend})
    monkeypatch.setattr(coding_subscriptions, "connect",
                        lambda _url, _key, provider: connected.append(provider))
    monkeypatch.setattr(sys, "argv", ["coding_subscriptions.py", "--connect-claude"])
    assert coding_subscriptions.main() == 0
    assert connected == ["claude_code"]


def test_a_claude_token_that_landed_in_the_database_is_refused(monkeypatch):
    monkeypatch.setenv("BOSS_API_KEY", "k")
    monkeypatch.setattr(coding_subscriptions, "discover", lambda: {"codex": False, "claude_code": True})
    monkeypatch.setattr(coding_subscriptions, "claude_credential", lambda: "tok")
    monkeypatch.setattr(coding_subscriptions, "send_to_boss",
                        lambda *a, **k: {"ok": True, "backend": "database"})
    monkeypatch.setattr(coding_subscriptions, "connect",
                        lambda *a: pytest.fail("recorded a token stored in the database"))
    monkeypatch.setattr(sys, "argv", ["coding_subscriptions.py", "--connect-claude"])
    with pytest.raises(RuntimeError, match="secret store"):
        coding_subscriptions.main()
