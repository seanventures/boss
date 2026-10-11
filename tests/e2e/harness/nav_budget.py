"""The console NAVIGATION budget: what a person waits for, in a real browser. Blocks a release.

    scripts/e2e.sh up                     # a stack
    scripts/e2e.sh budget --seed          # seed volume, then walk and check the budgets below
    scripts/e2e.sh all                    # runs this after the console walk; a breach fails it

Why a browser and not the render timings in perf.py: the operator's "Loading Factory…" for 20 s
(2026-10-10, reported ~10 times) was invisible to every server-side measure — the server answered
the area in under a second, and the browser never committed it (Next's router left the refresh
pending until the 20 s background poll). Only a click in a browser, timed until the area is on
screen, sees that. Measured before the fix on a seeded stack: first clicks on the Factory after a
load took 20.1-20.2 s in 4 of 6 tries.

What it checks, signed in as the Owner with a STALE second factor (older than the step-up window,
so any write a page sends by itself would be refused step_up_required — the state the operator was
in when "Confirm it's you" popped up on a plain refresh):

  * NAVIGATION: for each main area, a fresh browser loads the Command Center, waits as a person
    does (1-3 s — the window where the router hang lived), and clicks the area in the rail. The
    time until that area's content is on screen (its section shown, no "Loading …") is one
    sample. p95 over every sample of every area must be <= NAV_P95_MS.
  * CALLS PER PAGE LOAD: each area loaded cold (a document load of `?area=X`, then 2 s for the
    panels' first reads). API requests made for the signed-in person in that window, from the API's
    own request log (server render and browser together), must be <= CALLS_PER_LOAD.
  * NO POLLING STORM: every area opened once, then the Command Center left on screen for
    IDLE_SECONDS; browser requests in that time must be <= IDLE_REQUESTS (areas off screen poll
    nothing; app/area-frame.tsx).
  * NO STEP-UP ON VIEW: at no point in any of the above may the step-up sheet appear.

The tab must be visible (a hidden automation tab never finishes streaming the console — a
harness artefact, not the product); the walk checks `document.visibilityState` and fails loudly
if it is not "visible".
"""
from __future__ import annotations

import json
import random
import statistics
import sys
import time
from pathlib import Path
from typing import Any, Optional

from . import REPO_ROOT
from .stack import StackState

ORG = "org_boss"

#: Budgets on the seeded local stack. Measured after the fix (2026-10-11, this laptop, headless
#: Chrome, 3 clicks per area): navigation p50 361 ms, p95 387 ms (before: first clicks 0.6-20.2 s,
#: most at 20.1 s); API calls per page load max 28 (Workflows; Command Center 19); idle 18 browser
#: requests in 30 s (before, every area mounted: 81). The headroom is for a slower machine, not
#: for a regression: a panel that starts polling from off screen, or a render that starts asking
#: for the same feed twice, fails here.
NAV_P95_MS = 1000.0
CALLS_PER_LOAD = 35
IDLE_SECONDS = 30
IDLE_REQUESTS = 25

#: Areas with a rail entry that render inside the console page (route areas such as Identity
#: open their own page and are measured by perf.py).
SKIP_AREAS = {"enterprise"}

STEP_UP = "[data-testid='step-up-sheet']"
LOADING = "[data-testid='area-loading']"


def _shown_js(area: str) -> str:
    return (
        "(() => { if (document.querySelector(\"" + LOADING.replace('"', '\\"') + "\")) return false;"
        f" const main = document.querySelector('main.bx-content');"
        f" if (!main || main.dataset.active !== '{area}') return false;"
        f" return [...main.querySelectorAll('section')].some(s => (s.dataset.area === '{area}'"
        f" || s.querySelector('[data-area=\"{area}\"]')) && s.getClientRects().length > 0); }})()"
    )


def _pct(values: list[float], p: float) -> float:
    ordered = sorted(values)
    if not ordered:
        return 0.0
    k = min(len(ordered) - 1, max(0, int(round(p / 100 * (len(ordered) - 1)))))
    return ordered[k]


def _log_size(log: Path) -> int:
    return log.stat().st_size if log.exists() else 0


def _log_since(log: Path, start: int) -> list[dict[str, Any]]:
    if not log.exists():
        return []
    with open(log, "rb") as fh:
        fh.seek(start)
        rows = []
        for line in fh.read().decode("utf-8", "replace").splitlines():
            try:
                rows.append(json.loads(line))
            except ValueError:
                continue
        return rows


