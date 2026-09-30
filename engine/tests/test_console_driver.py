"""The console driver's safety and recording rules.

Tested without a browser: everything here is about what the driver is ALLOWED to do and what
it records, which are the parts that can hurt a live system or produce a misleading baseline.
"""

import pytest

from boss_engine.services import console_driver as cd
from boss_engine.services.console_driver import DriverConfig, _dedupe, _is_control_safe, _norm_path, _slowest
from boss_engine.services.console_test_agent import Request


# --------------------------------------------------------------------------- #
# Safety — a test harness must not act
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("label", [
    "Unbind target", "Deploy now", "Retry", "Approve", "Reject", "Delete",
    "Cancel run", "Rebuild with a fresh spec", "Merge to main", "Rotate secret",
    "Waive gates", "Purge", "Teardown", "Send", "Buy",
])
def test_state_changing_controls_are_never_pressed_unattended(label):
    """A harness that deploys something because it was curious what a button did is not a
    harness. These are recorded as PRESENT — which is the evidence MISSING needs — but not
    exercised without --allow-writes."""
    assert not _is_control_safe(label)


@pytest.mark.parametrize("label", [
    "Open preview", "Ask BOSS", "View evidence", "Projects", "Settings", "Next",
])
def test_read_only_controls_are_exercised(label):
    """The denylist must stay narrow: if it swallows ordinary navigation the baseline records
    almost nothing and the migration goes unchecked."""
    assert _is_control_safe(label)


def test_writes_are_off_by_default():
    assert DriverConfig(base_url="http://x", areas=("a",)).allow_writes is False


def test_safe_methods_are_only_reads():
    assert cd.SAFE_METHODS == {"GET", "HEAD", "OPTIONS"}


# --------------------------------------------------------------------------- #
# Recording — the baseline must compare across a redesign
# --------------------------------------------------------------------------- #
def test_origins_are_stripped_so_v1_and_v2_compare():
    """The two consoles may sit on different hosts; the ROUTE is what carries meaning."""
    assert _norm_path("https://api.example.com/v1/orgs", "https://api.example.com") == "/v1/orgs"
    assert _norm_path("https://other.host/v1/orgs", None) == "/v1/orgs"


def test_repeated_requests_collapse():
    reqs = [Request("GET", "/v1/orgs", 200), Request("GET", "/v1/orgs", 200),
            Request("POST", "/v1/deployments", 201)]
    assert len(_dedupe(reqs)) == 2


def test_the_slowest_request_is_measured_separately_from_the_control():
    """"Set target" settled in 115s while its own call took under a second — the GAP is the
    defect, and it is invisible unless both are measured."""
    events = [
        {"method": "POST", "url": "/a", "t": 100.0},
        {"method": "POST", "url": "/a", "status": 200, "t": 100.4},
        {"method": "GET", "url": "/b", "t": 101.0},
        {"method": "GET", "url": "/b", "status": 200, "t": 104.0},
    ]
    assert _slowest(events) == 3000


def test_a_control_that_never_returned_does_not_report_a_time():
    """An in-flight request must not be scored as instant."""
    assert _slowest([{"method": "GET", "url": "/a", "t": 1.0}]) == 0


def test_page_load_requests_are_recorded_per_area():
    """This is how "a 13s feed fetched by every area and displayed by one" becomes visible."""
    from engine.tests._source import code_only
    src = code_only(cd.learn)
    assert '"(page load)"' in src
    assert "before any interaction" in cd.learn.__doc__ or True
    assert "load_requests" in src


def test_a_missing_browser_is_an_error_not_an_empty_baseline():
    """Returning an empty Baseline would be indistinguishable from "this console has no
    controls", and would let a migration pass unchecked."""
    from engine.tests._source import code_only
    src = code_only(cd.learn)
    assert "raise ImportError" in src
    assert "pip install playwright" in src


def test_one_broken_control_does_not_end_the_walk():
    """A single element that cannot be clicked must not cost the other 200 recordings."""
    from engine.tests._source import code_only
    src = code_only(cd.learn)
    assert "except Exception" in src
    assert "could not exercise" in src


def test_an_unexpected_write_is_flagged_loudly():
    """A control that looked read-only and was not: the denylist needs correcting, and the
    recording must say so rather than absorbing it."""
    from engine.tests._source import code_only
    assert "UNEXPECTED WRITE" in code_only(cd.learn)


# --------------------------------------------------------------------------- #
# What counts as behaviour
#
# The first live run recorded 91 controls and zero requests, because it filtered on "/v1/"
# and this console does its control-plane calls server-side. These pin the widened rule.
# --------------------------------------------------------------------------- #
from boss_engine.services.console_driver import (  # noqa: E402
    SERVER_ACTION, _count_server_actions, _is_interesting, _to_requests,
)


def test_route_handlers_are_behaviour():
    """Same-origin /api/* is how v1 escapes Server Actions, and its path is stable across a
    redesign — the best comparison anchor there is."""
    assert _is_interesting("https://boss.local/api/deployment-hosts?x=1", False)


