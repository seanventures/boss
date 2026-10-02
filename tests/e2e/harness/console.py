"""Sign in to the console in a real browser and walk every area, every page route, every declared
primary action and the milestone journeys — failing on console errors, failed requests, error
banners, and presses that show nothing.

WHY A BROWSER. The 214 web test files render single components to static markup or grep the
source; nothing pressed a button. So a screen could render perfectly in a test and still, in a
signed-in browser against an authed API, throw on hydration, call a route that answers 403 (the
agent wizard posting as `vp_agents` on boss-1) or show a red banner on first paint — and no test
would notice. This is the check that looks at the console the way an operator does.

What is walked comes from the code (discover.py): every entry of AREAS in ConsoleShell.tsx via
`/?area=<id>`, every static page route, and — for each area the sidebar shows — the sidebar
entry itself (`[data-area-nav=<id>]`). A new area or route is walked the day it is added; the
self-test then fails until its primary action is declared. Routes with a dynamic segment
(`/v2/missions/[id]`) are NOT walked; the report lists them as such rather than implying a link
was followed.

Primary actions come from a CONTRACT, not a CSS heuristic (console_contract.py): each area
names the controls an operator would press, by data-testid, and what must appear afterwards.
The first walk picked buttons whose class was exactly `primary`, pressed eight across thirty-six
areas, and passed every press that did nothing. Then the journeys (journeys.py) do the
milestone's jobs end to end: create a skill and register an MCP server (R1), create an agent
holding them and change it (R2), bind the skill and an MCP tool to workflow steps and run them
(R3), name an agent on one step and leave one to be picked, and run that (R4), plus two builder
smoke checks. On a live install, presses marked `write` and the journeys run only with
--allow-writes.

A finding fails the walk when it is one of:
  * a console error or an uncaught page error,
  * a same-origin /v1, /api or document response of 400 or more,
  * a visible error banner (`[role=alert]`, `.error-banner`) or Next's error overlay text,
  * an area whose content never rendered,
  * a declared action that is missing, cannot be pressed, or shows nothing afterwards,
  * a journey that does not reach its visible result.
Soft findings (reported, not failing): disabled buttons with no stated reason, host warnings,
write actions not pressed on a live install.

REQUESTS ARE OBSERVED, NOT RE-FETCHED. By default the walk records each response's status as
the browser saw it. The first walks re-fetched every /api and /v1 call through Playwright
(`route.fetch`) to capture failure bodies, and only those walks ever saw intermittent 502s
("Response.clone: Body has already been consumed") — possibly an artifact of the interception
itself. `--capture-bodies` turns interception back on for diagnosis, and every failure recorded
under it is marked "(under interception)".
"""
from __future__ import annotations

import json
import re
import sys
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Optional

from .console_contract import PRIMARY_ACTIONS, Press, selector_for
from .discover import console_areas, console_dynamic_routes, console_routes
from .journeys import JOURNEYS, Context as JourneyContext, run_journey
from .target import Target

DESTRUCTIVE = re.compile(
    r"\b(delete|remove|retire|purge|stop|cancel|disconnect|reset|revoke|discard|archive|"
    r"tear ?down|sign ?out|log ?out|uninstall|disable|reject|wipe|drop)\b", re.I)
# Routes that cannot be walked as a page: an OAuth landing that needs a provider's query string,
# and the sign-in page the walk has already used.
UNWALKABLE_ROUTES = {"/cloud/callback": "an OAuth return URL; needs the provider's code",
                     "/login": "used to sign in"}
ERROR_TEXT = re.compile(r"(Application error|Unhandled Runtime Error|Internal Server Error|"
                        r"Something went wrong|This page could not be found)", re.I)
SETTLE_TIMEOUT_MS = 90_000
ACTION_OUTCOME_SECONDS = 30

# EVERY SELECTOR THE WALK DEPENDS ON, AND WHERE THE CONSOLE PROMISES IT.
# (file under apps/web, literal that must appear, what the walk uses it for). The self-test
# enumerates this table, so renaming one of these breaks a fast test with this sentence instead
# of turning the whole walk into timeouts.
SOURCE_CONTRACT = (
    ("app/login/page.tsx", 'id="console-password"', "typing the console password"),
    ("app/org-gate.tsx", "data-testid={`org-card-${o.org_id}`}", "choosing the org"),
    ("app/ConsoleShell.tsx", 'data-testid="console-area" data-active={active}',
     "knowing which area rendered (main[data-active=<area>])"),
    ("app/ConsoleShell.tsx", "data-area-nav={a.id}", "pressing each sidebar entry"),
    ("app/ConsoleShell.tsx", 'data-testid="console-loading"', "waiting for the first paint"),
    ("app/host-advice.tsx", 'data-testid="host-advice"', "telling host trouble from screen trouble"),
    ("app/org-gate.tsx", "Reconnecting to the control plane", "waiting out the reconnect screen"),
)


