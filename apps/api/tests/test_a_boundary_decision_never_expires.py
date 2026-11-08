"""A question on the compliance boundary never silently expires.

Four "Approve code change" merges for the helpdesk security fixes (dec_409e473b476e464fbdaa,
dec_3fcc49212d8e4812b2a2, dec_9f0036438f3243f580dd, dec_83c8a35490a741999fa2; default hold,
deadline 24 h after they were asked) went to `expired` on 2026-11-08 and dropped out of the
project's Needs you, the bell and Approvals' default view. Nothing was applied; the PRs stayed
open and the runs waited "on PR #3 to be merged" — for a person who could no longer see them.

The rule (decision_actions.never_expires): a hold-default decision on decide_list (production
deploy, merge to the default branch, spend above the cap, secrets, outside messages, deleting
data, who can do what) stays open past its deadline, ages from when it was asked, and gets ONE
`decision.overdue` reminder; one an older build expired is reopened by the sweep and at boot.
A hold question OFF the list still expires, and stays visible while it can be answered.
"""
from __future__ import annotations

import json
from uuid import uuid4

from sqlalchemy import text

from apps.api.app import main as api_main
from apps.api.app.config import get_settings
from apps.api.tests.test_preview_approval_gate import _insert_merge_decision


def _pid() -> str:
    return get_settings().default_project_id


def _age(engine, did: str, *, status: str, asked_hours_ago: int = 25, deadline_hours: int = 24):
    """The live shape: asked `asked_hours_ago`, deadline `deadline_hours` after it was asked."""
    with engine.begin() as c:
        c.execute(text(
            "UPDATE decisions SET status = :s, default_action = 'hold', "
            "created_at = now() - make_interval(hours => :a), "
            "deadline_at = now() - make_interval(hours => :a) + make_interval(hours => :d) "
            "WHERE id = :i"), {"s": status, "a": asked_hours_ago, "d": deadline_hours, "i": did})


def _row(engine, did: str) -> dict:
    with engine.begin() as c:
        return dict(c.execute(text("SELECT status, deadline_at FROM decisions WHERE id = :i"),
                              {"i": did}).mappings().one())


def _events(engine, did: str, kind: str) -> int:
    with engine.begin() as c:
        return int(c.execute(text(
            "SELECT count(*) FROM events WHERE entity_id = :i AND event_type = :t"),
            {"i": did, "t": kind}).scalar_one())


def _inbox(client, pid: str) -> dict[str, dict]:
    items = client.get("/v1/decisions/inbox", params={"project_id": pid}).json()["items"]
    return {i["id"]: i for i in items}


def test_a_merge_past_its_deadline_stays_in_needs_you_and_approvals_and_merges_when_approved(
        client, engine):
    pid = _pid()
    did = f"dec_noexp_{uuid4().hex[:10]}"
    _insert_merge_decision(engine, decision_id=did, branch_name="boss/security-fix",
                           pull_request_url="https://github.com/acme/helpdesk/pull/3",
                           project_id=pid)
    _age(engine, did, status="pending")

    # Approvals: the sweep runs on this read and keeps it open, waiting on you.
    item = _inbox(client, pid)[did]
    assert item["status"] == "pending" and item["attention"] == "needs_action"
    assert _row(engine, did)["deadline_at"] is None
    assert _events(engine, did, "decision.overdue") == 1
    assert _events(engine, did, "decision.expired") == 0
    _inbox(client, pid)                                   # a second sweep reminds no more
    assert _events(engine, did, "decision.overdue") == 1

    # Needs you on the project page carries it as a merge card.
    overview = client.get(f"/v1/projects/{pid}/overview")
    assert overview.status_code == 200, overview.text
    cards = {c["id"]: c for c in overview.json()["needs_you"]}
    assert cards[did]["kind"] == "merge" and cards[did]["label"] == "Merge PR #3"

    # Approving it runs the governed merge.
    previous = api_main.decisions.settings.enable_sandbox_merge
    api_main.decisions.settings.enable_sandbox_merge = True
    try:
        approved = client.post(f"/v1/decisions/{did}/approve",
                               json={"actor": "ceo", "note": "Ship the security fix."})
        assert approved.status_code == 200, approved.text
    finally:
        api_main.decisions.settings.enable_sandbox_merge = previous
    with engine.begin() as c:
        job = c.execute(text(
            "SELECT status, payload FROM jobs WHERE kind = 'github.pull_request.merge' "
            "AND payload->>'decision_id' = :i"), {"i": did}).mappings().one()
    assert job["status"] == "queued" and job["payload"]["pull_request_number"] == 3
    assert _row(engine, did)["status"] == "approved"