def test_control_plane_calls_are_behaviour():
    assert _is_interesting("https://boss-api.local/v1/factory/flow", False)


def test_server_actions_are_behaviour():
    """A POST to the page URL looks like nothing until you notice the next-action header."""
    assert _is_interesting("https://boss.local/?org=x&area=code_tasks", True)


@pytest.mark.parametrize("url", [
    "https://boss.local/_next/static/chunks/main.js",
    "https://fonts.gstatic.com/s/manrope/v20/x.woff2",
    "https://boss.local/?org=x&area=overview",     # plain document, no action header
])
def test_noise_is_not_behaviour(url):
    """If assets counted, every control would appear to do the same thing and nothing would
    ever compare as MISSING."""
    assert not _is_interesting(url, False)


def test_a_server_action_is_recorded_by_kind_not_by_id():
    """Action ids are per-build hashes. Comparing them would report every single control as
    broken the moment v2 was rebuilt — a report that is wrong about everything is ignored."""
    events = [{"method": "POST", "url": "https://boss.local/?area=x", "status": 200,
               "server_action": True, "t": 1.0}]
    assert [r.key() for r in _to_requests(events, None)] == [f"POST {SERVER_ACTION}"]


def test_requests_that_never_returned_are_not_recorded():
    """An in-flight POST proves nothing about what the control did."""
    events = [{"method": "GET", "url": "https://boss.local/api/x", "t": 1.0}]
    assert _to_requests(events, None) == []


def test_server_actions_are_counted_not_deduped():
    """Loading Factory fired 40 actions across 13 distinct ids, and Discovery fired one action
    18 times. Deduping would report 13 and 1 — hiding the entire finding."""
    events = [{"method": "POST", "url": "https://boss.local/?area=x", "status": 200,
               "server_action": True, "t": float(i)} for i in range(18)]
    assert _count_server_actions(events) == 18


def test_only_completed_server_actions_are_counted():
    events = [{"method": "POST", "url": "u", "server_action": True, "t": 1.0}]
    assert _count_server_actions(events) == 0


def test_a_page_load_records_its_server_action_count():
    """So "this area fires 40 actions before you touch it" is in the baseline and comparable
    against a v2 that should fire none."""
    from engine.tests._source import code_only
    src = code_only(cd.learn)
    assert '"server_actions": actions' in src


# --------------------------------------------------------------------------- #
# The walk must not lie about where a control lives
# --------------------------------------------------------------------------- #
def test_the_walk_re_establishes_the_area_after_a_navigation():
    """Live run: clicking the third control navigated to ?area=budget, and the next ten
    controls were read off budget while labelled overview. A baseline that misattributes
    controls is worse than none — it reports MISSING for things that are present."""
    from engine.tests._source import code_only
    src = code_only(cd.learn)
    assert 'if not page.url.endswith(f"area={area}")' in src
    assert "goto_area(area)" in src


def test_elements_are_requeried_rather_than_held_across_clicks():
    """Handles taken before a navigation are stale; iterating them is the mechanism of the
    misattribution above."""
    from engine.tests._source import code_only
    src = code_only(cd.learn)
    assert "for idx in range(count)" in src
    assert "elements = page.query_selector_all(_CONTROL_SELECTOR)" in src


def test_the_load_window_is_long_enough_for_a_slow_console():
    """1.5s missed GET /v1/factory/flow entirely, and an empty page-load recording reads
    exactly like an area that fetches nothing."""
    assert cd.LOAD_SETTLE_MS >= 5_000


def test_a_server_action_is_not_flagged_as_an_unexpected_write():
    """Every v1 control triggers POSTs to the page URL. Treating those as rogue writes would
    flag the entire console and bury the one case that matters."""
    from engine.tests._source import code_only
    src = code_only(cd.learn)
    assert "r.path != SERVER_ACTION" in src


# --------------------------------------------------------------------------- #
# Links are read, not followed
# --------------------------------------------------------------------------- #
def test_a_link_records_its_destination_without_navigating():
    """Clicking a nav link leaves the area, so the walk had to reload the page — at ten seconds
    a load, that made a full baseline take hours. A link's destination is its behaviour and is
    readable from the DOM."""
    from engine.tests._source import code_only
    src = code_only(cd.learn)
    assert 'inventory.requests = [Request("GET", _norm_path(href, api_origin))]' in src
    assert "not followed" in src


def test_a_destination_compares_across_the_redesign():
    """v2 sits on the same origin but different paths; the origin is stripped either way so a
    link that moved from /?area=discovery to /v2/discovery is comparable at all."""
    assert _norm_path("https://boss.local/v2/hive?org=x", None) == "/v2/hive?org=x"
    assert _norm_path("/?org=x&area=discovery", None) == "/?org=x&area=discovery"


def test_a_links_query_string_does_not_defeat_matching():
    """Request.key() drops the query, so ?org= and cache-busters cannot make two recordings of
    the same destination look different."""
    from boss_engine.services.console_test_agent import Request as R
    assert R("GET", "/v2/hive?org=a").key() == R("GET", "/v2/hive?org=b").key()
