"""P2/P4 — Plane status sync-back: event→state mapping, comments, and the loop-guarded orchestration."""
from apps.api.app.services import plane_sync as ps


class _FakeClient:
    def __init__(self, enabled=True):
        self._enabled = enabled
        self.states = []
        self.comments = []

    def enabled(self):
        return self._enabled

    def set_state(self, project_id, issue_id, state):
        self.states.append((issue_id, state))
        return True

    def add_comment(self, project_id, issue_id, html):
        self.comments.append((issue_id, html))
        return True


def _plane_run(**over):
    run = {"id": "crun_1", "pull_request_url": "https://github.com/x/y/pull/9",
           "finalize_ctx": {"source": "plane", "plane_issue_id": "iss-1", "plane_project_id": "proj-1"}}
    run.update(over)
    return run


# --- mapping -----------------------------------------------------------------
def test_event_state_map():
    assert ps._EVENT_STATE["code_task.started"] == "In Progress"
    assert ps._EVENT_STATE["software_factory.pull_request_merged"] == "Done"
    assert ps._EVENT_STATE["code_task.failed"] is None          # comment only


def test_comment_for_each_event():
    run = _plane_run()
    assert "picked this up" in ps.comment_for("code_task.started", run)
    assert "pull request" in ps.comment_for("code_task.pr_opened", run).lower()
    assert "/pull/9" in ps.comment_for("code_task.pr_opened", run)
    assert "merged" in ps.comment_for("software_factory.pull_request_merged", run).lower()
    fail = ps.comment_for("code_task.failed", _plane_run(error="boom"))
    assert "failed" in fail.lower() and "boom" in fail


# --- sync_run ----------------------------------------------------------------
def test_sync_run_moves_state_and_comments():
    c = _FakeClient()
    out = ps.sync_run(c, _plane_run(), "code_task.pr_opened")
    assert out["synced"] and out["state"] == "In Progress"
    assert c.states == [("iss-1", "In Progress")] and c.comments and "pull/9" in c.comments[0][1]


def test_sync_run_merged_moves_to_done():
    c = _FakeClient()
    ps.sync_run(c, _plane_run(), "software_factory.pull_request_merged")
    assert ("iss-1", "Done") in c.states


def test_sync_run_failed_comments_without_state_change():
    c = _FakeClient()
    ps.sync_run(c, _plane_run(error="nope"), "code_task.failed")
    assert c.states == []                                        # no state change on failure
    assert c.comments and "nope" in c.comments[0][1]


def test_sync_run_skips_non_plane_runs():
    c = _FakeClient()
    non_plane = {"id": "r", "finalize_ctx": {"source": "console"}}
    assert ps.sync_run(c, non_plane, "code_task.started")["synced"] is False
    assert c.states == [] and c.comments == []


def test_sync_run_skips_unmapped_event_and_disabled_client():
    assert ps.sync_run(_FakeClient(), _plane_run(), "code_task.random")["synced"] is False
    assert ps.sync_run(_FakeClient(enabled=False), _plane_run(), "code_task.started")["synced"] is False
