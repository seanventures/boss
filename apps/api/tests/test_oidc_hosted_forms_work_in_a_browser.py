"""The hosted sign-in pages, as a browser posts them (P0-1, P0-2).

P0-1: every hosted page sent `Referrer-Policy: no-referrer`, so Chrome posted its forms with
`Origin: null`, and the CSRF check refused them all. P0-2: a sign-in step's POST that ends in the
redirect back to the app was blocked by CSP `form-action 'self'` on every page but consent.

The class, checked on every page a full set of flows draws: each hosted page keeps the Origin
header (Referrer-Policy same-origin); each form and passkey button carries a signed token bound to
this browser's pre-auth cookie and to its interaction (or pool); each page of an interaction allows
the interaction's validated redirect origin in form-action; and the steps accept what Chrome
really sends (Origin null + Sec-Fetch-Site same-origin) while refusing anything cross-site, without
a token, or with another browser's token.
"""
from __future__ import annotations

import re
from urllib.parse import urlencode, urlsplit

import pytest

from apps.api.tests.oidc_world import (
    APP, HOSTED, ORIGIN, REDIRECT, STRONG_PW, browser_client, customer_world, field_of, hosted, last_code, pkce,
    query_of, totp_code, totp_from_page)

CHROME = {"Origin": "null", "Sec-Fetch-Site": "same-origin", "Content-Type": "application/x-www-form-urlencoded"}


@pytest.fixture
def cw(engine, monkeypatch):
    w, mail = customer_world(engine, monkeypatch)
    client, secret = w.client(pool_id=w.cpool["id"], name="Bank")
    w.cclient, w.csecret, w.mail = client, secret, mail
    w.pages = []                                      # every hosted HTML response, as drawn
    w.http.event_hooks["response"].append(lambda r: w.pages.append(r) if "text/html" in
                                          r.headers.get("content-type", "") else None)
    return w


def _authorize(w, **extra):
    verifier, challenge = pkce()
    r, params = w.authorize(w.cclient["id"], challenge=challenge, extra={"scope": "openid email", **extra})
    return r, params, verifier


def _post(w, step, form, headers=CHROME):
    if "csrf" not in form:
        form = {**form, "csrf": w.http.form_token}
    return w.http.post(f"/oidc/{w.org}/hosted/{step}", content=urlencode(form), headers=headers, follow_redirects=False)


def _form_action(response) -> str:
    return re.search(r"form-action ([^;]+);", response.headers["content-security-policy"]).group(1)


def test_chrome_signs_up_and_signs_in_again_and_every_page_keeps_the_contract(cw):
    # sign-up, emailed code, TOTP enrolment, consent: every post as Chrome sends it
    r, params, _ = _authorize(cw)
    interaction = field_of(r.text, "interaction")
    cw.http.get(f"/oidc/{cw.org}/account", params={"view": "signup", "interaction": interaction})
    r = _post(cw, "signup", {"interaction": interaction, "name": "Cara", "email": "cara@example.test",
                             "password": STRONG_PW})
    assert r.status_code == 200 and "Check your email" in r.text, r.text[:300]
    r = _post(cw, "verify-email", {"interaction": interaction, "code": last_code(cw.mail)})
    secret = totp_from_page(r.text)
    r = _post(cw, "enrol-totp", {"interaction": interaction, "code": totp_code(secret)})
    assert "Bank wants to sign you in" in r.text
    consent = cw.http.post(f"/oidc/{cw.org}/consent", content=urlencode({
        "interaction": field_of(r.text, "interaction"), "csrf": field_of(r.text, "csrf"), "decision": "allow"}),
        headers=CHROME, follow_redirects=False)
    assert consent.status_code == 303 and consent.headers["location"].startswith(REDIRECT)

    # a returning sign-in in a fresh browser: password, TOTP, and NO consent screen: the MFA
    # step's POST answers with the redirect to the app, which its page's form-action must allow
    cw.http.cookies.clear()
    r, params, _ = _authorize(cw)
    interaction = field_of(r.text, "interaction")
    r = _post(cw, "signin", {"interaction": interaction, "email": "cara@example.test", "password": STRONG_PW})
    assert "Second step" in r.text
    mfa_page = r
    done = _post(cw, "mfa", {"interaction": interaction, "code": totp_code(secret, 1)})
    assert done.status_code == 303 and done.headers["location"].startswith(REDIRECT), done.text[:300]
    assert query_of(done.headers["location"])["state"] == params["state"]
    assert APP in _form_action(mfa_page).split(), _form_action(mfa_page)

    # THE CONTRACT, on every hosted page drawn above
    hosted_pages = [p for p in cw.pages if urlsplit(str(p.request.url)).path.startswith(f"/oidc/{cw.org}/")]
    assert len(hosted_pages) >= 6
    for page in hosted_pages:
        where = f"{page.request.method} {urlsplit(str(page.request.url)).path} -> {page.status_code}"
        assert page.headers["referrer-policy"] == "same-origin", where
        body = page.text
        if '<form method="post"' in body or "data-passkey" in body:
            assert re.search(r'name="csrf" value="[^"]+"', body) or 'data-csrf="' in body, where
        if 'name="interaction"' in body and "/hosted/" in body:
            assert APP in _form_action(page).split(), where


