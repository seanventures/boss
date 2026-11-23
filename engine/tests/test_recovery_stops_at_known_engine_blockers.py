from datetime import datetime, timedelta, timezone

import pytest

from boss_engine.services import failure_owner, harness_failures, step_recovery
from boss_engine.services.codex_code_agent import CodexCodeAgent


def test_known_writer_block_stops_before_launch(monkeypatch):
    agent = object.__new__(CodexCodeAgent)
    monkeypatch.setattr(agent, "_resolve_binary", lambda: "codex")
    monkeypatch.setattr(agent, "_policy", lambda: {
        "state": "cannot_write", "managed": True, "sandbox_proven": False,
        "sandbox_error": "sandbox transition refused",
    })
    with pytest.raises(harness_failures.HarnessFailure, match="engine_write_unavailable") as refused:
        agent._codex_argv(read_only=False, model=None, resume=None, final_path="/tmp/result")
    # BOSS re-checks the engine itself before refusing (dev autonomy, BOSSAPP-1441).
    assert "BOSS re-checked Codex itself" in refused.value.verdict.fix
    with pytest.raises(harness_failures.HarnessFailure):
        agent._codex_argv(read_only=True, by_copy=True, model=None,
                          resume="session", final_path="/tmp/result")
    argv = agent._codex_argv(read_only=True, model=None, resume=None, final_path="/tmp/result")
    assert argv[argv.index("--sandbox") + 1] == "read-only"


def test_known_host_block_is_not_an_app_retry_even_when_classified_infra():
    kind, cause, action = failure_owner.classify("engine_write_unavailable", failure_class="infra")
    assert kind == failure_owner.PERSON
    assert cause == "engine_write_unavailable"
    assert "host administrator" in action
    assert "Re-check" in action and "Setup › Models" in action


def test_invalid_recovery_response_does_not_claim_model_is_disconnected():
    decision = step_recovery.no_model_ask(
        {"headline": "The app branch is missing"}, {"action": "retry"},
        refused="not the JSON object asked for")
    assert decision["action"] == step_recovery.ASK_PERSON
    question = decision["question"]
    assert question.startswith("The app branch is missing.")
    assert "changed nothing" in question
    assert "press Retry on this step" in question and "Setup › Models" in question
    assert "Connect a model" not in question
    assert "Retry" in decision["after"]


def test_new_failure_ends_the_person_retry_grace_period():
    now = datetime.now(timezone.utc)
    approved = now - timedelta(minutes=2)
    row = {"owned_by": "person", "state": failure_owner.FIXING, "updated_at": approved}
    item = failure_owner.Item("workflow_run", "run", "run", "project", "Repair", "failed",
                              now - timedelta(minutes=1))
    assert not failure_owner._person_retry_pending(row, item, now)
    item.failed_at = approved - timedelta(minutes=1)
    assert failure_owner._person_retry_pending(row, item, now)
    assert not failure_owner._person_retry_pending(row, item, now + timedelta(hours=1))


def test_a_refused_answer_still_asks_boss_own_question_when_it_has_one():
    triage = {"action": step_recovery.ASK_PERSON, "question": "Which port should the app use?",
              "cause": "port clash"}
    decision = step_recovery.no_model_ask({"headline": "Port 3000 is taken"}, triage,
                                          refused="not the JSON object asked for")
    assert decision["question"] == "Which port should the app use?"
    assert "answer was refused" in decision["reasoning"]
