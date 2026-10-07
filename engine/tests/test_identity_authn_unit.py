"""Stream S1 unit tests without a database: argon2id profiles and the memory semaphore (A13), the
password rules, key format and checksum (A28), CSRF, client IP resolution and cookie attributes.
"""
from __future__ import annotations

import threading
import time

import pytest

from boss_engine.identity import authn, authn_support, passwords, sessions
from boss_engine.identity.authn import AuthnError, RequestCredentials


@pytest.fixture(autouse=True)
def _small(monkeypatch):
    from boss_engine.config import get_settings

    monkeypatch.setattr(get_settings(), "argon2_profile", "small")
    monkeypatch.setattr(get_settings(), "identity_server_key", "dGVzdC1vbmx5LWlkZW50aXR5LXNlcnZlci1rZXktLS0tLS0tLS0tLS0tLS0=")   # test-only (F3/L2)
    authn_support.reset_server_key_for_tests()
    yield
    authn_support.reset_server_key_for_tests()


# ---- argon2id -------------------------------------------------------------------------------------

def test_hashes_are_phc_argon2id_with_the_profile_parameters(monkeypatch):
    from boss_engine.config import get_settings

    small = passwords.hash_password("correct horse battery staple")
    assert small.startswith("$argon2id$v=19$m=19456,t=2,p=1$")
    monkeypatch.setattr(get_settings(), "argon2_profile", "default")
    assert passwords.PROFILES["default"].memory_kib == 65536 and passwords.PROFILES["default"].iterations == 3
    assert passwords.needs_rehash(small)                       # below the default profile: upgrade
    assert not passwords.needs_rehash("$argon2id$v=19$m=65536,t=3,p=1$c2FsdA$aGFzaA")
    assert passwords.needs_rehash("$2b$12$bcrypt-is-not-argon2id")


def test_verify_normalises_nfkc_and_never_truncates():
    phc = passwords.hash_password("ｐａｓｓｐｈｒａｓｅ-long-1")   # full-width → NFKC
    assert passwords.verify_password("passphrase-long-1", phc)
    long_pw = "x" * 100 + "A"
    assert not passwords.verify_password("x" * 100 + "B", passwords.hash_password(long_pw))
    assert not passwords.verify_password("anything", None)        # dummy work, never a match


