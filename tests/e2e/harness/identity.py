"""Signing in to a BOSS that has identity (IDENTITY.md §4): claim the first Owner, seed one account
per role, and sign a browser in as any of them through the real sign-in page.

WHY THIS EXISTS. The console used to sit behind one shared password (`app/login/page.tsx`,
its password field). That page is gone: an installed BOSS now has people. The first Owner claims
the install (a setup link from the host shell, or the legacy shared password), and everyone after
signs in with email + password + a second factor (Owner and Admin must have one). A walk that
types the old console password into a page that no longer exists times out on every screen.

THE ACCOUNTS. `python -m boss_engine.identity.cli seed-test-accounts` (host shell only, never
reachable over HTTP) makes one person per built-in role, a custom `developer` role and person,
and a service account with a key, and writes their credentials ONLY to a 0600 file. This module
reads that file; it never prints a password, a TOTP secret or a key, and nothing it raises
carries one.

    claim_owner(console_origin, setup_token, OWNER)      # once, on a fresh install
    seed_accounts(run_host, OWNER.email, out)             # one per role, into `out` (0600)
    accounts = load_accounts(out)
    sign_in(page, console_origin, accounts["admin"])      # the real /login, password + TOTP

`run_host` is how the stack runs a host-shell command: the local stack runs Python with the
services' environment; a compose stack runs it inside the api container (`compose_host`).
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import re
import struct
import subprocess
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Optional

#: The person who claims a disposable stack. Never a real address.
@dataclass(frozen=True)
class Claimant:
    name: str = "E2E Owner"
    email: str = "e2e-owner@example.test"
    password: str = ""          # filled per stack (claim_password()); never a constant


@dataclass(frozen=True)
class Account:
    role: str
    email: str
    password: str
    principal_id: str
    totp_secret: Optional[str] = None

    def __repr__(self) -> str:      # a traceback or a log line must never carry a credential
        return f"Account(role={self.role!r}, email={self.email!r}, principal_id={self.principal_id!r})"

    __str__ = __repr__


@dataclass(frozen=True)
class ServiceAccount:
    name: str
    principal_id: str
    role: str
    scopes: tuple[str, ...]
    key: str

    def __repr__(self) -> str:
        return f"ServiceAccount(name={self.name!r}, role={self.role!r}, scopes={self.scopes!r})"

    __str__ = __repr__


class IdentitySetupError(RuntimeError):
    """A step of claim/seed/sign-in that did not work, in words with no secret in them."""


# ---- TOTP (RFC 6238), so a walk can answer the second factor ----------------------------------

_LAST_STEP: dict[str, int] = {}


def totp(secret_b32: str, at: Optional[float] = None) -> str:
    clean = secret_b32.replace(" ", "").upper()
    key = base64.b32decode(clean + "=" * (-len(clean) % 8))
    counter = int((at if at is not None else time.time()) // 30)
    digest = hmac.new(key, struct.pack(">Q", counter), hashlib.sha1).digest()
    o = digest[-1] & 0x0F
    return f"{(struct.unpack('>I', digest[o:o + 4])[0] & 0x7FFFFFFF) % 1_000_000:06d}"


def fresh_totp(secret_b32: str) -> str:
    """A code from a step this process has not spent yet: the API refuses a replayed step
    (identity_totp.last_step), so two sign-ins of one account inside 30 s must wait a step."""
    step = int(time.time() // 30)
    if _LAST_STEP.get(secret_b32, -1) >= step:
        time.sleep(31 - (time.time() % 30))
        step = int(time.time() // 30)
    _LAST_STEP[secret_b32] = step
    return totp(secret_b32)


# ---- host-shell runners -------------------------------------------------------------------------

RunHost = Callable[[list[str]], "subprocess.CompletedProcess[str]"]


def local_host(env: dict[str, str], cwd: Path) -> RunHost:
    """Run `python -m <module> ...` on this machine with the services' own environment."""
    import sys

    def run(argv: list[str]) -> "subprocess.CompletedProcess[str]":
        return subprocess.run([sys.executable, *argv], cwd=str(cwd), env={**env, "BOSS_ENVIRONMENT": "e2e"},
                              capture_output=True, text=True, timeout=180)
    return run


def compose_host(project: str, compose_file: Path, env_file: Optional[Path] = None,
                 service: str = "api") -> RunHost:
    """Run `python -m <module> ...` inside a compose stack's api container, as the image's
    `boss` user, as an operator's `docker compose exec` would."""
    def run(argv: list[str]) -> "subprocess.CompletedProcess[str]":
        cmd = ["docker", "compose", "-p", project, "-f", str(compose_file)]
        if env_file:
            cmd += ["--env-file", str(env_file)]
        cmd += ["exec", "-T", "-u", "boss", "-e", "BOSS_ENVIRONMENT=e2e", service, "python", *argv]
        return subprocess.run(cmd, capture_output=True, text=True, timeout=180)
    return run


