"""A Codex reviewer whose tools cannot run did not review (BOSSAPP-1350).

crun_9f190ed1ede845c09cdc: every review seat, and the acceptance judge, ran on Codex with
`--sandbox read-only`, which needs user namespaces the container does not grant. Each command
died with `bwrap: No permissions to create a new namespace`; the judge then called 30 items
"partial: could not be verified" and the adversarial review "refuted" code that had met 29/30.

  1. A read-only Codex seat runs on BOSS's throwaway copy with no inner sandbox, so it can read.
  2. BOSS holds read-only itself: a seat that edits the live tree is reverted and fails.
  3. A seat or judge whose tools could not run is NOT RUN with the reason, never a verdict.
  4. A reviewer of a local repository is told there is no GitHub for it.

The binary is a fake `codex`; no model is called.
"""
from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
import textwrap

import pytest

from boss_engine.config import get_settings
from boss_engine.services import acceptance_gate as ag
from boss_engine.services import engine_contract as ec
from boss_engine.services import llm_telemetry, review_input, review_tools

BWRAP = "bwrap: No permissions to create a new namespace"

FAKE_CODEX = textwrap.dedent('''\
    #!{python}
    import json, os, sys
    argv = sys.argv[1:]
    prompt = sys.stdin.read()
    with open({log!r}, "a") as log:
        log.write(json.dumps({{"argv": argv, "prompt": prompt, "cwd": os.getcwd()}}) + "\\n")
    behaviour = open({behaviour!r}).read().strip()
    if behaviour == "edit_live":
        open({live!r}, "w").write("tampered\\n")
    final = argv[argv.index("--output-last-message") + 1]
    text = "REVIEWED: a.py\\nVERDICT: SURVIVED"
    if behaviour == "blind":
        text = "Every command failed: {bwrap}. The change could not be verified."
    print(json.dumps({{"type": "thread.started", "thread_id": "t1"}}), flush=True)
    print(json.dumps({{"type": "item.completed",
                      "item": {{"type": "agent_message", "text": text}}}}), flush=True)
    print(json.dumps({{"type": "turn.completed",
                      "usage": {{"input_tokens": 1, "output_tokens": 1}}}}), flush=True)
    open(final, "w").write(text)
''')


@pytest.fixture
def seat(tmp_path, monkeypatch):
    from boss_engine.services.cli_harness import load_specs
    from boss_engine.services.codex_code_agent import CodexCodeAgent

    behaviour = tmp_path / "behaviour"
    behaviour.write_text("ok")
    tree = tmp_path / "tree"
    tree.mkdir()
    (tree / "a.py").write_text("x = 1\n")
    for cmd in (["init", "-q"], ["add", "."], ["-c", "user.name=t", "-c", "user.email=t@t",
                                               "commit", "-qm", "init"]):
        subprocess.run(["git", *cmd], cwd=tree, check=True)
    fake = tmp_path / "codex"
    fake.write_text(FAKE_CODEX.format(python=sys.executable, log=str(tmp_path / "fake.log"),
                                      behaviour=str(behaviour), live=str(tree / "a.py"),
                                      bwrap=BWRAP))
    fake.chmod(fake.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setenv("PATH", f"{tmp_path}{os.pathsep}{os.environ.get('PATH', '')}")
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "codex-home"))
    monkeypatch.setenv("BOSS_CODE_AGENT_IDLE_TIMEOUT_SECONDS", "0")
    monkeypatch.setattr(llm_telemetry, "record_llm_call", lambda **kw: None)
    monkeypatch.setattr(llm_telemetry, "record_agent_turns", lambda *a, **kw: 0)
    inner = CodexCodeAgent(get_settings(), load_specs(get_settings())["codex"])
    inner._auth_checked = True
    monkeypatch.setattr(inner, "remember_rotated_credential", lambda: False)
    agent = ec.ContractedAgent(inner)

    def calls() -> list[dict]:
        path = tmp_path / "fake.log"
        return [json.loads(x) for x in path.read_text().splitlines()] if path.exists() else []

    return agent, tree, behaviour, calls


