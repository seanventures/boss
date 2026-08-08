"""Standards BOSS mandates, and where they reach the agent.

Written because the metrics rules for the first instrumented app lived in ONE task's prose. That
made them a property of whoever wrote the task rather than of the platform, so the next app
would have been built without them — and the collector BOSS deploys would scrape an endpoint
that did not exist.

The assertions are about REACH and RESTRAINT: the standard must arrive in every stage of a
service task, must not arrive for tasks it cannot apply to, and must carry the reasoning that
lets an agent generalise it.

Run directly (pytest collection wipes the local BOSS database):
    PYTHONPATH=engine .venv/bin/python tests/test_platform_standards.py
"""
from __future__ import annotations

import sys

sys.path.insert(0, "engine")

from boss_engine.services import platform_standards as ps  # noqa: E402
from boss_engine.services.governed_code_task_service import _ctx_block  # noqa: E402

_failures: list[str] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    if condition:
        print(f"  PASS  {name}")
    else:
        _failures.append(f"{name}: {detail}")
        print(f"  FAIL  {name} — {detail}")


def test_it_applies_to_services_and_not_to_everything() -> None:
    check("an HTTP app is in scope", ps.applies_to("Build a web app with a REST api"))
    check("a server task is in scope", ps.applies_to("add an endpoint to serve_stock.py"))
    check("a library change is NOT in scope",
          not ps.applies_to("refactor the date parsing helper and add unit tests"),
          "demanding /metrics of a library produces cargo-culted dead code")
    check("a docs change is NOT in scope",
          not ps.applies_to("update the README with setup instructions"))


def test_the_standard_reaches_every_stage() -> None:
    """_ctx_block is shared verbatim by standards, spec, plan, implement and review."""
    block = _ctx_block("Build a stock API", "Serve quotes over http", ["works"])
    check("the standard is present", "BOSS PLATFORM STANDARDS" in block)
    check("it says where it comes from", "not \nfrom this repository" in block
          or "not from this repository" in block.replace("\n", " "),
          "an agent must know this is not a repo convention it can override")

    quiet = _ctx_block("Refactor date helpers", "tidy the parsing utility", ["tests pass"])
    check("a non-service task gets NO standards block",
          "BOSS PLATFORM STANDARDS" not in quiet,
          "an empty section in every prompt teaches agents to skim past it")


def test_it_states_the_reasoning_not_just_the_rule() -> None:
    text = ps.OBSERVABILITY_STANDARD
    check("route templating is required", ":symbol" in text and "route" in text)
    check("it explains WHY unbounded labels are fatal",
          "series" in text and "falls over" in text,
          "a rule without a reason is applied narrowly and generalises to nothing")
    check("it forbids user data in labels", "usernames" in text or "single out a person" in text)
    check("it tells the app NOT to self-report environment",
          "self-report" in text.lower() and "environment" in text,
          "an app that reports its own environment lies after a promotion")
    check("it warns against dependency-heavy clients",
          "standard library" in text and "no package install" in text)
    check("scrapes must not pollute the app's own metrics",
          "dominate the request rate" in text)


def test_the_required_series_match_what_the_dashboards_query() -> None:
    """The generated Grafana panels and this standard must name the same metrics.

    Two lists of metric names is two chances to disagree, and the disagreement shows up as a
    dashboard of "No data" against an app that is dutifully exporting something else.
    """
    from boss_engine.services.observability_stack import render_dashboard

    dash = render_dashboard({"app": "x", "environment": "dev", "project": "p"})
    for metric in ("http_requests_total", "http_request_duration_seconds_bucket"):
        check(f"dashboard queries {metric}", metric in dash)
        base = metric.replace("_bucket", "")
        check(f"the standard requires {base}", base in ps.OBSERVABILITY_STANDARD,
              "the dashboard would show No data for an app that followed the standard")


if __name__ == "__main__":
    for fn in (test_it_applies_to_services_and_not_to_everything,
               test_the_standard_reaches_every_stage,
               test_it_states_the_reasoning_not_just_the_rule,
               test_the_required_series_match_what_the_dashboards_query):
        print(f"\n{fn.__name__}")
        fn()
    print()
    if _failures:
        print(f"{len(_failures)} FAILURE(S):")
        for f in _failures:
            print(f"  - {f}")
        sys.exit(1)
    print("all platform-standard checks passed")
