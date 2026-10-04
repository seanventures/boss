"""The Command Center and the Approvals inbox agree on what needs the operator.

Review round 2 of the console stream: the inbox's default "Needs action" view started showing
every expired hold-default decision (127 of 165 on the live project, some offering "Approve and
merge" on pull requests from weeks ago) while the Command Center still counted only pending and
delegated. The landing page said one number and the inbox another. Both now read
decision_actions.attention. This seeds every shape that sits in a real decisions table and
reads both endpoints against the same rows.
"""
from __future__ import annotations

import json

from sqlalchemy import text

from apps.api.app.config import get_settings

SEEDED = {
    # id: (status, action, default_action, deadline SQL, where it belongs)
    "dec_agree_pending": ("pending", "provide_github_token", "hold", "NULL", "needs_action"),
    "dec_agree_delegated": ("delegated", "provide_github_token", "hold", "NULL",
                            "needs_action"),
    "dec_agree_stuck_pr": ("revision_requested", "review_pull_request", "hold", "NULL",
                           "needs_action"),
    "dec_agree_revising": ("revision_requested", "role.autonomy_change", "hold", "NULL",
                           "resolved"),
    "dec_agree_expired_hold": ("expired", "review_pull_request", "hold",
                               "now() - interval '20 days'", "expired"),
    "dec_agree_expired_reject": ("expired", "provide_github_token", "reject",
                                 "now() - interval '20 days'", "resolved"),
    # Overdue but not yet swept: the Command Center reads first and must already call it
    # expired, because the inbox's expire_overdue will.
    "dec_agree_overdue": ("pending", "provide_github_token", "hold",
                          "now() - interval '1 minute'", "expired"),
    "dec_agree_approved": ("approved", "provide_github_token", "hold", "NULL", "resolved"),
}


def _seed(engine, pid):
    with engine.begin() as c:
        for did, (status, action, default, deadline, _) in SEEDED.items():
            c.execute(text(
                "INSERT INTO decisions (id, project_id, question, recommendation, status, "
                "        default_action, deadline_at) "
                f"VALUES (:i, :p, :q, CAST(:r AS JSONB), :s, :d, {deadline}) "
                "ON CONFLICT (id) DO UPDATE SET status = EXCLUDED.status, "
                "        deadline_at = EXCLUDED.deadline_at"),
                {"i": did, "p": pid, "q": f"Seeded {did}",
                 "r": json.dumps({"action": action}), "s": status, "d": default})


def test_the_command_center_count_equals_the_inbox_needs_action_count(engine, client):
    """Over the whole project, whatever else other tests left in it: both read every row."""
    pid = get_settings().default_project_id
    _seed(engine, pid)
    cc = client.get("/v1/console/command-center", params={"project_id": pid}).json()
    items = client.get("/v1/decisions/inbox", params={"project_id": pid}).json()["items"]

    by_id = {i["id"]: i for i in items}
    for did, (*_, expected) in SEEDED.items():
        assert by_id[did]["attention"] == expected, did

    needs_action = {i["id"] for i in items if i["attention"] == "needs_action"}
    expired = {i["id"] for i in items if i["attention"] == "expired"}
    listed = {n["decision_id"] for n in cc["needs_you"] if n["kind"] == "approval"}
    assert listed == needs_action
    assert cc["counts"]["approvals"] == len(needs_action)
    assert cc["counts"]["expired_answerable"] == len(expired)


def test_an_expired_hold_decision_can_still_be_answered_from_its_own_group(engine, client):
    pid = get_settings().default_project_id
    _seed(engine, pid)
    items = client.get("/v1/decisions/inbox", params={"project_id": pid}).json()["items"]
    expired = next(i for i in items if i["id"] == "dec_agree_expired_hold")
    assert expired["attention"] == "expired"
    assert [a["label"] for a in expired["allowed_actions"]][0] == "Approve and merge"


def test_the_v2_queue_lists_the_same_decisions_as_needs_action(engine, client):
    pid = get_settings().default_project_id
    _seed(engine, pid)
    items = client.get("/v1/decisions/inbox", params={"project_id": pid}).json()["items"]
    needs_action = {i["id"] for i in items if i["attention"] == "needs_action"}
    queue = client.get("/v1/console/v2/decisions", params={"project_id": pid}).json()
    if len(needs_action) <= 60:  # the v2 page size
        assert {i["id"] for i in queue["items"]} == needs_action
    stuck = next(i for i in queue["items"] if i["id"] == "dec_agree_stuck_pr")
    assert stuck["kind"] == "pull_request"


def test_every_count_of_waiting_on_you_equals_the_inbox_needs_action(engine, client):
    """Review round 3: the Approvals nav badge (GET /v1/status pending_decisions) said 2 while
    the inbox and the Command Center said 3 over these same rows, because /v1/status counted
    pending and delegated itself and left out the pull request stuck in revision_requested.
    The Workflow screen's Decisions KPI reads the same number; the dashboard's Awaiting You
    and the v2 overview's gates kept lists of their own. Every one of them is the inbox's
    Needs action now."""
    pid = get_settings().default_project_id
    _seed(engine, pid)
    items = client.get("/v1/decisions/inbox", params={"project_id": pid}).json()["items"]
    needs_action = {i["id"] for i in items if i["attention"] == "needs_action"}
    assert "dec_agree_stuck_pr" in needs_action

    status = client.get("/v1/status", params={"project_id": pid})
    assert status.status_code == 200, status.text
    assert status.json()["pending_decisions"] == len(needs_action)

    cc = client.get("/v1/console/command-center", params={"project_id": pid}).json()
    assert cc["counts"]["approvals"] == len(needs_action)

    dashboard = client.get("/v1/dashboard", params={"project_id": pid})
    assert dashboard.status_code == 200, dashboard.text
    assert dashboard.json()["workload_state"]["pending_decisions"] == len(needs_action)

    overview = client.get("/v1/console/v2/overview", params={"project_id": pid})
    assert overview.status_code == 200, overview.text
    assert overview.json()["gates"] == len(needs_action)


