"""The installer subscription helper: sign in inside BOSS, then prove; never import."""
from __future__ import annotations

import io
import json
import urllib.error
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


def _view(provider, state="working"):
    """What POST /v1/setup/models answers: the whole Repo and models view."""
    return {"models": [{"provider": provider, "source_id": f"src-{provider}",
                        "pill": {"state": state, "label": "Working" if state == "working"
                                 else "Login expired"}}]}


@pytest.mark.parametrize("provider", ["codex", "claude_code", "grok"])
def test_the_installer_asks_boss_to_prove_the_connection_with_a_live_turn(
        monkeypatch, capsys, provider):
    """Recorded unproved, a wrong Claude credential looked connected until a workflow failed on
    it with a 401 (boss-1). The installer uses Repo and models' own connect, which answers one
    reserved, receipted test turn before the row says Working."""
    sent = []

    def answer(request, timeout):
        sent.append((request, timeout))
        return _Response(json.dumps(_view(provider)).encode())

    monkeypatch.setattr(coding_subscriptions.urllib.request, "urlopen", answer)
    coding_subscriptions.connect("http://127.0.0.1:8000", "private-api-key", provider)

    request, timeout = sent[0]
    assert timeout == 180
    assert request.full_url.endswith("/v1/setup/models")
    assert json.loads(request.data) == {
        "project_id": "boss-dogfood", "actor": "ceo", "provider": provider,
        "kind": "subscription",
    }
    output = capsys.readouterr().out
    assert "answered a test turn" in output
    assert "private-api-key" not in output


@pytest.mark.parametrize("reply", [
    {"models": []},
    {"models": [{"provider": "codex", "pill": {"state": "working"}}]},
    _view("claude_code", state="login_expired"),
    {"models": [{"provider": "claude_code", "pill": {"state": "not_checked"}}]},
])
def test_installer_refuses_to_claim_an_unrecorded_or_unproved_connection(monkeypatch, reply):
    monkeypatch.setattr(coding_subscriptions.urllib.request, "urlopen",
                        lambda *_args, **_kwargs: _Response(json.dumps(reply).encode()))
    with pytest.raises(RuntimeError, match="BOSS did not (record|prove)"):
        coding_subscriptions.connect("http://127.0.0.1:8000", "private-api-key", "claude_code")


def _refuse_with(detail, code=409):
    import urllib.error

    def refuse(*_a, **_k):
        raise urllib.error.HTTPError("http://x", code, "Refused", {},
                                     io.BytesIO(json.dumps({"detail": detail}).encode()))
    return refuse


def test_a_refused_connection_shows_boss_s_own_reason(monkeypatch):
    """BOSS's real refusal body ({state, headline, fix}, no `message`): printed as
    "headline — fix", never `None`, never advice the server did not give."""
    from boss_engine.services.setup_actions import subscription_import_refusal

    detail = subscription_import_refusal().detail
    monkeypatch.setattr(coding_subscriptions.urllib.request, "urlopen", _refuse_with(detail))
    with pytest.raises(RuntimeError) as refused:
        coding_subscriptions.connect("http://127.0.0.1:8000", "private-api-key", "claude_code")
    message = str(refused.value)
    assert f"{detail['headline']} — {detail['fix']}" in message
    assert "None" not in message
    assert "accept-database-fallback" not in message


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
    monkeypatch.setenv("BOSS_API_KEY", "private-api-key")
    monkeypatch.setattr(coding_subscriptions.sys, "argv",
                        ["coding_subscriptions.py", "--connect-claude-local"])

    assert coding_subscriptions.main() == 0
    assert calls == ["status", "connect"]


# ---- the one route: sign in inside BOSS, then prove ------------------------------------------

