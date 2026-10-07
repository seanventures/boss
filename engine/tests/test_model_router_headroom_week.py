"""A week of subscription headroom, simulated (docs/product/MODEL_ROUTER.md, "Realistic
examples"): usage climbs Monday to Sunday the way the CLI reports it, and each day the router
routes the same mix of work. Fast and Balanced work leaves Claude when it turns *near limit*;
Frontier work stays until the window is *exhausted*; everything comes back at Monday's reset.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from ._router_fixtures import metered, policy
from boss_engine.services.model_router.classifier import CLASSIFIER
from boss_engine.services.model_router.headroom import assess, windows_from_rate_limit_event
from boss_engine.services.model_router.router import RouteRequest, Router, Snapshot
from boss_engine.services.model_router.selection import SourceState

MONDAY = datetime(2026, 10, 5, 9, 0, tzinfo=timezone.utc)
RESET = MONDAY + timedelta(days=7)
#: Fraction of the weekly window used at 09:00 each day, as Claude Code reports it.
USED = {"Mon": 0.05, "Tue": 0.30, "Wed": 0.66, "Thu": 0.92, "Fri": 0.97, "Sat": 1.0, "Sun": 1.0}

WORK = {
    "docs": RouteRequest(kind="workflow_step", step="docs", project_id="p1",
                         text="Write the changelog for this week's changes."),
    "implement": RouteRequest(kind="build_stage", step="implement", project_id="p1",
                              spec={"title": "", "instructions": "Build a new app that tracks "
                                                                 "reading habits."}),
    "security": RouteRequest(kind="build_stage", step="security_check", project_id="p1",
                             spec={"title": "", "instructions": "Build a new app that tracks "
                                                                "reading habits."}),
}


def _day(name: str, now: datetime, observed: dict) -> dict[str, tuple]:
    pol = policy()
    head = assess("claude_code", observed, thresholds=pol.thresholds, now=now)
    snapshot = Snapshot(project_id="p1", policy=pol, sources={
        "claude_code": SourceState("claude_code", "subscription", headroom=head),
        "openrouter": metered("openrouter")})
    router = Router()
    return {k: (head.state, *(lambda d: (d.tier, d.engine))(router.route(r, snapshot)))
            for k, r in WORK.items()}


def test_a_week_of_headroom():
    CLASSIFIER.clear()
    observed: dict = {"windows": {}}
    week = {}
    for i, (name, used) in enumerate(USED.items()):
        now = MONDAY + timedelta(days=i)
        event = {"type": "rate_limit_event", "rate_limit_info": {
            "status": "rejected" if used >= 1 else
            ("allowed_warning" if used >= 0.75 else "allowed"),
            "rateLimitType": "seven_day", "utilization": used,
            "resetsAt": int(RESET.timestamp())}}
        observed["windows"].update(windows_from_rate_limit_event(event))
        week[name] = _day(name, now, observed)

    # Mon-Wed: plenty or tight — everything stays on the subscription.
    for day in ("Mon", "Tue", "Wed"):
        assert {v[2] for v in week[day].values()} == {"claude_code"}, (day, week[day])
    assert week["Wed"]["docs"][0] == "tight"
    # Thursday, 8% left: routine and balanced work move; the security review keeps Opus.
    assert week["Thu"]["docs"] == ("near_limit", "fast", "openrouter")
    assert week["Thu"]["implement"] == ("near_limit", "balanced", "openrouter")
    assert week["Thu"]["security"] == ("near_limit", "frontier", "claude_code")
    assert week["Fri"]["security"][2] == "claude_code"
    # Saturday: exhausted — now even Frontier leaves, for the next Frontier target.
    assert week["Sat"]["security"] == ("exhausted", "frontier", "openrouter")
    # The window resets: Monday again, all back.
    back = _day("Mon", RESET + timedelta(hours=1), observed)
    assert {v[2] for v in back.values()} == {"claude_code"}
    assert {v[0] for v in back.values()} == {"plenty"}
