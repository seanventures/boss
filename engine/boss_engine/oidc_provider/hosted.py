"""The hosted pages and their steps (§6.4): customer sign-in, sign-up, email verification, MFA
(TOTP and passkeys), password reset and the account page; and the workforce re-authentication
(prompt=login, max_age, require_mfa) through identity's own step-up.

Every POST step is CSRF-checked (Origin = the public origin, Sec-Fetch-Site same-origin; JSON
steps also need application/json, which a cross-site form cannot send), and binds its state to
this browser: the pre-auth cookie for a half-done sign-in, the app session for the account page
(plus an HMAC token), the console session for a workforce step-up. A finished sign-in always makes
a NEW session (no fixation) and continues the pending authorization request once.
"""
from __future__ import annotations

import html
from typing import Any, Mapping, Optional
from urllib.parse import urlencode

from sqlalchemy import text

from . import clients, consent, customers, pages, pools
from .customers import RealmError
from .util import audit

WORDS = {
    "bad_credentials": "That email and password do not match.",
    "locked": "Too many attempts. Wait a few minutes, then try again.",
    "unverified": "Confirm your email first: we sent you a code when you signed up.",
    "bad_email": "Enter a valid email address.",
    "bad_code": "That code is not right, or it expired.",
    "signup_closed": "Sign-up is by invitation only. Ask the app's administrator.",
    "signup_domain": "Sign-up is open to certain email domains only.",
    "password_too_short": "Use a longer password.",
    "password_too_long": "That password is too long.",
    "password_too_common": "That password is too common. Choose another.",
    "password_breached": "That password has appeared in a data breach. Choose another.",
    "last_factor": "This pool requires a second factor: add another before removing this one.",
    "mail_unavailable": "This BOSS cannot send email yet, so this step is unavailable.",
    "too_many": "Too many attempts. Wait a few minutes, then try again.",
}


def _words(code: str) -> str:
    return WORDS.get(code, "That did not work. Try again.")


def _reply(status: int, page: tuple[str, dict[str, str]], cookies: Optional[list[str]] = None) -> Any:
    from .protocol import Reply

    body, headers = page
    return Reply(status, body, headers, list(cookies or []))


def _err(status: int, title: str, message: str) -> Any:
    return _reply(status, pages.error_page(title, message))


# ---- context ---------------------------------------------------------------------------------------

def _context(db: Any, org_id: str, *, interaction: Optional[str], pool_id: Optional[str]
             ) -> Optional[tuple[dict[str, Any], Optional[dict[str, Any]], Optional[dict[str, Any]]]]:
    """(pool, client, interaction row) for a customer step: from a live interaction, or from a
    customer pool of THIS org (account flows). None when neither."""
    from .protocol import peek_interaction

    if interaction:
        it = peek_interaction(db, org_id, interaction)
        if it is None or "subject_ref" in it["params"]:
            return None
        client = clients.load(db, org_id=org_id, client_id=it["params"].get("client_id"))
        pool = pools.load(db, org_id=org_id, pool_id=client["pool_id"]) if client else None
        if pool is None or pool["kind"] != "customer":
            return None
        return pool, client, it
    pool = pools.load(db, org_id=org_id, pool_id=pool_id)
    if pool is None or pool["kind"] != "customer":
        return None
    return pool, None, None


def _hidden(interaction: Optional[str], pool: Mapping[str, Any]) -> dict[str, str]:
    return {"interaction": interaction or "", "pool": "" if interaction else pool["id"]}


def _q(org_id: str, view: str, interaction: Optional[str], pool: Mapping[str, Any]) -> str:
    q = {"view": view, **({"interaction": interaction} if interaction else {"pool": pool["id"]})}
    return f"/oidc/{org_id}/account?{urlencode(q)}"


def _brand(pool: Mapping[str, Any], client: Optional[Mapping[str, Any]]) -> str:
    return client["name"] if client else pool["name"]


# ---- pages -------------------------------------------------------------------------------------------

def signin_page(db: Any, *, org_id: str, pool: Mapping[str, Any], client: Optional[Mapping[str, Any]],
                interaction: Optional[str], origin: str = "", mfa_needed: bool = False, error: Optional[str] = None,
                email: str = "", status: int = 200) -> Any:
    links = []
    if pool["signup"] in ("open", "domain"):
        links.append(("Create an account", _q(org_id, "signup", interaction, pool)))
    links.append(("Forgot password?", _q(org_id, "forgot", interaction, pool)))
    passkey = None
    if pool.get("passkeys"):
        passkey = {"base": f"/oidc/{org_id}", "purpose": "login", "interaction": interaction or "",
                   "pool": "" if interaction else pool["id"], "label": "Sign in with a passkey"}
    return _reply(status, pages.hosted_page(
        title=f"Sign in to {_brand(pool, client)}", intro="Use your email and password, or a passkey.",
        action=f"/oidc/{org_id}/hosted/signin", step="signin", hidden=_hidden(interaction, pool),
        inputs=[pages._input("email", "Email", "email", email, "username"),
                pages._input("password", "Password", "password", "", "current-password")],
        submit="Sign in", error=error, links=links, brand=_brand(pool, client), passkey=passkey))