class _FakeBoss:
    """POST /v1/setup/sign-in -> waiting; GET -> `final` after `polls`; POST /v1/setup/models
    -> working. Records every request so a test can see exactly what was sent."""

    def __init__(self, *, provider="codex", first=None, polls=1, final="signed_in",
                 needs_code=False):
        self.provider, self.polls, self.final, self.needs_code = provider, polls, final, needs_code
        self.first = first
        self.sent = []

    def session(self, state):
        return {"sign_in_id": "si_1", "provider": self.provider, "state": state,
                "verification_url": "https://auth.openai.com/codex/device",
                "user_code": None if self.needs_code else "ABCD-EFGH1",
                "needs_code_input": self.needs_code and state == "waiting", "expires_at": None,
                "headline": {"failed": "Sign-in failed"}.get(state, "h"),
                "fix": {"failed": "Codex's login stopped. Start the sign-in again."}.get(state, "f")}

    def __call__(self, request, timeout):
        body = json.loads(request.data) if request.data else None
        path = request.full_url.split("8000", 1)[1]
        method = request.get_method()
        self.sent.append((method, path, body))
        if path == "/v1/setup/sign-in":
            return _Response(json.dumps(self.first or self.session("waiting")).encode())
        if path.endswith("/code"):
            self.needs_code = False
            return _Response(json.dumps(self.session("waiting")).encode())
        if path.startswith("/v1/setup/sign-in/") and method == "GET":
            self.polls -= 1
            state = self.final if self.polls <= 0 else "waiting"
            return _Response(json.dumps(self.session(state)).encode())
        if path.startswith("/v1/setup/sign-in/") and method == "DELETE":
            return _Response(json.dumps(self.session("cancelled")).encode())
        if path == "/v1/setup/models":
            return _Response(json.dumps(_view(body["provider"])).encode())
        raise AssertionError(path)


def _main(monkeypatch, fake, *argv, terminal=True):
    monkeypatch.setenv("BOSS_API_KEY", "private-api-key")
    monkeypatch.setattr(coding_subscriptions.urllib.request, "urlopen", fake)
    monkeypatch.setattr(coding_subscriptions.time, "sleep", lambda _s: None)
    monkeypatch.setattr(coding_subscriptions, "_terminal",
                        lambda: io.StringIO() if terminal else None)
    monkeypatch.setattr(coding_subscriptions, "discover",
                        lambda: pytest.fail("signing in inside BOSS never reads host logins"))
    monkeypatch.setattr(coding_subscriptions.sys, "argv", ["coding_subscriptions.py", *argv])
    return coding_subscriptions.main()


@pytest.mark.parametrize("argv", [("--sign-in", "codex"), ("--connect-codex",)])
def test_codex_signs_in_inside_boss_then_is_proved_once(monkeypatch, capsys, argv):
    fake = _FakeBoss()
    assert _main(monkeypatch, fake, *argv) == 0
    out = capsys.readouterr().out
    assert "https://auth.openai.com/codex/device" in out and "ABCD-EFGH1" in out
    assert "answered a test turn" in out
    assert fake.sent[0] == ("POST", "/v1/setup/sign-in", {
        "project_id": "boss-dogfood", "actor": "ceo", "provider": "codex"})
    models = [b for _m, p, b in fake.sent if p == "/v1/setup/models"]
    assert models == [{"project_id": "boss-dogfood", "actor": "ceo", "provider": "codex",
                       "kind": "subscription"}]
    assert not any("credential" in p for _m, p, _b in fake.sent)
    assert "private-api-key" not in out


def test_claude_asks_for_the_code_on_the_terminal_only_when_the_login_wants_one(
        monkeypatch, capsys):
    fake = _FakeBoss(provider="claude_code", needs_code=True)
    asked = []
    monkeypatch.setattr(coding_subscriptions, "_ask_code",
                        lambda _tty: asked.append(1) or "code#state")
    assert _main(monkeypatch, fake, "--sign-in", "claude") == 0
    assert asked == [1]
    codes = [b for _m, p, b in fake.sent if p.endswith("/code")]
    assert codes == [{"project_id": "boss-dogfood", "code": "code#state"}]
    assert "code#state" not in capsys.readouterr().out

    device = _FakeBoss()
    monkeypatch.setattr(coding_subscriptions, "_ask_code",
                        lambda _tty: pytest.fail("a device login never asks for a code"))
    assert _main(monkeypatch, device, "--sign-in", "codex") == 0


