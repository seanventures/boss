"""Roll back is the API's call: only the target's latest applied deployment offers it.

The console used to offer it on every healthy or failed record. Rolling back an OLDER healthy
record from the recent list planned a commit older than the one before the live release, and a
deployment that failed at preflight — never touching the host — offered to "roll back" by
redeploying what was already running.
"""
from datetime import datetime, timedelta

import pytest

from boss_engine.services import stack_deploy

T0 = datetime(2026, 10, 1, 12, 0, 0)


def _row(i, status, commit, applied=True):
    return {"id": f"sdep_{i}", "status": status, "commit_sha": commit,
            "created_at": T0 + timedelta(minutes=i),
            "apply_started_at": (T0 + timedelta(minutes=i, seconds=5)) if applied else None}


def test_the_live_release_rolls_back_to_the_commit_before_it():
    a, b, c = _row(1, "healthy", "aaa"), _row(2, "healthy", "bbb"), _row(3, "healthy", "ccc")
    assert stack_deploy.rollback_option(c, [a, b, c]) == {
        "rollback_available": True, "rollback_commit": "bbb"}


def test_an_older_healthy_record_offers_no_rollback():
    a, b, c = _row(1, "healthy", "aaa"), _row(2, "healthy", "bbb"), _row(3, "healthy", "ccc")
    assert stack_deploy.rollback_option(b, [a, b, c])["rollback_available"] is False


def test_a_preflight_failure_never_touched_the_host_and_offers_no_rollback():
    a, b = _row(1, "healthy", "aaa"), _row(2, "healthy", "bbb")
    refused = _row(3, "failed", "ccc", applied=False)
    history = [a, b, refused]
    assert stack_deploy.rollback_option(refused, history)["rollback_available"] is False
    # ... and it does not take the live release's rollback away either.
    assert stack_deploy.rollback_option(b, history) == {
        "rollback_available": True, "rollback_commit": "aaa"}


def test_a_failed_apply_rolls_back_to_the_last_healthy_commit():
    a, b = _row(1, "healthy", "aaa"), _row(2, "failed", "bbb")
    assert stack_deploy.rollback_option(b, [a, b])["rollback_commit"] == "aaa"


def test_a_first_deployment_has_nothing_to_roll_back_to():
    only = _row(1, "healthy", "aaa")
    assert stack_deploy.rollback_option(only, [only])["rollback_available"] is False


def test_a_deployment_still_running_offers_no_rollback():
    a, b = _row(1, "healthy", "aaa"), _row(2, "running", "bbb")
    assert stack_deploy.rollback_option(b, [a, b])["rollback_available"] is False


def test_stack_facts_are_read_from_the_target_row_the_stack_route_writes():
    assert stack_deploy.is_stack_target({"target_kind": "stack"})
    # The console's old spelling, stack facts inside required_resources, is not a stack target.
    assert not stack_deploy.is_stack_target({"target_kind": "ssh", "required_resources": [
        {"kind": "deploy_kind", "name": "stack"}]})


# ── actions: the one list the console renders, and the rollback route obeys ─────────────────

def _actions(row, history, decision="pending"):
    return stack_deploy.deployment_actions(row, history, decision)


def test_approve_and_reject_only_while_the_decision_is_pending():
    waiting = {**_row(1, "awaiting_approval", "aaa", applied=False), "decision_id": "dec_1"}
    assert _actions(waiting, [waiting]) == ["approve", "reject"]
    assert _actions(waiting, [waiting], decision="approved") == []
    assert _actions({**waiting, "decision_id": None}, [waiting]) == []


def test_open_only_when_healthy_with_an_address():
    a = {**_row(1, "healthy", "aaa"), "open_url": "https://boss.example.test"}
    assert _actions(a, [a]) == ["open"]
    assert _actions({**a, "open_url": None}, [a]) == []
    running = {**_row(2, "running", "bbb"), "open_url": "https://boss.example.test"}
    assert "open" not in _actions(running, [a, running])


def test_rollback_appears_exactly_where_rollback_option_allows_it():
    a, b = _row(1, "healthy", "aaa"), _row(2, "healthy", "bbb")
    assert _actions(b, [a, b]) == ["rollback"]
    assert _actions(a, [a, b]) == []
    assert _actions({**_row(2, "rejected", "bbb", applied=False), "decision_id": "d"},
                    [a], decision="rejected") == []