def test_the_live_shape_comes_back_on_upgrade_with_no_hand_edit(client, engine):
    """dec_409e473b…: review_pull_request, hold, deadline 24 h after it was asked, status
    expired. The boot step and the tick's sweep each bring such a decision back."""
    pid = _pid()
    at_boot, on_tick = (f"dec_live_{uuid4().hex[:10]}" for _ in range(2))
    for n, did in enumerate((at_boot, on_tick), start=3):
        _insert_merge_decision(engine, decision_id=did, branch_name=f"boss/fix-{n}",
                               pull_request_url=f"https://github.com/acme/helpdesk-ai-v3/pull/{n}",
                               project_id=pid)
        _age(engine, did, status="expired")

    with engine.begin() as c:                              # the lifespan's boot step
        reopened = api_main.decisions.reopen_boundary_decisions(c)
    assert at_boot in reopened and on_tick in reopened
    _age(engine, on_tick, status="expired")                # expire it again for the tick
    with engine.begin() as c:                              # monitor_scheduler's "deadlines"
        api_main.decisions.expire_overdue(c)

    for did in (at_boot, on_tick):
        row = _row(engine, did)
        assert row["status"] == "pending" and row["deadline_at"] is None, did
        assert _events(engine, did, "decision.reopened") >= 1
    items = _inbox(client, pid)
    assert items[at_boot]["attention"] == items[on_tick]["attention"] == "needs_action"


def test_a_boundary_question_whose_run_ended_is_not_reopened(engine):
    pid = _pid()
    did = f"dec_gone_{uuid4().hex[:10]}"
    _insert_merge_decision(engine, decision_id=did, branch_name="boss/gone",
                           pull_request_url="https://github.com/acme/x/pull/9", project_id=pid)
    _age(engine, did, status="expired")
    with engine.begin() as c:
        c.execute(text("UPDATE decisions SET status = 'withdrawn' WHERE id = :i"), {"i": did})
        assert did not in api_main.decisions.reopen_boundary_decisions(c)
    assert _row(engine, did)["status"] == "withdrawn"


def test_an_expired_question_off_the_list_is_still_shown_while_answerable(client, engine):
    pid = _pid()
    did = f"dec_off_{uuid4().hex[:10]}"
    with engine.begin() as c:
        c.execute(text(
            "INSERT INTO decisions (id, project_id, question, recommendation, default_action, "
            "status, created_at, deadline_at) VALUES (:i, :p, 'Reconcile connection proof', "
            "CAST(:r AS JSONB), 'hold', 'pending', now() - interval '2 days', "
            "now() - interval '1 day')"),
            {"i": did, "p": pid, "r": json.dumps({"action": "connection_reconciliation"})})

    item = _inbox(client, pid)[did]                        # the sweep expires it
    assert item["status"] == "expired" and item["attention"] == "expired"
    assert "approve" in {a["action"] for a in item["allowed_actions"]}

    overview = client.get(f"/v1/projects/{pid}/overview").json()
    assert did not in {c["id"] for c in overview["needs_you"]}
    line = overview["still_answerable"]
    assert line and line["count"] >= 1 and "decision_filter=expired" in line["href"]
    assert line["since"]

    answered = client.post(f"/v1/decisions/{did}/approve", json={"actor": "ceo", "note": "ok"})
    assert answered.status_code == 200, answered.text


def test_the_bell_the_project_strip_and_approvals_count_the_overdue_merge_alike(client, engine):
    pid = _pid()
    did = f"dec_count_{uuid4().hex[:10]}"
    _insert_merge_decision(engine, decision_id=did, branch_name="boss/count",
                           pull_request_url="https://github.com/acme/helpdesk/pull/5",
                           project_id=pid)
    _age(engine, did, status="pending")

    items = _inbox(client, pid)
    needs_action = {i for i, it in items.items() if it["attention"] == "needs_action"}
    assert did in needs_action
    badge = client.get("/v1/status", params={"project_id": pid}).json()["pending_decisions"]
    bell = client.get("/v1/agents/live", params={"project_id": pid}).json()["attention"]["count"]
    overview = client.get(f"/v1/projects/{pid}/overview").json()
    decided = [c for c in overview["needs_you"] if c["id"].startswith("dec_")]
    summary = client.get("/v1/projects/summary").json()["items"]
    strip = next((s for s in summary if s["id"] == pid), None)
    assert badge == bell == len(needs_action)
    assert {c["id"] for c in decided} <= needs_action and did in {c["id"] for c in decided}
    if strip is not None:          # the bootstrap project is not listed (placeholder)
        setup = len([c for c in overview["needs_you"] if not c["id"].startswith(("dec_", "work:"))])
        assert strip["needs_you"] == len(needs_action) + setup
