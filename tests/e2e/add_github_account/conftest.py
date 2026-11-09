"""ADD A GITHUB ACCOUNT, DRIVEN LIKE A PERSON (BOSSAPP-945, agentic-factory 4-add-github-account
wireframe): fixtures.

A real Chromium drives Setup › Add a GitHub account against a running BOSS whose GitHub is the fake
(apps/api/tests/github_accounts_fake.py), at 1440 and at 390. The dev launcher's /__dev/gh seam does
what a person does on github.com: makes the app public, installs it on an account. Every test also
checks, at its key states:

  * no console error and no page error;
  * no /v1 response >= 400 except the ones the test provokes;
  * no horizontal scroll on the page, and nothing inside the sheet drawn outside its box.

Environment (the suite is skipped without them):

  BOSS_E2E_CONSOLE    the console, e.g. http://localhost:3845
  BOSS_E2E_API        the dev API it proxies to, e.g. http://127.0.0.1:8845 (with /__dev/gh)
  BOSS_E2E_VIEWPORTS  default "1440,390"
  BOSS_E2E_ORG / BOSS_E2E_PROJECT   default org_boss / helpdesk-ai-demo
  BOSS_E2E_SHOTS      a folder: a screenshot of every state (and of every failing test)

Run:  BOSS_E2E_CONSOLE=… BOSS_E2E_API=… python3 -m pytest -q -p no:cacheprovider tests/e2e/add_github_account
"""
from __future__ import annotations

import json
import os
import re
import urllib.request
from pathlib import Path
from typing import Any, Optional

import pytest

CONSOLE = os.environ.get("BOSS_E2E_CONSOLE", "").rstrip("/")
API = os.environ.get("BOSS_E2E_API", "").rstrip("/")
ORG = os.environ.get("BOSS_E2E_ORG", "org_boss")
PROJECT = os.environ.get("BOSS_E2E_PROJECT", "helpdesk-ai-demo")
VIEWPORTS = [int(v) for v in os.environ.get("BOSS_E2E_VIEWPORTS", "1440,390").split(",") if v.strip()]
SHOTS = os.environ.get("BOSS_E2E_SHOTS", "")

if not (CONSOLE and API):
    pytest.skip("BOSS_E2E_CONSOLE and BOSS_E2E_API are not set", allow_module_level=True)
try:
    from playwright.sync_api import Page, sync_playwright
except ImportError:  # pragma: no cover
    pytest.skip("playwright is not installed", allow_module_level=True)


