"""The worker's push-pending route is a lease protocol, not an operator action.

The worker hands its leased repo.push_pending job to POST /v1/repos/push-pending/execute
when GitHub is back (BOSSAPP-922). The path ends in execute, a high-risk action tail, so
the API held it for a person and answered the (non-human) worker 409. The waiting push
never ran. The handler is already fenced to the worker and the job lease.
"""
from boss_engine.mcp.api_surface import exclusion_reason, needs_operator_confirmation

ROUTE = "/v1/repos/push-pending/execute"


def test_the_route_is_excluded_as_a_worker_lease_protocol():
    assert exclusion_reason("POST", ROUTE) == "push_pending_worker_lease_protocol"


def test_it_would_otherwise_be_held_which_is_why_the_exclusion_matters():
    assert needs_operator_confirmation("POST", ROUTE) is True
