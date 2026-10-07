"""THE ROLE x ROUTE MATRIX ON A REAL STACK: every operation in tests/identity/matrix.yaml, called
over HTTP against an installed, enforce-mode BOSS with its REAL handlers, as each seeded person
(owner, admin, operator, viewer, auditor, the custom `developer`) through the console's origin
with their session cookie, and as the seeded service account's key against the API directly.

WHY, ON TOP OF apps/api/tests/test_identity_authz_matrix.py. That test is the decision only: stub
handlers, roles bound at org scope, a fixture world, in-process. Here nothing is stubbed: the
console proxy, the cookie, the CSRF check, the real guard, the real PDP, the real handler and the
real database. A route that bypasses the guard, a handler that answers 200 before the guard has
run, a proxy that drops the session — each shows up here and nowhere else.

WHAT IS CALLED, AND WHAT IS NOT. A principal's expected answer is computed from the route's
declaration and that principal's live permission set (`GET /v1/identity/me/permissions`):
  * expected DENIED (any method): the request is made; 403 or 404 passes. 2xx is an escalation
    (hard). 5xx is a crash on the deny path (hard). 400/409/422 means the handler or validation
    ran before the guard refused — reported as `validated_before_authz` (hard: a denied caller
    must learn nothing about the body it sent, and the guard must run first).
  * expected ALLOWED, GET/HEAD: the request is made; anything but 401/403 passes (404/422 are a
    dummy id or a missing query parameter); 401 `step_up_required` passes on a step-up route.
    401/403 otherwise is a hard finding (a role the catalogue allows is refused).
  * expected ALLOWED, a write: NOT made (it would change the shared stack). Counted as skipped.
The role's live set is also checked against the catalogue (permissions_gen.ROLE_PERMISSIONS, the
seed's CUSTOM_ROLE_PERMISSIONS, and the key's scopes ∩ its role) — a drift is a hard finding.

    cd tests && python3 -m e2e.identity.api_sweep --console http://localhost:53100 \
        --api http://127.0.0.1:58100 --accounts <seed file> --storage-dir <dir> --out report.json

`--storage-dir` holds `<role>.storage.json` Playwright storage states (signed-in sessions); a
missing or expired one is made by signing in through /login (harness/identity.py). Secrets are
read from files and never printed.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

REPO = Path(__file__).resolve().parents[3]
MATRIX = REPO / "tests" / "identity" / "matrix.yaml"
LINE = re.compile(r'^"(?P<method>[A-Z]+) (?P<path>[^"]+)": \["(?P<decl>[^"]+)", "(?P<roles>[^"]+)"\]$')
PERMIT = re.compile(r"^permit (?P<perm>[a-z_]+:[a-z_]+) @ (?P<scope>[a-z]+)")
PROJECT = "boss-dogfood"
DUMMY = "fxd-none"
HUMAN_ROLES = ("owner", "admin", "operator", "viewer", "auditor", "developer")
LETTER = {"owner": "O", "admin": "A", "operator": "P", "viewer": "V", "auditor": "U"}


@dataclass
class Op:
    method: str
    path: str
    decl: str
    roles: str

    @property
    def permission(self) -> Optional[str]:
        m = PERMIT.match(self.decl)
        return m.group("perm") if m else None

    @property
    def scope(self) -> Optional[str]:
        m = PERMIT.match(self.decl)
        return m.group("scope") if m else None


def load_matrix() -> list[Op]:
    ops = []
    for line in MATRIX.read_text().splitlines():
        m = LINE.match(line.strip())
        if m:
            ops.append(Op(m["method"], m["path"], m["decl"], m["roles"]))
    return ops


def fill(path: str) -> str:
    path = re.sub(r"\{project_id(:[^}]*)?\}", PROJECT, path)
    return re.sub(r"\{[^}]+\}", DUMMY, path)


SCOPE_SOURCE = re.compile(r"@ (?P<kind>[a-z_]+)\((?P<where>query|body)=(?P<name>[a-z_]+)\)")


def scope_inputs(op: "Op") -> tuple[dict[str, str], dict[str, str]]:
    """The query parameters and body fields the declaration reads its scope from, filled the way
    a real caller would (the project for a project scope, a dummy id for an object): without them
    the guard refuses `missing_permission (scope: query value ... is missing)` with a 403 that
    names a permission even the Owner holds — reported separately as `scope_missing_is_403`."""
    query: dict[str, str] = {}
    body: dict[str, str] = {"project_id": PROJECT}
    for m in SCOPE_SOURCE.finditer(op.decl):
        value = PROJECT if m["kind"] == "project" else DUMMY
        (query if m["where"] == "query" else body)[m["name"]] = value
    return query, body


#: Endpoints that hold the connection open (server-sent events): not called.
STREAMING = re.compile(r"/(stream|events|sse)(/|$)")


@dataclass
class Caller:
    name: str
    base: str                       # where requests go
    headers: dict[str, str]         # auth (cookie or bearer)
    csrf: str = ""
    permissions: frozenset[str] = frozenset()
    via_console: bool = True

    @property
    def is_person(self) -> bool:
        """A person's own session (a cookie), not a key or token."""
        return "Cookie" in self.headers

    def call(self, method: str, path: str, body: Any = None,
             query: Optional[dict[str, str]] = None) -> tuple[int, dict[str, Any]]:
        from urllib.parse import urlencode

        sep = "&" if "?" in path else "?"
        url = f"{self.base}{path}{sep}{urlencode({'project_id': PROJECT, **(query or {})})}"
        data = None if method in ("GET", "HEAD", "DELETE") else json.dumps(body if body is not None else {}).encode()
        h = {"Accept": "application/json", **self.headers}
        if data is not None:
            h["Content-Type"] = "application/json"
        if self.via_console:
            # What the console's own fetches carry: the CSRF token on every call and the browser's
            # Sec-Fetch-Site (a GET declared `writes_on_get` needs one of them), Origin on writes.
            h["X-Boss-CSRF"] = self.csrf
            h["Sec-Fetch-Site"] = "same-origin"
            if method not in ("GET", "HEAD"):
                h["Origin"] = self.base
        req = urllib.request.Request(url, data=data, method=method, headers=h)
        for attempt in range(3):
            try:
                with urllib.request.urlopen(req, timeout=60) as resp:
                    return resp.status, _json(resp.read())
            except urllib.error.HTTPError as exc:
                if exc.code == 429 and attempt < 2:
                    time.sleep(float(exc.headers.get("Retry-After") or 2))
                    continue
                return exc.code, _json(exc.read())
            except Exception as exc:  # noqa: BLE001 — a dropped connection is a finding, not a crash
                if attempt < 2:      # the console proxy occasionally drops a kept-alive socket
                    time.sleep(1)
                    continue
                return 599, {"error": type(exc).__name__}
        return 599, {"error": "retries"}


