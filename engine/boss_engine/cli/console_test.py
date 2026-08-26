"""Run the console test agent from a terminal.

    # 1. Learn what today's console does (safe: presses nothing that changes state)
    python -m boss_engine.cli.console_test learn \\
        --url https://console.example/?org=ORG \\
        --out baselines/v1.json

    # 2. After migrating, check v2 still does it
    python -m boss_engine.cli.console_test verify \\
        --baseline baselines/v1.json \\
        --url https://console.example/v2?org=ORG

    # 3. Ask whether the baseline is even capable of catching known bugs
    python -m boss_engine.cli.console_test coverage --baseline baselines/v1.json

Exit codes are for CI: 0 means nothing MISSING or BROKEN, 1 means something was lost. MOVED
never fails a run — v2 relocates almost everything by design, and a report that treats moving
as breaking is noise nobody reads.
"""

from __future__ import annotations

import argparse
import json
import pathlib
import sys

from ..services.console_driver import DriverConfig, learn, observe_screen, probe_links
from ..services.console_screen_check import check, summarise as summarise_screens
from ..services.console_test_agent import Baseline, compare, defect_coverage, summarise

#: The v1 areas worth recording first. Deliberately the five the operator called core, plus
#: the two where this week's defects actually lived.
DEFAULT_AREAS = (
    "overview", "discovery", "meetings", "code_tasks",
    "decisions", "delivery", "monitoring",
)


def _learn(args: argparse.Namespace) -> int:
    def progress(note: str) -> None:
        # stderr, unbuffered: a walk takes minutes, and a tool that says nothing until it
        # finishes is indistinguishable from one that has hung.
        print(note, file=sys.stderr, flush=True)

    baseline = learn(
        DriverConfig(
            base_url=args.url,
            areas=tuple(args.areas.split(",")) if args.areas else DEFAULT_AREAS,
            allow_writes=args.allow_writes,
            headless=not args.headed,
            on_progress=None if args.quiet else progress,
        ),
        api_origin=args.api_origin,
    )
    out = pathlib.Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(baseline.to_json(), encoding="utf-8")

    pressed = [c for c in baseline.controls if c.requests]
    skipped = [c for c in baseline.controls if "not pressed" in c.note]
    surprises = [c for c in baseline.controls if "UNEXPECTED WRITE" in c.note]

    print(f"recorded {len(baseline.controls)} controls across "
          f"{len({c.area for c in baseline.controls})} areas -> {out}")
    print(f"  exercised      : {len(pressed)}")
    print(f"  left untouched : {len(skipped)}  (would change state)")
    if surprises:
        # Loud, because it means the denylist is wrong and a live system was written to.
        print(f"  UNEXPECTED WRITES: {len(surprises)}")
        for c in surprises[:5]:
            print(f"    {c.area}/{c.label}: {c.note}")

    slow = sorted(
        (c for c in baseline.controls if c.state_after.get("elapsed_ms", 0) > 3000),
        key=lambda c: -c.state_after["elapsed_ms"],
    )[:5]
    if slow:
        print("  slowest controls:")
        for c in slow:
            waited = c.state_after["elapsed_ms"]
            req = c.state_after.get("slowest_request_ms", 0)
            # The gap is the interesting number: waiting far longer than any request took
            # means the UI made you wait for something other than the work.
            gap = " <- waiting on something other than its own request" if waited > req * 3 else ""
            print(f"    {waited:6}ms  (slowest request {req}ms)  {c.area}/{c.label}{gap}")
    return 0


def _verify(args: argparse.Namespace) -> int:
    baseline = Baseline.from_json(pathlib.Path(args.baseline).read_text(encoding="utf-8"))
    observed = learn(
        DriverConfig(
            base_url=args.url,
            areas=tuple(args.areas.split(",")) if args.areas else DEFAULT_AREAS,
            allow_writes=args.allow_writes,
            headless=not args.headed,
            on_progress=(
                None if args.quiet
                else lambda note: print(note, file=sys.stderr, flush=True)
            ),
        ),
        api_origin=args.api_origin,
    )
    area_map = json.loads(pathlib.Path(args.area_map).read_text()) if args.area_map else None
    findings = compare(baseline, observed, area_map)
    result = summarise(findings)

    counts = result["counts"]
    print(f"MISSING {counts.get('MISSING', 0)}  "
          f"BROKEN {counts.get('BROKEN', 0)}  "
          f"MOVED {counts.get('MOVED', 0)}  "
          f"OK {counts.get('OK', 0)}")
    for f in findings:
        if f.verdict == "MISSING":
            print(f"\nMISSING  {f.label}  (was in {f.v1_area})")
            print(f"         nothing in v2 issues: {', '.join(f.expected)}")
        elif f.verdict == "BROKEN":
            print(f"\nBROKEN   {f.label}  ({f.v1_area} -> {f.v2_area})")
            print(f"         expected: {', '.join(f.expected)}")
            print(f"         actual  : {', '.join(f.actual) or '(nothing)'}")

    if args.out:
        pathlib.Path(args.out).write_text(json.dumps(result, indent=1), encoding="utf-8")
    return 0 if result["passed"] else 1


