"""THE PER-ROLE WALK OF THE REAL CONSOLE (FX-D): every area and page, as every role, against a
running enforce-mode stack.

    cd tests && python3 -m e2e.identity.role_walk --console http://localhost:53100 \\
        --accounts <accounts.json> --out <dir> [--roles viewer auditor] [--shots <dir>] [--no-click]

For each role (owner, admin, operator, viewer, auditor, the custom `developer`) it signs in through
the real /login and asserts:

  perms     /v1/identity/me/permissions (org, and ?project_id=<project>) equals the catalogue's set
            for a built-in role (packages/identity/catalogue.yaml) or the seed's CUSTOM_ROLE_PERMISSIONS.
  storage   no localStorage/sessionStorage value is a session id, the CSRF token, a boss_(pat|sak|rt)_
            secret or a JWT; no URL visited while signing in carries a token in its query. Only key
            names and verdicts are written.
  rail      every [data-area-nav] shown is an area the role may open.
  denied    every area/page the role may NOT open renders AccessDenied naming the permission, and
            neither the raw HTML (fetched with the role's cookies) nor the DOM holds any data token
            (ids, seed emails) the Owner's render of the same URL holds, minus the role's own layout
            tokens (its /account render).
  allowed   every area/page it may open renders without AccessDenied, error banners, console
            errors, or same-origin /v1 answers >= 400 (a 403 there is always hard).
  buttons   every [data-permission][data-can] control: data-can matches the role's set at the
            right scope; a "no" is aria-disabled with a tooltip naming the need. AREA_ACTIONS whose
            permission the role lacks must not be found enabled. Ungated buttons are listed (soft).
  clicks    viewer and auditor only: press every enabled control on every allowed page; a mutating
            same-origin request that answers 2xx is hard unless it is self-service.

What is decided where (parsed, never hand-listed): console_map.py. Failures already owned by a fix
stream are in KNOWN below and reported as xfail; the exit status is non-zero only on a NEW hard
failure (or when the stack is down).
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import time
import traceback
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional
from urllib.parse import parse_qsl, urlsplit

from e2e.harness import identity as I
from e2e.identity import console_map as M

ROLES = ("owner", "admin", "operator", "viewer", "auditor", "developer")
CLICK_ROLES = ("viewer", "auditor")
ORG = "org_boss"
PROJECT = "boss-dogfood"
SCRATCH = Path("/private/tmp/claude-501/-Users-param-startup-sean-boss/b8a9beba-5883-4930-93e4-516678b875a0/scratchpad")

TOKEN_RE = re.compile(r"\b(?:usr|grp|arl|sva|key|dlg|agt|pas|wfd|crun|run|ses|inv|idp)_[a-z0-9]{6,}\b")
SELF_SERVICE = ("/v1/identity/me/", "/v1/auth/", "/v1/identity/access-requests", "/v1/identity/step-up")
SECRETISH = [re.compile(r"boss_(?:pat|sak|rt)_"), re.compile(r"eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}"),
             re.compile(r"\bses_[a-z0-9]{6,}")]
QUERY_SECRET_NAMES = re.compile(r"token|session|csrf|secret|password|passwd|code|key|otp|jwt", re.I)
#: Never pressed, whatever the role: leaving the page, ending sessions, asking for access.
SKIP_PRESS = re.compile(r"sign ?out|log ?out|\bend\b|revoke|request access|disconnect", re.I)
#: On My-account pages, also never pressed: anything that could break the seeded person's sign-in.
SKIP_PRESS_ACCOUNT = re.compile(r"remove|delete|disable|turn off|revoke|reset|regenerate|change password|rotate", re.I)

# ---- failures already owned by a fix stream (reported as xfail, never hidden) -------------------
# Each entry: (regex over a finding id, owner item, note). A finding id is
#   "<check>|<role>|<target>|<detail>". Keep these narrow: a broad pattern would hide a new bug.
KNOWN: list[tuple[str, str, str]] = [
    # A "writing GET" proxy (apps/web/app/api/{kit,mcp,run-acts}/route.ts) forwards the browser's GET
    # to the API as a POST with the session cookie but no X-Boss-CSRF, so enforce mode answers
    # 403 csrf_failed for EVERY role, Owner included: Skills & MCP shows "Can't reach BOSS API".
    (r"^http-error\|[a-z]+\|[^|]+\|GET /api/(kit|mcp|run-acts) 403$", "unassigned (FX-C?)",
     "writing-GET proxies drop the CSRF header: 403 csrf_failed for every role"),
    (r"^error-banner\|[a-z]+\|(skills|capabilities)\|can-t-reach-boss-api", "unassigned (FX-C?)",
     "the banner the /api/kit csrf_failed 403 causes; also P1-11/12/13 (it says 'can't reach', the API answered)"),
    # Pages that call a route the role cannot use (§9.10: no page 403s after it loads).
    (r"^http-error\|(operator|viewer|developer)\|(observability|monitoring|setup|overview|/|/v2/settings)\|GET /api/improve 403$",
     "unassigned (FX-C P1-18?)", "Improve-BOSS sharing card fetched without the permission check"),
    (r"^http-error\|developer\|[^|]+\|GET /v1/agents/identities 403$", "unassigned (FX-C P1-18?)",
     "the shell's agent-face resolver needs agent_passport:read; a custom role without it 403s on every page"),
    (r"^http-error\|developer\|(setup|/v2/settings|skills)\|GET /v1/(entitlements/sources|entitlement-connection-sessions) 403$",
     "unassigned (FX-C P1-18?)", "Setup fetches entitlements (entitlement:read) for a role that may open setup"),
    (r"^http-error\|developer\|(delivery_workflows|/v2/boss|workforce|/v2/timeline)\|GET /api/(deployment-targets|deployment-hosts|model-routes) 403$",
     "unassigned (FX-C P1-18?)", "Workflows fetches infra/model routes the developer role does not hold"),
]


# Findings checked by hand and found NOT to be bugs (reads that travel as POSTs). Downgraded to info,
# with the reason. Same id format as KNOWN.
BENIGN: list[tuple[str, str]] = [
    (r"^mutation-2xx\|(viewer|auditor)\|[^|]*\|POST /api/delivery-workflows action=(check|wire)$",
     "readonly_post: /v1/delivery-workflows/check and /wire are @permit(workflow:read, readonly_post=True)"),
    (r"^mutation-2xx\|(viewer|auditor)\|org :: Export\|server-action /$",
     "a read: exportPackAction returns the pack's YAML (app/actions.ts exportPackYaml)"),
    # Triaged by the lead after the final run:
    (r"^mutation-2xx\|(viewer|auditor)\|code_tasks :: [^|]*\|POST /api/improve$",
     "self-service, held: opening a build shows the rate-it card, which records improve op "
     "prompt_shown -> POST /v1/feedback/prompts/shown, permit feedback:submit (Viewer and Auditor hold it)"),
    (r"^mutation-2xx\|(viewer|auditor)\|ops :: Run merge check\|server-action /$",
     "readonly_post: mergeCheckAction -> POST /v1/factory/merge-check is "
     "@permit(code_task:read, readonly_post=True) and only evaluates the gate"),
    (r"^mutation-2xx\|(viewer|auditor)\|processes :: [^|]*\|server-action /$",
     "a read: opening a process row loads its view through a server action (same as on page load)"),
]


@dataclass
class Finding:
    id: str
    check: str
    role: str
    url: str
    seen: str
    expected: str
    severity: str = "hard"             # hard | soft | info
    status: str = "fail"               # fail | xfail | soft | info
    owner: str = ""

    def classify(self) -> "Finding":
        if self.severity != "hard":
            self.status = self.severity
            return self
        for pattern, why in BENIGN:
            if re.search(pattern, self.id):
                self.severity = self.status = "info"
                self.owner = f"benign: {why}"
                return self
        for pattern, item, _note in KNOWN:
            if re.search(pattern, self.id):
                self.status, self.owner = "xfail", item
                return self
        self.status = "fail"
        return self


@dataclass
class RoleState:
    role: str
    org: frozenset[str] = frozenset()
    project: frozenset[str] = frozenset()
    me: dict[str, Any] = field(default_factory=dict)

    def holds(self, permission: str, at_project: bool, scope_of: dict[str, str]) -> bool:
        """What the console's checkWith answers: project-scoped keys ask the project snapshot when
        the page has an active project; everything else asks the org snapshot."""
        if at_project and scope_of.get(permission) == "project":
            return permission in self.project
        return permission in self.org


# ---- page observation ---------------------------------------------------------------------------

JS_SCAN = r"""() => {
  const vis = (el) => { const r = el.getBoundingClientRect(); const s = getComputedStyle(el);
    return r.width > 0 && r.height > 0 && s.visibility !== 'hidden' && s.display !== 'none'; };
  const txt = (el) => (el.innerText || el.getAttribute('aria-label') || el.getAttribute('title') || '').trim().replace(/\s+/g, ' ').slice(0, 80);
  const gated = [...document.querySelectorAll('[data-permission][data-can]')].map((el) => ({
    permission: el.getAttribute('data-permission'), can: el.getAttribute('data-can'),
    ariaDisabled: el.getAttribute('aria-disabled'), disabled: !!el.disabled,
    title: el.getAttribute('title') || '', text: txt(el), visible: vis(el) }));
  const denied = [...document.querySelectorAll('[data-testid=access-denied]')].map((el) => el.getAttribute('data-permission'));
  const rail = [...document.querySelectorAll('[data-area-nav]')].filter(vis).map((el) => el.getAttribute('data-area-nav'));
  const errSel = '.error-banner, .idn-err, [data-testid$="load-error"], [data-testid="area-error"], .inf-error, .ld-error, [role=alert]';
  const errors = [...document.querySelectorAll(errSel)].filter((el) => vis(el) && txt(el) && !el.closest('[data-testid=access-denied]'))
    .map((el) => txt(el));
  const skip = (el) => el.closest('[data-area-nav], [role=tablist], .bx-nav-item');
  const ungated = [...document.querySelectorAll('button, [role=button]:not(a)')]
    .filter((el) => vis(el) && !el.hasAttribute('data-permission') && !el.closest('[data-permission]') && !skip(el)
            && !el.disabled && el.getAttribute('aria-disabled') !== 'true')
    .map(txt).filter(Boolean);
  return {gated, denied, rail, errors, ungated};
}"""

JS_TAG = r"""([skip, skipAccount, onAccount]) => {
  const reSkip = new RegExp(skip, 'i'), reAcc = new RegExp(skipAccount, 'i');
  const vis = (el) => { const r = el.getBoundingClientRect(); const s = getComputedStyle(el);
    return r.width > 0 && r.height > 0 && s.visibility !== 'hidden' && s.display !== 'none'; };
  const txt = (el) => (el.innerText || el.getAttribute('aria-label') || el.getAttribute('title') || '').trim().replace(/\s+/g, ' ').slice(0, 80);
  document.querySelectorAll('[data-fxd-i]').forEach((el) => el.removeAttribute('data-fxd-i'));
  const out = []; let i = 0;
  for (const el of document.querySelectorAll('button, [role=button]:not(a)')) {
    if (!vis(el) || el.disabled || el.getAttribute('aria-disabled') === 'true') continue;
    if (el.closest('[data-area-nav], a[href]')) continue;
    const t = txt(el);
    const why = reSkip.test(t) ? 'never pressed' : (onAccount && reAcc.test(t)) ? 'protects the seeded account' : '';
    el.setAttribute('data-fxd-i', String(i));
    out.push({i, text: t, permission: el.getAttribute('data-permission'), skip: why, shell: !el.closest('main')});
    i++;
  }
  return out;
}"""


def same_origin(url: str, origin: str) -> bool:
    return url.startswith(origin)


def tokens_of(text: str, emails: set[str]) -> set[str]:
    found = set(TOKEN_RE.findall(text))
    low = text.lower()
    found |= {e for e in emails if e.lower() in low}
    return found


class Walk:
    def __init__(self, args: argparse.Namespace) -> None:
        self.args = args
        self.origin = args.console.rstrip("/")
        self.accounts = I.load_accounts(Path(args.accounts))
        self.cat = M.catalogue()
        self.custom_key, self.custom_perms = M.custom_role()
        self.area_perm = M.areas()
        self.actions = M.area_actions()
        self.pages = M.pages()
        self.findings: list[Finding] = []
        self.checks = {"pass": 0, "fail": 0, "xfail": 0, "soft": 0, "info": 0}
        self.report: dict[str, Any] = {"console": self.origin, "started": time.strftime("%Y-%m-%dT%H:%M:%S"),
                                       "roles": {}, "skipped_pages": []}
        self.shots = Path(args.shots)
        self.owner_tokens: dict[str, set[str]] = {}
        self.ids: dict[str, Optional[str]] = {}
        self.outages = 0
        self.emails = {a.email for k, a in self.accounts.items() if isinstance(a, I.Account)}
        owner_file = Path(args.accounts).parent / "owner.json"
        try:
            self.emails.add(json.loads(owner_file.read_text()).get("email") or "e2e-owner@example.test")
        except Exception:  # noqa: BLE001
            self.emails.add("e2e-owner@example.test")

    # -- bookkeeping --
    def ok(self) -> None:
        self.checks["pass"] += 1

    def find(self, check: str, role: str, target: str, detail: str, url: str, seen: str, expected: str,
             severity: str = "hard") -> Finding:
        f = Finding(f"{check}|{role}|{target}|{detail}", check, role, url, seen[:600], expected[:300], severity).classify()
        if not any(x.id == f.id for x in self.findings):
            self.findings.append(f)
            self.checks[f.status] += 1
        return f

    # -- urls --
    def area_url(self, area: str) -> str:
        return f"{self.origin}/?org={ORG}&area={area}"

    def page_targets(self) -> list[tuple[str, M.Page]]:
        """(concrete path, page) for every declared page, `[id]` filled with a real object."""
        fill = {
            "/identity/people/[id]": self.ids.get("person"),
            "/identity/groups/[id]": self.ids.get("group"),
            "/identity/roles/[id]": self.ids.get("custom_role"),
            "/identity/agents/[id]": self.ids.get("agent"),
            "/identity/agents/delegations/[id]": self.ids.get("delegation"),
            "/v2/missions/[id]": self.ids.get("mission"),
        }
        out = []
        for p in self.pages:
            if "[id]" in p.path:
                real = fill.get(p.path)
                if not real:
                    if not any(s["path"] == p.path for s in self.report["skipped_pages"]):
                        self.report["skipped_pages"].append({"path": p.path, "why": "no such object on this stack"})
                    continue
                out.append((p.path.replace("[id]", real), p))
            else:
                out.append((p.path, p))
        return out

    # -- page helpers --
    def settle(self, page: Any) -> None:
        try:
            page.wait_for_load_state("networkidle", timeout=12_000)
        except Exception:  # noqa: BLE001
            pass
        try:
            page.wait_for_selector("[data-testid=area-loading]", state="detached", timeout=15_000)
        except Exception:  # noqa: BLE001
            pass
        page.wait_for_timeout(400)

    def goto(self, page: Any, url: str) -> None:
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=45_000)
        except Exception as e:  # noqa: BLE001
            page.wait_for_timeout(500)
            if "ERR_ABORTED" not in str(e):
                raise
        self.settle(page)

    def healthy(self, ctx: Any, signed_in: bool = True) -> bool:
        try:
            status = ctx.request.get(f"{self.origin}/v1/identity/me", timeout=15_000).status
            return status == 200 or (not signed_in and status == 401)
        except Exception:  # noqa: BLE001
            return False

    def wait_healthy(self, ctx: Any, seconds: int = 240, signed_in: bool = True) -> bool:
        """The shared stack restarts now and then (other walks, the lead): wait it out rather than
        report an outage as an authorization failure."""
        end = time.time() + seconds
        while time.time() < end:
            if self.healthy(ctx, signed_in):
                return True
            time.sleep(3)
        return False

    @staticmethod
    def degraded(page: Any) -> bool:
        try:
            body = page.inner_text("body", timeout=3_000)
        except Exception:  # noqa: BLE001
            return True
        return "Reconnecting to the control plane" in body

    def shot(self, page: Any, role: str, name: str) -> str:
        d = self.shots / "roles" / role
        d.mkdir(parents=True, exist_ok=True)
        path = d / (re.sub(r"[^A-Za-z0-9_.-]+", "_", name).strip("_")[:90] + ".png")
        try:
            page.screenshot(path=str(path))
        except Exception:  # noqa: BLE001
            return ""
        return str(path)

    # -- the Owner's view: ids for [id] pages and the data tokens of every URL --
    def owner_baseline(self, page: Any, ctx: Any, targets_only: bool = False) -> None:
        users = I.api_in_page(page, "GET", "/v1/identity/users")["body"] or {}
        self.ids["person"] = self.accounts["operator"].principal_id
        groups = (I.api_in_page(page, "GET", "/v1/identity/groups")["body"] or {}).get("groups") or []
        mine = [g for g in groups if g.get("key") in ("fxd-walk-group", "fxd_walk_group") or g.get("name") == "fxd-walk-group"]
        if not groups:
            for key in ("fxd-walk-group", "fxd_walk_group"):
                r = I.api_in_page(page, "POST", "/v1/identity/groups",
                                  {"key": key, "name": "fxd-walk-group", "description": "FX-D role walk: a group to open."})
                if r["status"] in (200, 201):
                    groups = [r["body"]]
                    break
            self.report["group_created"] = bool(groups)
        g = (mine or groups or [None])[0]
        self.ids["group"] = (g or {}).get("id")
        roles = (I.api_in_page(page, "GET", "/v1/identity/access-roles")["body"] or {}).get("roles") or []
        self.ids["custom_role"] = next((r["id"] for r in roles if r.get("key") == self.custom_key), None)
        agents = (I.api_in_page(page, "GET", "/v1/identity/agents")["body"] or {}).get("agents") or []
        self.ids["agent"] = agents[0]["id"] if agents else None
        dl = (I.api_in_page(page, "GET", "/v1/identity/delegations")["body"] or {}).get("delegations") or []
        self.ids["delegation"] = dl[0].get("id") if dl else None
        html = ctx.request.get(f"{self.origin}/v2?org={ORG}").text()
        m = re.search(r'/v2/missions/([^?"\\&]+)', html)
        self.ids["mission"] = m.group(1) if m else PROJECT
        self.report["ids"] = {k: bool(v) for k, v in self.ids.items()}
        self.report["people_seen_by_owner"] = len(users.get("users") or [])

    def owner_tokens_for(self, ctx: Any, page: Any, url: str) -> set[str]:
        if url not in self.owner_tokens:
            raw = ctx.request.get(url).text()
            toks = tokens_of(raw, self.emails)
            self.owner_tokens[url] = toks
        return self.owner_tokens[url]

    # -- one role --
    def walk_role(self, browser: Any, role: str) -> None:
        acct = self.accounts[role]
        rep: dict[str, Any] = {"areas": [], "pages": [], "clicks": [], "mutations": [], "rail": {}}
        self.report["roles"][role] = rep
        ctx = browser.new_context(viewport={"width": 1440, "height": 900})
        page = ctx.new_page()
        navigated: list[str] = []
        page.on("framenavigated", lambda fr: navigated.append(fr.url) if fr == page.main_frame else None)
        page.on("dialog", lambda d: d.dismiss())
        ctx.on("page", lambda p: p.close() if p != page else None)
        state = {"url": "", "press": None, "console": [], "bad": [], "mut": []}

        def on_console(msg: Any) -> None:
            if msg.type == "error":
                state["console"].append(msg.text[:240])

        def on_response(resp: Any) -> None:
            u = resp.url
            if not same_origin(u, self.origin):
                return
            path = urlsplit(u).path
            req = resp.request
            if (path.startswith("/v1/") or path.startswith("/api/")) and resp.status >= 400:
                code = ""
                try:
                    j = json.loads(resp.text()[:2000])
                    code = str(j.get("error") or j.get("code") or j.get("detail") or "")[:60] if isinstance(j, dict) else ""
                except Exception:  # noqa: BLE001
                    pass
                state["bad"].append({"method": req.method, "path": path, "status": resp.status, "code": code})
            if req.method not in ("GET", "HEAD", "OPTIONS") and not req.headers.get("next-action"):
                rec = {"method": req.method, "path": path, "status": resp.status, "page": state["url"],
                       "press": state["press"], "server_action": False}
                try:
                    sent = json.loads(req.post_data or "null")
                    if isinstance(sent, dict) and isinstance(sent.get("action"), str):
                        rec["action"] = sent["action"][:40]      # which verb a proxy route was asked for
                except Exception:  # noqa: BLE001
                    pass
                state["mut"].append(rec)

        def on_server_action(route: Any) -> None:
            """A server action answers 200 whatever the API said; its ActionResult says. Read
            through a route (a response listener often cannot get the body back)."""
            req = route.request
            if req.method != "POST" or not req.headers.get("next-action") or not same_origin(req.url, self.origin):
                route.fallback()
                return
            try:
                resp = route.fetch()
                body = resp.text()[:6000]
            except Exception:  # noqa: BLE001
                route.fallback()
                return
            rec = {"method": "POST", "path": urlsplit(req.url).path, "status": resp.status, "page": state["url"],
                   "press": state["press"], "server_action": True,
                   "refused_in_body": bool(re.search(r'"ok":false|"error":"(forbidden|step_up_required|csrf_failed)', body)),
                   "ok_in_body": '"ok":true' in body}
            m = re.search(r'"error":"([^"]{0,80})"', body)
            if m:
                rec["error"] = m.group(1)
            state["mut"].append(rec)
            route.fulfill(response=resp, body=body if len(body) < 6000 else None)

        page.on("console", on_console)
        page.on("pageerror", lambda e: state["console"].append(f"pageerror: {str(e)[:220]}"))
        page.on("response", on_response)
        page.route("**/*", on_server_action)

        # 1. sign in (the real /login), watching every URL
        if not self.wait_healthy(ctx, signed_in=False):
            self.find("stack-unavailable", role, "signin", "outage", self.origin, "the API did not answer for 4 minutes", "a healthy stack", severity="info")
            ctx.close()
            return
        err = self.sign_in(page, ctx, acct)
        if err:
            self.find("signin", role, "/login", "refused", f"{self.origin}/login", err, "the seeded person signs in")
            ctx.close()
            return
        self.settle(page)
        self.check_urls(role, navigated, rep)
        me = I.api_in_page(page, "GET", "/v1/identity/me")
        if me["status"] != 200:
            self.find("signin", role, "/v1/identity/me", str(me["status"]), self.origin, f"/me answered {me['status']}", "200")
            ctx.close()
            return
        rs = RoleState(role, me=me["body"] or {})
        self.check_storage(page, role, rs, rep)
        self.check_perms(page, role, rs, rep)
        if role == "owner":
            self.owner_baseline(page, ctx)

        # the role's own layout tokens: what it may see on /account
        acc_html = ctx.request.get(f"{self.origin}/account").text()
        own = tokens_of(acc_html, self.emails)
        self.goto(page, f"{self.origin}/account")
        own |= tokens_of(page.content(), self.emails)
        allowed_tokens: set[str] = set(own)
        forbidden_samples: list[dict[str, Any]] = []
        rail_seen: set[str] = set()

        # 2. areas, then pages
        targets: list[tuple[str, str, Optional[str], bool, str]] = []   # (kind, url, perm, at_project, key)
        for area, perm in self.area_perm.items():
            if self.args.only and area not in self.args.only:
                continue
            targets.append(("area", self.area_url(area), perm, True, area))
        for path, p in self.page_targets():
            if self.args.only and p.path not in self.args.only:
                continue
            at_project = not (path.startswith("/identity") or path.startswith("/account"))
            targets.append(("page", f"{self.origin}{path}", p.permission, at_project, p.path))

        for kind, url, perm, at_project, key in targets:
            may = perm is None or rs.holds(perm, at_project, self.cat.scope)
            state["console"].clear(); state["bad"].clear()
            state["url"] = url
            entry: dict[str, Any] = {"kind": kind, "key": key, "url": url, "permission": perm, "allowed": may}
            try:
                stable = False
                for attempt in range(3):
                    if not self.wait_healthy(ctx):
                        break
                    state["console"].clear(); state["bad"].clear()
                    if role == "owner":
                        self.owner_tokens.pop(url, None)
                        self.owner_tokens_for(ctx, page, url)
                    self.goto(page, url)
                    if not self.degraded(page) and self.healthy(ctx) and not any(b["status"] in (502, 503, 504) for b in state["bad"]):
                        stable = True
                        break
                    self.outages += 1
                if not stable:
                    self.find("stack-unavailable", role, key, "outage", url, "the API was down or restarting while this page loaded",
                              "a healthy stack", severity="info")
                    entry["skipped"] = "stack unavailable"
                    rep["areas" if kind == "area" else "pages"].append(entry)
                    continue
                scan = page.evaluate(JS_SCAN)
                if kind == "area":
                    rail_seen |= set(scan["rail"])
                if may:
                    dom_toks = tokens_of(page.content(), self.emails)
                    allowed_tokens |= dom_toks
                    entry["shot"] = self.shot(page, role, f"allowed_{kind}_{key}")
                    self.check_allowed(page, role, kind, key, url, perm, scan, state, entry, rs, at_project)
                else:
                    raw = ctx.request.get(url).text()
                    entry["shot"] = self.shot(page, role, f"denied_{kind}_{key}")
                    forbidden_samples.append({"kind": kind, "key": key, "url": url, "perm": perm,
                                              "raw": tokens_of(raw, self.emails), "dom": tokens_of(page.content(), self.emails),
                                              "denied": scan["denied"], "entry": entry})
                    self.check_denied_render(role, kind, key, url, perm, scan, entry)
                if state["bad"]:
                    entry["v1_errors"] = list(state["bad"])
            except Exception as e:  # noqa: BLE001
                self.find("walk", role, key, "exception", url, f"{type(e).__name__}: {str(e)[:200]}", "the page opens")
                entry["error"] = str(e)[:200]
            rep["areas" if kind == "area" else "pages"].append(entry)

        # 3. rail
        self.check_rail(role, rs, rail_seen, rep)
        # 4. no data on forbidden pages (after allowed pages, so tokens it may see elsewhere are known)
        self.check_leaks(role, forbidden_samples, own, allowed_tokens)
        # 5. click everything (viewer, auditor)
        if role in CLICK_ROLES and not self.args.no_click:
            for kind, url, perm, at_project, key in targets:
                if perm is None or rs.holds(perm, at_project, self.cat.scope):
                    try:
                        self.click_all(page, role, key, url, state, rep)
                    except Exception as e:  # noqa: BLE001
                        self.find("walk", role, key, "click-crash", url, f"{type(e).__name__}: {str(e).splitlines()[0][:160]}",
                                  "every control on the page pressed", severity="soft")
                        state["press"] = None
        # 6. mutations seen at any time in this role's session
        self.judge_mutations(role, state["mut"], rep)
        ctx.close()

    def sign_in(self, page: Any, ctx: Any, acct: Any) -> str:
        """The harness's sign_in, retried once: a TOTP step another walk spent, or a slow redirect
        after the code, can make one attempt give up while the session was in fact made."""
        last = ""
        for attempt in range(2):
            try:
                I.sign_in(page, self.origin, acct, next_path=f"/?org={ORG}")
                return ""
            except Exception as e:  # noqa: BLE001
                last = str(e).splitlines()[0][:200]
                page.wait_for_timeout(3_000)
                if self.healthy(ctx):
                    self.goto(page, f"{self.origin}/?org={ORG}")
                    return ""
                if attempt == 0:
                    page.wait_for_timeout(31_000)     # a fresh TOTP step
        return last

    # -- checks --
    def check_urls(self, role: str, navigated: list[str], rep: dict[str, Any]) -> None:
        bad = []
        for u in navigated:
            q = parse_qsl(urlsplit(u).query, keep_blank_values=True)
            for name, value in q:
                looks = any(r.search(value) for r in SECRETISH) or (QUERY_SECRET_NAMES.search(name) and len(value) >= 6)
                if looks:
                    bad.append(f"{urlsplit(u).path}?{name}=<redacted>")
        rep["signin_urls"] = {"count": len(navigated), "paths": sorted({urlsplit(u).path for u in navigated}),
                              "token_in_query": bad}
        if bad:
            self.find("url-token", role, "signin", "query", self.origin, "; ".join(bad), "no token in any visited URL")
        else:
            self.ok()

    def check_storage(self, page: Any, role: str, rs: RoleState, rep: dict[str, Any]) -> None:
        dump = page.evaluate("""() => { const d = (s) => { const o = {}; try { for (let i = 0; i < s.length; i++) { const k = s.key(i); o[k] = s.getItem(k); } } catch (e) {} return o; };
                                 return {local: d(window.localStorage), session: d(window.sessionStorage)}; }""")
        secrets = [v for v in (rs.me.get("session_id"), rs.me.get("csrf_token")) if v]
        verdict: dict[str, list[str]] = {"local": [], "session": []}
        flagged = []
        for where in ("local", "session"):
            for k, v in (dump.get(where) or {}).items():
                verdict[where].append(k)
                v = v or ""
                if any(s in v for s in secrets) or any(r.search(v) for r in SECRETISH):
                    flagged.append(f"{where}Storage[{k}]")
        rep["storage"] = {"local_keys": verdict["local"], "session_keys": verdict["session"],
                          "verdict": "token-like value found" if flagged else "clean", "flagged_keys": flagged}
        print(f"  [{role}] storage keys local={verdict['local']} session={verdict['session']} -> {rep['storage']['verdict']}")
        if flagged:
            self.find("storage-token", role, "storage", ",".join(flagged), self.origin, f"token-like values under {flagged}", "no session/access token in web storage")
        else:
            self.ok()

    def check_perms(self, page: Any, role: str, rs: RoleState, rep: dict[str, Any]) -> None:
        o = I.api_in_page(page, "GET", "/v1/identity/me/permissions")
        p = I.api_in_page(page, "GET", f"/v1/identity/me/permissions?project_id={PROJECT}")
        rs.org = frozenset((o["body"] or {}).get("permissions") or [])
        rs.project = frozenset((p["body"] or {}).get("permissions") or [])
        expected = self.custom_perms if role == self.custom_key else self.cat.roles.get(role, frozenset())
        src = "seed_accounts.CUSTOM_ROLE_PERMISSIONS" if role == self.custom_key else "catalogue.yaml"
        rep["permissions"] = {"org": len(rs.org), "project": len(rs.project), "expected": len(expected), "source": src,
                              "org_status": o["status"], "project_status": p["status"]}
        for scope, got in (("org", rs.org), ("project", rs.project)):
            extra, missing = sorted(got - expected), sorted(expected - got)
            if extra or missing:
                self.find("perms", role, scope, "mismatch", f"{self.origin}/v1/identity/me/permissions",
                          f"extra={extra} missing={missing}", f"exactly the {src} set for {role}")
            else:
                self.ok()

    def check_denied_render(self, role, kind, key, url, perm, scan, entry) -> None:
        entry["denied_permissions"] = scan["denied"]
        if perm in scan["denied"]:
            self.ok()
        else:
            self.find("denied-render", role, key, "no-access-denied", url,
                      f"AccessDenied with {scan['denied'] or 'none'}", f"[data-testid=access-denied][data-permission={perm}]")

    def check_allowed(self, page, role, kind, key, url, perm, scan, state, entry, rs: RoleState, at_project) -> None:
        if scan["denied"]:
            self.find("allowed-denied", role, key, "access-denied", url, f"AccessDenied {scan['denied']}",
                      f"the page opens (role holds {perm})")
        else:
            self.ok()
        page.wait_for_timeout(300)
        errs = [e for e in scan["errors"]]
        entry["error_banners"] = errs
        for e in errs:
            self.find("error-banner", role, key, re.sub(r"[^a-z]+", "-", e.lower())[:40], url, e, "no error banner on an allowed page")
        if not errs:
            self.ok()
        entry["console_errors"] = list(state["console"])
        for c in state["console"]:
            norm = re.sub(r"\d+", "N", re.sub(TOKEN_RE, "<id>", c))[:60]
            self.find("console-error", role, key, norm, url, c, "no console error", severity="soft")
        for b in state["bad"]:
            norm_path = re.sub(TOKEN_RE, "<id>", b["path"])
            sev = "hard"
            self.find("http-error", role, key, f"{b['method']} {norm_path} {b['status']}", url,
                      f"{b['method']} {b['path']} -> {b['status']} {b.get('code') or ''}".strip(),
                      "no same-origin /v1 or /api call fails on an allowed page" + (" (403: the page calls a route the role cannot use)" if b["status"] == 403 else ""),
                      severity=sev)
        if not state["bad"]:
            self.ok()
        # buttons
        gated = scan["gated"]
        entry["gated_controls"] = len(gated)
        for g in gated:
            want = "yes" if rs.holds(g["permission"], at_project, self.cat.scope) else "no"
            label = f"{g['permission']}:{g['text'][:30]}"
            if g["can"] != want:
                self.find("button-can", role, key, label, url,
                          f"data-can={g['can']} on '{g['text']}' ({g['permission']})", f"data-can={want}")
            elif want == "no" and not (g["ariaDisabled"] == "true" or g["disabled"]):
                self.find("button-disabled", role, key, label, url, f"'{g['text']}' data-can=no but not aria-disabled", "aria-disabled=true")
            elif want == "no" and g["permission"] not in g["title"] and "Needs" not in g["title"]:
                self.find("button-tooltip", role, key, label, url, f"'{g['text']}' title='{g['title']}'", f"a tooltip naming {g['permission']}")
            else:
                self.ok()
        # AREA_ACTIONS the role lacks: none found enabled; note the ones the page never gates
        acts = self.actions.get(key) or self.actions.get(key.lstrip("/").replace("account", "me"), {})
        missing_gate = []
        for action, ap in acts.items():
            if rs.holds(ap, at_project, self.cat.scope):
                continue
            ctl = [g for g in gated if g["permission"] == ap]
            if any(g["can"] == "yes" or not (g["ariaDisabled"] == "true" or g["disabled"]) for g in ctl):
                self.find("action-enabled", role, key, action, url, f"{action} ({ap}) enabled", "disabled with the reason")
            elif not ctl:
                missing_gate.append(f"{action}:{ap}")
        if missing_gate:
            entry["actions_not_found_gated"] = missing_gate
        if scan["ungated"]:
            entry["ungated_buttons"] = scan["ungated"][:60]

    def check_rail(self, role: str, rs: RoleState, seen: set[str], rep: dict[str, Any]) -> None:
        bad = sorted(a for a in seen if a in self.area_perm and not rs.holds(self.area_perm[a], True, self.cat.scope))
        allowed = sorted(a for a, p in self.area_perm.items() if rs.holds(p, True, self.cat.scope))
        rep["rail"] = {"seen": sorted(seen), "forbidden_shown": bad,
                       "allowed_not_in_rail": [a for a in allowed if a not in seen]}
        for a in bad:
            self.find("rail", role, a, "shown", self.area_url(a), f"rail shows {a} ({self.area_perm[a]})", "only areas the role may open")
        if not bad:
            self.ok()
        unknown = sorted(a for a in seen if a not in self.area_perm)
        if unknown:
            rep["rail"]["undeclared"] = unknown
            self.find("rail-undeclared", role, ",".join(unknown), "undeclared", self.origin, f"rail ids not in AREA_ACCESS: {unknown}",
                      "every rail entry declared", severity="info")

    def check_leaks(self, role: str, samples: list[dict[str, Any]], own: set[str], allowed_tokens: set[str]) -> None:
        mine = self.accounts[role].email
        for s in samples:
            owner = self.owner_tokens.get(s["url"])
            if owner is None:
                self.find("leak-baseline", role, s["key"], "no-owner-render", s["url"], "the Owner's render was not taken",
                          "an Owner baseline", severity="info")
                continue
            data = owner - own - {mine} - tokens_of(s["url"], self.emails)   # the URL's own id is not data
            s["entry"]["leak_check"] = {"owner_tokens": len(owner), "data_tokens_checked": len(data),
                                        "raw_tokens": len(s["raw"]), "dom_tokens": len(s["dom"])}
            for where in ("raw", "dom"):
                leaked = sorted(data & s[where])
                hard = [t for t in leaked if t not in allowed_tokens]
                soft = [t for t in leaked if t in allowed_tokens]
                if hard:
                    self.find(f"leak-{where}", role, s["key"], "data", s["url"],
                              f"{len(hard)} Owner data tokens in the {where} HTML: {hard[:8]}", "none of the Owner's data tokens")
                else:
                    self.ok()
                if soft:
                    self.find(f"leak-{where}-shared", role, s["key"], "shared", s["url"],
                              f"{len(soft)} tokens also shown to {role} on an allowed page: {soft[:8]}",
                              "nothing area-specific on a forbidden page", severity="soft")

    def tag(self, page: Any, on_account: bool) -> list[dict[str, Any]]:
        """Mark the page's enabled controls; a page still navigating is settled and asked again."""
        for _ in range(3):
            try:
                return page.evaluate(JS_TAG, [SKIP_PRESS.pattern, SKIP_PRESS_ACCOUNT.pattern, on_account])
            except Exception:  # noqa: BLE001
                self.settle(page)
        return []

    def click_all(self, page: Any, role: str, key: str, url: str, state: dict[str, Any], rep: dict[str, Any]) -> None:
        on_account = "/account" in url
        self.wait_healthy(page.context)
        deadline = time.time() + self.args.page_budget
        self.goto(page, url)
        state["url"] = url
        tags = self.tag(page, on_account)
        pressed = skipped = 0
        shell_done = state.setdefault("shell_pressed", set())
        for t in tags:
            if t["shell"] and not t["skip"]:
                # the console chrome (top bar, account menu) is the same on every page: press once per role
                if t["text"] in shell_done:
                    continue
                shell_done.add(t["text"])
            if t["skip"]:
                skipped += 1
                rep["clicks"].append({"page": key, "text": t["text"], "skipped": t["skip"]})
                continue
            if time.time() > deadline:
                rep["clicks"].append({"page": key, "text": t["text"], "skipped": "page time budget"})
                skipped += 1
                continue
            loc = page.locator(f'[data-fxd-i="{t["i"]}"]')
            try:
                gone = loc.count() == 0
            except Exception:  # noqa: BLE001
                gone = True
            if gone:
                self.goto(page, url)
                self.tag(page, on_account)
                loc = page.locator(f'[data-fxd-i="{t["i"]}"]')
            if loc.count() == 0:
                rep["clicks"].append({"page": key, "text": t["text"], "skipped": "gone after reload"})
                continue
            state["press"] = f"{key} :: {t['text']}"
            before = len(state["mut"])
            outcome = "pressed"
            try:
                loc.first.click(timeout=3_000)
                page.wait_for_timeout(700)
            except Exception as e:  # noqa: BLE001
                outcome = f"not clickable: {str(e).splitlines()[0][:80]}"
            for _ in range(2):
                try:
                    page.keyboard.press("Escape")
                except Exception:  # noqa: BLE001
                    pass
            page.wait_for_timeout(150)
            muts = state["mut"][before:]
            rep["clicks"].append({"page": key, "text": t["text"], "permission": t["permission"], "outcome": outcome,
                                  "requests": [{k: m[k] for k in ("method", "path", "status", "server_action")} for m in muts]})
            pressed += 1
            state["press"] = None
            try:
                cur = urlsplit(page.url)
            except Exception:  # noqa: BLE001
                cur = urlsplit("")
            if (cur.path, cur.query) != (urlsplit(url).path, urlsplit(url).query):
                self.goto(page, url)
                self.tag(page, on_account)
        print(f"  [{role}] clicks {key}: pressed {pressed}, skipped {skipped}")

    def judge_mutations(self, role: str, muts: list[dict[str, Any]], rep: dict[str, Any]) -> None:
        rep["mutations"] = [{k: m.get(k) for k in ("method", "path", "status", "action", "server_action", "refused_in_body", "ok_in_body", "error", "press", "page")} for m in muts]
        if role not in CLICK_ROLES:
            return
        self_service = [m for m in muts if m["path"].startswith(SELF_SERVICE)]
        rep["self_service_mutations"] = [{k: m.get(k) for k in ("method", "path", "status", "press")} for m in self_service]
        for m in muts:
            if m.get("press") and m["status"] == 403 and m not in self_service:
                # refused by the API, so safe; but the control was offered enabled (D7: grey it)
                self.find("ungated-press-refused", role, (m.get("press") or "").split(" :: ")[-1][:40],
                          re.sub(TOKEN_RE, "<id>", m["path"]), m.get("page") or "",
                          f"enabled control '{m.get('press')}' sent {m['method']} {m['path']} -> 403",
                          "a control the role cannot use is shown disabled with the reason", severity="soft")
            if m in self_service or not (200 <= m["status"] < 300):
                continue
            what = re.sub(TOKEN_RE, "<id>", m["path"]) + (f" action={m['action']}" if m.get("action") else "")
            if m["server_action"]:
                if m.get("refused_in_body") and not m.get("ok_in_body"):
                    continue
                if not m.get("press"):      # a page's own load (callAction reads, e.g. the process list)
                    self.find("server-action-on-load", role, m.get("page") or "?", "load", m.get("page") or "",
                              f"server action POST {m['path']} on page load answered {m['status']}", "reads only", severity="info")
                    continue
                self.find("mutation-2xx", role, m.get("press") or m.get("page") or "?", f"server-action {what}", m.get("page") or "",
                          f"server action POST {m['path']} answered {m['status']}, result "
                          + ("ok:true" if m.get("ok_in_body") else "with no refusal in it") + f" (press: {m.get('press')})",
                          "a refused write (the role holds no write permission)",
                          severity="hard" if m.get("ok_in_body") else "soft")
            else:
                self.find("mutation-2xx", role, m.get("press") or m.get("page") or "?", f"{m['method']} {what}", m.get("page") or "",
                          f"{m['method']} {m['path']}{' action=' + m['action'] if m.get('action') else ''} answered {m['status']} (press: {m.get('press')})",
                          "403 for a write the role does not hold")


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--console", default="http://localhost:53100")
    ap.add_argument("--accounts", required=True)
    ap.add_argument("--out", default=str(SCRATCH / "id-fxd"))
    ap.add_argument("--shots", default=str(SCRATCH / "id-fxd-shots"))
    ap.add_argument("--roles", nargs="*", default=list(ROLES))
    ap.add_argument("--only", nargs="*", default=None, help="area ids / page patterns to walk (debugging)")
    ap.add_argument("--no-click", action="store_true")
    ap.add_argument("--page-budget", type=int, default=150, help="seconds of pressing per page (viewer/auditor)")
    args = ap.parse_args(argv)
    try:
        sys.stdout.reconfigure(line_buffering=True)   # progress shows when piped to a log
    except Exception:  # noqa: BLE001
        pass

    import urllib.request
    try:
        urllib.request.urlopen(f"{args.console.rstrip('/')}/login", timeout=10)
    except Exception as e:  # noqa: BLE001
        print(f"the console at {args.console} is not answering ({type(e).__name__}); the stack is down — not walking")
        return 2

    from playwright.sync_api import sync_playwright

    walk = Walk(args)
    roles = list(args.roles)
    if "owner" not in roles:
        roles.insert(0, "owner")          # the Owner's render is the leak baseline
    roles.sort(key=lambda r: r != "owner")
    t0 = time.time()
    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        try:
            for role in roles:
                print(f"== {role}", flush=True)
                t = time.time()
                try:
                    walk.walk_role(browser, role)
                except Exception as e:  # noqa: BLE001
                    walk.find("walk", role, "role", "crashed", args.console, f"{type(e).__name__}: {e}"[:300], "the walk finishes")
                    traceback.print_exc()
                print(f"   {role} done in {time.time() - t:.0f}s", flush=True)
        finally:
            browser.close()

    rep = walk.report
    rep["seconds"] = round(time.time() - t0)
    rep["outage_retries"] = walk.outages
    rep["coverage"] = {r: {"areas": len(v.get("areas", [])), "pages": len(v.get("pages", [])),
                           "presses": sum(1 for c in v.get("clicks", []) if "outcome" in c)}
                       for r, v in rep["roles"].items()}
    rep["checks"] = walk.checks
    rep["known"] = [{"pattern": p, "owner": o, "note": n} for p, o, n in KNOWN]
    rep["benign"] = [{"pattern": p, "why": w} for p, w in BENIGN]
    rep["findings"] = [f.__dict__ for f in walk.findings]
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    (out / "role-walk.json").write_text(json.dumps(rep, indent=2, default=sorted))
    new = [f for f in walk.findings if f.status == "fail"]
    print(f"checks: {walk.checks}")
    for f in walk.findings:
        if f.status in ("fail", "xfail"):
            print(f"  {f.status.upper():5} {f.id}  [{f.owner}]  seen: {f.seen[:160]}")
    print(f"report: {out / 'role-walk.json'}; screenshots: {Path(args.shots) / 'roles'}")
    return 1 if new else 0


if __name__ == "__main__":
    sys.exit(main())
