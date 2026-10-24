"""BOSS's email templates: one small shared layout, an invitation, and a notice for everything else.

Built for the mail clients people actually use (Gmail, Outlook, Apple Mail), light and dark:
tables and inline CSS only, no web fonts, no remote images (the mark travels as a cid:
attachment), a bulletproof button, and a real text/plain part with the same words.

Every value that came from a person — an inviter's name, an org's name, a link — is escaped here,
once, so no caller can put markup into a message (tests/test_mail_templates.py).
"""
from __future__ import annotations

import html as _html
import re
from dataclasses import dataclass, field
from datetime import datetime
from functools import lru_cache
from pathlib import Path
from typing import Iterable, Optional

from .email_adapter import InlineImage

LOGO_CID = "boss-mark"
_LOGO_PATH = Path(__file__).with_name("mail_assets") / "boss-mark.png"

# Colours: AA contrast or better on their background in both schemes.
_LIGHT = {"page": "#f3f4f6", "card": "#ffffff", "text": "#111827", "muted": "#4b5563",
          "rule": "#e5e7eb", "button": "#1e4f8c", "button_text": "#ffffff", "link": "#1e4f8c"}
_DARK = {"page": "#0b0f17", "card": "#161b26", "text": "#f3f4f6", "muted": "#cbd5e1",
         "rule": "#2b3445", "button": "#2563a8", "button_text": "#ffffff", "link": "#9cc8f5"}
_FONT = ("-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,Helvetica,Arial,sans-serif")


@dataclass(frozen=True)
class Rendered:
    subject: str
    text: str
    html: str
    inline_images: tuple[InlineImage, ...] = field(default_factory=tuple)


@lru_cache(maxsize=1)
def logo() -> InlineImage:
    return InlineImage(cid=LOGO_CID, data=_LOGO_PATH.read_bytes(), subtype="png",
                       filename="boss.png")


def plain(value: object, limit: int = 120) -> str:
    """One line of a person's text: no line breaks or control characters (a header must not be
    split), and bounded."""
    text = re.sub(r"[\x00-\x1f\x7f]+", " ", str(value or "")).strip()
    text = re.sub(r"\s{2,}", " ", text)
    return text[:limit].rstrip() + ("…" if len(text) > limit else "")


def esc(value: object) -> str:
    return _html.escape(str(value or ""), quote=True)


def _safe_url(url: str) -> str:
    url = str(url or "").strip()
    return url if re.match(r"^https?://", url, re.I) else ""


def layout(*, title: str, preheader: str, blocks_html: str, footer_html: str) -> str:
    """The shared frame: mark, card, footer. `blocks_html` and `footer_html` are already escaped."""
    L, D = _LIGHT, _DARK
    return f"""<!DOCTYPE html>
<html lang="en" xmlns="http://www.w3.org/1999/xhtml">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="color-scheme" content="light dark">
<meta name="supported-color-schemes" content="light dark">
<title>{esc(title)}</title>
<style>
  body,table,td,a {{ -webkit-text-size-adjust:100%; -ms-text-size-adjust:100%; }}
  table {{ border-collapse:collapse; }}
  a {{ color:{L['link']}; }}
  @media (prefers-color-scheme: dark) {{
    .b-page {{ background:{D['page']} !important; }}
    .b-card {{ background:{D['card']} !important; border-color:{D['rule']} !important; }}
    .b-text {{ color:{D['text']} !important; }}
    .b-muted {{ color:{D['muted']} !important; }}
    .b-link {{ color:{D['link']} !important; }}
    .b-btn {{ background:{D['button']} !important; }}
    .b-rule {{ border-color:{D['rule']} !important; }}
  }}
  [data-ogsc] .b-page {{ background:{D['page']} !important; }}
  [data-ogsc] .b-card {{ background:{D['card']} !important; }}
  [data-ogsc] .b-text {{ color:{D['text']} !important; }}
  [data-ogsc] .b-muted {{ color:{D['muted']} !important; }}
  [data-ogsc] .b-link {{ color:{D['link']} !important; }}
  @media (max-width:600px) {{ .b-wrap {{ width:100% !important; }} .b-pad {{ padding:24px !important; }} }}
</style>
</head>
<body class="b-page" style="margin:0;padding:0;background:{L['page']};">
<div style="display:none;max-height:0;overflow:hidden;opacity:0;mso-hide:all;">{esc(preheader)}</div>
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" class="b-page" style="background:{L['page']};">
<tr><td align="center" style="padding:32px 12px;">
  <table role="presentation" width="560" cellpadding="0" cellspacing="0" border="0" class="b-wrap" style="width:560px;max-width:560px;">
    <tr><td align="left" style="padding:0 4px 16px 4px;">
      <img src="cid:{LOGO_CID}" width="40" height="48" alt="BOSS" style="display:block;border:0;outline:none;width:40px;height:48px;">
    </td></tr>
    <tr><td class="b-card b-pad" style="background:{L['card']};border:1px solid {L['rule']};border-radius:12px;padding:32px;font-family:{_FONT};">
{blocks_html}
    </td></tr>
    <tr><td class="b-muted" style="padding:16px 4px;font-family:{_FONT};font-size:12px;line-height:18px;color:{L['muted']};">
{footer_html}
    </td></tr>
  </table>
</td></tr>
</table>
</body>
</html>
"""