def api(method: str, path: str, body: Optional[dict] = None, timeout: float = 30) -> tuple[int, Any]:
    req = urllib.request.Request(f"{API}{path}", method=method,
                                 data=json.dumps(body).encode() if body is not None else None,
                                 headers={"content-type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read()
            return r.status, (json.loads(raw) if raw else None)
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode(errors="replace")


def github(**state: Any) -> dict:
    """What a person does on github.com (the dev API's fake GitHub)."""
    status, body = api("POST", "/__dev/gh", state)
    if status == 404:
        pytest.skip("needs the dev API's /__dev/gh seam")
    assert status == 200, body
    return body


MEASURE = r"""() => {
  const out = { hscroll: document.documentElement.scrollWidth > document.documentElement.clientWidth + 1, outside: [] };
  const sheet = document.querySelector('.su-drawer');
  if (!sheet) return out;
  const S = sheet.getBoundingClientRect();
  const shown = (el) => { const st = getComputedStyle(el); return st.display !== 'none' && st.visibility !== 'hidden' && !el.closest('details:not([open]) > :not(summary)'); };
  for (const el of sheet.querySelectorAll('*')) {
    if (!shown(el)) continue;
    const r = el.getBoundingClientRect();
    if (!r.width || !r.height) continue;
    if (r.left < S.left - 1 || r.right > S.right + 1)
      out.outside.push(`${(el.getAttribute('data-testid') || el.className || el.tagName).toString().slice(0, 40)} [${Math.round(r.left)}..${Math.round(r.right)}] vs [${Math.round(S.left)}..${Math.round(S.right)}]`);
  }
  return out;
}"""


class Person:
    def __init__(self, page: Page, width: int, name: str):
        self.p = page
        self.width = width
        self.name = name
        self.errors: list[str] = []
        self.bad: list[str] = []
        self.allowed: list[re.Pattern] = []
        page.on("console", lambda m: self.errors.append(m.text[:400]) if m.type == "error" else None)
        page.on("pageerror", lambda e: self.errors.append(f"PAGEERROR {str(e)[:400]}"))
        page.on("response", self._response)

    def _response(self, r) -> None:
        if "/v1/" not in r.url or r.status < 400:
            return
        said = ""
        try:
            said = " " + r.text()[:200].replace("\n", " ")
        except Exception:  # noqa: BLE001
            pass
        self.bad.append(f"{r.status} {r.request.method} {r.url.split('?')[0][len(CONSOLE):]}{said}")

    @property
    def phone(self) -> bool:
        return self.width < 600

    def tid(self, testid: str):
        return self.p.locator(f"[data-testid='{testid}']")

    def open_sheet(self) -> None:
        q = f"?org={ORG}&project_id={PROJECT}&area=setup&focus=repository&open=add"
        try:
            self.p.goto(f"{CONSOLE}/{q}", wait_until="domcontentloaded", timeout=120_000)
        except Exception:  # noqa: BLE001 — next dev stalled once: once more
            self.p.goto(f"{CONSOLE}/{q}", wait_until="domcontentloaded", timeout=180_000)
        self.p.wait_for_selector("[data-testid='ga-input']", timeout=180_000)

    def look_up(self, text: str) -> None:
        box = self.tid("ga-input")
        box.fill(text)
        box.press("Enter")
        self.p.wait_for_selector("[data-testid='ga-found'], [data-testid='ga-error']", timeout=60_000)

    def path(self) -> str:
        return self.tid("ga-sheet").get_attribute("data-path") or ""

    def wait_path(self, want: str, timeout_s: float = 40) -> float:
        """BOSS notices on its own (it polls GitHub): the seconds it took."""
        import time
        t = time.monotonic()
        self.p.wait_for_selector(f"[data-testid='ga-sheet'][data-path='{want}']", timeout=timeout_s * 1000)
        return time.monotonic() - t

    def shot(self, state: str) -> None:
        if not SHOTS:
            return
        Path(SHOTS).mkdir(parents=True, exist_ok=True)
        self.p.wait_for_timeout(250)
        self.p.screenshot(path=str(Path(SHOTS) / f"{state}-{self.width}.png"), full_page=False)
        # The whole sheet too (it scrolls inside the drawer on a phone).
        sheet = self.p.locator(".su-drawer")
        if sheet.count():
            sheet.screenshot(path=str(Path(SHOTS) / f"{state}-{self.width}-sheet.png"))

    def expect(self, *patterns: str) -> None:
        self.allowed.extend(re.compile(p) for p in patterns)

    def check(self, where: str = "") -> None:
        m = self.p.evaluate(MEASURE)
        problems = []
        if m["hscroll"]:
            problems.append("the page scrolls sideways")
        problems += [f"outside the sheet: {x}" for x in m["outside"][:5]]
        dev_restart = re.compile(r"ERR_CONNECTION_REFUSED|ERR_INCOMPLETE_CHUNKED_ENCODING|Failed to fetch RSC payload")
        errs = [e for e in self.errors if not any(a.search(e) for a in self.allowed)
                and "Failed to load resource" not in e and "Download the React DevTools" not in e
                and not dev_restart.search(e)]
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
    kw: dict[str, Any] = dict(viewport={"width": width, "height": 844 if width < 600 else 900}, color_scheme="dark")
    if width < 600:
        kw.update(is_mobile=True, has_touch=True, device_scale_factor=2)
    ctx = browser.new_context(**kw)
    host = re.sub(r"^https?://", "", CONSOLE).split(":")[0].split("/")[0]
    ctx.add_cookies([{"name": "__Host-boss_session", "value": "dev", "domain": host, "path": "/", "secure": True,
                      "sameSite": "Lax"}])
    page = ctx.new_page()
    page.set_default_timeout(30_000)
    github(reset=True)
    api("POST", "/__dev/state", {"mfa": "fresh"})
    who = Person(page, width, request.node.name)
    yield who
    failed = getattr(request.node, "rep_call", None)
    if SHOTS and failed is not None and failed.failed:
        Path(SHOTS, "failed").mkdir(parents=True, exist_ok=True)
        try:
            page.screenshot(path=str(Path(SHOTS, "failed") / f"{request.node.name}.png"), full_page=True)
        except Exception:  # noqa: BLE001
            pass
    ctx.close()


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_makereport(item, call):
    out = yield
    rep = out.get_result()
    if rep.when == "call":
        item.rep_call = rep
