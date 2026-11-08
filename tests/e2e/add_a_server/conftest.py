"""ADD A SERVER, DRIVEN LIKE A PERSON (agentic-factory 3-add-a-server.html): fixtures.

A real Chromium drives Infra › Add a server against a running BOSS, at 1440 and at 390, and every
test also checks, at the end and at its key states:

  * no console error and no page error;
  * no /v1 response >= 400 except the ones the test provokes (a refusal it asks for);
  * no horizontal scroll on the page, and nothing inside the sheet or the server page drawn
    outside it, and no chip or box value running out of its box.

THE MACHINES ARE A DEV SEAM, NOT THE NETWORK. The dev API (BOSS_E2E_DEV_SEAMS=1) replaces the ONE
SSH transport every host call uses (deploy_service._ssh) with fake machines per address, which
let in only a key whose public half is in their authorized_keys. So "the key landed" is detected
by BOSS's real verify path, exactly as on a server; the test "runs the line" by handing the line
the page shows to POST /__dev/machine {run}. The network probe (port open, host key) answers from
the same fake machines. Docker disk on the laptop did not allow a local sshd container.

Environment (the suite is skipped without the first three):

  BOSS_E2E_CONSOLE       the console, e.g. http://localhost:3861
  BOSS_E2E_API           the API it proxies to, e.g. http://127.0.0.1:8861
  BOSS_E2E_DATABASE_URL  the API's DISPOSABLE database (its name or search_path says "test" or
                         "walk"). The suite removes the servers it added (TEST-NET 203.0.113.x)
                         and seeds one repository binding. Never printed.
  BOSS_E2E_DEV_SEAMS     "1" when the API is the dev launcher with /__dev/state and /__dev/machine.
  BOSS_E2E_VIEWPORTS     default "1440,390".
  BOSS_E2E_ORG / BOSS_E2E_PROJECT   default org_boss / helpdesk-demo.
  BOSS_E2E_SHOTS         a folder for a screenshot of every failing test.
"""
from __future__ import annotations

import json
import os
import random
import re
import urllib.request
from pathlib import Path
from typing import Any, Optional

import pytest

import sys as _sys

_sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import popover_guard as popguard  # noqa: E402 — tests/e2e/popover_guard.py, shared by the three suites

CONSOLE = os.environ.get("BOSS_E2E_CONSOLE", "").rstrip("/")
API = os.environ.get("BOSS_E2E_API", "").rstrip("/")
DB = os.environ.get("BOSS_E2E_DATABASE_URL", "")
SEAMS = os.environ.get("BOSS_E2E_DEV_SEAMS") == "1"
ORG = os.environ.get("BOSS_E2E_ORG", "org_boss")
PROJECT = os.environ.get("BOSS_E2E_PROJECT", "helpdesk-demo")
VIEWPORTS = [int(v) for v in os.environ.get("BOSS_E2E_VIEWPORTS", "1440,390").split(",") if v.strip()]
SHOTS = os.environ.get("BOSS_E2E_SHOTS", "")
STEP_UP_PASSWORD = "pxdev-pass"
NET = "203.0.113."

if not (CONSOLE and API and DB):
    pytest.skip("BOSS_E2E_CONSOLE, BOSS_E2E_API and BOSS_E2E_DATABASE_URL are not set", allow_module_level=True)
if not re.search(r"test|walk", DB.rsplit("/", 1)[-1], re.I):
    pytest.skip("BOSS_E2E_DATABASE_URL does not name a disposable database", allow_module_level=True)
if not SEAMS:
    pytest.skip("needs the dev API's machine seam (BOSS_E2E_DEV_SEAMS=1)", allow_module_level=True)
try:
    from playwright.sync_api import Page, sync_playwright
except ImportError:  # pragma: no cover
    pytest.skip("playwright is not installed", allow_module_level=True)


# ── the API side ────────────────────────────────────────────────────────────────────────────