def signup_page(db: Any, *, org_id: str, pool: Mapping[str, Any], client: Optional[Mapping[str, Any]],
                interaction: Optional[str], error: Optional[str] = None, status: int = 200) -> Any:
    if pool["signup"] not in ("open", "domain"):
        return signin_page(db, org_id=org_id, pool=pool, client=client, interaction=interaction,
                           error=_words("signup_closed"), status=403)
    return _reply(status, pages.hosted_page(
        title=f"Create your {_brand(pool, client)} account", intro="We will email you a code to confirm the address.",
        action=f"/oidc/{org_id}/hosted/signup", step="signup", hidden=_hidden(interaction, pool),
        inputs=[pages._input("name", "Name", "text", "", "name"), pages._input("email", "Email", "email", "", "email"),
                pages._input("password", "Password (12 characters or more)", "password", "", "new-password")],
        submit="Create account", error=error, links=[("I have an account", _q(org_id, "signin", interaction, pool))],
        brand=_brand(pool, client)))


def forgot_page(db: Any, *, org_id: str, pool: Mapping[str, Any], client: Optional[Mapping[str, Any]],
                interaction: Optional[str], error: Optional[str] = None, status: int = 200) -> Any:
    return _reply(status, pages.hosted_page(
        title="Reset your password", intro="We will email you a code if the address has an account.",
        action=f"/oidc/{org_id}/hosted/forgot", step="forgot", hidden=_hidden(interaction, pool),
        inputs=[pages._input("email", "Email", "email", "", "username")], submit="Send code", error=error,
        links=[("Back to sign in", _q(org_id, "signin", interaction, pool))], brand=_brand(pool, client)))


def _code_page(org_id: str, pool: Mapping[str, Any], client: Optional[Mapping[str, Any]], interaction: Optional[str],
               *, step: str, title: str, intro: str, new_password: bool = False, error: Optional[str] = None,
               status: int = 200, cookies: Optional[list[str]] = None) -> Any:
    inputs = [pages._input("code", "Code", "text", "", "one-time-code")]
    if new_password:
        inputs.append(pages._input("password", "New password", "password", "", "new-password"))
    return _reply(status, pages.hosted_page(
        title=title, intro=intro, action=f"/oidc/{org_id}/hosted/{step}", step=step, hidden=_hidden(interaction, pool),
        inputs=inputs, submit="Continue", error=error, brand=_brand(pool, client)), cookies)


def _mfa_page(db: Any, org_id: str, pool: Mapping[str, Any], client: Optional[Mapping[str, Any]],
              interaction: Optional[str], user_id: str, *, error: Optional[str] = None, status: int = 200,
              cookies: Optional[list[str]] = None) -> Any:
    has_totp = customers.has_factor(db, user_id, "totp")
    passkey = None
    if pool.get("passkeys") and customers.has_factor(db, user_id, "webauthn"):
        passkey = {"base": f"/oidc/{org_id}", "purpose": "mfa", "interaction": interaction or "",
                   "pool": "" if interaction else pool["id"], "label": "Use a passkey"}
    return _reply(status, pages.hosted_page(
        title="Second step", intro="Enter the code from your authenticator app." if has_totp else "Use your passkey.",
        action=f"/oidc/{org_id}/hosted/mfa" if has_totp else "", step="mfa", hidden=_hidden(interaction, pool),
        inputs=[pages._input("code", "Code", "text", "", "one-time-code")], submit="Verify", error=error,
        brand=_brand(pool, client), passkey=passkey), cookies)


def _enrol_page(db: Any, org_id: str, pool: Mapping[str, Any], client: Optional[Mapping[str, Any]],
                interaction: Optional[str], user: Mapping[str, Any], preauth: Optional[str], *,
                error: Optional[str] = None, status: int = 200) -> Any:
    secret, uri, _preauth, cookie = customers.totp_begin(db, org_id=org_id, pool=pool, user=user, preauth=preauth)
    extra = (f"<div class=\"qr\">{pages.qr_svg(uri)}</div><p>Or enter this key: <code>{html.escape(secret)}</code></p>")
    return _reply(status, pages.hosted_page(
        title="Set up your authenticator app", intro="Scan the code with an authenticator app, then enter the 6 digits.",
        action=f"/oidc/{org_id}/hosted/enrol-totp", step="enrol-totp", hidden=_hidden(interaction, pool),
        inputs=[pages._input("code", "Code", "text", "", "one-time-code")], submit="Confirm", error=error,
        brand=_brand(pool, client), extra=extra), [cookie])


