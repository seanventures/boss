"""ADD A PROJECT, DRIVEN LIKE A PERSON (new-project-v2 a/b/c wireframes): fixtures.

A real Chromium drives the console's Add a project sheet against a running BOSS, at 1440 and at
390, and every test also checks, at the end and at its key states:

  * no console error and no page error (React's "Maximum update depth" was the first bug);
  * no /v1 response >= 400 except the ones the test expects (a refusal it provokes);
  * no horizontal scroll on the page, and nothing inside the sheet drawn outside its box;
  * nothing in the sheet's body drawn under the footer or below the sheet.

Environment (the suite is skipped without the first two):

  BOSS_E2E_CONSOLE    the console, e.g. http://localhost:3843
  BOSS_E2E_API        the API it proxies to, e.g. http://127.0.0.1:8843 (read-backs of what
                      Create and start made: GET /v1/projects/{id}/add-project/started)
  BOSS_E2E_DEV_SEAMS  "1" when the API is the dev launcher with its /__dev/state seam (second
                      factor fresh/lapsed, model cli/none/slow, readiness refusal). Tests that
                      need a seam are skipped without it.
  BOSS_E2E_DATABASE_URL  optional: the API's database, to count projects by name (one press,
                      one project). Never printed.
  BOSS_E2E_VIEWPORTS  default "1440,390".
  BOSS_E2E_ORG / BOSS_E2E_PROJECT   default org_boss / helpdesk-ai-demo.
  BOSS_E2E_SHOTS      a folder for a screenshot of every failing test.

Run:  BOSS_E2E_CONSOLE=… BOSS_E2E_API=… BOSS_E2E_DEV_SEAMS=1 \\
      python3 -m pytest -q -p no:cacheprovider tests/e2e/add_project
"""
from __future__ import annotations

import json
import os
import re
import time
import urllib.request
import uuid
from pathlib import Path
from typing import Any, Iterable, Optional

import pytest

import sys as _sys

_sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import popover_guard as popguard  # noqa: E402 — tests/e2e/popover_guard.py, shared by the three suites

CONSOLE = os.environ.get("BOSS_E2E_CONSOLE", "").rstrip("/")
API = os.environ.get("BOSS_E2E_API", "").rstrip("/")
SEAMS = os.environ.get("BOSS_E2E_DEV_SEAMS") == "1"
ORG = os.environ.get("BOSS_E2E_ORG", "org_boss")
PROJECT = os.environ.get("BOSS_E2E_PROJECT", "helpdesk-ai-demo")
VIEWPORTS = [int(v) for v in os.environ.get("BOSS_E2E_VIEWPORTS", "1440,390").split(",") if v.strip()]
SHOTS = os.environ.get("BOSS_E2E_SHOTS", "")
#: The first understanding must be on screen within this many seconds of ↵.
FIRST_READ_S = 25

if not (CONSOLE and API):
    pytest.skip("BOSS_E2E_CONSOLE and BOSS_E2E_API are not set", allow_module_level=True)
try:
    from playwright.sync_api import Page, sync_playwright
except ImportError:  # pragma: no cover
    pytest.skip("playwright is not installed", allow_module_level=True)

IDEA = ("Expense tracker for our team: people snap a receipt, it reads the amount and vendor, "
        "a manager approves, and finance exports to CSV monthly.")
TEN_LINES = "\n".join([
    "A helpdesk for our support team.",
    "Customers email support@ and it becomes a ticket.",
    "AI drafts the reply from our past answers.",
    "Staff sign in with Google, edit and approve before it sends.",
    "Track the 4-hour first-reply SLA.",
    "Tag tickets by product.",
    "Send the lead a daily summary by email.",
    "Managers see a weekly chart of volume.",
    "Customers get a link to see their ticket.",
    "No chat or phone for now.",
])


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
    """Set the dev launcher's seams (second factor, model, readiness refusal)."""
    if not SEAMS:
        pytest.skip("needs the dev API's /__dev/state seam (BOSS_E2E_DEV_SEAMS=1)")
    status, body = api("POST", "/__dev/state", state)
    assert status == 200, body
    return body