def _p(text_html: str, *, muted: bool = False, size: int = 15, top: int = 0) -> str:
    color = _LIGHT["muted"] if muted else _LIGHT["text"]
    cls = "b-muted" if muted else "b-text"
    return (f'<p class="{cls}" style="margin:{top}px 0 16px 0;font-size:{size}px;'
            f'line-height:{int(size * 1.5)}px;color:{color};">{text_html}</p>')


def _heading(text: str) -> str:
    return (f'<h1 class="b-text" style="margin:0 0 16px 0;font-size:22px;line-height:30px;'
            f'font-weight:600;color:{_LIGHT["text"]};">{esc(text)}</h1>')


def _button(label: str, url: str) -> str:
    """A bulletproof button: the cell carries the colour, so Outlook draws it without CSS."""
    href = esc(url)
    return (
        '<table role="presentation" cellpadding="0" cellspacing="0" border="0" style="margin:8px 0 24px 0;">'
        f'<tr><td class="b-btn" align="center" bgcolor="{_LIGHT["button"]}" '
        f'style="border-radius:8px;background:{_LIGHT["button"]};">'
        f'<a href="{href}" target="_blank" rel="noopener" style="display:inline-block;padding:12px 24px;'
        f'font-family:{_FONT};font-size:16px;line-height:20px;font-weight:600;'
        f'color:{_LIGHT["button_text"]};text-decoration:none;border-radius:8px;">{esc(label)}</a>'
        '</td></tr></table>')


def _raw_link(url: str) -> str:
    href = esc(url)
    return _p("Or paste this link into your browser:<br>"
              f'<a class="b-link" href="{href}" style="color:{_LIGHT["link"]};word-break:break-all;">{href}</a>',
              muted=True, size=13)


def _footer(org: str, from_address: str) -> str:
    who = esc(org) if org else "BOSS"
    sender = f" &lt;{esc(from_address)}&gt;" if from_address else ""
    return f"Sent by {who}{sender} using BOSS."


def _expiry(expires_at: datetime) -> str:
    from datetime import timezone

    if expires_at.tzinfo is not None:
        expires_at = expires_at.astimezone(timezone.utc)
    return f"{expires_at.day} {expires_at:%B %Y, %H:%M} UTC"