def test_cross_site_tokenless_and_foreign_token_posts_are_refused(cw):
    r, _, _ = _authorize(cw)
    interaction = field_of(r.text, "interaction")
    form = {"interaction": interaction, "email": "x@example.test", "password": STRONG_PW}
    for headers in ({**HOSTED, "Origin": "https://evil.test"}, {**HOSTED, "Sec-Fetch-Site": "cross-site"},
                    {**HOSTED, "Sec-Fetch-Site": "same-site"}, {**CHROME, "Sec-Fetch-Site": "none"},
                    {"Origin": "null", "Content-Type": "application/x-www-form-urlencoded"},
                    {"Content-Type": "application/x-www-form-urlencoded"}):
        refused = _post(cw, "signin", form, headers=headers)
        assert refused.status_code == 403 and "Request refused" in refused.text, headers
    assert _post(cw, "signin", {**form, "csrf": ""}).status_code == 403
    # another browser's page and token (login CSRF): its token is bound to ITS pre-auth cookie
    other = browser_client()
    page = other.get(f"/oidc/{cw.org}/account", params={"view": "signin", "interaction": interaction})
    foreign = field_of(page.text, "csrf")
    assert foreign and foreign != cw.http.form_token
    assert _post(cw, "signin", {**form, "csrf": foreign}).status_code == 403
    # a token for another interaction or pool does not stand in for this one
    r2, _, _ = _authorize(cw)
    assert _post(cw, "signin", {**form, "csrf": field_of(r2.text, "csrf")}).status_code == 403
    # this browser's own token for this interaction: through to the password check
    cw.http.get(f"/oidc/{cw.org}/account", params={"view": "signin", "interaction": interaction})
    assert _post(cw, "signin", form).status_code == 401


def test_a_page_without_an_interaction_never_widens_form_action(cw):
    page = cw.http.get(f"/oidc/{cw.org}/account", params={"view": "forgot", "pool": cw.cpool["id"]})
    assert _form_action(page).split() == ["'self'"]
    assert page.headers["referrer-policy"] == "same-origin" and 'name="csrf"' in page.text


def test_the_account_page_finds_its_pool_without_the_query(cw):
    # no session and one customer pool: that pool's sign-in, never "expired"
    page = cw.http.get(f"/oidc/{cw.org}/account")
    assert page.status_code == 200 and "Sign in to Bank customers" in page.text and "expired" not in page.text
    # two customer pools and no session: where to open it from
    from boss_engine.db import begin
    from boss_engine.oidc_provider import pools as oidc_pools

    with begin() as db:
        oidc_pools.create(db, org_id=cw.org, actor="usr_test", body={"name": "Shop", "kind": "customer"})
    page = cw.http.get(f"/oidc/{cw.org}/account")
    assert page.status_code == 404 and "Open your account from the app" in page.text


def test_the_account_page_offers_only_what_is_missing_and_puts_sign_out_last(cw):
    r, params, verifier = _authorize(cw)
    interaction = field_of(r.text, "interaction")
    hosted(cw, "signup", {"interaction": interaction, "name": "Cara", "email": "cara@example.test", "password": STRONG_PW})
    r = hosted(cw, "verify-email", {"interaction": interaction, "code": last_code(cw.mail)})
    r = hosted(cw, "enrol-totp", {"interaction": interaction, "code": totp_code(totp_from_page(r.text))})
    page = cw.http.get(f"/oidc/{cw.org}/account")          # the session's own pool, no ?pool
    assert "Your Bank customers account" in page.text
    assert "Add authenticator app" not in page.text           # one is already added
    assert page.text.index("Add a passkey") < page.text.index(">Sign out</button>")
    out = hosted(cw, "account", {"pool": cw.cpool["id"], "action": "signout", "csrf": field_of(page.text, "csrf")})
    assert out.status_code == 200 and "<i></i>Bank customers</div>" in out.text and "signed out of Bank customers" in out.text


_ = ORIGIN
