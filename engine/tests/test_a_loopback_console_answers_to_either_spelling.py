"""A LOOPBACK CONSOLE ANSWERS TO EITHER SPELLING (stranger test r3 review of #208).

Every console URL the host tools print now says http://localhost:<port>. An install claimed on
http://127.0.0.1:<port> (legacy password, or before rc10) has THAT bound as its public origin, and
a sign-in from localhost failed `csrf_failed`. The origin checks accept another loopback spelling
(localhost, 127.0.0.1, [::1]) of a loopback bound origin on the same scheme and port; nothing
else. A passkey still belongs to the bound origin's RP ID (webauthn.rp_for keeps it). Pure; no DB.
"""
from __future__ import annotations

import pytest

from boss_engine.identity import authn
from boss_engine.identity.authn import AuthnError, RequestCredentials
from boss_engine.identity.authn_support import same_console_origin


def _creds(origin: str) -> RequestCredentials:
    return RequestCredentials("POST", "/v1/auth/login", {"origin": origin}, {})


@pytest.mark.parametrize("origin", ["http://localhost:3100", "http://127.0.0.1:3100", "http://[::1]:3100"])
def test_a_127_bound_install_accepts_each_loopback_spelling(origin: str) -> None:
    assert same_console_origin(origin, "http://127.0.0.1:3100")
    authn.check_origin_only(_creds(origin), public_origin="http://127.0.0.1:3100")


def test_a_localhost_bound_install_accepts_127() -> None:
    authn.check_origin_only(_creds("http://127.0.0.1:3000"), public_origin="http://localhost:3000")


@pytest.mark.parametrize("origin", [
    "https://evil.example",          # not loopback
    "http://localhost.evil.example:3100",
    "http://10.0.0.5:3100",
    "http://localhost:3101",         # another port is another site (previews live there)
    "https://localhost:3100",        # another scheme
    "",
])
def test_anything_else_is_still_refused(origin: str) -> None:
    with pytest.raises(AuthnError) as err:
        authn.check_origin_only(_creds(origin), public_origin="http://127.0.0.1:3100")
    assert err.value.code == "csrf_failed"


def test_a_non_loopback_bound_origin_never_accepts_loopback() -> None:
    assert not same_console_origin("http://localhost:443", "https://boss.example.com")
    with pytest.raises(AuthnError):
        authn.check_origin_only(_creds("http://localhost:3000"), public_origin="https://boss.example.com")


def test_the_session_csrf_check_uses_the_same_rule() -> None:
    import inspect

    assert "same_console_origin(origin, expected)" in inspect.getsource(authn.check_csrf)


def test_a_passkey_keeps_the_bound_rp_id() -> None:
    from boss_engine.identity import webauthn

    assert webauthn.rp_for("http://localhost:3000") == ("localhost", "http://localhost:3000")
    assert webauthn.passkey_blocker("http://127.0.0.1:3000") == webauthn.NEEDS_DOMAIN