def test_a13_at_most_four_hashes_in_flight_then_busy(monkeypatch):
    live = {"now": 0, "max": 0}
    lock = threading.Lock()

    class SlowArgon:
        def __init__(self, **kwargs):
            pass

        def derive_phc_encoded(self, secret):
            with lock:
                live["now"] += 1
                live["max"] = max(live["max"], live["now"])
            time.sleep(0.05)
            with lock:
                live["now"] -= 1
            return "$argon2id$v=19$m=1,t=1,p=1$c2FsdA$aGFzaA"

    monkeypatch.setattr(passwords, "Argon2id", SlowArgon)
    monkeypatch.setattr(passwords, "QUEUE_SECONDS", 0.12)
    outcomes: list[str] = []

    def one():
        try:
            passwords.hash_password("a passphrase that is long")
            outcomes.append("ok")
        except passwords.PasswordBusy:
            outcomes.append("busy")

    threads = [threading.Thread(target=one) for _ in range(50)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert live["max"] <= passwords.MAX_CONCURRENT_HASHES
    assert "busy" in outcomes and "ok" in outcomes


@pytest.mark.parametrize("pw, code", [
    ("tiny", "password_too_short"),
    ("x" * 129, "password_too_long"),
    ("passwordpassword", "password_too_common"),
    ("boss12345678", "password_too_common"),
    ("alice2024alice", "password_too_common"),
])
def test_new_password_rules(pw, code):
    with pytest.raises(passwords.PasswordRejected) as exc:
        passwords.check_new_password(pw, email="alice@example.test")
    assert exc.value.code == code and pw not in str(exc.value)


def test_a_good_password_passes_and_composition_rules_do_not_exist():
    assert passwords.check_new_password("all lowercase words here", email="a@b.test")


def test_hibp_unreachable_accepts_with_a_warning(monkeypatch):
    from boss_engine.identity import http

    def refuse(*a, **k):
        raise http.OutboundRefused("offline")

    monkeypatch.setattr(http, "fetch", refuse)
    assert passwords.hibp_check("whatever passphrase") == passwords.BreachCheck(False, False)


# ---- keys -------------------------------------------------------------------------------------------

def test_key_format_and_checksum():
    for kind, prefix in authn.API_KEY_PREFIXES.items():
        key = authn.mint_api_key(kind)
        assert key.startswith(prefix) and len(key) == len(prefix) + 49
        parsed = authn.parse_api_key(key)
        assert parsed.kind == kind and parsed.checksum_ok
        flipped = key[:len(prefix) + 3] + ("A" if key[len(prefix) + 3] != "A" else "B") + key[len(prefix) + 4:]
        assert authn.parse_api_key(flipped).checksum_ok is False
    assert authn.parse_api_key("bss_session") is None
    assert len(authn.hash_token("x")) == 64


# ---- CSRF -------------------------------------------------------------------------------------------

def _creds(headers):
    return RequestCredentials("POST", "/v1/x", {k.lower(): v for k, v in headers.items()}, {})


def test_csrf_needs_origin_fetch_site_and_the_session_hmac():
    sid = "ses_abc"
    good = {"Origin": "https://boss.test", "Sec-Fetch-Site": "same-origin",
            authn.CSRF_HEADER: sessions.csrf_token(sid)}
    authn.check_csrf(_creds(good), session_id=sid, public_origin="https://boss.test")
    for broken in ({**good, "Origin": "https://evil.test"}, {k: v for k, v in good.items() if k != "Origin"},
                   {**good, "Sec-Fetch-Site": "cross-site"}, {**good, authn.CSRF_HEADER: "nope"},
                   {**good, authn.CSRF_HEADER: sessions.csrf_token("ses_other")}):
        with pytest.raises(AuthnError) as exc:
            authn.check_csrf(_creds(broken), session_id=sid, public_origin="https://boss.test")
        assert (exc.value.code, exc.value.status) == ("csrf_failed", 403)


def test_client_ip_believes_forwarded_for_only_from_a_trusted_proxy():
    trusted = "127.0.0.0/8"
    assert authn_support.resolve_client_ip("203.0.113.5", "10.0.0.1", trusted) == "203.0.113.5"
    assert authn_support.resolve_client_ip("127.0.0.1", "198.51.100.7", trusted) == "198.51.100.7"
    # a client-written hop LEFT of the proxy's own append is never chosen
    assert authn_support.resolve_client_ip("127.0.0.1", "1.2.3.4, 198.51.100.7", trusted) == "198.51.100.7"


def test_identity_cookies_are_always_host_prefixed_secure_httponly_lax():
    header = sessions.set_cookie_header(authn.SESSION_COOKIE, "bss_x", max_age=10)
    assert header == "__Host-boss_session=bss_x; Path=/; Secure; HttpOnly; SameSite=Lax; Max-Age=10"
    with pytest.raises(ValueError):
        sessions.set_cookie_header("boss_session", "x", max_age=1)


def test_a20_only_https_or_loopback_origins_may_carry_a_session():
    assert authn_support.secure_origin_ok("https://boss.example")
    assert authn_support.secure_origin_ok("http://localhost:3000")
    assert authn_support.secure_origin_ok("http://127.0.0.1:3000")
    assert not authn_support.secure_origin_ok("http://boss.example")
    assert not authn_support.secure_origin_ok(None)


def test_rate_limit_identifiers_are_never_stored_in_clear():
    h = authn_support.keyed_hash("ratelimit:signin.account", "alice@example.test")
    assert "alice" not in h and len(h) == 64
    assert authn_support.identifier_hash("Alice@Example.test") == authn_support.identifier_hash("alice@example.test")
