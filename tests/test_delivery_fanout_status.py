"""Merge fanout must preserve a failed deployment's status and explanation."""

from boss_engine.services.delivery_pipeline import fanout_entry_status


def test_failed_and_blocked_deployments_are_not_reported_as_executed() -> None:
    for outcome in ("failed", "blocked", "not_found", "rolled_back"):
        assert fanout_entry_status({"status": outcome}) == outcome
    assert fanout_entry_status(None) == "execute_failed"
    assert fanout_entry_status({"status": "unknown"}) == "execute_failed"


def test_successful_deployments_are_reported_as_executed() -> None:
    for outcome in ("succeeded", "executed", "healthy"):
        assert fanout_entry_status({"status": outcome}) == "executed"
