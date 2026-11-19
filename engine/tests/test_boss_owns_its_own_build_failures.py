"""BOSS's own build failures are its agents' to fix, never a question to a person (2026-11-18).

THE CASE (laptop, recipe-box-fd3ab4a3). crun_6883ec7c failed on BOSS's own git-index permission
bug (failure_class `infra`). Its automatic retry was withheld, and the Chief of Staff put the
failure on a card asking the person "What kind of problem is 'Build failed: …'?" — a question
only BOSS can answer, about a problem only BOSS can fix.

THE RULE, AS A CLASS:
  * a failed build BOSS is already retrying (a newer attempt continues it) raises no signal and,
    if one was seen, is only observed: nobody is asked;
  * a failure only a person can fix (needs_human) raises no ambient signal: the build's own card
    names its fix (build_failure.action_for);
  * an `infra` failure is read as a BOSS self-fix from the build's own record, without a model;
  * no failure of BOSS's own (a build, a workflow step) is ever a classification question;
  * when a card does say it, it says what BOSS did, never "What kind of problem…?".
No test calls a model.
"""
from __future__ import annotations

import json
from types import SimpleNamespace
from uuid import uuid4

from sqlalchemy import text

from boss_engine.config import get_settings
from boss_engine.services import build_failure
from boss_engine.services.ambient import chief_of_staff as cos, signals

from ._ambient_support import eng, project  # noqa: F401


def _build(c, pid, *, status="failed", fctx=None, error="git add failed: fatal: .git/index: "
           "index file open failed: Permission denied"):
    rid = f"crun_{uuid4().hex[:10]}"
    c.execute(text("INSERT INTO code_task_runs (id, project_id, title, status, error, finalize_ctx) "
                   "VALUES (:i, :p, 'Recipe box', :s, :e, CAST(:f AS JSONB))"),
              {"i": rid, "p": pid, "s": status, "e": error, "f": json.dumps(fctx or {})})
    return rid


def _settings():
    base = get_settings()
    return SimpleNamespace(**{**base.__dict__, "ambient_cos_model_calls": 0})


def _no_calls():
    return cos.Budget(calls_left=0)


def _build_signals(c, pid):
    return [dict(r) for r in c.execute(text(
        "SELECT * FROM signals WHERE project_id = :p AND kind = 'build.failed'"),
        {"p": pid}).mappings()]


def test_a_build_boss_is_retrying_or_a_person_must_fix_raises_no_signal(eng, project):  # noqa: F811
    with eng.begin() as c:
        retried = _build(c, project, fctx={"failure_class": "infra"})
        _build(c, project, status="queued", fctx={"parent_run_id": retried,
                                                   "auto_infra_retry": 1})
        _build(c, project, fctx={"failure_class": "needs_human", "reason_code": "harness_unavailable"},
               error="claude: not logged in")
        spent = _build(c, project, fctx={"failure_class": "infra", "auto_infra_retry": 2})
        signals.sweep(c, project_id=project, only=["build_failures"])
        rows = _build_signals(c, project)
    assert [r["subject_id"] for r in rows] == [spent], "only the failure BOSS could not recover"


def test_an_infra_build_failure_is_read_as_a_boss_self_fix_without_asking(eng, project):  # noqa: F811
    with eng.begin() as c:
        rid = _build(c, project, fctx={"failure_class": "infra", "reason_code": "pipeline_exception"})
        sig = {"id": "sig_x", "kind": "build.failed", "source": "build", "subject_kind": "code_task",
               "subject_id": rid, "title": "Build failed: git add failed", "severity": "medium"}
        reading = cos.classify(c, _settings(), project, sig, budget=_no_calls(), bar=0.7)
    assert reading.task_class == "platform_self_fix" and reading.by == "rule"
    assert not reading.asked


def test_an_unclear_build_failure_is_still_never_a_question_for_a_person(eng, project):  # noqa: F811
    with eng.begin() as c:
        rid = _build(c, project, fctx={})          # an older row: no failure class recorded
        sig = {"id": "sig_y", "kind": "build.failed", "source": "build", "subject_kind": "code_task",
               "subject_id": rid, "title": "Build failed: something", "severity": "medium"}
        reading = cos.classify(c, _settings(), project, sig, budget=_no_calls(), bar=0.7)
    # Below the bar, the old rule asked "What kind of problem is this?"; BOSS's own failure is
    # its agents' to read, so its best reading stands.
    assert reading.confidence < 0.7 and not reading.asked


def test_a_signal_about_a_build_boss_is_retrying_is_observed_not_asked(eng, project):  # noqa: F811
    with eng.begin() as c:
        rid = _build(c, project, fctx={"failure_class": "infra"})
        signals.sweep(c, project_id=project, only=["build_failures"])
        # The retry starts after the sweep saw the failure (the race the triage closes).
        _build(c, project, status="queued", fctx={"parent_run_id": rid, "auto_infra_retry": 1})
        report = cos.triage_project(c, _settings(), project, mode="route", max_model_calls=0)
        cards = c.execute(text("SELECT question FROM decisions WHERE project_id = :p "
                               "AND status = 'pending'"), {"p": project}).scalars().all()
    assert report["counts"] == {"observed": 1}, report["counts"]
    assert cards == []


def test_an_unrecovered_infra_failure_is_handed_to_its_agent_with_no_card(eng, project):  # noqa: F811
    with eng.begin() as c:
        _build(c, project, fctx={"failure_class": "infra", "auto_infra_retry": 2})
        signals.sweep(c, project_id=project, only=["build_failures"])
        report = cos.triage_project(c, _settings(), project, mode="route", max_model_calls=0)
        cards = c.execute(text("SELECT question FROM decisions WHERE project_id = :p "
                               "AND status = 'pending'"), {"p": project}).scalars().all()
    assert report["counts"] == {"routed": 1}, report["counts"]
    assert not [q for q in cards if "What kind of problem" in q]


def test_a_self_fix_card_says_what_boss_is_doing_never_what_kind_of_problem():
    entry = {"task_class": "platform_self_fix", "asked": False, "owner": "agent:software_engineer",
             "owner_label": "Software Engineer", "title": "Build failed: git add failed",
             "words": "a BOSS fix for “Build failed: git add failed”"}
    line = cos.batch_question([entry])
    assert line.startswith("BOSS filed a fix for itself") and "What kind" not in line


def test_the_failed_build_card_says_what_boss_already_did():
    failure = {"id": "crun_r", "word": "Build failed", "label": "Recipe box",
               "reason": "Git refused this checkout's index.",
               "doing": build_failure.boss_did("infra", "2")}
    card = build_failure.needs_card(failure)
    assert card["detail"].startswith("BOSS retried this build twice")
    assert "?" not in card["detail"]
    assert build_failure.boss_did("needs_human", "2") is None
    assert build_failure.boss_did("infra", None) is None


def test_a_retry_boss_may_not_start_by_itself_asks_for_an_ok_not_a_fix():
    """A follow-up under a "this build only" grant (review of #222): BOSS may not retry it under
    that grant, so the card asks for one OK in words — never a red failure to fix."""
    words = build_failure.needs_ok_words("infra", "true", follow_up=True)
    assert words.startswith("BOSS needs your OK to retry this follow-up")
    assert build_failure.needs_ok_words("needs_human", "true") is None
    assert build_failure.needs_ok_words("infra", None) is None
    card = build_failure.needs_card({"id": "crun_f", "word": "Needs your OK", "label": "Recipe box",
                                     "reason": words, "tone": "info",
                                     "retry": {"run_id": "crun_f"}})
    assert card["tone"] == "info" and card["retry"] and card["label"].startswith("Needs your OK")
