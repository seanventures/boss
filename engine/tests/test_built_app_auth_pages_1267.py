"""Login, register, and password-reset pages that ship inside a built app.

A preview edit changes that app's copy. It does not rewrite the BOSS console
sign-in, and it does not add a magic-link sign-in for the console.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from boss_engine.services.built_app_auth_pages import (
    CONSOLE_SIGN_IN_HEADLINE,
    AuthPageError,
    AuthPageStore,
    edit_from_preview,
    fresh_pages,
    render_page,
)

CONSOLE_SIGN_IN = (
    Path(__file__).resolve().parents[2] / "apps" / "web" / "app" / "(auth)" / "login" / "sign-in.tsx"
)

LOGIN_LEDE = "Use the email and password this app already knows."
REGISTER_LEDE = "This account belongs to the app, not to BOSS."
RESET_LEDE = "Ask this app for a reset. It does not sign you in."


def test_the_built_app_renders_login_register_and_password_reset():
    pages = fresh_pages()
    login = render_page(pages, "login", app_name="Harbor")
    register = render_page(pages, "register", app_name="Harbor")
    reset = render_page(pages, "password-reset", app_name="Harbor")

    assert "<h1>Sign in</h1>" in login
    assert f"<p>{LOGIN_LEDE}</p>" in login
    assert len(LOGIN_LEDE.split()) > 6
    assert 'action="/login"' in login
    assert 'data-owner="built-app"' in login
    assert 'data-path="/login"' in login
    assert "Harbor" in login
    assert 'type="password"' in login

    assert "<h1>Create an account</h1>" in register
    assert f"<p>{REGISTER_LEDE}</p>" in register
    assert len(REGISTER_LEDE.split()) > 6
    assert 'action="/register"' in register
    assert 'data-path="/register"' in register
    assert 'autocomplete="new-password"' in register

    assert "<h1>Reset password</h1>" in reset
    assert f"<p>{RESET_LEDE}</p>" in reset
    assert len(RESET_LEDE.split()) > 6
    assert 'action="/password-reset"' in reset
    assert 'data-path="/password-reset"' in reset
    assert 'type="password"' not in reset

    for html in (login, register, reset):
        assert "Welcome back" not in html
        assert "magic" not in html.lower()
        assert "/v1/auth" not in html
        assert CONSOLE_SIGN_IN_HEADLINE == "Welcome back"


def test_a_preview_edit_changes_only_that_apps_copy():
    original = fresh_pages()
    edited = edit_from_preview(
        original, kind="login", field="headline", value="Members only",
    )
    assert original["login"]["headline"] == "Sign in"
    assert edited["login"]["headline"] == "Members only"
    assert edited["register"]["headline"] == "Create an account"
    assert edited["password-reset"]["headline"] == "Reset password"

    html = render_page(edited, "login")
    assert "<h1>Members only</h1>" in html
    assert "<h1>Sign in</h1>" not in html
    assert f"<p>{LOGIN_LEDE}</p>" in html
    assert "Welcome back" not in html

    again = edit_from_preview(
        edited, kind="register", field="lede", value="Bring your own name.",
    )
    assert again["login"]["headline"] == "Members only"
    assert "<p>Bring your own name.</p>" in render_page(again, "register")
    assert f"<p>{REGISTER_LEDE}</p>" not in render_page(again, "register")


def test_a_preview_edit_does_not_touch_the_console_sign_in():
    before = CONSOLE_SIGN_IN.read_bytes()
    assert b"Welcome back" in before
    assert b"Members only" not in before

    store = AuthPageStore()
    store.edit("harbor", kind="login", field="headline", value="Members only")
    harbor = store.pages("harbor")
    other = store.pages("other")

    assert harbor["login"]["headline"] == "Members only"
    assert other["login"]["headline"] == "Sign in"
    assert "Welcome back" not in render_page(harbor, "login")
    assert CONSOLE_SIGN_IN_HEADLINE == "Welcome back"
    assert "console" not in harbor
    assert CONSOLE_SIGN_IN.read_bytes() == before

    leaked = store.pages("harbor")
    leaked["login"]["headline"] = "Changed behind the store"
    assert store.pages("harbor")["login"]["headline"] == "Members only"


def test_a_bad_preview_edit_is_refused_and_leaves_the_page():
    pages = fresh_pages()
    with pytest.raises(AuthPageError, match="Write something to save"):
        edit_from_preview(pages, kind="login", field="headline", value="   ")
    with pytest.raises(AuthPageError, match="That line is too long"):
        edit_from_preview(pages, kind="login", field="headline", value="M" * 121)
    with pytest.raises(AuthPageError, match="Not the console sign-in"):
        edit_from_preview(pages, kind="console-sign-in", field="headline", value="Members only")
    with pytest.raises(AuthPageError, match="Unknown field"):
        edit_from_preview(pages, kind="login", field="action", value="/v1/auth/magic-link")
    with pytest.raises(AuthPageError, match="Unknown page"):
        render_page(pages, "magic-link")
    assert pages["login"]["headline"] == "Sign in"
    assert "<h1>Sign in</h1>" in render_page(pages, "login")


def test_a_preview_edit_cannot_inject_markup():
    pages = edit_from_preview(
        fresh_pages(), kind="login", field="headline", value="<script>alert(1)</script>",
    )
    html = render_page(pages, "login", app_name="<script>")
    assert "<script>" not in html
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in html
    assert "&lt;script&gt;" in html
