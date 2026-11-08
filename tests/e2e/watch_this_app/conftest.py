"""WATCH THIS APP, DRIVEN LIKE A PERSON (agentic-factory 2-watch-an-app.html): fixtures.

A real Chromium drives Project › Health › Watch this app against a running BOSS, at 1440 and at
390, and every test also checks, at the end and at its key states:

  * no console error and no page error;
  * no /v1 response >= 400 except the ones the test provokes (a refusal it asks for, the 409
    consent_required the first Start watching answers);
  * no horizontal scroll on the page, and nothing inside the sheet or the landing drawn outside
    it, and no chip, box value or watch line running out of its box.

THE OUTSIDE WORLD IS A DEV SEAM, NOT THE NETWORK. The dev API (BOSS_E2E_DEV_SEAMS=1):

  * POST /__dev/deliver runs the worker's own notify.deliver_due with a recorder instead of
    Slack: the test reads the 6-digit code the test message carries, as a person reads it in
    the channel;
  * POST /__dev/fire {target_id, kind, value | clear} hands the evaluator one rule's evaluation
    (as the sweep hands it Prometheus' answers) and runs app_alerts.evaluate_target, then
    triage_pending: the alert, its incident, routing, the SRE agent's triage and the response
    are BOSS's real code. No model answers on the dev stack, so triage records "no model".

Environment (the suite is skipped without the first three):

  BOSS_E2E_CONSOLE       the console, e.g. http://localhost:3871
  BOSS_E2E_API           the API it proxies to, e.g. http://127.0.0.1:8871
  BOSS_E2E_DATABASE_URL  the API's DISPOSABLE database (its name or search_path says "test" or
                         "walk"). The suite seeds two apps of the project (staging, prod) with a
                         week of health probes, and resets their watches before every test.
  BOSS_E2E_DEV_SEAMS     "1" when the API is the dev launcher with /__dev/state, /__dev/deliver
                         and /__dev/fire.
  BOSS_E2E_VIEWPORTS     default "1440,390".
  BOSS_E2E_ORG / BOSS_E2E_PROJECT   default org_boss / helpdesk-demo.
  BOSS_E2E_SHOTS         a folder for a screenshot of every failing test.
"""
from __future__ import annotations

import json
import os
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
STAGING = "t_wa_staging"
PROD = "t_wa_prod"
APP = "HelpDesk"
WORDS = "HelpDesk staging.\nWake me if replies stop sending or the inbox gets slow.\nTell #support-eng on Slack."
HOOK = "https://hooks.slack.example/T0001/B0001/e2e"

if not (CONSOLE and API and DB):
    pytest.skip("BOSS_E2E_CONSOLE, BOSS_E2E_API and BOSS_E2E_DATABASE_URL are not set", allow_module_level=True)
if not re.search(r"test|walk", DB.rsplit("/", 1)[-1], re.I):
    pytest.skip("BOSS_E2E_DATABASE_URL does not name a disposable database", allow_module_level=True)
if not SEAMS:
    pytest.skip("needs the dev API's seams (BOSS_E2E_DEV_SEAMS=1)", allow_module_level=True)
try:
    from playwright.sync_api import Page, sync_playwright
except ImportError:  # pragma: no cover
    pytest.skip("playwright is not installed", allow_module_level=True)


# ── the API side ────────────────────────────────────────────────────────────────────────────

def api(method: str, path: str, body: Optional[dict] = None, timeout: float = 90) -> tuple[int, Any]:
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


def deliver() -> list[dict]:
    """The outbox, delivered to the recorder: every message so far."""
    status, body = api("POST", "/__dev/deliver", {})
    assert status == 200, body
    return body["messages"]


def code_for(channel: str) -> str:
    """The code in the newest test message to the channel: delivered through the recorder,
    read as the channel holds it (a resend's message may wait on the channel's rate)."""
    deliver()
    rows = sql("SELECT n.body FROM notifications n JOIN contact_points c ON c.id = n.contact_point_id "
               "WHERE c.project_id = :p AND c.name = :n AND n.kind = 'test' ORDER BY n.created_at DESC LIMIT 1",
               p=PROJECT, n=channel)
    assert rows, f"no test message for {channel}"
    return re.search(r"\b(\d{6})\b", rows[0][0]).group(1)


