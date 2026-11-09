"""DELETE PROJECT, DRIVEN LIKE A PERSON: fixtures.

A real Chromium drives the console's Delete project dialog against a running BOSS, at 1440 and at
390. Each test seeds its OWN project (the HelpDesk story every project page test uses: runs,
builds, decisions, work items, alerts, a knowledge graph, a server and its staging app), so the
suite never deletes anything it did not make. Every test also checks, at its key states:

  * no console error and no page error;
  * no /v1 response >= 400 except the ones the test provokes;
  * no horizontal scroll, and nothing in the dialog drawn outside it.

Environment (the suite is skipped without the first three):

  BOSS_E2E_CONSOLE       the console, e.g. http://localhost:3853
  BOSS_E2E_API           the API it proxies to, e.g. http://127.0.0.1:8853 (the dev launcher with
                         its /__dev/state seam: second factor fresh/lapsed, servers answered)
  BOSS_E2E_DATABASE_URL  the API's database: the seed writes the project's story there. Never
                         printed. Run with PYTHONPATH=.:engine:apps/api:apps/worker.
  BOSS_E2E_VIEWPORTS     default "1440,390".
  BOSS_E2E_ORG           default org_boss.
  BOSS_E2E_SHOTS         a folder for the dialog's screenshots and every failing test's.
"""
from __future__ import annotations

import json
import os
import re
import urllib.request
import uuid
from pathlib import Path
from typing import Any, Optional

import pytest

CONSOLE = os.environ.get("BOSS_E2E_CONSOLE", "").rstrip("/")
API = os.environ.get("BOSS_E2E_API", "").rstrip("/")
DB_URL = os.environ.get("BOSS_E2E_DATABASE_URL", "")
ORG = os.environ.get("BOSS_E2E_ORG", "org_boss")
VIEWPORTS = [int(v) for v in os.environ.get("BOSS_E2E_VIEWPORTS", "1440,390").split(",") if v.strip()]
SHOTS = os.environ.get("BOSS_E2E_SHOTS", "")

if not (CONSOLE and API and DB_URL):
    pytest.skip("BOSS_E2E_CONSOLE, BOSS_E2E_API and BOSS_E2E_DATABASE_URL are not set",
                allow_module_level=True)
try:
    from playwright.sync_api import Page, sync_playwright
except ImportError:  # pragma: no cover
    pytest.skip("playwright is not installed", allow_module_level=True)


# ── the API side ────────────────────────────────────────────────────────────────────────────

def api(method: str, path: str, body: Optional[dict] = None, timeout: float = 120) -> tuple[int, Any]:
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


def _engine():
    from sqlalchemy import create_engine
    return create_engine(DB_URL, future=True)


def seeded_project(prefix: str = "Ledger demo") -> dict:
    """A new project in the org, made through the API, with the HelpDesk story seeded."""
    from sqlalchemy import text

    from apps.api.tests.project_experience_seed import seed

    slug = f"del-{uuid.uuid4().hex[:8]}"
    name = f"{prefix} {slug[-4:]}"
    dev(mfa="fresh")
    status, body = api("POST", "/v1/projects", {"name": name, "slug": slug})
    assert status == 201, body
    eng = _engine()
    try:
        with eng.begin() as db:
            story = seed(db, slug)
            db.execute(text(
                "INSERT INTO app_previews (id, project_id, repo, branch, status, mode) VALUES "
                "(:i, :p, 'acme/helpdesk', 'main', 'running', 'process')"),
                {"i": f"prv_{uuid.uuid4().hex[:12]}", "p": slug})
    finally:
        eng.dispose()
    return {"id": slug, "name": name, "target_id": story["target_id"]}


def rows_of(project_id: str) -> int:
    from sqlalchemy import text
    eng = _engine()
    try:
        with eng.connect() as db:
            return int(db.execute(text("SELECT count(*) FROM decisions WHERE project_id = :p"),
                                  {"p": project_id}).scalar() or 0) + int(db.execute(text(
                "SELECT count(*) FROM projects WHERE id = :p"), {"p": project_id}).scalar() or 0)
    finally:
        eng.dispose()


