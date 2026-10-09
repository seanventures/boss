"""Sign in with BOSS, in a real browser: every hosted flow, with real forms, back at a real RP.

The earlier F6 proof (rp.mjs, rp-customer.mjs) drove the hosted pages with fetch and wrote the
Origin and Sec-Fetch-Site headers itself, so it passed while Chrome refused every hosted form
(Referrer-Policy: no-referrer makes Chrome post `Origin: null`) and blocked every redirect back to
the app after a sign-in with no consent screen (CSP form-action). This one lets Chromium do it:
Playwright clicks and types on the pages BOSS serves, through the console's origin (`next dev`
forwarding /oidc/* to the API), and every flow must land on the relying party's callback and
exchange its code.

    DATABASE_URL=postgresql+psycopg://…/boss_test_<x> \\
    PYTHONPATH=.:engine python3 tests/e2e/oidc/browser_flows.py [--keep] [--headed]

Needs a DISPOSABLE database (the name must look like a test one: apps/api/tests/db_guard.py),
apps/web/node_modules, and Playwright's Chromium. Boots, on free localhost ports: an SMTP sink, the
API (`python -m apps.api.serve`), the console (`next dev`) and the RP. The RP's return address is
http://127.0.0.1:<port>/callback on purpose: Next rewrote a 127.0.0.1 redirect_uri in the query
(P1-1). Flows:

  1 customer sign-up with an emailed code, TOTP enrolment, consent
  2 a passkey added on the account page (opened without ?pool) via a virtual authenticator, then
    a passkey-only sign-in
  3 a password reset by emailed code, then a sign-in with the new password and TOTP
  4 a returning sign-in with MFA and no consent screen (P0-2)
  5 prompt=login on a signed-in browser
  6 a workforce member: prompt=login asks "Confirm it's you" with their authenticator app; a member
    with no second factor is asked for their password instead
  7 the console: App sign-in at 1360 px (the Identity tab strip shows every tab) and 390 px (no
    sideways scroll; a client opens in a side sheet)

Each check prints `ok` or `FAIL`; the exit status is the number of failures. Nothing prints a
secret: codes, passwords and tokens stay in variables.
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import http.server
import json
import os
import re
import secrets
import signal
import socket
import socketserver
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Callable, Optional

REPO = Path(__file__).resolve().parents[3]
sys.path[:0] = [str(REPO), str(REPO / "engine"), str(REPO / "tests")]

FAILURES: list[str] = []
STRONG = "violet-otter-harbour-1987"


def check(cond: Any, what: str) -> bool:
    print(("ok    " if cond else "FAIL  ") + what, flush=True)
    if not cond:
        FAILURES.append(what)
    return bool(cond)


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def wait_http(url: str, timeout: float, proc: Optional[subprocess.Popen] = None) -> None:
    end = time.time() + timeout
    while time.time() < end:
        if proc is not None and proc.poll() is not None:
            raise RuntimeError(f"{url}: the process exited ({proc.returncode})")
        try:
            with urllib.request.urlopen(url, timeout=5) as r:
                if r.status < 500:
                    return
        except urllib.error.HTTPError as e:
            if e.code < 500:
                return
        except Exception:  # noqa: BLE001
            pass
        time.sleep(1)
    raise RuntimeError(f"{url} did not answer in {timeout:.0f}s")


# ---- the SMTP sink and the relying party ----------------------------------------------------------

class Mailbox:
    def __init__(self) -> None:
        self.mail: list[dict[str, Any]] = []
        self.lock = threading.Lock()

    def add(self, bucket: str, item: dict[str, Any]) -> dict[str, Any]:
        with self.lock:
            self.mail.append(item)
        return item

    def code_for(self, to: str, after: int, timeout: float = 20) -> str:
        end = time.time() + timeout
        while time.time() < end:
            with self.lock:
                for m in self.mail[after:]:
                    if to in m["to"]:
                        found = re.search(r"\b(\d{6})\b", m["body"])
                        if found:
                            return found.group(1)
            time.sleep(0.3)
        raise RuntimeError(f"no code mailed to {to}")


def start_smtp(box: Mailbox) -> int:
    from e2e.stubs.fakes import SmtpHandler

    handler = type("Sink", (SmtpHandler,), {"world": box})
    server = socketserver.ThreadingTCPServer(("127.0.0.1", 0), handler)
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server.server_address[1]


class RP:
    """The app: its callback records what came back and answers a page."""

    def __init__(self) -> None:
        self.hits: list[str] = []
        rp = self

        class H(http.server.BaseHTTPRequestHandler):
            def do_GET(self) -> None:  # noqa: N802
                rp.hits.append(self.path)
                body = b"<!doctype html><title>RP</title><h1>Back at the app</h1>"
                self.send_response(200)
                self.send_header("Content-Type", "text/html")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *a: Any) -> None:
                pass

        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.origin = f"http://127.0.0.1:{self.server.server_address[1]}"
        self.callback = f"{self.origin}/callback"


# ---- the world --------------------------------------------------------------------------------------

def b64u(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


class TOTP:
    """Codes that are never replayed: each use takes a step after the last one used."""

    def __init__(self) -> None:
        self.last: dict[str, int] = {}

    def code(self, secret: str) -> str:
        from boss_engine.identity import totp

        while True:
            cur = totp.current_step()
            for step in (cur, cur + 1):
                if step > self.last.get(secret, -1):
                    self.last[secret] = step
                    return totp.code_at(secret, step)
            time.sleep(1)


def build_world(rp: RP) -> dict[str, Any]:
    from datetime import datetime, timezone

    from sqlalchemy import text

    from boss_engine.db import begin
    from boss_engine.identity import authz, sessions, totp, vault
    from boss_engine.identity.passwords import hash_password
    from boss_engine.oidc_provider import clients, pools

    w: dict[str, Any] = {}
    with begin() as db:
        org = db.execute(text("SELECT id FROM orgs ORDER BY created_at, id LIMIT 1")).scalar()
        project = db.execute(text("SELECT p.id FROM projects p JOIN workspaces w ON w.id = p.workspace_id "
                                  "WHERE w.org_id = :o ORDER BY p.created_at LIMIT 1"), {"o": org}).scalar()

        def person(name: str, role: str, *, scope: str, with_totp: bool) -> dict[str, Any]:
            pid = f"usr_{name}{secrets.token_hex(8)}"
            db.execute(text("INSERT INTO identity_principals (id, org_id, kind, status, display_name) VALUES "
                            "(:p, :o, 'user', 'active', :n)"), {"p": pid, "o": org, "n": name.title()})
            db.execute(text("INSERT INTO identity_users (principal_id, email, email_verified_at, password_hash) "
                            "VALUES (:p, :e, now(), :h)"),
                       {"p": pid, "e": f"{name}-{pid[-6:]}@example.test", "h": hash_password(STRONG)})
            db.execute(text("INSERT INTO identity_role_bindings (id, org_id, subject_type, subject_id, role_id, "
                            "scope_type, scope_id, granted_by) VALUES (:id, :o, 'principal', :p, :r, :st, :sid, 'e2e')"),
                       {"id": "rbd_" + secrets.token_hex(10), "o": org, "p": pid, "r": f"arl_{role}", "st": scope,
                        "sid": project if scope == "project" else org})
            seed = None
            if with_totp:
                seed = totp.generate_secret()
                item = vault.store(org_id=org, owner_principal_id=pid, kind=vault.VaultItemKind.TOTP_SECRET,
                                   provider="totp", secret=seed, db=db)
                db.execute(text("INSERT INTO identity_totp (principal_id, vault_item_id, last_step) VALUES (:p, :v, 0)"),
                           {"p": pid, "v": item})
            issued = sessions.issue(db, principal_id=pid, org_id=org,
                                    methods=["password", "totp"] if with_totp else ["password"],
                                    mfa_at=datetime.now(timezone.utc) if with_totp else None, ip="127.0.0.1",
                                    user_agent="e2e", device_id_hash=None)
            return {"id": pid, "session": issued.token, "totp": seed}

        w["member"] = person("ada", "viewer", scope="project", with_totp=True)
        w["plain"] = person("ben", "viewer", scope="project", with_totp=False)
        w["owner"] = person("own", "owner", scope="org", with_totp=True)
        authz.bump_epoch(org, db=db, reason="e2e")
        staff = pools.create(db, org_id=org, actor=w["owner"]["id"], body={"name": "Staff", "kind": "workforce"})
        cust = pools.create(db, org_id=org, actor=w["owner"]["id"], body={
            "name": "Bank customers", "kind": "customer", "signup": "open", "mfa": "required", "passkeys": True,
            "password_policy": {"verify_email": True}})
        scopes = ["openid", "profile", "email", "offline_access"]
        bank, bank_secret = clients.create(db, org_id=org, actor=w["owner"]["id"], body={
            "project_id": project, "app_key": "bank", "pool_id": cust["id"], "name": "Bank",
            "redirect_uris": [rp.callback], "post_logout_redirect_uris": [f"{rp.origin}/bye"], "allowed_scopes": scopes})
        intra, intra_secret = clients.create(db, org_id=org, actor=w["owner"]["id"], body={
            "project_id": project, "app_key": "intranet", "pool_id": staff["id"], "name": "Intranet",
            "redirect_uris": [rp.callback], "allowed_scopes": ["openid", "profile", "email"]})
    w.update(org=org, project=project, bank=(bank["id"], bank_secret), intra=(intra["id"], intra_secret),
             pool=cust["id"])
    return w


# ---- the protocol, from the RP's side ------------------------------------------------------------------

class Flow:
    def __init__(self, boss: str, w: dict[str, Any], rp: RP, client: tuple[str, str], scope: str) -> None:
        self.boss, self.w, self.rp, self.client = boss, w, rp, client
        self.issuer = f"{boss}/oidc/{w['org']}"
        self.verifier = b64u(secrets.token_bytes(32))
        self.state, self.nonce = secrets.token_urlsafe(12), secrets.token_urlsafe(12)
        self.scope = scope

    def url(self, **extra: str) -> str:
        q = {"response_type": "code", "client_id": self.client[0], "redirect_uri": self.rp.callback,
             "scope": self.scope, "state": self.state, "nonce": self.nonce,
             "code_challenge": b64u(hashlib.sha256(self.verifier.encode()).digest()),
             "code_challenge_method": "S256", **extra}
        return f"{self.issuer}/authorize?{urllib.parse.urlencode(q)}"

    def finish(self, landed: str, what: str, *, mfa: Optional[bool] = None) -> Optional[dict[str, Any]]:
        """The RP's half: state and iss, then the code at the token endpoint (through the console's
        origin), then the ID token's signature, issuer, audience and nonce."""
        import jwt

        q = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(landed).query))
        if not check(landed.startswith(self.rp.callback) and q.get("state") == self.state
                     and q.get("iss") == self.issuer and q.get("code"), f"{what}: back at the RP with code, state, iss"):
            return None
        basic = base64.b64encode(f"{urllib.parse.quote(self.client[0])}:{urllib.parse.quote(self.client[1])}".encode())
        req = urllib.request.Request(f"{self.issuer}/token", data=urllib.parse.urlencode({
            "grant_type": "authorization_code", "code": q["code"], "redirect_uri": self.rp.callback,
            "code_verifier": self.verifier}).encode(), headers={
            "Authorization": "Basic " + basic.decode(), "Content-Type": "application/x-www-form-urlencoded"})
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                tok = json.loads(r.read())
        except urllib.error.HTTPError as e:
            check(False, f"{what}: token exchange ({e.code})")
            return None
        with urllib.request.urlopen(f"{self.issuer}/jwks", timeout=30) as r:
            keys = jwt.PyJWKSet.from_dict(json.loads(r.read()))
        head = jwt.get_unverified_header(tok["id_token"])
        key = next(k for k in keys.keys if k.key_id == head.get("kid"))
        claims = jwt.decode(tok["id_token"], key.key, algorithms=[head["alg"]], audience=self.client[0],
                            issuer=self.issuer)
        check(claims.get("nonce") == self.nonce, f"{what}: ID token verified (signature, iss, aud, nonce)")
        if mfa is not None:
            check((claims.get("acr") == "urn:boss:acr:mfa") is mfa, f"{what}: acr says {'MFA' if mfa else 'password'}")
        return claims