@dataclass
class ScreenResult:
    kind: str                     # "area" | "route" | "action" | "nav"
    name: str
    url: str
    ok: bool = True
    console_errors: list[str] = field(default_factory=list)
    failed_requests: list[str] = field(default_factory=list)
    banners: list[str] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)
    soft: list[str] = field(default_factory=list)
    screenshot: Optional[str] = None
    seconds: float = 0.0


class Recorder:
    """Console errors and failed responses, collected per window of activity.

    Default: observe. `page.on("response")` records the status of every same-origin /api, /v1
    and document response exactly as the browser received it; bodies are not read (a body read
    on a response whose page navigated away never finishes, and has no timeout).

    With intercept=True (--capture-bodies) each /api and /v1 call is fetched by Playwright on the
    page's behalf and handed back, so a failure carries its body — but the walk then sits in the
    request path, so those findings are marked "(under interception)".
    """

    STREAMING = ("/api/preview-logs",)   # route handlers that stream; passed through untouched

    def __init__(self, page, origin: str, *, intercept: bool = False):
        self.origin = origin
        self.page = page
        self.intercept = intercept
        self.errors: list[str] = []
        self.failed: list[str] = []
        page.on("console", lambda m: self.errors.append(m.text[:400]) if m.type == "error" else None)
        page.on("pageerror", lambda e: self.errors.append(f"uncaught: {str(e)[:400]}"))
        page.on("response", self._on_response)
        if intercept:
            page.route(re.compile(r"^" + re.escape(origin) + r"/(api|v1)/"), self._through)

    def _through(self, route) -> None:
        request = route.request
        path = request.url[len(self.origin):]
        if path.startswith(self.STREAMING) or request.is_navigation_request():
            # A form post (sign-in) answers with a redirect the browser must follow itself.
            route.continue_()
            return
        try:
            response = route.fetch(timeout=120_000)
        except Exception as exc:  # noqa: BLE001 — the request itself failed
            self.failed.append(f"--- {request.method} {path[:200]} → {str(exc)[:200]} "
                               f"(under interception)")
            route.abort()
            return
        if response.status >= 400:
            said = ""
            try:
                said = " ".join((response.text() or "").split())[:240]
            except Exception:  # noqa: BLE001
                pass
            self.failed.append(f"{response.status} {request.method} {path[:200]}"
                               + (f" → {said}" if said else "") + " (under interception)")
        route.fulfill(response=response)

    def _on_response(self, response) -> None:
        if not response.url.startswith(self.origin) or response.status < 400:
            return
        path = response.url[len(self.origin):]
        is_document = response.request.resource_type == "document"
        if is_document or (not self.intercept and re.match(r"^/(api|v1)/", path)):
            self.failed.append(f"{response.status} {response.request.method} {path[:200]}")

    def take(self) -> tuple[list[str], list[str]]:
        errors, failed = self.errors, self.failed
        self.errors, self.failed = [], []
        return errors, failed


def _host_advice(page) -> list[str]:
    """host-advice.tsx: the console telling the operator their MACHINE is struggling (out of
    memory, stalled). True on a busy laptop, and about the laptop rather than the screen, so it
    is reported as a soft finding instead of failing the walk."""
    return page.evaluate("""() => Array.from(document.querySelectorAll('[data-testid=host-advice]'))
        .map(e => (e.innerText || '').trim().split('\\n')[0].slice(0, 200)).filter(Boolean)""") or []


def _banners(page) -> list[str]:
    found = page.evaluate("""() => {
        const visible = (e) => !!(e.offsetWidth || e.offsetHeight || e.getClientRects().length);
        const out = [];
        for (const e of document.querySelectorAll('[role=alert], .error-banner')) {
            const t = (e.innerText || '').trim();
            if (t && visible(e)) out.push(t.slice(0, 300));
        }
        return out;
    }""") or []
    body = page.evaluate("() => (document.body && document.body.innerText || '').slice(0, 20000)") or ""
    match = ERROR_TEXT.search(body)
    if match:
        found.append(f"page text: {match.group(0)}")
    return sorted(set(found))