def _coverage(args: argparse.Namespace) -> int:
    """Is this baseline even capable of catching the bugs we already know about?"""
    baseline = Baseline.from_json(pathlib.Path(args.baseline).read_text(encoding="utf-8"))
    rows = defect_coverage(baseline)
    for r in rows:
        mark = "yes" if r["covered"] else "NO "
        print(f"  [{mark}] {r['id']:32} {r['evidence']}")
    uncovered = [r for r in rows if not r["covered"]]
    if uncovered:
        print(f"\n{len(uncovered)} known defect(s) this baseline could not expose.")
        print("A baseline that cannot reproduce a bug we found by hand will not catch its")
        print("unknown siblings — widen the areas or enable --allow-writes on a scratch project.")
    return 1 if uncovered else 0


#: The v2 screens and the root element each must render. A screen missing from here is one
#: nobody checks, so this list is the definition of "all the screens" for the checker.
V2_SCREENS = {
    "/v2": "v2-projects",
    "/v2/hive": "v2-hive",
    "/v2/factory": "v2-workforce",
    "/v2/org": "v2-org",
    "/v2/panels": "v2-decisions",
    "/v2/timeline": "v2-receipts",
    "/v2/boss": "v2-workflows",
    "/v2/infra": "v2-infra",
    "/v2/settings": "v2-settings",
}


def _screens(args: argparse.Namespace) -> int:
    """Check every screen actually works: rendered, styled, no crash, no dead links.

    This is the half the request recorder could not do. It found nothing about styling because
    a page fetches the same things whether or not a stylesheet loaded, so an unstyled screen
    was invisible to it and visible to the operator — the wrong way round.
    """
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as exc:  # pragma: no cover - environment dependent
        raise ImportError(
            "the screen checker needs Playwright: pip install playwright==1.60.0 && "
            "python -m playwright install chromium"
        ) from exc

    origin = args.url.rstrip("/")
    query = f"?org={args.org}" if args.org else ""
    paths = args.paths.split(",") if args.paths else list(V2_SCREENS)

    results = {}
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=not args.headed)
        page = browser.new_page()
        for i, path in enumerate(paths, start=1):
            print(f"[{i}/{len(paths)}] {path}", file=sys.stderr, flush=True)
            observation = observe_screen(page, f"{path}{query}", origin)
            probe_links(page, observation, origin)
            findings = check(observation, expect_testid=V2_SCREENS.get(path))
            results[path] = findings
            for f in findings:
                if f.severity != "OK":
                    print(f"    {f.severity}: {f.rule} — {f.detail}", file=sys.stderr, flush=True)
        browser.close()

    result = summarise_screens(results)
    print(f"\n{result['screens']} screens checked")
    print(f"  broken   : {len(result['broken'])}")
    print(f"  suspect  : {len(result['suspect'])}")
    print(f"  unrefuted: {len(result['unrefuted'])}")
    for path, details in result["broken"].items():
        print(f"\nBROKEN {path}")
        for d in details:
            print(f"       {d}")
    for path, details in result["suspect"].items():
        print(f"\nsuspect {path}")
        for d in details:
            print(f"        {d}")

    if args.out:
        pathlib.Path(args.out).write_text(json.dumps(result, indent=1), encoding="utf-8")
    return 0 if result["passed"] else 1


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="console_test", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)

    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--url", required=True, help="console URL, including ?org=")
    common.add_argument("--areas", help="comma-separated area ids (default: the core seven)")
    common.add_argument("--api-origin", help="control-plane origin, stripped from recorded paths")
    common.add_argument("--allow-writes", action="store_true",
                        help="press controls that change state — a scratch project only")
    common.add_argument("--headed", action="store_true", help="watch it run")
    common.add_argument("--quiet", action="store_true",
                        help="suppress per-area progress on stderr")

    lp = sub.add_parser("learn", parents=[common], help="record what a console does")
    lp.add_argument("--out", default="baselines/v1.json")
    lp.set_defaults(fn=_learn)

    vp = sub.add_parser("verify", parents=[common], help="check another console still does it")
    vp.add_argument("--baseline", required=True)
    vp.add_argument("--area-map", help="JSON {v1_area: expected_v2_area} for MOVED reporting")
    vp.add_argument("--out", help="write the JSON report here")
    vp.set_defaults(fn=_verify)

    sp = sub.add_parser("screens", help="check every screen renders, is styled, and works")
    sp.add_argument("--url", required=True, help="console origin, e.g. https://boss.example")
    sp.add_argument("--org", help="org id to pass as ?org=")
    sp.add_argument("--paths", help="comma-separated paths (default: every v2 screen)")
    sp.add_argument("--headed", action="store_true", help="watch it run")
    sp.add_argument("--out", help="write the JSON report here")
    sp.set_defaults(fn=_screens)

    cp = sub.add_parser("coverage", help="can this baseline catch known defects?")
    cp.add_argument("--baseline", required=True)
    cp.set_defaults(fn=_coverage)

    args = p.parse_args(argv)
    return int(args.fn(args))


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
