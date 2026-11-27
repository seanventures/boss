from datetime import datetime, timedelta, timezone
from unittest.mock import Mock

from boss_engine.services import storage_cleanup, storage_incidents, storage_watch


def test_cleanup_status_distinguishes_blocked_running_and_exhausted():
    incident = {"status": "critical", "helper": {"reachable": False}, "plan": []}
    assert storage_incidents.activity_text(incident) == "Cleanup blocked: host helper unavailable"
    incident["plan"] = [{"state": "running", "where": "api"}]
    assert storage_incidents.activity_text(incident) == "Infra Agent is cleaning"
    incident["helper"]["reachable"] = True
    incident["plan"] = [{"state": "skipped"}]
    assert storage_incidents.activity_text(incident) == "Cleanup exhausted: free space by hand"
    incident["status"] = "asking"
    assert storage_incidents.activity_text(incident) == "Infra Agent needs one answer"


def test_tiny_backup_inventory_is_not_blamed_for_disk_pressure():
    key, message = storage_incidents.explain_cause(
        {"total_gb": 60, "free_gb": 5, "backups_gb": 0.001, "backups_n": 7}, None, {})
    assert key == "disk.low"
    assert "backups" not in message


def test_confirmed_downgrade_does_not_leave_incident_critical(monkeypatch):
    incident = {"id": "incident", "severity": "critical", "status": "critical", "plan": []}
    monkeypatch.setattr(storage_incidents, "current_for_host", lambda *args: incident)
    monkeypatch.setattr(storage_incidents, "default_project", lambda *args: None)
    monkeypatch.setattr(storage_incidents, "_row", lambda *args: incident)
    monkeypatch.setattr(storage_incidents, "_update", lambda *args, **fields: incident.update(fields))
    storage_incidents.on_level(None, host_id="host", level="warn", previous="critical",
                               sample={"total_gb": 60, "free_gb": 30}, forecast=None,
                               thresholds={"target_free_pct": 25})
    assert incident["severity"] == "warn"
    assert incident["status"] == "warn"


def test_new_readings_extend_exhausted_plan_without_repeating_attempts(monkeypatch):
    old = [{"tool": "storage.drop_scratch_dbs", "state": "skipped"},
           {"tool": "storage.remove_images", "state": "failed"}]
    candidates = [{"tool": step["tool"], "state": "planned"} for step in old]
    candidates.append({"tool": "storage.prune_build_cache", "state": "planned"})
    monkeypatch.setattr(storage_cleanup, "build_plan", lambda *args, **kwargs: candidates)
    extended = storage_cleanup.extend_exhausted_plan(None, {"severity": "critical"}, {}, old)
    assert extended == [*old, candidates[-1]]
    assert old[1]["state"] == "failed"
    assert storage_cleanup.extend_exhausted_plan(None, {}, {}, extended) == extended


def test_active_plan_is_never_rebuilt(monkeypatch):
    def unexpected(*args, **kwargs):
        raise AssertionError("active plans must not be rebuilt")
    monkeypatch.setattr(storage_cleanup, "build_plan", unexpected)
    for state in ("ask", "queued", "running", "planned"):
        plan = [{"tool": "storage.remove_images", "state": state}]
        assert storage_cleanup.extend_exhausted_plan(None, {}, {}, plan) is plan


def test_fresh_host_disk_wins_over_newer_container_fallback():
    now = datetime.now(timezone.utc)
    container = {"source": "api", "at": now, "total_gb": 60.0, "free_gb": 13.0,
                 "used_pct": None, "host_label": "container", "by_type": {}}
    host = {"source": "steward", "at": now - timedelta(seconds=15), "total_gb": 500.0,
            "free_gb": 9.0, "used_pct": None, "host_label": "laptop", "by_type": {}}
    connection = Mock()
    connection.execute.return_value.mappings.return_value.all.return_value = [container, host]
    sample = storage_watch.latest(connection, "install")
    assert sample["source"] == "steward"
    assert sample["free_gb"] == 9.0
    assert sample["total_gb"] == 500.0
    host["at"] = now - timedelta(minutes=3)
    sample = storage_watch.latest(connection, "install")
    assert sample["source"] == "api"
    assert sample["free_gb"] == 13.0


def test_forecast_and_hysteresis_query_only_the_selected_disk_source(monkeypatch):
    monkeypatch.setattr(storage_watch, "disk_source", lambda *args: "steward")
    connection = Mock()
    connection.execute.return_value.mappings.return_value.all.return_value = []
    connection.execute.return_value.all.return_value = []
    storage_watch.last_two(connection, "install")
    storage_watch.used_series(connection, "install")
    for call in connection.execute.call_args_list:
        assert "source = :source" in str(call.args[0])
        assert call.args[1]["source"] == "steward"


def test_clean_now_retries_a_finished_safe_plan_without_erasing_other_steps(monkeypatch):
    previous = [
        {"tool": "trim", "tier": 1, "state": "failed"},
        {"tool": "images", "tier": 1, "state": "refused"},
        {"tool": "backups", "tier": 3, "state": "done", "receipt_id": "kept"},
    ]
    trim = {"tool": "trim", "tier": 1, "state": "planned"}
    monkeypatch.setattr(storage_cleanup, "_plan", lambda c, i: ({"status": "critical"}, previous))
    monkeypatch.setattr(storage_cleanup, "build_plan", lambda c, i, s, max_tier: [
        trim, {"tool": "images", "tier": 1, "state": "planned"}])
    saved = []
    monkeypatch.setattr(storage_cleanup, "_save_plan", lambda c, i, p: saved.append(p))
    storage_cleanup.request_safe_cleanup(None, "incident", {})
    assert saved == [[trim, previous[1], previous[2]]]
    for state in ("planned", "running", "queued", "ask"):
        previous[0]["state"] = state
        storage_cleanup.request_safe_cleanup(None, "incident", {})
    assert len(saved) == 1