RECONNECTING = "Reconnecting to the control plane"


def _settle(page, area: Optional[str]) -> Optional[str]:
    """Wait for the area's content to render and the first-load bar to go. None, or why not.

    THE RECONNECT SCREEN IS WAITED OUT ONCE, NOT IGNORED. page.tsx shows OrgReconnect when the
    org list fails to load and promises to retry every five seconds. The first walk found it
    stuck there for five minutes on a healthy API ("API returned 0"), so a screen still
    reconnecting after 30s is reloaded once — and reported either way, because an operator
    looking at it would not know to reload.
    """
    reloaded = False
    note: Optional[str] = None
    if area is not None:
        deadline = time.time() + SETTLE_TIMEOUT_MS / 1000
        stuck_since: Optional[float] = None
        while True:
            if page.query_selector(f'main[data-active="{area}"]'):
                break
            text = page.evaluate("() => document.body ? document.body.innerText.slice(0, 2000) : ''") or ""
            if RECONNECTING in text:
                stuck_since = stuck_since or time.time()
                if time.time() - stuck_since > 30 and not reloaded:
                    reason = re.search(r"\(([^)]*)\)", text)
                    note = (f"stuck on '{RECONNECTING}…' for 30s"
                            f"{' (' + reason.group(1) + ')' if reason else ''}; reloaded once")
                    page.reload(wait_until="domcontentloaded")
                    reloaded, stuck_since = True, None
            if time.time() > deadline:
                return (note + "; still not rendered" if note else
                        f"did not finish rendering within {SETTLE_TIMEOUT_MS // 1000}s")
            page.wait_for_timeout(500)
    try:
        page.wait_for_selector('[data-testid="console-loading"]', state="detached",
                               timeout=SETTLE_TIMEOUT_MS)
    except Exception as exc:  # noqa: BLE001 — a screen that never settles is the finding
        return f"did not finish rendering within {SETTLE_TIMEOUT_MS // 1000}s: {str(exc)[:200]}"
    try:
        page.wait_for_load_state("networkidle", timeout=15_000)
    except Exception:  # noqa: BLE001 — live panels poll forever; the content is already there
        pass
    return note


def _disabled_without_reason(page) -> list[str]:
    return page.evaluate("""() => {
        const visible = (e) => !!(e.offsetWidth || e.offsetHeight || e.getClientRects().length);
        const out = [];
        for (const b of document.querySelectorAll('main button[disabled]')) {
            if (!visible(b)) continue;
            const why = b.getAttribute('title') || b.getAttribute('aria-describedby')
                     || b.getAttribute('aria-label');
            if (!why) out.push((b.innerText || '').trim().slice(0, 60) || '(unlabelled)');
        }
        return out;
    }""") or []


def _edition(target: Target) -> str:
    """The install's edition as its API states it; paid-only controls are skipped on Community."""
    from .client import owner_api

    try:
        body = owner_api(target).get("/v1/edition", project_id=target.project_id)
        return str((body or {}).get("edition_id") or "")
    except Exception:  # noqa: BLE001 — unknown: every declared control is pressed
        return ""


def visible_control(page, testid: str):
    """The first VISIBLE element with this testid (or `x-*` prefix). EVERY control a press
    touches goes through here — the target, each field it fills, each follow-up it presses.

    WHY ONE HELPER. Several screens render a control twice, a dock copy and a page copy, and
    one of them is hidden. Round two fixed "pressed the hidden instance" for the target and the
    follow-ups one call at a time and left `fill` on `.first`, so the hidden copy got the text
    and the visible form was submitted empty. The self-test now reads _press and refuses any
    locator built from a testid that does not come from this function."""
    return page.locator(f"{selector_for(testid)} >> visible=true").first


def _main_text(page) -> str:
    return page.evaluate("() => { const m = document.querySelector('main'); "
                         "return m ? m.innerText : ''; }") or ""