def fire(target_id: str, kind: str = "error_rate", value: float = 9.0, clear: bool = False) -> dict:
    status, body = api("POST", "/__dev/fire", {"target_id": target_id, "kind": kind, "value": value, "clear": clear})
    assert status == 200, body
    return body


_ENGINE = None


def sql(statement: str, **params: Any) -> list:
    global _ENGINE
    from sqlalchemy import create_engine, text
    if _ENGINE is None:
        _ENGINE = create_engine(DB, pool_size=2, max_overflow=2)
    with _ENGINE.begin() as c:
        res = c.execute(text(statement), params)
        return list(res) if res.returns_rows else []


def rules_of(target_id: str) -> dict[str, dict]:
    rows = sql("SELECT kind, id, enabled, triage, auto_respond, params, labels, urgency, for_seconds FROM app_alert_rules "
               "WHERE target_id = :t AND kind <> 'custom'", t=target_id)
    return {r[0]: {"id": r[1], "enabled": r[2], "triage": r[3], "auto_respond": r[4], "params": r[5], "labels": r[6],
                   "urgency": r[7], "for_seconds": r[8]} for r in rows}


def seed() -> None:
    ws = sql("SELECT id FROM workspaces WHERE org_id = :o ORDER BY created_at LIMIT 1", o=ORG)
    sql("INSERT INTO projects (id, workspace_id, name, slug) VALUES (:p, :w, 'HelpDesk AI Demo', :p) ON CONFLICT DO NOTHING",
        p=PROJECT, w=ws[0][0])
    for tid, env, host in ((STAGING, "staging", "staging.helpdesk.example"), (PROD, "prod", "helpdesk.example")):
        sql("INSERT INTO deployment_targets (id, project_id, app_name, repo, environment, target_kind, target_ref, status, "
            " created_by, domains) VALUES (:t, :p, :a, 'acme/helpdesk', :e, 'ssh', 'deploy@203.0.113.20:22', 'active', 'e2e', "
            " CAST(:d AS jsonb)) ON CONFLICT DO NOTHING", t=tid, p=PROJECT, a=APP, e=env, d=json.dumps([f"https://{host}"]))
        sql("INSERT INTO deployment_runs (id, project_id, deployment_target_id, app_name, environment, repo, target_kind, "
            " target_ref, commit_ref, status, created_by, finished_at) VALUES (:id, :p, :t, :a, :e, 'acme/helpdesk', 'ssh', "
            " 'x', 'a1b2c3d4e5', 'succeeded', 'e2e', now() - interval '8 days') ON CONFLICT DO NOTHING",
            id=f"dr_{tid}", p=PROJECT, t=tid, a=APP, e=env)
        if not sql("SELECT 1 FROM deployment_health_samples WHERE deployment_target_id = :t LIMIT 1", t=tid):
            sql("INSERT INTO deployment_health_samples (id, project_id, deployment_target_id, checked_at, ok, status_code, latency_ms) "
                "SELECT 'dhs_' || :t || '_' || g, :p, :t, now() - make_interval(mins => g * 5), true, 200, 160 + (g % 7) * 6 "
                "FROM generate_series(0, 2015) g", t=tid, p=PROJECT)


