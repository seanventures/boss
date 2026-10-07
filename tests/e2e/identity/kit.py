"""The shared kit for the real-stack identity journeys (journeys_real.py).

A journey drives a real browser through the console AND asserts the API half: status codes,
audit events (/v1/identity/audit), and database state (psql in the stack's db container). Every
check is recorded; a check that fails because of a known, assigned product bug is recorded as
`xfail` naming the fix item — the assertion itself is never weakened.

SECRETS. Passwords, TOTP secrets, recovery codes, keys, setup/invite/reset links are registered
with `secret()` the moment they exist; everything that reaches the report or stdout goes through
`scrub()`. Nothing here prints a credential.
"""
from __future__ import annotations

import contextlib
import http.client as http_client
import json
import os
import re
import secrets as _secrets
import subprocess
import sys
import time
import traceback
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterator, Optional

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from e2e.harness import identity as I  # noqa: E402

REPO = Path(__file__).resolve().parents[3]
COMPOSE_FILE = REPO / "scripts" / "install" / "community-compose.yml"

# ---- secrets ------------------------------------------------------------------------------------

_SECRETS: set[str] = set()
_PATTERNS = [
    (re.compile(r"#t=[A-Za-z0-9_\-]+"), "#t=<redacted>"),
    (re.compile(r"boss_(pat|sak|scim|egr)_[A-Za-z0-9]+"), r"boss_\1_<redacted>"),
    (re.compile(r"\bbss_[A-Za-z0-9_\-]{10,}"), "bss_<redacted>"),
    (re.compile(r"otpauth://[^\s\"']+"), "otpauth://<redacted>"),
    (re.compile(r"\"(password|secret|key|token|code|setup_token|legacy_password|client_secret|link)\"\s*:\s*\"[^\"]*\""),
     r'"\1": "<redacted>"'),
]


def secret(value: Optional[str]) -> Optional[str]:
    """Register a secret so scrub() removes it from anything reported."""
    if value and len(value) >= 6:
        _SECRETS.add(value)
    return value


def scrub(text: Any) -> str:
    s = text if isinstance(text, str) else json.dumps(text, default=str)
    for v in sorted(_SECRETS, key=len, reverse=True):
        s = s.replace(v, "<secret>")
    for rx, rep in _PATTERNS:
        s = rx.sub(rep, s)
    return s


def short(obj: Any, n: int = 400) -> str:
    return scrub(obj)[:n]


def strong_password(tag: str) -> str:
    return secret(f"fxd-{tag}-" + _secrets.token_urlsafe(15))


# ---- the stack ----------------------------------------------------------------------------------

@dataclass
class Stack:
    project: str
    web: str
    api: str
    env_file: Path

    @property
    def db_container(self) -> str:
        return f"{self.project}-db-1"

    def host(self) -> I.RunHost:
        return I.compose_host(self.project, COMPOSE_FILE, self.env_file)

    def psql(self, sql: str, params: Optional[dict[str, str]] = None) -> str:
        """One SQL statement in the stack's database, unaligned output. `params` become psql
        variables (:'name' in the SQL), so values are quoted by psql, never by us."""
        cmd = ["docker", "exec", "-i", self.db_container, "psql", "-U", "boss", "-d", "boss", "-At",
               "-v", "ON_ERROR_STOP=1"]
        for k, v in (params or {}).items():
            cmd += ["-v", f"{k}={v}"]
        done = subprocess.run(cmd, input=sql, capture_output=True, text=True, timeout=60)
        if done.returncode != 0:
            raise RuntimeError("psql failed: " + scrub(done.stderr.strip())[-300:])
        return done.stdout.strip()

    def bearer(self, method: str, path: str, key: str, body: Any = None,
               headers: Optional[dict[str, str]] = None, base: Optional[str] = None) -> tuple[int, Any]:
        """A call to the API with a bearer credential (keys are API-port credentials)."""
        data = None if body is None else json.dumps(body).encode()
        h = {"Authorization": f"Bearer {key}", "Accept": "application/json"}
        if body is not None:
            h["Content-Type"] = "application/scim+json" if path.startswith("/scim/") else "application/json"
        h.update(headers or {})
        return http(method, (base or self.api).rstrip("/") + path, data=data, headers=h)


