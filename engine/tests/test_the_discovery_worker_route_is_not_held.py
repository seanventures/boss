"""The worker's discovery route is a lease protocol, not an operator action.

The worker hands its leased discovery.run job to POST /v1/discoveries/{run_id}/execute. The route
ends in `execute`, a high-risk action tail, so the API held it for human confirmation and
answered the (non-human) worker 409 — every Community discovery sat queued until its job
deadlettered. Found on boss-1 (2026-10-01). The API test of the route had called it as a human,
which is exactly the caller the hold lets through.
"""
from boss_engine.mcp.api_surface import exclusion_reason, needs_operator_confirmation

ROUTE = "/v1/discoveries/{run_id}/execute"


def test_the_route_is_excluded_as_a_worker_lease_protocol():
    assert exclusion_reason("POST", ROUTE) == "discovery_worker_lease_protocol"


def test_it_would_otherwise_be_held_which_is_why_the_exclusion_matters():
    assert needs_operator_confirmation("POST", ROUTE) is True


def test_starting_and_deepening_stay_ordinary_policy_gated_routes():
    for path in ("/v1/discoveries", "/v1/discoveries/{run_id}/deepen"):
        assert exclusion_reason("POST", path) is None
