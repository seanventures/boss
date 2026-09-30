"""Factory flow diagnosis is evidence-only and never invents a pass."""
from boss_engine.services.factory_flow import diagnose_flow, flow_steps


def test_unbound_local_is_a_factory_crash():
    result = diagnose_flow(
        {
            "status": "failed",
            "phase": "done",
            "error": "cannot access local variable 'evidence' where it is not associated with a value",
        }
    )
    assert result["klass"] == "factory_crash"
    assert result["state"] == "failed"
    assert "evidence" in result["recommended_fix"].lower() or "dict" in result["recommended_fix"]


def test_exit_zero_log_is_verify_dishonest():
    result = diagnose_flow(
        {
            "status": "completed",
            "phase": "done",
            "error": "",
            "last_verify_output": "> hookwatch@1.0.0 verify\n> exit 0\n",
        }
    )
    assert result["klass"] == "verify_dishonest"


def test_queued_behind_single_worker():
    result = diagnose_flow({"status": "running", "phase": "queued", "error": ""})
    assert result["klass"] == "queued_serial"
    assert result["state"] == "waiting"


def test_phase_stall_after_threshold():
    result = diagnose_flow(
        {
            "status": "running",
            "phase": "run_tests",
            "error": "",
            "minutes_in_phase": 40,
        }
    )
    assert result["klass"] == "phase_stall"


def test_healthy_in_flight_is_not_a_finding():
    result = diagnose_flow(
        {
            "status": "running",
            "phase": "run_tests",
            "error": "",
            "minutes_in_phase": 4,
            "process_status": "waiting",
        }
    )
    assert result["klass"] == "in_flight"
    assert result["recommended_fix"] == ""


def test_flow_steps_merge_factory_and_swarm():
    steps = flow_steps(
        {
            "status": "running",
            "phase": "run_tests",
            "phase_timings": [{"phase": "write_tests", "ms": 10}, {"phase": "implement", "ms": 20}],
            "process_steps": [
                {"step_key": "intake_prd", "status": "succeeded", "role_key": "product_manager"},
                {"step_key": "captain_build", "status": "waiting", "role_key": "engineering_captain"},
            ],
        }
    )
    names = [item["name"] for item in steps]
    assert "write_tests" in names
    assert "captain_build" in names
    assert any(item["lane"] == "swarm" for item in steps)
