"""A previewed app's session cookie survives the preview sandbox (prev_7b65d5d7165c4c829b1b).

The sandbox CSP gives the page an opaque origin, so every fetch is cross-site and a browser drops a
SameSite=Lax cookie from the answer: HelpDesk AI's sign-in answered 200 and nobody stayed signed in.
"""
from boss_engine.services.preview_cookies import (
    cookie_path, for_the_sandbox, is_relayable_set_cookie, is_secure_context, outbound_set_cookie,
)


def test_in_a_secure_context_the_cookie_is_cross_site_and_still_this_previews():
    made = outbound_set_cookie("prv_a", "sid=abc; Path=/; HttpOnly; SameSite=Lax")
    out = for_the_sandbox(made, secure_context=True)
    assert out.endswith("; SameSite=None; Secure") and "SameSite=Lax" not in out
    assert f"Path={cookie_path('prv_a')}" in out and "HttpOnly" in out
    assert is_relayable_set_cookie("prv_a", out) and not is_relayable_set_cookie("prv_b", out)


def test_on_plain_http_elsewhere_the_cookie_is_left_as_the_app_made_it():
    made = outbound_set_cookie("prv_a", "sid=abc; Path=/; SameSite=Lax")
    assert for_the_sandbox(made, secure_context=False) == made


def test_which_origins_keep_a_secure_cookie():
    assert is_secure_context("https", "boss-1.example.ts.net")
    for host in ("localhost:3001", "127.0.0.1:3001", "[::1]:3001", "app.localhost"):
        assert is_secure_context("http", host), host
    assert not is_secure_context("http", "192.168.1.20:3001")