def reset() -> None:
    """No watch on either app: their rules, alerts, incidents, routes and channels go."""
    ids = [STAGING, PROD]
    incs = [r[0] for r in sql("SELECT id FROM incidents WHERE project_id = :p AND (dedupe_key LIKE 'app_alert:t_wa_%' "
                              "OR dedupe_key LIKE '%t_wa_staging' OR dedupe_key LIKE '%t_wa_prod')", p=PROJECT)]
    sql("DELETE FROM app_watches WHERE target_id = ANY(:ids)", ids=ids)
    sql("DELETE FROM notification_policies WHERE project_id = :p AND rule_id IS NOT NULL", p=PROJECT)
    sql("DELETE FROM app_alerts WHERE target_id = ANY(:ids)", ids=ids)
    for table in ("incident_evidence", "remediations", "incident_recovery_checks", "app_signal_receipts", "sre_postmortems"):
        sql(f"DELETE FROM {table} WHERE incident_id = ANY(:i)", i=incs)
    sql("DELETE FROM incidents WHERE id = ANY(:i)", i=incs)
    sql("DELETE FROM app_alert_silences WHERE rule_id IN (SELECT id FROM app_alert_rules WHERE target_id = ANY(:ids))", ids=ids)
    sql("DELETE FROM app_alert_rules WHERE target_id = ANY(:ids)", ids=ids)
    sql("DELETE FROM contact_points WHERE project_id = :p AND kind <> 'console' AND created_by <> 'boss'", p=PROJECT)


def revoke_grants() -> None:
    """The person's standing grants go, so Start watching asks for consent again."""
    sql("UPDATE identity_delegation_grants SET status = 'revoked', revoked_at = now(), revoked_by = 'e2e' "
        "WHERE user_principal_id = 'usr_asdev' AND status = 'active'")


def unseed() -> None:
    """The two apps the suite seeded go too, so another suite on the same database (Add a
    server's deployments) starts from the project as it was."""
    reset()
    ids = [STAGING, PROD]
    for table, col in (("deployment_health_samples", "deployment_target_id"), ("deployment_runs", "deployment_target_id"),
                       ("monitored_services", "target_id"), ("app_metric_scrapes", "target_id")):
        try:
            sql(f"DELETE FROM {table} WHERE {col} = ANY(:ids)", ids=ids)
        except Exception:  # noqa: BLE001 — a table (or column) this install does not have
            pass
    sql("DELETE FROM deployment_targets WHERE id = ANY(:ids)", ids=ids)


@pytest.fixture(scope="session", autouse=True)
def seeded():
    seed()
    yield
    unseed()


@pytest.fixture(autouse=True)
def fresh():
    reset()
    dev(mfa="fresh")
    yield
    dev(mfa="fresh")


# ── the browser side ────────────────────────────────────────────────────────────────────────