# ---- claim --------------------------------------------------------------------------------------

def setup_token(run_host: RunHost) -> str:
    """A fresh setup link's token, from the host shell (`claim setup-link`). The link carries
    it in the fragment (`/claim#t=...`); only the token is returned."""
    done = run_host(["-m", "boss_engine.identity.claim", "setup-link"])
    found = re.search(r"#t=([A-Za-z0-9_\-]+)", done.stdout or "")
    if done.returncode != 0 or not found:
        raise IdentitySetupError("no setup link: " + (done.stderr or done.stdout or "").strip()[-300:]
                                 .split("#t=")[0])
    return found.group(1)


def setup_status(console_origin: str) -> dict[str, Any]:
    with urllib.request.urlopen(f"{console_origin.rstrip('/')}/v1/auth/setup/status", timeout=30) as resp:
        return json.loads(resp.read().decode() or "{}")


def claim_password() -> str:
    import secrets

    return "e2e-" + secrets.token_urlsafe(18)


def claim_owner(console_origin: str, token: str, who: Claimant) -> None:
    """Claim the install through the CONSOLE's origin, as the claim page does. The claim binds
    the public origin to the Origin it was made from (H2), so it must be the console's — a
    claim made against the API's own port would bind that, and every later sign-in at the
    console would be refused as cross-origin."""
    origin = console_origin.rstrip("/")
    body = json.dumps({"name": who.name, "email": who.email, "password": who.password,
                       "setup_token": token}).encode()
    req = urllib.request.Request(f"{origin}/v1/auth/setup/claim", data=body, method="POST",
                                 headers={"Content-Type": "application/json", "Origin": origin})
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            if resp.status >= 300:
                raise IdentitySetupError(f"claim answered {resp.status}")
    except urllib.error.HTTPError as exc:
        code = ""
        try:
            code = json.loads(exc.read().decode() or "{}").get("error", "")
        except Exception:  # noqa: BLE001
            pass
        raise IdentitySetupError(f"claim refused: {exc.code} {code}") from None


# ---- seed ---------------------------------------------------------------------------------------

def seed_accounts(run_host: RunHost, owner_email: str, out: Path, *, org: str = "org_boss",
                  copy_out: Optional[Callable[[str, Path], None]] = None) -> Path:
    """`seed-test-accounts` as the claimed Owner. With `copy_out` (a compose stack) the file is
    written inside the container, copied to `out` (0600) and removed there."""
    inner = str(out) if copy_out is None else f"/tmp/seed-{os.getpid()}.json"
    # The `seed-test-accounts` CLI command left the runtime (8bf0581b7: contributor QA tools are
    # not shipped); the seeding function is still there, so the harness calls it directly.
    done = run_host(["-c", (
        "import sys\nfrom boss_engine.db import begin\n"
        "from boss_engine.identity.seed_accounts import seed_test_accounts\n"
        "with begin() as db:\n"
        "    seed_test_accounts(db, org_id=sys.argv[1], owner_email=sys.argv[2], out=sys.argv[3])\n"),
        org, owner_email, inner])
    if done.returncode != 0:
        raise IdentitySetupError("seed-test-accounts failed: " + (done.stderr or done.stdout).strip()[-400:])
    if copy_out is not None:
        copy_out(inner, out)
    os.chmod(out, 0o600)
    return out


