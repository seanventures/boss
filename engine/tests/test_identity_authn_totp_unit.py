"""Stream S1 unit tests: TOTP arithmetic (RFC 6238), the ±1 window and replay boundary, and
recovery-code normalisation. No database, no app."""
from __future__ import annotations

import base64

from boss_engine.identity import recovery, totp

RFC_SECRET = base64.b32encode(b"12345678901234567890").decode()


def test_code_matches_the_rfc_6238_sha1_vector():
    # T = 59 s -> step 1 -> 94287082 (8 digits); BOSS uses 6 digits: 287082
    assert totp.code_at(RFC_SECRET, 59 // 30) == "287082"
    # T = 1111111109 -> 07081804 -> 081804
    assert totp.code_at(RFC_SECRET, 1111111109 // 30) == "081804"


def test_the_window_is_plus_minus_one_step_and_nothing_wider():
    now = 1_000_000_000.0
    step = totp.current_step(now)
    for offset in (-1, 0, 1):
        assert totp.match_step(RFC_SECRET, totp.code_at(RFC_SECRET, step + offset), last_step=0, now=now) == step + offset
    for offset in (-2, 2):
        assert totp.match_step(RFC_SECRET, totp.code_at(RFC_SECRET, step + offset), last_step=0, now=now) is None


def test_a_code_at_or_below_the_last_accepted_step_is_refused():
    now = 1_000_000_000.0
    step = totp.current_step(now)
    code = totp.code_at(RFC_SECRET, step)
    assert totp.match_step(RFC_SECRET, code, last_step=step, now=now) is None        # replay
    assert totp.match_step(RFC_SECRET, code, last_step=step - 1, now=now) == step
    older = totp.code_at(RFC_SECRET, step - 1)
    assert totp.match_step(RFC_SECRET, older, last_step=step - 1, now=now) is None


def test_malformed_codes_never_match():
    for bad in ("", "12345", "1234567", "abcdef", "12 34 5"):
        assert totp.match_step(RFC_SECRET, bad, last_step=0) is None


def test_otpauth_uri_names_the_parameters():
    uri = totp.otpauth_uri(RFC_SECRET, "ann@example.test")
    assert uri.startswith("otpauth://totp/BOSS%3Aann%40example.test?") and "digits=6" in uri and "period=30" in uri


def test_recovery_codes_normalise_case_and_separators():
    assert recovery._normalize("abcd-efgh-2345") == "ABCDEFGH2345"
    assert recovery._normalize(" ab cd ef gh 23 45 ") == "ABCDEFGH2345"
    assert recovery._display("ABCDEFGH2345") == "ABCD-EFGH-2345"
    # characters outside the base32 alphabet are dropped, so 0/1/8/9 cannot sneak in
    assert recovery._normalize("0189") == ""