def started(project_id: str) -> dict:
    status, body = api("GET", f"/v1/projects/{project_id}/add-project/started")
    assert status == 200, body
    return body


def project_count() -> Optional[int]:
    """How many projects there are (None without BOSS_E2E_DATABASE_URL): "nothing is created"."""
    url = os.environ.get("BOSS_E2E_DATABASE_URL")
    if not url:
        return None
    from sqlalchemy import create_engine, text
    eng = create_engine(url)
    try:
        with eng.connect() as c:
            return int(c.execute(text("SELECT count(*) FROM projects")).scalar() or 0)
    finally:
        eng.dispose()


def projects_named(name: str) -> Optional[int]:
    """How many projects carry this name (None without BOSS_E2E_DATABASE_URL)."""
    url = os.environ.get("BOSS_E2E_DATABASE_URL")
    if not url:
        return None
    from sqlalchemy import create_engine, text
    eng = create_engine(url)
    try:
        with eng.connect() as c:
            return int(c.execute(text("SELECT count(*) FROM projects WHERE name = :n"), {"n": name}).scalar() or 0)
    finally:
        eng.dispose()


def unique(prefix: str) -> str:
    return f"{prefix} {uuid.uuid4().hex[:6]}"


# ── the browser side ────────────────────────────────────────────────────────────────────────