def http(method: str, url: str, *, data: Optional[bytes] = None,
         headers: Optional[dict[str, str]] = None, timeout: int = 60) -> tuple[int, Any]:
    req = urllib.request.Request(url, data=data, method=method, headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode() or ""
            status = resp.status
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode(errors="replace") or ""
        status = exc.code
    except (urllib.error.URLError, ConnectionError, OSError, http_client.HTTPException) as exc:
        return 0, f"{type(exc).__name__}"
    try:
        return status, json.loads(raw) if raw else None
    except ValueError:
        return status, raw[:2000]


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a: Any, **kw: Any) -> None:  # noqa: D401
        return None


def http_raw(method: str, url: str, *, data: Optional[bytes] = None, headers: Optional[dict[str, str]] = None,
             timeout: int = 60) -> tuple[int, dict[str, list[str]], str]:
    """One request, redirects NOT followed: (status, headers (lowercased, multi), body)."""
    opener = urllib.request.build_opener(_NoRedirect)
    req = urllib.request.Request(url, data=data, method=method, headers=headers or {})
    try:
        resp = opener.open(req, timeout=timeout)
        status, hdrs, body = resp.status, resp.headers, resp.read().decode(errors="replace")
    except urllib.error.HTTPError as exc:
        status, hdrs, body = exc.code, exc.headers, exc.read().decode(errors="replace")
    out: dict[str, list[str]] = {}
    for k, v in hdrs.items():
        out.setdefault(k.lower(), []).append(v)
    return status, out, body


# ---- people -------------------------------------------------------------------------------------

@dataclass
class Person:
    """A person a journey made for itself (never a seed-* account)."""
    role: str
    email: str
    name: str
    password: str
    principal_id: str = ""
    totp_secret: Optional[str] = None
    recovery_codes: list[str] = field(default_factory=list)

    def __repr__(self) -> str:
        return f"Person(role={self.role!r}, email={self.email!r}, principal_id={self.principal_id!r})"

    __str__ = __repr__

    def account(self) -> I.Account:
        return I.Account(role=self.role, email=self.email, password=self.password,
                         principal_id=self.principal_id, totp_secret=self.totp_secret)


def as_account(who: Any) -> I.Account:
    return who.account() if isinstance(who, Person) else who


# ---- the recorder -------------------------------------------------------------------------------

class PageOnly:
    """A (ctx, page) pair over a SHARED context: close() closes only the page."""

    def __init__(self, page: Any):
        self.page = page

    def close(self) -> None:
        try:
            self.page.close()
        except Exception:  # noqa: BLE001
            pass


class JourneyAbort(Exception):
    pass


