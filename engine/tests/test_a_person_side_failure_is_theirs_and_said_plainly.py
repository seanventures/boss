"""A failure in the person's own setup is theirs, said plainly; the work view tells the truth.

Stranger test r6 (2026-11-18), CSV Quality Desk on a plain-http custom model:
  * "The org secret sref_… belongs to custom and is sent only to r6-fakellm; refusing to send it
    to r6-fakellm …" was classed `infra`, auto-retried 14 times and filed as a BOSS self-fix;
  * the card read "The org secret a stored secret belongs to …" and "retried twice";
  * the drawer showed "[substrate=local_subprocess]", "Retry 14 … Retry 1" and "Build done,
    Review & test done" for a build that never built;
  * the Command Center kept "retry 11: the platform is holding this job for another 0 minutes:
    no reason recorded. Let it continue, or cancel it?" after that attempt had ended.
"""
from __future__ import annotations

import json
from uuid import uuid4

import pytest
from sqlalchemy import text

from boss_engine.identity.redaction import redact_text
from boss_engine.services import build_failure, system_doctor
from boss_engine.services.code_task_service import classify_pipeline_exception
from boss_engine.services.env_refs import Binding, CredentialMisdirected
from boss_engine.services.project_experience import work

from ._ambient_support import eng, project  # noqa: F401

R6 = ("The org secret sref_f45dfad071974218874b belongs to custom and is sent only to "
      "r6-fakellm; refusing to send it to r6-fakellm. A custom URL needs a credential stored for "
      "that integration (a project secret), or the operator lists the host in "
      "BOSS_CREDENTIAL_HOSTS. [substrate=local_subprocess]")


@pytest.mark.parametrize("reason,code", [
    (R6, "credential_misdirected"),
    ("Your model at http://r6-fakellm:8080 is plain http; BOSS only sends keys over https. "
     "Use an https address (Setup › Models).", "credential_plain_http"),
    ('Error code: 401 - {"error": {"type": "authentication_error", "message": "invalid x-api-key"}}',
     "model_auth_rejected"),
    ("remote: Permission to acme/web.git denied to bob.", "repo_access_denied"),
    ("fatal: repository 'https://github.com/acme/gone/' not found", "repo_access_denied"),
])
def test_a_refusal_of_what_the_person_connected_is_needs_human(reason, code):
    assert classify_pipeline_exception(reason) == ("needs_human", code)


@pytest.mark.parametrize("reason", [
    "git add failed: fatal: .git/index: index file open failed: Permission denied",
    "git -c failed: Cloning into '.'...\nerror: RPC failed; HTTP 502",
    # BOSS's OWN authentication (review of #227): its egress, its credential helper, its tokens.
    "git fetch failed: fatal: Authentication failed for "
    "'http://boss-egress:8090/git/github/acme/web.git/'",
    "git clone failed: fatal: could not read Username for 'https://github.com': "
    "No such device or address",
    "remote: Repository not found. fatal: repository "
    "'http://boss-egress:8090/git/github/acme/web.git/' not found",
    "egress refused: 401 Unauthorized handle_expired",
    "401 Unauthorized",
])
def test_boss_side_failures_stay_infra(reason):
    assert classify_pipeline_exception(reason)[0] == "infra"


def test_a_plain_http_model_is_said_as_that_with_no_userinfo():
    err = CredentialMisdirected("The org secret sref_abc", Binding("custom", ("r6-fakellm",)),
                                "r6-fakellm", "http://user:pw@r6-fakellm:8080/v1/chat")
    assert str(err) == ("Your model at http://r6-fakellm:8080 is plain http; BOSS only sends keys "
                        "over https. Use an https address (Setup › Models).")
    fix = build_failure.action_for("p", source="code_task", run_id="crun_x",
                                   failure_class="needs_human",
                                   reason_code="credential_plain_http", error=str(err))
    assert fix["fix"]["label"] == "Use an https address in Setup › Models"
    assert "focus=models" in fix["fix"]["href"] and "plain http" in fix["fix"]["detail"]


def test_a_secrets_id_is_replaced_without_doubling_the_words():
    said = redact_text("The org secret sref_f45dfad0719 belongs to custom.", public=True)
    assert said == "A stored secret belongs to custom."
    words = build_failure.failure_words(R6)
    assert "a stored secret belongs" in words.lower() and "secret a stored" not in words
    assert "substrate" not in words


def test_the_spent_retries_card_counts_the_real_tries_and_points_the_right_way():
    said = build_failure.boss_did("infra", 14)
    assert said.startswith("BOSS tried this build 15 times") and "updated" not in said
    assert build_failure.boss_did("needs_human", 2) is None


def test_a_build_that_never_built_shows_the_build_failed_and_nothing_after_it():
    strip = work._build_strip({"phase": "done", "status": "failed", "built": False}, "failed")
    states = {s["key"]: s["state"] for s in strip}
    assert states["build"] == "failed" and states["review"] == "todo" and states["pr"] == "todo"
    # One that built and then failed saving keeps the old reading (its PR station failed).
    strip = work._build_strip({"phase": "done", "status": "failed", "built": True}, "failed")
    assert {s["key"]: s["state"] for s in strip}["pr"] == "failed"


def test_automatic_retries_are_one_line():
    def a(i, auto):
        return ({"id": f"crun_{i}", "label": f"Retry {i}", "kind": "retry"},
                {"auto_retry": "1" if auto else None})

    pairs = [a(3, True), a(2, True), a(1, False), a(0, False)]   # newest first
    out = work._collapse_auto_retries(pairs)
    assert [x["label"] for x in out] == ["BOSS tried 3 times", "Retry 1", "Retry 0"]
    assert out[0]["kind"] == "auto_retry" and out[0]["auto_retries"] == 2


def test_a_question_about_a_build_that_ended_is_not_on_the_command_center(eng, project):  # noqa: F811
    ended, live = (f"crun_{uuid4().hex[:10]}" for _ in range(2))
    with eng.begin() as c:
        for rid, status in ((ended, "failed"), (live, "running")):
            c.execute(text("INSERT INTO code_task_runs (id, project_id, title, status, phase) "
                           "VALUES (:i, :p, 'CSV quality desk · retry 11', :s, 'coding')"),
                      {"i": rid, "p": project, "s": status})
            c.execute(text("INSERT INTO shepherd_escalations (id, project_id, task_ref, question, "
                           "options) VALUES (:e, :p, :r, :q, CAST(:o AS JSONB))"),
                      {"e": f"shesc_{uuid4().hex[:8]}", "p": project, "r": rid,
                       "q": "the platform is holding this job for another 0 minutes: no reason "
                            "recorded. Let it continue, or cancel it?",
                       "o": json.dumps(["continue", "cancel"])})
        cards = system_doctor.shepherd_escalation_cards(c, project)
    assert len(cards) == 1, cards