def _json(raw: bytes) -> dict[str, Any]:
    try:
        body = json.loads(raw.decode() or "{}")
        return body if isinstance(body, dict) else {"_": body}
    except Exception:  # noqa: BLE001
        return {"_raw": raw[:200].decode(errors="replace")}


def session_caller(role: str, console: str, storage: Path, accounts: dict[str, Any]) -> Caller:
    def from_state() -> Optional[Caller]:
        if not storage.exists():
            return None
        cookies = json.loads(storage.read_text()).get("cookies", [])
        jar = "; ".join(f"{c['name']}={c['value']}" for c in cookies)
        c = Caller(role, console.rstrip("/"), {"Cookie": jar})
        status, me = c.call("GET", "/v1/identity/me")
        if status != 200:
            return None
        c.csrf = me.get("csrf_token") or ""
        return c

    c = from_state()
    if c is None:
        from playwright.sync_api import sync_playwright

        from e2e.harness import identity

        with sync_playwright() as pw:
            browser = pw.chromium.launch()
            ctx = browser.new_context()
            identity.sign_in(ctx.new_page(), console, accounts[role])
            ctx.storage_state(path=str(storage))
            browser.close()
        c = from_state()
    if c is None:
        raise RuntimeError(f"{role}: could not get a session")
    return c