def account_page(db: Any, *, org_id: str, pool: Mapping[str, Any], sess: Any, notice: Optional[str] = None) -> Any:
    e = html.escape
    user = sess.user
    token = account_csrf(sess.id)

    def form(action: str, label: str, **fields: str) -> str:
        hidden = "".join(f"<input type=\"hidden\" name=\"{e(k)}\" value=\"{e(v)}\">" for k, v in
                         {"step": "account", "pool": pool["id"], "csrf": token, "action": action, **fields}.items())
        return f"<form method=\"post\" action=\"/oidc/{e(org_id)}/hosted/account\">{hidden}<button type=\"submit\">{e(label)}</button></form>"

    fs = "".join(f"<div class=\"item\"><span>{e(f['label'] or f['kind'])}</span>{form('remove-factor', 'Remove', factor=f['id'])}</div>"
                 for f in customers.factors(db, user["id"]))
    apps = db.execute(text("SELECT c.id, c.name, k.scopes FROM oidc_consents k JOIN oidc_clients c ON c.id = k.client_id "
                           "WHERE k.subject_ref = :s AND k.revoked_at IS NULL AND c.pool_id = :p ORDER BY c.name"),
                      {"s": user["id"], "p": pool["id"]}).mappings().all()
    ap = "".join(f"<div class=\"item\"><span>{e(a['name'])}</span>{form('revoke', 'Disconnect', client_id=a['id'])}</div>"
                 for a in apps) or "<p>No connected apps.</p>"
    note = f"<p class=\"err\" role=\"status\">{e(notice)}</p>" if notice else ""
    extra = (f"{note}<h2>Sign-in methods</h2>{fs or '<p>Password only.</p>'}"
             f"<div class=\"row\">{form('enrol-totp', 'Add authenticator app')}</div>"
             f"<h2>Connected apps</h2>{ap}<div class=\"row\">{form('signout', 'Sign out')}</div>")
    passkey = None
    if pool.get("passkeys"):
        passkey = {"base": f"/oidc/{org_id}", "purpose": "register", "pool": pool["id"], "label": "Add a passkey"}
    return _reply(200, pages.hosted_page(title=f"Your {pool['name']} account", intro=user["email"], action="",
                                         step="account", hidden={}, inputs=[], submit="", brand=pool["name"],
                                         extra=extra, passkey=passkey))


def account_csrf(session_id: str) -> str:
    from ..identity.authn_support import keyed_hash

    return keyed_hash("oidc-account-csrf", session_id)


def workforce_reauth_page(db: Any, *, org_id: str, client: Mapping[str, Any], interaction: str, principal: Any,
                          mfa_needed: bool, error: Optional[str] = None, status: int = 200,
                          cookies: Optional[list[str]] = None) -> Any:
    inputs = [pages._input("code", "Code from your authenticator app", "text", "", "one-time-code")]
    intro = (f"{client['name']} needs you to confirm it's you" + (" with a second factor." if mfa_needed else "."))
    page = pages.hosted_page(
        title=f"Confirm it's you, {principal.display_name}", intro=intro, action=f"/oidc/{org_id}/hosted/wf-reauth",
        step="wf-reauth", hidden={"interaction": interaction}, inputs=inputs, submit="Confirm", error=error,
        brand=client["name"], passkey={"base": f"/oidc/{org_id}", "purpose": "step_up", "interaction": interaction,
                                       "label": "Use a passkey"},
        links=[] if mfa_needed else [("Use my password instead", f"/oidc/{org_id}/account?{urlencode({'view': 'wf-password', 'interaction': interaction})}")])
    return _reply(status, page, cookies)


def workforce_password_page(org_id: str, client: Mapping[str, Any], interaction: str, error: Optional[str] = None) -> Any:
    return _reply(200 if not error else 401, pages.hosted_page(
        title="Confirm it's you", intro="Enter your BOSS password.", action=f"/oidc/{org_id}/hosted/wf-reauth",
        step="wf-reauth", hidden={"interaction": interaction},
        inputs=[pages._input("password", "Password", "password", "", "current-password")], submit="Confirm",
        error=error, brand=client["name"]))


# ---- GET /oidc/<org>/account ------------------------------------------------------------------------

