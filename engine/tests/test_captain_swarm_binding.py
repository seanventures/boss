"""Official runs bind the captain/process swarm instead of a lone factory job."""
from pathlib import Path


def test_followup_and_official_post_bind_captain_swarm():
    follow = Path("engine/boss_engine/services/code_task_service.py").read_text(encoding="utf-8")
    assert "bind_captain_swarm(" in follow
    assert "attach_run_id=run_id" in follow
    main = Path("apps/api/app/main.py").read_text(encoding="utf-8")
    assert "bind_captain_swarm(" in main
    process = Path("engine/boss_engine/services/agent_process.py").read_text(encoding="utf-8")
    assert "attach_run_id" in process
    assert 'patches[0]["attached"]' not in process
    assert 'attached": True' in process or '"attached": True' in process
    assert "def bind_captain_swarm(" in process


def test_attached_build_does_not_call_start_code_task():
    src = Path("engine/boss_engine/services/agent_process.py").read_text(encoding="utf-8")
    attach_idx = src.find("if attach:")
    start_idx = src.find("run_id = start_code_task(")
    assert attach_idx != -1
    assert start_idx != -1
    assert attach_idx < start_idx