def live_permissions(c: Caller) -> frozenset[str]:
    status, body = c.call("GET", "/v1/identity/me/permissions")
    if status != 200:
        raise RuntimeError(f"{c.name}: /v1/identity/me/permissions answered {status}")
    return frozenset(body.get("permissions") or [])


def expected_set(role: str, accounts: dict[str, Any]) -> frozenset[str]:
    sys.path.insert(0, str(REPO / "engine"))
    from boss_engine.identity.permissions_gen import ROLE_PERMISSIONS
    from boss_engine.identity.seed_accounts import CUSTOM_ROLE_PERMISSIONS, SERVICE_ACCOUNT_ROLE, SERVICE_ACCOUNT_SCOPES

    if role == "developer":
        return frozenset(CUSTOM_ROLE_PERMISSIONS)
    if role == "service_account":
        return frozenset(ROLE_PERMISSIONS[SERVICE_ACCOUNT_ROLE]) & frozenset(SERVICE_ACCOUNT_SCOPES)
    return frozenset(ROLE_PERMISSIONS[role])


def system_only() -> frozenset[str]:
    from boss_engine.identity.permissions_gen import PERMISSION_META

    return frozenset(k for k, m in PERMISSION_META.items() if m.system_only)


def step_up_perms() -> frozenset[str]:
    from boss_engine.identity.permissions_gen import PERMISSION_META

    return frozenset(k for k, m in PERMISSION_META.items() if m.step_up or m.critical)


@dataclass
class Result:
    caller: str
    op: str
    expected: str           # allow | deny
    status: int
    verdict: str            # pass | fail | skipped
    why: str = ""
    error: str = ""


@dataclass
class Report:
    results: list[Result] = field(default_factory=list)
    set_checks: dict[str, Any] = field(default_factory=dict)

    def add(self, r: Result) -> None:
        self.results.append(r)


