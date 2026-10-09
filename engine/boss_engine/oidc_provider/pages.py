"""The hosted pages (§6.4): error, consent, signed out. Plain server-rendered HTML in BOSS's
sign-in style, themed only with the app's name. Every value is escaped; the CSP allows nothing
but this page's own inline style (by nonce) and a form post to BOSS and, on every page whose form
can end in a redirect back to the app, to that app's validated, registered redirect origin (a
form's redirect is checked against form-action); no framing.

Referrer-Policy is `same-origin`, never `no-referrer`: with `no-referrer` Chrome posts a form with
`Origin: null`, and the CSRF check (protocol.same_origin_post) could not tell BOSS's own page from
anyone's. `same-origin` still sends the app nothing.
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
:root{color-scheme:light dark;--bg:#f4f5f8;--card:#fff;--ink:#14171f;--muted:#5b6474;--line:#dfe3ea;--accent:#2f5bea;--bad:#c2341b}
@media (prefers-color-scheme:dark){:root{--bg:#0d0f14;--card:#161920;--ink:#e8ebf1;--muted:#9aa3b2;--line:#2a2f3a;--accent:#7c9cff;--bad:#ff8a73}}
*{box-sizing:border-box}body{margin:0;min-height:100vh;display:grid;place-items:center;background:var(--bg);color:var(--ink);
font:15px/1.5 system-ui,-apple-system,"Segoe UI",sans-serif;padding:16px}
main{width:100%;max-width:440px;background:var(--card);border:1px solid var(--line);border-radius:26px;padding:34px 36px 28px}
@media (max-width:480px){main{padding:24px 20px;border-radius:18px}}
h1{font-size:21px;margin:0 0 6px}p{color:var(--muted);margin:0 0 16px}ul{margin:0 0 20px;padding-left:20px}
li{margin:4px 0}.row{display:flex;gap:10px;margin-top:6px}button{flex:1;font:inherit;padding:11px 14px;border-radius:12px;cursor:pointer;
border:1px solid var(--line);background:transparent;color:var(--ink)}button.primary{background:var(--accent);border-color:var(--accent);color:#fff}
label{display:block;font-size:13px;font-weight:600;margin:0 0 6px}input{width:100%;font:inherit;padding:10px 12px;border-radius:12px;
border:1px solid var(--line);background:transparent;color:var(--ink);margin:0 0 14px}.err{color:var(--bad);font-weight:600}
.brand{display:flex;align-items:center;gap:8px;font-weight:700;letter-spacing:.04em;margin-bottom:18px}.brand i{width:10px;height:10px;border-radius:3px;background:var(--accent)}
.links{display:flex;flex-wrap:wrap;justify-content:space-between;gap:8px;margin-top:16px;font-size:13px}a{color:var(--accent)}
.qr{display:flex;justify-content:center;margin:0 0 12px}.qr svg{width:168px;height:168px;background:#fff;border-radius:12px;padding:8px}
code{word-break:break-all;font-size:12px}.item{display:flex;justify-content:space-between;align-items:center;gap:10px;padding:8px 0;border-top:1px solid var(--line)}
.item button{flex:0 0 auto;padding:6px 10px}.ok{color:var(--ink);font-weight:600}
h2{font-size:15px;margin:18px 0 6px}.item span{overflow-wrap:anywhere}
"""


REFERRER_POLICY = "same-origin"


def _origin(uri: str) -> Optional[str]:
    p = urlsplit(uri)
    return f"{p.scheme}://{p.netloc}" if p.scheme in ("https", "http") and p.netloc else None


def headers(nonce: str, *, form_targets: Iterable[str] = (), script: bool = False) -> dict[str, str]:
    form_action = " ".join(["'self'", *sorted({o for o in (_origin(t) for t in form_targets) if o})])
    scripts = f"script-src 'nonce-{nonce}'; connect-src 'self'; " if script else ""
    return {
        "Content-Security-Policy": (f"default-src 'none'; style-src 'nonce-{nonce}'; {scripts}form-action {form_action}; "
                                    "frame-ancestors 'none'; base-uri 'none'"),
        "X-Frame-Options": "DENY", "X-Content-Type-Options": "nosniff", "Referrer-Policy": REFERRER_POLICY,
        "Cache-Control": "no-store", "Pragma": "no-cache",
    }


