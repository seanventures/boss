"""A build has ONE state, and every surface reads it (factory spec §4.4, §8.1).

Codex cloud's list said Failed while its detail said Working and its CLI said PENDING. The
Factory's Floor, its cockpit, Agents › Live and MCP get_code_task all read
factory_view.build_state, so a build says one word everywhere. These cases pin each row of the
spec's table and its precedence, from fields a real run carries — nothing guessed.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from boss_engine.services import factory_view as fv

NOW = datetime(2026, 10, 4, 12, 0, tzinfo=timezone.utc)


def run(status="running", phase="implement", *, fctx=None, state=None, minutes_ago=1, **kw):
    return {"id": "crun_x", "project_id": "p", "status": status, "phase": phase,
            "title": "Invoice export", "spec": {}, "state": state or {},
            "finalize_ctx": fctx or {}, "error": kw.pop("error", None),
            "created_at": (NOW - timedelta(minutes=60)).isoformat(),
            "updated_at": (NOW - timedelta(minutes=minutes_ago)).isoformat(), **kw}


def act(step, status="active", *, minutes_ago=2, ended=False, role="software_engineer", seat=None):
    start = NOW - timedelta(minutes=minutes_ago)
    return {"act_id": f"act_{step}_{seat or 0}_{minutes_ago}", "kind": "stage", "step_id": step,
            "status": status, "role_key": role, "seat": seat, "how": "next_stage",
            "started_at": start, "ended_at": start + timedelta(minutes=1) if ended else None,
            "label": step, "handed_by_kind": "boss"}


def state(r, **kw):
    return fv.build_state(r, now=NOW, **kw)


def test_queued_and_starting():
    assert state(run(phase="queued"))["word"] == "Queued"
    s = state(run(phase="provisioning"))
    assert (s["state"], s["word"], s["tip"]) == ("queued", "Starting", "Cloning the repo")


def test_building_says_the_open_acts_verb():
    s = state(run(), acts=[act("plan", "complete", ended=True, minutes_ago=9,
                               role="chief_architect"),
                           act("implement")])
    assert (s["state"], s["word"], s["station"]) == ("building", "Coding", "build")


def test_a_repair_round_is_said():
    acts = [act("implement", "complete", ended=True, minutes_ago=9),
            act("review_code", "complete", ended=True, minutes_ago=7, role="adversarial_reviewer"),
            act("repair", minutes_ago=2)]
    s = state(run(phase="convergence"), acts=acts)
    assert s["rounds"] == 1
    assert s["word"] == "Coding · round 2"


def test_quiet_after_twenty_minutes_and_not_before():
    assert state(run(minutes_ago=19), acts=[act("implement", minutes_ago=19)])["state"] == "building"
    s = state(run(minutes_ago=21), acts=[act("implement", minutes_ago=21)])
    assert s["state"] == "quiet"
    assert s["word"] == "Quiet · 21m"
    assert s["tip"] == "No progress for 21m. It may be stuck."


def test_budget_wait_wins_over_quiet():
    s = state(run(minutes_ago=40, fctx={"budget_wait_since": NOW.isoformat()}))
    assert (s["state"], s["word"]) == ("budget_wait", "Waiting · budget")


def test_needs_plan_waiver_and_budget_refused():
    assert state(run("blocked", "awaiting_plan_approval"))["word"] == "Needs you · plan"
    s = state(run("blocked", "awaiting_gate_override",
                  fctx={"failing_gates": ["code_review_passed", "security_clean"]}))
    assert (s["state"], s["word"]) == ("needs_waiver", "Checks failed")
    assert s["tip"] == "Review passed · Security clean"
    s = state(run("blocked", "done", fctx={"reason_code": "budget_denied"}),
              acts=[act("plan", "complete", ended=True, role="chief_architect")])
    assert (s["state"], s["word"], s["station"]) == ("budget_refused", "Budget refused", "tests")
    assert state(run("blocked", "implement", error="repo gone\nmore"))["tip"] == "repo gone"


def test_failed_and_cancelled():
    s = state(run("failed", "review_code", fctx={"failure_class": "recoverable"}))
    assert (s["word"], s["tip"]) == ("Failed · Review", "Failed · retry may fix")
    s = fv.build_state(run("cancelled", "implement", fctx={"cancelled_by": "ceo"}), now=NOW,
                       viewer="ceo")
    assert s["word"] == "Stopped by you"
    s = state(run("cancelled", "implement", fctx={"cancelled_by": "ceo"}))
    assert s["word"] == "Stopped by the owner"     # a name, never the actor id


def test_a_cancelled_run_with_a_pr_is_cancelled():
    s = state(run("cancelled", "done", pull_request_url="https://x/pull/3", pull_request_number=3))
    assert s["state"] == "cancelled"


@pytest.mark.parametrize("dstatus,expected", [("pending", "pr_open"), ("approved", "merged"),
                                              ("executed", "merged")])
def test_pr_open_then_merged(dstatus, expected):
    r = run("completed", "done", pull_request_url="https://x/pull/842", pull_request_number=842)
    decision = {"id": "d", "status": dstatus, "recommendation": {"action": "review_pull_request"},
                "deadline_at": None, "default_action": "hold", "risk_tier": 1}
    assert state(r, decision=decision)["state"] == expected


def test_deployed_and_done_no_pr():
    r = run("completed", "done", pull_request_url="https://x/pull/842")
    s = state(r, deploys=[{"status": "healthy", "environment": "dev"}])
    assert (s["state"], s["word"]) == ("deployed", "Deployed · dev")
    s = state(run("completed", "done", fctx={"decomposition_index": 1,
                                             "decomposition_total": 3}))
    assert (s["state"], s["word"], s["tip"]) == ("done_no_pr", "Done · no PR", "Part 1 of 3")


def test_preview_is_a_flag_never_a_state():
    s = state(run("completed", "done"), preview={"status": "running", "url": "http://h/",
                                                 "expires_at": NOW + timedelta(hours=1)})
    assert s["state"] == "done_no_pr" and s["preview"] == "running"
    gone = state(run(), preview={"status": "running", "expires_at": NOW - timedelta(minutes=1)})
    assert gone["preview"] is None


def test_every_state_word_is_short():
    """Labels of three words or fewer (studio spec §4): the state word is a label."""
    samples = [state(run(phase="queued")), state(run(minutes_ago=30)),
               state(run("blocked", "awaiting_gate_override")),
               state(run("failed", "implement")), state(run("completed", "done"))]
    for s in samples:
        assert len(s["word"].replace("·", " ").split()) <= 3, s["word"]


def test_a_job_the_budget_holds_is_waiting_on_budget_not_a_worker():
    """Factory test D3: the budget governor holds a job by queueing it with a later run_after
    and `last_error.kind = budget_hold`. That read "Queued · Waiting for a worker" for 12 min."""
    held = {"status": "queued", "run_after": NOW + timedelta(minutes=14),
            "last_error": {"kind": "budget_hold", "reason": "red_halts_non_p0_work"}}
    s = state(run(phase="queued"), job=held)
    assert (s["state"], s["word"]) == ("budget_wait", "Waiting · budget")
    # Released (run_after passed) or never held: the ordinary words.
    assert state(run(phase="queued"), job={**held, "run_after": NOW - timedelta(seconds=1)})[
        "word"] == "Queued"
    assert state(run(phase="queued"), job={"status": "queued", "last_error": {}})[
        "word"] == "Queued"


def test_a_preview_is_live_only_when_it_has_finished_booting():
    """D10: a preview row says `running` while its steps still boot the app."""
    booting = {"status": "running", "url": "http://x", "steps": [
        {"name": "install", "status": "ok"}, {"name": "start", "status": "running"}]}
    assert fv.preview_flag(booting, NOW) == {"status": "starting", "url": None}
    up = {**booting, "steps": [{"name": "install", "status": "ok"}, {"name": "start", "status": "ok"}]}
    assert fv.preview_flag(up, NOW) == {"status": "running", "url": "http://x"}


def test_a_passed_over_fallback_does_not_hold_a_running_preview_at_starting():
    """Tester P1-4: `auto` records a substrate that could not serve as `failed`, then the next one
    serves. The preview runs (health 200) and must read as running with its link, not
    "Starting… 1 of 3" forever."""
    java = {"status": "running", "url": "http://x", "steps": [
        {"step": "start_rootfs_preview", "status": "failed"},
        {"step": "start_container_preview", "status": "failed"},
        {"step": "start_preview", "status": "ok"}]}
    assert fv.preview_flag(java, NOW) == {"status": "running", "url": "http://x"}
    still = {**java, "steps": [*java["steps"][:2], {"step": "start_preview", "status": "running"}]}
    assert fv.preview_flag(still, NOW) == {"status": "starting", "url": None}


def test_counts_in_words_are_singular_for_one():
    """`Not ready · 1 check failed`, `1 of 1 command passed`: never `1 checks`."""
    assert fv._plural(1, "check failed", "checks failed") == "1 check failed"
    assert fv._plural(2, "check failed", "checks failed") == "2 checks failed"


@pytest.mark.parametrize("title,name", [
    ("Lead Triage Inbox.", "Lead Triage Inbox"),
    ("“Invoice export”!", "Invoice export"),
    ("Team inbox", "Team inbox"),
])
def test_an_app_name_is_a_name_not_a_sentence(title, name):
    """boss-1 read "Lead Triage Inbox." — the brief's sentence, period and all."""
    assert fv._a_name(title) == name