def view(db: Any, org_id: str, params: Mapping[str, str], browser: Any) -> Any:
    which = params.get("view") or "account"
    interaction = params.get("interaction") or None
    if which == "wf-password":
        from .protocol import peek_interaction

        it = peek_interaction(db, org_id, interaction)
        client = clients.load(db, org_id=org_id, client_id=it["params"].get("client_id")) if it else None
        if client is None:
            return _err(400, "This sign-in has expired", "Go back to the app and sign in again.")
        return workforce_password_page(org_id, client, interaction or "")
    ctx = _context(db, org_id, interaction=interaction, pool_id=params.get("pool"))
    if ctx is None:
        return _err(400, "This page has expired", "Go back to the app and sign in again.")
    pool, client, _ = ctx
    if which == "signup":
        return signup_page(db, org_id=org_id, pool=pool, client=client, interaction=interaction)
    if which == "forgot":
        return forgot_page(db, org_id=org_id, pool=pool, client=client, interaction=interaction)
    sess = customers.session_lookup(db, pool_id=pool["id"], token=browser.app_session)
    if which == "account" and sess is not None and interaction is None:
        return account_page(db, org_id=org_id, pool=pool, sess=sess)
    return signin_page(db, org_id=org_id, pool=pool, client=client, interaction=interaction)


# ---- POST /oidc/<org>/hosted/<step> ------------------------------------------------------------------

def step(db: Any, org_slug: str, name: str, params: Mapping[str, Any], browser: Any) -> Any:
    from .protocol import resolve, same_origin_post
    from .util import OidcError

    try:
        org_id, _issuer, origin = resolve(db, org_slug)
    except OidcError as e:
        return _err(e.status, "Sign-in unavailable", e.description)
    is_json = name.startswith("passkey-") or name.startswith("wf-passkey-")
    if not same_origin_post(browser, origin) or (is_json and not (browser.content_type or "").startswith("application/json")):
        if is_json:
            from .protocol import Reply

            return Reply(403, {"error": "forbidden"}, {"Cache-Control": "no-store"})
        return _err(403, "Request refused", "This form did not come from BOSS's own page.")
    if name in ("passkey-options", "passkey-verify") and params.get("purpose") == "step_up":
        name = "wf-" + name
    if name in ("wf-reauth", "wf-passkey-options", "wf-passkey-verify"):
        return _workforce(db, org_id, origin, name, params, browser)
    interaction = str(params.get("interaction") or "") or None
    ctx = _context(db, org_id, interaction=interaction, pool_id=str(params.get("pool") or "") or None)
    if ctx is None:
        if is_json:
            from .protocol import Reply

            return Reply(400, {"error": "expired"}, {"Cache-Control": "no-store"})
        return _err(400, "This sign-in has expired", "Go back to the app and sign in again.")
    pool, client, _it = ctx
    handler = _STEPS.get(name)
    if handler is None:
        return _err(404, "Not found", "There is no such step.")
    from ..identity.passwords import PasswordBusy, hashing_for

    try:
        with hashing_for(browser.ip):
            return handler(db, org_id, origin, pool, client, interaction, params, browser)
    except PasswordBusy:
        return signin_page(db, org_id=org_id, pool=pool, client=client, interaction=interaction,
                           error="BOSS is busy. Try again in a moment.", status=429)


def _signed_in(db: Any, org_id: str, pool: Mapping[str, Any], interaction: Optional[str], browser: Any,
               user: Mapping[str, Any], methods: list[str], mfa: bool, extra_cookies: Optional[list[str]] = None) -> Any:
    """A NEW app session, then the pending authorization (or the account page)."""
    from ..identity.sessions import clear_cookie_header
    from .protocol import Browser, Reply, continue_after_signin

    token, cookie = customers.issue_session(db, pool_id=pool["id"], user_id=user["id"], methods=methods, mfa=mfa,
                                            replaces=browser.app_session, ip=browser.ip, user_agent=browser.user_agent)
    cookies = [*(extra_cookies or []), cookie, clear_cookie_header(customers.APP_PREAUTH_COOKIE)]
    if interaction:
        nb = Browser(session_cookie=browser.session_cookie, ip=browser.ip, user_agent=browser.user_agent,
                     origin=browser.origin, sec_fetch_site=browser.sec_fetch_site, app_session=token)
        reply = continue_after_signin(db, org_id, interaction, nb)
        if reply.status in (301, 302, 307, 308):
            reply.status = 303
        reply.cookies = [*cookies, *reply.cookies]
        return reply
    return Reply(303, None, {"Location": f"/oidc/{org_id}/account?{urlencode({'pool': pool['id']})}",
                             "Cache-Control": "no-store"}, cookies)


def _after_password(db: Any, org_id: str, pool: Mapping[str, Any], client: Optional[Mapping[str, Any]],
                    interaction: Optional[str], browser: Any, user: Mapping[str, Any], first: str) -> Any:
    """The first factor is done: a second factor, an enrolment, or the session."""
    need = pool["mfa"] == "required"
    if interaction:
        from .protocol import peek_interaction

        it = peek_interaction(db, org_id, interaction)
        if it and (it["params"].get("mfa") or (client and client["require_mfa"])):
            need = True
    has = customers.has_factor(db, user["id"])
    if has and pool["mfa"] != "off":
        _pre, cookie = customers.new_challenge(db, pool_id=pool["id"], user_id=user["id"], purpose="mfa",
                                               payload={"first": first}, preauth=browser.app_preauth)
        return _mfa_page(db, org_id, pool, client, interaction, user["id"], cookies=[cookie])
    if need:
        return _enrol_page(db, org_id, pool, client, interaction, user, browser.app_preauth)
    return _signed_in(db, org_id, pool, interaction, browser, user, [first], mfa=False)