def test_already_signed_in_connects_without_a_terminal(monkeypatch):
    fake = _FakeBoss(first={"sign_in_id": "si_0", "provider": "grok", "state": "signed_in",
                            "headline": "h", "fix": "f"}, provider="grok")
    assert _main(monkeypatch, fake, "--sign-in", "grok", terminal=False) == 0
    assert [p for _m, p, _b in fake.sent] == ["/v1/setup/sign-in", "/v1/setup/models"]


def test_no_terminal_cancels_and_names_the_next_step(monkeypatch):
    fake = _FakeBoss()
    with pytest.raises(coding_subscriptions.NeedsPerson, match="Run: boss connect codex"):
        _main(monkeypatch, fake, "--sign-in", "codex", terminal=False)
    assert [m for m, _p, _b in fake.sent] == ["POST", "DELETE"]


def test_a_failed_sign_in_says_boss_s_words_and_connects_nothing(monkeypatch):
    fake = _FakeBoss(final="failed")
    with pytest.raises(RuntimeError) as failed:
        _main(monkeypatch, fake, "--sign-in", "codex")
    assert str(failed.value) == "Sign-in failed — Codex's login stopped. Start the sign-in again."
    assert not any(p == "/v1/setup/models" for _m, p, _b in fake.sent)


def test_a_refused_start_is_rendered_as_headline_and_fix(monkeypatch):
    monkeypatch.setattr(coding_subscriptions.urllib.request, "urlopen", _refuse_with(
        {"state": "failed", "headline": "Not a subscription",
         "fix": "Sign-in works for codex, claude_code and grok."}, code=422))
    with pytest.raises(RuntimeError) as refused:
        coding_subscriptions.sign_in("http://127.0.0.1:8000", "k", "codex",
                                     terminal=io.StringIO())
    assert str(refused.value) == ("Not a subscription — Sign-in works for codex, claude_code "
                                  "and grok.")


class _StaleLoginBoss(_FakeBoss):
    """The live loop (2026-10-25): the first sign-in answers signed_in (the CLI's status says
    logged in), and Connect is refused Login expired. The fix: sign out, sign in fresh, Connect."""

    def __init__(self, *, refuse_again=False):
        super().__init__()
        self.connects = 0
        self.refuse_again = refuse_again

    def __call__(self, request, timeout):
        body = json.loads(request.data) if request.data else None
        path = request.full_url.split("8000", 1)[1]
        if path == "/v1/setup/sign-in" and not (body or {}).get("fresh"):
            self.sent.append((request.get_method(), path, body))
            return _Response(json.dumps({**self.session("signed_in"), "sign_in_id": "si_0"})
                             .encode())
        if path == "/v1/setup/models":
            self.connects += 1
            if self.connects == 1 or self.refuse_again:
                self.sent.append((request.get_method(), path, body))
                raise urllib.error.HTTPError(
                    request.full_url, 409, "refused", {}, io.BytesIO(json.dumps({"detail": {
                        "state": "login_expired", "headline": "Login expired",
                        "fix": "Run `boss connect codex` (signs in inside BOSS) or press Sign "
                               "in, then Test."}}).encode()))
        return super().__call__(request, timeout)


def test_a_stale_login_is_signed_out_and_signed_in_fresh_never_a_loop(monkeypatch, capsys):
    fake = _StaleLoginBoss()
    assert _main(monkeypatch, fake, "--sign-in", "codex") == 0
    out = capsys.readouterr().out
    starts = [b for m, p, b in fake.sent if p == "/v1/setup/sign-in"]
    assert starts == [{"project_id": "boss-dogfood", "actor": "ceo", "provider": "codex"},
                      {"project_id": "boss-dogfood", "actor": "ceo", "provider": "codex",
                       "fresh": True}]
    assert fake.connects == 2
    assert "signing it out and signing in again" in out
    assert "ABCD-EFGH1" in out and "answered a test turn" in out


def test_a_fresh_sign_in_refused_again_says_why_and_stops(monkeypatch):
    fake = _StaleLoginBoss(refuse_again=True)
    with pytest.raises(RuntimeError) as failed:
        _main(monkeypatch, fake, "--sign-in", "codex")
    assert "The new sign-in was refused too" in str(failed.value)
    assert fake.connects == 2, "one fresh sign-in, never a loop"