def age_second_factor(state: StackState, page: Any, origin: str, minutes: int = 11) -> None:
    """Make this session's second factor older than the step-up window (10 min)."""
    import psycopg

    me = page.evaluate("async () => (await fetch('/v1/identity/me', {cache: 'no-store'})).json()")
    principal, session = me.get("principal_id"), me.get("session_id")
    if not principal or not session:
        raise RuntimeError(f"/v1/identity/me did not name the session: {sorted(me)}")
    url = state.database_url.replace("postgresql+psycopg", "postgresql")
    with psycopg.connect(url, autocommit=True) as conn:
        conn.execute("UPDATE identity_sessions SET mfa_at = now() - make_interval(mins => %s) "
                     "WHERE id = %s AND principal_id = %s", (minutes, session, principal))


class StepUpSeen(AssertionError):
    pass


def _no_step_up(page: Any, where: str) -> None:
    if page.locator(STEP_UP).count() or "Confirm it’s you" in (page.evaluate(
            "document.body ? document.body.innerText : ''") or "").replace("'", "’"):
        raise StepUpSeen(f"the step-up sheet opened with nobody pressing anything: {where}")


def _wait(page: Any, js: str, limit_s: float, where: str) -> Optional[float]:
    t0 = time.perf_counter()
    while time.perf_counter() - t0 < limit_s:
        if page.evaluate(js):
            return (time.perf_counter() - t0) * 1000
        _no_step_up(page, where)
        page.wait_for_timeout(25)
    return None


def walk(state: StackState, *, rounds: int = 3, headed: bool = False, seed: int = 7,
         areas: Optional[list[str]] = None) -> dict[str, Any]:
    from playwright.sync_api import sync_playwright

    from .identity import load_accounts, sign_in

    rng = random.Random(seed)
    origin = f"http://127.0.0.1:{state.web_port}"
    log = Path(state.run_dir) / "api-requests.jsonl"
    owner = load_accounts(Path(state.accounts_file))["owner"]
    report: dict[str, Any] = {"nav": {}, "calls": {}, "idle": {}, "step_up": None}
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=not headed)
        signer = browser.new_context(viewport={"width": 1600, "height": 1000})
        page = signer.new_page()
        sign_in(page, origin, owner, next_path=f"/?org={ORG}")
        age_second_factor(state, page, origin)
        storage = signer.storage_state()
        signer.close()

        def fresh(path: str) -> tuple[Any, Any, list[Any]]:
            ctx = browser.new_context(storage_state=storage, viewport={"width": 1600, "height": 1000})
            pg = ctx.new_page()
            seen: list[Any] = []
            pg.on("request", lambda r: seen.append(r))
            pg.goto(origin + path, wait_until="domcontentloaded")
            vis = pg.evaluate("document.visibilityState")
            if vis != "visible":
                raise RuntimeError(f"the automation tab is {vis!r}: a hidden tab never finishes "
                                   "streaming the console, so nothing it measures is real")
            return ctx, pg, seen

        try:
            ctx, pg, _ = fresh(f"/?org={ORG}&area=overview")
            if _wait(pg, _shown_js("overview"), 30, "Command Center, first load") is None:
                raise RuntimeError("the Command Center never showed")
            rail = [a for a in pg.evaluate(
                "[...document.querySelectorAll('[data-area-nav]')].map(e => e.dataset.areaNav)")
                if a not in SKIP_AREAS and a != "overview"]
            ctx.close()
            if areas:
                rail = [a for a in rail if a in areas]
            report["areas"] = rail

            # 1. NAVIGATION: a rail click, timed until the area is on screen.
            samples: list[float] = []
            for area in rail:
                got = []
                for _ in range(rounds):
                    ctx, pg, _ = fresh(f"/?org={ORG}&area=overview")
                    _wait(pg, _shown_js("overview"), 30, "Command Center")
                    pg.wait_for_timeout(rng.randint(1000, 3000))
                    pg.click(f"[data-area-nav='{area}']")
                    ms = _wait(pg, _shown_js(area), 30, f"rail click on {area}")
                    got.append(round(ms if ms is not None else 30_000.0, 1))
                    pg.wait_for_timeout(500)
                    _no_step_up(pg, f"after opening {area}")
                    ctx.close()
                report["nav"][area] = got
                samples.extend(got)
            report["nav_p50_ms"] = round(statistics.median(samples), 1) if samples else 0
            report["nav_p95_ms"] = round(_pct(samples, 95), 1)
            report["nav_max_ms"] = round(max(samples), 1) if samples else 0

            # 2. CALLS PER PAGE LOAD: the area loaded cold, then 2 s for its panels' first reads.
            for area in ["overview", *rail]:
                start = _log_size(log)
                ctx, pg, seen = fresh(f"/?org={ORG}&area={area}")
                _wait(pg, _shown_js(area), 30, f"cold load of {area}")
                pg.wait_for_timeout(2000)
                _no_step_up(pg, f"cold load of {area}")
                rows = [r for r in _log_since(log, start) if r.get("cred") == "session"]
                # Reported, not failed: a page may re-check stale rows by itself (Repo and models),
                # and must then never ask for a second factor — the step-up check above is the
                # gate. A server action's read is a POST to …/load and changes nothing.
                writes = [f"{r['method']} {r['path']}" for r in rows
                          if r.get("method") != "GET" and not str(r.get("path", "")).endswith("/load")]
                browser_v1 = [r.url for r in seen if "/v1/" in r.url or "/api/" in r.url]
                report["calls"][area] = {"api": len(rows), "browser": len(browser_v1),
                                         "writes": writes}
                ctx.close()
            report["calls_max"] = max((v["api"] for v in report["calls"].values()), default=0)

            # 3. NO POLLING STORM: every area opened once, then the Command Center on screen.
            ctx, pg, seen = fresh(f"/?org={ORG}&area=overview")
            _wait(pg, _shown_js("overview"), 30, "Command Center")
            for area in rail:
                pg.click(f"[data-area-nav='{area}']")
                _wait(pg, _shown_js(area), 30, f"opening {area}")
                pg.wait_for_timeout(300)
            pg.click("[data-area-nav='overview']")
            _wait(pg, _shown_js("overview"), 30, "back to the Command Center")
            pg.wait_for_timeout(3000)
            n0 = len(seen)
            deadline = time.time() + IDLE_SECONDS
            while time.time() < deadline:
                _no_step_up(pg, "idle on the Command Center")
                pg.wait_for_timeout(1000)
            idle = [r for r in seen[n0:] if r.resource_type in ("fetch", "xhr")]
            by: dict[str, int] = {}
            for r in idle:
                key = r.url.split("?", 1)[0].replace(origin, "")
                by[key] = by.get(key, 0) + 1
            report["idle"] = {"seconds": IDLE_SECONDS, "requests": len(idle),
                              "by_path": dict(sorted(by.items(), key=lambda kv: -kv[1]))}
            ctx.close()
        except StepUpSeen as seen_it:
            report["step_up"] = str(seen_it)
        finally:
            browser.close()
    return report