# ---- the browser -----------------------------------------------------------------------------------------

class Watch:
    """What Chromium did: CSP violations, and the Origin it sent on every hosted form POST."""

    def __init__(self, page: Any) -> None:
        self.csp: list[str] = []
        self.origins: list[str] = []
        page.on("console", lambda m: self.csp.append(m.text) if "Content Security Policy" in m.text else None)
        page.on("request", lambda r: self.origins.append(r.headers.get("origin", "<none>"))
                if r.method == "POST" and "/oidc/" in r.url and "/token" not in r.url else None)


def submit(page: Any, label: str) -> None:
    page.get_by_role("button", name=label, exact=True).click()


def landed(page: Any, rp: RP, timeout: float = 30_000) -> str:
    page.wait_for_url(f"{rp.callback}*", timeout=timeout)
    return page.url


def run_flows(boss: str, w: dict[str, Any], rp: RP, box: Mailbox, headed: bool) -> None:
    from playwright.sync_api import sync_playwright

    codes = TOTP()
    email = f"cara-{secrets.token_hex(3)}@example.test"
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=not headed)

        # 1. customer sign-up with an emailed code, TOTP enrolment, consent -------------------------
        ctx = browser.new_context(viewport={"width": 1280, "height": 900})
        page = ctx.new_page()
        watch = Watch(page)
        f = Flow(boss, w, rp, w["bank"], "openid profile email offline_access")
        first = page.goto(f.url())
        check(first and first.headers.get("referrer-policy") == "same-origin",
              "hosted page: Referrer-Policy same-origin (Chrome keeps the Origin header)")
        check(page.get_by_role("heading", name="Sign in to Bank").is_visible(), "1 sign-in page names the app")
        page.get_by_role("link", name="Create an account").click()
        page.fill("#name", "Cara")
        page.fill("#email", email)
        page.fill("#password", STRONG)
        sent = len(box.mail)
        submit(page, "Create account")
        page.get_by_role("heading", name="Check your email").wait_for()
        page.fill("#code", box.code_for(email, sent))
        submit(page, "Continue")
        page.get_by_role("heading", name="Set up your authenticator app").wait_for()
        seed = page.locator("code").first.inner_text().strip()
        page.fill("#code", codes.code(seed))
        submit(page, "Confirm")
        page.get_by_role("heading", name="Bank wants to sign you in").wait_for()
        check("BOSS" not in page.locator("main").inner_text(), "1 the customer's consent page never mentions BOSS")
        submit(page, "Allow")
        f.finish(landed(page, rp), "1 sign-up + emailed code + TOTP + consent", mfa=True)
        check(watch.origins and "null" not in watch.origins and "<none>" not in watch.origins,
              f"1 Chromium sent a real Origin on every hosted POST ({len(watch.origins)} posts)")

        # 2. the account page without ?pool; a passkey; a passkey-only sign-in ----------------------
        page.goto(f"{boss}/oidc/{w['org']}/account")
        page.get_by_role("heading", name="Your Bank customers account").wait_for()
        body = page.locator("main").inner_text()
        check("expired" not in body.lower(), "2 the account page opens without ?pool")
        check("Add authenticator app" not in body, "2 no second authenticator app is offered once one is added")
        cdp = ctx.new_cdp_session(page)
        cdp.send("WebAuthn.enable")
        cdp.send("WebAuthn.addVirtualAuthenticator", {"options": {
            "protocol": "ctap2", "transport": "internal", "hasResidentKey": True, "hasUserVerification": True,
            "isUserVerified": True, "automaticPresenceSimulation": True}})
        add = page.get_by_role("button", name="Add a passkey")
        out = page.get_by_role("button", name="Sign out")
        check(add.bounding_box()["y"] < out.bounding_box()["y"], "2 'Add a passkey' sits above 'Sign out'")
        add.click()
        page.wait_for_load_state("networkidle")
        page.goto(f"{boss}/oidc/{w['org']}/account")
        check("Passkey" in page.locator("main").inner_text(), "2 the passkey is listed on the account page")
        page.get_by_role("button", name="Sign out").click()
        page.get_by_role("heading", name="Signed out").wait_for()
        check(page.locator(".brand").inner_text().strip() == "Bank customers", "2 the signed-out page is the app's, not BOSS's")
        f = Flow(boss, w, rp, w["bank"], "openid profile email offline_access")
        page.goto(f.url())
        page.get_by_role("button", name="Sign in with a passkey").click()
        f.finish(landed(page, rp), "2 passkey-only sign-in (virtual authenticator)", mfa=True)
        check(not watch.csp, f"1-2 no CSP violation ({watch.csp[:1]})")
        ctx.close()

        # 3. a password reset by emailed code, then sign in with it --------------------------------
        ctx = browser.new_context()
        page = ctx.new_page()
        watch = Watch(page)
        f = Flow(boss, w, rp, w["bank"], "openid email")
        page.goto(f.url())
        page.get_by_role("link", name="Forgot password?").click()
        page.fill("#email", email)
        sent = len(box.mail)
        submit(page, "Send code")
        page.get_by_role("heading", name="Check your email").wait_for()
        page.fill("#code", box.code_for(email, sent))
        page.fill("#password", "new-" + STRONG)
        submit(page, "Continue")
        page.get_by_text("Password changed. Sign in with it.").wait_for()
        page.fill("#password", "new-" + STRONG)
        submit(page, "Sign in")
        page.get_by_role("heading", name="Second step").wait_for()
        page.fill("#code", codes.code(seed))
        submit(page, "Verify")
        f.finish(landed(page, rp), "3 password reset, then password + TOTP", mfa=True)
        ctx.close()

        # 4. a returning sign-in with MFA and no consent screen (P0-2) ---------------------------
        ctx = browser.new_context()
        page = ctx.new_page()
        watch = Watch(page)
        f = Flow(boss, w, rp, w["bank"], "openid email")
        page.goto(f.url())
        page.fill("#email", email)
        page.fill("#password", "new-" + STRONG)
        submit(page, "Sign in")
        page.get_by_role("heading", name="Second step").wait_for()
        page.fill("#code", codes.code(seed))
        submit(page, "Verify")
        f.finish(landed(page, rp), "4 returning sign-in, MFA, no consent: the redirect is not blocked", mfa=True)

        # 5. prompt=login on the signed-in browser -----------------------------------------------
        f = Flow(boss, w, rp, w["bank"], "openid email")
        page.goto(f.url(prompt="login"))
        page.get_by_role("heading", name="Sign in to Bank").wait_for()
        page.fill("#email", email)
        page.fill("#password", "new-" + STRONG)
        submit(page, "Sign in")
        page.get_by_role("heading", name="Second step").wait_for()
        page.fill("#code", codes.code(seed))
        submit(page, "Verify")
        f.finish(landed(page, rp), "5 prompt=login asks again and returns", mfa=True)
        check(not watch.csp and "null" not in watch.origins, "3-5 no CSP violation, no Origin: null")
        ctx.close()

        # 6. workforce: confirm it's you ------------------------------------------------------------
        def console_context(who: dict[str, Any], **kw: Any) -> Any:
            c = browser.new_context(**kw)
            # a __Host- cookie: Secure, Path=/, no Domain (Chromium treats http://localhost as secure)
            c.add_cookies([{"name": "__Host-boss_session", "value": who["session"], "domain": "localhost", "path": "/",
                            "secure": True, "httpOnly": True, "sameSite": "Lax"}])
            return c

        ctx = console_context(w["member"])
        page = ctx.new_page()
        watch = Watch(page)
        f = Flow(boss, w, rp, w["intra"], "openid profile email")
        page.goto(f.url(prompt="login"))
        page.get_by_role("heading", name=re.compile("Confirm it's you")).wait_for()
        page.fill("#code", codes.code(w["member"]["totp"]))
        submit(page, "Confirm")
        page.wait_for_url(re.compile(rf"^({re.escape(rp.callback)}|{re.escape(boss)})"))
        if not page.url.startswith(rp.callback):
            page.get_by_role("heading", name="Intranet wants to sign you in").wait_for()
            check("Signed in to BOSS as" in page.locator("main").inner_text(), "6 a member's consent names their BOSS account")
            submit(page, "Allow")
        f.finish(landed(page, rp), "6 workforce member: prompt=login, confirm it's you with TOTP")
        check(not watch.csp and "null" not in watch.origins, "6 no CSP violation, no Origin: null")
        ctx.close()

        ctx = console_context(w["plain"])
        page = ctx.new_page()
        f = Flow(boss, w, rp, w["intra"], "openid profile email")
        page.goto(f.url(prompt="login"))
        page.get_by_role("heading", name="Confirm it's you").wait_for()
        check(page.locator("#password").count() == 1 and page.locator("#code").count() == 0,
              "6 a member with no second factor is asked for their password, not a code")
        page.fill("#password", STRONG)
        submit(page, "Confirm")
        page.wait_for_url(re.compile(rf"^({re.escape(rp.callback)}|{re.escape(boss)})"))
        if not page.url.startswith(rp.callback):
            page.get_by_role("heading", name="Intranet wants to sign you in").wait_for()
            submit(page, "Allow")
        f.finish(landed(page, rp), "6 workforce member with a password only: confirm it's you")
        ctx.close()

        # 7. the console: App sign-in at 1360 px and 390 px ---------------------------------------
        ctx = console_context(w["owner"], viewport={"width": 1360, "height": 900})
        page = ctx.new_page()
        page.goto(f"{boss}/identity/app-sign-in", timeout=180_000)
        page.locator('[data-testid="client-row"]').first.wait_for(timeout=120_000)
        strip = page.locator('[data-testid="identity-tabs"]')
        box_ = strip.bounding_box()
        tabs = strip.locator("a")
        clipped = [tabs.nth(i).inner_text() for i in range(tabs.count())
                   if (lambda b: b is None or b["x"] + b["width"] > box_["x"] + box_["width"] + 1)(tabs.nth(i).bounding_box())]
        check(tabs.count() >= 10 and not clipped, f"7 at 1360 px every Identity tab is visible ({tabs.count()} tabs; clipped {clipped})")
        row = page.locator('[data-testid="client-row"]').first
        check("confidential" not in row.inner_text() and "offline_access" not in row.inner_text(),
              "7 the Clients table uses plain words")
        row.click()
        sheet = page.locator(".idn-sheet.side")
        sheet.wait_for()
        b = sheet.bounding_box()
        check(b["x"] + b["width"] >= 1359 and b["height"] >= 880, "7 a client opens in a side sheet (docked right, full height)")
        ctx.close()
        ctx = console_context(w["owner"], viewport={"width": 390, "height": 844}, device_scale_factor=2, is_mobile=True,
                              has_touch=True)
        page = ctx.new_page()
        page.goto(f"{boss}/identity/app-sign-in", timeout=180_000)
        page.locator('[data-testid="client-row"]').first.wait_for(timeout=120_000)
        width = page.evaluate("document.documentElement.scrollWidth")
        check(width <= 390, f"7 at 390 px nothing scrolls sideways (page {width} px)")
        ctx.close()
        browser.close()