class Walk:
    def __init__(self, target: Target, out_dir: Path, *, allow_writes: bool, headed: bool,
                 capture_bodies: bool = False):
        self.target = target
        self.capture_bodies = capture_bodies
        self.edition = _edition(target)
        self.origin = (target.console_url or "").rstrip("/")
        self.out_dir = out_dir
        self.shots = out_dir / "console"
        self.shots.mkdir(parents=True, exist_ok=True)
        self.allow_writes = allow_writes
        self.headed = headed
        self.results: list[ScreenResult] = []
        self.org_id: Optional[str] = None
        self.default_org = "org_boss"  # BOSS_DEFAULT_ORG_ID on an installed BOSS

    def _finish(self, page, rec: Recorder, result: ScreenResult, started: float,
                shot_name: str) -> ScreenResult:
        errors, failed = rec.take()
        result.console_errors += errors
        result.failed_requests += failed
        result.banners += _banners(page)
        result.seconds = round(time.time() - started, 1)
        path = self.shots / f"{re.sub(r'[^a-zA-Z0-9_.-]+', '_', shot_name)}.png"
        try:
            page.screenshot(path=str(path), full_page=False)
            result.screenshot = str(path)
        except Exception:  # noqa: BLE001
            pass
        result.ok = not (result.console_errors or result.failed_requests or result.banners
                         or result.problems)
        self.results.append(result)
        return result

    def sign_in(self, page, rec: Recorder) -> None:
        page.goto(f"{self.origin}/login", wait_until="domcontentloaded")
        page.fill("#console-password", self.target.console_password or "")
        page.click("button[type=submit]")
        page.wait_for_url(lambda url: "/login" not in url, timeout=60_000)
        rec.take()  # sign-in noise (the 401 that sends you to /login) is not a finding
        self.choose_org(page, rec)

    def choose_org(self, page, rec: Recorder) -> None:
        """The org gate stands in front of every area. Press "Enter" on the target's org, as an
        operator's first click does, and record how that went as a screen of its own.

        The walk then carries the org on every URL instead of relying on that press: the first
        walk found "Enter" doing nothing visible for over 90 seconds (router.push waits for the
        whole server render of the console), and one slow press must not hide the other 49
        screens behind it.
        """
        started = time.time()
        result = ScreenResult("route", "org gate: Enter", f"{self.origin}/")
        page.goto(f"{self.origin}/", wait_until="domcontentloaded")
        cards = ('[data-testid^="org-card-"]:not([data-testid="org-card-onboard"]), '
                 'button.org-card:not(.org-card-add)')
        try:
            page.wait_for_selector(f"{cards}, main[data-active]", timeout=SETTLE_TIMEOUT_MS)
        except Exception as exc:  # noqa: BLE001
            result.problems.append(f"neither the org gate nor the console rendered: {str(exc)[:200]}")
            self._finish(page, rec, result, started, "org-gate")
            return
        card = (page.query_selector(f'[data-testid="org-card-{self.default_org}"]')
                or page.query_selector(cards))
        if card is None:
            # Already inside an org (one remembered in this browser): nothing to choose.
            found = re.search(r"[?&]org=([^&#]+)", page.url)
            self.org_id = found.group(1) if found else self.default_org
            return
        testid = card.get_attribute("data-testid") or ""
        self.org_id = testid[len("org-card-"):] if testid.startswith("org-card-") else self.default_org
        card.click()
        try:
            page.wait_for_selector("main[data-active]", timeout=SETTLE_TIMEOUT_MS)
        except Exception:  # noqa: BLE001
            result.problems.append(
                f"pressing Enter on the {self.org_id} card did not open the console within "
                f"{SETTLE_TIMEOUT_MS // 1000}s — the org gate looks dead while the whole console "
                f"renders behind it")
        self._finish(page, rec, result, started, "org-gate")

    def area_url(self, area_id: str) -> str:
        # The org rides on every area URL: without it the org gate answers, and its stored-org
        # redirect drops the area — every deep link would land on the default screen.
        org = f"org={self.org_id}&" if self.org_id else ""
        return f"{self.origin}/?{org}area={area_id}"

    def area(self, page, rec: Recorder, area_id: str, nav_visible: bool) -> None:
        started = time.time()
        result = ScreenResult("area", area_id, self.area_url(area_id))
        page.goto(result.url, wait_until="domcontentloaded")
        problem = _settle(page, area_id)
        if problem:
            result.problems.append(problem)
        result.soft += [f"disabled button without a reason: {b}" for b in _disabled_without_reason(page)]
        result.soft += [f"host advice shown: {a}" for a in sorted(set(_host_advice(page)))]
        self._finish(page, rec, result, started, f"area-{area_id}")
        if not problem:
            self.actions(page, rec, area_id)
        if nav_visible:
            self.nav(page, rec, area_id)

    def nav(self, page, rec: Recorder, area_id: str) -> None:
        """The sidebar entry, pressed: does it take you to that area?"""
        started = time.time()
        result = ScreenResult("nav", area_id, f"{self.origin}/ [data-area-nav={area_id}]")
        try:
            page.goto(self.area_url("overview"), wait_until="domcontentloaded")
            _settle(page, "overview")
            rec.take()
            page.click(f'[data-area-nav="{area_id}"]', timeout=15_000)
            problem = _settle(page, area_id)
            if problem:
                result.problems.append(f"pressing the sidebar entry: {problem}")
        except Exception as exc:  # noqa: BLE001
            result.problems.append(f"sidebar entry [data-area-nav={area_id}] not pressable: "
                                   f"{str(exc)[:200]}")
        self._finish(page, rec, result, started, f"nav-{area_id}")

    def actions(self, page, rec: Recorder, area_id: str) -> None:
        """Each declared Press of this area, from a FRESH load of the area, then checked for the
        outcome the contract names. Journeys run later, in order (one feeds the next)."""
        entries = PRIMARY_ACTIONS.get(area_id)
        if entries is None:
            result = ScreenResult("action", f"{area_id}: (none declared)", self.area_url(area_id))
            result.problems.append(f"area {area_id} has no entry in console_contract."
                                   f"PRIMARY_ACTIONS; declare its primary action")
            self._finish(page, rec, result, time.time(), f"action-{area_id}-undeclared")
            return
        for n, entry in enumerate(e for e in entries if isinstance(e, Press)):
            label = f"{area_id}: {entry.testid}"
            result = ScreenResult("action", label, self.area_url(area_id))
            if entry.paid_only and self.edition == "community":
                result.soft.append("a paid-edition control; Community does not ship it")
                self.results.append(result)
                continue
            if entry.write and not self.allow_writes:
                result.soft.append("changes state; not pressed on a live install without "
                                   "--allow-writes")
                self.results.append(result)
                continue
            if n:
                page.goto(self.area_url(area_id), wait_until="domcontentloaded")
                _settle(page, area_id)
            rec.take()
            started = time.time()
            try:
                self._press(page, entry, result)
            except Exception as exc:  # noqa: BLE001
                result.problems.append(f"could not press [{entry.testid}]: "
                                       f"{' '.join(str(exc).split())[:240]}")
            self._finish(page, rec, result, started, f"action-{area_id}-{entry.testid[:30]}")
            page.keyboard.press("Escape")

    def _press(self, page, entry: Press, result: ScreenResult) -> None:
        if entry.unless and page.locator(entry.unless).count():
            result.soft.append(f"already done ({entry.unless} is showing); not pressed again")
            return
        target = visible_control(page, entry.testid)
        try:
            target.wait_for(state="visible", timeout=20_000)
        except Exception:  # noqa: BLE001
            result.problems.append(f"the declared action [{entry.testid}] is not on screen "
                                   f"(declared in apps/web/{entry.source})")
            return
        for testid, text in entry.fill:
            visible_control(page, testid).fill(text, timeout=10_000)
        before_text = _main_text(page)
        before_count = page.locator(entry.grows).count() if entry.grows else 0
        if entry.opens:
            if target.get_attribute("open") is None:
                target.locator("summary").first.click(timeout=10_000)
        else:
            target.click(timeout=10_000)
        for testid in entry.then:
            visible_control(page, testid).click(timeout=15_000)
        deadline = time.time() + ACTION_OUTCOME_SECONDS
        while time.time() < deadline:
            # ANY visible match: the dock's composer is the visible one of two agent-send
            # buttons, and `.first` alone picked the hidden one and called a working press dead.
            if entry.expect and page.locator(f"{entry.expect} >> visible=true").count():
                return
            if entry.grows and page.locator(entry.grows).count() > before_count:
                return
            if not entry.expect and not entry.grows and _main_text(page) != before_text:
                return
            refused = page.locator('[data-ok="false"]')
            if refused.count() and refused.first.is_visible():
                result.problems.append(f"pressing [{entry.testid}] was refused: "
                                       f"{' '.join(refused.first.inner_text().split())[:240]}")
                return
            page.wait_for_timeout(500)
        wanted = (f"{entry.expect} to appear" if entry.expect
                  else f"more {entry.grows}" if entry.grows else "the screen to change")
        result.problems.append(f"pressing [{entry.testid}] showed nothing: waited "
                               f"{ACTION_OUTCOME_SECONDS}s for {wanted}")

    def journeys(self, page, rec: Recorder, wanted: set[str]) -> None:
        """The milestone's operator journeys (journeys.py), in order, on fresh area loads."""
        if not self.allow_writes:
            for requirement, name, _ in JOURNEYS:
                result = ScreenResult("journey", f"{requirement} {name}", self.origin)
                result.soft.append("creates things; not run on a live install without "
                                   "--allow-writes")
                self.results.append(result)
            return
        ctx = JourneyContext(stamp=time.strftime("%H%M%S"), fakes_url=self.target.fakes_url,
                             target=self.target)

        def goto(area_id: str) -> None:
            page.goto(self.area_url(area_id), wait_until="domcontentloaded")
            problem = _settle(page, area_id)
            if problem:
                raise AssertionError(f"area {area_id} did not render: {problem}")

        for requirement, name, fn in JOURNEYS:
            if wanted and name not in wanted:
                continue
            print(f"  journey {requirement} {name}", flush=True)
            rec.take()
            started = time.time()
            result = ScreenResult("journey", f"{requirement} {name}", self.origin)
            ok, said = run_journey(fn, page, ctx, goto)
            (result.soft if ok else result.problems).append(said)
            self._finish(page, rec, result, started, f"journey-{name}")
            page.keyboard.press("Escape")

    def route(self, page, rec: Recorder, route: str) -> None:
        started = time.time()
        result = ScreenResult("route", route, f"{self.origin}{route}")
        response = page.goto(result.url, wait_until="domcontentloaded")
        if response is not None and response.status >= 400:
            result.problems.append(f"the page itself answered {response.status}")
        page.wait_for_timeout(4_000)
        self._finish(page, rec, result, started, f"route{route}")

    def run(self, only: list[str]) -> list[ScreenResult]:
        from playwright.sync_api import sync_playwright

        from .discover import Area

        areas: list[Area] = [a for a in console_areas() if not only or a.id in only]
        routes = [r for r in console_routes() if r not in UNWALKABLE_ROUTES
                  and (not only or r in only)]
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=not self.headed)
            context = browser.new_context(viewport={"width": 1440, "height": 900})
            page = context.new_page()
            rec = Recorder(page, self.origin, intercept=self.capture_bodies)
            self.sign_in(page, rec)
            page.goto(self.area_url("overview"), wait_until="domcontentloaded")
            _settle(page, "overview")
            in_sidebar = set(page.eval_on_selector_all(
                "[data-area-nav]", "els => els.map(e => e.getAttribute('data-area-nav'))") or [])
            rec.take()
            import faulthandler

            for area in areas:
                print(f"  area {area.id}", flush=True)
                # A screen that takes ten minutes is stuck somewhere: say where, to stderr.
                faulthandler.dump_traceback_later(600, exit=False)
                self.area(page, rec, area.id, nav_visible=area.id in in_sidebar)
                faulthandler.cancel_dump_traceback_later()
            for route in routes:
                print(f"  route {route}", flush=True)
                self.route(page, rec, route)
            journey_names = {name for _, name, _ in JOURNEYS}
            if not only or set(only) & (journey_names | {a.id for a in areas}):
                self.journeys(page, rec, set(only) & journey_names)
            for route in console_dynamic_routes():
                result = ScreenResult("route", route, f"{self.origin}{route}")
                result.soft.append("a dynamic route: not walked (it needs an id the walk does "
                                   "not have)")
                self.results.append(result)
            browser.close()
        return self.results


