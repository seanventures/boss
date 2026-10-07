"""IDENTITY JOURNEYS J1–J15 AGAINST A REAL, ENFORCE-MODE STACK (IDENTITY.md §15.2).

The console-stub journeys (apps/web/tests/_helpers/identity-journeys.ts) prove the screens; these
prove the whole journey: a real browser through the real console, AND the API half of every step
(status codes, audit events, database state). A check that fails because of a known, assigned
bug is recorded `xfail` naming the fix item; assertions are never weakened to pass.

    cd tests && python3 -m e2e.identity.journeys_real --console http://localhost:53100 \\
        --accounts <accounts.json> --out <dir> [--only J3,J7]

J1, J2, J7 and J14 need a stack this run controls (an unclaimed install, env it can change, a
Keycloak): they bring up a second compose project (`--stack2`, default bossfxd2) and always take
it down again. The others run on the given, already claimed and seeded stack, and only ever touch
people, roles, groups and service accounts they create themselves (named fxd-j<N>-*).

Exit status: 0 when no check failed (xfails allowed), 1 otherwise. Report: <out>/journeys.json
(or --report), screenshots under <out>/shots/<journey>/ (or --shots).
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import time
from pathlib import Path
from typing import Any, Callable

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from e2e.harness import identity as I  # noqa: E402
from e2e.identity import kit as K  # noqa: E402
from e2e.identity import keycloak as KC  # noqa: E402
from e2e.identity.kit import Journey, api, audit_has, audit_seq, me, perms, short  # noqa: E402
from e2e.identity.stack2 import Stack2, docker_headroom_gib  # noqa: E402

PROJECT = "boss-dogfood"


class Env:
    """What every journey gets: the browser, the main stack, the seeded accounts, options."""

    def __init__(self, args: argparse.Namespace, browser: Any):
        self.args = args
        self.browser = browser
        self.web = args.console.rstrip("/")
        self.stack = K.Stack(project=args.project, web=self.web, api=args.api.rstrip("/"),
                             env_file=Path(args.env_file))
        self.A = I.load_accounts(Path(args.accounts))
        for v in self.A.values():
            for attr in ("password", "totp_secret", "key"):
                K.secret(getattr(v, attr, None))
        self.shared: dict[str, Any] = {}     # values one journey leaves for a later one (J8 → J9/J15)

    # ---- the run's own Owner and Admin -------------------------------------------------------------
    # Every walk on this stack signs in as seed-owner/seed-admin; their TOTP steps and the per-person
    # MFA rate limit (20/h) are shared, so a run that signs in as them for every journey is refused.
    # The run therefore makes its OWN Owner and Admin once (invited by seed-owner, one sign-in),
    # keeps them in <out>/fxd-people.json (0600) for later runs, and removes them with --drop-people.

    def _people_file(self) -> Path:
        return Path(self.args.out) / "fxd-people.json"

    def _load_people(self) -> dict[str, K.Person]:
        f = self._people_file()
        if not f.exists():
            return {}
        out = {}
        for role, d in json.loads(f.read_text()).items():
            p = K.Person(**d)
            for v in (p.password, p.totp_secret, *p.recovery_codes):
                K.secret(v)
            out[role] = p
        return out

    def _save_people(self, people: dict[str, K.Person]) -> None:
        K.write_private(self._people_file(), json.dumps({r: vars(p) for r, p in people.items()}))

    def _own(self, role: str) -> K.Person:
        people = self.shared.setdefault("_people", self._load_people())
        p = people.get(role)
        if p is not None and not self.shared.get(f"_checked_{role}"):
            ctx, page = K.new_page(self.browser)
            page.goto(f"{self.web}/login", wait_until="domcontentloaded")
            r = K.raw_fetch(page, "POST", "/v1/auth/login", {"email": p.email, "password": p.password}, csrf=False)
            ctx.close()
            if r["status"] != 200:
                p = None
            self.shared[f"_checked_{role}"] = True
        if p is None:
            j = Journey("setup", "", "", Path(self.args.out) / "shots-setup")
            if role == "owner":       # the one seed-owner sign-in of a run
                ctx, page = K.signed_in_page(self.browser, self.web, self.A["owner"])
                p, _, _ = K.make_person(self.stack, self.browser, page, self.A["owner"], role, f"run{role}", j)
                ctx.close()
            else:                     # everyone else is invited by the run's own Owner
                h, page = self.owner_page()
                p, _, _ = K.make_person(self.stack, self.browser, page, self.owner, role, f"run{role}", j)
                h.close()
            people[role] = p
            self._save_people(people)
            self.shared[f"_checked_{role}"] = True
        return p

    @property
    def owner(self) -> K.Person:
        return self._own("owner")

    @property
    def admin(self) -> K.Person:
        return self._own("admin")

    def owner_page(self, j: Any = None) -> tuple[Any, Any]:
        """A page in ONE seed-owner session shared by every journey (fewer sign-ins as an account
        other walks also use). Close the returned handle: it closes only the page."""
        held = self.shared.get("_owner_ctx")
        if held is None or not _alive(held[1]):
            ctx, page = K.signed_in_page(self.browser, self.web, self.owner)
            self.shared["_owner_ctx"] = (ctx, page)
        ctx = self.shared["_owner_ctx"][0]
        page = ctx.new_page()
        page.goto(f"{self.web}/identity", wait_until="domcontentloaded")
        K.settle(page, 8000)
        return K.PageOnly(page), page


def _alive(page: Any) -> bool:
    try:
        return not page.is_closed() and me(page)["status"] == 200
    except Exception:  # noqa: BLE001
        return False


JOURNEYS: dict[str, tuple[str, str, Callable[[Env, Journey], None]]] = {}


def journey(jid: str, title: str, proves: str):
    def deco(fn: Callable[[Env, Journey], None]):
        JOURNEYS[jid] = (title, proves, fn)
        return fn
    return deco


def _role_id(page: Any, key: str) -> str:
    for r in (api(page, "GET", "/v1/identity/access-roles").get("body") or {}).get("roles", []):
        if r.get("key") == key:
            return r["id"]
    return ""


# =================================================================================================
# J3 — invite an Operator, accept, set a password, TOTP enrol, sign out, sign in with TOTP, step-up
# =================================================================================================

@journey("J3", "Invite an Operator, accept, enrol TOTP, sign in again, step-up",
         "The grant limit greys out roles the inviter does not fully hold (and the API refuses them); "
         "an invitation becomes a person with a password and TOTP; sign-out ends the session; "
         "password+TOTP signs in as Operator; a step-up permission asks again once MFA is stale.")
def j3(env: Env, j: Journey) -> None:
    owner, admin = env.owner, env.admin
    octx, opage = env.owner_page(j)
    # A role the Admin does not fully hold: identity_provider:manage is Owner-only.
    key = f"fxd_j3_reach_{int(time.time()) % 100000}"
    made = K.stepped(opage, owner, "POST", "/v1/identity/access-roles",
                     {"key": key, "name": f"fxd-j3-out-of-reach-{key[-5:]}",
                      "permissions": ["directory:read", "identity_provider:manage"],
                      "bindable_scopes": ["org"], "confirm_critical": ["identity_provider:manage"]}, j)
    j.check("Owner creates a custom role holding an Owner-only permission", made["status"] == 201,
            expect="201", actual=f"{made['status']} {short(made.get('body'))}", fatal=True)
    env.shared.setdefault("_cleanup", []).append(lambda rid=made["body"]["id"]: _drop_role(env, rid))
    octx.close()

    actx, apage = K.signed_in_page(env.browser, env.web, admin, j)
    apage.goto(f"{env.web}/identity/people", wait_until="domcontentloaded")
    apage.wait_for_selector("[data-testid=people-invite]", timeout=30000)
    j.shot(apage, "admin-people")
    apage.click("[data-testid=people-invite]")
    apage.wait_for_selector("#invite-emails", timeout=15000)
    radio = apage.get_by_role("radio").filter(has_text=re.compile(re.escape(made["body"].get("name", key)), re.I))
    j.check("the out-of-reach role is offered disabled with a reason", radio.count() == 1
            and radio.first.get_attribute("aria-disabled") == "true"
            and "hold" in (radio.first.get_attribute("title") or ""),
            expect="radio aria-disabled=true, title says you don't hold all of it",
            actual=f"count={radio.count()} aria-disabled={radio.first.get_attribute('aria-disabled') if radio.count() else None} "
                   f"title={radio.first.get_attribute('title') if radio.count() else None}")
    refused = K.stepped(apage, admin, "POST", "/v1/identity/users",
                        {"email": f"fxd-j3-nope-{int(time.time())}@example.test",
                         "bindings": [{"role_key": key, "scope_type": "org"}], "group_ids": []}, j)
    j.check("API: an Admin inviting into a role they do not hold is refused", refused["status"] == 403,
            expect="403 (grant limit, §6.11)", actual=f"{refused['status']} {short(refused.get('body'))}",
            repro=f"POST /v1/identity/users as seed-admin with bindings role_key={key}")
    if refused["status"] == 201 and (refused.get("body") or {}).get("invitation_id"):
        api(apage, "DELETE", f"/v1/identity/invitations/{refused['body']['invitation_id']}")

    # Invite an Operator through the sheet.
    stamp = time.strftime("%H%M%S")
    p = K.Person(role="operator", email=f"fxd-j3-{stamp}@example.test", name=f"FXD J3 {stamp}",
                 password=K.strong_password("j3"))
    seq0 = audit_seq(apage)
    apage.fill("#invite-emails", p.email)
    apage.get_by_role("radio").filter(has_text=re.compile("^Operator", re.I)).first.click()
    j.shot(apage, "invite-sheet")
    apage.click("[data-testid=invite-send]")
    K.answer_step_up_sheet(apage, admin, timeout_ms=5000)
    apage.wait_for_selector("[data-testid=invite-links] code", timeout=20000)
    link = K.secret(apage.locator("[data-testid=invite-links] code").first.inner_text().strip()) or ""
    j.check("no SMTP: the sheet shows one single-use link for the person", "/invite#t=" in link,
            expect="a /invite#t=… link", actual="link shown" if link else "no link")
    j.shot(apage, "invite-link-shown")
    inv = audit_has(apage, "user.invited", since_seq=seq0)
    j.check("audit: user.invited by the Admin", bool(inv) and (inv.get("raw") or {}).get("actor_principal_id") == admin.principal_id,
            expect="a user.invited event by seed-admin", actual=short(inv))
    actx.close()

    # Accept as the invitee.
    ictx, ipage = K.new_page(env.browser, j)
    ipage.goto(link, wait_until="domcontentloaded")
    ipage.wait_for_selector("#inv-name", timeout=30000)
    j.shot(ipage, "invite-accept")
    ipage.fill("#inv-name", p.name)
    ipage.fill("#inv-pw", p.password)
    ipage.click("[data-testid=invite-accept]")
    ipage.wait_for_url(lambda u: "/invite" not in u, timeout=30000)
    m = me(ipage)
    p.principal_id = (m.get("body") or {}).get("principal_id", "")
    j.check("accepted: signed in as the new Operator", m["status"] == 200
            and (m.get("body") or {}).get("access_roles") == ["operator"],
            expect="me 200, access_roles [operator]", actual=f"{m['status']} roles={(m.get('body') or {}).get('access_roles')} at {ipage.url}")
    reuse = K.raw_fetch(ipage, "POST", "/v1/auth/invitations/accept",
                        {"token": K.token_of(link), "name": "again", "password": K.strong_password("j3b")}, csrf=False)
    j.check("API: the invitation link works once", reuse["status"] in (400, 404),
            expect="400 invitation_invalid", actual=f"{reuse['status']} {short(reuse.get('body'))}")

    # Enrol TOTP in the console.
    ipage.goto(f"{env.web}/enrol", wait_until="domcontentloaded")
    ipage.wait_for_selector("[data-testid=enrol-totp], [data-testid=totp-step]", timeout=30000)
    if ipage.locator("[data-testid=enrol-totp]").count():
        ipage.click("[data-testid=enrol-totp]")
    ipage.wait_for_selector("[aria-label='Authenticator key']", timeout=20000)
    p.totp_secret = K.secret(ipage.locator("[aria-label='Authenticator key']").inner_text().strip().replace(" ", ""))
    j.shot(ipage, "totp-secret-shown-once")
    K.code_box(ipage, "Code from your app").first.fill(I.fresh_totp(p.totp_secret))
    ipage.wait_for_selector("[data-testid=recovery-codes]", timeout=20000)
    codes = [K.secret(x.strip()) for x in ipage.locator("[aria-label='Recovery codes'] li").all_inner_texts()]
    j.check("recovery codes shown once with Continue gated", len(codes) == 10
            and ipage.locator("[data-testid=codes-continue]").is_disabled(),
            expect="10 codes, Continue disabled until stored", actual=f"{len(codes)} codes, disabled={ipage.locator('[data-testid=codes-continue]').is_disabled()}")
    j.shot(ipage, "recovery-codes")
    ipage.check("[data-testid=codes-stored]")
    ipage.click("[data-testid=codes-continue]")
    ipage.wait_for_timeout(1500)
    m = me(ipage)
    j.check("API: TOTP enrolled", (m.get("body") or {}).get("sign_in", {}).get("totp") is True,
            expect="me.sign_in.totp true", actual=short((m.get("body") or {}).get("sign_in")))
    # Sign out from the account menu.
    ipage.goto(f"{env.web}/", wait_until="domcontentloaded")
    with j.tolerate("sign out through the account menu"):
        ipage.click("[data-testid=account-menu]", timeout=20000)
        ipage.click("[data-testid=sign-out]", timeout=10000)
        ipage.wait_for_url(re.compile(r"/(login|signin)"), timeout=20000)
    after = K.raw_fetch(ipage, "GET", "/v1/identity/me", csrf=False)
    j.check("API: signed out — /v1/identity/me is 401", after["status"] == 401,
            expect="401", actual=after["status"])
    ictx.close()

    # Sign in with password + TOTP.
    sctx, spage = K.new_page(env.browser, j)
    K.sign_in(spage, env.web, p)
    j.shot(spage, "signed-in-password-totp")
    m = me(spage)
    sess = api(spage, "GET", "/v1/identity/me/sessions")
    cur = [s for s in (sess.get("body") or {}).get("sessions", []) if s.get("current")]
    j.check("API: the new session's methods are password + TOTP",
            bool(cur) and set(cur[0].get("methods") or []) >= {"pwd", "totp"},
            expect="methods ⊇ {pwd, totp}", actual=short(cur))
    nope = K.stepped(spage, p, "POST", "/v1/identity/users",
                     {"email": "fxd-j3-x@example.test", "bindings": [{"role_key": "viewer", "scope_type": "org"}],
                      "group_ids": []}, j)
    j.check("API: an Operator cannot invite people", nope["status"] == 403,
            expect="403 forbidden user:invite", actual=f"{nope['status']} {short(nope.get('body'))}")

    # Step-up: MFA older than 10 minutes, then a step-up action (new recovery codes).
    K.age_mfa(env.stack, p.principal_id)
    stale = api(spage, "POST", "/v1/identity/me/recovery-codes")
    j.check("API: with stale MFA a step-up action answers 401 step_up_required",
            stale["status"] == 401 and (stale.get("body") or {}).get("error") == "step_up_required",
            expect="401 step_up_required", actual=f"{stale['status']} {short(stale.get('body'))}")
    seq1 = audit_seq(spage)
    spage.goto(f"{env.web}/account", wait_until="domcontentloaded")
    spage.wait_for_selector("[data-testid=regen-codes]", timeout=30000)
    spage.click("[data-testid=regen-codes]")
    sheet = K.code_box(spage)
    with j.tolerate("the step-up sheet opens and accepts the code"):
        sheet.first.wait_for(state="visible", timeout=10000)
        j.shot(spage, "step-up-sheet")
        j.check("the step-up sheet asks for the authenticator code", True, expect="sheet", actual="sheet open")
        K.answer_step_up_sheet(spage, p)
        spage.wait_for_timeout(2000)
        j.shot(spage, "after-step-up")
    ok_codes = audit_has_as_owner(env, "recovery_codes.generated", target=p.principal_id, since_seq=seq1)
    stepped_ev = audit_has_as_owner(env, "auth.step_up", actor=p.principal_id, since_seq=seq1)
    j.check("audit: auth.step_up then recovery_codes.generated for the Operator", bool(stepped_ev) and bool(ok_codes),
            expect="both events", actual=f"step_up={bool(stepped_ev)} generated={bool(ok_codes)}")
    sctx.close()
    env.shared["j3_person"] = p


def audit_has_as_owner(env: Env, event_type: str, **kw: Any) -> Any:
    """Audit lookups as the Owner (an Operator cannot read the audit log)."""
    if env.shared.get("_owner_ctx") is None or not _alive(env.shared["_owner_ctx"][1]):
        h, _ = env.owner_page(None)
        h.close()
    return audit_has(env.shared["_owner_ctx"][1], event_type, **kw)


# =================================================================================================
# J4 — forgot password; reset; other sessions revoked; MFA still required
# =================================================================================================

@journey("J4", "Forgot password, reset, other sessions revoked, MFA still required",
         "Forgot-password answers the same words for any address; a reset link (emailed with SMTP, "
         "else issued by an admin and shown once) sets a new password, ends every other session, "
         "and does not bypass the second factor.")
def j4(env: Env, j: Journey) -> None:
    owner = env.owner
    octx, opage = env.owner_page(j)
    p, _, _ = K.make_person(env.stack, env.browser, opage, owner, "operator", "j4", j)

    actx, apage = K.new_page(env.browser, j)
    apage.goto(f"{env.web}/login", wait_until="domcontentloaded")
    opts = K.raw_fetch(apage, "GET", "/v1/auth/options", csrf=False)
    smtp = bool((opts.get("body") or {}).get("smtp"))
    j.note(f"SMTP configured on this stack: {smtp}")
    apage.wait_for_selector("#email", timeout=30000)
    apage.fill("#email", p.email)
    if apage.locator("#password").count() == 0:
        apage.click("[data-testid=email-submit]")
    if smtp:
        apage.wait_for_selector("text=Forgot password?", timeout=30000)
        apage.click("text=Forgot password?")
        apage.wait_for_url(re.compile(r"/reset"), timeout=15000)
    else:
        hint = apage.locator("text=Ask an admin").first
        hint.wait_for(timeout=30000)
        j.check("no SMTP: the sign-in page says to ask an admin for a reset link (no Forgot link)",
                "reset link" in (hint.get_attribute("title") or "") and apage.locator("text=Forgot password?").count() == 0,
                expect="“Ask an admin” with the reset-link tooltip", actual=hint.get_attribute("title"))
        j.shot(apage, "ask-an-admin")
        apage.goto(f"{env.web}/reset", wait_until="domcontentloaded")
    apage.wait_for_timeout(1000)
    j.shot(apage, "forgot-password")
    known = K.raw_fetch(apage, "POST", "/v1/auth/password/forgot", {"email": p.email}, csrf=False)
    unknown = K.raw_fetch(apage, "POST", "/v1/auth/password/forgot",
                          {"email": f"nobody-{int(time.time())}@example.test"}, csrf=False)
    j.check("API: forgot-password answers 202 with identical bodies for a known and an unknown address",
            known["status"] == unknown["status"] == 202 and known.get("body") == unknown.get("body"),
            expect="202 and 202, same body", actual=f"{known['status']} {short(known.get('body'))} | {unknown['status']} {short(unknown.get('body'))}")
    if smtp:
        apage.fill("#reset-email", p.email)
        apage.get_by_role("button", name=re.compile("Email me a link")).click()
        apage.wait_for_selector("[data-testid=reset-sent]", timeout=15000)
        j.check("uniform words after sending", "If an account exists" in K.body_text(apage),
                expect="“If an account exists…”", actual=K.body_text(apage)[:200])
    else:
        txt = K.body_text(apage)
        j.check("no SMTP: /reset says an admin issues the link (no form that pretends to send)",
                "admin" in txt.lower() and apage.locator("#reset-email").count() == 0 and "hit an error" not in txt,
                expect="admin hand-over words, no email form", actual=txt[:200],
                known="unassigned (new: /reset crashes server-side — emailSignInOffered() is a client function called from the server page)",
                repro=f"GET {env.web}/reset anonymous, no SMTP → “This page hit an error”; web log: Attempted to call emailSignInOffered() from the server")
    actx.close()

    # Two live sessions of the person.
    s1ctx, s1 = K.signed_in_page(env.browser, env.web, p, j)
    s2ctx, s2 = K.signed_in_page(env.browser, env.web, p, j)
    wrong = K.raw_fetch(s1, "POST", "/v1/auth/login", {"email": p.email, "password": "not-the-password-123"}, csrf=False)
    nobody = K.raw_fetch(s1, "POST", "/v1/auth/login", {"email": f"nobody-{int(time.time())}@example.test",
                                                         "password": "not-the-password-123"}, csrf=False)
    j.check("API: a wrong password answers 401 invalid_credentials, the same as an unknown address",
            wrong["status"] == nobody["status"] == 401 and wrong.get("body") == nobody.get("body") == {"error": "invalid_credentials"},
            expect='401 {"error": "invalid_credentials"} for both', actual=f"{wrong['status']} {short(wrong.get('body'))} | {nobody['status']} {short(nobody.get('body'))}",
            known="unassigned (new: auth.login.failed audit refuses its own details — identifier_hash() has no 'hmac:' prefix — so every failed sign-in is a 500 AuditWriteRefused)",
            repro="POST /v1/auth/login {email: any, password: wrong} → 500 internal_error; api log: AuditWriteRefused auth.login.failed: 'identifier' must be stored as identifier_hmac(...) (A79)")
    sid1 = (me(s1).get("body") or {}).get("session_id")
    sid2 = (me(s2).get("body") or {}).get("session_id")
    live = api(s1, "GET", "/v1/identity/me/sessions")
    j.check("two live sessions before the reset", len((live.get("body") or {}).get("sessions") or []) >= 2,
            expect=">= 2", actual=len((live.get("body") or {}).get("sessions") or []))

    # Without SMTP, the admin path: People → the person → a reset link, shown once.
    opage.goto(f"{env.web}/identity/people/{p.principal_id}", wait_until="domcontentloaded")
    opage.wait_for_timeout(2500)
    j.shot(opage, "person-page-actions")
    j.check("the person page offers the admin reset link the reset page promises",
            opage.get_by_role("button", name=re.compile("reset link|password", re.I)).count() > 0,
            expect="a 'Reset link' action on People › person (the /reset page sends people there)",
            actual="no such control", known="FX-C P1-17 (dead end: /reset says an admin can issue a link; People has no control)",
            repro=f"{env.web}/identity/people/{p.principal_id} as seed-owner")
    seq0 = audit_seq(opage)
    rl = K.stepped(opage, owner, "POST", f"/v1/identity/users/{p.principal_id}/reset-link", None, j)
    link = K.secret((rl.get("body") or {}).get("link") or "")
    j.check("API: the admin reset link is issued once to the authenticated admin", rl["status"] == 200 and "/reset#t=" in (link or ""),
            expect="200 with a /reset#t=… link", actual=f"{rl['status']} {short(rl.get('body'))}", fatal=True)

    rctx, rpage = K.new_page(env.browser, j)
    rpage.goto(link, wait_until="domcontentloaded")
    new_pw = K.strong_password("j4new")
    ui_ok = False
    try:
        rpage.wait_for_selector("#new-pw", timeout=20000)
        ui_ok = True
    except Exception:  # noqa: BLE001
        pass
    j.shot(rpage, "reset-link-page")
    j.check("the reset link opens the new-password form", ui_ok, expect="#new-pw form",
            actual=K.body_text(rpage, 200),
            known="unassigned (new: /reset crashes server-side — emailSignInOffered() called from the server)",
            repro=f"open the admin-issued {env.web}/reset#t=… link")
    if ui_ok:
        rpage.fill("#new-pw", new_pw)
        rpage.fill("#new-pw2", new_pw)
        rpage.get_by_role("button", name=re.compile("Set password")).click()
        rpage.wait_for_selector("text=Password changed", timeout=20000)
        j.shot(rpage, "password-changed")
        j.check("the page says the second factor is still needed", "second factor" in K.body_text(rpage),
                expect="“…your second factor is still needed”", actual=K.body_text(rpage)[:200])
    else:
        rpage.goto(f"{env.web}/login", wait_until="domcontentloaded")
        done = K.raw_fetch(rpage, "POST", "/v1/auth/password/reset",
                           {"token": K.token_of(link), "new_password": new_pw}, csrf=False)
        j.check("API: the reset (the page's own call) answers next: signin, no session", done["status"] == 200
                and (done.get("body") or {}).get("next") == "signin",
                expect='200 {"next": "signin"}', actual=f"{done['status']} {short(done.get('body'))}")
    for name, pg, sid in (("first", s1, sid1), ("second", s2, sid2)):
        r = K.raw_fetch(pg, "GET", "/v1/identity/me", csrf=False)
        row = env.stack.psql("SELECT coalesce(revoked_at::text, 'live') || ' ' || coalesce(revoked_reason, '') || ' replaced_by=' || coalesce(replaced_by, '-') "
                             "FROM identity_sessions WHERE id = :'sid'", {"sid": sid})
        j.check(f"API: the {name} other session is revoked", r["status"] == 401, expect="401",
                actual=f"{r['status']} (db: {row}; session {sid})")
    ev = audit_has(opage, "password.reset_completed", target=p.principal_id, since_seq=seq0)
    j.check("audit: password.reset_completed with sessions_revoked >= 2",
            bool(ev) and int(((ev.get("raw") or {}).get("details") or {}).get("sessions_revoked") or 0) >= 2,
            expect="event with sessions_revoked >= 2", actual=short(ev))
    again = K.raw_fetch(rpage, "POST", "/v1/auth/password/reset", {"token": K.token_of(link), "new_password": K.strong_password("j4again")}, csrf=False)
    j.check("API: the reset link works once", again["status"] == 400, expect="400 reset_invalid",
            actual=f"{again['status']} {short(again.get('body'))}")
    old = K.raw_fetch(rpage, "POST", "/v1/auth/login", {"email": p.email, "password": p.password}, csrf=False)
    j.check("API: the old password no longer signs in", old["status"] == 401, expect="401 invalid_credentials",
            actual=f"{old['status']} {short(old.get('body'))}", known="unassigned (the failed-sign-in 500: AuditWriteRefused, see above)")
    if old["status"] >= 500:
        rpage.wait_for_timeout(1500)      # the console's proxy answers 502 once after the API's 500
    p.password = new_pw
    fresh = K.raw_fetch(rpage, "POST", "/v1/auth/login", {"email": p.email, "password": new_pw}, csrf=False)
    if fresh["status"] == 502:
        j.note("the console answered 502 right after the API's 500; retried once")
        fresh = K.raw_fetch(rpage, "POST", "/v1/auth/login", {"email": p.email, "password": new_pw}, csrf=False)
    j.check("API: the new password signs in only as far as the second factor", fresh["status"] == 200
            and (fresh.get("body") or {}).get("next") == "mfa",
            expect='200 {"next": "mfa"}', actual=f"{fresh['status']} {short(fresh.get('body'))}")
    pre = K.raw_fetch(rpage, "GET", "/v1/identity/me", csrf=False)
    j.check("API: no session before the second factor", pre["status"] == 401, expect="401", actual=pre["status"])
    for c in (s1ctx, s2ctx, rctx):
        c.close()
    vctx, vpage = K.signed_in_page(env.browser, env.web, p, j)
    j.check("the new password + TOTP signs in through /login", me(vpage)["status"] == 200, expect="me 200",
            actual=me(vpage)["status"])
    vctx.close()
    octx.close()


# =================================================================================================
# J5 — lost MFA: recovery code; forced regeneration; admin reset; an Owner's reset needs an Owner
# =================================================================================================

@journey("J5", "Lost MFA: recovery code, forced regeneration, admin reset, Owner needs an Owner",
         "A recovery code signs in once and is spent; below 3 left BOSS forces regeneration; an Admin "
         "resets an Operator's MFA (step-up, sessions end, re-enrol next time); resetting an Owner "
         "needs another Owner.")
def j5(env: Env, j: Journey) -> None:
    owner, admin = env.owner, env.admin
    octx, opage = env.owner_page(j)
    p, _, _ = K.make_person(env.stack, env.browser, opage, owner, "operator", "j5", j)
    j.check("the person has 10 recovery codes", len(p.recovery_codes) == 10, expect="10",
            actual=len(p.recovery_codes), fatal=True)

    # A recovery code in the console.
    seq0 = audit_seq(opage)
    rctx, rpage = K.new_page(env.browser, j)
    K.login_form(rpage, env.web, p.email, p.password)
    rpage.wait_for_selector("text=Use a recovery code", timeout=30000)
    rpage.click("text=Use a recovery code")
    rpage.wait_for_selector("#rc", timeout=10000)
    j.shot(rpage, "recovery-code-field")
    rpage.fill("#rc", p.recovery_codes[0])
    rpage.get_by_role("button", name="Verify").click()
    rpage.wait_for_url(lambda u: "/login" not in u, timeout=30000)
    K.settle(rpage)
    j.shot(rpage, "signed-in-with-recovery-code")
    m = me(rpage)
    j.check("API: signed in; 9 codes left", m["status"] == 200
            and (m.get("body") or {}).get("sign_in", {}).get("recovery_codes_remaining") == 9,
            expect="me 200, recovery_codes_remaining 9", actual=f"{m['status']} {short((m.get('body') or {}).get('sign_in'))}")
    ev = audit_has(opage, "recovery_code.used", actor=p.principal_id, since_seq=seq0)
    j.check("audit: recovery_code.used", bool(ev), expect="event", actual=short(ev))
    rctx.close()
    spent = K.raw_fetch(opage, "GET", "/v1/identity/me", csrf=False)  # keep the owner page warm
    del spent

    # Spend codes down to 2 (API sign-ins), the last one through the console.
    last = {}
    for code in p.recovery_codes[1:7]:
        cctx, cpage = K.new_page(env.browser)
        cpage.goto(f"{env.web}/login", wait_until="domcontentloaded")
        K.raw_fetch(cpage, "POST", "/v1/auth/login", {"email": p.email, "password": p.password}, csrf=False)
        last = K.raw_fetch(cpage, "POST", "/v1/auth/mfa/recovery", {"code": code}, csrf=False)
        cctx.close()
    j.check("API: the 7th code answers 3 left, no forced regeneration yet",
            (last.get("body") or {}).get("recovery_codes_remaining") == 3
            and (last.get("body") or {}).get("must_regenerate_recovery_codes") is False,
            expect="remaining 3, must_regenerate false", actual=short(last.get("body")))
    reuse_ctx, reuse = K.new_page(env.browser)
    reuse.goto(f"{env.web}/login", wait_until="domcontentloaded")
    K.raw_fetch(reuse, "POST", "/v1/auth/login", {"email": p.email, "password": p.password}, csrf=False)
    again = K.raw_fetch(reuse, "POST", "/v1/auth/mfa/recovery", {"code": p.recovery_codes[0]}, csrf=False)
    j.check("API: a spent recovery code is refused", again["status"] == 401, expect="401 invalid_code",
            actual=f"{again['status']} {short(again.get('body'))}")
    reuse_ctx.close()
    ectx, epage = K.new_page(env.browser)
    epage.goto(f"{env.web}/login", wait_until="domcontentloaded")
    K.raw_fetch(epage, "POST", "/v1/auth/login", {"email": p.email, "password": p.password}, csrf=False)
    eighth = K.raw_fetch(epage, "POST", "/v1/auth/mfa/recovery", {"code": p.recovery_codes[7]}, csrf=False)
    body = eighth.get("body") or {}
    j.check("API: below 3 left the sign-in answer demands regeneration",
            body.get("recovery_codes_remaining") == 2 and body.get("must_regenerate_recovery_codes") is True,
            expect="remaining 2, must_regenerate_recovery_codes true", actual=short(body))
    ectx.close()
    fctx, fpage = K.new_page(env.browser, j)
    K.login_form(fpage, env.web, p.email, p.password)
    fpage.wait_for_selector("text=Use a recovery code", timeout=30000)
    fpage.click("text=Use a recovery code")
    fpage.fill("#rc", p.recovery_codes[8])
    fpage.get_by_role("button", name="Verify").click()
    fpage.wait_for_timeout(3000)
    K.settle(fpage)
    j.shot(fpage, "one-code-left")
    forced = ("/account" in fpage.url and "recovery" in K.body_text(fpage).lower()) or \
        fpage.locator("[data-testid=recovery-codes]").count() > 0
    j.check("the console forces regeneration (new codes shown / required before going on)", forced,
            expect="the person is taken to make new recovery codes", actual=f"landed at {fpage.url}",
            known="FX-A P2 (recovery-code forced regeneration)", repro=f"sign in {env.web}/login with a recovery code when 3 are left")
    fctx.close()

    # Admin resets the Operator's MFA in the console (step-up first).
    actx, apage = K.signed_in_page(env.browser, env.web, admin, j)
    K.age_my_session(env.stack, apage)
    live_ctx, live_page = K.signed_in_page(env.browser, env.web, p, j)
    seq1 = audit_seq(opage)
    apage.goto(f"{env.web}/identity/people/{p.principal_id}", wait_until="domcontentloaded")
    apage.wait_for_selector("[data-testid=person-reset-mfa]", timeout=30000)
    j.shot(apage, "person-page")
    apage.click("[data-testid=person-reset-mfa]")
    apage.wait_for_selector("[data-testid=confirm-mfa-go]", timeout=10000)
    j.shot(apage, "reset-mfa-confirm")
    apage.click("[data-testid=confirm-mfa-go]")
    sheet = K.answer_step_up_sheet(apage, admin, timeout_ms=8000)
    j.check("the reset asks for step-up first (Admin's MFA was stale)", sheet, expect="step-up sheet", actual=sheet)
    apage.wait_for_timeout(2500)
    j.shot(apage, "reset-mfa-done")
    f = api(opage, "GET", f"/v1/identity/users/{p.principal_id}")
    fac = (f.get("body") or {}).get("factors") or {}
    j.check("API: the Operator has no factors left", f["status"] == 200 and not fac.get("totp")
            and not fac.get("passkeys") and not fac.get("recovery_codes_remaining"),
            expect="totp false, 0 passkeys, 0 codes", actual=short(fac))
    gone = K.raw_fetch(live_page, "GET", "/v1/identity/me", csrf=False)
    j.check("API: the Operator's live session ended", gone["status"] == 401, expect="401", actual=gone["status"])
    ev = audit_has(opage, "user.mfa_reset", target=p.principal_id, since_seq=seq1)
    j.check("audit: user.mfa_reset by the Admin", bool(ev) and (ev.get("raw") or {}).get("actor_principal_id") == admin.principal_id,
            expect="event by seed-admin", actual=short(ev))
    nctx, npage = K.new_page(env.browser, j)
    npage.goto(f"{env.web}/login", wait_until="domcontentloaded")
    nxt = K.raw_fetch(npage, "POST", "/v1/auth/login", {"email": p.email, "password": p.password}, csrf=False)
    j.check("API: the next sign-in must enrol a factor again", nxt["status"] == 200
            and (nxt.get("body") or {}).get("next") in ("enrol", "done") and
            ((nxt.get("body") or {}).get("next") == "enrol" or (nxt.get("body") or {}).get("mfa_enrolment_required")),
            expect='next "enrol" (or a session flagged mfa_enrolment_required)', actual=f"{nxt['status']} {short(nxt.get('body'))}")
    nctx.close()
    live_ctx.close()

    # An Owner's MFA: an Admin may not reset it; another Owner may.
    op, _, _ = K.make_person(env.stack, env.browser, opage, owner, "owner", "j5owner", j)
    refused = K.stepped(apage, admin, "POST", f"/v1/identity/users/{op.principal_id}/reset-mfa", None, j)
    j.check("API: an Admin resetting an Owner's MFA is refused", refused["status"] == 403,
            expect="403 (needs owner:assign)", actual=f"{refused['status']} {short(refused.get('body'))}",
            repro=f"POST /v1/identity/users/<fxd owner>/reset-mfa as seed-admin")
    okr = K.stepped(opage, owner, "POST", f"/v1/identity/users/{op.principal_id}/reset-mfa", None, j)
    j.check("API: a second Owner may reset an Owner's MFA", okr["status"] == 200, expect="200",
            actual=f"{okr['status']} {short(okr.get('body'))}")
    # Leave no extra Owner behind on the shared stack.
    d1 = K.stepped(opage, owner, "POST", f"/v1/identity/users/{op.principal_id}/disable", None, j)
    d2 = K.stepped(opage, owner, "DELETE", f"/v1/identity/users/{op.principal_id}", None, j)
    j.note(f"cleanup: the fxd Owner disabled {d1['status']}, deleted {d2['status']}")
    actx.close()
    octx.close()


# =================================================================================================
# J6 — sessions on two browsers
# =================================================================================================

@journey("J6", "Sessions on two browsers: end one from My account",
         "My account lists both sessions; ending the other one removes it, and that browser is "
         "refused on its very next request and sent to sign in.")
def j6(env: Env, j: Journey) -> None:
    owner = env.owner
    octx, opage = env.owner_page(j)
    p, _, _ = K.make_person(env.stack, env.browser, opage, owner, "operator", "j6", j)
    actx, apage = K.signed_in_page(env.browser, env.web, p, j)
    bctx, bpage = K.signed_in_page(env.browser, env.web, p, j)
    b_sid = (me(bpage).get("body") or {}).get("session_id")
    seq0 = audit_seq(opage)
    apage.goto(f"{env.web}/account/sessions", wait_until="domcontentloaded")
    apage.wait_for_selector("[data-testid=my-sessions] li", timeout=30000)
    apage.wait_for_timeout(1000)
    rows = apage.locator("[data-testid=my-sessions] li")
    j.check("My account lists both sessions, this one marked", rows.count() == 2
            and apage.locator("[data-testid=my-sessions]").get_by_text("this one").count() == 1,
            expect="2 rows, one 'this one'", actual=f"{rows.count()} rows")
    j.shot(apage, "two-sessions")
    apage.locator("[data-testid=my-sessions] li").get_by_role("button", name=re.compile(r"^End")).first.click()
    K.wait_for(lambda: apage.locator("[data-testid=my-sessions] li").count() == 1, 10)
    j.check("the ended session disappears from the list", apage.locator("[data-testid=my-sessions] li").count() == 1,
            expect="1 row", actual=apage.locator("[data-testid=my-sessions] li").count())
    j.shot(apage, "one-session-left")
    r = K.raw_fetch(bpage, "GET", "/v1/identity/me", csrf=False)
    j.check("API: the other browser's next request is refused", r["status"] == 401, expect="401", actual=r["status"])
    bpage.goto(f"{env.web}/identity", wait_until="domcontentloaded")
    bpage.wait_for_timeout(2500)
    j.check("that browser is sent to sign in", "/login" in bpage.url or "/signin" in bpage.url,
            expect="/login", actual=bpage.url)
    j.shot(bpage, "other-browser-signed-out")
    still = me(apage)
    j.check("API: this browser is still signed in", still["status"] == 200, expect="200", actual=still["status"])
    ev = audit_has(opage, "session.revoked", target=b_sid, since_seq=seq0)
    j.check("audit: session.revoked names the ended session", bool(ev), expect="event targeting that session",
            actual=short(ev))
    for c in (actx, bctx, octx):
        c.close()


# =================================================================================================
# J11 — the Viewer walk (the journey's steps; the full click-every-control walk is role_walk.py)
# =================================================================================================

@journey("J11", "The Viewer walk",
         "A Viewer sees only areas they can open (Identity, via directory:read); Invite is disabled "
         "naming user:invite; People answers Access denied naming user:read and the Admins; the API "
         "refuses the same reads and writes.")
def j11(env: Env, j: Journey) -> None:
    viewer = env.A["viewer"]
    vctx, vpage = K.signed_in_page(env.browser, env.web, viewer, j)
    vpage.goto(f"{env.web}/", wait_until="domcontentloaded")
    K.settle(vpage)
    j.shot(vpage, "viewer-home")
    held = perms(vpage)
    j.check("API: the Viewer holds directory:read and not user:read / user:invite",
            "directory:read" in held and "user:read" not in held and "user:invite" not in held,
            expect="directory:read only", actual=sorted(p for p in held if p.split(":")[0] in ("directory", "user")))
    items = [t.strip() for t in vpage.locator("nav.bx-nav .bx-nav-item").all_inner_texts() if t.strip()]
    items = [re.sub(r"\s+\d+$", "", t) for t in items]
    j.note(f"rail: {items}")
    j.check("the rail offers Identity", "Identity" in items, expect="an Identity entry", actual=items)
    denied = []
    for text in items:
        vpage.goto(f"{env.web}/", wait_until="domcontentloaded")
        K.settle(vpage, 6000)
        btn = vpage.locator("nav.bx-nav .bx-nav-item").filter(has_text=text).first
        try:
            btn.click(timeout=5000)
        except Exception:  # noqa: BLE001
            continue
        K.settle(vpage, 6000)
        if vpage.locator("[data-testid=access-denied], [data-testid=page-denied]").count():
            denied.append(f"{text} ({vpage.url})")
    j.check("every area the rail offers the Viewer opens (no Access denied behind a rail entry)", not denied,
            expect="no rail entry leads to Access denied", actual=denied)
    vpage.goto(f"{env.web}/", wait_until="domcontentloaded")
    K.settle(vpage)
    nb = vpage.get_by_role("button", name=re.compile("New build"))
    if "code_task:create" not in held and nb.count():
        b = nb.first
        j.check("“New build” is disabled for a Viewer (no code_task:create)",
                b.is_disabled() or b.get_attribute("aria-disabled") == "true",
                expect="disabled with the permission tooltip",
                actual=f"enabled (title={b.get_attribute('title')!r})", known="FX-C P1-18 (pre-grey actions the person cannot take)",
                repro=f"{env.web}/ as seed-viewer: top bar “+ New build”")
    vpage.goto(f"{env.web}/identity", wait_until="domcontentloaded")
    vpage.wait_for_selector("[data-testid=overview-invite]", timeout=30000)
    j.shot(vpage, "viewer-identity-overview")
    inv = vpage.locator("[data-testid=overview-invite]").first
    title = inv.get_attribute("title") or ""
    j.check("Invite is disabled and names user:invite and who to ask",
            (inv.get_attribute("aria-disabled") == "true" or inv.is_disabled()) and "user:invite" in title,
            expect="aria-disabled, title “Needs user:invite · ask an Admin”", actual=f"aria-disabled={inv.get_attribute('aria-disabled')} title={title!r}")
    vpage.goto(f"{env.web}/identity/people", wait_until="domcontentloaded")
    vpage.wait_for_selector("[data-testid=access-denied], [data-testid=page-denied]", timeout=30000)
    vpage.wait_for_timeout(1000)
    j.shot(vpage, "viewer-people-denied")
    txt = K.body_text(vpage)
    j.check("People answers Access denied naming the page and user:read",
            "People" in txt and "user:read" in txt and ("can't open" in txt or "can’t open" in txt),
            expect="“You can't open People” · Needs user:read", actual=txt[:300])
    j.check("the denial names who can grant it and offers Request access",
            vpage.locator("[data-testid=access-denied-grantors]").count() > 0
            and vpage.locator("[data-testid=request-access]").count() > 0,
            expect="grantors listed + Request access", actual=txt[:300])
    r = api(vpage, "GET", "/v1/identity/users")
    j.check("API: GET /v1/identity/users is 403 naming user:read", r["status"] == 403
            and (r.get("body") or {}).get("permission") == "user:read",
            expect='403 {"permission": "user:read"}', actual=f"{r['status']} {short(r.get('body'))}")
    w = api(vpage, "POST", "/v1/identity/users", {"email": "fxd-j11@example.test",
                                                    "bindings": [{"role_key": "viewer", "scope_type": "org"}], "group_ids": []})
    j.check("API: inviting is 403", w["status"] == 403, expect="403", actual=f"{w['status']} {short(w.get('body'))}")
    vctx.close()


# =================================================================================================
# J12 — role editor: clone Operator, remove deployment:create, impact preview, step-up, epoch
# =================================================================================================

DEPLOY_PROBE = ("POST", "/v1/deployments/stack/plan", {"project_id": PROJECT, "target_id": "dtg_fxd_none",
                                                       "repository": "fxd/none", "ref": "main"})


def _deploy_refusal(page: Any) -> dict[str, Any]:
    m, path, body = DEPLOY_PROBE
    return api(page, m, path, body)


@journey("J12", "Role editor: clone Operator, remove deployment:create, save with step-up",
         "Cloning a built-in opens an editable copy; turning off a permission shows the diff and an "
         "impact preview naming the people, pages and API operations; saving needs step-up and "
         "bumps the version; the epoch makes the change bite at once for a holder (the API refuses "
         "deployment:create on their next request).")
def j12(env: Env, j: Journey) -> None:
    admin, owner = env.admin, env.owner
    actx, apage = K.signed_in_page(env.browser, env.web, admin, j)
    apage.goto(f"{env.web}/identity/roles", wait_until="domcontentloaded")
    apage.wait_for_selector("[data-testid=clone-operator]", timeout=30000)
    j.shot(apage, "roles")
    apage.click("[data-testid=clone-operator]")
    K.answer_step_up_sheet(apage, admin, timeout_ms=4000)
    apage.wait_for_url(re.compile(r"/identity/roles/arl_"), timeout=30000)
    K.settle(apage)
    role_id = apage.url.rstrip("/").split("/")[-1].split("?")[0]
    role = api(apage, "GET", f"/v1/identity/access-roles/{role_id}").get("body") or {}
    j.check("a custom copy of Operator opens in the editor", role.get("cloned_from") in ("arl_operator", "operator")
            and not role.get("builtin") and "deployment:create" in (role.get("permissions") or []),
            expect="custom role, cloned_from operator, holding deployment:create",
            actual=f"{role_id} cloned_from={role.get('cloned_from')} builtin={role.get('builtin')} v{role.get('version')}")
    name = f"fxd-j12-{time.strftime('%H%M%S')}"
    ren = K.stepped(apage, admin, "PATCH", f"/v1/identity/access-roles/{role_id}", {"name": name}, j)
    j.check("rename the copy (fxd-j12-*)", ren["status"] == 200, expect="200", actual=f"{ren['status']} {short(ren.get('body'))}")
    env.shared.setdefault("_cleanup", []).append(lambda: _drop_role(env, role_id))
    version0 = int((api(apage, "GET", f"/v1/identity/access-roles/{role_id}").get("body") or {}).get("version") or 0)

    # A holder of the copy, signed in.
    octx, opage = env.owner_page(j)
    holder, _, _ = K.make_person(env.stack, env.browser, opage, owner, role["key"], "j12", j)
    hctx, hpage = K.signed_in_page(env.browser, env.web, holder, j)
    before = perms(hpage, PROJECT)
    probe0 = _deploy_refusal(hpage)
    epoch0 = int((api(hpage, "GET", "/v1/identity/me/permissions").get("body") or {}).get("epoch") or 0)
    j.check("the holder can deploy before the change", "deployment:create" in before
            and not (probe0["status"] == 403 and (probe0.get("body") or {}).get("permission") == "deployment:create"),
            expect="deployment:create held; the deploy route is not refused for it",
            actual=f"held={'deployment:create' in before} probe={probe0['status']} {short(probe0.get('body'), 160)}")

    # Turn deployment:create off in the editor.
    apage.goto(f"{env.web}/identity/roles/{role_id}", wait_until="domcontentloaded")
    apage.wait_for_selector("[data-testid=matrix-filter]", timeout=30000)
    apage.fill("[data-testid=matrix-filter]", "deployment")
    tog = apage.locator("button.idn-tg[data-perm='deployment:create']")
    tog.first.wait_for(timeout=10000)
    tog.first.click()
    apage.wait_for_selector("[data-testid=diff]", timeout=10000)
    K.wait_for(lambda: "Affects" in (apage.locator("[data-testid=impact]").inner_text() or ""), 15)
    diff = apage.locator("[data-testid=diff]").inner_text()
    impact = apage.locator("[data-testid=impact]").inner_text()
    j.shot(apage, "diff-and-impact")
    j.check("Changes shows −deployment:create", "deployment:create" in diff and "−" in diff,
            expect="−deployment:create", actual=diff[:200])
    j.check("the impact preview names the holder and counts people", "Affects 1 person" in impact
            and holder.name in impact, expect=f"Affects 1 person · {holder.name}", actual=impact[:300])
    lost = re.search(r"[−-](\d+)\s*\n?\s*API operations", impact)
    j.check("the impact preview counts the API operations lost", bool(lost) and int(lost.group(1)) > 0,
            expect="−N API operations, N > 0", actual=impact[:400].replace("\n", " · "),
            known="FX-C P1-11/12/13 (impact preview counts)", repro=f"{env.web}/identity/roles/<clone of Operator>: turn off deployment:create")
    pv = K.stepped(apage, admin, "POST", f"/v1/identity/access-roles/{role_id}/preview",
                   {"permissions": [x for x in role.get("permissions", []) if x != "deployment:create"]}, j)
    pvb = pv.get("body") or {}
    people = [p.get("id") for p in (pvb.get("affected") or {}).get("people", [])] + list(pvb.get("affected_principals") or [])
    j.check("API: the preview names the holder", pv["status"] == 200 and holder.principal_id in people,
            expect="the holder among the affected", actual=short(pvb, 300))
    j.check("API: the preview counts pages/routes/tools lost (§10 shape)", int((pvb.get("routes") or {}).get("lost") or 0) > 0,
            expect="routes.lost > 0 (and pages±, tools±)", actual=f"keys={sorted(pvb)}",
            known="FX-C P1-11/12/13 (impact preview counts)", repro=f"POST /v1/identity/access-roles/<id>/preview as seed-admin")

    # Save: stale MFA first, so the step-up sheet must appear.
    K.age_my_session(env.stack, apage)
    seq0 = audit_seq(opage)
    apage.click("[data-testid=save]")
    apage.wait_for_timeout(1000)
    if apage.locator("[data-testid=confirm-save]").count():
        apage.fill("#confirm-name", name)
        apage.click("[data-testid=confirm-save]")
    sheet = K.answer_step_up_sheet(apage, admin, timeout_ms=8000)
    j.check("saving asks for step-up", sheet, expect="the step-up sheet", actual=sheet)
    K.wait_for(lambda: apage.locator("[data-testid=unsaved]").count() == 0, 15)
    j.shot(apage, "saved")
    after_role = api(apage, "GET", f"/v1/identity/access-roles/{role_id}").get("body") or {}
    j.check("API: version +1 and deployment:create gone", int(after_role.get("version") or 0) == version0 + 1
            and "deployment:create" not in (after_role.get("permissions") or []),
            expect=f"v{version0 + 1} without deployment:create", actual=f"v{after_role.get('version')}")
    after = perms(hpage, PROJECT)
    epoch1 = int((api(hpage, "GET", "/v1/identity/me/permissions").get("body") or {}).get("epoch") or 0)
    probe1 = _deploy_refusal(hpage)
    j.check("the epoch moved and the holder lost deployment:create on the next request",
            epoch1 > epoch0 and "deployment:create" not in after,
            expect="epoch increased; permission gone", actual=f"epoch {epoch0}→{epoch1}; held={'deployment:create' in after}")
    j.check("API: the holder's deploy is now refused naming deployment:create",
            probe1["status"] == 403 and (probe1.get("body") or {}).get("permission") == "deployment:create",
            expect='403 {"permission": "deployment:create"}', actual=f"{probe1['status']} {short(probe1.get('body'))}")
    ev = audit_has(opage, "access_role.updated", target=role_id, since_seq=seq0) or \
        audit_has(opage, "role.updated", target=role_id, since_seq=seq0)
    j.check("audit: the role change is in the security log", bool(ev), expect="access_role.updated (or role.updated) for the role",
            actual=short(ev) or [e.get("event_type") for e in K.audit_events(opage, limit="15")])
    for c in (hctx, actx, octx):
        c.close()


def _drop_role(env: Env, role_id: str) -> None:
    """Cleanup: unbind and delete a role this run made."""
    h, page = env.owner_page()
    try:
        for b in (api(page, "GET", "/v1/identity/role-bindings").get("body") or {}).get("bindings", []):
            if b.get("role_id") == role_id:
                K.stepped(page, env.owner, "DELETE", f"/v1/identity/role-bindings/{b['id']}")
        K.stepped(page, env.owner, "DELETE", f"/v1/identity/access-roles/{role_id}")
    finally:
        h.close()


# =================================================================================================
# J13 — service account key: shown once, never again; rotate with overlap; old key stops
# =================================================================================================

@journey("J13", "Service account key: shown once, rotate with overlap",
         "A key is shown exactly once in a dialog that cannot be dismissed until stored; afterwards "
         "only its id is ever shown (page and API); it works within its scopes only; rotation with a "
         "1 h overlap keeps both keys working, and the old one stops when the overlap ends.")
def j13(env: Env, j: Journey) -> None:
    admin = env.admin
    actx, apage = K.signed_in_page(env.browser, env.web, admin, j)
    for x in (api(apage, "GET", "/v1/identity/service-accounts").get("body") or {}).get("service_accounts") or []:
        if str(x.get("name") or x.get("display_name") or "").startswith("fxd-j13-"):
            _drop_sa(env, x["id"])          # an earlier run's leftovers
    name = f"fxd-j13-ci-{time.strftime('%H%M%S')}"
    apage.goto(f"{env.web}/identity/service-accounts", wait_until="domcontentloaded")
    apage.get_by_role("button", name=re.compile(r"^\s*New( service account)?\s*$|New account", re.I)).first.click(timeout=30000)
    apage.wait_for_selector("#sa-name", timeout=10000)
    apage.wait_for_timeout(500)
    apage.fill("#sa-name", name)
    sheet_radios = apage.locator("[data-testid=new-sa]").get_by_role("radio")
    reach = sheet_radios.filter(has_text=re.compile("fxd-j3-out-of-reach"))
    if reach.count():
        r0 = reach.first
        j.check("the new-account sheet greys out roles the Admin does not fully hold (as Invite does)",
                r0.get_attribute("aria-disabled") == "true",
                expect="aria-disabled with “You don't hold all of it”", actual=f"aria-disabled={r0.get_attribute('aria-disabled')} title={r0.get_attribute('title')!r}",
                known="unassigned (new: NewAccount sheet offers every role; the invite sheet applies the grant limit)",
                repro=f"{env.web}/identity/service-accounts → New, as seed-admin: an Owner-only custom role is selectable")
    sheet_radios.filter(has_text=re.compile("^Operator")).first.click()
    j.shot(apage, "new-service-account")
    apage.locator("[data-testid=new-sa] .idn-mf .idn-btn.primary").click()
    K.answer_step_up_sheet(apage, admin, timeout_ms=5000)
    K.wait_for(lambda: apage.locator("[data-testid=new-sa]").count() == 0, 15)
    sas = (api(apage, "GET", "/v1/identity/service-accounts").get("body") or {}).get("service_accounts") or []
    sa = next((x for x in sas if name in (x.get("name"), x.get("display_name"))), None)
    detail = api(apage, "GET", f"/v1/identity/service-accounts/{sa['id']}").get("body") if sa else {}
    j.check("the service account exists, bound to Operator", bool(sa) and "operator" in ((detail or {}).get("access_roles") or []),
            expect="listed, access_roles [operator]", actual=short(detail), fatal=True)
    sa_id = sa["id"]
    env.shared.setdefault("_cleanup", []).append(
        lambda: _drop_sa(env, sa_id))
    apage.goto(f"{env.web}/identity/service-accounts?sa={sa_id}", wait_until="domcontentloaded")
    apage.wait_for_selector("[data-testid=new-key]", timeout=30000)
    apage.click("[data-testid=new-key]")
    apage.wait_for_selector("#key-name", timeout=10000)
    apage.fill("#key-name", "fxd-j13 deploy")
    apage.locator("[data-testid=key-form]").get_by_role("button", name="CI deploy").click()
    scopes = apage.locator("[data-testid=scopes]").inner_text()
    j.check("the CI deploy preset picks deployment:create, deployment:read, code_task:read",
            all(x in scopes for x in ("deployment:create", "deployment:read", "code_task:read")),
            expect="the three CI scopes", actual=scopes[:200])
    j.shot(apage, "key-form")
    apage.click("[data-testid=create-key]")
    K.answer_step_up_sheet(apage, admin, timeout_ms=5000)
    apage.wait_for_selector("[data-testid=secret]", timeout=20000)
    key1 = K.secret(apage.locator("[data-testid=secret]").inner_text().strip()) or ""
    j.check("the shown-once dialog shows a boss_sak_ key", key1.startswith("boss_sak_"), expect="boss_sak_…",
            actual="boss_sak_<redacted>" if key1.startswith("boss_sak_") else "something else")
    j.check("Done is disabled until “I've stored this key”", apage.locator("[data-testid=done]").is_disabled(),
            expect="disabled", actual=apage.locator("[data-testid=done]").is_disabled())
    apage.keyboard.press("Escape")
    apage.wait_for_timeout(600)
    j.check("Escape does not close it", apage.locator("[data-testid=secret]").count() == 1,
            expect="still open", actual=apage.locator("[data-testid=secret]").count())
    j.shot(apage, "shown-once")
    apage.check("[data-testid=stored]")
    apage.click("[data-testid=done]")
    apage.wait_for_timeout(1000)
    page_html = apage.content()
    chunks = [key1[i:i + 10] for i in range(9, len(key1) - 10, 5)]
    j.check("after closing, the page holds no part of the secret", not any(c in page_html for c in chunks),
            expect="no 10-char piece of the key anywhere in the page", actual="found" if any(c in page_html for c in chunks) else "none")
    keys = api(apage, "GET", f"/v1/identity/service-accounts/{sa_id}/keys")
    kj = json.dumps(keys.get("body"))
    rows = (keys.get("body") or {}).get("keys") or []
    j.check("API: the keys list carries ids only, never the secret or its hash",
            keys["status"] == 200 and not any(c in kj for c in chunks) and "token_hash" not in kj and len(rows) == 1
            and str(rows[0].get("id", "")).startswith("key_"),
            expect="one key_… row, no secret material", actual=f"{keys['status']} ids={[r.get('id') for r in rows]}")
    kid1 = rows[0]["id"] if rows else ""
    j.check("the keys table shows the key id", kid1 and apage.locator(f"tr[data-key='{kid1}']").count() == 1,
            expect=f"row {kid1}", actual=apage.locator("tr[data-key]").count())

    # Use it: inside its scopes, then outside.
    st, _ = env.stack.bearer("GET", f"/v1/deployment-targets?project_id={PROJECT}", key1)
    j.check("the key reads deployments (deployment:read)", st == 200, expect="200", actual=st)
    st2, b2 = env.stack.bearer("GET", "/v1/identity/users", key1)
    j.check("the key is refused outside its scopes (user:read)", st2 == 403, expect="403", actual=f"{st2} {short(b2, 160)}")
    st3, b3 = env.stack.bearer("GET", f"/v1/projects?project_id={PROJECT}", key1)
    j.note(f"key on /v1/projects (project:read, not in scopes): {st3}")

    # Rotate with a 1 h overlap, in the console.
    apage.locator(f"tr[data-key='{kid1}']").get_by_role("button", name=re.compile("Rotate")).click()
    apage.wait_for_selector("[data-testid=key-form]", timeout=10000)
    apage.locator("[data-testid=key-form]").get_by_role("button", name="1 h").click()
    j.shot(apage, "rotate-form")
    apage.click("[data-testid=create-key]")
    K.answer_step_up_sheet(apage, admin, timeout_ms=5000)
    apage.wait_for_selector("[data-testid=secret]", timeout=20000)
    key2 = K.secret(apage.locator("[data-testid=secret]").inner_text().strip()) or ""
    apage.check("[data-testid=stored]")
    apage.click("[data-testid=done]")
    apage.wait_for_timeout(1500)
    j.shot(apage, "both-keys-during-overlap")
    rows = (api(apage, "GET", f"/v1/identity/service-accounts/{sa_id}/keys").get("body") or {}).get("keys") or []
    old = next((r for r in rows if r.get("id") == kid1), {})
    j.check("API: the old key is in grace (about 1 h), the new one active", len(rows) == 2
            and old.get("status") == "grace" and old.get("grace_until"),
            expect="2 keys, old status grace", actual=short([(r.get("id"), r.get("status"), r.get("grace_until")) for r in rows]))
    j.check("the table shows the old key's grace", "grace" in apage.locator(f"tr[data-key='{kid1}']").inner_text(),
            expect="a ‘grace 1h’ pill", actual=apage.locator(f"tr[data-key='{kid1}']").inner_text()[:120])
    a1, _ = env.stack.bearer("GET", f"/v1/deployment-targets?project_id={PROJECT}", key1)
    a2, _ = env.stack.bearer("GET", f"/v1/deployment-targets?project_id={PROJECT}", key2)
    j.check("both keys work during the overlap", a1 == 200 and a2 == 200, expect="200 and 200", actual=f"{a1} {a2}")
    # The overlap ends (moved to the past in the database: a test cannot wait an hour).
    env.stack.psql("UPDATE identity_api_credentials SET grace_until = now() - interval '1 second' WHERE id = :'kid'",
                   {"kid": kid1})
    b1, _ = env.stack.bearer("GET", f"/v1/deployment-targets?project_id={PROJECT}", key1)
    b2_, _ = env.stack.bearer("GET", f"/v1/deployment-targets?project_id={PROJECT}", key2)
    j.check("after the overlap the old key is refused, the new one works", b1 == 401 and b2_ == 200,
            expect="401 and 200", actual=f"{b1} {b2_}")
    actx.close()


def _drop_sa(env: Env, sa_id: str) -> None:
    h, page = env.owner_page()
    try:
        K.stepped(page, env.owner, "DELETE", f"/v1/identity/service-accounts/{sa_id}")
    finally:
        h.close()


# =================================================================================================
# J15 — audit: filter by a grant, the event drawer, export with step-up, verify the chain
# =================================================================================================

@journey("J15", "Audit: filter by grant, drawer, export (step-up), verify",
         "The Auditor filters the security log by a grant, opens an event's drawer (chain, why, "
         "redacted JSON, hashes), export demands step-up first, and Verify reports the hash chain intact.")
def j15(env: Env, j: Journey) -> None:
    auditor = env.A["auditor"]
    xctx, xpage = K.signed_in_page(env.browser, env.web, auditor, j)
    grant = env.shared.get("grant_id")
    if not grant:
        h, opage = env.owner_page(j)
        ds = (api(opage, "GET", "/v1/identity/delegations").get("body") or {}).get("delegations") or []
        grant = ds[0]["id"] if ds else None
        h.close()
    j.note(f"grant used for the filter: {grant}")
    url = f"{env.web}/identity/audit" + (f"?grant={grant}" if grant else "")
    xpage.goto(url, wait_until="domcontentloaded")
    xpage.wait_for_selector("[data-testid=audit-table]", timeout=30000)
    K.settle(xpage)
    j.shot(xpage, "audit-filtered")
    if grant:
        evs = K.audit_events(xpage, grant=grant, limit="100")
        j.check("API: the grant filter returns only that grant's events", bool(evs)
                and all((e.get("grant_id") or (e.get("raw") or {}).get("grant_id")) == grant for e in evs),
                expect="events, each with this grant_id", actual=f"{len(evs)} events; grants={sorted({e.get('grant_id') for e in evs})[:5]}")
    rows = xpage.locator("[data-testid^=event-]")
    j.check("the table lists events", rows.count() > 0, expect="> 0 rows", actual=rows.count())
    denied = xpage.locator("[data-testid^=event-]").filter(has_text=re.compile("denied", re.I))
    (denied.first if denied.count() else rows.first).click()
    xpage.wait_for_selector("[data-testid=audit-drawer]", timeout=10000)
    xpage.wait_for_timeout(800)
    drawer = xpage.locator("[data-testid=audit-drawer]").inner_text()
    j.shot(xpage, "event-drawer")
    j.check("the drawer shows the chain or why rows, the JSON and the integrity hashes",
            (xpage.locator("[data-testid=drawer-chain], [data-testid=drawer-why]").count() > 0)
            and ("hash" in drawer.lower()),
            expect="chain/why + hashes", actual=drawer[:300])
    xpage.keyboard.press("Escape")

    # Export: stale MFA → the step-up sheet before anything is exported.
    K.age_my_session(env.stack, xpage)
    stale = api(xpage, "POST", "/v1/identity/audit/export", {})
    j.check("API: export with stale MFA answers 401 step_up_required",
            stale["status"] == 401 and (stale.get("body") or {}).get("error") == "step_up_required",
            expect="401 step_up_required", actual=f"{stale['status']} {short(stale.get('body'), 160)}")
    h, opage = env.owner_page(j)
    seq0 = audit_seq(opage)
    xpage.goto(url, wait_until="domcontentloaded")
    xpage.wait_for_selector("[data-testid=export-audit]", timeout=30000)
    K.settle(xpage)
    with xpage.expect_download(timeout=30000) as dl:
        xpage.click("[data-testid=export-audit]")
        sheet = K.answer_step_up_sheet(xpage, auditor, timeout_ms=8000)
    j.check("export opens the step-up sheet first", sheet, expect="step-up sheet", actual=sheet)
    try:
        d = dl.value
        j.check("the export downloads a file", bool(d.suggested_filename), expect="a file", actual=d.suggested_filename)
    except Exception as exc:  # noqa: BLE001
        j.check("the export downloads a file", False, expect="a file", actual=f"{type(exc).__name__}")
    ev = audit_has(opage, "audit.exported", actor=auditor.principal_id, since_seq=seq0)
    j.check("audit: audit.exported by the Auditor", bool(ev), expect="event", actual=short(ev))

    # Verify the chain.
    xpage.goto(f"{env.web}/identity/audit", wait_until="domcontentloaded")
    xpage.wait_for_selector("[data-testid=verify-chain]", timeout=30000)
    K.settle(xpage)
    xpage.click("[data-testid=verify-chain]")
    K.answer_step_up_sheet(xpage, auditor, timeout_ms=3000)
    xpage.wait_for_timeout(3000)
    status = xpage.locator("[data-testid=chain-status]").inner_text() if xpage.locator("[data-testid=chain-status]").count() else ""
    j.shot(xpage, "chain-verified")
    v = api(xpage, "POST", "/v1/identity/audit/verify", {})
    vb = v.get("body") or {}
    j.check("API: verify says the chain is intact", v["status"] == 200 and (vb.get("ok") is True or vb.get("verified") is True
                                                                           or vb.get("status") in ("ok", "verified")),
            expect="200 ok", actual=f"{v['status']} {short(vb, 300)}")
    j.check("the “Chain verified” pill shows it", "verified" in status.lower(), expect="Chain verified", actual=status[:120])
    h.close()
    xctx.close()


# =================================================================================================
# J8 — JIT consent for a build, then the lineage
# =================================================================================================

@journey("J8", "JIT consent for a build, then the lineage",
         "A person's build needs authority beyond their standing grants: the start answers 409 "
         "consent_required and the console's consent sheet asks; choosing “for this build only” "
         "creates a task-bound grant no wider than the person holds, the run carries it, and the "
         "lineage page shows the chain person → agents with narrowing scopes.")
def j8(env: Env, j: Journey) -> None:
    # Phase A, the main stack: the consent sheet, and the D28 guard (a multi-person install checks the
    # person's OWN GitHub access; a test person cannot link GitHub there — the OAuth goes to github.com).
    main = Target(env.web, env.stack, env.owner, lambda: env.owner_page(j))
    granted = _j8_on(env, j, main, phase="main")
    if granted:
        return
    # Phase B, the second stack (no GitHub repo configured, so no GitHub resource in the grant).
    s2, s2owner, _ = _s2_owner(env, j)
    _j8_on(env, j, Target(s2.web, s2.stack, s2owner, lambda: _s2_owner_page(env)), phase="stack2")


class Target:
    def __init__(self, web: str, stack: K.Stack, owner: Any, owner_page: Callable[[], tuple[Any, Any]]):
        self.web, self.stack, self.owner, self.owner_page = web, stack, owner, owner_page


def _s2_owner_page(env: Env) -> tuple[Any, Any]:
    ctx = env.shared["s2_owner"][1]
    page = ctx.new_page()
    page.goto(f"{env.shared['_s2'].web}/identity", wait_until="domcontentloaded")
    K.settle(page, 8000)
    return K.PageOnly(page), page


def _j8_on(env: Env, j: Journey, T: Target, *, phase: str) -> bool:
    owner = T.owner
    h, opage = T.owner_page()
    p, pctx, ppage = K.make_person(T.stack, env.browser, opage, owner, "operator", "j8", j, keep_open=True)
    held = perms(ppage, PROJECT)
    # The person's own GitHub (multi-person installs check the person's repo access, D28).
    con = K.stepped(ppage, p, "POST", "/v1/identity/me/connections", {"provider": "github"}, j)
    redirect = (con.get("body") or {}).get("redirect")
    j.note(f"connect GitHub: {con['status']} redirect={'yes' if redirect else 'no'} {short(con.get('body'), 160) if not redirect else ''}")
    if redirect:
        try:
            ppage.goto(redirect, wait_until="domcontentloaded")
            ppage.wait_for_url(re.compile(r"/account/agents"), timeout=30000)
        except Exception as exc:  # noqa: BLE001
            j.note(f"GitHub connect did not come back: {type(exc).__name__} at {ppage.url.split('?')[0]}")
        j.shot(ppage, "github-connected")
        j.note(f"after connect: {ppage.url.split('#')[0][-120:]}")
    before = {g["id"] for g in (api(ppage, "GET", "/v1/identity/me/delegations").get("body") or {}).get("delegations", [])}
    ppage.goto(f"{T.web}/?org=org_boss&area=workflows", wait_until="domcontentloaded")
    K.settle(ppage)
    newp = ppage.get_by_role("button", name=re.compile(r"New project"))
    if newp.count():
        newp.first.click()
        ppage.wait_for_timeout(1500)
    ppage.get_by_text("Fast fix", exact=True).first.click(timeout=20000)
    ppage.locator("textarea").first.fill(f"FX-D J8 {int(time.time())}: fix the typo in the README heading")
    anyway = ppage.get_by_text(re.compile("Start anyway"))
    if anyway.count():
        anyway.first.click()
    j.shot(ppage, "build-form")
    starts: list[Any] = []
    ppage.on("response", lambda r: r.request.method == "POST" and ("workflow" in r.url or "delivery" in r.url)
             and starts.append((r.status, r.url.split("?")[0][-60:])))
    ppage.get_by_role("button", name=re.compile("Start the project")).click()
    ppage.wait_for_selector("[data-testid=consent-durations]", timeout=30000)
    ppage.wait_for_timeout(800)
    j.shot(ppage, "consent-sheet")
    j.check("API: the start answered 409 consent_required", any(st == 409 for st, _ in starts),
            expect="a 409 on the start", actual=starts[-4:])
    sheet = K.body_text(ppage)
    offers = [b.inner_text().strip() for b in ppage.locator("[data-testid=consent-durations] button").all()]
    j.check("the consent sheet names the agents and offers “for this build only”", "act for you" in sheet.lower() or "Let " in sheet,
            expect="“Let … act for you?” + durations", actual=f"offers={offers}")
    grants_posted: list[Any] = []

    def _grant_resp(r: Any) -> None:
        if r.request.method == "POST" and "/v1/identity/me/delegations" in r.url:
            try:
                grants_posted.append((r.status, r.json()))
            except Exception:  # noqa: BLE001
                grants_posted.append((r.status, None))
    ppage.on("response", _grant_resp)
    task_btn = ppage.locator("[data-testid=consent-durations] button").filter(has_text=re.compile("build|run only", re.I))
    (task_btn.first if task_btn.count() else ppage.locator("[data-testid=consent-durations] button").first).click()
    K.answer_step_up_sheet(ppage, p, timeout_ms=5000)
    ppage.wait_for_timeout(5000)
    j.shot(ppage, "build-started")
    j.note(f"grant POSTs from the sheet: {short(grants_posted, 600)}; sheet error: {K.body_text(ppage) if False else ''}")
    err = ppage.locator("[data-testid=consent-durations] ~ .idn-err, .idn-err[role=alert]")
    refused = [b for st, b in grants_posted if st >= 400]
    if refused:
        said = err.first.inner_text()[:200] if err.count() else ""
        j.check("a refused grant is explained in the sheet (not “That didn't work”)", bool(said) and "didn't work" not in said,
                expect=f"words for {short(refused[-1], 120)}", actual=said,
                known="FX-C P1-11/12/13 (real error states, no raw codes)",
                repro="consent sheet → For this run only, as a person whose GitHub cannot reach the repo")
    mine = (api(ppage, "GET", "/v1/identity/me/delegations").get("body") or {}).get("delegations", [])
    new = [g for g in mine if g["id"] not in before]
    if not new and any(st == 403 and (b or {}).get("error") == "github_access_lacking" for st, b in grants_posted):
        j.check(f"[{phase}] API: without the person's own GitHub access the grant is refused (D28 guard)", True,
                expect="403 github_access_lacking", actual="403 github_access_lacking")
        j.note(f"[{phase}] no grant possible here for a test person; continuing on the second stack")
        pctx.close()
        h.close()
        return False
    j.check(f"[{phase}] a grant was created for this build", len(new) == 1, expect="1 new grant", actual=[(g["id"], g["kind"]) for g in new], fatal=True)
    g = new[0]
    env.shared["grant_id"] = g["id"]
    env.shared["j8_target"] = T
    env.shared["j8_person"] = p
    j.check("the grant is task-bound (for this build only)", g.get("kind") == "task" and g.get("task_run_id"),
            expect="kind task, bound to the run", actual=short({k: g.get(k) for k in ("kind", "task_run_id", "status")}))
    j.check("the grant is never wider than the person holds", set(g.get("permissions") or []) <= held,
            expect="grant ⊆ person's permissions", actual=sorted(set(g.get("permissions") or []) - held))
    run = T.stack.psql("SELECT id || ' ' || coalesce(on_behalf_of, '-') || ' ' || status FROM workflow_runs "
                         "WHERE grant_id = :'g' ORDER BY created_at DESC LIMIT 1", {"g": g["id"]})
    j.check("DB: the run carries the grant and acts on behalf of the person", bool(run) and p.principal_id in run,
            expect="workflow_runs(grant_id) on_behalf_of the person", actual=run)
    env.shared["j8_run"] = run.split(" ")[0] if run else None
    ppage.goto(f"{T.web}/identity/agents/delegations/{g['id']}", wait_until="domcontentloaded")
    K.settle(ppage)
    j.shot(ppage, "lineage")
    hops = ppage.locator("[data-testid^=hop-]")
    txt = K.body_text(ppage, 6000)
    j.check(f"[{phase}] the lineage page names the grant's person, agents, scope and “this build only”",
            all(a["name"] in txt for a in g.get("agents") or []) and "this build only" in txt and "boss-dogfood" in txt,
            expect="person → agents, project, this build only", actual=txt[-400:])
    if hops.count() >= 2:
        j.check(f"[{phase}] the live chain shows person → agent hops", True, expect="hops", actual=hops.count())
    else:
        j.note(f"[{phase}] live chain: {hops.count()} hops — no agent has acted under the grant on this stack "
               "(no model runs here), so the chain view has nothing to draw yet")
    ev = audit_has(opage, "delegation.created", actor=p.principal_id)
    j.check("audit: delegation.created by the person", bool(ev), expect="event", actual=short(ev))
    pctx.close()
    h.close()
    return True


# =================================================================================================
# J9 — revoke mid-run: the cascade
# =================================================================================================

@journey("J9", "Revoke the grant mid-run: the cascade",
         "The Owner revokes the build's grant from its lineage page: the cascade preview lists what "
         "stops; after Revoke and halt the grant is revoked, its run tokens are dead, the run halts "
         "with 'authority revoked', and the audit log says delegation.revoked.")
def j9(env: Env, j: Journey) -> None:
    grant = env.shared.get("grant_id")
    j.check("a build grant from J8 to revoke", bool(grant), expect="J8 ran first", actual=grant, fatal=True)
    T = env.shared.get("j8_target") or Target(env.web, env.stack, env.owner, lambda: env.owner_page(j))
    owner = T.owner
    h, opage = T.owner_page()
    K.age_my_session(T.stack, opage)
    opage.goto(f"{T.web}/identity/agents/delegations/{grant}", wait_until="domcontentloaded")
    opage.wait_for_selector("[data-testid=revoke-grant]", timeout=30000)
    K.settle(opage)
    preview = api(opage, "GET", f"/v1/identity/delegations/{grant}/cascade-preview").get("body") or {}
    opage.click("[data-testid=revoke-grant]")
    opage.wait_for_selector("[data-testid=revoke-confirm]", timeout=20000)
    opage.wait_for_timeout(1000)
    j.shot(opage, "cascade-preview")
    items = [x.get("kind") for x in preview.get("items") or []]
    j.check("the cascade preview lists what stops (grant, run, tokens)", bool(items) and opage.locator("[data-testid^=cascade-]").count() > 0,
            expect="items such as halt runs / revoke tokens / revoke grant", actual=items)
    seq0 = audit_seq(opage)
    t0 = time.time()
    opage.click("[data-testid=revoke-confirm]")
    sheet = K.answer_step_up_sheet(opage, owner, timeout_ms=6000)
    j.check("revoking asks for step-up (stale MFA)", sheet, expect="step-up sheet", actual=sheet)
    opage.wait_for_selector("[data-testid=revoked]", timeout=30000)
    j.shot(opage, "revoked")
    g = api(opage, "GET", f"/v1/identity/delegations/{grant}").get("body") or {}
    j.check("API: the grant is revoked", g.get("status") == "revoked", expect="revoked", actual=g.get("status"))
    live = T.stack.psql("SELECT count(*) FROM identity_run_credentials WHERE grant_id = :'g' AND revoked_at IS NULL", {"g": grant})
    j.check("DB: no live run token under the grant", live == "0", expect="0", actual=live)
    run = env.shared.get("j8_run")
    if run:
        halted = ("running", "queued", "pending", "waiting", "created")
        st = K.wait_for(lambda: (lambda v: v if v and v not in halted else None)(
            T.stack.psql("SELECT status FROM workflow_runs WHERE id = :'r'", {"r": run})), max(0.5, 5 - (time.time() - t0)))
        final = st or T.stack.psql("SELECT status FROM workflow_runs WHERE id = :'r'", {"r": run})
        j.check("the run halts within 5 s of the revoke (authority revoked)", bool(st),
                expect="a halted/terminal status within 5 s", actual=f"{final} after {time.time() - t0:.1f}s")
    ev = audit_has(opage, "delegation.revoked", target=grant, since_seq=seq0) or audit_has(opage, "delegation.revoked", since_seq=seq0)
    j.check("audit: delegation.revoked", bool(ev), expect="event", actual=short(ev))
    again = K.stepped(opage, owner, "POST", f"/v1/identity/delegations/{grant}/revoke", {}, j)
    j.check("API: a revoked grant stays revoked (terminal)", again["status"] == 409, expect="409 revoked_is_terminal",
            actual=f"{again['status']} {short(again.get('body'))}")
    h.close()


# =================================================================================================
# J10 — lower Forge's ceiling
# =================================================================================================

@journey("J10", "Lower Forge's ceiling",
         "An Admin turns preview:manage off Forge's ceiling (step-up); never-delegable permissions are "
         "locked; the change is in the audit/lineage; and from then on no grant can hand Forge "
         "preview:manage (403 exceeds_ceiling naming it). The ceiling is restored afterwards.")
def j10(env: Env, j: Journey) -> None:
    admin, owner = env.admin, env.owner
    actx, apage = K.signed_in_page(env.browser, env.web, admin, j)
    agents = (api(apage, "GET", "/v1/identity/agents").get("body") or {}).get("agents") or []
    # The stub's "Forge" is the builder; a real install's builder agent is software_engineer.
    forge = next((a for a in agents if a.get("role_key") in ("forge", "software_engineer")), None)
    j.check("Forge (the builder) has a Passport", bool(forge), expect="agent with role_key forge/software_engineer", actual=[a.get("role_key") for a in agents][:20], fatal=True)
    fid = forge["agent_id"]
    ceiling0 = set(forge.get("ceiling") or [])
    j.check("Forge's ceiling holds preview:manage to begin with", "preview:manage" in ceiling0, expect="held",
            actual=sorted(ceiling0)[:10], fatal=True)
    env.shared.setdefault("_cleanup", []).append(lambda: _restore_ceiling(env, fid, ceiling0))
    h, opage = env.owner_page(j)
    pre = K.stepped(opage, owner, "POST", "/v1/identity/me/delegations",
                    {"agents": [fid], "permissions": ["preview:manage"], "projects": [PROJECT], "kind": "standing", "days": 1, "purpose": "fxd-j10 before"}, j)
    j.check("before: a grant may hand Forge preview:manage", pre["status"] == 201, expect="201",
            actual=f"{pre['status']} {short(pre.get('body'))}")
    if pre["status"] == 201:
        K.stepped(opage, owner, "POST", f"/v1/identity/me/delegations/{pre['body']['id']}/revoke", {}, j)
    apage.goto(f"{env.web}/identity/agents/{fid}", wait_until="domcontentloaded")
    apage.wait_for_selector("[data-testid='ceil-preview:manage']", timeout=30000)
    K.settle(apage)
    locked = apage.locator("[data-testid='ceil-user:invite']")
    if locked.count():
        j.check("never-delegable permissions are locked", locked.first.get_attribute("aria-disabled") == "true"
                and "Never delegable" in (locked.first.get_attribute("title") or ""),
                expect="aria-disabled, “Never delegable”", actual=locked.first.get_attribute("title"))
    K.age_my_session(env.stack, apage)
    seq0 = audit_seq(opage)
    apage.click("[data-testid='ceil-preview:manage']")
    j.shot(apage, "ceiling-edit")
    apage.click("[data-testid=save-ceiling]")
    sheet = K.answer_step_up_sheet(apage, admin, timeout_ms=8000)
    j.check("saving the ceiling asks for step-up", sheet, expect="step-up sheet", actual=sheet)
    apage.wait_for_timeout(2000)
    j.shot(apage, "ceiling-saved")
    now = api(apage, "GET", f"/v1/identity/agents/{fid}/passport").get("body") or {}
    j.check("API: preview:manage is off Forge's ceiling", "preview:manage" not in (now.get("ceiling") or []),
            expect="removed", actual="still there" if "preview:manage" in (now.get("ceiling") or []) else "removed")
    ev = audit_has(opage, "agent.passport.ceiling_changed", since_seq=seq0)
    j.check("audit/lineage: agent.passport.ceiling_changed (−preview:manage)", bool(ev) and "preview:manage" in json.dumps(ev),
            expect="event naming preview:manage", actual=short(ev))
    lin = api(apage, "GET", f"/v1/identity/agents/{fid}/lineage")
    j.check("API: the agent's lineage route lists ceiling_changed", lin["status"] == 200 and "ceiling" in json.dumps(lin.get("body")),
            expect="GET /v1/identity/agents/{id}/lineage → ceiling_changed", actual=lin["status"],
            known="FX-B P2 (lineage route)")
    post = K.stepped(opage, owner, "POST", "/v1/identity/me/delegations",
                     {"agents": [fid], "permissions": ["preview:manage"], "projects": [PROJECT], "kind": "standing", "days": 1, "purpose": "fxd-j10 after"}, j)
    j.check("after: no grant can hand Forge preview:manage (403 exceeds_ceiling naming it)",
            post["status"] == 403 and "preview:manage" in json.dumps(post.get("body")),
            expect='403 {"error": "exceeds_ceiling", "permissions": ["preview:manage"]}', actual=f"{post['status']} {short(post.get('body'))}")
    _restore_ceiling(env, fid, ceiling0)
    back = api(apage, "GET", f"/v1/identity/agents/{fid}/passport").get("body") or {}
    j.check("cleanup: Forge's ceiling restored", set(back.get("ceiling") or []) == ceiling0, expect="as before",
            actual=sorted(ceiling0 ^ set(back.get("ceiling") or [])))
    h.close()
    actx.close()


def _restore_ceiling(env: Env, fid: str, ceiling: set[str]) -> None:
    h, page = env.owner_page()
    try:
        cur = set((api(page, "GET", f"/v1/identity/agents/{fid}/passport").get("body") or {}).get("ceiling") or [])
        if cur != ceiling:
            K.stepped(page, env.owner, "PATCH", f"/v1/identity/agents/{fid}/passport", {"ceiling": sorted(ceiling)})
    finally:
        h.close()


# =================================================================================================
# The second stack (J1, J2, J7, J14): an unclaimed install this run controls
# =================================================================================================

S2_API_ENV = {"BOSS_OUTBOUND_ALLOW_PRIVATE": KC.NAME}


def _s2(env: Env, j: Journey, *, fresh: bool, api_env: dict[str, str] | None = None,
        web_env: dict[str, str] | None = None, extra: dict[str, Any] | None = None,
        env_values: dict[str, str] | None = None) -> Stack2:
    """The second stack: brought up (or reset) unclaimed and seeded. Always torn down at the end."""
    s2 = env.shared.get("_s2")
    if s2 is None:
        head = docker_headroom_gib()
        j.note(f"docker VM headroom before {env.args.stack2}: {head:.1f} GiB")
        if head < 2.0:
            raise RuntimeError(f"not starting {env.args.stack2}: only {head:.1f} GiB free in the docker VM")
        s2 = Stack2.from_main(Path(env.args.env_file), project=env.args.stack2,
                              out_dir=Path(env.args.env_file).parent)
        env.shared["_s2"] = s2
        env.shared.setdefault("_cleanup", []).append(lambda: _s2_down(env))
        fresh = True
    if env_values:
        s2.set_values(**env_values)
    if fresh:
        services: dict[str, Any] = {"api": {"environment": {**S2_API_ENV, **(api_env or {})}}}
        if web_env:
            services["web"] = {"environment": web_env}
        services.update(extra or {})
        j.note(f"(re)starting {s2.project} unclaimed")
        s2.down()
        s2.up(services)
        s2.seed()
        env.shared.pop("s2_owner", None)
    return s2


def _s2_down(env: Env) -> None:
    s2 = env.shared.get("_s2")
    if s2 is not None:
        s2.down()
        print(f"{s2.project}: down -v done", flush=True)


def _claim_codes_and_land(page: Any, j: Journey) -> list[str]:
    page.wait_for_selector("[data-testid=recovery-codes]", timeout=40000)
    codes = [K.secret(x.strip()) for x in page.locator("[aria-label='Recovery codes'] li").all_inner_texts()]
    j.shot(page, "claim-recovery-codes")
    gated = page.locator("[data-testid=codes-continue]").is_disabled()
    j.check("recovery codes shown once, Continue gated until stored", len(codes) == 10 and gated,
            expect="10 codes, Continue disabled", actual=f"{len(codes)} codes, disabled={gated}")
    page.check("[data-testid=codes-stored]")
    page.click("[data-testid=codes-continue]")
    page.wait_for_url(re.compile(r"/identity"), timeout=30000)
    K.settle(page)
    return codes


def _claim_codes_and_land_enrol(page: Any, j: Journey) -> None:
    page.wait_for_selector("[data-testid=recovery-codes]", timeout=40000)
    j.shot(page, "enrol-recovery-codes")
    page.check("[data-testid=codes-stored]")
    page.click("[data-testid=codes-continue]")
    page.wait_for_timeout(2000)
    K.settle(page)


def _s2_owner_after_claim(env: Env, j: Journey, s2: Stack2, page: Any, ctx: Any, p: K.Person) -> None:
    """Give the claimant an authenticator app too (API step-ups) and keep their session."""
    c = K.enrol_totp_api(page, p)
    j.check("the new Owner adds an authenticator app (for later step-ups)", c["status"] == 200,
            expect="200", actual=f"{c['status']} {short(c.get('body'))}")
    p.principal_id = (me(page).get("body") or {}).get("principal_id", "")
    env.shared["s2_owner"] = (p, ctx, page)


# =================================================================================================
# J1 — fresh install: setup link → claim with a passkey → Owner → the setup endpoint is gone
# =================================================================================================

@journey("J1", "Fresh install: claim with a passkey",
         "An unclaimed install answers the setup link from the host shell; the claim makes a "
         "passkey Owner who lands on Identity; from then on the setup endpoint answers 404 for good "
         "and /claim says Already claimed; the Owner signs in again with the passkey alone.")
def j1(env: Env, j: Journey) -> None:
    # A fresh install with no shared console password (the setup-link path).
    s2 = _s2(env, j, fresh=True, api_env={"BOSS_CONSOLE_PASSWORD": ""})
    st = I.setup_status(s2.web)
    j.check("API: the new install is unclaimed and offers the setup link", st.get("claimed") is False
            and "setup_token" in (st.get("paths") or []), expect="claimed false, paths ∋ setup_token", actual=st)
    token = K.secret(I.setup_token(s2.host())) or ""
    ctx, page = K.new_page(env.browser, j)
    cdp, aid = K.authenticator(ctx, page)
    page.goto(f"{s2.web}/claim#t={token}", wait_until="domcontentloaded")
    page.wait_for_selector("#claim-name", timeout=30000)
    K.settle(page)
    txt = K.body_text(page)
    j.check("the claim panel: “Claim this BOSS”, the setup link's expiry, name/email",
            "Claim this BOSS" in txt and "setup link" in txt and page.locator("#claim-email").count() == 1,
            expect="title, “setup link · expires in N min”, fields", actual=txt[:200])
    j.shot(page, "claim-form")
    p = K.Person(role="owner", email=f"fxd-j1-owner-{int(time.time()) % 100000}@example.test",
                 name="FXD J1 Owner", password="")
    page.fill("#claim-name", p.name)
    page.fill("#claim-email", p.email)
    if page.locator("#claim-seat").count():
        page.select_option("#claim-seat", index=0)
    page.click("[data-testid=claim-submit]")
    _claim_codes_and_land(page, j)
    creds = cdp.send("WebAuthn.getCredentials", {"authenticatorId": aid})["credentials"]
    j.check("a passkey was registered on the virtual authenticator", len(creds) == 1, expect="1", actual=len(creds))
    j.shot(page, "owner-identity-overview")
    m = me(page).get("body") or {}
    j.check("lands on Identity as Owner with a passkey", "/identity" in page.url and "owner" in (m.get("access_roles") or [])
            and (m.get("sign_in") or {}).get("passkeys") == 1,
            expect="/identity, roles ∋ owner, 1 passkey (a claimed seat may add its legacy role)", actual=f"{page.url} {m.get('access_roles')} {short(m.get('sign_in'))}")
    pill = K.body_text(page, 1500)
    j.check("the top bar shows the Owner", "Owner" in pill, expect="“… · Owner”", actual=pill[:200])
    after = I.setup_status(s2.web)
    j.check("API: setup status says claimed", after.get("claimed") is True, expect="claimed true", actual=after)
    again = K.raw_fetch(page, "POST", "/v1/auth/setup/claim",
                        {"setup_token": token, "name": "Again", "email": "again@example.test"}, csrf=False)
    j.check("API: the setup endpoint answers 404 for good", again["status"] == 404, expect="404",
            actual=f"{again['status']} {short(again.get('body'))}")
    link_cli = s2.host()(["-m", "boss_engine.identity.claim", "setup-link"])
    j.check("the host CLI no longer issues a setup link once claimed", "#t=" not in (link_cli.stdout or ""),
            expect="no link", actual=K.scrub((link_cli.stdout or link_cli.stderr or "").strip())[-160:])
    ev = audit_has(page, "bootstrap.claimed")
    j.check("audit: bootstrap.claimed", bool(ev), expect="event", actual=short(ev))
    cctx, cpage = K.new_page(env.browser, j)
    cpage.goto(f"{s2.web}/claim", wait_until="domcontentloaded")
    K.settle(cpage)
    j.shot(cpage, "already-claimed")
    j.check("/claim now says Already claimed", "Already claimed" in K.body_text(cpage), expect="Already claimed",
            actual=K.body_text(cpage, 200))
    cctx.close()
    # The passkey alone signs the Owner in, in a fresh browser.
    pctx, ppage = K.new_page(env.browser, j)
    pcdp, paid = K.authenticator(pctx, ppage)
    for c in creds:
        pcdp.send("WebAuthn.addCredential", {"authenticatorId": paid, "credential": c})
    ppage.goto(f"{s2.web}/login", wait_until="domcontentloaded")
    ppage.wait_for_selector("[data-testid=passkey-sign-in]", timeout=30000)
    K.settle(ppage)
    ppage.click("[data-testid=passkey-sign-in]")
    ppage.wait_for_url(lambda u: "/login" not in u, timeout=30000)
    K.settle(ppage)
    pm = me(ppage).get("body") or {}
    j.check("a fresh browser signs in with the passkey alone", pm.get("principal_id") == m.get("principal_id")
            and "owner" in (pm.get("access_roles") or []),
            expect="me = the Owner", actual=short({k: pm.get(k) for k in ("access_roles", "email")}))
    j.shot(ppage, "passkey-sign-in")
    pctx.close()
    _s2_owner_after_claim(env, j, s2, page, ctx, p)


# =================================================================================================
# J2 — an install with the shared console password: claim with it; what it becomes afterwards
# =================================================================================================

@journey("J2", "Upgrade with the shared console password",
         "On an install that has BOSS_CONSOLE_PASSWORD, /login offers the claim; /claim explains "
         "where the password is without showing it; the shared password claims the first Owner "
         "(with a passkey); afterwards it maps to the legacy Operator with a countdown banner.")
def j2(env: Env, j: Journey) -> None:
    s2 = _s2(env, j, fresh=True)
    shared = s2.value("BOSS_CONSOLE_PASSWORD")
    ctx, page = K.new_page(env.browser, j)
    cdp, aid = K.authenticator(ctx, page)
    page.goto(f"{s2.web}/login", wait_until="domcontentloaded")
    K.settle(page)
    j.shot(page, "login-claim-door")
    j.check("/login shows the claim door above the providers", page.locator("[data-testid=claim-door]").count() == 1
            and "Claim" in page.locator("[data-testid=claim-door]").inner_text(),
            expect="“This BOSS has no Owner yet. Claim it”", actual=K.body_text(page, 300))
    # The setup link from the host shell must still work on such an install (§4.11 step 2).
    token = K.secret(I.setup_token(s2.host())) or ""
    page.goto(f"{s2.web}/claim#t={token}", wait_until="domcontentloaded")
    page.wait_for_selector("#claim-name", timeout=30000)
    K.settle(page)
    j.shot(page, "claim-with-setup-link-on-a-password-install")
    j.check("a setup link is honoured on an install that also has a shared password",
            page.locator("[data-testid=claim-legacy-password]").count() == 0,
            expect="the claim proceeds with the setup link (no shared password asked)",
            actual="the page asks for the shared console password",
            known="unassigned (new: /v1/auth/options picks legacy_password over setup_token, so the claim page ignores #t=)",
            repro=f"{s2.web}/claim#t=<setup-link token> on a compose install (BOSS_CONSOLE_PASSWORD is always set)")
    page.goto(f"{s2.web}/claim", wait_until="domcontentloaded")
    page.wait_for_selector("#claim-name", timeout=30000)
    K.settle(page)
    hint = page.locator("[data-testid=console-password-hint]")
    if hint.count():
        hint.locator("summary").click()
    hint_text = hint.inner_text() if hint.count() else ""
    j.shot(page, "wheres-my-password")
    j.check("“Where's my password?” shows the commands with Copy and never the password",
            hint.count() == 1 and "Copy" in hint_text and shared not in page.content(),
            expect="commands + Copy; the password nowhere in the page", actual=K.scrub(hint_text)[:200])
    p = K.Person(role="owner", email=f"fxd-j2-owner-{int(time.time()) % 100000}@example.test", name="FXD J2 Owner", password="")
    page.fill("#claim-name", p.name)
    page.fill("#claim-email", p.email)
    page.fill("[data-testid=claim-legacy-password]", shared)
    if page.locator("#claim-seat").count():
        page.select_option("#claim-seat", index=0)
    page.click("[data-testid=claim-submit]")
    _claim_codes_and_land(page, j)
    m = me(page).get("body") or {}
    j.check("an Owner session with a passkey", "owner" in (m.get("access_roles") or []) and (m.get("sign_in") or {}).get("passkeys") == 1,
            expect="owner + 1 passkey", actual=short({k: m.get(k) for k in ("access_roles", "sign_in")}))
    j.shot(page, "owner-after-legacy-claim")
    kinds = [b.get("kind") for b in m.get("banners") or []]
    j.check("a banner counts down the shared password's last days", any("legacy" in str(k) for k in kinds)
            or page.locator("[data-testid=identity-banners]").filter(has_text=re.compile("password", re.I)).count() > 0,
            expect="a legacy-password banner with a countdown", actual=kinds,
            known="FX-A P0-2 (wire the banners)")
    again = K.raw_fetch(page, "POST", "/v1/auth/setup/claim",
                        {"legacy_password": shared, "name": "x", "email": "x@example.test"}, csrf=False)
    j.check("API: the claim answers 404 for good, even with the shared password", again["status"] == 404,
            expect="404", actual=again["status"])
    # After the claim, the shared password maps to usr_legacy_shared (Operator) for 30 days.
    lctx, lpage = K.new_page(env.browser, j)
    lpage.goto(f"{s2.web}/login", wait_until="domcontentloaded")
    K.settle(lpage)
    opts = K.raw_fetch(lpage, "GET", "/v1/auth/options", csrf=False).get("body") or {}
    j.shot(lpage, "login-after-claim")
    shared_ui = lpage.get_by_text(re.compile("shared (console )?password", re.I)).count() > 0
    j.check("the sign-in page still offers the shared password (as the legacy Operator, 30 days)",
            bool(opts.get("legacy_password")) and shared_ui,
            expect="a shared-password sign-in that lands as usr_legacy_shared (Operator)",
            actual=f"options.legacy_password={opts.get('legacy_password')} ui={shared_ui}",
            known="FX-A P0-1 (shared password signs in as Operator for 30 days)")
    lctx.close()
    _s2_owner_after_claim(env, j, s2, page, ctx, p)


# =================================================================================================
# J7 — a real Keycloak: OIDC and SAML sign-in, JIT, invitation binding, group sync → access, SCIM
# =================================================================================================

def _s2_owner(env: Env, j: Journey) -> tuple[Stack2, K.Person, Any]:
    """The second stack's Owner page (J1/J2's claimant; or claim one now through the API)."""
    s2 = env.shared.get("_s2") or _s2(env, j, fresh=True)
    held = env.shared.get("s2_owner")
    if held and _alive(held[2]):
        return s2, held[0], held[2]
    if I.setup_status(s2.web).get("claimed"):
        s2 = _s2(env, j, fresh=True)
    p = K.Person(role="owner", email=f"fxd-s2-owner-{int(time.time()) % 100000}@example.test", name="FXD S2 Owner",
                 password=K.strong_password("s2owner"))
    token = K.secret(I.setup_token(s2.host())) or ""
    ctx, page = K.new_page(env.browser, j)
    page.goto(f"{s2.web}/login", wait_until="domcontentloaded")
    c = K.raw_fetch(page, "POST", "/v1/auth/setup/claim",
                    {"setup_token": token, "name": p.name, "email": p.email, "password": p.password}, csrf=False)
    if c["status"] != 200:
        raise RuntimeError(f"claim answered {c['status']} {short(c.get('body'))}")
    _s2_owner_after_claim(env, j, s2, page, ctx, p)
    return s2, p, page


def _sso_sign_in(page: Any, web: str, provider_id: str, username: str, password: str, j: Journey,
                 shot: str) -> str:
    """From /login, press the provider button, sign in at Keycloak, come back. Returns the URL landed on."""
    page.goto(f"{web}/login", wait_until="domcontentloaded")
    if K.raw_fetch(page, "GET", "/v1/identity/me", csrf=False)["status"] == 200:
        K.raw_fetch(page, "POST", "/v1/auth/logout")
        page.goto(f"{web}/login", wait_until="domcontentloaded")
    K.settle(page)
    page.click(f"[data-testid=sso-{provider_id}]", timeout=20000)
    page.wait_for_load_state("domcontentloaded")
    if page.locator("#username").count() or "fxd-keycloak" in page.url:
        try:
            KC.login_at_keycloak(page, username, password, timeout_ms=20000)
        except Exception:  # noqa: BLE001
            pass       # an existing Keycloak session signs in without the form
    page.wait_for_url(lambda u: u.startswith(web), timeout=40000)
    K.settle(page)
    j.shot(page, shot)
    return page.url


def _last_exception(s2: Stack2) -> str:
    lines = s2.logs("api", 400).splitlines()
    hits = [ln for ln in lines if re.search(r"(Error|Exception|Refused)\b", ln) and "Traceback" not in ln]
    return " | ".join(h.strip()[-220:] for h in hits[-3:])


def _keycloak_ctx(env: Env, j: Journey) -> tuple[Any, Any]:
    b = env.shared.get("_kc_browser")
    if b is None:
        b = env.pw.chromium.launch(headless=not env.args.headed, args=KC.browser_args())
        env.shared["_kc_browser"] = b
        env.shared.setdefault("_cleanup", []).insert(0, lambda: b.close())
    return K.new_page(b, j, https=True)


@journey("J7", "Keycloak: OIDC and SAML sign-in, group sync grants access, SCIM disable",
         "A real IdP: OIDC discovery through the SSRF-safe client; JIT creates the person with no "
         "roles; the IdP group maps to a BOSS group whose Operator binding GRANTS EFFECTIVE ACCESS "
         "(permissions and a 2xx on an Operator route), and removing the IdP group takes it away at "
         "the next sign-in; an invitation binds the first SSO identity; the same through SAML; SCIM "
         "provisions and disables; an IdP group is read-only to the manual groups API.")
def j7(env: Env, j: Journey) -> None:
    s2, owner, opage = _s2_owner(env, j)
    kc = KC.Keycloak(network=s2.network())
    env.shared.setdefault("_cleanup", []).insert(0, kc.stop)
    j.note("starting Keycloak (fxd-keycloak)")
    kc.start()
    web = s2.web
    pw = {u: K.strong_password(f"kc{u}") for u in ("alice", "bob", "carol", "dave")}

    # Keycloak: realm fxd (OIDC), group fxd-builders, users.
    kc.realm("fxd")
    kg = kc.group("fxd", "fxd-builders")
    secret_ = kc.oidc_client("fxd", "boss-fxd", web)
    bob = kc.user("fxd", "bob", "bob@fxd.test", pw["bob"], first="Bob", groups=(kg,))
    kc.user("fxd", "alice", "alice@fxd.test", pw["alice"], first="Alice")
    kc.user("fxd", "carol", "carol@fxd.test", pw["carol"], first="Carol")

    # BOSS: a group bound to Operator, the provider mapping the IdP group to it.
    g = K.stepped(opage, owner, "POST", "/v1/identity/groups", {"key": "fxd-j7-builders", "name": "fxd-j7 builders"}, j)
    gid = (g.get("body") or {}).get("id")
    j.check("Owner creates the BOSS group", g["status"] == 201 and gid, expect="201", actual=f"{g['status']} {short(g.get('body'))}", fatal=True)
    rb = K.stepped(opage, owner, "POST", "/v1/identity/role-bindings",
                   {"subject_type": "group", "subject_id": gid, "role_id": _role_id(opage, "operator")}, j)
    j.check("…and binds it to Operator", rb["status"] == 201, expect="201", actual=f"{rb['status']} {short(rb.get('body'))}")
    pv = K.stepped(opage, owner, "POST", "/v1/identity/providers", {
        "protocol": "oidc", "preset": "keycloak", "name": "FXD Keycloak", "issuer": kc.issuer("fxd"),
        "client_id": "boss-fxd", "client_secret": secret_, "scopes": ["openid", "email", "profile"],
        "domains": ["fxd.test"], "jit_mode": "domain", "groups_claim": "groups",
        "group_mappings": {"fxd-builders": gid}}, j)
    pid = (pv.get("body") or {}).get("id")
    j.check("Owner registers the OIDC provider (Sign-in & SSO API)", pv["status"] == 201 and pid,
            expect="201", actual=f"{pv['status']} {short(pv.get('body'))}", fatal=True)
    j.check("API: the provider view carries no secret", "client_secret" not in json.dumps(pv.get("body"))
            or (pv.get("body") or {}).get("has_client_secret") is True and secret_ not in json.dumps(pv.get("body")),
            expect="has_client_secret, never the value", actual=short(pv.get("body")))
    t = K.stepped(opage, owner, "POST", f"/v1/identity/providers/{pid}/test", None, j)
    j.check("connection test: discovery + JWKS through the SSRF-safe client (allow-listed private host)",
            (t.get("body") or {}).get("ok") is True, expect='{"ok": true}', actual=short(t.get("body")))
    opage.goto(f"{web}/identity/sign-in", wait_until="domcontentloaded")
    K.settle(opage)
    j.shot(opage, "sign-in-and-sso-settings")
    j.check("Sign-in & SSO lists the provider", opage.locator(f"[data-testid=provider-{pid}]").count() == 1
            or "FXD Keycloak" in K.body_text(opage), expect="the provider row", actual=K.body_text(opage, 300))

    # Bob: first OIDC sign-in → JIT, group sync, effective Operator access.
    bctx, bpage = _keycloak_ctx(env, j)
    bpage.goto(f"{web}/login", wait_until="domcontentloaded")
    K.settle(bpage)
    j.shot(bpage, "login-with-sso-button")
    j.check("/login shows the provider button", bpage.locator(f"[data-testid=sso-{pid}]").count() == 1,
            expect="Continue with FXD Keycloak", actual=K.body_text(bpage, 300))
    landed = _sso_sign_in(bpage, web, pid, "bob", pw["bob"], j, "bob-after-oidc")
    m = me(bpage)
    mb = m.get("body") or {}
    j.check("OIDC sign-in lands signed in (JIT for the fxd.test domain)", m["status"] == 200 and "error=" not in landed
            and mb.get("email") == "bob@fxd.test", expect="me 200 as bob@fxd.test", actual=f"{landed} {m['status']} {short(mb.get('email'))}",
            fatal=True)
    bob_pid = mb.get("principal_id")
    direct = s2.stack.psql("SELECT count(*) FROM identity_role_bindings WHERE subject_type = 'principal' "
                           "AND subject_id = :'p'", {"p": bob_pid})
    j.check("JIT gives no access role of its own (only the group's)", direct == "0" and mb.get("access_roles") == ["operator"],
            expect="0 direct bindings; access_roles [operator] through the group", actual=f"direct={direct} roles={mb.get('access_roles')}")
    link = s2.stack.psql("SELECT issuer || ' ' || subject FROM identity_user_identities WHERE principal_id = :'p'", {"p": bob_pid})
    j.check("DB: the identity is linked by (issuer, subject)", link.startswith(kc.issuer("fxd") + " ") and len(link.split()) == 2,
            expect=f"{kc.issuer('fxd')} <sub>", actual=link)
    grp = api(opage, "GET", f"/v1/identity/groups/{gid}").get("body") or {}
    mem = next((x for x in grp.get("members") or [] if x.get("principal_id") == bob_pid or x.get("id") == bob_pid), None)
    j.check("group sync: bob is a member of the mapped BOSS group, source oidc", bool(mem) and mem.get("source") == "oidc",
            expect="member, source oidc", actual=short(mem or grp.get("members")))
    held = perms(bpage, PROJECT)
    j.check("THE GROUP BINDING GRANTS EFFECTIVE ACCESS: Operator permissions are held",
            {"code_task:create", "deployment:read"} <= held, expect="⊇ {code_task:create, deployment:read}",
            actual=sorted(held)[:12])
    r = api(bpage, "GET", f"/v1/deployment-targets?project_id={PROJECT}")
    j.check("…and an Operator route answers 2xx", 200 <= r["status"] < 300, expect="2xx on GET /v1/deployment-targets",
            actual=f"{r['status']} {short(r.get('body'), 120)}")
    ev = audit_has(opage, "sso.login", actor=bob_pid)
    j.check("audit: sso.login for bob", bool(ev), expect="event", actual=short(ev))
    opage.goto(f"{web}/identity/groups/{gid}", wait_until="domcontentloaded")
    K.settle(opage)
    j.shot(opage, "mapped-group")
    main_txt = opage.locator("main").inner_text() if opage.locator("main").count() else K.body_text(opage)
    member_row = opage.locator(f"[data-testid=member-{bob_pid}]")
    removable = opage.locator(f"[data-testid=remove-member-{bob_pid}]").count() > 0 or \
        (member_row.count() > 0 and member_row.get_by_role("button", name="Remove").count() > 0)
    j.check("the group page shows bob's membership as IdP-synced and read-only (source badge, no Remove)",
            ("OIDC" in main_txt or "Keycloak" in main_txt) and not removable,
            expect="an OIDC/IdP source badge on the membership; no Remove", actual=f"removable={removable}; " + main_txt[-300:],
            known="unassigned (new: an OIDC-synced membership shows as a plain member with Remove)",
            repro=f"{web}/identity/groups/<group mapped from the IdP> as Owner after an OIDC group sync")
    j.check("the group page shows names, never raw principal ids", "usr_" not in main_txt,
            expect="no usr_… ids", actual=re.findall(r"usr_[a-z0-9]{6,}", main_txt)[:3],
            known="FX-C P1-11/12/13 (no raw codes, ids, jargon)", repro=f"{web}/identity/groups/<id>: Access roles “granted by usr_…”")
    rm = K.stepped(opage, owner, "DELETE", f"/v1/identity/groups/{gid}/members/{bob_pid}", None, j)
    j.check("API: the manual groups API refuses removing an IdP-synced membership", rm["status"] in (403, 409, 422),
            expect="409 idp read-only", actual=f"{rm['status']} {short(rm.get('body'))}",
            known="unassigned (new: an OIDC-synced membership of a mapped group can be removed by hand; the next sign-in re-adds it)",
            repro=f"DELETE /v1/identity/groups/<mapped group>/members/<oidc member> as Owner → 200")
    if rm["status"] < 300:
        _sso_sign_in(bpage, web, pid, "bob", pw["bob"], j, "bob-resynced")

    # The IdP removes bob from the group: the next sign-in takes the access away.
    kc.leave_group("fxd", bob, kg)
    K.raw_fetch(bpage, "POST", "/v1/auth/logout")
    _sso_sign_in(bpage, web, pid, "bob", pw["bob"], j, "bob-after-group-removed")
    held2 = perms(bpage, PROJECT)
    r2 = api(bpage, "GET", f"/v1/deployment-targets?project_id={PROJECT}")
    grp2 = api(opage, "GET", f"/v1/identity/groups/{gid}").get("body") or {}
    j.check("after the IdP drops the group: no longer a member", not any(
        (x.get("principal_id") or x.get("id")) == bob_pid for x in grp2.get("members") or []),
        expect="not a member", actual=short(grp2.get("members")))
    j.check("…and the access is gone", "code_task:create" not in held2 and r2["status"] == 403,
            expect="no Operator permissions; 403 on the Operator route", actual=f"{sorted(held2)[:6]} {r2['status']}")
    bctx.close()

    # Alice: an invitation binds her first SSO identity (§4.8 linking 3).
    inv = K.invite_api(opage, owner, "alice@fxd.test", "viewer", j=j)
    j.check("Owner invites alice@fxd.test as Viewer", inv["status"] == 201, expect="201", actual=inv["status"])
    actx, apage = _keycloak_ctx(env, j)
    _sso_sign_in(apage, web, pid, "alice", pw["alice"], j, "alice-after-oidc")
    am = me(apage).get("body") or {}
    j.check("the invitation binds alice's first SSO sign-in (Viewer)", am.get("email") == "alice@fxd.test"
            and am.get("access_roles") == ["viewer"], expect="alice, roles [viewer]", actual=short({k: am.get(k) for k in ("email", "access_roles")}))
    invs = (api(opage, "GET", "/v1/identity/invitations").get("body") or {}).get("invitations") or []
    j.check("API: the invitation is used up", not any(i.get("email") == "alice@fxd.test" for i in invs),
            expect="no open invitation for alice", actual=[i.get("email") for i in invs])
    actx.close()

    # SCIM: a provisioning key, a provisioned person, disable.
    sk = K.stepped(opage, owner, "POST", f"/v1/identity/providers/{pid}/scim-keys", {"name": "fxd-j7"}, j)
    skey = K.secret((sk.get("body") or {}).get("key")) or ""
    j.check("Owner creates a SCIM key (shown once)", sk["status"] == 201 and skey.startswith("boss_scim_"),
            expect="201 boss_scim_…", actual=sk["status"])
    st_web, _ = K.http("GET", f"{web}/scim/v2/Users", headers={"Authorization": f"Bearer {skey}"})
    j.note(f"SCIM through the console origin answers {st_web} (the console does not forward /scim)")
    cs, cb = s2.stack.bearer("POST", "/scim/v2/Users", skey, {
        "schemas": ["urn:ietf:params:scim:schemas:core:2.0:User"], "userName": "carol@fxd.test",
        "name": {"formatted": "Carol FXD"}, "emails": [{"value": "carol@fxd.test", "primary": True}], "active": True})
    carol_pid = (cb or {}).get("id") if isinstance(cb, dict) else None
    j.check("SCIM provisions carol (pending until her first SSO sign-in)", cs == 201 and carol_pid,
            expect="201", actual=f"{cs} {short(cb, 200)}")
    cctx, cpage = _keycloak_ctx(env, j)
    landed = _sso_sign_in(cpage, web, pid, "carol", pw["carol"], j, "carol-after-oidc")
    if "/callback" in landed:
        j.note("the SSO callback did not redirect; API log: " + _last_exception(s2))
    cm = me(cpage)
    carol_in = cm["status"] == 200 and (cm.get("body") or {}).get("principal_id") == carol_pid
    j.check("the SCIM-provisioned person's first SSO sign-in activates her", carol_in,
            expect="signed in as the SCIM-made carol", actual=f"{landed} me={cm['status']}",
            known="unassigned (new: a SCIM-provisioned person is an unlinked existing account, which social.resolve refuses — and the refusal's audit write fails (identifier not hmac'd), so the callback is a 500)",
            repro="SCIM POST /scim/v2/Users carol@fxd.test, then SSO sign-in as carol → /signin?error=not_invited")
    seq0 = audit_seq(opage)
    ds, db_ = s2.stack.bearer("PATCH", f"/scim/v2/Users/{carol_pid}", skey, {
        "schemas": ["urn:ietf:params:scim:api:messages:2.0:PatchOp"],
        "Operations": [{"op": "replace", "path": "active", "value": False}]})
    j.check("SCIM PATCH active=false answers 200 and active false", ds == 200 and (db_ or {}).get("active") is False,
            expect="200 active false", actual=f"{ds} {short(db_, 200)}")
    u = api(opage, "GET", f"/v1/identity/users/{carol_pid}").get("body") or {}
    j.check("API: carol is disabled in BOSS", u.get("status") == "disabled", expect="status disabled", actual=u.get("status"))
    if carol_in:
        gone = K.raw_fetch(cpage, "GET", "/v1/identity/me", csrf=False)
        j.check("her live session ends", gone["status"] == 401, expect="401", actual=gone["status"])
    landed2 = _sso_sign_in(cpage, web, pid, "carol", pw["carol"], j, "carol-after-scim-disable")
    if "/callback" in landed2:
        j.note("the SSO callback did not redirect; API log: " + _last_exception(s2))
    j.check("a disabled person's SSO sign-in is refused (with words, not a server error)",
            "error=" in landed2 and me(cpage)["status"] == 401,
            expect="/signin?error=…", actual=landed2.split("?")[0] + " (HTTP 500 on the callback)" if "/callback" in landed2 else landed2,
            known="unassigned (new: the SSO callback answers 500 when it refuses an unlinked or disabled account)" if "/callback" in landed2 else None,
            repro="SSO sign-in by a SCIM-provisioned (unlinked) person → GET /v1/auth/sso/<id>/callback → 500")
    ev = audit_has(opage, "scim.user.deactivated", target=carol_pid, since_seq=seq0)
    j.check("audit: scim.user.deactivated", bool(ev), expect="event", actual=short(ev))
    cctx.close()
    nb, nbb = s2.stack.bearer("PATCH", f"/scim/v2/Users/{bob_pid}", skey, {
        "schemas": ["urn:ietf:params:scim:api:messages:2.0:PatchOp"],
        "Operations": [{"op": "replace", "path": "active", "value": False}]})
    j.note(f"SCIM disabling JIT-made bob (not provisioned by SCIM): {nb} {short(nbb, 160)}")
    gs, gb = s2.stack.bearer("POST", "/scim/v2/Groups", skey, {
        "schemas": ["urn:ietf:params:scim:schemas:core:2.0:Group"], "displayName": "fxd-j7-scim-group"})
    sgid = (gb or {}).get("id") if isinstance(gb, dict) else None
    j.check("SCIM creates a group", gs == 201 and sgid, expect="201", actual=f"{gs} {short(gb, 160)}")
    add = K.stepped(opage, owner, "POST", f"/v1/identity/groups/{sgid}/members", {"principal_id": bob_pid}, j)
    j.check("API: the manual groups API refuses editing a SCIM group's members", add["status"] == 409
            and (add.get("body") or {}).get("error") == "idp_group_read_only",
            expect="409 idp_group_read_only", actual=f"{add['status']} {short(add.get('body'))}")
    opage.goto(f"{web}/identity/groups/{sgid}", wait_until="domcontentloaded")
    K.settle(opage)
    j.shot(opage, "scim-group")
    j.check("the SCIM group page shows the source badge, members read-only",
            opage.locator("[data-testid=idp-read-only]").count() > 0 and opage.locator("[data-testid=group-add-member]").count() == 0,
            expect="idp-read-only badge, no Add member", actual=K.body_text(opage, 300))

    # SAML: realm fxdsaml, dave in fxd-builders.
    kc.realm("fxdsaml", https_frontend=True)
    skg = kc.group("fxdsaml", "fxd-builders")
    sp_entity = f"{web}/saml/v2/metadata"
    kc.saml_client("fxdsaml", sp_entity, f"{web}/saml/v2/acs")
    dave = kc.user("fxdsaml", "dave", "dave@fxd.test", pw["dave"], first="Dave", groups=(skg,))
    sp = K.stepped(opage, owner, "POST", "/v1/identity/providers", {
        "protocol": "saml", "preset": "generic", "name": "FXD SAML", "issuer": kc.issuer("fxdsaml"),
        "saml_sso_url": kc.saml_sso_url("fxdsaml"), "saml_certificates": [kc.signing_cert_pem("fxdsaml")],
        "domains": ["fxd.test"], "jit_mode": "domain", "groups_claim": "groups",
        "group_mappings": {"fxd-builders": gid}}, j)
    spid = (sp.get("body") or {}).get("id")
    j.check("Owner registers the SAML provider", sp["status"] == 201 and spid, expect="201",
            actual=f"{sp['status']} {short(sp.get('body'))}", fatal=True)
    meta_st, meta = K.http("GET", f"{web}/saml/v2/metadata?provider_id={spid}")
    j.check("the SP metadata is served on the console origin", meta_st == 200 and "AssertionConsumerService" in str(meta),
            expect="200 metadata XML", actual=f"{meta_st} {str(meta)[:120]}",
            known="unassigned (new: the console forwards /v1/* only — /saml/v2/* on the public origin is the console's own page)",
            repro=f"GET {web}/saml/v2/metadata")
    dctx, dpage = _keycloak_ctx(env, j)
    landed = _sso_sign_in(dpage, web, spid, "dave", pw["dave"], j, "dave-after-saml")
    dm = me(dpage)
    j.check("SAML sign-in lands signed in as dave (JIT)", dm["status"] == 200 and (dm.get("body") or {}).get("email") == "dave@fxd.test",
            expect="me 200 as dave@fxd.test", actual=f"{landed} me={dm['status']}",
            known="unassigned (new: the SAML POST to {origin}/saml/v2/acs reaches the console, not the API)",
            repro=f"SP-initiated SAML through {web}: the IdP posts to {web}/saml/v2/acs")
    if dm["status"] == 200:
        dave_pid = (dm.get("body") or {}).get("principal_id")
        held = perms(dpage, PROJECT)
        j.check("SAML group sync grants Operator access", "code_task:create" in held, expect="code_task:create held",
                actual=sorted(held)[:8])
        kc.leave_group("fxdsaml", dave, skg)
        K.raw_fetch(dpage, "POST", "/v1/auth/logout")
        _sso_sign_in(dpage, web, spid, "dave", pw["dave"], j, "dave-after-saml-group-removed")
        j.check("…and losing the IdP group removes it at the next SAML sign-in", "code_task:create" not in perms(dpage, PROJECT),
                expect="no code_task:create", actual="see screenshot")
        del dave_pid
    else:
        # The assertion itself, posted straight to the API with the browser's own pre-auth cookie,
        # proves (or not) the API half of SAML: Keycloak's real signed response.
        j.note("SAML via the console origin failed; posting Keycloak's response to the API directly")
        def saml_round(shot: str) -> tuple[int, str, bool]:
            """Sign dave in at Keycloak, catch the browser's POST to the console ACS, and post the
            same signed SAMLResponse to the API's ACS with this browser's pre-auth cookie."""
            cctx_, cpage_ = _keycloak_ctx(env, j)
            captured: dict[str, str] = {}

            def grab(route: Any) -> None:
                captured["body"] = route.request.post_data or ""
                route.abort()
            cpage_.route(f"{web}/saml/v2/acs", grab)
            try:
                _sso_sign_in(cpage_, web, spid, "dave", pw["dave"], j, shot)
            except Exception:  # noqa: BLE001
                pass
            import urllib.parse as up
            resp = dict(up.parse_qsl(captured.get("body", ""))).get("SAMLResponse", "")
            pre = {c["name"]: c["value"] for c in cctx_.cookies(web)}.get("__Host-boss_preauth", "")
            K.secret(pre)
            st, hdrs, _ = K.http_raw("POST", f"{s2.api}/saml/v2/acs", data=up.urlencode({"SAMLResponse": resp}).encode(),
                                     headers={"Content-Type": "application/x-www-form-urlencoded",
                                              "Cookie": f"__Host-boss_preauth={pre}"})
            cctx_.close()
            loc = (hdrs.get("location") or [""])[0]
            sess = any(c.startswith("__Host-boss_session=") for c in hdrs.get("set-cookie", []))
            return st, loc, bool(resp) and sess
        st_api, loc, signed_in = saml_round("dave-saml-captured")
        j.check("API half: Keycloak's real signed SAML response, posted to the API's ACS, signs dave in",
                st_api == 303 and "error=" not in loc and signed_in, expect="303 to the console with a session cookie",
                actual=f"{st_api} → {loc} session={signed_in}")
        dave_pid = s2.stack.psql("SELECT principal_id FROM identity_user_identities WHERE issuer = :'i'",
                                 {"i": kc.issuer("fxdsaml")})
        in_group = s2.stack.psql("SELECT source FROM identity_group_members WHERE group_id = :'g' AND principal_id = :'p'",
                                 {"g": gid, "p": dave_pid}) if dave_pid else ""
        j.check("DB: SAML JIT-made dave, linked by (entityID, NameID), synced into the Operator group",
                bool(dave_pid) and in_group == "oidc", expect="a linked principal; membership source oidc",
                actual=f"principal={dave_pid or None} membership={in_group or None}")
        kc.leave_group("fxdsaml", dave, skg)
        st2, loc2, ok2 = saml_round("dave-saml-after-group-removed")
        gone = s2.stack.psql("SELECT count(*) FROM identity_group_members WHERE group_id = :'g' AND principal_id = :'p'",
                             {"g": gid, "p": dave_pid}) if dave_pid else ""
        j.check("…and the next SAML sign-in without the IdP group removes the membership", ok2 and gone == "0",
                expect="signed in; membership gone", actual=f"{st2} {loc2} memberships={gone}")
    dctx.close()


# =================================================================================================
# J14 — network mode (goldeneye rehearsal): the bridge, the claim, Require sign-in
# =================================================================================================

TLS_PORT = 58243
NET_BUG = ("unassigned (new: with BOSS_REQUIRE_API_AUTH=true — compose's only setting — the API's transport "
           "middleware refuses every credential-less request before identity runs, so the network bridge "
           "only ever reaches the @public routes: the claim works, the bridged console does not)")


def _tls_front(dirpath: Path) -> tuple[dict[str, Any], str]:
    """An nginx TLS terminator in front of the console (Origin == Host, https), a throwaway cert,
    and the SPKI hash Chromium is told to trust (so WebAuthn sees a secure, error-free origin)."""
    import base64
    import hashlib
    import subprocess

    dirpath.mkdir(parents=True, exist_ok=True)
    subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "2", "-subj", "/CN=localhost",
                    "-addext", "subjectAltName=DNS:localhost", "-keyout", str(dirpath / "key.pem"),
                    "-out", str(dirpath / "cert.pem")], check=True, capture_output=True)
    (dirpath / "key.pem").chmod(0o644)
    der = subprocess.run(["openssl", "x509", "-in", str(dirpath / "cert.pem"), "-pubkey", "-noout"],
                         capture_output=True, text=True, check=True).stdout
    spki = subprocess.run(["openssl", "pkey", "-pubin", "-outform", "der"], input=der.encode(),
                          capture_output=True, check=True).stdout
    pin = base64.b64encode(hashlib.sha256(spki).digest()).decode()
    (dirpath / "default.conf").write_text(
        "server {\n  listen 443 ssl;\n  ssl_certificate /tls/cert.pem;\n  ssl_certificate_key /tls/key.pem;\n"
        "  location / {\n    proxy_pass http://web:3000;\n    proxy_http_version 1.1;\n"
        "    proxy_set_header Host $http_host;\n    proxy_set_header X-Forwarded-Proto https;\n"
        "    proxy_set_header Upgrade $http_upgrade;\n    proxy_set_header Connection \"upgrade\";\n"
        "    proxy_buffering off;\n    proxy_read_timeout 300s;\n  }\n}\n")
    service = {"image": "nginx:alpine", "depends_on": ["web"], "ports": [f"127.0.0.1:{TLS_PORT}:443"],
               "volumes": [f"{dirpath}/default.conf:/etc/nginx/conf.d/default.conf:ro", f"{dirpath}:/tls:ro"]}
    return service, pin


@journey("J14", "Network mode (goldeneye rehearsal)",
         "Behind an https origin with BOSS_CONSOLE_AUTH=network: before the claim a visitor is the "
         "network operator (banner; identity settings refused); /claim shows the origin it will bind "
         "and needs the confirm and a passkey; after the claim the bridge maps to Operator; Require "
         "sign-in (step-up) disables the network principal and keeps its history.")
def j14(env: Env, j: Journey) -> None:
    tls_dir = Path(env.args.out) / "j14-tls"
    front, pin = _tls_front(tls_dir)
    s2 = _s2(env, j, fresh=True, api_env={"BOSS_CONSOLE_PASSWORD": ""}, web_env={"BOSS_CONSOLE_AUTH": "network"},
             extra={"fxdtls": front}, env_values={"BOSS_CONSOLE_PUBLIC_ORIGIN": ""})
    origin = f"https://localhost:{TLS_PORT}"
    b = env.pw.chromium.launch(headless=not env.args.headed, args=[f"--ignore-certificate-errors-spki-list={pin}"])
    env.shared.setdefault("_cleanup", []).insert(0, lambda: b.close())
    deadline = time.time() + 60
    while time.time() < deadline and K.http("GET", f"http://127.0.0.1:{TLS_PORT}/")[0] == 0:
        time.sleep(2)
    vctx, vpage = K.new_page(b, j)
    vpage.goto(f"{origin}/", wait_until="domcontentloaded")
    K.settle(vpage)
    j.shot(vpage, "network-visitor-home")
    m = me(vpage)
    mb = m.get("body") or {}
    j.check("before the claim a visitor is the network operator", m["status"] == 200 and mb.get("principal_id") == "usr_network_operator"
            and mb.get("network_bound") is True, expect="me = usr_network_operator, network_bound",
            actual=f"{m['status']} {short({k: mb.get(k) for k in ('principal_id', 'network_bound', 'access_roles')})} at {vpage.url}",
            known=NET_BUG, repro=f"compose stack, web BOSS_CONSOLE_AUTH=network, {origin}/ anonymous → GET /v1/identity/me 401 "
                                 "“Valid BOSS API key or OIDC bearer token required.” (main.py transport middleware)")
    kinds = [x.get("kind") for x in mb.get("banners") or []]
    j.check("the banner says anyone who can reach it acts as the operator", "network_bridge" in kinds
            and "acts as the operator" in K.body_text(vpage, 20000), expect="the network banner in /me and on the page",
            actual=f"banners={kinds}", known=NET_BUG)
    g = api(vpage, "POST", "/v1/identity/groups", {"key": "fxd-j14", "name": "fxd-j14"})
    j.check("the network operator may not touch identity settings (only the claim)", g["status"] == 403,
            expect="403 forbidden (bridged, but not for identity settings)", actual=f"{g['status']} {short(g.get('body'))}",
            known=NET_BUG if g["status"] == 401 else None)
    st = K.raw_fetch(vpage, "GET", "/v1/auth/setup/status", csrf=False).get("body") or {}
    j.check("API: the network claim path is offered to the bridged browser", "network" in (st.get("paths") or []),
            expect="paths ∋ network", actual=st)

    # The claim.
    cdp, aid = K.authenticator(vctx, vpage)
    vpage.goto(f"{origin}/claim", wait_until="domcontentloaded")
    vpage.wait_for_selector("[data-testid=claim-origin]", timeout=30000)
    K.settle(vpage)
    j.shot(vpage, "claim-shows-origin")
    shown = vpage.locator("[data-testid=claim-origin] code").inner_text()
    origin_ok = shown == origin
    j.check("/claim shows the origin it will bind to", origin_ok, expect=origin, actual=shown,
            known=None if origin_ok else "unassigned (new: the claim page is rendered server-side and asks /v1/auth/options without the browser's Origin, so it offers to bind the console's internal API URL)",
            repro=f"{origin}/claim on a network-mode compose stack shows “This BOSS will be bound to http://api:8000”")
    p = K.Person(role="owner", email=f"fxd-j14-owner-{int(time.time()) % 100000}@example.test", name="FXD J14 Owner", password="")
    vpage.fill("#claim-name", p.name)
    vpage.fill("#claim-email", p.email)
    if vpage.locator("#claim-seat").count():
        vpage.select_option("#claim-seat", index=0)
    j.check("Claim stays disabled until the origin is confirmed", vpage.locator("[data-testid=claim-submit]").is_disabled(),
            expect="disabled", actual=vpage.locator("[data-testid=claim-submit]").is_disabled())
    vpage.check("[data-testid=claim-origin-confirm]")
    if origin_ok:
        vpage.click("[data-testid=claim-submit]")
        _claim_codes_and_land(vpage, j)
    else:
        # Submitting would bind the wrong origin: the claim's API half with the browser's origin instead.
        j.note("claiming through the API with the browser's own origin (the page offers the wrong one)")
        c = K.raw_fetch(vpage, "POST", "/v1/auth/setup/claim",
                        {"origin": origin, "confirm_origin": origin, "name": p.name, "email": p.email}, csrf=False)
        j.check("API: the network claim with the browser's origin makes the Owner", c["status"] == 200,
                expect="200", actual=f"{c['status']} {short(c.get('body'))}", fatal=True)
        vpage.goto(f"{origin}/enrol", wait_until="domcontentloaded")
        vpage.wait_for_selector("[data-testid=enrol-passkey]", timeout=30000)
        K.settle(vpage)
        vpage.click("[data-testid=enrol-passkey]")
        _claim_codes_and_land_enrol(vpage, j)
    om = me(vpage).get("body") or {}
    j.check("the claimant is the Owner, with a passkey", "owner" in (om.get("access_roles") or [])
            and (om.get("sign_in") or {}).get("passkeys") == 1, expect="owner + passkey",
            actual=short({k: om.get(k) for k in ("access_roles", "sign_in", "principal_id")}))
    opts = K.raw_fetch(vpage, "GET", "/v1/auth/options", csrf=False).get("body") or {}
    j.check("API: the public origin is bound by the claim", opts.get("public_origin") == origin and opts.get("public_origin_source") == "claim",
            expect=f"{origin} (claim)", actual=short({k: opts.get(k) for k in ("public_origin", "public_origin_source")}))

    # After the claim: a cookie-less visitor is the bridge as Operator.
    actx, apage = K.new_page(b, j)
    apage.goto(f"{origin}/", wait_until="domcontentloaded")
    K.settle(apage)
    am = me(apage).get("body") or {}
    j.check("after the claim the bridge maps to Operator", am.get("principal_id") == "usr_network_operator"
            and am.get("access_roles") == ["operator"], expect="usr_network_operator, [operator]",
            actual=short({k: am.get(k) for k in ("principal_id", "access_roles")}), known=NET_BUG)
    j.shot(apage, "bridge-as-operator")

    # Require sign-in (step-up with the passkey).
    K.age_my_session(s2.stack, vpage)
    vpage.goto(f"{origin}/identity/sign-in", wait_until="domcontentloaded")
    vpage.wait_for_selector("[data-testid=require-sign-in]", timeout=30000)
    K.settle(vpage)
    j.shot(vpage, "network-bridge-on")
    vpage.click("[data-testid=require-sign-in]")
    stepped_ = False
    try:
        vpage.wait_for_selector("[data-testid=step-up-passkey]", timeout=8000)
        vpage.click("[data-testid=step-up-passkey]")
        stepped_ = True
    except Exception:  # noqa: BLE001
        pass
    j.check("Require sign-in asks for step-up (passkey first)", stepped_, expect="step-up sheet", actual=stepped_)
    vpage.wait_for_timeout(3000)
    j.shot(vpage, "required-sign-in")
    status = s2.stack.psql("SELECT status FROM identity_principals WHERE id = 'usr_network_operator'")
    j.check("DB: the network principal is disabled", status == "disabled", expect="disabled", actual=status)
    bctx, bpage = K.new_page(b, j)
    bpage.goto(f"{origin}/", wait_until="domcontentloaded")
    K.settle(bpage)
    gone = K.raw_fetch(bpage, "GET", "/v1/identity/me", csrf=False)
    j.check("a visitor now has to sign in (sent to /login)", gone["status"] == 401 and ("/login" in bpage.url or "/signin" in bpage.url),
            expect="401 and /login", actual=f"{gone['status']} {bpage.url}",
            known="unassigned (new: a network-mode console build never routes a visitor to /login, even after Require sign-in: it shows the org picker with an enabled “Onboard organization”)",
            repro=f"{origin}/ in a fresh browser after Require sign-in")
    j.shot(bpage, "visitor-sent-to-login")
    hist = [e for e in K.audit_events(vpage, limit="200") if (e.get("actor") or {}).get("id") == "usr_network_operator"]
    j.check("the network principal's history keeps its name and IP", bool(hist) and all((e.get("actor") or {}).get("name") for e in hist)
            and any(e.get("source") for e in hist), expect="events by the network operator, named, with a source IP",
            actual=short([((e.get("actor") or {}).get("name"), e.get("event_type"), e.get("source")) for e in hist[:4]]))
    for c in (bctx, actx, vctx):
        c.close()


# =================================================================================================
# main
# =================================================================================================

def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--console", required=True)
    ap.add_argument("--api", default="", help="the API's direct origin (bearer keys); default: derived")
    ap.add_argument("--accounts", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--only", default="")
    ap.add_argument("--project", default="bossfxd", help="compose project of the main stack")
    ap.add_argument("--env-file", default="", help="the main stack's env file (default: stack.env next to --accounts)")
    ap.add_argument("--stack2", default="bossfxd2", help="compose project for J1/J2/J7/J14")
    ap.add_argument("--report", default="")
    ap.add_argument("--shots", default="")
    ap.add_argument("--headed", action="store_true")
    ap.add_argument("--drop-people", action="store_true",
                    help="after the journeys, disable and delete the run's own Owner and Admin (fxd-people.json)")
    args = ap.parse_args(argv)
    if not args.api:
        port = int(args.console.rsplit(":", 1)[-1].strip("/"))
        args.api = f"http://127.0.0.1:{port + 5000}"
    if not args.env_file:
        args.env_file = str(Path(args.accounts).parent / "stack.env")
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    shots = Path(args.shots) if args.shots else out / "shots"
    report_path = Path(args.report) if args.report else out / "journeys.json"
    wanted = [x.strip().upper() for x in args.only.split(",") if x.strip()] or list(JOURNEYS)

    from playwright.sync_api import sync_playwright

    results: list[dict[str, Any]] = []
    started = time.time()
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=not args.headed)
        env = Env(args, browser)
        env.pw = pw
        try:
            for jid in sorted(wanted, key=lambda x: int(x[1:])):
                if jid not in JOURNEYS:
                    print(f"unknown journey {jid}", file=sys.stderr)
                    continue
                title, proves, fn = JOURNEYS[jid]
                j = Journey(jid, title, proves, shots)
                print(f"=== {jid}: {title}", flush=True)
                try:
                    fn(env, j)
                except K.JourneyAbort as exc:
                    j.error = f"stopped at: {exc}"
                except Exception as exc:  # noqa: BLE001
                    j.error = K.fmt_exc(exc)
                    j.shoot_open_pages()
                    j.check("journey ran to the end", False, expect="no exception", actual=j.error[-500:])
                j.finished = time.time()
                results.append(j.as_dict())
                print(f"=== {jid}: {j.status}", flush=True)
                _write(report_path, args, results, started)
        finally:
            oc = env.shared.get("_owner_ctx")
            if oc:
                try:
                    oc[0].close()
                except Exception:  # noqa: BLE001
                    pass
            if args.drop_people:
                env.shared.setdefault("_cleanup", []).append(lambda: _drop_people(env))
            for fn in env.shared.get("_cleanup", []):
                try:
                    fn()
                except Exception as exc:  # noqa: BLE001
                    print(f"cleanup failed: {K.scrub(str(exc))[:200]}", file=sys.stderr)
            browser.close()
    _write(report_path, args, results, started)
    failed = [r["id"] for r in results if r["status"] == "fail"]
    print(json.dumps({r["id"]: r["status"] for r in results}))
    return 1 if failed else 0


def _drop_people(env: Env) -> None:
    """Disable and delete the run's own Owner and Admin: the Admin by the run's Owner, the Owner
    by seed-owner (one sign-in)."""
    people = env._load_people()
    if not people:
        return
    if "admin" in people and "owner" in people:
        h, page = env.owner_page()
        aid = people["admin"].principal_id
        K.stepped(page, env.owner, "POST", f"/v1/identity/users/{aid}/disable")
        K.stepped(page, env.owner, "DELETE", f"/v1/identity/users/{aid}")
        h.close()
    if "owner" in people:
        ctx, page = K.signed_in_page(env.browser, env.web, env.A["owner"])
        oid = people["owner"].principal_id
        d1 = K.stepped(page, env.A["owner"], "POST", f"/v1/identity/users/{oid}/disable")
        d2 = K.stepped(page, env.A["owner"], "DELETE", f"/v1/identity/users/{oid}")
        print(f"dropped the run's Owner: disable {d1['status']}, delete {d2['status']}")
        ctx.close()
    env._people_file().unlink()


def _write(path: Path, args: argparse.Namespace, results: list[dict[str, Any]], started: float) -> None:
    doc = {"console": args.console, "api": args.api, "project": args.project, "stack2": args.stack2,
           "started": time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(started)),
           "seconds": round(time.time() - started, 1),
           "summary": {s: [r["id"] for r in results if r["status"] == s] for s in ("pass", "xfail", "fail")},
           "journeys": results}
    path.write_text(K.scrub(json.dumps(doc, indent=2, default=str)))


if __name__ == "__main__":
    sys.exit(main())
