"""A direct connector cannot deploy without the Resource Plane receipt boundary.

The old connector verdict tests reached apply() directly. That path is now held before
preflight, so these tests check the actual policy result and lack of host effects.
"""
from __future__ import annotations

import sys
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

sys.path.insert(0, "engine")

from boss_engine.services import delivery_pipeline as dp  # noqa: E402

class _Maps:
    def __init__(self, row):
        self._row = row

    def first(self):
        return self._row


class _Result:
    def __init__(self, row):
        self._row = row

    def mappings(self):
        return _Maps(self._row)


class _Conn:
    def __init__(self, run_row, target_row):
        self.run_row = run_row
        self.target_row = target_row
        self.updates: list[dict] = []

    def execute(self, stmt, params=None):
        params = params or {}
        sql = str(stmt)
        if "FROM deployment_runs" in sql and "SELECT" in sql:
            return _Result(self.run_row)
        if "FROM deployment_targets" in sql:
            return _Result(self.target_row)
        if "UPDATE deployment_runs" in sql:
            self.updates.append(dict(params))
            return _Result(None)
        return _Result(None)

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def test_unlinked_connector_run_is_blocked_before_host_effects() -> None:
    run = {
        "id": "drun_1",
        "project_id": "proj_1",
        "status": "created",
        "deployment_target_id": "deptgt_1",
        "commit_ref": "deadbeef",
        "release_packet": {"code_task_run_id": "crun_1"},
    }
    target = {
        "id": "deptgt_1",
        "project_id": "proj_1",
        "target_kind": "ssh",
        "target_ref": "ubuntu@10.0.0.1",
        "app_name": "stock-app",
        "repo": "boss-sandbox",
        "domains": ["10.0.0.1:3004"],
        "health_checks": [{"kind": "http", "path": "/", "expect": 200, "scheme": "http"}],
        "secret_refs": [],
        "required_resources": [],
    }
    conn = _Conn(run, target)
    engine = SimpleNamespace()

    class _ConnCtx:
        def __call__(self, _engine):
            return conn

    fake_connector = MagicMock()
    fake_connector.kind = "ssh"
    with patch("boss_engine.db.begin", _ConnCtx()), \
         patch("boss_engine.services.deployment_connectors.preflight") as preflight, \
         patch("boss_engine.services.deployment_connectors.connector_for", return_value=fake_connector), \
         patch.object(dp, "create_blocking_decision", return_value="decision_1") as decision, \
         patch.object(dp, "emit_event"):
        result = dp.execute_deployment(engine, SimpleNamespace(), "drun_1")

    assert result["status"] == "blocked"
    assert result["reason_code"] == "deployment_missing_changeset"
    decision.assert_called_once()
    assert any(update.get("decision_id") == "decision_1" for update in conn.updates)
    preflight.assert_not_called()
    fake_connector.apply.assert_not_called()
    fake_connector.observe.assert_not_called()


def test_foreign_changeset_run_is_blocked_before_host_effects() -> None:
    run = {
        "id": "drun_2",
        "project_id": "proj_1",
        "status": "created",
        "changeset_id": "changeset_other_project",
        "deployment_target_id": "deptgt_1",
        "commit_ref": "abcd123",
        "release_packet": {"code_task_run_id": "crun_2"},
    }
    target = {
        "id": "deptgt_1", "project_id": "proj_1", "target_kind": "ssh",
        "target_ref": "ubuntu@10.0.0.1", "app_name": "stock-app", "repo": "boss-sandbox",
        "domains": ["10.0.0.1:3004"],
        "health_checks": [{"kind": "http", "path": "/", "expect": 200}],
        "secret_refs": [], "required_resources": [],
    }
    conn = _Conn(run, target)

    class _ConnCtx:
        def __call__(self, _engine):
            return conn

    fake_connector = MagicMock()
    fake_connector.kind = "ssh"
    with patch("boss_engine.db.begin", _ConnCtx()), \
         patch("boss_engine.services.deployment_connectors.preflight") as preflight, \
         patch("boss_engine.services.deployment_connectors.connector_for", return_value=fake_connector), \
         patch.object(dp, "create_blocking_decision") as decision, \
         patch.object(dp, "emit_event"):
        result = dp.execute_deployment(SimpleNamespace(), SimpleNamespace(), "drun_2")

    assert result["status"] == "blocked"
    assert result["reason_code"] == "deployment_changeset_scope_mismatch"
    decision.assert_not_called()
    assert conn.updates == []  # A linked run remains available for governed resolution.
    preflight.assert_not_called()
    fake_connector.apply.assert_not_called()
    fake_connector.observe.assert_not_called()