def test_a_codex_reviewer_runs_on_a_copy_without_the_inner_sandbox(seat):
    agent, tree, _, calls = seat
    agent.run_role("Review it.", tree, read_only=True)
    ran = calls()[-1]
    assert "danger-full-access" in ran["argv"] and "read-only" not in ran["argv"]
    assert os.path.realpath(ran["cwd"]) != os.path.realpath(str(tree))   # the copy, not the tree


def test_a_reviewer_that_edits_the_live_tree_is_reverted_and_fails(seat):
    agent, tree, behaviour, _ = seat
    behaviour.write_text("edit_live")
    with pytest.raises(ec.EngineSessionFailed) as failed:
        agent.run_role("Review it.", tree, read_only=True)
    assert "a.py" in str(failed.value) and "reverted" in str(failed.value)
    assert (tree / "a.py").read_text() == "x = 1\n"


def test_a_reviewer_leaves_the_builders_uncommitted_work_alone(seat):
    agent, tree, _, _ = seat
    (tree / "a.py").write_text("x = 2\n")           # the change under review, not committed
    agent.run_role("Review it.", tree, read_only=True)
    assert (tree / "a.py").read_text() == "x = 2\n"


def test_an_edit_over_uncommitted_work_restores_that_work(seat):
    agent, tree, behaviour, _ = seat
    (tree / "a.py").write_text("x = 2\n")
    behaviour.write_text("edit_live")
    with pytest.raises(ec.EngineSessionFailed):
        agent.run_role("Review it.", tree, read_only=True)
    assert (tree / "a.py").read_text() == "x = 2\n"


def test_a_writer_is_not_copied_or_guarded(seat):
    agent, tree, _, calls = seat
    agent.run_role("Build it.", tree)
    assert os.path.realpath(calls()[-1]["cwd"]) == os.path.realpath(str(tree))


def test_a_blind_reviewer_is_not_run_never_a_verdict(seat):
    agent, tree, behaviour, _ = seat
    behaviour.write_text("blind")
    out = agent.run_role("Review it.", tree, read_only=True)
    reason = review_input.hollow_reason(out["summary"], ["a.py"])
    assert reason and reason.startswith(review_tools.NOT_RUN)
    assert "namespace" in reason


def test_a_review_that_cites_lines_is_not_blind_for_naming_a_sandbox():
    said = f"FINDING: MAJOR a.py:3 — handles {BWRAP} wrongly"
    assert review_tools.blocked_reason(said) is None


def test_an_acceptance_judge_whose_tools_could_not_run_leaves_its_items_unjudged(tmp_path):
    (tmp_path / "a.py").write_text("x = 1\n")
    items = ag.acceptance_items({"acceptance_criteria": [f"criterion {n} holds" for n in range(5)]})
    reply = "\n".join(f"ITEM {n}: PARTIAL — could not be verified: {BWRAP}"
                      for n in range(1, 6))
    rec, _ = ag.run_check(items, ask=lambda p: {"summary": reply, "cost_usd_micros": 0},
                          title="t", stack={}, root=tmp_path)
    assert rec["verdict"] == "not_run" and rec["refused"]
    assert rec["counts"]["partial"] == 0 and rec["counts"]["unjudged"] == 5
    assert review_tools.NOT_RUN in rec["why"]


def test_a_judge_that_says_every_item_could_not_be_verified_is_not_run(tmp_path):
    (tmp_path / "a.py").write_text("x = 1\n")
    items = ag.acceptance_items({"acceptance_criteria": [f"criterion {n} holds" for n in range(4)]})
    reply = "\n".join(f"ITEM {n}: PARTIAL — could not be verified, no command ran"
                      for n in range(1, 5))
    rec, _ = ag.run_check(items, ask=lambda p: {"summary": reply, "cost_usd_micros": 0},
                          title="t", stack={}, root=tmp_path)
    assert rec["verdict"] == "not_run" and rec["counts"]["partial"] == 0


def test_a_local_repository_reviewer_is_told_there_is_no_github(seat):
    agent, tree, _, calls = seat
    agent.local_repo = True
    agent.run_role("Review it.", tree, read_only=True)
    assert "LOCAL to BOSS" in calls()[-1]["prompt"]
    agent.local_repo = False
    agent.run_role("Review it again.", tree, read_only=True)
    assert "LOCAL to BOSS" not in calls()[-1]["prompt"]
