"""Playbooks are saved guidance; captain still refuses invented finish."""
from boss_engine.services.captain_coordination import plan_staffing
from boss_engine.services.captain_playbook import (
    apply_playbook_to_plan,
    compile_playbook,
    default_official_playbook,
)


def test_compile_playbook_dictates_required_roles_and_loop():
    book = compile_playbook(
        "Official canary. Must include QA and UX. Panel of 5. "
        "Loop until tests pass. Never invent scores. Max 8 ticks."
    )
    assert book["constraints"]["required_roles"]
    assert "qa" in book["constraints"]["required_roles"]
    assert "ux_engineer" in book["constraints"]["required_roles"]
    assert book["constraints"]["must_panel"] is True
    assert book["constraints"]["panel_size"] == 5
    assert book["loop"]["autonomous"] is True
    assert book["loop"]["max_ticks"] == 8
    assert book["goal"]["require_tests_passed"] is True


def test_compile_without_ux_forbids_ux():
    book = compile_playbook("Build workflow without UX. Must include QA.")
    assert "ux_engineer" in book["constraints"]["forbidden_roles"]
    assert "qa" in book["constraints"]["required_roles"]


def test_empty_playbook_text_fails_closed():
    try:
        compile_playbook("   ")
    except ValueError as exc:
        assert "empty" in str(exc)
    else:
        raise AssertionError("empty playbook must fail")


def test_playbook_cannot_waive_honest_finish():
    snap = {
        "id": "crun_x",
        "official": True,
        "status": "running",
        "phase": "run_tests",
        "process_run_id": "aprun_1",
        "diagnosis": {"klass": "in_flight"},
        "tests_passed": None,
    }
    plan = apply_playbook_to_plan(plan_staffing(snap), default_official_playbook())
    assert plan["finish"]["ready"] is False
    assert plan["playbook_key"] == "official_verify_green"
    now_ids = {item["agent_id"] for item in plan["assignments"] if item["timing"] == "now"}
    assert "qa" in now_ids
    assert "ux_engineer" in now_ids
    assert plan["panel"]["size"] == 5
