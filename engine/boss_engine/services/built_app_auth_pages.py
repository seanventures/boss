"""Login, register, and password-reset pages that ship inside a built app.

The preview edits this copy. The BOSS console sign-in is a different page and
is not rendered or updated here. There is no magic-link sign-in in this module.
"""
from __future__ import annotations

import html
from copy import deepcopy
from typing import Mapping, Optional

PAGE_KINDS = ("login", "register", "password-reset")
EDITABLE = ("headline", "lede", "submit")
CONSOLE_KINDS = ("console", "console-sign-in", "console_sign_in", "boss-sign-in", "(auth)")

#: The console sign-in's headline. Kept so a caller can prove an edit did not write it.
CONSOLE_SIGN_IN_HEADLINE = "Welcome back"

PATHS = {
    "login": "/login",
    "register": "/register",
    "password-reset": "/password-reset",
}

_STUB = {"login": "Login", "register": "Register", "password-reset": "Reset"}

_DEFAULTS: dict[str, dict[str, str]] = {
    "login": {
        "headline": "Sign in",
        "lede": "Use the email and password this app already knows.",
        "submit": "Sign in",
        "email_label": "Email",
        "secret_label": "Password",
    },
    "register": {
        "headline": "Create an account",
        "lede": "This account belongs to the app, not to BOSS.",
        "submit": "Create account",
        "name_label": "Name",
        "email_label": "Email",
        "secret_label": "Password",
    },
    "password-reset": {
        "headline": "Reset password",
        "lede": "Ask this app for a reset. It does not sign you in.",
        "submit": "Send reset",
        "email_label": "Email",
    },
}

_MAX = 120

# Mineral paper, pine ink, one brass stub. Not the console's navy sign-in.
_STYLE = """
:root{color-scheme:light;--paper:#e4ebe6;--card:#f4f7f5;--ink:#1a2e26;--muted:#4e655b;
--line:#c5d1c9;--brass:#9a6430;--bad:#8f2d2d}
*{box-sizing:border-box}
body{margin:0;min-height:100vh;display:grid;place-items:center;background:var(--paper);color:var(--ink);
font:16px/1.45 "Avenir Next","Trebuchet MS","Segoe UI",sans-serif;padding:24px}
article{display:flex;width:min(440px,100%);background:var(--card);border:1px solid var(--line)}
.stub{display:flex;flex-direction:column;justify-content:space-between;align-items:center;gap:12px;
width:46px;padding:16px 0;background:#24362e;color:#f3e2cf;font:600 12px/1 "Avenir Next Condensed","Arial Narrow",sans-serif;
letter-spacing:.18em;text-transform:uppercase}
.stub i{flex:1;width:0;border-left:1px dashed #9a6430}
main{flex:1;padding:28px 26px 22px}
.brand{margin:0 0 14px;font:600 13px/1 "Avenir Next Condensed","Arial Narrow",sans-serif;letter-spacing:.08em;
text-transform:uppercase;color:var(--brass)}
h1{margin:0 0 8px;font:700 32px/1.05 "Avenir Next Condensed","Arial Narrow",sans-serif;letter-spacing:-.02em}
p{margin:0 0 18px;color:var(--muted)}
label{display:block;margin:0 0 6px;font-size:13px;font-weight:600}
input{width:100%;margin:0 0 14px;padding:10px 12px;border:1px solid var(--line);background:#fff;color:var(--ink);
font:inherit}
button{width:100%;padding:11px 14px;border:0;background:var(--brass);color:#fffaf4;font:600 15px/1 inherit;cursor:pointer}
.links{display:flex;justify-content:space-between;gap:12px;margin-top:14px;font-size:14px}
a{color:var(--brass)}
:focus-visible{outline:2px solid var(--brass);outline-offset:2px}
@media (max-width:480px){article{flex-direction:column}.stub{flex-direction:row;width:auto;padding:10px 14px}
.stub i{width:auto;height:0;border-left:0;border-top:1px dashed #9a6430}}
@media (prefers-reduced-motion:reduce){*{transition:none}}
"""


class AuthPageError(ValueError):
    """A preview edit or a page request that cannot be done. The text is for a person."""


def fresh_pages() -> dict[str, dict[str, str]]:
    """The three pages as a built app ships them, before any preview edit."""
    return deepcopy(_DEFAULTS)