def invitation(*, inviter: str, org: str, roles: Iterable[str], link: str,
               expires_at: datetime, from_address: str = "") -> Rendered:
    """The invitation: who invited you, to what, as what; one button, the raw link, the expiry."""
    inviter_p = plain(inviter) or "Someone"
    org_p = plain(org) or "their team"
    role_list = [plain(r, 60) for r in roles if plain(r, 60)]
    roles_p = ", ".join(role_list) or "Member"
    url = _safe_url(link)
    if not url:
        raise ValueError("an invitation link must be an http(s) URL")
    when = _expiry(expires_at)
    headline = f"{inviter_p} invited you to {org_p} on BOSS"
    subject = plain(headline, 150)

    label = "Your role" if len(role_list) <= 1 else "Your roles"
    blocks = "\n".join([
        _heading(headline),
        _p(f'{label}: <strong>{esc(roles_p)}</strong>'),
        _button("Accept invite", url),
        _raw_link(url),
        _p(f"This invite expires on {esc(when)}. It works once.", muted=True, size=13),
        f'<hr class="b-rule" style="border:0;border-top:1px solid {_LIGHT["rule"]};margin:8px 0 16px 0;">',
        _p("Didn't expect this? You can ignore it — nothing happens unless you accept.",
           muted=True, size=13),
    ])
    html = layout(title=subject, preheader=f"{label}: {roles_p}. Accept by {when}.",
                  blocks_html=blocks, footer_html=_footer(org_p, from_address))
    text = (
        f"{headline}.\n\n"
        f"{label}: {roles_p}\n\n"
        f"Accept the invite:\n{url}\n\n"
        f"This invite expires on {when}. It works once.\n\n"
        "Didn't expect this? You can ignore it — nothing happens unless you accept.\n\n"
        f"-- \nSent by {org_p}{f' <{from_address}>' if from_address else ''} using BOSS.\n"
    )
    return Rendered(subject, text, html, (logo(),))


_URL_LINE = re.compile(r"^\s*(https?://\S+)\s*$")


def notice(subject: str, body: str, *, org: str = "", from_address: str = "",
           action_label: str = "Open BOSS") -> Rendered:
    """Any other identity email (reset, sign-in link or code, security notices) in the same frame.

    The text part is the caller's text unchanged; in the HTML a line that is only a URL becomes the
    button plus the raw link, and every other line a paragraph."""
    subject_p = plain(subject, 150) or "BOSS"
    blocks = [_heading(subject_p)]
    seen_button = False
    for para in re.split(r"\n\s*\n", (body or "").strip()):
        lines = [ln for ln in para.splitlines() if ln.strip()]
        for line in lines:
            m = _URL_LINE.match(line)
            url = _safe_url(m.group(1)) if m else ""
            if url:
                if not seen_button:
                    blocks.append(_button(_label_for(subject_p, action_label), url))
                    seen_button = True
                blocks.append(_raw_link(url))
                lines = [x for x in lines if x is not line]
        if lines:
            blocks.append(_p("<br>".join(esc(x) for x in lines)))
    html = layout(title=subject_p, preheader=plain(body, 90), blocks_html="\n".join(blocks),
                  footer_html=_footer(plain(org), from_address))
    return Rendered(subject_p, (body or "").strip() + "\n", html, (logo(),))


def _label_for(subject: str, default: str) -> str:
    low = subject.lower()
    if "reset" in low:
        return "Reset password"
    if "sign-in link" in low or "sign in" in low:
        return "Sign in"
    if "account" in low and "finish" in low:
        return "Finish creating account"
    return default


def preview_context() -> dict[str, object]:
    """Example values for a rendered preview (docs, screenshots): example.com only."""
    from datetime import timezone

    return {"inviter": "Ana Rivera", "org": "Example Co", "roles": ["Operator"],
            "link": "https://boss.example.com/invite#binv_EXAMPLE",
            "expires_at": datetime(2026, 10, 26, 14, 0, tzinfo=timezone.utc),
            "from_address": "boss@example.com"}


def write_preview(path: Optional[str] = None, *, dark: bool = False) -> str:
    """Render the invitation to an .html file a browser opens (the mark inlined as data: for the
    preview only — real mail carries it as cid:). `dark` applies the dark-mode rules
    unconditionally, as a client in dark mode does."""
    import base64

    rendered = invitation(**preview_context())  # type: ignore[arg-type]
    data = base64.b64encode(logo().data).decode()
    page = rendered.html.replace(f"cid:{LOGO_CID}", f"data:image/png;base64,{data}")
    if dark:
        page = page.replace("@media (prefers-color-scheme: dark)", "@media all")
    target = Path(path or ("invite-preview-dark.html" if dark else "invite-preview.html"))
    target.write_text(page, encoding="utf-8")
    return str(target)


if __name__ == "__main__":  # python -m boss_engine.services.mail_templates [dir]
    import sys

    folder = Path(sys.argv[1] if len(sys.argv) > 1 else ".")
    print(write_preview(str(folder / "invite-preview.html")))
    print(write_preview(str(folder / "invite-preview-dark.html"), dark=True))