def api(method: str, path: str, body: Optional[dict] = None, timeout: float = 60) -> tuple[int, Any]:
    req = urllib.request.Request(f"{API}{path}", method=method,
                                 data=json.dumps(body).encode() if body is not None else None,
                                 headers={"content-type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read()
            return r.status, (json.loads(raw) if raw else None)
    except urllib.error.HTTPError as e:
        raw = e.read()
        try:
            return e.code, json.loads(raw)
        except ValueError:
            return e.code, raw.decode(errors="replace")


def dev(**state: Any) -> dict:
    status, body = api("POST", "/__dev/state", state)
    assert status == 200, body
    return body


def machine(address: str, **kw: Any) -> dict:
    """Make (or change) a fake machine; `run=<line>` is a person running the line on it."""
    status, body = api("POST", "/__dev/machine", {"address": address, **kw})
    assert status == 200, body
    return body["machines"][address]


def sql(statement: str, **params: Any) -> list:
    from sqlalchemy import create_engine, text
    eng = create_engine(DB)
    try:
        with eng.begin() as c:
            res = c.execute(text(statement), params)
            return list(res) if res.returns_rows else []
    finally:
        eng.dispose()


def setup_of(host_id: str) -> dict:
    status, body = api("GET", f"/v1/servers/{host_id}/setup")
    assert status == 200, body
    return body


def host_named(name: str) -> Optional[dict]:
    rows = sql("SELECT id, name, address, ssh_user, ssh_port, status, labels, port_min, port_max, admin_identity_file "
               "FROM deployment_hosts WHERE project_id = :p AND name = :n", p=PROJECT, n=name)
    if not rows:
        return None
    r = rows[0]
    return {"id": r[0], "name": r[1], "address": r[2], "user": r[3], "port": r[4], "status": r[5], "labels": r[6],
            "port_min": r[7], "port_max": r[8], "identity": r[9]}


def fresh_address() -> str:
    used = {r[0] for r in sql("SELECT address FROM deployment_hosts WHERE address LIKE :a", a=f"{NET}%")}
    while True:
        a = f"{NET}{random.randint(10, 250)}"
        if a not in used:
            return a


def clear_servers() -> None:
    ids = [r[0] for r in sql("SELECT id FROM deployment_hosts WHERE address LIKE :a", a=f"{NET}%")]
    if not ids:
        return
    sql("DELETE FROM deployment_targets WHERE host_id = ANY(:ids)", ids=ids)
    keys = [r[0] for r in sql("SELECT key_secret_ref FROM deployment_hosts WHERE id = ANY(:ids) AND key_secret_ref IS NOT NULL", ids=ids)]
    sql("DELETE FROM deployment_hosts WHERE id = ANY(:ids)", ids=ids)
    sql("DELETE FROM secret_refs WHERE (scope_id = ANY(:ids) OR id = ANY(:k)) AND id NOT IN "
        "(SELECT key_secret_ref FROM deployment_hosts WHERE key_secret_ref IS NOT NULL)", ids=ids, k=keys)


def seed_repo() -> None:
    sql("INSERT INTO github_connections (id, project_id, connection_label, auth_method, credential_ref, created_by) "
        "VALUES ('ghc_as_e2e', :p, 'e2e', 'personal_access_token', 'env:BOSS_E2E_NONE', 'e2e') ON CONFLICT DO NOTHING", p=PROJECT)
    sql("INSERT INTO github_repository_bindings (id, project_id, connection_id, owner, name, full_name, created_by) "
        "VALUES ('grb_as_e2e', :p, 'ghc_as_e2e', 'acme', 'helpdesk', 'acme/helpdesk', 'e2e') ON CONFLICT DO NOTHING", p=PROJECT)


@pytest.fixture(autouse=True)
def fresh():
    clear_servers()
    dev(mfa="fresh", writes=False, reset_machines=True)
    yield
    dev(mfa="fresh", writes=False)


def clear_keys() -> None:
    """The stored keys the suite made (e2e-key-*), once no server uses them."""
    try:
        sql("DELETE FROM secret_refs WHERE ref_key ILIKE '%e2e_key_%' AND id NOT IN "
            "(SELECT key_secret_ref FROM deployment_hosts WHERE key_secret_ref IS NOT NULL)")
    except Exception:  # noqa: BLE001 — a key still referenced elsewhere stays
        pass


@pytest.fixture(scope="session", autouse=True)
def leave_it_clean():
    seed_repo()
    yield
    clear_servers()
    clear_keys()


# ── the browser side ────────────────────────────────────────────────────────────────────────

MEASURE = r"""() => {
  if (!document.documentElement || !document.body) throw new Error('not loaded yet');
  const out = { hscroll: document.documentElement.scrollWidth > document.documentElement.clientWidth + 1, outside: [], clipped: [] };
  const root = document.querySelector('[data-testid="as-sheet"]') || document.querySelector('[data-testid="inf-host-drawer"]')
            || document.querySelector('[data-testid="px-deployments"]');
  if (!root) return out;
  const R = root.getBoundingClientRect();
  const name = (el) => (el.getAttribute('data-testid') || (typeof el.className === 'string' ? el.className : '') || el.tagName).slice(0, 40)
     + (el.textContent ? ` "${el.textContent.trim().slice(0, 30)}"` : '');
  const shown = (el) => { const st = getComputedStyle(el); return st.display !== 'none' && st.visibility !== 'hidden' && !el.closest('[hidden],[aria-hidden="true"],svg,.sr-only,details:not([open]) > :not(summary)'); };
  for (const el of root.querySelectorAll('*')) {
    if (!shown(el)) continue;
    const r = el.getBoundingClientRect();
    if (!r.width || !r.height) continue;
    if (r.left < R.left - 1 || r.right > R.right + 1) out.outside.push(`${name(el)} [${Math.round(r.left)}..${Math.round(r.right)}] vs [${Math.round(R.left)}..${Math.round(R.right)}]`);
    if (el.matches('.as-chip,.as-pill,.as-tag,.as-box-v,.as-box-s,.as-primary,.as-btn,.ss-node b,.ss-stat b') && el.scrollWidth > el.clientWidth + 2) {
      const st = getComputedStyle(el);
      if (st.textOverflow !== 'ellipsis') out.clipped.push(`${name(el)} ${el.scrollWidth}>${el.clientWidth}`);
    }
  }
  return out;
}"""


class Person:
    """One person at one width: the page, what went wrong on it, and the presses."""

    def __init__(self, page: Page, width: int):
        self.p = page
        self.width = width
        self.errors: list[str] = []
        self.bad: list[str] = []
        self.allowed: list[re.Pattern] = []
        page.on("console", lambda m: self.errors.append(m.text[:400]) if m.type == "error" else None)
        page.on("pageerror", lambda e: self.errors.append(f"PAGEERROR {str(e)[:400]}"))
        page.on("dialog", lambda d: (self.errors.append("DIALOG " + d.message[:200]), d.dismiss()))
        page.on("response", self._response)

    def _response(self, r) -> None:
        if ("/v1/" not in r.url and "/api/" not in r.url) or r.status < 400:
            return
        said = ""
        try:
            said = " " + r.text()[:240].replace("\n", " ")
        except Exception:  # noqa: BLE001
            pass
        self.bad.append(f"{r.status} {r.request.method} {r.url.split('?')[0][len(CONSOLE):]}{said}")

    @property
    def phone(self) -> bool:
        return self.width < 600

    def go(self, query: str = "area=infra", wait: str = "[data-testid='infra']") -> None:
        url = f"{CONSOLE}/?org={ORG}&project_id={PROJECT}&{query}"
        try:
            self.p.goto(url, wait_until="domcontentloaded", timeout=120_000)
        except Exception:  # noqa: BLE001 — a dev server that stalled once: once more
            self.p.goto(url, wait_until="domcontentloaded", timeout=180_000)
        if wait:
            self.p.wait_for_selector(wait, timeout=180_000)

    def tid(self, testid: str):
        return self.p.locator(f"[data-testid='{testid}']")

    @property
    def sheet(self):
        return self.tid("as-sheet")

    def open_sheet(self) -> None:
        self.go("area=infra&add=server", wait="[data-testid='as-sheet']")

    def type(self, text: str) -> None:
        """Type into the one input, and wait for the read to settle."""
        box = self.tid("as-input")
        box.fill(text)
        self.p.wait_for_function(
            "() => { const el = document.querySelector('[data-testid=\"as-probe\"]'); return el && !/Probing|Reading…/.test(el.textContent || ''); }",
            timeout=30_000)
        self.p.wait_for_timeout(300)

    def box(self, key: str):
        self.tid(f"as-box-{key}").click()
        pop = self.tid(f"as-pop-{key}")
        pop.wait_for()
        return pop

    def step_up(self, confirm: bool) -> None:
        sheet = self.tid("step-up-sheet")
        sheet.wait_for(timeout=30_000)
        if not confirm:
            sheet.get_by_role("button", name="Cancel").click()
            sheet.wait_for(state="detached")
            return
        pw = sheet.locator("input[type='password']")
        if pw.count():
            pw.fill(STEP_UP_PASSWORD)
            sheet.get_by_role("button", name="Confirm").click()
        else:
            sheet.locator("input").first.fill("000000")
        sheet.wait_for(state="detached", timeout=30_000)

    def expect(self, *patterns: str) -> None:
        self.allowed.extend(re.compile(p) for p in patterns)

    def check(self, where: str = "") -> None:
        self.p.wait_for_load_state("domcontentloaded")
        try:
            m = self.p.evaluate(MEASURE)
        except Exception:  # noqa: BLE001 — measured mid-navigation
            self.p.wait_for_timeout(1500)
            m = self.p.evaluate(MEASURE)
        problems = []
        if m["hscroll"]:
            problems.append("the page scrolls sideways")
        problems += [f"outside: {x}" for x in m["outside"][:5]]
        problems += [f"runs out of its box: {x}" for x in m["clipped"][:5]]
        errs = [e for e in self.errors if not any(a.search(e) for a in self.allowed)
                and "Failed to load resource" not in e and "Download the React DevTools" not in e]
        bad = [b for b in self.bad if not any(a.search(b) for a in self.allowed)]
        problems += [f"console: {e}" for e in errs[:5]] + [f"http: {b}" for b in bad[:5]]
        # Every popover that opened on this page was whole in view (popover_guard).
        problems += [f"popover: {x}" for x in popguard.recorded(self.p)[:3]]
        assert not problems, f"{where} @{self.width}: " + "\n  ".join(problems)


@pytest.fixture(scope="session")
def browser():
    pw = sync_playwright().start()
    b = pw.chromium.launch(headless=os.environ.get("BOSS_E2E_HEADED") != "1")
    yield b
    b.close()
    pw.stop()


@pytest.fixture(params=VIEWPORTS, ids=lambda w: f"w{w}")
def person(request, browser):
    yield from _person(request, browser, request.param, 844 if request.param < 600 else 900)


@pytest.fixture(params=popguard.SIZES, ids=lambda s: f"{s[0]}x{s[1]}")
def sized(request, browser):
    """A person at each of the popover guard's sizes: 1440, 1024, 390 and a short 1440x760."""
    yield from _person(request, browser, *request.param)


def _person(request, browser, width: int, height: int):
    kw: dict[str, Any] = dict(viewport={"width": width, "height": height},
                              color_scheme="dark", timezone_id="UTC")
    if width < 600:
        kw.update(is_mobile=True, has_touch=True, device_scale_factor=2)
    ctx = browser.new_context(**kw)
    popguard.install(ctx)
    ctx.grant_permissions(["clipboard-read", "clipboard-write"], origin=CONSOLE)
    host = re.sub(r"^https?://", "", CONSOLE).split(":")[0].split("/")[0]
    ctx.add_cookies([{"name": "__Host-boss_session", "value": "dev", "domain": host, "path": "/", "secure": True, "sameSite": "Lax"}])
    # `next dev`'s own badge (bottom left) sits over a phone's last row; it is the dev server's,
    # not the console's, so it is hidden. Errors are still caught from the console itself.
    ctx.add_init_script("document.addEventListener('DOMContentLoaded', () => { const s = document.createElement('style');"
                        " s.textContent = 'nextjs-portal{display:none!important}'; document.head.appendChild(s); });")
    page = ctx.new_page()
    page.set_default_timeout(30_000)
    who = Person(page, width)
    yield who
    failed = getattr(request.node, "rep_call", None)
    if SHOTS and failed is not None and failed.failed:
        Path(SHOTS).mkdir(parents=True, exist_ok=True)
        try:
            page.screenshot(path=str(Path(SHOTS) / f"{request.node.name}.png"), full_page=True)
        except Exception:  # noqa: BLE001
            pass
    ctx.close()


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_makereport(item, call):
    out = yield
    rep = out.get_result()
    if rep.when == "call":
        item.rep_call = rep