def unique(prefix: str) -> str:
    return f"{prefix} {uuid.uuid4().hex[:6]}"


# ── the browser side ────────────────────────────────────────────────────────────────────────

MEASURE = r"""() => {
  const out = { hscroll: document.documentElement.scrollWidth > document.documentElement.clientWidth + 1, outside: [] };
  const d = document.querySelector('[data-testid="delete-dialog"]');
  if (!d) return out;
  const D = d.getBoundingClientRect();
  if (D.left < -1 || D.right > window.innerWidth + 1) out.outside.push(`the dialog [${Math.round(D.left)}..${Math.round(D.right)}] vs ${window.innerWidth}`);
  for (const el of d.querySelectorAll('*')) {
    const st = getComputedStyle(el);
    if (st.display === 'none' || st.visibility === 'hidden' || el.closest('.sr-only,svg')) continue;
    const r = el.getBoundingClientRect();
    if (!r.width || !r.height) continue;
    if (r.left < D.left - 1 || r.right > D.right + 1) out.outside.push(`${el.getAttribute('data-testid') || el.tagName} [${Math.round(r.left)}..${Math.round(r.right)}]`);
  }
  return out;
}"""


class Person:
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
        self.bad.append(f"{r.status} {r.request.method} {r.url[len(CONSOLE):]}")

    @property
    def phone(self) -> bool:
        return self.width < 600

    def go(self, query: str, wait: str) -> None:
        url = f"{CONSOLE}/?org={ORG}&{query}"
        try:
            self.p.goto(url, wait_until="domcontentloaded", timeout=120_000)
        except Exception:  # noqa: BLE001 — a dev server that stalled once: once more
            self.p.goto(url, wait_until="domcontentloaded", timeout=180_000)
        self.p.wait_for_selector(wait, timeout=180_000)

    def settings(self, project_id: str) -> None:
        self.go(f"project_id={project_id}&area=project&tab=settings", "[data-testid='px-set-danger']")

    def tid(self, testid: str):
        return self.p.locator(f"[data-testid='{testid}']")

    @property
    def dialog(self):
        return self.tid("delete-dialog")

    def ready(self) -> None:
        self.p.wait_for_selector("[data-testid='delete-dialog'][data-state='ready']", timeout=60_000)

    def shot(self, name: str) -> None:
        if SHOTS:
            Path(SHOTS).mkdir(parents=True, exist_ok=True)
            self.p.screenshot(path=str(Path(SHOTS) / f"{name}-{self.width}.png"))

    def expect(self, *patterns: str) -> None:
        self.allowed.extend(re.compile(p) for p in patterns)

    def check(self, where: str = "") -> None:
        m = self.p.evaluate(MEASURE)
        problems = []
        if m["hscroll"]:
            problems.append("the page scrolls sideways")
        problems += [f"outside the dialog: {x}" for x in m["outside"][:5]]
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
    kw: dict[str, Any] = dict(viewport={"width": width, "height": 844 if width < 600 else 900},
                              color_scheme="dark")
    if width < 600:
        kw.update(is_mobile=True, has_touch=True, device_scale_factor=2)
    ctx = browser.new_context(**kw)
    host = re.sub(r"^https?://", "", CONSOLE).split(":")[0].split("/")[0]
    ctx.add_cookies([{"name": "__Host-boss_session", "value": "dev", "domain": host, "path": "/",
                      "secure": True, "sameSite": "Lax"}])
    page = ctx.new_page()
    page.set_default_timeout(30_000)
    who = Person(page, width)
    dev(mfa="fresh", clear=True)
    yield who
    dev(mfa="fresh")
    failed = getattr(request.node, "rep_call", None)
    if SHOTS and failed is not None and failed.failed:
        Path(SHOTS, "fail").mkdir(parents=True, exist_ok=True)
        try:
            page.screenshot(path=str(Path(SHOTS, "fail") / f"{request.node.name}.png"), full_page=True)
        except Exception:  # noqa: BLE001
            pass
    ctx.close()


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_makereport(item, call):
    out = yield
    rep = out.get_result()
    if rep.when == "call":
        item.rep_call = rep
