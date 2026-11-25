"""WHAT #264 DID LIVE, AND WHAT IT MUST NOT (northgate-gateway, 2026-11-21 16:55 UTC).

1. CHANGE #9 WAS ORPHANED. Its dead merge job was held by the person's merge card; the loop's
   stale-card withdrawal closed THAT card (not one of its own) before any build started, and
   the next tick read "its merge question was withdrawn" as resolved. No builder, no card.
   Now: the loop withdraws only its own cards; a merge card is withdrawn only once a builder run
   exists and is linked (`merge_failed.handed_to`); a withdrawn conflict with no builder on a
   change still open is picked up again (handed off, or its card reopened with the steps).
2. THE HELD PROOF WAS NEVER SETTLED: "The call's job was taken by another worker." — the proof's
   job was DEAD-LETTERED (held by nobody), which the late-settle refused as if taken. A job that
   ended is settled; a person's card is retried (counted) instead of standing for good.
"""
from __future__ import annotations

import json
from uuid import uuid4

import pytest
from sqlalchemy import text

from boss_engine.services import failure_owner as fo, merge_failures

from ._ambient_support import eng, make_project, purge  # noqa: F401
from .test_agents_fix_failures_with_the_starters_authority import (  # noqa: F401
    _TABLES, _ago, _local_change_9, _settings)


@pytest.fixture()
def project(eng):  # noqa: F811
    pid = make_project(eng)
    yield pid
    with eng.begin() as c:
        for table in _TABLES:
            try:
                with c.begin_nested():
                    c.execute(text(f"DELETE FROM {table} WHERE project_id = :p"), {"p": pid})
            except Exception:  # noqa: BLE001
                pass
    purge(eng, pid)


def _held_by_its_merge_card(c, pid, url, card):
    """The owner row exactly as it was at 16:55: needs_person, held by the merge card."""
    key = f"github.pull_request.merge:{url}"
    item = fo.Item("job", key, "x", pid, "Merge", "", _ago(minutes=5), {})
    fo._write(c, item, state=fo.NEEDS_PERSON, words="Its merge card says why the merge failed",
              by="merge_failures", reason="A merge runs only on your approval.", decision_id=card)
    c.execute(text("UPDATE failure_owners SET item_id = (SELECT d.id FROM deadletters d JOIN jobs "
                   "j ON j.id = d.source_id WHERE j.project_id = :p AND j.kind = "
                   "'github.pull_request.merge' LIMIT 1) WHERE item_key = :k"), {"p": pid, "k": key})
    return key


def _local_change_set(c, pid, run):
    c.execute(text("INSERT INTO local_change_sets (id, project_id, repository, number, branch, "
                   "base_branch, run_id, state) VALUES (:i, :p, :r, 9, :b, 'main', :run, 'open')"),
              {"i": f"lcs_{uuid4().hex[:12]}", "p": pid, "r": f"local_{pid}/{pid}",
               "b": "boss/code/x", "run": run})


def _orphaned(c, pid, card, *, by=merge_failures.ORPHAN_ACTOR, why=merge_failures.ORPHAN_WHY):
    """Withdrawn as the owner loop's old stale-card cleanup did it on #9 (its event and why)."""
    from boss_engine.services.core import emit_event

    c.execute(text("UPDATE decisions SET status = 'withdrawn' WHERE id = :d"), {"d": card})
    emit_event(c, pid, "decision.withdrawn", by, "decision", card, {"why": why, "key": "k"})


def _status(eng, card):  # noqa: F811
    with eng.begin() as c:
        return c.execute(text("SELECT status, recommendation FROM decisions WHERE id = :d"),
                         {"d": card}).one()


def test_the_merge_card_is_never_withdrawn_when_no_builder_started(eng, project, monkeypatch):  # noqa: F811
    monkeypatch.setattr(merge_failures, "hand_to_builder", lambda *a, **k: None)
    with eng.begin() as c:
        url, _runs, card, _reason = _local_change_9(c, project)
        _held_by_its_merge_card(c, project, url, card)
    for _ in range(3):
        fo.sweep(eng, _settings(), project_id=project)
    status, _rec = _status(eng, card)
    assert status == "pending", "no builder, so the person's card stays"