# ---- customer steps ----------------------------------------------------------------------------------

def _signin(db, org_id, origin, pool, client, interaction, params, browser):
    email = str(params.get("email") or "")
    try:
        user = customers.password_signin(db, org_id=org_id, pool=pool, email=email,
                                         password=str(params.get("password") or ""), ip=browser.ip)
    except RealmError as exc:
        return signin_page(db, org_id=org_id, pool=pool, client=client, interaction=interaction,
                           error=_words(exc.code), email=email[:254], status=401 if exc.code != "locked" else 429)
    return _after_password(db, org_id, pool, client, interaction, browser, user, "password")


def _signup(db, org_id, origin, pool, client, interaction, params, browser):
    from ..identity import passwords

    if pool["signup"] not in ("open", "domain"):
        return signup_page(db, org_id=org_id, pool=pool, client=client, interaction=interaction, status=403)
    if not customers._hit(db, "oidc.signup.ip", browser.ip or "-", customers.SIGNUPS_PER_IP):  # noqa: SLF001
        return signup_page(db, org_id=org_id, pool=pool, client=client, interaction=interaction,
                           error=_words("too_many"), status=429)
    try:
        email = customers.normalise_email(params.get("email"))
        if pool["signup"] == "domain" and email.rsplit("@", 1)[1].lower() not in pool["signup_domains"]:
            raise RealmError("signup_domain")
        password = customers.check_password(pool, str(params.get("password") or ""), email=email)
    except RealmError as exc:
        return signup_page(db, org_id=org_id, pool=pool, client=client, interaction=interaction,
                           error=_words(exc.code), status=400)
    verify = bool((pool.get("password_policy") or {}).get("verify_email", True))
    if verify and not customers.mail_available():
        return signup_page(db, org_id=org_id, pool=pool, client=client, interaction=interaction,
                           error=_words("mail_unavailable"), status=503)
    hashed = passwords.hash_password(password)          # the same work whether or not the address exists
    existing = customers.find_user(db, pool_id=pool["id"], email=email)
    if existing is None:
        user = customers.create_user(db, pool=pool, email=email, name=str(params.get("name") or ""),
                                     password_hash=hashed, status="pending" if verify else "active", verified=False)
        audit(db, org_id=org_id, event_type="oidc.pool_user.signed_up", actor_kind="pool_user",
              target_type="oidc_pool_user", target_id=user["id"], ip=browser.ip, details={"pool_id": pool["id"]})
        if not verify:
            return _after_password(db, org_id, pool, client, interaction, browser, user, "password")
    else:
        user = None       # anti-enumeration: the same page, and no code that could ever verify
    _pre, cookie = customers.send_code(db, pool=pool, user=user, email=email, purpose="verify_email",
                                       preauth=browser.app_preauth, subject=f"Confirm your {pool['name']} account")
    return _code_page(org_id, pool, client, interaction, step="verify-email", title="Check your email",
                      intro="Enter the 6-digit code we sent to your address.", cookies=[cookie])


def _verify_email(db, org_id, origin, pool, client, interaction, params, browser):
    user = customers.check_code(db, pool=pool, preauth=browser.app_preauth, purpose="verify_email",
                                code=str(params.get("code") or ""))
    if user is None or user["status"] == "disabled":
        return _code_page(org_id, pool, client, interaction, step="verify-email", title="Check your email",
                          intro="Enter the 6-digit code we sent to your address.", error=_words("bad_code"), status=401)
    db.execute(text("UPDATE oidc_pool_users SET email_verified = true, status = 'active' WHERE id = :id AND "
                    "status = 'pending'"), {"id": user["id"]})
    db.execute(text("UPDATE oidc_pool_users SET email_verified = true WHERE id = :id"), {"id": user["id"]})
    audit(db, org_id=org_id, event_type="oidc.pool_user.email_verified", actor_kind="pool_user",
          target_type="oidc_pool_user", target_id=user["id"], details={"pool_id": pool["id"]})
    user = customers.load_user(db, pool_id=pool["id"], user_id=user["id"])
    return _after_password(db, org_id, pool, client, interaction, browser, user, "password")