def _materialize(pages: Optional[Mapping]) -> dict[str, dict[str, str]]:
    base = fresh_pages()
    if not pages:
        return base
    for kind in PAGE_KINDS:
        got = pages.get(kind)
        if not isinstance(got, Mapping):
            continue
        for field, value in got.items():
            if field in base[kind] and isinstance(value, str):
                base[kind][field] = value
    return base


def edit_from_preview(pages: Optional[Mapping], *, kind: str, field: str, value: str) -> dict:
    """Return a new copy with one field changed. Does not mutate `pages`.

    Refuses the console sign-in, an unknown page, an unknown field, a blank
    value, or a line over 120 characters. The message is the visible error.
    """
    if kind in CONSOLE_KINDS:
        raise AuthPageError("Not the console sign-in.")
    if kind not in PAGE_KINDS:
        raise AuthPageError("Unknown page.")
    if field not in EDITABLE:
        raise AuthPageError("Unknown field.")
    text = (value or "").strip()
    if not text:
        raise AuthPageError("Write something to save.")
    if len(text) > _MAX:
        raise AuthPageError("That line is too long.")
    out = _materialize(pages)
    out[kind][field] = text
    return out


def _fields(kind: str, copy: Mapping[str, str]) -> str:
    e = html.escape
    email = (
        f'<label for="email">{e(copy["email_label"])}</label>'
        f'<input id="email" name="email" type="email" autocomplete="username" required>'
    )
    if kind == "password-reset":
        return email
    auto = "new-password" if kind == "register" else "current-password"
    secret = (
        f'<label for="password">{e(copy["secret_label"])}</label>'
        f'<input id="password" name="password" type="password" autocomplete="{auto}" required>'
    )
    if kind == "register":
        name = (
            f'<label for="name">{e(copy["name_label"])}</label>'
            f'<input id="name" name="name" type="text" autocomplete="name" required>'
        )
        return name + email + secret
    return email + secret


def _links(kind: str) -> str:
    if kind == "login":
        return (
            '<div class="links"><a href="/register">Create an account</a>'
            '<a href="/password-reset">Reset password</a></div>'
        )
    if kind == "register":
        return '<div class="links"><a href="/login">Sign in</a></div>'
    return '<div class="links"><a href="/login">Back to sign in</a></div>'


def render_page(pages: Optional[Mapping], kind: str, *, app_name: str = "This app") -> str:
    """One HTML document the built app would serve for this door."""
    if kind in CONSOLE_KINDS:
        raise AuthPageError("Not the console sign-in.")
    if kind not in PAGE_KINDS:
        raise AuthPageError("Unknown page.")
    copy = _materialize(pages)[kind]
    e = html.escape
    path = PATHS[kind]
    name = (app_name or "").strip() or "This app"
    body = (
        f'<article>'
        f'<div class="stub" aria-hidden="true"><span>{e(_STUB[kind])}</span><i></i></div>'
        f'<main><div class="brand">{e(name)}</div>'
        f'<h1>{e(copy["headline"])}</h1><p>{e(copy["lede"])}</p>'
        f'<form method="post" action="{e(path)}">{_fields(kind, copy)}'
        f'<button type="submit">{e(copy["submit"])}</button></form>'
        f'{_links(kind)}</main></article>'
    )
    return (
        f'<!doctype html><html lang="en"><head><meta charset="utf-8">'
        f'<meta name="viewport" content="width=device-width,initial-scale=1">'
        f'<title>{e(copy["headline"])}</title><style>{_STYLE}</style></head>'
        f'<body data-owner="built-app" data-page="{e(kind)}" data-path="{e(path)}">{body}</body></html>'
    )


class AuthPageStore:
    """Per-app copy. One app's preview edit does not change another's, or the console."""

    def __init__(self) -> None:
        self._by_app: dict[str, dict[str, dict[str, str]]] = {}

    def pages(self, app_id: str) -> dict[str, dict[str, str]]:
        return deepcopy(self._by_app.get(app_id) or fresh_pages())

    def edit(self, app_id: str, *, kind: str, field: str, value: str) -> dict[str, dict[str, str]]:
        nxt = edit_from_preview(self.pages(app_id), kind=kind, field=field, value=value)
        self._by_app[app_id] = nxt
        return deepcopy(nxt)