def walk(target: Target, report_dir: Path, *, only: list[str] | None = None,
         headed: bool = False, allow_writes: bool = False, capture_bodies: bool = False) -> int:
    if not target.console_url or not target.console_password:
        print("e2e: no console to walk — the stack was started with --no-web, or the live "
              "target has no BOSS_E2E_CONSOLE_URL / BOSS_E2E_CONSOLE_PASSWORD.", file=sys.stderr)
        return 2
    results = Walk(target, report_dir, allow_writes=allow_writes, headed=headed,
                   capture_bodies=capture_bodies).run(only or [])
    report = report_dir / "console.json"
    report.write_text(json.dumps([asdict(r) for r in results], indent=2))
    failed = [r for r in results if not r.ok]
    for r in failed:
        print(f"  FAIL {r.kind} {r.name}")
        for line in (r.problems + [f"console: {e}" for e in r.console_errors[:3]]
                     + [f"request: {f}" for f in r.failed_requests[:5]]
                     + [f"banner: {b}" for b in r.banners[:3]]):
            print(f"      • {line}")
    soft = sum(len(r.soft) for r in results)
    print(f"\nconsole: {len(results) - len(failed)} of {len(results)} screens and actions clean; "
          f"{soft} soft findings; report {report}")
    return 1 if failed or not results else 0
