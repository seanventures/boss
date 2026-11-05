"""One incident fix, two cards: the release is what ships, the pull request merges after recovery.

The operator was shown a release gate and an "Approve and merge" card and could not tell
which to press. On the demo the deploy shipped the pull request's head while it was still
unmerged. These cards stay two cards — the release is not a merge — and both say the order.
An ordinary pull request still says "Approve and merge".
"""
from __future__ import annotations

import json
from typing import Any, Optional

from boss_engine.services import decision_actions as da

ORDER = ("Approve the release first — that is what ships. "
         "Merge the pull request only after recovery is verified.")


def _by_action(card: dict) -> dict[str, dict]:
    return {a["action"]: a for a in da.allowed_actions(card)}


def test_an_incident_release_and_its_pull_request_say_which_comes_first():
    release = {"status": "pending", "recommendation": {
        "workflow_run_id": "wfr_inc", "capability": "release_approval",
        "incident_order": "release_then_merge",
    }}
    pull = {"status": "pending", "recommendation": {
        "action": "review_pull_request", "pull_request_url": "https://github.com/acme/app/pull/9",
        "incident_order": "release_then_merge",
    }}
    rel = _by_action(release)
    pr = _by_action(pull)
    assert rel["approve"]["label"] == "Approve the release"
    assert ORDER in rel["approve"]["effect"]
    assert rel["reject"]["label"] == "Don't ship"
    assert pr["approve"]["label"] == "Merge after recovery"
    assert pr["approve"]["label"] != "Approve and merge"
    assert "Approve the release first" in pr["approve"]["effect"]
    assert "after recovery" in pr["approve"]["effect"]
    assert "delegate" in pr and "delegate" in rel


def test_an_unreviewed_incident_release_keeps_its_warning_and_says_the_order():
    card = {"status": "pending", "recommendation": {
        "workflow_run_id": "wfr_inc", "capability": "release_approval",
        "incident_order": "release_then_merge",
        "release_commits": {"needs_reason": True, "content_hash": "sha256:card", "commits": []},
    }}
    approve = _by_action(card)["approve"]
    assert approve["label"] == "Ship with unreviewed commits"
    assert approve["needs"] == "note"
    assert ORDER in approve["effect"]


def test_an_ordinary_pull_request_still_says_approve_and_merge():
    from boss_engine.services.decision_actions import allowed_actions

    card = {"status": "pending", "recommendation": {
        "action": "review_pull_request", "pull_request_url": "https://x/pull/7"}}
    approve = next(a for a in allowed_actions(card) if a["action"] == "approve")
    assert approve["label"] == "Approve and merge"
    assert "recovery" not in approve["effect"]


class _Rows:
    def __init__(self, row: Optional[dict] = None):
        self._row = row

    def mappings(self):
        return self

    def first(self):
        return self._row


class _Conn:
    def __init__(self, row: Optional[dict] = None):
        self.row = row
        self.calls: list[tuple[str, dict]] = []

    def execute(self, statement: Any, params: Optional[dict] = None):
        sql = str(getattr(statement, "text", statement))
        self.calls.append((sql, dict(params or {})))
        if "code_task_runs" in sql.lower() and "select" in sql.lower():
            return _Rows(self.row)
        return _Rows(None)


def test_linking_stamps_the_release_and_the_open_pull_request_and_does_not_merge():
    from boss_engine.services.incident_response import link_release_and_merge

    conn = _Conn({"id": "dec_pr", "status": "pending", "recommendation": {
        "action": "review_pull_request", "pull_request_url": "https://github.com/acme/app/pull/9",
    }})
    rec = link_release_and_merge(
        conn, "proj", {"capability": "release_approval", "step_label": "Release"}, "crun_fix")
    assert rec["incident_order"] == "release_then_merge"
    assert rec["why"] == ORDER
    assert rec["capability"] == "release_approval"
    writes = [(sql, params) for sql, params in conn.calls if sql.strip().lower().startswith("update")]
    assert len(writes) == 1
    sql, params = writes[0]
    assert "decisions" in sql.lower()
    assert "question" not in sql.lower()
    assert "merge" not in sql.lower()
    patch = json.loads(params["patch"])
    assert patch["incident_order"] == "release_then_merge"
    assert patch["why"] == ORDER
    assert params["d"] == "dec_pr"
    assert params["p"] == "proj"


def test_a_release_with_no_pull_request_yet_still_says_the_order():
    from boss_engine.services.incident_response import link_release_and_merge

    conn = _Conn(None)
    rec = link_release_and_merge(conn, "proj", {"capability": "release_approval"}, "crun_fix")
    assert rec["why"] == ORDER
    assert not any(sql.strip().lower().startswith("update") for sql, _ in conn.calls)