def _page(title: str, body: str, nonce: str, *, brand: str = "BOSS", script: str = "") -> str:
    js = f"<script nonce=\"{nonce}\">{script}</script>" if script else ""
    return (f"<!doctype html><html lang=\"en\"><head><meta charset=\"utf-8\">"
            f"<meta name=\"viewport\" content=\"width=device-width,initial-scale=1\">"
            f"<title>{html.escape(title)}</title><style nonce=\"{nonce}\">{_STYLE}</style></head>"
            f"<body><main><div class=\"brand\"><i></i>{html.escape(brand)}</div>{body}</main>{js}</body></html>")


def error_page(title: str, message: str) -> tuple[str, dict[str, str]]:
    nonce = secrets.token_urlsafe(16)
    body = f"<h1>{html.escape(title)}</h1><p>{html.escape(message)}</p>"
    return _page(title, body, nonce), headers(nonce)


def error_page_for(brand: str, title: str, message: str) -> tuple[str, dict[str, str]]:
    nonce = secrets.token_urlsafe(16)
    body = f"<h1>{html.escape(title)}</h1><p>{html.escape(message)}</p>"
    return _page(title, body, nonce, brand=brand), headers(nonce)


def signed_out_page(brand: Optional[str] = None) -> tuple[str, dict[str, str]]:
    """Branded as the app (or the pool) the person signed out of; BOSS only when unknown."""
    if not brand:
        return error_page("Signed out", "You are signed out of the app. You can close this tab.")
    return error_page_for(brand, "Signed out", f"You are signed out of {brand}. You can close this tab.")


def consent_page(*, app_name: str, user_label: str, scopes: list[str], action: str, interaction: str,
                 csrf: str, redirect_uri: str, workforce: bool = True) -> tuple[str, dict[str, str]]:
    """`workforce`: the person signed in with their BOSS account. A customer never had one, so
    their page names only the app."""
    nonce = secrets.token_urlsafe(16)
    items = "".join(f"<li>{html.escape(SCOPE_WORDS.get(s, s))}</li>" for s in scopes)
    e = html.escape
    who = f"Signed in to BOSS as {e(user_label)}." if workforce else f"Signed in as {e(user_label)}."
    body = (f"<h1>{e(app_name)} wants to sign you in</h1>"
            f"<p>{who} {e(app_name)} will get:</p><ul>{items}</ul>"
            f"<form method=\"post\" action=\"{e(action)}\">"
            f"<input type=\"hidden\" name=\"interaction\" value=\"{e(interaction)}\">"
            f"<input type=\"hidden\" name=\"csrf\" value=\"{e(csrf)}\">"
            f"<div class=\"row\"><button type=\"submit\" name=\"decision\" value=\"deny\">Cancel</button>"
            f"<button class=\"primary\" type=\"submit\" name=\"decision\" value=\"allow\">Allow</button></div></form>")
    return _page(f"Sign in to {app_name}", body, nonce, brand=app_name if not workforce else "BOSS"), \
        headers(nonce, form_targets=[redirect_uri])


# ---- the hosted sign-in pages (customer pools, workforce re-authentication) -------------------------

PASSKEY_JS = """
(function(){
const root=document.querySelector('[data-passkey]');if(!root||!window.PublicKeyCredential)return;
const base=root.dataset.base,purpose=root.dataset.purpose,interaction=root.dataset.interaction||'',pool=root.dataset.pool||'',csrf=root.dataset.csrf||'';
const out=document.querySelector('[data-passkey-error]');
const d=s=>Uint8Array.from(atob(s.replace(/-/g,'+').replace(/_/g,'/')+'==='.slice((s.length+3)%4)),c=>c.charCodeAt(0));
const e=b=>btoa(String.fromCharCode(...new Uint8Array(b))).replace(/\\+/g,'-').replace(/\\//g,'_').replace(/=+$/,'');
async function post(step,body){const r=await fetch(base+'/hosted/'+step,{method:'POST',credentials:'same-origin',
 headers:{'content-type':'application/json'},body:JSON.stringify(body)});return [r.ok,await r.json()];}
root.hidden=false;root.addEventListener('click',async()=>{try{
 const [ok,o]=await post('passkey-options',{purpose,interaction,pool,csrf});if(!ok)throw o.error;const pk=o.publicKey;
 pk.challenge=d(pk.challenge);let cred,res;
 if(purpose==='register'){pk.user.id=d(pk.user.id);(pk.excludeCredentials||[]).forEach(c=>c.id=d(c.id));
  cred=await navigator.credentials.create({publicKey:pk});
  res={id:cred.id,rawId:e(cred.rawId),type:cred.type,response:{clientDataJSON:e(cred.response.clientDataJSON),
   attestationObject:e(cred.response.attestationObject),transports:cred.response.getTransports?cred.response.getTransports():[]}};}
 else{(pk.allowCredentials||[]).forEach(c=>c.id=d(c.id));cred=await navigator.credentials.get({publicKey:pk});
  res={id:cred.id,rawId:e(cred.rawId),type:cred.type,response:{clientDataJSON:e(cred.response.clientDataJSON),
   authenticatorData:e(cred.response.authenticatorData),signature:e(cred.response.signature),
   userHandle:cred.response.userHandle?e(cred.response.userHandle):null}};}
 const [ok2,v]=await post('passkey-verify',{purpose,interaction,pool,csrf,credential:res});if(!ok2)throw v.error;
 location.assign(v.next);}catch(err){if(out)out.textContent='That passkey did not work. Try again or use your password.';}});
})();
"""