def judge(op: Op, c: Caller, *, sysonly: frozenset[str], stepup: frozenset[str]) -> Optional[Result]:
    key = f"{op.method} {op.path}"
    if op.method == "WS":
        return None
    if op.decl.startswith("public"):
        return None   # public routes are their own suite (test_identity_authz_public_routes.py)
    if c.via_console and not op.path.startswith("/v1/"):
        return None   # the console proxies /v1 only; /scim and /saml are bearer/IdP surfaces
    if STREAMING.search(op.path):
        return Result(c.name, key, "?", 0, "skipped", "a stream (server-sent events) is held open, not called")
    perm = op.permission
    if op.decl.startswith("authenticated"):
        # `authenticated:…:people` is a person's own session only (@authenticated(people_only=True)):
        # a service account or any other bearer is refused 403 people_only BY DESIGN — e.g. GET
        # /v1/identity/me/profile is a person's name, email and avatar; an API key has none.
        allowed = not (":people" in op.decl.split(" ", 1)[0] and not c.is_person)
    else:
        allowed = bool(perm) and perm in c.permissions and perm not in sysonly
    if allowed and op.method not in ("GET", "HEAD"):
        return Result(c.name, key, "allow", 0, "skipped", "an allowed write is not made on the shared stack")
    query, body_in = scope_inputs(op)
    status, body = c.call(op.method, fill(op.path), body_in, query)
    err = str(body.get("error") or body.get("detail") or "")[:120]
    if allowed:
        if status in (401, 403):
            if status == 401 and "step_up" in err and perm in stepup:
                return Result(c.name, key, "allow", status, "pass", "allowed; needs a fresh second factor", err)
            if "not an active member" in err:
                return Result(c.name, key, "allow", status, "fail",
                              "a legacy org-member check in the handler refuses an identity principal", err)
            return Result(c.name, key, "allow", status, "fail", "the catalogue allows it; the stack refused", err)
        if status >= 500:
            return Result(c.name, key, "allow", status, "fail", "allowed GET crashed (5xx)", err)
        return Result(c.name, key, "allow", status, "pass")
    if status in (403, 404):
        return Result(c.name, key, "deny", status, "pass")
    if 200 <= status < 300:
        return Result(c.name, key, "deny", status, "fail", "ESCALATION: a denied caller got 2xx", err)
    if status >= 500:
        return Result(c.name, key, "deny", status, "fail", "the deny path crashed (5xx)", err)
    if status == 401:
        return Result(c.name, key, "deny", status, "fail", "denied as unauthenticated (401) though signed in", err)
    return Result(c.name, key, "deny", status, "fail", "validated_before_authz: answered before the guard refused", err)


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--console", required=True)
    ap.add_argument("--api", required=True)
    ap.add_argument("--accounts", required=True, type=Path)
    ap.add_argument("--storage-dir", required=True, type=Path)
    ap.add_argument("--out", required=True, type=Path)
    ap.add_argument("--roles", default=",".join((*HUMAN_ROLES, "service_account")))
    ap.add_argument("--known", type=Path, default=Path(__file__).with_name("api_sweep_known.json"),
                    help="known failures, each naming the fix stream item; reported as xfail")
    args = ap.parse_args(argv)

    sys.path.insert(0, str(REPO / "tests"))
    sys.path.insert(0, str(REPO / "engine"))
    from e2e.harness import identity

    accounts = identity.load_accounts(args.accounts)
    ops = load_matrix()
    sysonly, stepup = system_only(), step_up_perms()
    report = Report()
    known = json.loads(args.known.read_text()) if args.known.exists() else {}
    for role in args.roles.split(","):
        if role == "service_account":
            sa = accounts["service_account"]
            c = Caller("service_account", args.api.rstrip("/"), {"Authorization": f"Bearer {sa.key}"}, via_console=False)
        else:
            c = session_caller(role, args.console, args.storage_dir / f"{role}.storage.json", accounts)
        c.permissions = live_permissions(c)
        want = expected_set(role, accounts)
        drift = {"missing": sorted(want - c.permissions), "extra": sorted(c.permissions - want)}
        report.set_checks[role] = {"live": len(c.permissions), "catalogue": len(want), **drift,
                                   "ok": not drift["missing"] and not drift["extra"]}
        started = time.time()
        for op in ops:
            r = judge(op, c, sysonly=sysonly, stepup=stepup)
            if r is not None:
                report.add(r)
        print(f"  {role}: {sum(1 for r in report.results if r.caller == role)} ops in {time.time() - started:.0f}s",
              flush=True)

    def kind(r: Result) -> str:
        if r.verdict != "fail":
            return r.verdict
        return "xfail" if (f"{r.caller} {r.op}" in known or f"* {r.op}" in known) else "fail"

    summary: dict[str, dict[str, int]] = {}
    for r in report.results:
        summary.setdefault(r.caller, {}).setdefault(kind(r), 0)
        summary[r.caller][kind(r)] += 1
    set_fail = [k for k, v in report.set_checks.items() if not v["ok"]]
    out = {"summary": summary, "set_checks": report.set_checks,
           "failures": [r.__dict__ | {"known": known.get(f"{r.caller} {r.op}") or known.get(f"* {r.op}")}
                        for r in report.results if r.verdict == "fail"]}
    args.out.write_text(json.dumps(out, indent=1))
    print(json.dumps(summary, indent=1))
    if set_fail:
        print(f"live permission sets differ from the catalogue: {set_fail}")
    new = [r for r in report.results if kind(r) == "fail"]
    for r in new[:60]:
        print(f"  FAIL {r.caller:<16} {r.op:<70} {r.status} {r.why} {r.error}")
    print(f"{len(new)} new failures, {sum(1 for r in report.results if kind(r) == 'xfail')} known; report {args.out}")
    return 1 if new or set_fail else 0


if __name__ == "__main__":
    raise SystemExit(main())