def test_the_command_center_applies_overdue_deadlines_before_it_reads(engine, client):
    """Review round 3: an overdue decision whose default is reject dropped off the Command
    Center (attention calls it resolved) while nothing had recorded the expiry, so it vanished
    with no line saying what had happened to it. The Command Center now runs the same expiry
    as /v1/status and the inbox, so every surface reads the written state."""
    pid = get_settings().default_project_id
    with engine.begin() as c:
        c.execute(text(
            "INSERT INTO decisions (id, project_id, question, recommendation, status, "
            "        default_action, deadline_at) "
            "VALUES ('dec_agree_overdue_reject', :p, 'Seeded overdue reject', "
            "        CAST(:r AS JSONB), 'pending', 'reject', now() - interval '1 minute') "
            "ON CONFLICT (id) DO UPDATE SET status = 'pending', "
            "        deadline_at = now() - interval '1 minute'"),
            {"p": pid, "r": json.dumps({"action": "provide_github_token"})})
    assert client.get("/v1/console/command-center",
                      params={"project_id": pid}).status_code == 200
    with engine.begin() as c:
        written = c.execute(text(
            "SELECT status FROM decisions WHERE id = 'dec_agree_overdue_reject'")).scalar_one()
        expired_events = c.execute(text(
            "SELECT count(*) FROM events WHERE event_type = 'decision.expired' "
            "AND entity_id = 'dec_agree_overdue_reject'")).scalar_one()
    assert written == "expired"
    assert expired_events >= 1


def test_agents_live_and_the_pill_count_what_the_inbox_and_command_center_count(engine, client):
    """Agents home spec §6.3: the top-bar pill counted AGENTS in a needs-you state, so three
    gates on one agent said 1 and a pull request with no agent was never counted, while
    Approvals said the real number. The pill (`/v1/agents/live` attention) and Agents › Live
    (`/v1/agents/live-board`) now read the Command Center's list, after the same expiry."""
    pid = get_settings().default_project_id
    _seed(engine, pid)
    board = client.get("/v1/agents/live-board", params={"project_id": pid})
    assert board.status_code == 200, board.text
    board = board.json()
    pill = client.get("/v1/agents/live", params={"project_id": pid}).json()["attention"]
    cc = client.get("/v1/console/command-center", params={"project_id": pid}).json()
    items = client.get("/v1/decisions/inbox", params={"project_id": pid}).json()["items"]
    needs_action = {i["id"] for i in items if i["attention"] == "needs_action"}

    badge = client.get("/v1/status", params={"project_id": pid}).json()["pending_decisions"]

    # ONE NUMBER (agents-home D2): DECISIONS waiting on the operator. The pill, Live's header,
    # the Command Center, the Approvals badge and its Needs action all say it.
    assert board["counts"]["needs_you"] == pill["count"] == cc["counts"]["needs_you"] \
        == badge == len(needs_action)
    assert {a["id"] for a in board["attention"] if a["kind"] == "approval"} == needs_action
    assert "dec_agree_stuck_pr" in {a["id"] for a in board["attention"]}  # no agent, counted
    # The overdue one was expired by the same sweep, so no surface lists it.
    assert "dec_agree_overdue" not in {a["id"] for a in board["attention"]}
    # The pill lists what it counts: decisions, in the shared order.
    decisions = [a["id"] for a in board["attention"] if a["kind"] == "approval"]
    assert [i["id"] for i in pill["items"]] == decisions[:5]


def test_a_setup_gap_is_listed_but_never_counted_as_needs_you(engine, client, monkeypatch):
    """Agents home D2: "Connect a repository" and "Connect a model" made the pill say 3 while
    Approvals said 1. A setup gap has its own badge (Setup); it is listed on Live and the
    Command Center, and counted apart as `setup`, never in needs_you."""
    from boss_engine.services import setup_service

    pid = get_settings().default_project_id
    _seed(engine, pid)
    gaps = [{"id": "github", "title": "Connect a repository", "done": False},
            {"id": "model", "title": "Connect a model", "done": False}]
    monkeypatch.setattr(setup_service, "setup_status",
                        lambda *a, **k: {"steps": gaps})
    items = client.get("/v1/decisions/inbox", params={"project_id": pid}).json()["items"]
    needs_action = sum(1 for i in items if i["attention"] == "needs_action")
    board = client.get("/v1/agents/live-board", params={"project_id": pid}).json()
    pill = client.get("/v1/agents/live", params={"project_id": pid}).json()["attention"]
    cc = client.get("/v1/console/command-center", params={"project_id": pid}).json()
    badge = client.get("/v1/status", params={"project_id": pid}).json()["pending_decisions"]
    assert {a["id"] for a in board["attention"] if a["kind"] == "readiness"} == {
        "ready:github", "ready:model"}
    assert board["counts"]["setup"] == cc["counts"]["setup"] == 2
    assert board["counts"]["needs_you"] == pill["count"] == cc["counts"]["needs_you"] \
        == badge == needs_action
    assert not [i for i in pill["items"] if i["id"].startswith("ready:")]
    # Setup comes last in the shared order.
    kinds = [a["kind"] for a in board["attention"]]
    assert kinds[-2:] == ["readiness", "readiness"]
