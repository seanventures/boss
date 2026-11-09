"""WHERE PEOPLE DECIDE, DRIVEN LIKE A PERSON (agentic-factory 1-where-people-decide.html): fixtures.

A real Chromium drives Govern › Where people decide against a running BOSS, at 1440 and at 390,
and every test also checks, at the end and at its key states:

  * no console error and no page error;
  * no /v1 response >= 400 except the ones the test provokes (a refusal it asks for);
  * no horizontal scroll on the page, and nothing inside the page's section drawn outside it.

Environment (the suite is skipped without the first three):

  BOSS_E2E_CONSOLE       the console, e.g. http://localhost:3851
  BOSS_E2E_API           the API it proxies to, e.g. http://127.0.0.1:8851
  BOSS_E2E_DATABASE_URL  the API's DISPOSABLE database (it must be a test one: its name or its
                         search_path says "test" or "walk"). The suite clears the list's saved
                         rules and seeds its own `dec_e2e_*` decisions, relative to now. Never
                         printed.
  BOSS_E2E_DEV_SEAMS     "1" when the API is a dev launcher with the /__dev/state seam (second
                         factor fresh/lapsed, model cli/none). Tests that need it are skipped
                         without it.
  BOSS_E2E_VIEWPORTS     default "1440,390".
  BOSS_E2E_ORG / BOSS_E2E_PROJECT   default org_boss / helpdesk-demo.
  BOSS_E2E_SHOTS         a folder for a screenshot of every failing test.

Run:  BOSS_E2E_CONSOLE=… BOSS_E2E_API=… BOSS_E2E_DATABASE_URL=… BOSS_E2E_DEV_SEAMS=1 \\
      python3 -m pytest -q -p no:cacheprovider tests/e2e/where_people_decide
"""
from __future__ import annotations

import json
import os
import re
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

import pytest

CONSOLE = os.environ.get("BOSS_E2E_CONSOLE", "").rstrip("/")
API = os.environ.get("BOSS_E2E_API", "").rstrip("/")
DB = os.environ.get("BOSS_E2E_DATABASE_URL", "")
SEAMS = os.environ.get("BOSS_E2E_DEV_SEAMS") == "1"
ORG = os.environ.get("BOSS_E2E_ORG", "org_boss")
PROJECT = os.environ.get("BOSS_E2E_PROJECT", "helpdesk-demo")
VIEWPORTS = [int(v) for v in os.environ.get("BOSS_E2E_VIEWPORTS", "1440,390").split(",") if v.strip()]
SHOTS = os.environ.get("BOSS_E2E_SHOTS", "")
#: The step-up sheet's dev password (the dev launcher's /v1/auth/step-up seam).
STEP_UP_PASSWORD = "pxdev-pass"

if not (CONSOLE and API and DB):
    pytest.skip("BOSS_E2E_CONSOLE, BOSS_E2E_API and BOSS_E2E_DATABASE_URL are not set", allow_module_level=True)
if not re.search(r"test|walk", DB.rsplit("/", 1)[-1], re.I):
    pytest.skip("BOSS_E2E_DATABASE_URL does not name a disposable database", allow_module_level=True)
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
    """Set the dev launcher's seams (second factor, model)."""
    if not SEAMS:
        pytest.skip("needs the dev API's /__dev/state seam (BOSS_E2E_DEV_SEAMS=1)")
    status, body = api("POST", "/__dev/state", state)
    assert status == 200, body
    return body


def board() -> dict:
    status, body = api("GET", f"/v1/decide?project_id={PROJECT}")
    assert status == 200, body
    return body


def card(key: str) -> dict:
    return next(c for c in board()["cards"] if c["key"] == key)


def sql(statement: str, **params: Any) -> list:
    from sqlalchemy import create_engine, text
    eng = create_engine(DB)
    try:
        with eng.begin() as c:
            res = c.execute(text(statement), params)
            return list(res) if res.returns_rows else []
    finally:
        eng.dispose()


#: The decisions the suite owns: (id suffix, question, recommendation, status, hours ago, by).
SEED = [
    ("merge_wait", "Merge PR #41: ticket export", {"action": "review_pull_request"}, "pending", 6, None),
    ("prod_wait", "Deploy HelpDesk v1.5 to production?", {"capability": "deploy", "environment": "production"}, "pending", 1, None),
    ("prod_done", "Deploy HelpDesk v1.4.2 to production?", {"capability": "deploy", "environment": "production"}, "approved", 3, "usr_wddev"),
    ("staging", "Deploy HelpDesk to staging", {"capability": "deploy", "environment": "staging"}, "approved", 2, "autonomy:full_auto"),
    ("sweep", "Run the nightly flaky-test sweep", {}, "approved", 4, "auto_approval"),
]
PROD_WAIT = "dec_e2e_prod_wait"


def reset_list() -> None:
    """The list back to its defaults (no saved rule, no change), and the suite's decisions fresh."""
    sql("DELETE FROM decide_rules")
    sql("DELETE FROM decide_rule_changes")
    sql("DELETE FROM decisions WHERE id LIKE 'dec_e2e_%'")
    now = datetime.now(timezone.utc)
    for suffix, question, rec, status, hours, by in SEED:
        at = now - timedelta(hours=hours)
        sql("INSERT INTO decisions (id, project_id, question, recommendation, risk_tier, status, approved_by, "
            "approved_at, created_at, updated_at) VALUES (:id, :p, :q, CAST(:r AS jsonb), 1, :s, :by, :aa, :at, :at)",
            id=f"dec_e2e_{suffix}", p=PROJECT, q=question, r=json.dumps(rec), s=status, by=by,
            aa=(at + timedelta(minutes=5)) if status == "approved" else None, at=at)