#: What the guard measures in the sheet: every element's box against the sheet's, the body's
#: against the footer's top, and any text that runs out of its own box.
MEASURE = r"""() => {
  if (!document.documentElement || !document.body) throw new Error('not loaded yet');
  const out = { hscroll: document.documentElement.scrollWidth > document.documentElement.clientWidth + 1, outside: [], clipped: [], underFoot: [] };
  const sheet = document.querySelector('.npx-sheet');
  if (!sheet) return out;
  const S = sheet.getBoundingClientRect();
  const name = (el) => (el.getAttribute('data-testid') || (typeof el.className === 'string' ? el.className : '') || el.tagName).slice(0, 40)
     + (el.textContent ? ` "${el.textContent.trim().slice(0, 30)}"` : '');
  const shown = (el) => { const st = getComputedStyle(el); return st.display !== 'none' && st.visibility !== 'hidden' && !el.closest('[hidden],[aria-hidden="true"],svg,.sr-only,.ag-sr'); };
  for (const el of sheet.querySelectorAll('*')) {
    if (!shown(el)) continue;
    const r = el.getBoundingClientRect();
    if (!r.width || !r.height) continue;
    if (r.left < S.left - 1 || r.right > S.right + 1) out.outside.push(`${name(el)} [${Math.round(r.left)}..${Math.round(r.right)}] vs sheet [${Math.round(S.left)}..${Math.round(S.right)}]`);
    // A box's own value running out of it (text wider than the box, not clipped by ellipsis).
    if (el.matches('.npx-v,.npx-s,.npx-node,.npx-pchip,.npx-pick,.npx-tag,.px-tag,.npx-btn,.npx-primary,.px-btn,.npx-flink,.npx-opt') && el.scrollWidth > el.clientWidth + 2) {
      const st = getComputedStyle(el);
      if (st.overflow === 'visible' && st.textOverflow !== 'ellipsis') out.clipped.push(`${name(el)} ${el.scrollWidth}>${el.clientWidth}`);
    }
  }
  const foot = sheet.querySelector('.npx-foot');
  const body = sheet.querySelector('.npx-body');
  if (foot && body) {
    const F = foot.getBoundingClientRect();
    const vh = window.innerHeight;
    // What can be seen BELOW the footer: content drawn between the footer and the viewport's bottom.
    if (F.bottom < vh - 2 && F.bottom < S.bottom - 2) {
      for (const el of body.querySelectorAll('*')) {
        if (!shown(el) || el.children.length) continue;
        const r = el.getBoundingClientRect();
        if (r.height && r.top >= F.bottom - 1 && r.top < vh && r.bottom <= S.bottom + 1) { out.underFoot.push(`${name(el)} top=${Math.round(r.top)} foot=${Math.round(F.bottom)}`); if (out.underFoot.length > 4) break; }
      }
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
        self.calls: list[str] = []
        self.consented = 0
        self.started_anyway = 0
        # The crew's consent is asked with a 409 consent_required, then the same call goes again.
        self.allowed.append(re.compile(r"^409 POST /v1/(add-project|templates/[^ ]+/apply) \{\"error\":\"consent_required\""))
        page.on("console", lambda m: self.errors.append(m.text[:400]) if m.type == "error" else None)
        page.on("pageerror", lambda e: self.errors.append(f"PAGEERROR {str(e)[:400]}"))
        page.on("dialog", lambda d: (self.errors.append("DIALOG " + d.message[:200]), d.dismiss()))
        page.on("request", lambda r: self.calls.append(f"{r.method} {r.url}") if "/v1/" in r.url else None)
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

    # the page --------------------------------------------------------------------------------
    @property
    def phone(self) -> bool:
        return self.width < 600

    def go(self, query: str = "", area: str = "projects", wait: str = "[data-testid='px-projects'], [data-testid='px-add-project']") -> None:
        q = f"?org={ORG}&project_id={PROJECT}&area={area}" + (f"&{query}" if query else "")
        try:
            self.p.goto(f"{CONSOLE}/{q}", wait_until="domcontentloaded", timeout=120_000)
        except Exception:  # noqa: BLE001 — a dev server that stalled once (next dev, swap): once more
            self.p.goto(f"{CONSOLE}/{q}", wait_until="domcontentloaded", timeout=180_000)
        if wait:
            self.p.wait_for_selector(wait, timeout=180_000)

    def open_sheet(self, door: str = "idea") -> None:
        self.go(f"new_project={door}", wait="[data-testid='add-project']")
        self.p.wait_for_selector("[data-testid='ap-input']", timeout=60_000)

    def tid(self, testid: str):
        return self.p.locator(f"[data-testid='{testid}']")

    @property
    def sheet(self):
        return self.tid("add-project")

    @property
    def box(self):
        return self.tid("ap-input")

    def kind(self) -> str:
        return self.sheet.get_attribute("data-kind") or ""

    def type(self, text: str, delay: int = 0) -> None:
        self.box.click()
        if delay:
            self.box.press_sequentially(text, delay=delay)
        else:
            self.box.fill(text)

    def understood(self, timeout_s: float = 90) -> str:
        """Wait for Helm's brief (or its honest fallback) and say which."""
        sel = ",".join(f"[data-testid='ap-understood'][data-state='{s}']" for s in ("ok", "no_model", "timeout", "empty"))
        self.p.wait_for_selector(sel, timeout=timeout_s * 1000)
        return self.tid("ap-understood").get_attribute("data-state") or ""

    def idea_with_brief(self, text: str = IDEA) -> float:
        """Type an idea, press ↵, wait for the brief; the seconds it took."""
        self.type(text)
        t = time.monotonic()
        self.box.press("Enter")
        state = self.understood()
        took = time.monotonic() - t
        assert state == "ok", f"Helm's brief did not come back ({state}): {self.tid('ap-understood').inner_text()[:300]}"
        return took

    def pick_repo(self, full_name: str) -> None:
        """Pick a repository from the results the way a person does: ↵ on a free one. A repo
        already in a project opens that project on ↵, so for a new project on it the person says
        "Make a separate project anyway" and confirms (that picks it)."""
        self.type(full_name)
        row = self.p.locator("[data-testid='ap-results'] [role='option']").first
        row.wait_for()
        if row.get_attribute("data-in-use") is None:
            self.box.press("Enter")
            return
        rid = (row.get_attribute("data-testid") or "").removeprefix("ap-repo-")
        self.tid(f"ap-repo-{rid}-separate").click()
        self.tid(f"ap-repo-{rid}-separate-yes").click()
        self.tid("ap-results").wait_for(state="detached")

    def name(self, name: str) -> None:
        self.tid("ap-chip-name").click()
        inp = self.tid("ap-name-input")
        inp.fill(name)
        inp.press("Enter")
        assert name in self.tid("ap-chip-name").inner_text()

    def create(self, allow_egress: bool = True) -> None:
        """Press Create and start; when the connectors Helm picked talk to outside hosts, the
        sheet asks first (inline) and the person ticks it and presses again."""
        self.tid("ap-create").click()
        if not allow_egress:
            return
        ask = self.tid("ap-egress-allow")
        try:
            ask.wait_for(timeout=1500)
        except Exception:  # noqa: BLE001 — nothing to allow
            return
        if not ask.is_checked():
            ask.check()
            self.tid("ap-create").click()

    def landed(self, timeout_s: float = 90, start_anyway: bool = True) -> str:
        """The project Create and start landed on (its id from the URL)."""
        gone = (f"() => {{ const u = new URL(location.href); const p = u.searchParams.get('project_id'); "
                f"return !!p && p !== '{PROJECT}' && !u.searchParams.get('new_project'); }}")
        end = time.monotonic() + timeout_s
        while time.monotonic() < end:
            try:
                if self.p.evaluate(gone):
                    break
            except Exception:  # noqa: BLE001 — the page is navigating (that is the landing)
                self.p.wait_for_load_state("domcontentloaded")
                continue
            # The first start asks the person to let the crew act for them (the consent sheet):
            # they allow it, once.
            allow = self.p.locator("[data-testid='consent-durations'] button, [data-testid='consent-mr-allow']")
            if allow.count() and allow.first.is_visible() and allow.first.is_enabled():
                self.consented += 1
                allow.first.click()
            # What this dev install lacks (a mail server for an email channel …) is refused with
            # the readiness checklist: the person ticks each item and presses Start anyway.
            panel = self.tid("ap-readiness")
            if start_anyway and panel.count() and panel.is_visible():
                boxes = panel.locator("input[type='checkbox']:not(:checked)")
                if boxes.count():
                    for b in boxes.all():
                        b.check()
                    self.started_anyway += 1
                    self.tid("ap-create").click()
            self.p.wait_for_timeout(400)
        else:
            said = self.tid("ap-error").inner_text() if self.tid("ap-error").count() else ""
            raise AssertionError(f"did not land: {said!r}; http: {self.bad[-3:]}")
        return self.p.evaluate("() => new URL(location.href).searchParams.get('project_id')")

    def expect(self, *patterns: str) -> None:
        """Responses >= 400 the test provokes on purpose (regexes on 'STATUS METHOD /path')."""
        self.allowed.extend(re.compile(p) for p in patterns)

    # the guard ------------------------------------------------------------------------------
    def check(self, where: str = "") -> None:
        self.p.wait_for_load_state("domcontentloaded")
        try:
            m = self.p.evaluate(MEASURE)
        except Exception:  # noqa: BLE001 — measured mid-navigation (a landing): measure the new page
            self.p.wait_for_timeout(1500)
            self.p.wait_for_load_state("domcontentloaded")
            m = self.p.evaluate(MEASURE)
        problems = []
        if m["hscroll"]:
            problems.append("the page scrolls sideways")
        problems += [f"outside the sheet: {x}" for x in m["outside"][:5]]
        problems += [f"runs out of its box: {x}" for x in m["clipped"][:5]]
        problems += [f"drawn under the footer: {x}" for x in m["underFoot"][:3]]
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
                              color_scheme="dark", permissions=["clipboard-read", "clipboard-write"])
    if width < 600:
        kw.update(is_mobile=True, has_touch=True, device_scale_factor=2)
    ctx = browser.new_context(**kw)
    popguard.install(ctx)
    host = re.sub(r"^https?://", "", CONSOLE).split(":")[0].split("/")[0]
    ctx.add_cookies([{"name": "__Host-boss_session", "value": "dev", "domain": host, "path": "/", "secure": True, "sameSite": "Lax"}])
    page = ctx.new_page()
    page.set_default_timeout(30_000)
    who = Person(page, width)
    if SEAMS:
        api("POST", "/__dev/state", {"mfa": "fresh", "model": "cli", "refuse": False})
    yield who
    if SEAMS:
        api("POST", "/__dev/state", {"mfa": "fresh", "model": "cli", "refuse": False})
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


def lines_of(locator) -> int:
    return int(locator.evaluate("el => Math.round(el.getBoundingClientRect().height / (parseFloat(getComputedStyle(el).lineHeight) || 20))"))


def all_text(items: Iterable[str]) -> str:
    return " ".join(items)
