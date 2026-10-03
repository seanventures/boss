"""Roll back is the API's call: only the target's latest applied deployment offers it.

The console used to offer it on every healthy or failed record. Rolling back an OLDER healthy
record from the recent list planned a commit older than the one before the live release, and a
deployment that failed at preflight — never touching the host — offered to "roll back" by
redeploying what was already running.
"""
from datetime import datetime, timedelta

import pytest

from boss_engine.services import stack_targets

T0 = datetime(2026, 10, 1, 12, 0, 0)


def _row(i, status, commit, applied=True):
    return {"id": f"sdep_{i}", "status": status, "commit_sha": commit,
            "created_at": T0 + timedelta(minutes=i),
            "apply_started_at": (T0 + timedelta(minutes=i, seconds=5)) if applied else None}


def test_the_live_release_rolls_back_to_the_commit_before_it():
    a, b, c = _row(1, "healthy", "aaa"), _row(2, "healthy", "bbb"), _row(3, "healthy", "ccc")
    assert stack_targets.rollback_option(c, [a, b, c]) == {
        "rollback_available": True, "rollback_commit": "bbb"}


def test_an_older_healthy_record_offers_no_rollback():
    a, b, c = _row(1, "healthy", "aaa"), _row(2, "healthy", "bbb"), _row(3, "healthy", "ccc")
    assert stack_targets.rollback_option(b, [a, b, c])["rollback_available"] is False


def test_a_preflight_failure_never_touched_the_host_and_offers_no_rollback():
    a, b = _row(1, "healthy", "aaa"), _row(2, "healthy", "bbb")
    refused = _row(3, "failed", "ccc", applied=False)
    history = [a, b, refused]
    assert stack_targets.rollback_option(refused, history)["rollback_available"] is False
    # ... and it does not take the live release's rollback away either.
    assert stack_targets.rollback_option(b, history) == {
        "rollback_available": True, "rollback_commit": "aaa"}


def test_a_failed_apply_rolls_back_to_the_last_healthy_commit():
    a, b = _row(1, "healthy", "aaa"), _row(2, "failed", "bbb")
    assert stack_targets.rollback_option(b, [a, b])["rollback_commit"] == "aaa"


def test_a_first_deployment_has_nothing_to_roll_back_to():
    only = _row(1, "healthy", "aaa")
    assert stack_targets.rollback_option(only, [only])["rollback_available"] is False


def test_a_deployment_still_running_offers_no_rollback():
    a, b = _row(1, "healthy", "aaa"), _row(2, "running", "bbb")
    assert stack_targets.rollback_option(b, [a, b])["rollback_available"] is False


def test_stack_facts_are_read_from_required_resources():
    target = {"required_resources": [
        {"kind": "container_runtime", "name": "docker"},
        {"kind": "deploy_kind", "name": "stack"},
        {"kind": "public_url", "name": "https://boss.example.test"},
    ]}
    assert stack_targets.is_stack_target(target)
    assert stack_targets.manifest_path(target) == "boss.deploy.yaml"
    assert stack_targets.public_url(target) == "https://boss.example.test"
    assert not stack_targets.is_stack_target({"required_resources": []})


@pytest.mark.parametrize("body, message", [
    ({"deploy_kind": "container"}, "deploy_kind"),
    ({"manifest_path": "../etc/passwd"}, "manifest_path"),
    ({"manifest_path": "/abs.yaml"}, "manifest_path"),
    ({"public_url": "boss.example.test"}, "public_url"),
])
def test_a_stack_fact_that_would_fail_at_deploy_is_refused_when_written(body, message):
    with pytest.raises(ValueError, match=message):
        stack_targets.stack_resource_updates(body)