def test_the_conflict_is_handed_over_and_only_then_the_card_withdrawn_and_linked(
        eng, project, monkeypatch):  # noqa: F811
    made = []

    def builder(connection, settings, run_id, note, **_k):
        new = f"crun_{uuid4().hex[:20]}"
        with connection.engine.begin() as c2:
            c2.execute(text("INSERT INTO code_task_runs (id, project_id, title, status, "
                            "finalize_ctx) VALUES (:i, :p, 'fix', 'queued', CAST(:f AS JSONB))"),
                       {"i": new, "p": project, "f": json.dumps({"root_run_id": run_id})})
        made.append((run_id, new, note))
        return new

    monkeypatch.setattr(merge_failures, "hand_to_builder", builder)
    with eng.begin() as c:
        url, runs, card, _reason = _local_change_9(c, project)
        key = _held_by_its_merge_card(c, project, url, card)
    fo.sweep(eng, _settings(), project_id=project)
    fo.sweep(eng, _settings(), project_id=project)
    status, rec = _status(eng, card)
    assert len(made) == 1 and made[0][0] == runs[-1] and "keeping BOTH" in made[0][2]
    assert status == "withdrawn" and rec["merge_failed"]["handed_to"] == made[0][1]
    with eng.begin() as c:
        state = c.execute(text("SELECT state FROM failure_owners WHERE item_key = :k"),
                          {"k": key}).scalar()
    assert state == "resolved", "settled only because a linked builder exists"


def test_an_orphaned_conflict_is_picked_up_again(eng, project, monkeypatch):  # noqa: F811
    """The live #9 state after 16:55: card withdrawn, no handed_to, dead letter closed, change
    set still open — the sweep hands it to the builder."""
    made = []
    monkeypatch.setattr(merge_failures, "hand_to_builder",
                        lambda conn, s, run_id, note, **_k: made.append(run_id) or "crun_recovered")
    with eng.begin() as c:
        url, runs, card, _reason = _local_change_9(c, project)
        _local_change_set(c, project, runs[-1])
        _orphaned(c, project, card)
        c.execute(text("UPDATE deadletters SET status = 'resolved' WHERE project_id = :p"),
                  {"p": project})
    report = fo.sweep(eng, _settings(), project_id=project)
    status, rec = _status(eng, card)
    assert made == [runs[-1]] and report["orphaned_conflicts"][0]["handed_to"] == "crun_recovered"
    assert rec["merge_failed"]["handed_to"] == "crun_recovered"
    fo.sweep(eng, _settings(), project_id=project)
    assert made == [runs[-1]], "linked once, never handed off twice"


def test_an_orphan_the_builder_cannot_take_is_reopened_for_the_person_with_steps(
        eng, project, monkeypatch):  # noqa: F811
    monkeypatch.setattr(merge_failures, "hand_to_builder", lambda *a, **k: None)
    with eng.begin() as c:
        url, runs, card, _reason = _local_change_9(c, project)
        _local_change_set(c, project, runs[-1])
        _orphaned(c, project, card)
    fo.sweep(eng, _settings(), project_id=project)
    status, rec = _status(eng, card)
    assert status == "pending" and "keeping both sides" in rec["merge_failed"]["next"]


def test_a_merged_change_is_not_an_orphan(eng, project, monkeypatch):  # noqa: F811
    monkeypatch.setattr(merge_failures, "hand_to_builder",
                        lambda *a, **k: pytest.fail("a merged change is never handed off"))
    with eng.begin() as c:
        url, runs, card, _reason = _local_change_9(c, project)
        _local_change_set(c, project, runs[-1])
        c.execute(text("UPDATE local_change_sets SET state = 'merged' WHERE project_id = :p"),
                  {"p": project})
        _orphaned(c, project, card)
    fo.sweep(eng, _settings(), project_id=project)
    assert _status(eng, card)[0] == "withdrawn"


