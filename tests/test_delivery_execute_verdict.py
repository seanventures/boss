"""execute_deployment must not mark a rolled-back apply as succeeded.

deploy_ssh health-gates and returns status=rolled_back while restoring the previous
build. observe() then sees that previous build as healthy. Trusting observe alone marked
the failed placement succeeded and left commit_ref naming the broken SHA.
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


def test_rolled_back_apply_is_failed_not_succeeded() -> None:
    run = {
        "id": "drun_1",
        "project_id": "proj_1",
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
    fake_connector.plan.return_value = {"steps": []}
    fake_connector.apply.return_value = {
        "status": "rolled_back",
        "detail": "health check returned 500; rolled back to previous build",
    }
    # Previous build is healthy — this is exactly the trap.
    fake_connector.observe.return_value = {"healthy": True, "checks": []}

    with patch("boss_engine.db.begin", _ConnCtx()), \
         patch("boss_engine.services.deployment_connectors.preflight", return_value={"ready": True, "problems": []}), \
         patch("boss_engine.services.deployment_connectors.connector_for", return_value=fake_connector), \
         patch.object(dp, "emit_event"):
        result = dp.execute_deployment(engine, SimpleNamespace(), "drun_1")

    assert result.get("status") == "failed", result
    assert fake_connector.observe.call_count == 0
    assert result.get("healthy") is False, result
    statuses = [u.get("s") for u in conn.updates if "s" in u]
    assert "failed" in statuses, conn.updates


def test_deployed_then_unhealthy_observe_is_failed() -> None:
    run = {
        "id": "drun_2",
        "project_id": "proj_1",
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
    fake_connector.plan.return_value = {"steps": []}
    fake_connector.apply.return_value = {
        "status": "deployed", "deployed_sha": "abcd123",
    }
    fake_connector.observe.return_value = {"healthy": False, "checks": [{"ok": False}]}

    with patch("boss_engine.db.begin", _ConnCtx()), \
         patch("boss_engine.services.deployment_connectors.preflight", return_value={"ready": True, "problems": []}), \
         patch("boss_engine.services.deployment_connectors.connector_for", return_value=fake_connector), \
         patch.object(dp, "emit_event"):
        result = dp.execute_deployment(SimpleNamespace(), SimpleNamespace(), "drun_2")

    assert result.get("status") == "failed", result
    assert fake_connector.observe.call_count == 1