MEASURE = r"""() => {
  if (!document.documentElement || !document.body) throw new Error('not loaded yet');
  const out = { hscroll: document.documentElement.scrollWidth > document.documentElement.clientWidth + 1, outside: [], clipped: [] };
  const roots = [document.querySelector('[data-testid="wa-sheet"]'), ...document.querySelectorAll('[data-testid^="wa-landing-"]'),
                 document.querySelector('[data-testid="px-h-watch-row"]')].filter(Boolean);
  const name = (el) => (el.getAttribute('data-testid') || (typeof el.className === 'string' ? el.className : '') || el.tagName).slice(0, 40)
     + (el.textContent ? ` "${el.textContent.trim().slice(0, 30)}"` : '');
  const shown = (el) => { const st = getComputedStyle(el); return st.display !== 'none' && st.visibility !== 'hidden' && !el.closest('[hidden],[aria-hidden="true"],svg,.sr-only'); };
  for (const root of roots) {
    const R = root.getBoundingClientRect();
    for (const el of root.querySelectorAll('*')) {
      if (!shown(el)) continue;
      const r = el.getBoundingClientRect();
      if (!r.width || !r.height) continue;
      if (r.left < R.left - 1 || r.right > R.right + 1) out.outside.push(`${name(el)} [${Math.round(r.left)}..${Math.round(r.right)}] vs [${Math.round(R.left)}..${Math.round(R.right)}]`);
      if (el.matches('.as-chip,.as-pill,.as-tag,.as-box-v,.as-box-s,.as-primary,.as-btn,.wa-tone,.wa-cx,.wa-sug,.ss-node b,.wa-fl-n b') && el.scrollWidth > el.clientWidth + 2) {
        const st = getComputedStyle(el);
        if (st.textOverflow !== 'ellipsis') out.clipped.push(`${name(el)} ${el.scrollWidth}>${el.clientWidth}`);
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

    def go(self, query: str = "tab=health", wait: str = "[data-testid='px-h-watch']") -> None:
        url = f"{CONSOLE}/?org={ORG}&area=project&project_id={PROJECT}&{query}"
        try:
            self.p.goto(url, wait_until="domcontentloaded", timeout=120_000)
        except Exception:  # noqa: BLE001 — a dev server that stalled once: once more
            self.p.goto(url, wait_until="domcontentloaded", timeout=180_000)
        if wait:
            self.p.wait_for_selector(wait, timeout=180_000)

    def tid(self, testid: str):
        return self.p.locator(f"[data-testid='{testid}']")

    def open_sheet(self) -> None:
        self.go()
        self.tid("px-h-watch").click()
        self.tid("wa-sheet").wait_for()

    def type(self, text: str = WORDS) -> None:
        """Type into the one input, and wait for the agent's read to land."""
        self.tid("wa-input").fill(text)
        self.p.wait_for_function(
            "() => { const el = document.querySelector('[data-testid=\"wa-reading\"]'); return el && !/reading/i.test(el.textContent || ''); }",
            timeout=60_000)
        self.p.wait_for_timeout(300)

    def box(self, key: str):
        self.tid(f"wa-box-{key}").click()
        pop = self.tid(f"wa-pop-{key}")
        pop.wait_for()
        return pop

    def slack_url(self, url: str = HOOK) -> None:
        self.box("tell")
        self.tid("wa-tell-url").fill(url)
        self.tid("wa-pop-done").click()

    def consent(self, grant: bool = True) -> bool:
        """The consent sheet, when the first Start watching asks: grant for 30 days."""
        sheet = self.tid("consent-sheet")
        try:
            sheet.wait_for(timeout=20_000)
        except Exception:  # noqa: BLE001 — a standing grant already covers it
            return False
        self.expect(r"409 POST /v1/projects/[^ ]+/watch .*consent_required", r"status of 409")
        if grant:
            self.tid("consent-standing").click()
            self.step_up_if_asked()
        else:
            self.p.keyboard.press("Escape")  # the consent sheet's own close: Esc
        sheet.wait_for(state="detached", timeout=30_000)
        return True

    def step_up_if_asked(self) -> None:
        s = self.tid("step-up-sheet")
        try:
            s.wait_for(timeout=3_000)
        except Exception:  # noqa: BLE001
            return
        self.step_up(confirm=True)

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

    def start(self, consent: bool = True) -> str:
        """Press Start watching; grant consent if asked; land on Health with the watched card."""
        self.tid("wa-submit").click()
        if consent:
            self.consent()
        self.tid("wa-sheet").wait_for(state="detached", timeout=60_000)
        card = self.p.locator("[data-testid^='wa-landing-']").first
        card.wait_for(timeout=60_000)
        return card.get_attribute("data-testid").replace("wa-landing-", "")

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
        # `next dev` RESTARTS ITSELF near its memory threshold ("Server is approaching the used
        # memory threshold, restarting…"): the page's in-flight requests to the DEV SERVER then
        # fail with these exact signatures. They are the dev server's, never the console's; any
        # /v1 failure is still caught below (self.bad).
        dev_restart = re.compile(r"ERR_CONNECTION_REFUSED|ERR_INCOMPLETE_CHUNKED_ENCODING|Failed to fetch RSC payload")
        errs = [e for e in self.errors if not any(a.search(e) for a in self.allowed)
                and "Download the React DevTools" not in e and not dev_restart.search(e)]
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
    host = re.sub(r"^https?://", "", CONSOLE).split(":")[0].split("/")[0]
    ctx.add_cookies([{"name": "__Host-boss_session", "value": "dev", "domain": host, "path": "/", "secure": True, "sameSite": "Lax"}])
    # `next dev`'s own badge sits over a phone's last row; it is the dev server's, not the console's.
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
