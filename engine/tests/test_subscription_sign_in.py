"""The in-container subscription sign-in (BOSSAPP-769), driven against stub CLIs.

Each stub prints what the real CLI was observed to print (codex/grok: a device URL and a code;
claude: an authorize URL and a paste prompt that reads stdin), plus a line of "account data"
that must never leave the service.
"""
from __future__ import annotations

import os
import stat
import time
from pathlib import Path

import pytest

from boss_engine.services import subscription_sign_in as sign_in

ACCOUNT = "private-person@example.com"


@pytest.fixture(autouse=True)
def _clean():
    sign_in.reset_for_tests()
    yield
    sign_in.reset_for_tests()


def _stub(tmp_path: Path, name: str, body: str) -> Path:
    path = tmp_path / name
    path.write_text("#!/bin/sh\n" + body)
    path.chmod(path.stat().st_mode | stat.S_IXUSR)
    return path


def _device_cli(tmp_path: Path, *, exit_code: int = 0, url: str =
                "https://auth.openai.com/codex/device") -> Path:
    """Prints the link and code, waits for `approve`, then signs in (writes `done`)."""
    return _stub(tmp_path, "codex", f"""
echo 'WARNING: proceeding, even though we could not create PATH aliases'
printf '\\033[94m{url}\\033[0m\\n'
echo '2. Enter this one-time code (expires in 15 minutes)'
printf '   \\033[94mABCD-EFGH1\\033[0m\\n'
echo 'Signed in as {ACCOUNT}'
while [ ! -e "{tmp_path}/approve" ]; do sleep 0.05; done
[ {exit_code} -eq 0 ] && touch "{tmp_path}/done"
exit {exit_code}
""")


#: What the fake `claude setup-token` prints on success. Not a real credential.
FAKE_TOKEN = "sk-ant-oat01-" + "FakeT0ken_-x" * 8


def _claude_cli(tmp_path: Path) -> Path:
    """A fake `claude setup-token`, shaped like 2.1.197's Ink output (measured 2026-10-27 in a
    throwaway container): it refuses a pipe, moves the cursor where text has spaces, prints the
    authorize URL and the paste prompt, reads the code, and on a good one prints the long-lived
    token on its own line and exits 0. A refused code prints OAuth error and waits for Enter."""
    return _stub(tmp_path, "claude", f"""
[ -t 0 ] || {{ echo 'Raw mode is not supported on the current process.stdin'; exit 1; }}
printf 'Welcome\\033[9Gto\\033[12GClaude\\033[19GCode\\r\\n'
printf "Browser didn't open?\\033[23GUse the url below to sign in\\r\\n\\r\\n"
printf 'https://claude.com/cai/oauth/authorize?code=true&state=s1\\r\\n'
echo 'Org: {ACCOUNT}'
printf '\\033[2GPaste\\033[8Gcode\\033[13Ghere\\033[18Gif\\033[21Gprompted\\033[30G>\\r\\n'
read code
if [ "$code" = "good#s1" ]; then
  printf '\\033[32m\\342\\234\\223 Long-lived authentication token created successfully!\\033[39m\\r\\n'
  printf 'Your OAuth token (valid for 1 year):\\r\\n'
  printf '\\033[33m{FAKE_TOKEN}\\033[39m\\r\\n'
  printf "Store this token securely. You won't be able to see it again.\\r\\n"
  exit 0
fi
printf 'OAuth error: Requ\\033[20Gst failed with status code 400\\r\\n'
printf 'Press\\033[8GEnter\\033[14Gto retry.\\r\\n'
sleep 30
""")


class _Store:
    """The server-side store the API hands a Claude sign-in (claude_credential.sign_in_store)."""

    def __init__(self, raises: Exception | None = None):
        self.calls: list[tuple[str, object]] = []
        self.raises = raises

    def __call__(self, token, *, valid_days=None):
        self.calls.append((token, valid_days))
        if self.raises is not None:
            raise self.raises