class Journey:
    """One journey's record: checks (pass/fail/xfail), notes, screenshots."""

    def __init__(self, jid: str, title: str, proves: str, shots_root: Path):
        self.id, self.title, self.proves = jid, title, proves
        self.shots = shots_root / jid
        self.shots.mkdir(parents=True, exist_ok=True)
        for old in self.shots.glob("*.png"):      # a rerun's pictures replace the last run's
            old.unlink()
        self.checks: list[dict[str, Any]] = []
        self.notes: list[str] = []
        self.error: Optional[str] = None
        self.started = time.time()
        self.finished: Optional[float] = None
        self._n = 0
        self.pages: list[Any] = []

    # -- recording
    def note(self, msg: str) -> None:
        line = scrub(msg)
        self.notes.append(line)
        print(f"[{self.id}] {line}", flush=True)

    def check(self, name: str, ok: bool, *, expect: str, actual: Any = "", known: Optional[str] = None,
              fatal: bool = False, repro: str = "") -> bool:
        status = "pass" if ok else ("xfail" if known else "fail")
        rec = {"name": name, "status": status, "expect": scrub(expect), "actual": short(actual, 700)}
        if known and not ok:
            rec["known"] = known
        if ok and known:
            rec["unexpected_pass"] = known      # the known bug looks fixed: say so
        if repro and not ok:
            rec["repro"] = scrub(repro)
        self.checks.append(rec)
        mark = {"pass": "ok  ", "fail": "FAIL", "xfail": "xfail"}[status]
        print(f"[{self.id}] {mark} {name}" + ("" if ok else f" — expected {rec['expect']}; got {rec['actual'][:300]}")
              + (f" (known: {known})" if known and not ok else ""), flush=True)
        if fatal and not ok:
            raise JourneyAbort(name)
        return ok

    def shot(self, page: Any, name: str) -> None:
        self._n += 1
        path = self.shots / f"{self._n:02d}-{name}.png"
        try:
            page.screenshot(path=str(path), full_page=True)
        except Exception as exc:  # noqa: BLE001
            self.note(f"screenshot {name} failed: {type(exc).__name__}")

    def shoot_open_pages(self) -> None:
        """On a failure: a screenshot and the visible text of every page still open."""
        for n, pg in enumerate(self.pages):
            try:
                if not pg.is_closed():
                    self.shot(pg, f"fail-page{n}")
                    self.note(f"open page {n} at {pg.url.split('#')[0]}: " + body_text(pg, 300).replace("\n", " | "))
            except Exception:  # noqa: BLE001
                pass

    @contextlib.contextmanager
    def tolerate(self, name: str, known: Optional[str] = None) -> Iterator[None]:
        """A step whose exception is recorded (as xfail when `known`, else fail) and the journey
        continues with the next step."""
        try:
            yield
        except JourneyAbort:
            raise
        except Exception as exc:  # noqa: BLE001
            self.check(name, False, expect="the step completes",
                       actual=f"{type(exc).__name__}: {str(exc)[:400]}", known=known)

    @property
    def status(self) -> str:
        if self.error and not any(c["status"] == "fail" for c in self.checks):
            return "fail"
        sts = {c["status"] for c in self.checks}
        if "fail" in sts:
            return "fail"
        if "xfail" in sts:
            return "xfail"
        return "pass" if sts else "fail"

    def as_dict(self) -> dict[str, Any]:
        return {"id": self.id, "title": self.title, "proves": self.proves, "status": self.status,
                "error": self.error, "seconds": round((self.finished or time.time()) - self.started, 1),
                "shots": str(self.shots), "checks": self.checks, "notes": self.notes[-80:]}


# ---- browser helpers ----------------------------------------------------------------------------

def new_page(browser: Any, j: Optional[Journey] = None, *, https: bool = False,
             storage: Optional[str] = None) -> tuple[Any, Any]:
    kw: dict[str, Any] = {"viewport": {"width": 1360, "height": 900}}
    if https:
        kw["ignore_https_errors"] = True
    if storage:
        kw["storage_state"] = storage
    ctx = browser.new_context(**kw)
    page = ctx.new_page()
    if j is not None:
        j.pages.append(page)
        page.on("response", lambda r: (r.status >= 500 and ("/v1/" in r.url or "/scim/" in r.url))
                and j.note(f"HTTP {r.status} {r.request.method} {r.url.split('?')[0][-90:]}"))
    return ctx, page


def authenticator(ctx: Any, page: Any) -> tuple[Any, str]:
    """A CDP virtual WebAuthn authenticator (platform, resident keys, user verification)."""
    cdp = ctx.new_cdp_session(page)
    cdp.send("WebAuthn.enable")
    aid = cdp.send("WebAuthn.addVirtualAuthenticator", {"options": {
        "protocol": "ctap2", "transport": "internal", "hasResidentKey": True,
        "hasUserVerification": True, "isUserVerified": True, "automaticPresenceSimulation": True}})["authenticatorId"]
    return cdp, aid


def api(page: Any, method: str, path: str, body: Any = None) -> dict[str, Any]:
    """A same-origin console call from `page` (cookie + CSRF). Retries when the page was
    navigating (a client redirect after sign-in destroys the evaluation context)."""
    return _settled(page, lambda: I.api_in_page(page, method, path, body))


def raw_fetch(page: Any, method: str, path: str, body: Any = None, csrf: bool = True) -> dict[str, Any]:
    """A same-origin call that does NOT fetch /me first (so it never touches the session
    except through this one request); csrf=False sends no CSRF header."""
    return _settled(page, lambda: page.evaluate("""async ([m, p, b, c]) => {
        const h = {'Content-Type': 'application/json'};
        if (c) { try { const me = await (await fetch('/v1/identity/me')).json(); h['X-Boss-CSRF'] = me.csrf_token || ''; } catch (e) {} }
        const r = await fetch(p, {method: m, headers: h, credentials: 'same-origin', body: b === null ? undefined : JSON.stringify(b)});
        let j = null; try { j = await r.json(); } catch (e) {}
        return {status: r.status, body: j};
    }""", [method, path, body, csrf]))