@pytest.mark.parametrize("who, why", [
    ("user:ceo", "the person cancelled it"),                     # a person / withdraw_with_run
    ("boss", "its pull request was settled"),                    # cleared_causes, stale builds
])
def test_a_card_withdrawn_on_purpose_is_never_revived(eng, project, monkeypatch, who, why):  # noqa: F811
    monkeypatch.setattr(merge_failures, "hand_to_builder",
                        lambda *a, **k: pytest.fail("withdrawn on purpose: never revived"))
    with eng.begin() as c:
        url, runs, card, _reason = _local_change_9(c, project)
        _local_change_set(c, project, runs[-1])
        _orphaned(c, project, card, by=who, why=why)
    fo.sweep(eng, _settings(), project_id=project)
    assert _status(eng, card)[0] == "withdrawn"


def test_a_cancelled_lineage_is_never_revived(eng, project, monkeypatch):  # noqa: F811
    monkeypatch.setattr(merge_failures, "hand_to_builder",
                        lambda *a, **k: pytest.fail("cancelled: never revived"))
    with eng.begin() as c:
        url, runs, card, _reason = _local_change_9(c, project)
        _local_change_set(c, project, runs[-1])
        c.execute(text("UPDATE code_task_runs SET status = 'cancelled' WHERE id = :r"),
                  {"r": runs[-1]})
        _orphaned(c, project, card)
    fo.sweep(eng, _settings(), project_id=project)
    assert _status(eng, card)[0] == "withdrawn"


@pytest.mark.parametrize("state, is_open", [("open", True), ("closed", False),
                                            ("merged", False), (None, False)])
def test_a_github_pr_closed_without_merging_is_not_open(eng, project, monkeypatch, state,  # noqa: F811
                                                        is_open):
    monkeypatch.setattr(merge_failures, "PR_STATE_READER",
                        lambda c, pid, repo, n, settings: state)
    with eng.begin() as c:
        assert merge_failures.change_open(
            c, project, "https://github.com/acme/web/pull/12") is is_open