def check(report: dict[str, Any]) -> list[str]:
    failures = []
    if report.get("step_up"):
        failures.append(str(report["step_up"]))
        return failures
    if report.get("nav_p95_ms", 0) > NAV_P95_MS:
        slow = {a: v for a, v in report["nav"].items() if max(v) > NAV_P95_MS}
        failures.append(f"navigation p95 {report['nav_p95_ms']} ms > {NAV_P95_MS} ms (slow: {slow})")
    for area, got in report.get("calls", {}).items():
        if got["api"] > CALLS_PER_LOAD:
            failures.append(f"{area}: {got['api']} API calls for one page load > {CALLS_PER_LOAD}")
    idle = report.get("idle", {})
    if idle.get("requests", 0) > IDLE_REQUESTS:
        failures.append(f"idle: {idle['requests']} requests in {idle['seconds']} s > {IDLE_REQUESTS} "
                        f"({list(idle.get('by_path', {}).items())[:6]})")
    return failures


def main(argv: list[str]) -> int:
    import argparse

    parser = argparse.ArgumentParser(prog="budget", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--seed", action="store_true", help="write perf_seed.py's volume first")
    parser.add_argument("--rounds", type=int, default=3, help="rail clicks per area")
    parser.add_argument("--headed", action="store_true")
    parser.add_argument("--only", action="append", default=[], help="limit to these areas")
    parser.add_argument("--out", default="")
    parser.add_argument("--no-budget", action="store_true", help="report only")
    args = parser.parse_args(argv)
    state = StackState.load()
    if args.seed:
        from .perf_seed import seed

        print(json.dumps({"seeded": seed(state.database_url)}))
    log = Path(state.run_dir) / "api-requests.jsonl"
    if not log.exists():
        # The API's per-request log is what counts calls per load (perf.restart_api sets it).
        from .perf import restart_api

        restart_api(state, root=REPO_ROOT, log=log)
    report = walk(state, rounds=args.rounds, headed=args.headed, areas=args.only or None)
    text = json.dumps(report, indent=2)
    print(text)
    if args.out:
        Path(args.out).write_text(text)
    if args.no_budget:
        return 0
    failures = check(report)
    for f in failures:
        print(f"BUDGET: {f}", file=sys.stderr)
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
