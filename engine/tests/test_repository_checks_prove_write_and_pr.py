"""The repository check proves what a build needs: read, write a branch, open a pull request.

The old check read `GET /repos → permissions.push`. For a fine-grained token that object is the
account's role on the repository, not what the token may do — so a token with Contents
read-only showed a green "Can push", and the pull-request permission was never asked. These run
the probes against the token profiles in engine/tests/fixtures/github_probe_*.json.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from boss_engine.services import setup_checks


def _probe(github_probes, profile, calls=None):
    return setup_checks.probe_repository(f"ghp_e2e_{profile}", "e2e/app",
                                         http=github_probes.http_for_tests(calls))


def test_a_full_token_passes_all_three(github_probes):
    calls: list = []
    found = _probe(github_probes, "full", calls)
    assert found["state"] == "working"
    assert all(found["checks"][k]["ok"] for k in ("read", "write", "pull_requests"))
    assert found["default_branch"] == "main" and found["private"] is True
    # No branch is ever created: the write is a blob nothing references, and the pull
    # request names a head that does not exist (the fake asserts no git/refs call).
    assert [c for c in calls if c[0] == "POST"] == [
        ("POST", "/repos/e2e/app/git/blobs"), ("POST", "/repos/e2e/app/pulls")]


def test_the_role_does_not_decide_it(github_probes):
    """The read-only token's repository answer still says push: true (the account is admin)."""
    found = _probe(github_probes, "contents_read_only")
    assert found["state"] == "cant_write"
    assert found["checks"]["read"]["ok"] is True
    assert found["checks"]["write"]["ok"] is False


def test_no_pull_request_permission_is_its_own_failure(github_probes):
    found = _probe(github_probes, "no_pull_requests")
    assert found["state"] == "cant_open_prs"
    assert found["checks"]["write"]["ok"] is True


@pytest.mark.parametrize(("profile", "state"), [
    ("expired", "token_rejected"),
    ("no_access", "no_access"),
    ("org_approval_pending", "awaiting_org_approval"),
    ("rate_limited", "rate_limited"),
])
def test_a_failed_read_says_which_kind(github_probes, profile, state):
    found = _probe(github_probes, profile)
    assert found["state"] == state
    if state == "rate_limited":
        assert found["retry_at"]


def test_the_expiry_header_is_kept(github_probes):
    found = _probe(github_probes, "full")
    assert found["expires_at"].startswith("2099-01-02")


def test_a_token_two_days_from_expiry_reads_amber(github_probes):
    found = _probe(github_probes, "expiring")
    expires = datetime.fromisoformat(found["expires_at"])
    state = setup_checks.repository_state(
        {"state": found["state"]}, expires_at=expires, checked_at=datetime.now(timezone.utc))
    assert state == "expiring"


def test_and_past_it_reads_expired_even_before_a_recheck():
    state = setup_checks.repository_state(
        {"state": "working"}, expires_at=datetime.now(timezone.utc) - timedelta(hours=1),
        checked_at=datetime.now(timezone.utc) - timedelta(days=1))
    assert state == "expired"


def test_github_sending_the_current_time_is_not_read_as_expired():
    """go-github#3708: fine-grained tokens have been seen to carry the server time here."""
    now = datetime.now(timezone.utc)
    assert setup_checks.parse_token_expiry(now.strftime("%Y-%m-%d %H:%M:%S UTC"), now=now) is None
    assert setup_checks.parse_token_expiry("2027-01-02 10:00:00 +0500").isoformat() \
        == "2027-01-02T05:00:00+00:00"


def test_inspecting_a_token_lists_its_repositories_and_owner(github_probes):
    found = setup_checks.inspect_token("ghp_e2e_full", http=github_probes.http_for_tests())
    assert found["account_login"] == "e2e"
    assert [r["full_name"] for r in found["repos"]] == ["e2e/app", "e2e/site", "e2e-org/platform"]
    assert found["repos"][2]["owner_type"] == "Organization"
    rejected = setup_checks.inspect_token("ghp_e2e_expired", http=github_probes.http_for_tests())
    assert rejected["ok"] is False and rejected["state"] == "token_rejected"


def test_a_rate_limit_that_has_reset_is_checked_again_not_shown_as_busy():
    now = datetime.now(timezone.utc)
    past = {"state": "rate_limited", "retry_at": (now - timedelta(minutes=1)).isoformat()}
    future = {"state": "rate_limited", "retry_at": (now + timedelta(minutes=5)).isoformat()}
    assert setup_checks.repository_state(past, expires_at=None, checked_at=now) == "checking"
    assert setup_checks.repository_state(future, expires_at=None, checked_at=now) == "rate_limited"
    assert setup_checks.repository_state({"state": "rate_limited"}, expires_at=None,
                                         checked_at=now) == "checking"


def test_a_gateway_in_the_way_is_cant_reach_not_check_failed():
    for status in (0, 502, 503, 504):
        assert setup_checks.classify_model_failure(status, "") == "unreachable"