def _mfa(db, org_id, origin, pool, client, interaction, params, browser):
    ch = customers.challenge(db, pool_id=pool["id"], preauth=browser.app_preauth, purpose="mfa")
    if ch is None:
        return signin_page(db, org_id=org_id, pool=pool, client=client, interaction=interaction,
                           error="Your sign-in expired. Start again.", status=401)
    if not customers.totp_verify(db, org_id=org_id, pool=pool, user_id=ch["pool_user_id"],
                                 code=str(params.get("code") or ""), ip=browser.ip):
        spent = customers.strike(db, ch)
        if spent:
            return signin_page(db, org_id=org_id, pool=pool, client=client, interaction=interaction,
                               error=_words("too_many"), status=429)
        return _mfa_page(db, org_id, pool, client, interaction, ch["pool_user_id"], error=_words("bad_code"), status=401)
    customers.spend(db, ch)
    user = customers.load_user(db, pool_id=pool["id"], user_id=ch["pool_user_id"])
    return _signed_in(db, org_id, pool, interaction, browser, user, [ch["payload"].get("first", "password"), "totp"],
                      mfa=True)


def _enrol_totp(db, org_id, origin, pool, client, interaction, params, browser):
    ch = customers.challenge(db, pool_id=pool["id"], preauth=browser.app_preauth, purpose="enrol_totp")
    if ch is None:
        return signin_page(db, org_id=org_id, pool=pool, client=client, interaction=interaction,
                           error="Your sign-in expired. Start again.", status=401)
    user = customers.load_user(db, pool_id=pool["id"], user_id=ch["pool_user_id"])
    if user is None or not customers.totp_confirm(db, org_id=org_id, pool=pool, preauth=browser.app_preauth,
                                                  code=str(params.get("code") or ""), user_id=user["id"]):
        if user is None or customers.challenge(db, pool_id=pool["id"], preauth=browser.app_preauth,
                                               purpose="enrol_totp") is None:
            return signin_page(db, org_id=org_id, pool=pool, client=client, interaction=interaction,
                               error=_words("too_many"), status=429)
        return _reply(401, pages.hosted_page(
            title="Set up your authenticator app", intro="Enter the 6 digits your app shows now.",
            action=f"/oidc/{org_id}/hosted/enrol-totp", step="enrol-totp", hidden=_hidden(interaction, pool),
            inputs=[pages._input("code", "Code", "text", "", "one-time-code")], submit="Confirm",
            error=_words("bad_code"), brand=_brand(pool, client)))
    sess = customers.session_lookup(db, pool_id=pool["id"], token=browser.app_session)
    if sess is not None and sess.user["id"] == user["id"] and interaction is None:
        return account_page(db, org_id=org_id, pool=pool, sess=sess, notice="Authenticator app added.")
    return _signed_in(db, org_id, pool, interaction, browser, user, ["password", "totp"], mfa=True)


def _forgot(db, org_id, origin, pool, client, interaction, params, browser):
    try:
        email = customers.normalise_email(params.get("email"))
    except RealmError:
        return forgot_page(db, org_id=org_id, pool=pool, client=client, interaction=interaction,
                           error=_words("bad_email"), status=400)
    if not customers.mail_available():
        return forgot_page(db, org_id=org_id, pool=pool, client=client, interaction=interaction,
                           error=_words("mail_unavailable"), status=503)
    user = customers.find_user(db, pool_id=pool["id"], email=email)
    if user is not None and user["status"] == "disabled":
        user = None
    _pre, cookie = customers.send_code(db, pool=pool, user=user, email=email, purpose="reset",
                                       preauth=browser.app_preauth, subject=f"Reset your {pool['name']} password")
    return _code_page(org_id, pool, client, interaction, step="reset", title="Check your email",
                      intro="If the address has an account, we sent it a code. Enter it and a new password.",
                      new_password=True, cookies=[cookie])


def _reset(db, org_id, origin, pool, client, interaction, params, browser):
    ch = customers.challenge(db, pool_id=pool["id"], preauth=browser.app_preauth, purpose="reset")
    user = customers.check_code(db, pool=pool, preauth=browser.app_preauth, purpose="reset",
                                code=str(params.get("code") or "")) if ch else None
    if user is None:
        return _code_page(org_id, pool, client, interaction, step="reset", title="Check your email",
                          intro="Enter the code and a new password.", new_password=True, error=_words("bad_code"),
                          status=401)
    try:
        customers.set_password(db, pool=pool, user=user, password=str(params.get("password") or ""))
    except RealmError as exc:
        _pre, cookie = customers.new_challenge(db, pool_id=pool["id"], user_id=user["id"], purpose="reset",
                                               payload=ch["payload"], preauth=browser.app_preauth)
        return _code_page(org_id, pool, client, interaction, step="reset", title="Choose another password",
                          intro="Enter the same code and a different password.", new_password=True,
                          error=_words(exc.code), status=400, cookies=[cookie])
    db.execute(text("UPDATE oidc_pool_users SET email_verified = true, status = 'active' WHERE id = :id "
                    "AND status <> 'disabled'"), {"id": user["id"]})
    customers.disable_everything(db, user_id=user["id"], reason="password_reset")
    audit(db, org_id=org_id, event_type="oidc.pool_user.password_reset", actor_kind="pool_user",
          target_type="oidc_pool_user", target_id=user["id"], details={"pool_id": pool["id"]})
    return signin_page(db, org_id=org_id, pool=pool, client=client, interaction=interaction,
                       error="Password changed. Sign in with it.", email=user["email"])