def test_one_count_rule_for_every_surface():
    from boss_engine.services.command_center import attention_counts

    items = [{"kind": "approval"}, {"kind": "failed_step"}, {"kind": "failed_step"},
             {"kind": "gate_override"}, {"kind": "readiness"}]
    assert attention_counts(items) == {"needs_you": 1, "stopped": 3, "setup": 1}


def test_a_builds_app_name_drops_the_briefs_period():
    name = fv.app_name({"id": "crun_x", "title": "Build a lead triage inbox. Lead Triage Inbox.",
                        "finalize_ctx": {}, "spec": {}})
    assert not name.endswith((".", "!", "?", ",", ";", ":")), name


@pytest.mark.parametrize("title", [
    "Re‐build of *Path Verification* (paramg/boss-sandbox) — make the checks pass",
    "Go/no-go on a US-equity rule-based screener for weekly rebalancing",
])
def test_an_app_name_never_ends_mid_word(title):
    """Apps on the shared server: "Re‐build of *Path Verification* (paramg/bo", "Go/no-go on a US-equity
    ru" — cut mid-word at 42 characters, with the brief's markdown in it."""
    name = fv.app_name({"id": "crun_x", "title": title, "finalize_ctx": {}, "spec": {}})
    assert "*" not in name and not name.endswith(("(", "/", "-"))
    words = title.replace("*", "").split()
    assert all(w in words or w.rstrip(",:;") in [x.rstrip(",:;") for x in words]
               for w in name.split()), name