def _hidden(fields: dict[str, str]) -> str:
    e = html.escape
    return "".join(f"<input type=\"hidden\" name=\"{e(k)}\" value=\"{e(v)}\">" for k, v in fields.items() if v)


def _input(name: str, label: str, kind: str = "text", value: str = "", auto: str = "") -> str:
    e = html.escape
    return (f"<label for=\"{e(name)}\">{e(label)}</label><input id=\"{e(name)}\" name=\"{e(name)}\" type=\"{kind}\" "
            f"value=\"{e(value)}\" autocomplete=\"{e(auto)}\" required>")


PASSKEY_SLOT = "<!--passkey-->"


def hosted_page(*, title: str, intro: str, action: str, step: str, hidden: dict[str, str], inputs: list[str],
                submit: str, error: Optional[str] = None, links: Optional[list[tuple[str, str]]] = None,
                brand: str = "BOSS", passkey: Optional[dict[str, str]] = None, extra: str = "",
                csrf: str = "", return_to: Optional[str] = None, notice: Optional[str] = None,
                ) -> tuple[str, dict[str, str]]:
    """One form step of the hosted sign-in: escaped values, CSP by nonce, no framing. `passkey`
    (data attributes) adds the passkey button and its script, where `extra` holds PASSKEY_SLOT
    or else after the form. `csrf` is the form's signed token (required with a form or a passkey);
    `return_to` is the interaction's validated redirect_uri, whose origin form-action then allows
    (a sign-in step's POST answers with a redirect to the app)."""
    e = html.escape
    if (action or passkey) and not csrf:
        raise ValueError("a hosted form or passkey button needs its signed token")
    nonce = secrets.token_urlsafe(16)
    if passkey:
        passkey = {**passkey, "csrf": csrf}
    err = f"<p class=\"err\" role=\"alert\">{e(error)}</p>" if error else ""
    if notice:
        err += f"<p class=\"ok\" role=\"status\">{e(notice)}</p>"
    pk = ""
    if passkey:
        attrs = " ".join(f"data-{e(k)}=\"{e(v)}\"" for k, v in passkey.items())
        pk = (f"<div class=\"row\"><button type=\"button\" data-passkey {attrs} hidden>"
              f"{e(passkey.get('label', 'Use a passkey'))}</button></div><p class=\"err\" data-passkey-error></p>")
    link_html = ""
    if links:
        link_html = "<div class=\"links\">" + "".join(f"<a href=\"{e(h)}\">{e(t)}</a>" for t, h in links) + "</div>"
    form = ""
    if action:
        form = (f"<form method=\"post\" action=\"{e(action)}\">{_hidden({**hidden, 'step': step, 'csrf': csrf})}"
                f"{''.join(inputs)}"
                f"<div class=\"row\"><button class=\"primary\" type=\"submit\">{e(submit)}</button></div></form>")
    if PASSKEY_SLOT in extra:
        extra, pk = extra.replace(PASSKEY_SLOT, pk), ""
    body = f"<h1>{e(title)}</h1><p>{e(intro)}</p>{err}{extra}{form}{pk}{link_html}"
    return _page(title, body, nonce, brand=brand, script=PASSKEY_JS if passkey else ""), \
        headers(nonce, script=bool(passkey), form_targets=[return_to] if return_to else ())


def qr_svg(uri: str) -> str:
    import segno

    return segno.make(uri, error="m").svg_inline(scale=4, border=1)