def _passkey_options(db, org_id, origin, pool, client, interaction, params, browser):
    from .protocol import Reply

    purpose = str(params.get("purpose") or "")
    user = None
    if purpose == "mfa":
        ch = customers.challenge(db, pool_id=pool["id"], preauth=browser.app_preauth, purpose="mfa")
        user = customers.load_user(db, pool_id=pool["id"], user_id=ch["pool_user_id"]) if ch else None
        if user is None:
            return Reply(401, {"error": "expired"}, {"Cache-Control": "no-store"})
    elif purpose == "register":
        sess = customers.session_lookup(db, pool_id=pool["id"], token=browser.app_session)
        if sess is None:
            return Reply(401, {"error": "unauthenticated"}, {"Cache-Control": "no-store"})
        user = sess.user
    try:
        options, _pre, cookie = customers.passkey_options(db, pool=pool, origin=origin, purpose=purpose, user=user,
                                                          preauth=browser.app_preauth)
    except RealmError as exc:
        return Reply(409, {"error": exc.code}, {"Cache-Control": "no-store"})
    return Reply(200, {"publicKey": options}, {"Cache-Control": "no-store"}, [cookie])


def _passkey_verify(db, org_id, origin, pool, client, interaction, params, browser):
    from .protocol import Reply

    purpose = str(params.get("purpose") or "")
    mfa_ch = customers.challenge(db, pool_id=pool["id"], preauth=browser.app_preauth, purpose="mfa") \
        if purpose == "mfa" else None
    if purpose == "register":
        sess = customers.session_lookup(db, pool_id=pool["id"], token=browser.app_session)
        if sess is None:
            return Reply(401, {"error": "unauthenticated"}, {"Cache-Control": "no-store"})
    try:
        user = customers.passkey_verify(db, org_id=org_id, pool=pool, origin=origin, purpose=purpose,
                                        preauth=browser.app_preauth, credential=params.get("credential"))
    except RealmError as exc:
        return Reply(401, {"error": exc.code}, {"Cache-Control": "no-store"})
    if purpose == "register":
        if user["id"] != sess.user["id"]:
            return Reply(401, {"error": "passkey_failed"}, {"Cache-Control": "no-store"})
        return Reply(200, {"next": f"/oidc/{org_id}/account?{urlencode({'pool': pool['id']})}"}, {"Cache-Control": "no-store"})
    if purpose == "mfa":
        if mfa_ch is None or mfa_ch["pool_user_id"] != user["id"]:
            return Reply(401, {"error": "passkey_failed"}, {"Cache-Control": "no-store"})
        customers.spend(db, mfa_ch)
        methods = [mfa_ch["payload"].get("first", "password"), "passkey"]
    else:
        methods = ["passkey"]
    token, cookie = customers.issue_session(db, pool_id=pool["id"], user_id=user["id"], methods=methods, mfa=True,
                                            replaces=browser.app_session, ip=browser.ip, user_agent=browser.user_agent)
    from ..identity.sessions import clear_cookie_header

    nxt = (f"/oidc/{org_id}/resume?{urlencode({'interaction': interaction})}" if interaction
           else f"/oidc/{org_id}/account?{urlencode({'pool': pool['id']})}")
    return Reply(200, {"next": nxt}, {"Cache-Control": "no-store"},
                 [cookie, clear_cookie_header(customers.APP_PREAUTH_COOKIE)])


