"""A Codex reviewer whose tools cannot run did not review (BOSSAPP-1350).

crun_9f190ed1ede845c09cdc: every review seat, and the acceptance judge, ran on Codex with
`--sandbox read-only`, which needs user namespaces the container does not grant. Each command
died with `bwrap: No permissions to create a new namespace`; the judge then called 30 items
"partial: could not be verified" and the adversarial review "refuted" code that had met 29/30.

  1. A read-only Codex seat runs on BOSS's throwaway copy with no inner sandbox, so it can read.
  2. BOSS watches the live tree (report-only): a change is reported, never reverted, and never
     fails the seat; one no concurrent verify explains marks the verdict suspect.
  3. A seat or judge whose tools could not run — the ENGINE's report, never the model's prose —
     is NOT RUN with the reason, never a verdict.
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
    if behaviour == "write_gosum":
        open({gosum!r}, "w").write("h1:verify\\n")
    if behaviour == "write_untracked":
        open({untracked!r}, "w").write("new\\n")
    final = argv[argv.index("--output-last-message") + 1]
    text = "REVIEWED: a.py\\nVERDICT: SURVIVED"
    if behaviour == "blind":
        text = "Every command failed. The change could not be verified."
        print("{bwrap}", file=sys.stderr)
    if behaviour == "talks_about_it":
        text = "REVIEWED: a.py\\n1. **FINDING: MAJOR a.py:8** seccomp filter is wrong; the " \\
               "user namespace is not unshared.\\nVERDICT: REFUTED"
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
    (tree / "app").mkdir()
    (tree / "app" / "main.go").write_text("package main\n")
    for cmd in (["init", "-q"], ["add", "."], ["-c", "user.name=t", "-c", "user.email=t@t",
                                               "commit", "-qm", "init"]):
        subprocess.run(["git", *cmd], cwd=tree, check=True)
    fake = tmp_path / "codex"
    fake.write_text(FAKE_CODEX.format(python=sys.executable, log=str(tmp_path / "fake.log"),
                                      behaviour=str(behaviour), live=str(tree / "a.py"),
                                      gosum=str(tree / "app" / "go.sum"),
                                      untracked=str(tree / "app" / "notes.txt"),
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


def test_an_edit_of_the_live_tree_is_reported_and_marks_the_verdict_suspect(seat):
    agent, tree, behaviour, _ = seat
    behaviour.write_text("edit_live")
    out = agent.run_role("Review it.", tree, read_only=True)
    assert out["live_tree_changed"]["paths"] == ["a.py"]
    assert "a.py" in out["verdict_suspect"]
    assert (tree / "a.py").read_text() == "tampered\n"          # never reverted


def test_the_guard_works_from_the_repository_top_level_for_an_app_subfolder(seat):
    agent, tree, behaviour, _ = seat
    behaviour.write_text("edit_live")
    out = agent.run_role("Review it.", tree / "app", read_only=True)
    assert out["live_tree_changed"]["paths"] == ["a.py"]


def test_a_concurrent_verify_writing_go_sum_is_no_failure_and_no_suspicion(seat):
    agent, tree, behaviour, _ = seat
    behaviour.write_text("write_gosum")
    out = agent.run_role("Review it.", tree / "app", read_only=True)
    assert out["live_tree_changed"]["paths"] == ["app/go.sum"]
    assert not out["live_tree_changed"]["unexplained"] and "verdict_suspect" not in out
    assert (tree / "app" / "go.sum").exists()                      # never reverted


def test_an_untracked_file_is_reported(seat):
    agent, tree, behaviour, _ = seat
    behaviour.write_text("write_untracked")
    out = agent.run_role("Review it.", tree, read_only=True)
    assert out["live_tree_changed"]["unexplained"] == ["app/notes.txt"]
    assert "verdict_suspect" in out


def test_a_reviewer_leaves_the_builders_uncommitted_work_alone(seat):
    agent, tree, _, _ = seat
    (tree / "a.py").write_text("x = 2\n")           # the change under review, not committed
    out = agent.run_role("Review it.", tree, read_only=True)
    assert (tree / "a.py").read_text() == "x = 2\n" and "live_tree_changed" not in out


def test_a_cwd_that_is_not_a_repo_is_not_copied(seat, tmp_path, monkeypatch):
    agent, _, _, calls = seat
    plain = tmp_path / "plain"
    plain.mkdir()
    monkeypatch.chdir(plain)                  # the control-plane cwd ("." in verifier_debate)
    agent.run_role("Review it.", plain, read_only=True)
    assert os.path.realpath(calls()[-1]["cwd"]) == os.path.realpath(str(plain))


def test_a_writer_is_not_copied_or_guarded(seat):
    agent, tree, _, calls = seat
    agent.run_role("Build it.", tree)
    assert os.path.realpath(calls()[-1]["cwd"]) == os.path.realpath(str(tree))


def test_a_reviewer_whose_commands_died_in_the_sandbox_is_not_run(seat):
    agent, tree, behaviour, _ = seat
    behaviour.write_text("blind")
    out = agent.run_role("Review it.", tree, read_only=True)
    assert "namespace" in out["tools_blocked"]


def test_a_real_review_about_seccomp_and_namespaces_stays_a_verdict(seat):
    agent, tree, behaviour, _ = seat
    behaviour.write_text("talks_about_it")
    out = agent.run_role("Review it.", tree, read_only=True)
    assert "tools_blocked" not in out
    assert review_tools.blocked_by_transport("", json.dumps(
        {"type": "item.completed", "item": {"type": "agent_message", "text": BWRAP}})) is None


def test_a_command_event_that_printed_the_error_is_a_transport_signal():
    event = json.dumps({"type": "item.completed", "item": {
        "type": "command_execution", "exit_code": 1, "aggregated_output": BWRAP}})
    assert "namespace" in review_tools.blocked_by_transport("", event)


def _judge(tmp_path, n, reply_for, **extra):
    (tmp_path / "a.py").write_text("x = 1\n")
    items = ag.acceptance_items({"acceptance_criteria": [f"criterion {i} holds"
                                                         for i in range(n)]})
    reply = "\n".join(f"ITEM {i}: {reply_for(i)}" for i in range(1, n + 1))
    return ag.run_check(items, ask=lambda p: {"summary": reply, "cost_usd_micros": 0, **extra},
                        title="t", stack={}, root=tmp_path)[0]


def test_a_two_item_judge_with_a_tool_error_is_not_run(tmp_path):
    rec = _judge(tmp_path, 2, lambda i: "PARTIAL — could not be verified",
                 tools_blocked=review_tools.blocked_by_transport(BWRAP))
    assert rec["verdict"] == "not_run" and rec["counts"]["partial"] == 0
    assert rec["counts"]["unjudged"] == 2 and review_tools.NOT_RUN in rec["why"]


def test_missing_items_that_could_not_be_verified_stay_missing(tmp_path):
    rec = _judge(tmp_path, 3, lambda i: "MISSING — could not be verified, nothing found")
    assert rec["verdict"] != "not_run" and rec["counts"]["missing"] == 3


def test_a_judge_that_cited_nothing_and_claimed_nothing_missing_is_not_run(tmp_path):
    rec = _judge(tmp_path, 4, lambda i: "PARTIAL — could not be verified")
    assert rec["verdict"] == "not_run" and rec["counts"]["partial"] == 0


def test_a_local_repository_reviewer_is_told_there_is_no_github(seat):
    agent, tree, _, calls = seat
    agent.local_repo = True
    agent.run_role("Review it.", tree, read_only=True)
    assert "LOCAL to BOSS" in calls()[-1]["prompt"]
    agent.local_repo = False
    agent.run_role("Review it again.", tree, read_only=True)
    assert "LOCAL to BOSS" not in calls()[-1]["prompt"]


def test_a_numbered_bold_finding_counts_as_having_read_something():
    from boss_engine.services import review_comments

    assert review_comments.parse_findings(
        "1. **FINDING: MAJOR a.py:8** the seccomp filter is wrong")


def test_a_clean_review_that_mentions_namespaces_stays_survived():
    said = ("REVIEWED: a.py\nThe change unshares the user namespace and installs a seccomp "
            "filter correctly.\nVERDICT: SURVIVED")
    assert review_input.hollow_reason(said, ["a.py"]) is None