def test_two_concurrent_sweeps_hand_an_orphan_off_once_and_never_reopen_it(
        eng, project, monkeypatch):  # noqa: F811
    import threading
    import time

    calls = []

    def slow_builder(conn, settings, run_id, note, **_k):
        calls.append(run_id)
        time.sleep(1.5)
        return "crun_only_one"

    monkeypatch.setattr(merge_failures, "hand_to_builder", slow_builder)
    with eng.begin() as c:
        url, runs, card, _reason = _local_change_9(c, project)
        _local_change_set(c, project, runs[-1])
        _orphaned(c, project, card)
    out = []
    threads = [threading.Thread(target=lambda: out.append(merge_failures.recover_orphaned_conflicts(
        eng, _settings(), project_id=project))) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    status, rec = _status(eng, card)
    assert calls == [runs[-1]], "one hand-off, whoever sweeps"
    assert status == "withdrawn" and rec["merge_failed"]["handed_to"] == "crun_only_one"


def test_a_later_attempt_is_linked_only_when_it_was_told_the_conflict(eng, project, monkeypatch):  # noqa: F811
    handed = []
    monkeypatch.setattr(merge_failures, "hand_to_builder",
                        lambda conn, s, run_id, note, **_k: handed.append(run_id) or "crun_new")
    with eng.begin() as c:
        url, runs, card, _reason = _local_change_9(c, project)
        _local_change_set(c, project, runs[-1])
        c.execute(text("UPDATE code_task_runs SET finalize_ctx = jsonb_build_object("
                       "'root_run_id', :root) WHERE id = ANY(:ids) AND id <> :root"),
                  {"root": runs[0], "ids": runs})
        unrelated = f"crun_{uuid4().hex[:20]}"
        c.execute(text("INSERT INTO code_task_runs (id, project_id, title, status, finalize_ctx) "
                       "VALUES (:i, :p, 'more tests', 'completed', CAST(:f AS JSONB))"),
                  {"i": unrelated, "p": project, "f": json.dumps(
                      {"root_run_id": runs[0], "feedback_history": [{"fix_note": "add tests"}]})})
        _orphaned(c, project, card)
    fo.sweep(eng, _settings(), project_id=project)
    assert handed == [runs[-1]], "an unrelated later attempt is not the hand-off"
    assert _status(eng, card)[1]["merge_failed"]["handed_to"] == "crun_new"


def test_another_owner_s_question_is_closed_once_the_remedy_started_never_an_approval(
        eng, project):  # noqa: F811
    from boss_engine.services.core import create_blocking_decision

    with eng.begin() as c:
        ask = create_blocking_decision(c, project, None, "Re-authorize the run?",
                                       {"action": "step_recovery_ask", "agent": "Pilot"}, {},
                                       "needs_human", default_action="hold")
        deploy = create_blocking_decision(c, project, None, "Deploy to production?",
                                          {"action": "deploy_stack"}, {}, "deploy",
                                          default_action="hold")
        item = fo.Item("workflow_run", "wfr_x", "wfr_x", project, "t", "", None, {})
        fo._close_other_owners_card(c, item, ask, "running its 'deploy' step again")
        fo._close_other_owners_card(c, item, deploy, "running it again")
        got = dict(c.execute(text("SELECT id, status FROM decisions WHERE id = ANY(:i)"),
                             {"i": [ask, deploy]}).all())
    assert got[ask] == "withdrawn" and got[deploy] == "pending"


def _due_now(eng, card):  # noqa: F811
    with eng.begin() as c:
        c.execute(text("UPDATE decisions SET recommendation = jsonb_set(recommendation, "
                       "'{merge_failed,handoff_refused,next_at}', to_jsonb(CAST(:t AS text))) "
                       "WHERE id = :d"), {"t": _ago(minutes=1).isoformat(), "d": card})


def test_a_refused_hand_off_is_retried_by_boss_and_the_card_withdrawn_once_linked(
        eng, project, monkeypatch):  # noqa: F811
    """#9 after #272's install: BOSS's own hand-off was refused (the restart guard), the card was
    reopened — and must not wait for a person. The card says it was BOSS's refusal; the next due
    sweep tries again itself and withdraws the card only once a builder is linked."""
    refuse = {"on": True}
    made = []

    def builder(conn, settings, run_id, note, why=None):
        if refuse["on"]:
            why.append("it failed before BOSS last started")
            return None
        made.append(run_id)
        return "crun_after_fix"

    monkeypatch.setattr(merge_failures, "hand_to_builder", builder)
    with eng.begin() as c:
        url, runs, card, _reason = _local_change_9(c, project)
        _local_change_set(c, project, runs[-1])
        _orphaned(c, project, card)
    fo.sweep(eng, _settings(), project_id=project)
    status, rec = _status(eng, card)
    marker = rec["merge_failed"]["handoff_refused"]
    assert status == "pending" and marker["attempts"] == 1 and marker["next_at"]
    assert marker["why"] == "it failed before BOSS last started", "this call's own reason"
    fo.sweep(eng, _settings(), project_id=project)                     # not due yet: untouched
    assert made == [] and _status(eng, card)[0] == "pending"
    refuse["on"] = False                                                # the guard is fixed
    _due_now(eng, card)
    fo.sweep(eng, _settings(), project_id=project)
    status, rec = _status(eng, card)
    assert made == [runs[-1]] and status == "withdrawn"
    assert rec["merge_failed"]["handed_to"] == "crun_after_fix"


def test_a_refused_hand_off_is_retried_a_bounded_number_of_times(eng, project, monkeypatch):  # noqa: F811
    tries = []
    monkeypatch.setattr(merge_failures, "hand_to_builder",
                        lambda *a, **k: tries.append(1) or None)
    with eng.begin() as c:
        url, runs, card, _reason = _local_change_9(c, project)
        _local_change_set(c, project, runs[-1])
        _orphaned(c, project, card)
    fo.sweep(eng, _settings(), project_id=project)
    for _ in range(5):
        _due_now(eng, card)
        fo.sweep(eng, _settings(), project_id=project)
    assert len(tries) == 1 + merge_failures.HANDOFF_RETRIES, "then it is the person's"
    assert _status(eng, card)[0] == "pending"


def test_a_card_reopened_before_the_marker_existed_is_retried_and_a_plain_card_never(
        eng, project, monkeypatch):  # noqa: F811
    from boss_engine.services.core import emit_event

    made = []
    monkeypatch.setattr(merge_failures, "hand_to_builder",
                        lambda conn, s, run_id, note, **_k: made.append(run_id) or "crun_x")
    with eng.begin() as c:
        url, runs, card, _reason = _local_change_9(c, project)
        _local_change_set(c, project, runs[-1])
        emit_event(c, project, "merge.conflict_orphan_recovered", "boss", "decision", card,
                   {"handed_to": None, "reopened": True})
    fo.sweep(eng, _settings(), project_id=project)
    assert made == [runs[-1]] and _status(eng, card)[0] == "withdrawn"


def test_a_pending_conflict_card_with_no_refusal_marker_is_never_touched(
        eng, project, monkeypatch):  # noqa: F811
    from boss_engine.services import project_settings

    monkeypatch.setattr(merge_failures, "hand_to_builder",
                        lambda *a, **k: pytest.fail("a person's card: never touched"))
    with eng.begin() as c:
        project_settings.set_one(c, project, "agents_fix_failures", 0)   # no Jobs-path remedy
        _local_change_9(c, project)
    fo.sweep(eng, _settings(), project_id=project)
    assert merge_failures.retry_refused_handoffs(eng, _settings(), project_id=project) == []


def test_a_stored_github_sentence_is_said_in_local_words_at_display_time():
    """#9's card stored "PR #9 conflicts with its base branch, so GitHub would not merge it"
    before #264; it is rendered from the repository's kind, whatever was stored."""
    url = "http://boss-local/local_northgate-gateway/northgate-gateway/pull/9"
    stored = "PR #9 conflicts with its base branch, so GitHub would not merge it"
    rec = {"pull_request_url": url, "summary": f"Merge failed: {stored}. Retry the build.",
           "merge_failed": {"kind": "not_mergeable", "reason": stored}}
    shown = merge_failures.for_display(rec)
    for said in (shown["summary"], shown["merge_failed"]["reason"],
                 merge_failures.local_words(f"Merge failed: {stored}.", url)):
        assert "GitHub" not in said and "PR #" not in said and "change #9" in said.lower()
    gh = {**rec, "pull_request_url": "https://github.com/acme/web/pull/9"}
    assert merge_failures.for_display(gh)["merge_failed"]["reason"] == stored, "GitHub stays"


@pytest.mark.parametrize("stored", [
    "PR #9 conflicts with its base branch, so GitHub would not merge it",
    {"kind": "not_mergeable",
     "reason": "PR #9 conflicts with its base branch, so GitHub would not merge it"},
])
def test_the_work_view_says_a_local_merge_failure_in_local_words(stored):
    from boss_engine.services.project_experience import work

    url = "http://boss-local/local_northgate-gateway/northgate-gateway/pull/9"
    out = work._merge_links([], {"id": "crun_x", "pull_request_url": url, "pull_request_number": 9,
                                 "merge_failed": stored})
    failed = out["merge_pr"]["failed"]
    said = failed if isinstance(failed, str) else failed["reason"]
    assert "GitHub" not in said and "change #9" in said.lower()
    gh = work._merge_links([], {"id": "crun_y", "pull_request_url": "https://github.com/acme/web/pull/9",
                                "merge_failed": stored})
    assert gh["merge_pr"]["failed"] == stored, "GitHub wording stays for GitHub"


def test_the_loop_never_withdraws_a_card_it_did_not_create(eng, project):  # noqa: F811
    from boss_engine.services.core import create_blocking_decision

    with eng.begin() as c:
        other = create_blocking_decision(c, project, None, "Merge it?",
                                         {"action": "review_pull_request"}, {},
                                         "merge_decision_required", default_action="hold")
        item = fo.Item("job", "k", "x", project, "t", "", None, {})
        fo._withdraw_own_card(c, item, other, "x")
        assert c.execute(text("SELECT status FROM decisions WHERE id = :d"),
                         {"d": other}).scalar() == "pending"


# ── 2. the held proof whose job ended ────────────────────────────────────────────────────────

class _Rows:
    def __init__(self, row):
        self.row = row

    def mappings(self):
        return self

    def first(self):
        return self.row


class _Conn:
    def __init__(self, job):
        self.job = job

    def execute(self, stmt, params=None):
        sql = str(stmt)
        if "FROM model_gateway_invocations" in sql:
            return _Rows({"id": "minv", "status": "in_progress", "model_gateway_lease_id": "l",
                          "job_id": "job_proof", "receipt_id": "rcpt"})
        if "FROM model_gateway_leases" in sql:
            return _Rows({"id": "l", "status": "active", "worker_id": "setup:usr_x:abc"})
        return _Rows(self.job)


@pytest.mark.parametrize("job, settles", [
    ({"id": "job_proof", "status": "deadlettered", "lease_owner": None}, True),
    ({"id": "job_proof", "status": "failed_final", "lease_owner": None}, True),
    ({"id": "job_proof", "status": "running", "lease_owner": "setup:usr_x:abc"}, True),
    ({"id": "job_proof", "status": "running", "lease_owner": "worker-2"}, False),
    ({"id": "job_proof", "status": "queued", "lease_owner": None}, False),
])
def test_a_proof_whose_job_ended_is_settled_not_called_taken(monkeypatch, job, settles):
    from boss_engine.services import model_gateway

    gw = model_gateway.ModelGatewayService(_settings())
    monkeypatch.setattr(gw, "_record_settlement", lambda *a, **k: {"settled": True})
    monkeypatch.setattr(gw.jobs, "close_abandoned", lambda *a, **k: None)
    monkeypatch.setattr(model_gateway, "emit_event", lambda *a, **k: None)
    call = lambda: gw.settle_retained_invocation(  # noqa: E731
        _Conn(job), "p", "minv", actor="agent:shepherd", succeeded=False, provider_ref=None,
        usage_units={}, actual_cost_usd_micros=0)
    if settles:
        assert call() == {"settled": True}
    else:
        with pytest.raises(model_gateway.ModelGatewayLeaseError, match="taken"):
            call()


def test_a_refused_settle_is_tried_again_counted_never_a_standing_card(
        eng, project, monkeypatch):  # noqa: F811
    from boss_engine.services import connection_spend
    from boss_engine.services.core import create_blocking_decision

    calls = []

    def settle(c, settings, pid, receipt, *, actor):
        calls.append(receipt)
        if len(calls) == 1:
            raise connection_spend.HoldRefused("BOSS could not settle this proof: The call's job "
                                               "was taken by another worker.")
        return {}

    monkeypatch.setattr(connection_spend, "agent_settle", settle)
    with eng.begin() as c:
        card = create_blocking_decision(
            c, project, None, "Reconcile connection proof setup:connection/claude_code",
            {"action": "connection_reconciliation"}, {"receipt_id": "rcpt_live"},
            "connection_reconciliation", default_action="hold")
    fo.sweep(eng, _settings(), project_id=project)           # refused → a card, attempt 1 of 2
    fo.sweep(eng, _settings(), project_id=project, now=None)  # tried again, settles
    with eng.begin() as c:
        status = c.execute(text("SELECT status FROM decisions WHERE id = :d"), {"d": card}).scalar()
        own = c.execute(text("SELECT count(*) FROM decisions WHERE project_id = :p AND status = "
                             "'pending' AND recommendation->>'action' = :a"),
                        {"p": project, "a": fo.CARD_ACTION}).scalar()
    assert calls == ["rcpt_live", "rcpt_live"]
    assert status == "resolved" and own == 0, "the person's card went once the agent settled it"