def _account(db, org_id, origin, pool, client, interaction, params, browser):
    from .protocol import Reply

    sess = customers.session_lookup(db, pool_id=pool["id"], token=browser.app_session)
    if sess is None or not customers.same(account_csrf(sess.id), str(params.get("csrf") or "")):
        return _err(403, "Request refused", "Sign in again to change your account.")
    action = params.get("action")
    if action == "signout":
        cookie = customers.end_session(db, browser.app_session)
        audit(db, org_id=org_id, event_type="oidc.pool_user.signed_out", actor_kind="pool_user",
              target_type="oidc_pool_user", target_id=sess.user["id"], details={"pool_id": pool["id"]})
        body, headers = pages.signed_out_page()
        return Reply(200, body, headers, [cookie])
    if action == "revoke":
        target = clients.load(db, org_id=org_id, client_id=params.get("client_id"), any_status=True)
        if target is None or target["pool_id"] != pool["id"]:
            return account_page(db, org_id=org_id, pool=pool, sess=sess, notice="No such app.")
        consent.revoke(db, org_id=org_id, client_id=target["id"], subject_ref=sess.user["id"], actor=None)
        return account_page(db, org_id=org_id, pool=pool, sess=sess, notice=f"{target['name']} is disconnected.")
    if action == "remove-factor":
        try:
            customers.remove_factor(db, org_id=org_id, pool=pool, user_id=sess.user["id"],
                                    factor_id=str(params.get("factor") or ""))
        except RealmError as exc:
            return account_page(db, org_id=org_id, pool=pool, sess=sess, notice=_words(exc.code))
        return account_page(db, org_id=org_id, pool=pool, sess=sess, notice="Removed.")
    if action == "enrol-totp":
        return _enrol_page(db, org_id, pool, None, None, sess.user, browser.app_preauth)
    return account_page(db, org_id=org_id, pool=pool, sess=sess)


_STEPS = {"signin": _signin, "signup": _signup, "verify-email": _verify_email, "mfa": _mfa,
          "enrol-totp": _enrol_totp, "forgot": _forgot, "reset": _reset, "passkey-options": _passkey_options,
          "passkey-verify": _passkey_verify, "account": _account}


# ---- workforce re-authentication (identity's own step-up) ---------------------------------------------

def _facts(db: Any, browser: Any, origin: str) -> Any:
    from ..identity.signin import RequestFacts

    return RequestFacts(ip=browser.ip, user_agent=browser.user_agent, session=browser.session_cookie,
                        origin=origin, request_origin=origin)


def _new_console_cookie(cookies: list[str]) -> Optional[str]:
    from ..identity.authn import SESSION_COOKIE

    for c in cookies:
        if c.startswith(f"{SESSION_COOKIE}="):
            return c.split(";", 1)[0].split("=", 1)[1] or None
    return None


def _workforce(db: Any, org_id: str, origin: str, name: str, params: Mapping[str, Any], browser: Any) -> Any:
    from ..identity import signin
    from .protocol import Browser, Reply, continue_after_signin, peek_interaction

    interaction = str(params.get("interaction") or "")
    it = peek_interaction(db, org_id, interaction)
    client = clients.load(db, org_id=org_id, client_id=it["params"].get("client_id")) if it else None
    if client is None:
        if name.startswith("wf-passkey"):
            return Reply(400, {"error": "expired"}, {"Cache-Control": "no-store"})
        return _err(400, "This sign-in has expired", "Go back to the app and sign in again.")
    facts = _facts(db, browser, origin)
    if name == "wf-passkey-options":
        answer = signin.passkey_options(db, facts, purpose="step_up")
        return Reply(answer.status, answer.body, {"Cache-Control": "no-store"}, list(answer.cookies))
    if name == "wf-passkey-verify":
        answer = signin.passkey_verify(db, facts, credential=params.get("credential"), purpose="step_up")
        if answer.status != 200:
            return Reply(answer.status, {"error": "passkey_failed"}, {"Cache-Control": "no-store"})
        return Reply(200, {"next": f"/oidc/{org_id}/resume?{urlencode({'interaction': interaction})}"},
                     {"Cache-Control": "no-store"}, list(answer.cookies))
    mfa_needed = bool(it["params"].get("mfa") or client["require_mfa"])
    if params.get("password") and not mfa_needed:
        answer = signin.step_up(db, facts, method="password", password=str(params.get("password")))
    else:
        answer = signin.step_up(db, facts, method="totp", code=str(params.get("code") or ""))
    if answer.status != 200:
        from ..identity import sessions, users

        row = sessions.lookup(db, browser.session_cookie)
        principal = users.load_principal(db, row.principal_id) if row else None
        if principal is None:
            return _err(401, "Sign in to BOSS first", "Sign in to BOSS, then return to the app and try again.")
        words = {"second_factor_required": "Use your authenticator app or passkey.",
                 "invalid_credentials": "That password is not right."}.get(answer.body.get("error"), _words("bad_code"))
        return workforce_reauth_page(db, org_id=org_id, client=client, interaction=interaction, principal=principal,
                                     mfa_needed=mfa_needed, error=words, status=answer.status)
    nb = Browser(session_cookie=_new_console_cookie(list(answer.cookies)), ip=browser.ip,
                 user_agent=browser.user_agent, origin=browser.origin, sec_fetch_site=browser.sec_fetch_site)
    reply = continue_after_signin(db, org_id, interaction, nb)
    if reply.status in (301, 302, 307, 308):
        reply.status = 303
    reply.cookies = [*answer.cookies, *reply.cookies]
    return reply

