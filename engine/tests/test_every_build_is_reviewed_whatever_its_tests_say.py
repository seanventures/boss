"""Every build is reviewed, whatever its tests say, and its PR carries the review (2026-10-30).

Tests decide a merge; review is the evidence that someone read the code. The panel used to be
skipped when tests still failed after a fix round, usually on failures the change did not cause,
so PRs reached the operator that no reviewer had read (KG P6a PR #25, CX PR #24). A PR with no
review conversation now says so, in words, on the PR."""
from __future__ import annotations

import inspect
from types import SimpleNamespace

import pytest

from boss_engine.services import fix_lane_triage
from boss_engine.services import governed_code_task_service as g

LEAN = {"panel_checks": ["regression"]}


@pytest.mark.parametrize("lane", ["full", fix_lane_triage.LANE_FIX])
@pytest.mark.parametrize("profile", [{}, LEAN])
@pytest.mark.parametrize("risk_tier", [0, 1, 2, 3])
def test_a_code_reviewer_always_sits(lane, profile, risk_tier):
    seats = g.review_panel_seats(lane, risk_tier, profile, ci_files=False)
    assert "review_code" in seats


def test_the_lean_profile_narrows_the_panel_but_keeps_the_code_reviewer():
    assert g.review_panel_seats("full", 0, LEAN, ci_files=False) == {"regression", "review_code"}


def test_above_tier_one_the_full_panel_sits_and_a_ci_change_adds_security():
    assert g.review_panel_seats("full", 2, LEAN, ci_files=False) == set(g._ALL_PANEL_SEATS)
    assert "security_check" in g.review_panel_seats("full", 0, LEAN, ci_files=True)


def test_test_results_do_not_choose_the_seats():
    params = inspect.signature(g.review_panel_seats).parameters
    assert not any("test" in name or "repair" in name for name in params)
    assert '"code_task.review_skipped_tests_failing"' not in inspect.getsource(g)


class _GitHub:
    def __init__(self):
        self.reviews = []

    def create_review(self, number, *, body, event, comments=None):
        self.reviews.append({"number": number, "body": body, "event": event})
        return SimpleNamespace(id=len(self.reviews), url=None)


def _no_review_record(monkeypatch, state):
    monkeypatch.setattr(g, "_review_record", lambda engine, run_id, project_id: ([], [], state))
    events, updates = [], []

    class _Conn:
        def execute(self, statement, params=None):
            updates.append(params)

    class _Tx:
        def __enter__(self):
            return _Conn()

        def __exit__(self, *a):
            return False

    monkeypatch.setattr(g, "_transaction", lambda engine: _Tx())
    monkeypatch.setattr(g, "emit_event", lambda c, p, kind, *a, **k: events.append(kind))
    return events, updates


def test_a_pr_with_no_review_conversation_says_so_on_the_pr(monkeypatch):
    events, updates = _no_review_record(monkeypatch, state={})
    github = _GitHub()
    out = g._post_review_comments_to_pr(
        SimpleNamespace(github=github), {"pull_request_number": 25},
        {"adversarial_verdict": "not_run", "security_verdict": "not_run"},
        None, "boss-platform", "crun_x")

    assert out["posted"] is False
    assert "code_task.review_comments_absent" in events
    assert len(github.reviews) == 1
    body = github.reviews[0]["body"]
    assert "NOT DONE" in body and "Do not treat it as reviewed" in body
    assert "crun_x" in body and "not_run" in body
    assert any(p and "absent_notice" in str(p.get("s")) for p in updates)


def test_the_no_review_notice_is_posted_once(monkeypatch):
    _no_review_record(monkeypatch, state={"absent_notice": True})
    github = _GitHub()
    g._post_review_comments_to_pr(SimpleNamespace(github=github), {"pull_request_number": 25},
                                  {}, None, "boss-platform", "crun_x")
    assert github.reviews == []
