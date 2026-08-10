"""Pressing Launch convenes the panel, and the panel's PRD becomes the build — in one press.

Running the same benchmark prompt two ways produced visibly different apps: straight to the
coding pipeline gave an improvised result, while the same prompt through a PRD meeting gave a
coherent one. The panel turns "build an expense approval workflow" into explicit roles, states
and flows, and the coding agent's output is bounded by the spec it receives.

The gate in between was the problem. A human had to approve a PRD that a product, engineering
and QA panel had already argued out — an approval that was granted every time and whose only
effect was the wait. Auto-approval RESOLVES that decision rather than skipping it, so the audit
trail still records who (or what) approved, and the PRD is still on the card to read.
"""

from __future__ import annotations

import pytest

from boss_engine.config import Settings


class _Recorder:
    """Captures the call instead of running a panel."""

    def __init__(self):
        self.calls: list[dict] = []

    def __call__(self, connection, **kwargs):
        self.calls.append(kwargs)
        return {"run_id": "mrun_1", "task_id": "tsk_1", "job_id": "job_1"}


@pytest.fixture()
def endpoint(monkeypatch):
    from apps.api.app import main as api_main

    monkeypatch.setattr(api_main, "database_is_reachable", lambda _e: True)
    monkeypatch.setattr(api_main, "require_project_actor", lambda *a, **k: None)

    class _Ctx:
        def __enter__(self):
            return object()

        def __exit__(self, *exc):
            return False

    monkeypatch.setattr(api_main, "begin", lambda _e: _Ctx())
    return api_main


def test_launch_convenes_the_panel_by_default(endpoint, monkeypatch):
    recorder = _Recorder()
    monkeypatch.setattr(
        "boss_engine.services.meeting_service.convene_prd_meeting_core", recorder
    )

    result = endpoint.start_qa_benchmark_app_endpoint(
        "bugbox", {"project_id": "boss-dogfood", "actor": "executive_console"}
    )

    assert result["status"] == "meeting_convened"
    assert result["benchmark"]["id"] == "bugbox"
    assert len(recorder.calls) == 1
    # The panel receives the benchmark's real brief, not a bare title.
    assert "BugBox" in recorder.calls[0]["directive"] or len(recorder.calls[0]["directive"]) > 200


def test_the_direct_route_remains_available_for_comparison(endpoint, monkeypatch):
    """Keeping both paths is how the two can be judged against each other at all."""
    started: dict = {}

    def _start(connection, **kwargs):
        started.update(kwargs)
        return "crun_direct"

    monkeypatch.setattr("apps.api.app.services.code_task_service.start_code_task", _start)

    result = endpoint.start_qa_benchmark_app_endpoint(
        "bugbox",
        {"project_id": "boss-dogfood", "actor": "executive_console", "route": "direct"},
    )
    assert result["status"] == "queued"
    assert result["run_id"] == "crun_direct"


def test_auto_approval_is_on_by_default_and_can_be_turned_off():
    assert Settings().meeting_prd_auto_approve is True
    assert Settings(BOSS_MEETING_PRD_AUTO_APPROVE=False).meeting_prd_auto_approve is False


def test_an_approved_prd_still_starts_the_build():
    """Auto-approving the gate is pointless if the follow-on never runs."""
    assert Settings().auto_code_task_after_prd is True