# ---- the stack ------------------------------------------------------------------------------------------

def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--keep", action="store_true", help="leave the servers running at the end (Ctrl-C stops)")
    ap.add_argument("--headed", action="store_true")
    args = ap.parse_args()
    url = os.environ.get("DATABASE_URL") or ""
    from apps.api.tests.db_guard import assert_safe_to_wipe

    assert_safe_to_wipe(url)
    run_dir = Path(os.environ.get("BOSS_E2E_RUN_DIR") or (REPO / "artifacts" / "e2e-oidc-browser"))
    run_dir.mkdir(parents=True, exist_ok=True)
    box, rp = Mailbox(), RP()
    smtp = start_smtp(box)
    api_port, web_port = free_port(), free_port()
    boss = f"http://localhost:{web_port}"
    env = {**os.environ,
           "PYTHONPATH": os.pathsep.join([str(REPO), str(REPO / "engine")]), "PYTHONUNBUFFERED": "1",
           "DATABASE_URL": url, "BOSS_EDITION": "community", "BOSS_PUBLIC_URL": boss,
           "BOSS_MASTER_KEY": os.environ.get("BOSS_MASTER_KEY") or base64.b64encode(secrets.token_bytes(32)).decode(),
           "BOSS_IDENTITY_SERVER_KEY": os.environ.get("BOSS_IDENTITY_SERVER_KEY") or base64.b64encode(secrets.token_bytes(32)).decode(),
           "BOSS_DEFAULT_ORG_ID": "org_boss", "BOSS_DEFAULT_WORKSPACE_ID": "ws_boss", "BOSS_DEFAULT_PROJECT_ID": "boss-dogfood",
           "BOSS_EMAIL_SMTP_HOST": "127.0.0.1", "BOSS_EMAIL_SMTP_PORT": str(smtp), "BOSS_EMAIL_SMTP_USE_TLS": "false",
           "BOSS_EMAIL_FROM_ADDRESS": "boss@example.test", "BOSS_EMAIL_REAL_SEND_ALLOWED": "true",
           "BOSS_TELEMETRY": "off", "BOSS_REQUIRE_API_AUTH": "true", "BOSS_PREVIEW_LISTEN": ""}
    env.pop("BOSS_MASTER_KEY_FILE", None)
    os.environ.update({k: env[k] for k in ("DATABASE_URL", "BOSS_MASTER_KEY", "BOSS_IDENTITY_SERVER_KEY", "BOSS_PUBLIC_URL",
                                            "BOSS_EDITION", "BOSS_DEFAULT_ORG_ID", "BOSS_DEFAULT_WORKSPACE_ID",
                                            "BOSS_DEFAULT_PROJECT_ID")})
    procs: list[subprocess.Popen] = []

    def spawn(name: str, argv: list[str], cwd: Path, penv: dict[str, str]) -> subprocess.Popen:
        log = open(run_dir / f"{name}.log", "wb")
        p = subprocess.Popen(argv, cwd=str(cwd), env=penv, stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
        procs.append(p)
        return p

    try:
        api = spawn("api", [sys.executable, "-m", "apps.api.serve", "--host", "127.0.0.1", "--port", str(api_port)],
                    REPO, env)
        wait_http(f"http://127.0.0.1:{api_port}/health", 240, api)
        seeded = subprocess.run([sys.executable, "-c", "from boss_engine.bootstrap import seed; seed()"], cwd=str(REPO),
                                env=env, capture_output=True, text=True)
        if seeded.returncode != 0:
            raise RuntimeError(f"seed failed:\n{seeded.stderr[-2000:]}")
        web_dir = REPO / "apps" / "web"
        web_env = {k: os.environ[k] for k in ("PATH", "HOME", "LANG", "TMPDIR") if k in os.environ}
        web_env.update({"BOSS_API_URL": f"http://127.0.0.1:{api_port}", "NEXT_PUBLIC_BOSS_API_URL": f"http://127.0.0.1:{api_port}",
                        "NEXT_PUBLIC_BOSS_PROJECT_ID": "boss-dogfood", "NEXT_TELEMETRY_DISABLED": "1"})
        web = spawn("web", [str(web_dir / "node_modules" / ".bin" / "next"), "dev", "-p", str(web_port)], web_dir, web_env)
        wait_http(f"{boss}/oidc/org_boss/.well-known/openid-configuration", 300, web)
        world = build_world(rp)
        print(f"stack: console {boss}, api :{api_port}, rp {rp.origin}, org {world['org']}", flush=True)
        run_flows(boss, world, rp, box, args.headed)
        if args.keep:
            print("--keep: the stack is up; Ctrl-C stops it", flush=True)
            signal.pause()
    except Exception as exc:  # noqa: BLE001
        check(False, f"the run stopped: {type(exc).__name__}: {str(exc)[:600]}")
    finally:
        for p in procs:
            try:
                os.killpg(p.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
    print(f"\n{len(FAILURES)} failure(s)" + ("".join(f"\n  - {f}" for f in FAILURES)), flush=True)
    return len(FAILURES)


if __name__ == "__main__":
    sys.exit(main())