def compose_copy_out(project: str, compose_file: Path, env_file: Optional[Path] = None,
                     service: str = "api") -> Callable[[str, Path], None]:
    def copy(inner: str, out: Path) -> None:
        base = ["docker", "compose", "-p", project, "-f", str(compose_file)]
        if env_file:
            base += ["--env-file", str(env_file)]
        fd = os.open(out, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as fh:
            done = subprocess.run([*base, "exec", "-T", "-u", "boss", service, "cat", inner],
                                  stdout=fh, stderr=subprocess.PIPE, text=True, timeout=60)
        subprocess.run([*base, "exec", "-T", "-u", "boss", service, "rm", "-f", inner],
                       capture_output=True, timeout=60)
        if done.returncode != 0:
            raise IdentitySetupError(f"could not copy the seeded accounts out: {done.stderr[-200:]}")
    return copy


def load_accounts(path: Path) -> dict[str, Any]:
    """{role: Account, ..., "service_account": ServiceAccount, "org_id": str}."""
    doc = json.loads(Path(path).read_text())
    found: dict[str, Any] = {"org_id": doc.get("org_id")}
    for p in doc.get("people", []):
        found[p["role"]] = Account(role=p["role"], email=p["email"], password=p["password"],
                                   principal_id=p["principal_id"], totp_secret=p.get("totp_secret"))
    sa = doc.get("service_account")
    if sa:
        found["service_account"] = ServiceAccount(name=sa["name"], principal_id=sa["principal_id"],
                                                  role=sa["role"], scopes=tuple(sa.get("scopes") or ()),
                                                  key=sa["key"])
    return found


# ---- sign-in through the page -------------------------------------------------------------------

#: What the walk types into, and where the console promises it (selftest checks these exist).
SIGN_IN_SELECTORS = (
    ("app/(auth)/login/sign-in.tsx", 'id="email"', "#email"),
    ("app/(auth)/login/sign-in.tsx", 'id="password"', "#password"),
    ("app/(auth)/login/sign-in.tsx", 'data-testid="email-submit"', "[data-testid=email-submit]"),
    ("components/identity/kit.tsx", 'role="group" aria-label={label}', "[role=group] input"),
    ("app/(auth)/login/sign-in.tsx", 'label="Authenticator code"', "the code's group label"),
)


def sign_in(page: Any, console_origin: str, account: Account, *, next_path: str = "/",
            timeout_ms: int = 60_000) -> None:
    """Sign in at /login as `account`: email + password, then the authenticator code when the
    role has one. Raises IdentitySetupError naming the step that did not finish."""
    origin = console_origin.rstrip("/")
    page.goto(f"{origin}/login?next={next_path}", wait_until="domcontentloaded")
    page.wait_for_selector("#email", timeout=timeout_ms)
    page.fill("#email", account.email)
    if page.locator("#password").count() == 0:
        page.click("[data-testid=email-submit]")
        page.wait_for_selector("#password", timeout=timeout_ms)
    page.fill("#password", account.password)
    page.click("[data-testid=email-submit]")
    code_box = page.locator('[role=group][aria-label="Authenticator code"] input')
    deadline = time.time() + timeout_ms / 1000
    while time.time() < deadline:
        if "/login" not in page.url and "/signin" not in page.url:
            return
        if code_box.count():
            if not account.totp_secret:
                raise IdentitySetupError(f"{account.role}: asked for a code but the seed gave no TOTP secret")
            # The code was accepted when the page leaves /login; the box can vanish first (the
            # "Signed in" stage while the navigation runs), so only an error on a still-visible
            # box means refused. Another process signing in as the same seeded person may have
            # spent this 30 s step (the API refuses a replayed step): one retry on the next step.
            fills = 0
            while time.time() < deadline:
                if "/login" not in page.url and "/signin" not in page.url:
                    return
                refused = page.locator(".idn-err[role=alert]")
                if fills == 0 or (code_box.count() and refused.count() and refused.first.is_visible()):
                    if fills == 2:
                        raise IdentitySetupError(f"{account.role}: the authenticator code was refused twice")
                    if fills:
                        _LAST_STEP[account.totp_secret] = int(time.time() // 30)   # force the next step
                    if not code_box.count():
                        page.wait_for_timeout(250)
                        continue
                    code_box.first.fill(fresh_totp(account.totp_secret))
                    fills += 1
                    page.wait_for_timeout(1_000)   # the page clears the old error as it submits
                page.wait_for_timeout(250)
            break
        err = page.locator("#signin-error, .idn-err[role=alert]")
        if err.count() and err.first.is_visible():
            raise IdentitySetupError(f"{account.role}: sign-in refused: {err.first.inner_text()[:120]}")
        page.wait_for_timeout(250)
    raise IdentitySetupError(f"{account.role}: sign-in did not finish within {timeout_ms // 1000}s (at {page.url})")


def api_in_page(page: Any, method: str, path: str, body: Any = None) -> dict[str, Any]:
    """A same-origin call from the signed-in page (cookie + CSRF), exactly as the console makes it."""
    return page.evaluate("""async ([m, p, b]) => {
        let csrf = '';
        try { const me = await (await fetch('/v1/identity/me', {credentials: 'same-origin'})).json(); csrf = me.csrf_token || ''; } catch (e) {}
        const h = {'Content-Type': 'application/json', 'X-Boss-CSRF': csrf};
        const r = await fetch(p, {method: m, headers: h, credentials: 'same-origin', body: b === null ? undefined : JSON.stringify(b)});
        let j = null; try { j = await r.json(); } catch (e) {}
        return {status: r.status, body: j};
    }""", [method, path, body])
