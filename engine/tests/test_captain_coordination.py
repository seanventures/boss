"""Captain staffs from capabilities and will not accept invented finish."""
from boss_engine.services.captain_coordination import (
    convene_panel,
    finish_check,
    needs_from_snapshot,
    plan_staffing,
    roster,
)


def test_roster_has_captain_and_specialists():
    ids = {item["id"] for item in roster()}
    assert "engineering_captain" in ids
    assert "qa" in ids
    assert "factory_driver" in ids


def test_new_official_run_needs_prd_review_and_build():
    needs = needs_from_snapshot(
        {"official": True, "status": "queued", "phase": "queued", "process_run_id": None}
    )
    assert "prd_review" in needs
    assert "build" in needs
    assert "coordinate" in needs


def test_crash_needs_repair_not_a_pass():
    needs = needs_from_snapshot(
        {
            "status": "failed",
            "phase": "done",
            "diagnosis": {"klass": "factory_crash"},
            "tests_passed": None,
        }
    )
    assert "repair" in needs
    assert "verify" in needs
    finish = finish_check(
        {
            "status": "failed",
            "tests_passed": None,
            "diagnosis": {"klass": "factory_crash"},
            "error": "UnboundLocalError",
        }
    )
    assert finish["ready"] is False
    assert finish["verdict"] == "not_finished"


def test_official_certify_panel_includes_ux():
    panel = convene_panel(
        {"official": True, "tests_passed": True, "phase": "review_panel", "diagnosis": {}},
        ["certify", "ux"],
    )
    assert panel is not None
    assert "ux_engineer" in panel["roles"]
    assert panel["size"] == 5


def test_plan_only_assigns_fitting_agents_now():
    plan = plan_staffing(
        {
            "id": "crun_x",
            "official": True,
            "status": "running",
            "phase": "run_tests",
            "process_run_id": "aprun_1",
            "diagnosis": {"klass": "in_flight"},
            "tests_passed": None,
        }
    )
    now_ids = {item["agent_id"] for item in plan["assignments"] if item["timing"] == "now"}
    assert "engineering_captain" in now_ids
    assert "factory_driver" in now_ids
    idle = [item for item in plan["roster"] if not item["fit"]]
    assert idle  # unused specialists stay idle
    assert plan["finish"]["ready"] is False


def test_finish_requires_completed_and_green_verify():
    done = finish_check(
        {
            "status": "completed",
            "tests_passed": True,
            "diagnosis": {"klass": "in_flight"},
            "error": None,
        }
    )
    assert done["ready"] is True
    assert done["verdict"] == "finished"
