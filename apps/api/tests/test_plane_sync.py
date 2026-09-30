"""P2/P4 — Plane status sync-back: event→state mapping, comments, and the loop-guarded orchestration."""
from sqlalchemy import text

from apps.api.app.config import get_settings
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


def _sync_unrecorded_fake(client, run, event_type):
    return ps.sync_run(client, run, event_type, allow_unrecorded_for_tests=True)


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
    out = _sync_unrecorded_fake(c, _plane_run(), "code_task.pr_opened")
    assert out["synced"] and out["state"] == "In Progress"
    assert c.states == [("iss-1", "In Progress")] and c.comments and "pull/9" in c.comments[0][1]


def test_sync_run_merged_moves_to_done():
    c = _FakeClient()
    _sync_unrecorded_fake(c, _plane_run(), "software_factory.pull_request_merged")
    assert ("iss-1", "Done") in c.states


def test_sync_run_failed_comments_without_state_change():
    c = _FakeClient()
    _sync_unrecorded_fake(c, _plane_run(error="nope"), "code_task.failed")
    assert c.states == []                                        # no state change on failure
    assert c.comments and "nope" in c.comments[0][1]


def test_sync_run_skips_non_plane_runs():
    c = _FakeClient()
    non_plane = {"id": "r", "finalize_ctx": {"source": "console"}}
    assert _sync_unrecorded_fake(c, non_plane, "code_task.started")["synced"] is False
    assert c.states == [] and c.comments == []


def test_sync_run_skips_unmapped_event_and_disabled_client():
    assert _sync_unrecorded_fake(_FakeClient(), _plane_run(), "code_task.random")["synced"] is False
    assert _sync_unrecorded_fake(_FakeClient(enabled=False), _plane_run(), "code_task.started")["synced"] is False


def test_sync_without_receipt_connection_fails_closed():
    client = _FakeClient()
    assert ps.sync_run(client, _plane_run(), "code_task.started") == {
        "synced": False, "reason": "receipt connection required"
    }
    assert not client.states and not client.comments


def _recorded_run():
    return _plane_run(id="crun_plane_sync_receipts", project_id=get_settings().default_project_id)


def test_recorded_sync_replay_does_not_repeat_plane_writes(engine):
    run = _recorded_run()
    client = _FakeClient()
    with engine.begin() as connection:
        first = ps.sync_run(client, run, "code_task.started", connection=connection)
    with engine.begin() as connection:
        replay = ps.sync_run(client, run, "code_task.started", connection=connection)
        rows = connection.execute(text(
            "SELECT effect_kind, status FROM receipts WHERE project_id=:project_id "
            "AND dedupe_key LIKE 'plane-sync:%' ORDER BY effect_kind"
        ), {"project_id": run["project_id"]}).mappings().all()
    assert first["synced"] and replay["synced"]
    assert first["receipt_ids"] == replay["receipt_ids"]
    assert len(client.states) == len(client.comments) == 1
    assert [(row["effect_kind"], row["status"]) for row in rows] == [
        ("plane.issue.comment.create", "succeeded"),
        ("plane.issue.state.update", "succeeded"),
    ]


def test_failed_plane_comment_is_visible_and_not_replayed(engine):
    class FailingClient(_FakeClient):
        def add_comment(self, project_id, issue_id, html):
            self.comments.append((issue_id, html))
            return False

    run = _recorded_run()
    client = FailingClient()
    with engine.begin() as connection:
        first = ps.sync_run(client, run, "code_task.started", connection=connection)
    with engine.begin() as connection:
        replay = ps.sync_run(client, run, "code_task.started", connection=connection)
        receipt = connection.execute(text(
            "SELECT status, error FROM receipts WHERE id=:id"
        ), {"id": first["receipt_ids"]["comment"]}).mappings().one()
        event_count = connection.execute(text(
            "SELECT count(*) FROM events WHERE project_id=:project_id "
            "AND event_type='plane.sync_failed'"
        ), {"project_id": run["project_id"]}).scalar_one()
        decision_count = connection.execute(text(
            "SELECT count(*) FROM decisions WHERE project_id=:project_id "
            "AND status='pending' AND recommendation->>'action'='review_plane_sync_failure'"
        ), {"project_id": run["project_id"]}).scalar_one()
    assert not first["synced"] and not replay["synced"]
    assert first["receipt_ids"] == replay["receipt_ids"]
    assert len(client.states) == len(client.comments) == 1
    assert receipt["status"] == "failed" and receipt["error"]["reason"]
    assert event_count == decision_count == 1


def test_uncertain_receipt_replay_escalates_once_without_resending(engine):
    run = _recorded_run()
    client = _FakeClient()
    with engine.begin() as connection:
        first = ps.sync_run(client, run, "code_task.started", connection=connection)
        connection.execute(text(
            "UPDATE receipts SET status='in_progress' WHERE id=:id"
        ), {"id": first["receipt_ids"]["comment"]})
    with engine.begin() as connection:
        second = ps.sync_run(client, run, "code_task.started", connection=connection)
    with engine.begin() as connection:
        third = ps.sync_run(client, run, "code_task.started", connection=connection)
        uncertain_events = connection.execute(text(
            "SELECT count(*) FROM events WHERE entity_id=:receipt_id "
            "AND event_type='plane.sync_uncertain'"
        ), {"receipt_id": first["receipt_ids"]["comment"]}).scalar_one()
        decisions = connection.execute(text(
            "SELECT count(*) FROM decisions WHERE project_id=:project_id "
            "AND status='pending' AND recommendation->>'action'='review_plane_sync_uncertain'"
        ), {"project_id": run["project_id"]}).scalar_one()
    assert not second["synced"] and not third["synced"]
    assert len(client.states) == len(client.comments) == 1
    assert uncertain_events == decisions == 1
