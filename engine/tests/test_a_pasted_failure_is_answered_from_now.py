"""A failure the operator pastes is answered from what is true NOW (reported_failure.py).

Harbor was handed "Failed at Preflight checks: /opt/boss-apps is not writable by root on
192.0.2.202 …" and told the operator to run sudo — for a step two later deploys had already got
past. These pin the read that now opens the prompt, using that day's records.
"""
from __future__ import annotations

import inspect
import json

from boss_engine.services import intent_router
from boss_engine.services.reported_failure import (
    ASKED_AGAIN, asked_before, current_state_note, looks_like_a_pasted_failure,
    matching_targets, summarize_now,
)

PASTE = ("Getting this error during the deployment, can you fix it? Failed at Preflight checks: "
         "/opt/boss-apps is not writable by root on 192.0.2.202; prepare the host once: "
         "sudo mkdir -p /opt/boss-apps && sudo chown root /opt/boss-apps")
OLD_REASON = ("/opt/boss-apps is not writable by root on 192.0.2.202; prepare the host once: "
              "sudo mkdir -p /opt/boss-apps && sudo chown root /opt/boss-apps")

TARGET = {"id": "deptgt_h", "app_name": "HelpDesk", "environment": "staging",
          "target_ref": "root@192.0.2.202:22", "domains": None}


def _steps(*pairs):
    return json.dumps([{"label": label, "status": status} for label, status in pairs])


RUNS = [  # newest first, as the loader returns them
    {"id": "deprun_60553", "status": "succeeded", "created_at": "2026-10-12T13:45:59",
     "failure_reason": None, "release_packet": {"code_task_run_id": "crun_86902"},
     "steps": _steps(("Resolve source", "ok"), ("Preflight checks", "ok"),
                     ("Plan and build", "ok"), ("Health check", "ok"))},
    {"id": "deprun_8bdeb", "status": "succeeded", "created_at": "2026-10-12T11:21:53",
     "failure_reason": None, "release_packet": {"code_task_run_id": "crun_86902"},
     "steps": _steps(("Resolve source", "ok"), ("Preflight checks", "ok"))},
    {"id": "deprun_659e", "status": "blocked", "created_at": "2026-10-12T05:24:46",
     "failure_reason": OLD_REASON, "release_packet": {"code_task_run_id": "crun_86902"},
     "steps": _steps(("Resolve source", "ok"), ("Preflight checks", "failed"))},
]


def test_a_pasted_deploy_failure_is_recognised_and_a_question_is_not():
    assert looks_like_a_pasted_failure(PASTE)
    assert not looks_like_a_pasted_failure("Is HelpDesk healthy right now?")
    assert not looks_like_a_pasted_failure("the build failed its unit tests")  # no deploy/host


def test_the_paste_is_matched_to_the_target_on_the_host_it_names():
    other = {**TARGET, "id": "deptgt_x", "app_name": "Shop", "target_ref": "root@10.0.0.9:22"}
    third = {**TARGET, "id": "deptgt_y", "app_name": "Blog", "target_ref": "root@10.0.0.8:22"}
    assert [t["id"] for t in matching_targets(PASTE, [other, TARGET, third])] == ["deptgt_h"]


def test_a_stale_paste_is_called_stale_and_the_latest_attempt_leads():
    note = summarize_now(PASTE, [TARGET], {"deptgt_h": RUNS})
    assert note.startswith("WHAT IS TRUE NOW")
    assert "deprun_60553 — succeeded" in note
    assert "The error the operator pasted is from deprun_659e" in note
    assert "got past Preflight checks" in note
    # The build id is there so the agent can offer `redeploy`, a runnable button.
    assert "build crun_86902" in note and "`redeploy`" in note


def test_a_host_preparation_failure_says_boss_prepares_the_host():
    note = summarize_now(PASTE, [TARGET], {"deptgt_h": RUNS})
    assert "HOST PREPARATION IS BOSS'S JOB" in note
    assert "retrying the deploy, not by asking the operator to run sudo" in note


def test_a_paste_that_is_still_the_latest_failure_is_not_called_stale():
    note = summarize_now(PASTE, [TARGET], {"deptgt_h": list(reversed(RUNS[2:]))})
    assert "WHAT IS TRUE NOW" in note
    assert "is from deprun_659e" not in note


def test_no_target_no_block():
    assert summarize_now(PASTE, [], {}) is None
    assert summarize_now("Is HelpDesk healthy?", [TARGET], {"deptgt_h": RUNS}) is None


def test_a_read_that_fails_leaves_the_turn_alone():
    class Broken:
        def execute(self, *a, **k):
            raise RuntimeError("db down")
    assert current_state_note(Broken(), "p", PASTE) is None


def test_the_same_question_twice_is_noticed_through_whitespace_and_case():
    msgs = [{"role": "user", "content": PASTE},
            {"role": "assistant", "content": "Same blocker as before …"},
            {"role": "user", "content": "  " + PASTE.upper() + " "}]
    assert asked_before(msgs)
    assert not asked_before(msgs[:1])
    assert not asked_before([{"role": "user", "content": "a"}, {"role": "user", "content": "b"}])
    assert "do not repeat your earlier answer" in ASKED_AGAIN


def test_the_answer_path_reads_now_before_the_model_is_called():
    src = inspect.getsource(intent_router.IntentRouter._answer)
    read = src.index("current_state_note(connection, project_id, _question)")
    again = src.index("if asked_before(messages):")
    model = src.index("self.budget.reserve(")
    assert read < model and again < model, "the current state must be in the FIRST prompt"