def _until(predicate, timeout: float = 10.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(0.05)
    raise AssertionError("condition not reached")


def _start(provider: str, cli: Path, tmp_path: Path, **kw):
    return sign_in.start(provider, project_id="p1", actor="ceo", argv=(str(cli),),
                         signed_in=lambda: (tmp_path / "done").exists(), **kw)


def _no_account_data(session: dict) -> None:
    assert ACCOUNT not in str(session)
    assert set(session) == {"sign_in_id", "provider", "state", "verification_url", "user_code",
                            "needs_code_input", "expires_at", "headline", "fix"}
    assert session["headline"] and session["fix"]


def test_device_sign_in_returns_only_the_link_and_code_then_signs_in(tmp_path):
    session = _start("codex", _device_cli(tmp_path), tmp_path)
    assert session["state"] == "waiting", session
    assert session["provider"] == "codex"
    assert session["verification_url"] == "https://auth.openai.com/codex/device"
    assert session["user_code"] == "ABCD-EFGH1"
    assert session["needs_code_input"] is False
    assert session["expires_at"]
    _no_account_data(session)

    (tmp_path / "approve").touch()
    done = _until(lambda: (s := sign_in.status(session["sign_in_id"]))["state"] == "signed_in" and s)
    assert done["verification_url"] is None and done["user_code"] is None
    assert "Connect" in done["fix"]
    _no_account_data(done)


def test_grok_device_url_from_its_own_host(tmp_path):
    cli = _device_cli(tmp_path, url="https://accounts.x.ai/oauth2/device?user_code=ABCD-EFGH1")
    session = _start("grok", cli, tmp_path)
    assert session["state"] == "waiting"
    assert session["verification_url"].startswith("https://accounts.x.ai/oauth2/device")
    assert session["user_code"] == "ABCD-EFGH1"


def test_a_url_on_another_host_is_never_shown(tmp_path):
    cli = _device_cli(tmp_path, url="https://evil.example.com/device")
    session = _start("codex", cli, tmp_path)
    assert session["verification_url"] is None
    assert session["state"] == "starting"


def test_a_login_that_exits_non_zero_fails_with_a_plain_fix(tmp_path):
    session = _start("codex", _device_cli(tmp_path, exit_code=1), tmp_path)
    (tmp_path / "approve").touch()
    failed = _until(lambda: (s := sign_in.status(session["sign_in_id"]))["state"] == "failed" and s)
    assert "exit 1" in failed["fix"] and "again" in failed["fix"]
    assert failed["headline"] == "Sign-in failed"
    _no_account_data(failed)


def test_claude_runs_setup_token_not_the_short_lived_login():
    login = sign_in.LOGINS["claude_code"]
    assert login.argv == ("claude", "setup-token")
    assert login.pty and login.captures_token
    assert login.logout == ()                  # the CLI's own login (the fallback) is left alone


def test_claude_setup_token_is_stored_on_the_server_and_never_returned(tmp_path, caplog):
    import logging

    caplog.set_level(logging.DEBUG)
    store = _Store()
    session = _start("claude_code", _claude_cli(tmp_path), tmp_path, store=store)
    assert session["state"] == "waiting", session
    assert session["verification_url"].startswith("https://claude.com/cai/oauth/authorize")
    assert session["user_code"] is None
    assert session["needs_code_input"] is True
    assert "long-lived" in session["fix"]
    _no_account_data(session)

    sent = sign_in.submit_code(session["sign_in_id"], "good#s1")
    assert sent["needs_code_input"] is False
    assert "good#s1" not in str(sent)
    done = _until(lambda: (s := sign_in.status(session["sign_in_id"]))["state"] == "signed_in" and s)
    _no_account_data(done)
    # The whole token, once, to the server-side store; 1 year as the CLI said.
    assert store.calls == [(FAKE_TOKEN, 365)]
    # Never in what the client sees, never kept on the session, never logged.
    assert FAKE_TOKEN not in str(done) and "sk-ant-" not in str(done)
    kept = sign_in._SESSIONS[session["sign_in_id"]]
    assert FAKE_TOKEN not in kept.output and FAKE_TOKEN not in repr(kept)
    assert FAKE_TOKEN not in caplog.text and "sk-ant-oat" not in caplog.text
    _until(lambda: _process(session["sign_in_id"]).poll() is not None)


def test_claude_wrong_code_fails_and_a_code_is_refused_when_not_expected(tmp_path):
    store = _Store()
    session = _start("claude_code", _claude_cli(tmp_path), tmp_path, store=store)
    with pytest.raises(sign_in.SignInError) as bad:
        sign_in.submit_code(session["sign_in_id"], "two\nlines")
    assert bad.value.status == 422
    sign_in.submit_code(session["sign_in_id"], "wrong")
    failed = _until(lambda: (s := sign_in.status(session["sign_in_id"]))["state"] == "failed" and s)
    assert "paste the code exactly" in failed["fix"]
    # setup-token waits for Enter after a refused code: BOSS ends it.
    _until(lambda: _process(session["sign_in_id"]).poll() is not None)
    assert store.calls == []
    with pytest.raises(sign_in.SignInError) as refused:
        sign_in.submit_code(session["sign_in_id"], "good#s1")
    assert refused.value.status == 409
    assert set(refused.value.detail) == {"state", "headline", "fix"}


def test_a_store_that_fails_fails_the_sign_in_without_echoing_anything(tmp_path):
    for raises, said in ((sign_in.StoreRefused("Re-run the installer."), "Re-run the installer."),
                         (RuntimeError(f"driver said {FAKE_TOKEN}"), "RuntimeError")):
        sign_in.reset_for_tests()
        session = _start("claude_code", _claude_cli(tmp_path), tmp_path, store=_Store(raises))
        sign_in.submit_code(session["sign_in_id"], "good#s1")
        failed = _until(lambda: (s := sign_in.status(session["sign_in_id"]))["state"] == "failed"
                        and s)
        assert said in failed["fix"]
        assert FAKE_TOKEN not in str(failed) and "sk-ant-" not in str(failed)


def test_claude_is_signed_in_when_boss_holds_the_token_it_obtained(monkeypatch, tmp_path):
    """The shortcut asks the store for BOSS's own long-lived token — never the CLI's login."""
    from boss_engine.services import claude_credential

    marker = tmp_path / "ran"
    cli = _stub(tmp_path, "claude", f"touch '{marker}'\nsleep 5\n")
    monkeypatch.setattr(claude_credential, "boss_obtained_token_stored", lambda _s: True)
    session = sign_in.start("claude_code", project_id="p1", actor="ceo", argv=(str(cli),),
                            settings=object())
    assert session["state"] == "signed_in"
    time.sleep(0.3)
    assert not marker.exists()
    monkeypatch.setattr(claude_credential, "boss_obtained_token_stored", lambda _s: False)
    sign_in.reset_for_tests()
    session = sign_in.start("claude_code", project_id="p1", actor="ceo",
                            argv=(str(_claude_cli(tmp_path)),), settings=object(),
                            store=_Store())
    assert session["state"] == "waiting"


def test_device_flow_never_asks_for_a_code(tmp_path):
    session = _start("codex", _device_cli(tmp_path), tmp_path)
    with pytest.raises(sign_in.SignInError) as refused:
        sign_in.submit_code(session["sign_in_id"], "ABCD-EFGH1")
    assert refused.value.status == 409


def test_already_signed_in_answers_at_once_without_a_process(tmp_path):
    marker = tmp_path / "ran"
    cli = _stub(tmp_path, "codex", f"touch '{marker}'\nsleep 5\n")
    session = sign_in.start("codex", project_id="p1", actor="ceo", argv=(str(cli),),
                            signed_in=lambda: True)
    assert session["state"] == "signed_in"
    assert session["verification_url"] is None
    time.sleep(0.3)
    assert not marker.exists()


def test_a_rejected_login_signs_in_again_even_when_the_cli_says_logged_in(tmp_path):
    session = sign_in.start("codex", project_id="p1", actor="ceo",
                            argv=(str(_device_cli(tmp_path)),), signed_in=lambda: True,
                            login_rejected=True)
    assert session["state"] == "waiting"


def test_one_live_session_per_provider(tmp_path):
    cli = _device_cli(tmp_path)
    first = _start("codex", cli, tmp_path)
    second = _start("codex", cli, tmp_path)
    assert second["sign_in_id"] == first["sign_in_id"]


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    # A zombie (exited, not yet reaped) counts as gone.
    try:
        return Path(f"/proc/{pid}/stat").read_text().split()[2] != "Z"
    except OSError:
        return True


def _process(sign_in_id: str):
    return sign_in._SESSIONS[sign_in_id].process


def test_cancel_kills_the_login(tmp_path):
    session = _start("codex", _device_cli(tmp_path), tmp_path)
    process = _process(session["sign_in_id"])
    cancelled = sign_in.cancel(session["sign_in_id"])
    assert cancelled["state"] == "cancelled"
    assert cancelled["verification_url"] is None
    assert process.poll() is not None
    # A fresh start after a cancel is a new session.
    again = _start("codex", _device_cli(tmp_path), tmp_path)
    assert again["sign_in_id"] != session["sign_in_id"]


def test_expiry_kills_the_login(tmp_path, monkeypatch):
    monkeypatch.setattr(sign_in, "SESSION_TTL_SECONDS", 1)
    session = _start("codex", _device_cli(tmp_path), tmp_path)
    process = _process(session["sign_in_id"])
    expired = _until(lambda: (s := sign_in.status(session["sign_in_id"]))["state"] == "expired" and s)
    assert "15 minutes" in expired["fix"]
    _until(lambda: process.poll() is not None)


def test_shutdown_kills_every_live_login(tmp_path):
    session = _start("codex", _device_cli(tmp_path), tmp_path)
    process = _process(session["sign_in_id"])
    sign_in.shutdown()
    assert process.poll() is not None
    assert sign_in.status(session["sign_in_id"])["state"] == "cancelled"


def test_unknown_provider_and_unknown_session_are_refusals():
    with pytest.raises(sign_in.SignInError) as provider:
        sign_in.start("cursor", project_id="p1", actor="ceo")
    assert provider.value.status == 422
    with pytest.raises(sign_in.SignInError) as missing:
        sign_in.status("si_nope")
    assert missing.value.status == 404
    assert set(missing.value.detail) == {"state", "headline", "fix"}


def test_a_missing_cli_fails_with_a_plain_fix(tmp_path):
    session = sign_in.start("grok", project_id="p1", actor="ceo",
                            argv=(str(tmp_path / "no-such-grok"),), signed_in=lambda: False)
    assert session["state"] == "failed"
    assert "not installed" in session["fix"]


# ── the live loop, 2026-10-25: `boss connect codex` → "already signed in" → Login expired ──────
#
# The Codex row on the laptop: last_check.state == "failed" (the 401 proof), observed_capacity
# .credential_state == "rejected", and `codex login status` still "Logged in using ChatGPT".
# Every surface read Login expired (setup_view.source_state); the sign-in asked only
# last_check.state == "login_expired", said "already signed in", Connect failed again: a loop.

LIVE_CODEX_METADATA = {"last_check": {"ok": False, "state": "failed",
                                      "at": "2026-10-24T05:21:30+00:00",
                                      "receipt_id": "rcpt_example"}}
LIVE_CODEX_CAPACITY = {"credential_state": "rejected", "credential_present": True}


class _Rows:
    def __init__(self, rows):
        self._rows = rows

    def mappings(self):
        return self

    def all(self):
        return self._rows


class _Conn:
    """The one SELECT last_login_rejected runs, answered with the live Codex row."""

    def __init__(self, rows):
        self.rows = rows

    def begin_nested(self):
        import contextlib
        return contextlib.nullcontext()

    def execute(self, *_a, **_k):
        return _Rows(self.rows)


def test_the_live_stale_codex_row_reads_as_a_refused_login():
    import json as _json

    for metadata, capacity in ((LIVE_CODEX_METADATA, LIVE_CODEX_CAPACITY),
                               (_json.dumps(LIVE_CODEX_METADATA), _json.dumps(LIVE_CODEX_CAPACITY))):
        conn = _Conn([{"connection_metadata": metadata, "observed_capacity": capacity}])
        assert sign_in.last_login_rejected(conn, ["p1"], "codex", None) is True
    working = _Conn([{"connection_metadata": {"last_check": {"ok": True, "state": "working"}},
                      "observed_capacity": {}}])
    assert sign_in.last_login_rejected(working, ["p1"], "codex", None) is False


def test_a_refused_login_is_signed_out_then_signed_in_fresh(tmp_path):
    """The CLI says signed in; the login was refused (or the person asked fresh): BOSS runs the
    CLI's own sign-out first, then a new device login — never 'already signed in'."""
    cli = _device_cli(tmp_path)
    (tmp_path / "done").touch()                       # `codex login status`: logged in
    logout = _stub(tmp_path, "codex-logout", f'touch "{tmp_path}/logged_out"\n')
    for kw in ({"login_rejected": True}, {"fresh": True}):
        sign_in.reset_for_tests()
        (tmp_path / "logged_out").unlink(missing_ok=True)
        session = _start("codex", cli, tmp_path, logout_argv=(str(logout),), **kw)
        assert session["state"] == "waiting", (kw, session)
        assert session["user_code"] == "ABCD-EFGH1"
        assert (tmp_path / "logged_out").exists(), "the stale session is signed out first"
        sign_in.cancel(session["sign_in_id"])


def test_without_a_refusal_a_signed_in_cli_is_left_alone(tmp_path):
    cli = _device_cli(tmp_path)
    (tmp_path / "done").touch()
    logout = _stub(tmp_path, "codex-logout", f'touch "{tmp_path}/logged_out"\n')
    session = _start("codex", cli, tmp_path, logout_argv=(str(logout),))
    assert session["state"] == "signed_in"
    assert not (tmp_path / "logged_out").exists()


def test_every_provider_signs_out_with_its_own_cli():
    assert sign_in.LOGINS["codex"].logout == ("codex", "logout")
    assert sign_in.LOGINS["grok"].logout == ("grok", "logout")
    # Claude's sign-in mints a token and never touches the CLI's own login (claude_code above).
    assert sign_in.LOGINS["claude_code"].logout == ()


# ── the live failure, 2026-10-27: a good code, then "waiting" forever ─────────────────────────
#
# si_7bb5e3329f6676e1d118: the code went in, no token was captured, no refusal matched, and the
# session sat in "waiting" (still asking for a code it then refused with 409) until expiry. Ink
# redraws by rewriting only the CELLS that changed, and wraps a long token at the terminal's
# width, so the token never arrives as one run of characters.

#: Not a real credential: 13 + 95 characters, like a real one.
LONG_TOKEN = "sk-ant-oat01-" + ("Q7z_K-" * 16)[:95]


def _ink_token_output(width: int = 80) -> bytes:
    """What Ink sends after a good code: it climbs back over the prompt line, which held text
    with some characters in the very cells the token now needs, writes only the cells that
    differ (cursor jumps over the rest), colours it, and wraps it at `width`."""
    t = LONG_TOKEN
    first, rest = t[:width - 2], t[width - 2:]
    out = "\x1b[4A\r\x1b[2K\x1b[32m✓ Long-lived authentication token created successfully!\x1b[39m\r\n"
    out += "Your OAuth token (valid for 1 year):\x1b[K\r\n"
    # Cells 20..29 (1-based col 23..32) already show the right characters (see the stub).
    out += "\x1b[33m  " + first[:20] + "\x1b[33G" + first[30:] + "\r\n"
    out += "\x1b[K" + rest + "\x1b[39m\r\n"
    out += "\x1b[2mStore\x1b[7Gthis token securely. You won't be able to see it again.\x1b[22m\r\n"
    return out.encode()


def _ink_cli(tmp_path: Path, after_code: bytes, *, then: str = "exit 0") -> Path:
    (tmp_path / "after.bin").write_bytes(after_code)
    pre = "  " + "#" * 20 + LONG_TOKEN[20:30] + "#" * 40
    return _stub(tmp_path, "claude", f"""
[ -t 0 ] || exit 1
printf 'https://claude.com/cai/oauth/authorize?code=true&state=s1\\r\\n'
printf 'Your OAuth token (valid for 1 year):\\r\\n{pre}\\r\\n'
printf 'Paste code here if prompted > '
read code
cat "{tmp_path}/after.bin"
{then}
""")


def test_a_token_wrapped_and_redrawn_by_ink_is_read_whole(tmp_path, caplog):
    import logging

    caplog.set_level(logging.DEBUG)
    store = _Store()
    session = _start("claude_code", _ink_cli(tmp_path, _ink_token_output()), tmp_path,
                     store=store)
    sign_in.submit_code(session["sign_in_id"], "good#s1")
    done = _until(lambda: (s := sign_in.status(session["sign_in_id"]))["state"] != "waiting"
                  and s)
    assert done["state"] == "signed_in", done["fix"]
    assert store.calls == [(LONG_TOKEN, 365)]
    assert LONG_TOKEN not in str(done) and LONG_TOKEN not in caplog.text


def test_the_screen_rebuilds_what_spacing_or_deleting_escapes_cannot():
    import re as _re

    screen = sign_in._Screen(80)
    screen.feed(b"https://claude.com/x\r\nYour OAuth token (valid for 1 year):\r\n"
                + ("  " + "#" * 20 + LONG_TOKEN[20:30] + "#" * 40).encode()
                + b"\r\nPaste code here if prompted > good#s1\r\n")
    screen.feed(_ink_token_output()[:37])               # an escape cut at a read boundary
    screen.feed(_ink_token_output()[37:])
    assert sign_in._token_on_screen(screen.lines()) == LONG_TOKEN
    raw = _ink_token_output().decode()
    deleted = _re.sub(r"\x1b\[[0-9;]*[A-Za-z]", "", raw)
    assert LONG_TOKEN not in deleted                    # the old parse could never have matched
    # A token cut short (no wrapped rest) is never taken as whole by the screen either way
    # once the line after it is missing: _take_token waits for "Store this token".
    partial = sign_in._Screen(80)
    partial.feed(("  " + LONG_TOKEN[:30]).encode())
    assert sign_in._token_on_screen(partial.lines()) is None


def test_a_code_with_no_answer_fails_instead_of_hanging(tmp_path, monkeypatch):
    monkeypatch.setattr(sign_in, "CODE_ANSWER_SECONDS", 1.0)
    session = _start("claude_code", _ink_cli(tmp_path, b"\x1b[2K\r", then="sleep 30"), tmp_path,
                     store=_Store())
    sent = sign_in.submit_code(session["sign_in_id"], "good#s1")
    assert sent["headline"] == "Finishing sign-in" and "paste" not in sent["fix"].lower()
    failed = _until(lambda: (s := sign_in.status(session["sign_in_id"]))["state"] == "failed"
                    and s)
    assert "did not answer the code" in failed["fix"]
    _until(lambda: _process(session["sign_in_id"]).poll() is not None)


def test_an_exit_0_without_a_token_says_so(tmp_path):
    session = _start("claude_code", _ink_cli(tmp_path, b"done\r\n"), tmp_path, store=_Store())
    sign_in.submit_code(session["sign_in_id"], "good#s1")
    failed = _until(lambda: (s := sign_in.status(session["sign_in_id"]))["state"] == "failed"
                    and s)
    assert "did not receive a token" in failed["fix"]


def test_a_stuck_session_is_replaced_never_returned(tmp_path, monkeypatch):
    monkeypatch.setattr(sign_in, "STUCK_AFTER_CODE_SECONDS", 0.0)
    cli = _ink_cli(tmp_path, b"\x1b[2K\r", then="sleep 30")
    first = _start("claude_code", cli, tmp_path, store=_Store())
    sign_in.submit_code(first["sign_in_id"], "good#s1")
    old = _process(first["sign_in_id"])
    second = _start("claude_code", cli, tmp_path, store=_Store())
    assert second["sign_in_id"] != first["sign_in_id"]
    assert second["state"] == "waiting" and second["needs_code_input"] is True
    assert sign_in.status(first["sign_in_id"])["state"] == "cancelled"
    _until(lambda: old.poll() is not None)
    # Not stuck (no code yet): the same session again — unless the person asks to start over.
    assert _start("claude_code", cli, tmp_path, store=_Store())["sign_in_id"] == second["sign_in_id"]
    third = _start("claude_code", cli, tmp_path, store=_Store(), fresh=True)
    assert third["sign_in_id"] != second["sign_in_id"]
