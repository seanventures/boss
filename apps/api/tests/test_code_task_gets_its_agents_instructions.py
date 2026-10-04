"""A code task that runs as an agent gets that agent's work instructions (spec §4.8, E11)."""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from apps.api.tests.instructions_support import MARKER, PROJECT, add, make_agent


def test_the_role_it_runs_as_gives_its_instructions_and_no_other(client, engine):
    from boss_engine.services.agent_instructions import for_code_task

    key = make_agent(client)
    other = make_agent(client)
    assert add(client, key, MARKER).status_code == 201
    with engine.begin() as c:
        mine = for_code_task(c, PROJECT, key)
        theirs = for_code_task(c, PROJECT, other)
        nobody = for_code_task(c, PROJECT, None)
    assert "ZEBRA-CHECK-7" in mine["text"] and "(BOSS agent v" in mine["text"]
    assert mine["meta"]["names"] == ["review-checklist.md"] and mine["meta"]["role_key"] == key
    assert theirs == {"text": "", "meta": None} and nobody == {"text": "", "meta": None}


def test_the_harness_prompt_carries_them(monkeypatch, tmp_path):
    from boss_engine.services import cli_harness, verification_target
    from boss_engine.services.code_agent import CodeTaskSpec

    seen: dict = {}
    agent = cli_harness.GenericCliCodeAgent(SimpleNamespace(),
                                            cli_harness.BUILTIN_CLI_HARNESSES["grok"])
    monkeypatch.setattr(agent, "run_role", lambda prompt, cwd: seen.setdefault(
        "prompt", prompt) and {"summary": "ok", "usage": {}})
    monkeypatch.setattr(verification_target, "run_until_failure", lambda sandbox, cmds: [])
    sandbox = SimpleNamespace(path=Path(tmp_path), diff=lambda: [], diff_summary=lambda: None)
    spec = CodeTaskSpec(task_id="t", title="Fix it", instructions="Do the thing.",
                        agent_instructions="INSTRUCTIONS FOR Raptor (BOSS agent v3)\n" + MARKER)
    agent.run(spec, SimpleNamespace(), sandbox)
    assert "ZEBRA-CHECK-7" in seen["prompt"] and "Raptor (BOSS agent v3)" in seen["prompt"]
    seen.clear()
    agent.run(CodeTaskSpec(task_id="t", title="Fix it", instructions="Do the thing."),
              SimpleNamespace(), sandbox)
    assert "ZEBRA-CHECK-7" not in seen["prompt"]
