"""One failed deployment must not disable deployment for that app permanently.

A deploy blocked on a missing credential. Every attempt after it — by anyone — was refused:

    "Deployment surface is owned by another deployer."

`acquire_surface` fences a surface so two deployers cannot write to it at once, and it honours
`expires_at`. The deployment path passed none, so the claim never lapsed. The run that held it
had already failed and would never relinquish anything, and the only remedies were a different
actor name or direct database access.

The hazard being fenced is CONCURRENT writes, which is bounded work, so the claim is bounded
too. A live deploy re-acquires under its own dedupe key; a dead one lets go by itself.
"""
from __future__ import annotations

import inspect

from boss_engine.services import resource_plane


class TestTheClaimIsALease:
    def test_the_deployment_surface_is_acquired_with_an_expiry(self):
        source = inspect.getsource(resource_plane)
        acquire = source[source.index("surface_key = f\"deployment_target:"):]
        acquire = acquire[:acquire.index("if not ownership_result")]
        assert "expires_at=" in acquire, (
            "a deployment surface claimed with no expiry outlives the deploy that made it, "
            "and one failure then blocks the app for good"
        )

    def test_the_lease_outlasts_a_real_deploy(self):
        """Too short is its own bug: a live deploy must not lose its own surface mid-flight."""
        assert resource_plane._SURFACE_LEASE_SECONDS >= 900

    def test_the_lease_is_not_effectively_forever(self):
        """The whole point is that a dead deployer lets go without a human intervening."""
        assert resource_plane._SURFACE_LEASE_SECONDS <= 6 * 3600


class TestAnAbandonedClaimCanBeReclaimed:
    """The lease alone would only protect targets that were not ALREADY wedged.

    Claims written before leases existed have expires_at NULL, which acquire_surface reads as
    active forever — and there is no relinquish path anywhere in the service or the API. So a
    target wedged yesterday would stay wedged after the fix, which is not a fix.
    """

    def test_the_deployment_claim_declares_when_a_claim_is_stale(self):
        source = inspect.getsource(resource_plane)
        acquire = source[source.index("surface_key = f\"deployment_target:"):]
        acquire = acquire[:acquire.index("if not ownership_result")]
        assert "stale_after=" in acquire

    def test_acquire_surface_understands_staleness(self):
        from boss_engine.services import assurance

        sig = inspect.signature(assurance.AssuranceService.acquire_surface)
        assert "stale_after" in sig.parameters

    def test_a_caller_that_passes_nothing_keeps_the_old_behaviour(self):
        """Long-lived surfaces must not start expiring because deployments needed a lease."""
        from boss_engine.services import assurance

        sig = inspect.signature(assurance.AssuranceService.acquire_surface)
        assert sig.parameters["stale_after"].default is None