@pytest.fixture(autouse=True)
def fresh_list():
    reset_list()
    if SEAMS:
        api("POST", "/__dev/state", {"mfa": "fresh", "model": "cli"})
    yield
    if SEAMS:
        api("POST", "/__dev/state", {"mfa": "fresh", "model": "cli"})


@pytest.fixture(scope="session", autouse=True)
def leave_it_clean():
    yield
    reset_list()
    sql("DELETE FROM decisions WHERE id LIKE 'dec_e2e_%'")


# ── the browser side ────────────────────────────────────────────────────────────────────────

MEASURE = r"""() => {
  if (!document.documentElement || !document.body) throw new Error('not loaded yet');
  const out = { hscroll: document.documentElement.scrollWidth > document.documentElement.clientWidth + 1, outside: [], clipped: [] };
  const root = document.querySelector('[data-testid="wd"], [data-testid="wd-lanes"]');
  if (!root) return out;
  const R = root.getBoundingClientRect();
  const name = (el) => (el.getAttribute('data-testid') || (typeof el.className === 'string' ? el.className : '') || el.tagName).slice(0, 40)
     + (el.textContent ? ` "${el.textContent.trim().slice(0, 30)}"` : '');
  const shown = (el) => { const st = getComputedStyle(el); return st.display !== 'none' && st.visibility !== 'hidden' && !el.closest('[hidden],[aria-hidden="true"],svg,.sr-only'); };
  for (const el of root.querySelectorAll('*')) {
    if (!shown(el)) continue;
    const r = el.getBoundingClientRect();
    if (!r.width || !r.height) continue;
    if (r.left < R.left - 1 || r.right > R.right + 1) out.outside.push(`${name(el)} [${Math.round(r.left)}..${Math.round(r.right)}] vs [${Math.round(R.left)}..${Math.round(R.right)}]`);
    if (el.matches('.wd-chip,.wd-btn,.wd-tag,.wd-who,.wd-count,.wd-def,.wd-lbl,b') && el.scrollWidth > el.clientWidth + 2) {
      const st = getComputedStyle(el);
      if (st.overflow === 'visible' && st.textOverflow !== 'ellipsis' && st.display !== 'inline') out.clipped.push(`${name(el)} ${el.scrollWidth}>${el.clientWidth}`);
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
        if "/v1/" not in r.url or r.status < 400:
            return
        said = ""
        try:
            said = " " + r.text()[:240].replace("\n", " ")
        except Exception:  # noqa: BLE001 — a body already gone
            pass
        self.bad.append(f"{r.status} {r.request.method} {r.url.split('?')[0][len(CONSOLE):]}{said}")

    @property
    def phone(self) -> bool:
        return self.width < 600

    def go(self, query: str = "area=budget&tab=autonomy", wait: str = "[data-testid='wd-card']") -> None:
        url = f"{CONSOLE}/?org={ORG}&project_id={PROJECT}&{query}"
        try:
            self.p.goto(url, wait_until="domcontentloaded", timeout=120_000)
        except Exception:  # noqa: BLE001 — a dev server that stalled once (next dev, swap): once more
            self.p.goto(url, wait_until="domcontentloaded", timeout=180_000)
        if wait:
            self.p.wait_for_selector(wait, timeout=180_000)

    def tid(self, testid: str):
        return self.p.locator(f"[data-testid='{testid}']")

    def card(self, key: str):
        return self.p.locator(f"[data-testid='wd-card'][data-kind='{key}']")

    def open_card(self, key: str):
        self.card(key).click()
        pop = self.p.locator(f"[data-testid='wd-pop'][data-kind='{key}']")
        pop.wait_for()
        return pop

    def step_up(self, confirm: bool) -> None:
        """Answer "Confirm it's you": the dev password and Confirm, or Cancel."""
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
        """Responses >= 400 (and their console lines) the test provokes on purpose."""
        self.allowed.extend(re.compile(p) for p in patterns)

    def check(self, where: str = "") -> None:
        self.p.wait_for_load_state("domcontentloaded")
        try:
            m = self.p.evaluate(MEASURE)
        except Exception:  # noqa: BLE001 — measured mid-navigation: measure the new page
            self.p.wait_for_timeout(1500)
            m = self.p.evaluate(MEASURE)
        problems = []
        if m["hscroll"]:
            problems.append("the page scrolls sideways")
        problems += [f"outside the page: {x}" for x in m["outside"][:5]]
        problems += [f"runs out of its box: {x}" for x in m["clipped"][:5]]
        errs = [e for e in self.errors if not any(a.search(e) for a in self.allowed)
                and "Failed to load resource" not in e and "Download the React DevTools" not in e]
        bad = [b for b in self.bad if not any(a.search(b) for a in self.allowed)]
        problems += [f"console: {e}" for e in errs[:5]] + [f"http: {b}" for b in bad[:5]]
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
    width = request.param
    # UTC, so a quiet window the test draws around "now" means the same hours to the API.
    kw: dict[str, Any] = dict(viewport={"width": width, "height": 844 if width < 600 else 900},
                              color_scheme="dark", timezone_id="UTC")
    if width < 600:
        kw.update(is_mobile=True, has_touch=True, device_scale_factor=2)
    ctx = browser.new_context(**kw)
    host = re.sub(r"^https?://", "", CONSOLE).split(":")[0].split("/")[0]
    ctx.add_cookies([{"name": "__Host-boss_session", "value": "dev", "domain": host, "path": "/", "secure": True, "sameSite": "Lax"}])
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