def _settled(page: Any, call: Callable[[], Any]) -> Any:
    for attempt in range(4):
        try:
            return call()
        except Exception as exc:  # noqa: BLE001
            if not any(w in str(exc) for w in ("context was destroyed", "navigat", "Failed to fetch")) or attempt == 3:
                raise
            page.wait_for_load_state("domcontentloaded")
            page.wait_for_timeout(700)
    raise RuntimeError("unreachable")


def code_box(page: Any, label: str = "Authenticator code") -> Any:
    return page.locator(f'[role=group][aria-label="{label}"] input')


def spend_step(secret_b32: str) -> None:
    I._LAST_STEP[secret_b32] = int(time.time() // 30)


def step_up(page: Any, who: Any, j: Optional[Journey] = None) -> dict[str, Any]:
    """Step up through the API with a fresh TOTP code (retrying a step another process spent)."""
    acct = as_account(who)
    last: dict[str, Any] = {}
    for _ in range(3):
        last = api(page, "POST", "/v1/auth/step-up", {"method": "totp", "code": I.fresh_totp(acct.totp_secret)})
        if last["status"] == 200:
            return last
        spend_step(acct.totp_secret)
    if j:
        j.note(f"step-up failed: {last.get('status')} {short(last.get('body'))}")
    return last


def stepped(page: Any, who: Any, method: str, path: str, body: Any = None,
            j: Optional[Journey] = None) -> dict[str, Any]:
    """A call that may need a fresh step-up: on 401 step_up_required, step up and retry once."""
    r = api(page, method, path, body)
    if r["status"] == 401 and (r.get("body") or {}).get("error") == "step_up_required":
        step_up(page, who, j)
        r = api(page, method, path, body)
    return r


def answer_step_up_sheet(page: Any, who: Any, timeout_ms: int = 8000) -> bool:
    """When the console's step-up sheet is open, answer it with the authenticator code."""
    acct = as_account(who)
    box = code_box(page)
    try:
        box.first.wait_for(state="visible", timeout=timeout_ms)
    except Exception:  # noqa: BLE001
        return False
    for attempt in range(2):
        box.first.fill(I.fresh_totp(acct.totp_secret))
        page.wait_for_timeout(2500)
        if not box.count() or not box.first.is_visible():
            return True
        spend_step(acct.totp_secret)
    return not (box.count() and box.first.is_visible())


def age_mfa(stack: Stack, principal_id: str, minutes: int = 11, session_id: Optional[str] = None) -> None:
    """Make MFA older than the step-up window (10 min), so the next step-up permission really
    asks. With `session_id`, only that session (use it for seed accounts other walks share);
    without, every live session of a person this run made."""
    if session_id:
        stack.psql("UPDATE identity_sessions SET mfa_at = now() - make_interval(mins => :mins) "
                   "WHERE id = :'sid' AND principal_id = :'pid'",
                   {"pid": principal_id, "sid": session_id, "mins": str(minutes)})
        return
    stack.psql("UPDATE identity_sessions SET mfa_at = now() - make_interval(mins => :mins) "
               "WHERE principal_id = :'pid' AND revoked_at IS NULL AND mfa_at IS NOT NULL",
               {"pid": principal_id, "mins": str(minutes)})


def age_my_session(stack: Stack, page: Any, minutes: int = 11) -> None:
    """Age only the session `page` is signed in with."""
    body = me(page).get("body") or {}
    age_mfa(stack, body["principal_id"], minutes, session_id=body["session_id"])


def audit_events(page: Any, **filters: str) -> list[dict[str, Any]]:
    q = "&".join(f"{k}={urllib.request.quote(str(v))}" for k, v in filters.items())
    r = api(page, "GET", "/v1/identity/audit" + (f"?{q}" if q else ""))
    return (r.get("body") or {}).get("events") or [] if r["status"] == 200 else []


def audit_has(page: Any, event_type: str, *, target: Optional[str] = None, actor: Optional[str] = None,
              since_seq: int = 0, limit: int = 200) -> Optional[dict[str, Any]]:
    for e in audit_events(page, event=event_type, limit=str(limit)):
        raw = e.get("raw") or {}
        if e.get("seq", 0) <= since_seq:
            continue
        if raw.get("event_type") != event_type and e.get("event_type") != event_type:
            continue
        if target and raw.get("target_id") != target:
            continue
        if actor and raw.get("actor_principal_id") != actor:
            continue
        return e
    return None


def audit_seq(page: Any) -> int:
    ev = audit_events(page, limit="1")
    return int(ev[0]["seq"]) if ev else 0


def me(page: Any) -> dict[str, Any]:
    return api(page, "GET", "/v1/identity/me")


def perms(page: Any, project_id: Optional[str] = None) -> set[str]:
    r = api(page, "GET", "/v1/identity/me/permissions" + (f"?project_id={project_id}" if project_id else ""))
    return set((r.get("body") or {}).get("permissions") or []) if r["status"] == 200 else set()


def body_text(page: Any, n: int = 4000) -> str:
    try:
        return page.inner_text("body")[:n]
    except Exception:  # noqa: BLE001
        return ""


# ---- making people ------------------------------------------------------------------------------

def invite_api(page: Any, inviter: Any, email: str, role_key: str, *, group_ids: Optional[list[str]] = None,
               j: Optional[Journey] = None) -> dict[str, Any]:
    r = stepped(page, inviter, "POST", "/v1/identity/users",
                {"email": email, "bindings": [{"role_key": role_key, "scope_type": "org"}],
                 "group_ids": group_ids or []}, j)
    link = (r.get("body") or {}).get("link")
    secret(link)
    return r


def token_of(link: str) -> str:
    return secret(link.split("#t=", 1)[1]) or ""


def accept_api(page: Any, web: str, link: str, name: str, password: str) -> dict[str, Any]:
    """Accept an invitation from a fresh browser at the console origin (the accept page's call)."""
    page.goto(f"{web}/login", wait_until="domcontentloaded")
    return raw_fetch(page, "POST", "/v1/auth/invitations/accept",
                     {"token": token_of(link), "name": name, "password": password}, csrf=False)


def enrol_totp_api(page: Any, person: Person) -> dict[str, Any]:
    r = api(page, "POST", "/v1/identity/me/mfa/totp")
    sec = (r.get("body") or {}).get("secret")
    if r["status"] != 200 or not sec:
        return r
    person.totp_secret = secret(sec.replace(" ", ""))
    c = api(page, "POST", "/v1/identity/me/mfa/totp/confirm", {"code": I.fresh_totp(person.totp_secret)})
    codes = (c.get("body") or {}).get("recovery_codes") or []
    for code in codes:
        secret(code)
    person.recovery_codes = list(codes)
    return c


def make_person(stack: Stack, browser: Any, inviter_page: Any, inviter: Any, role_key: str, tag: str,
                j: Journey, *, totp: bool = True, group_ids: Optional[list[str]] = None,
                keep_open: bool = False) -> tuple[Person, Any, Any]:
    """Invite → accept → (TOTP enrol + recovery codes). Returns (person, ctx, page) signed in;
    ctx is closed unless keep_open."""
    stamp = time.strftime("%H%M%S") + _secrets.token_hex(2)
    p = Person(role=role_key, email=f"fxd-{tag}-{stamp}@example.test", name=f"FXD {tag.upper()} {stamp}",
               password=strong_password(tag))
    inv = invite_api(inviter_page, inviter, p.email, role_key, group_ids=group_ids, j=j)
    if inv["status"] != 201 or not (inv.get("body") or {}).get("link"):
        raise RuntimeError(f"invite {role_key} answered {inv['status']} {short(inv.get('body'))}")
    ctx, page = new_page(browser, j)
    acc = accept_api(page, stack.web, inv["body"]["link"], p.name, p.password)
    if acc["status"] != 200:
        raise RuntimeError(f"accept answered {acc['status']} {short(acc.get('body'))}")
    m = me(page)
    p.principal_id = (m.get("body") or {}).get("principal_id", "")
    if totp:
        c = enrol_totp_api(page, p)
        if c["status"] != 200:
            raise RuntimeError(f"TOTP enrol answered {c['status']} {short(c.get('body'))}")
        if not p.recovery_codes:
            rc = api(page, "POST", "/v1/identity/me/recovery-codes")
            p.recovery_codes = [secret(x) for x in ((rc.get("body") or {}).get("recovery_codes") or [])]
    j.note(f"made {p} (totp={bool(p.totp_secret)}, recovery codes={len(p.recovery_codes)})")
    if not keep_open:
        raw_fetch(page, "POST", "/v1/auth/logout")       # leave no stray session behind
        ctx.close()
        return p, None, None
    return p, ctx, page


def login_form(page: Any, web: str, email: str, password: str) -> None:
    """Open /login, wait until the page is HYDRATED (a click before that submits the bare HTML
    form as a GET and reloads /login), then email + password."""
    page.goto(f"{web}/login", wait_until="domcontentloaded")
    page.wait_for_selector("#email", timeout=30000)
    settle(page, 10000)
    page.fill("#email", email)
    if page.locator("#password").count() == 0:
        page.click("[data-testid=email-submit]")
        page.wait_for_selector("#password", timeout=15000)
    page.fill("#password", password)
    page.click("[data-testid=email-submit]")


def sign_in(page: Any, web: str, who: Any, next_path: str = "/") -> None:
    """Password (+ TOTP) through the real /login. Retried: other walks sign in as the same seeded
    people and can spend the current TOTP step first (the API refuses a replayed step)."""
    acct = as_account(who)
    last = ""
    for attempt in range(4):
        login_form(page, web, acct.email, acct.password)
        box = code_box(page)
        deadline = time.time() + 30
        while time.time() < deadline:
            if "/login" not in page.url and "/signin" not in page.url:
                settle(page)
                return
            if box.count() and box.first.is_visible():
                if not acct.totp_secret:
                    raise I.IdentitySetupError(f"{acct.role}: asked for a code but has no TOTP secret")
                box.first.fill(I.fresh_totp(acct.totp_secret))
                try:
                    page.wait_for_url(lambda u: "/login" not in u, timeout=10000)
                    settle(page)
                    return
                except Exception:  # noqa: BLE001
                    spend_step(acct.totp_secret)
                    last = "code refused"
                    break
            err = page.locator("#signin-error, .idn-err[role=alert]")
            if err.count() and err.first.is_visible():
                last = err.first.inner_text()[:120]
                break
            page.wait_for_timeout(250)
        else:
            last = f"no progress at {page.url}"
        page.wait_for_timeout(1000)
    raise I.IdentitySetupError(f"{acct.role}: sign-in failed after retries ({last})")


def settle(page: Any, ms: int = 15000) -> None:
    """Let a client-side redirect finish before the next same-origin call."""
    try:
        page.wait_for_load_state("networkidle", timeout=ms)
    except Exception:  # noqa: BLE001
        pass
    page.wait_for_timeout(500)


def signed_in_page(browser: Any, web: str, who: Any, j: Optional[Journey] = None,
                   https: bool = False) -> tuple[Any, Any]:
    ctx, page = new_page(browser, j, https=https)
    sign_in(page, web, who)
    page.wait_for_load_state("domcontentloaded")
    page.wait_for_timeout(800)
    return ctx, page


def wait_for(fn: Callable[[], Any], timeout_s: float = 15, every_s: float = 0.5) -> Any:
    deadline = time.time() + timeout_s
    last = None
    while time.time() < deadline:
        last = fn()
        if last:
            return last
        time.sleep(every_s)
    return last


def fmt_exc(exc: BaseException) -> str:
    """The exception's words plus where in the journey it happened (not Playwright's internals)."""
    frames = [f for f in traceback.extract_tb(exc.__traceback__) if "/e2e/identity/" in f.filename]
    where = " <- ".join(f"{Path(f.filename).name}:{f.lineno} {f.line}" for f in reversed(frames[-2:]))
    return scrub(f"{type(exc).__name__}: {str(exc).strip()[:600]} @ {where}")[:1200]


def write_private(path: Path, content: str) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as fh:
        fh.write(content)
