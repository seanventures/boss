"""The hosted pages (§6.4): error, consent, signed out. Plain server-rendered HTML in BOSS's
sign-in style, themed only with the app's name. Every value is escaped; the CSP allows nothing
but this page's own inline style (by nonce) and a form post to BOSS and, for consent, to the app's
registered redirect origin (a form's redirect is checked against form-action); no framing.
"""
from __future__ import annotations

import html
import secrets
from typing import Iterable, Optional
from urllib.parse import urlsplit

SCOPE_WORDS = {
    "openid": "who you are (a private identifier for this app)",
    "profile": "your name",
    "email": "your email address",
    "roles": "your roles in this app",
    "offline_access": "to stay signed in when you are away (a refresh token)",
}

_STYLE = """
:root{color-scheme:light dark;--bg:#f6f7f9;--card:#fff;--ink:#14171f;--muted:#5b6474;--line:#dfe3ea;--accent:#2f5bea}
@media (prefers-color-scheme:dark){:root{--bg:#0f1115;--card:#171a21;--ink:#e8ebf1;--muted:#9aa3b2;--line:#2a2f3a;--accent:#7c9cff}}
*{box-sizing:border-box}body{margin:0;min-height:100vh;display:grid;place-items:center;background:var(--bg);color:var(--ink);
font:15px/1.5 system-ui,-apple-system,"Segoe UI",sans-serif;padding:16px}
main{width:100%;max-width:420px;background:var(--card);border:1px solid var(--line);border-radius:14px;padding:28px}
h1{font-size:20px;margin:0 0 6px}p{color:var(--muted);margin:0 0 16px}ul{margin:0 0 20px;padding-left:20px}
li{margin:4px 0}.row{display:flex;gap:10px}button{flex:1;font:inherit;padding:10px 14px;border-radius:10px;cursor:pointer;
border:1px solid var(--line);background:transparent;color:var(--ink)}button.primary{background:var(--accent);border-color:var(--accent);color:#fff}
.brand{font-size:12px;letter-spacing:.08em;text-transform:uppercase;color:var(--muted);margin-bottom:14px}
"""


def _origin(uri: str) -> Optional[str]:
    p = urlsplit(uri)
    return f"{p.scheme}://{p.netloc}" if p.scheme in ("https", "http") and p.netloc else None


def headers(nonce: str, *, form_targets: Iterable[str] = ()) -> dict[str, str]:
    form_action = " ".join(["'self'", *sorted({o for o in (_origin(t) for t in form_targets) if o})])
    return {
        "Content-Security-Policy": (f"default-src 'none'; style-src 'nonce-{nonce}'; form-action {form_action}; "
                                    "frame-ancestors 'none'; base-uri 'none'"),
        "X-Frame-Options": "DENY", "X-Content-Type-Options": "nosniff", "Referrer-Policy": "no-referrer",
        "Cache-Control": "no-store", "Pragma": "no-cache",
    }


def _page(title: str, body: str, nonce: str) -> str:
    return (f"<!doctype html><html lang=\"en\"><head><meta charset=\"utf-8\">"
            f"<meta name=\"viewport\" content=\"width=device-width,initial-scale=1\">"
            f"<title>{html.escape(title)}</title><style nonce=\"{nonce}\">{_STYLE}</style></head>"
            f"<body><main><div class=\"brand\">BOSS</div>{body}</main></body></html>")


def error_page(title: str, message: str) -> tuple[str, dict[str, str]]:
    nonce = secrets.token_urlsafe(16)
    body = f"<h1>{html.escape(title)}</h1><p>{html.escape(message)}</p>"
    return _page(title, body, nonce), headers(nonce)


def signed_out_page() -> tuple[str, dict[str, str]]:
    return error_page("Signed out", "You are signed out of the app. You can close this tab.")


def consent_page(*, app_name: str, user_label: str, scopes: list[str], action: str, interaction: str,
                 csrf: str, redirect_uri: str) -> tuple[str, dict[str, str]]:
    nonce = secrets.token_urlsafe(16)
    items = "".join(f"<li>{html.escape(SCOPE_WORDS.get(s, s))}</li>" for s in scopes)
    e = html.escape
    body = (f"<h1>{e(app_name)} wants to sign you in</h1>"
            f"<p>Signed in to BOSS as {e(user_label)}. {e(app_name)} will get:</p><ul>{items}</ul>"
            f"<form method=\"post\" action=\"{e(action)}\">"
            f"<input type=\"hidden\" name=\"interaction\" value=\"{e(interaction)}\">"
            f"<input type=\"hidden\" name=\"csrf\" value=\"{e(csrf)}\">"
            f"<div class=\"row\"><button type=\"submit\" name=\"decision\" value=\"deny\">Cancel</button>"
            f"<button class=\"primary\" type=\"submit\" name=\"decision\" value=\"allow\">Allow</button></div></form>")
    return _page(f"Sign in to {app_name}", body, nonce), headers(nonce, form_targets=[redirect_uri])
