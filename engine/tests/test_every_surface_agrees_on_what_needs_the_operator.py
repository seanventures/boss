"""Every surface that counts decisions agrees on what needs the operator.

Review round 2 of the console stream found three definitions. The Command Center counted
pending and delegated; the Approvals inbox counted any card the engine offered an answer on,
which swept every expired hold-default decision into the default "Needs action" view (127 of
165 decisions on the live project, some of them "Approve and merge" on pull requests from weeks
ago); the v2 queue counted every revision_requested, including ones the proposer was working
on. The landing page said one number and the inbox showed another.

`decision_actions.attention` is now the one rule. These tests pin it, and pin that the Command
Center and the v2 queue both read it, and that nothing else in engine/ or apps/ keeps a list of
its own (widened in review round 3, when the nav badge still disagreed). The API test
apps/api/tests/test_command_center_and_inbox_agree.py checks the same agreement end to end
through the real endpoints and a real database.
"""
from __future__ import annotations

import ast
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from boss_engine.services import command_center as cc
from boss_engine.services import decision_actions as da
from boss_engine.services.console_v2 import decision_queue

NOW = datetime(2026, 10, 2, 12, 0, tzinfo=timezone.utc)
ROOT = Path(__file__).resolve().parents[2]


def decision(did: str, status: str, *, action: str = "provide_github_token",
             default: str = "hold", deadline=None, **extra) -> dict:
    return {"id": did, "task_id": None, "question": f"q {did}", "status": status,
            "risk_tier": 1, "default_action": default, "deadline_at": deadline,
            "recommendation": {"action": action}, "options": [], "cost_summary": {},
            "delegated_to": None, "created_at": NOW - timedelta(days=3), **extra}


#: One of every shape that sits in a real decisions table, and where it belongs.
CASES = [
    (decision("pending", "pending"), "needs_action"),
    (decision("delegated", "delegated"), "needs_action"),
    # A pull request sent back with the old "Request changes": nothing reads that status for a
    # PR, so only the operator can move it.
    (decision("stuck_pr", "revision_requested", action="review_pull_request"), "needs_action"),
    # A proposal sent back for changes that its proposer does read: waiting on them, not you.
    (decision("revising", "revision_requested", action="role.autonomy_change"), "resolved"),
    # Expired on a hold default: nothing was applied and it can still be answered, but it is
    # not today's work.
    (decision("expired_hold", "expired"), "expired"),
    (decision("expired_reject", "expired", default="reject"), "resolved"),
    # Overdue by a minute but not yet swept by expire_overdue: the inbox would show it as
    # expired, so it is expired everywhere.
    (decision("overdue", "pending", deadline=NOW - timedelta(minutes=1)), "expired"),
    (decision("not_yet_due", "pending", deadline=NOW + timedelta(hours=2)), "needs_action"),
    (decision("approved", "approved"), "resolved"),
    (decision("rejected", "rejected"), "resolved"),
    (decision("executed", "executed"), "resolved"),
]


@pytest.mark.parametrize("row,expected", CASES, ids=[c[0]["id"] for c in CASES])
def test_each_shape_of_decision_has_one_place(row, expected):
    assert da.attention(row, NOW) == expected


def test_attention_is_one_of_three_and_rides_on_every_card():
    for row, _ in CASES:
        described = da.describe(row, NOW)
        assert described["attention"] in da.ATTENTION


def test_every_needs_action_or_expired_status_is_a_candidate_a_sql_reader_selects():
    """A reader selects candidates in SQL and classifies in Python. A status missing from the
    candidate list would be a decision the rule calls waiting that no surface ever reads."""
    for row, expected in CASES:
        if expected == "needs_action":
            assert row["status"] in da.NEEDS_ACTION_CANDIDATE_STATUSES, row["id"]
        if expected in ("needs_action", "expired"):
            assert row["status"] in da.ATTENTION_CANDIDATE_STATUSES, row["id"]


def test_the_command_center_counts_exactly_the_decisions_the_inbox_shows_under_needs_action():
    rows = [row for row, _ in CASES]
    out = cc.assemble(project_id="p", runs=[], steps=[], decisions=rows, readiness=[],
                      names={}, now=NOW)
    inbox_needs_action = {r["id"] for r in rows if da.describe(r, NOW)["attention"]
                          == "needs_action"}
    inbox_expired = {r["id"] for r in rows if da.describe(r, NOW)["attention"] == "expired"}
    listed = {n["decision_id"] for n in out["needs_you"] if n["kind"] == "approval"}
    assert listed == inbox_needs_action == {"pending", "delegated", "stuck_pr", "not_yet_due"}
    assert out["counts"]["approvals"] == len(inbox_needs_action)
    assert out["counts"]["expired_answerable"] == len(inbox_expired) == 2


def test_the_v2_queue_lists_the_same_decisions_with_their_buttons():
    class Conn:
        def execute(self, statement, params=None):
            rows = [r for r, _ in CASES if r["status"] in params["open"]]

            class _R:
                def mappings(self):
                    return self

                def all(self):
                    return rows
            return _R()

    out = decision_queue(Conn(), "p", now=NOW)
    assert {i["id"] for i in out["items"]} == {"pending", "delegated", "stuck_pr", "not_yet_due"}
    stuck = next(i for i in out["items"] if i["id"] == "stuck_pr")
    assert stuck["kind"] == "pull_request"
    assert [a["label"] for a in stuck["allowed_actions"]][0] == "Approve and merge"


# ---------------------------------------------------------------------------------------------
# NO READER KEEPS ITS OWN LIST.
#
# REWRITTEN (review round 3). This test scanned the Command Center and the v2 queue only, and
# the Approvals nav badge still disagreed with the inbox: GET /v1/status counted
# `status IN ('pending', 'delegated')` itself, so a pull request stuck in revision_requested
# made the badge say 2 while the inbox and the Command Center said 3. The Workflow screen's
# Decisions KPI, the dashboard's Awaiting You, the chat's factory briefing, health, ops and
# Metatron each kept the same list. Two readers is not the class. This walks every Python file
# in engine/ and apps/ and finds every place that names an open decision status in code, and
# every SQL statement that filters the decisions table on a pending status. Each one is either
# a reader that must go through decision_actions (and so must not be here), or a writer,
# a transition guard or a deduplication check listed below with why it is not a reader.
# ---------------------------------------------------------------------------------------------

SCANNED = ("engine/boss_engine", "apps/api/app", "apps/worker")
#: Where the rule itself lives.
THE_RULE = "engine/boss_engine/services/decision_actions.py"

#: (file, function) -> why it may name decision statuses itself. None of these tells the
#: operator what is waiting on them.
NOT_READERS = {
    ("engine/boss_engine/services/decisions.py", "expire_overdue"):
        "the writer that expires overdue pending and delegated decisions",
    ("engine/boss_engine/services/decisions.py", "transition"):
        "the transition guard (only an open decision may be answered) and the writer that "
        "sets a decision to delegated",
    ("engine/boss_engine/services/autonomy.py", "apply_policy"):
        "auto-approves pending decisions a policy covers (a writer)",
    ("engine/boss_engine/services/resource_plane.py", "approve_changeset"):
        "approves the change set's own pending decision (a writer)",
    ("engine/boss_engine/services/assurance.py", "schedule_due_metric_evaluations"):
        "deduplication: does not raise a second restore decision while one is pending",
    ("engine/boss_engine/services/core.py", "_create_blocking_decision_postgres"):
        "deduplication: reuses an identical pending decision instead of raising another",
    ("engine/boss_engine/services/jobs.py", "_create_or_reuse_budget_policy_decision"):
        "deduplication: reuses an open or approved budget decision",
    ("engine/boss_engine/tools/builtin.py", "consistency_report"):
        "a consistency check for pending decisions past their deadline that expire_overdue "
        "has not swept yet: it is about the sweep, not about what waits on the operator",
    ("engine/boss_engine/services/meeting_service.py", "<module>"):
        "DECISION_TO_PRD_STATE (console-b): a label table for where a meeting's PRD stands, "
        "mapping EVERY status decisions.py writes; it decides no one's attention",
    ("engine/boss_engine/services/run_decisions.py", "step_gate_question"):
        "deduplication: keeps a step's decision's question distinct in words from another "
        "run's (or step's) pending one",
    ("engine/boss_engine/services/workflow_capability_runner.py",
     "_refresh_waiting_previews"):
        "a writer: refreshes the action preview on gates still open, so nobody approves "
        "content that has since changed",
    ("apps/api/app/main.py", "decision_inbox"):
        "the inbox itself: it orders every row and each card carries attention, which the "
        "console filters on",
}


def _open_status_mentions(path: Path):
    """Every (function, line, snippet) in `path` that names an open decision status."""
    tree = ast.parse(path.read_text())
    parents = {child: node for node in ast.walk(tree) for child in ast.iter_child_nodes(node)}

    def enclosing(node):
        while node in parents:
            node = parents[node]
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                return node.name
        return "<module>"

    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            if isinstance(parents.get(node), ast.JoinedStr):
                continue
            value = node.value
        elif isinstance(node, ast.JoinedStr):
            value = "".join(v.value if isinstance(v, ast.Constant) else "{}" for v in node.values)
        else:
            continue
        flat = " ".join(value.split())
        names_delegated = value == "delegated" or "'delegated'" in flat or '"delegated"' in flat
        filters_pending = bool(DECISIONS_TABLE.search(flat) and PENDING_FILTER.search(flat))
        if names_delegated or filters_pending:
            yield enclosing(node), node.lineno, flat[:120]


DECISIONS_TABLE = re.compile(r"\b(from|join|update)\s+decisions\b", re.I)
PENDING_FILTER = re.compile(r"status\s*(in\s*\([^)]*'pending'|=\s*'pending')", re.I)


def _every_mention():
    found = {}
    for base in SCANNED:
        for path in sorted((ROOT / base).rglob("*.py")):
            rel = str(path.relative_to(ROOT))
            if rel == THE_RULE or "/tests/" in rel:
                continue
            for function, line, snippet in _open_status_mentions(path):
                found.setdefault((rel, function), []).append((line, snippet))
    return found


def test_no_reader_in_engine_or_apps_keeps_its_own_list_of_open_decision_statuses():
    """Every place that names an open decision status is the rule itself or a listed
    non-reader. A new badge or count that writes `status IN ('pending', 'delegated')` fails
    here with where it is and what to call instead."""
    readers = {k: v for k, v in _every_mention().items() if k not in NOT_READERS}
    assert not readers, (
        "These read decisions with their own list of open statuses, so they will disagree "
        "with the Approvals inbox. Use decision_actions.waiting_on_operator / "
        "count_waiting_on_operator (or attention), or, if it is a writer or a guard, add it to "
        "NOT_READERS with the reason:\n" + "\n".join(
            f"  {path}:{hits[0][0]} in {fn}(): {hits[0][1]}"
            for (path, fn), hits in sorted(readers.items())))


def test_every_listed_non_reader_still_exists():
    """A stale entry is an exemption nobody needs: if the code moved, the list must too."""
    stale = set(NOT_READERS) - set(_every_mention())
    assert not stale, f"NOT_READERS lists places that no longer name a status: {sorted(stale)}"


def test_the_scan_finds_the_reader_that_review_round_3_found(tmp_path):
    """The scan is only as good as its pattern. This is the /v1/status query as it was."""
    sample = tmp_path / "reader.py"
    sample.write_text(
        'def status(c):\n'
        '    return c.execute("""\n'
        '        SELECT COUNT(*) FROM decisions\n'
        "        WHERE project_id = :p AND status IN ('pending', 'delegated')\n"
        '    """)\n'
        'def badge(rows):\n'
        '    return [r for r in rows if r["status"] in {"pending", "delegated"}]\n'
        'def gates(c):\n'
        "    return c.execute(\"SELECT count(*) FROM decisions WHERE status = 'pending'\")\n")
    assert {fn for fn, _, _ in _open_status_mentions(sample)} == {"status", "badge", "gates"}


class _CandidateConn:
    """Answers the shared reader's one query from CASES, as Postgres would."""

    def __init__(self):
        self.seen = []

    def execute(self, statement, params=None):
        self.seen.append((str(statement), dict(params or {})))
        rows = [r for r, _ in CASES if r["status"] in params["statuses"]
                and (params.get("min_risk_tier") is None
                     or r["risk_tier"] >= params["min_risk_tier"])]

        class _R:
            def mappings(self):
                return self

            def all(self):
                return rows
        return _R()


def test_the_shared_reader_returns_exactly_what_the_inbox_calls_needs_action():
    """Every badge and count reads this, so it must equal the inbox's Needs action, and the
    stuck pull request the /v1/status badge left out must be in it."""
    conn = _CandidateConn()
    got = {r["id"] for r in da.waiting_on_operator(conn, "p", now=NOW)}
    assert got == {r["id"] for r, where in CASES if where == "needs_action"}
    assert "stuck_pr" in got
    assert da.count_waiting_on_operator(conn, "p", now=NOW) == len(got)
    # The rule stays in Python: SQL selects candidate statuses only.
    sql_text, params = conn.seen[0]
    assert "'pending'" not in sql_text
    assert set(params["statuses"]) == set(da.NEEDS_ACTION_CANDIDATE_STATUSES)


def test_the_shared_reader_adds_the_answerable_expired_ones_only_when_asked():
    got = {r["id"] for r in da.waiting_on_operator(_CandidateConn(), "p", now=NOW,
                                                   include_expired=True)}
    assert got == {r["id"] for r, where in CASES if where in ("needs_action", "expired")}


# ---------------------------------------------------------------------------------------------
# AGENTS › LIVE AND THE TOP-BAR PILL COUNT THE SAME LIST (agents-home spec §6.3, §8.1).
#
# The pill counted AGENTS in a needs-you state, from open acts: three gates on one agent said
# "1 needs you" while Approvals said 3, and a pull request or a refusal with no agent was never
# counted. Live's cards and the pill now read command_center.assemble().needs_you through
# agents_live_board — attributed, never re-derived — so the counts are one number.
# ---------------------------------------------------------------------------------------------

def _failed_run():
    run = {"id": "wfr_failed", "status": "failed", "workflow_key": "ship", "workflow_name": "Ship",
           "input": {"title": "Ship it #180"}, "created_at": NOW - timedelta(hours=1),
           "updated_at": NOW - timedelta(minutes=5), "completed_at": None,
           "spec": {"steps": [{"id": "plan"}, {"id": "build"}, {"id": "test"}]}}
    steps = [{"workflow_run_id": "wfr_failed", "step_id": "plan", "status": "succeeded"},
             {"workflow_run_id": "wfr_failed", "step_id": "build", "status": "failed",
              "last_error": "tests failed", "completed_at": NOW - timedelta(minutes=5)}]
    answers = [{"key": "retry_step", "label": "Retry", "state": "enabled", "step_id": "build"},
               {"key": "skip_step", "label": "Skip", "state": "enabled", "step_id": "build"}]
    return run, steps, {"wfr_failed": [(steps[1], answers)]}


def _board_attention(monkeypatch, needs_you, decisions, *, owner=None):
    from boss_engine.services import agent_acts, agents_live_board

    monkeypatch.setattr(agent_acts, "owners_of_decisions",
                        lambda c, p, ids: {i: owner for i in ids})
    monkeypatch.setattr(agent_acts, "step_owners",
                        lambda c, p, pairs: {pair: "software_engineer" for pair in pairs})
    return agents_live_board.attention_items(None, "p", needs_you, decisions)


def test_live_and_the_pill_count_exactly_what_the_command_center_counts(monkeypatch):
    rows = [row for row, _ in CASES]
    # Three gates on ONE agent: the pill used to say 1.
    rows += [decision(f"gate_{n}", "pending", recommendation={
        "action": "workflow_step_gate", "workflow_run_id": "wfr_x"}) for n in range(3)]
    run, steps, stopped = _failed_run()
    out = cc.assemble(project_id="p", runs=[run], steps=steps, decisions=rows,
                      readiness=[{"id": "model", "title": "Connect a model", "done": False}],
                      names={}, now=NOW, stopped=stopped)
    attention = _board_attention(monkeypatch, out["needs_you"], rows, owner="media_publisher")
    assert [a["id"] for a in attention] == [n["id"] for n in out["needs_you"]]
    assert len(attention) == out["counts"]["needs_you"]
    inbox = {r["id"] for r in rows if da.describe(r, NOW)["attention"] == "needs_action"}
    assert {a["id"] for a in attention if a["kind"] == "approval"} == inbox
    assert out["counts"]["needs_you"] == len(inbox) + 1 + 1  # + the failed step + the setup gap
    failed = next(a for a in attention if a["kind"] == "failed_step")
    assert failed["id"] == "run:wfr_failed:build"
    assert [x["label"] for x in failed["answers"]] == ["Retry", "Skip"]
    assert failed["run"]["step_index"] == 2 and failed["run"]["step_count"] == 3


def test_a_card_decides_in_place_only_where_the_server_allows(monkeypatch):
    from boss_engine.services.agents_live_board import decide_inline

    rows = [
        decision("low_gate", "pending", risk_tier=1,
                 recommendation={"action": "workflow_step_gate", "workflow_run_id": "r"}),
        decision("high_gate", "pending", risk_tier=3,
                 recommendation={"action": "workflow_step_gate", "workflow_run_id": "r"}),
        decision("pr", "pending", risk_tier=1, recommendation={"action": "review_pull_request"}),
        decision("unhashed", "pending", risk_tier=1, recommendation={
            "action": "workflow_step_gate", "workflow_run_id": "r",
            "action_preview": {"kind": "email", "title": "Weekly", "content_hash": None}}),
        decision("hashed", "pending", risk_tier=2, recommendation={
            "action": "workflow_step_gate", "workflow_run_id": "r",
            "action_preview": {"kind": "email", "title": "Weekly", "content_hash": "sha:1"}}),
    ]
    out = cc.assemble(project_id="p", runs=[], steps=[], decisions=rows, readiness=[],
                      names={}, now=NOW)
    got = {a["id"]: a for a in _board_attention(monkeypatch, out["needs_you"], rows)}
    assert got["low_gate"]["decide_inline"] is True
    assert got["high_gate"]["decide_inline"] is False
    assert got["pr"]["decide_inline"] is False
    assert got["unhashed"]["decide_inline"] is False
    assert got["hashed"]["decide_inline"] is True
    assert got["hashed"]["content_hash"] == "sha:1" and got["hashed"]["preview"] == "Weekly"
    # The buttons are the engine's own offer, label and effect included.
    labels = [a["label"] for a in got["low_gate"]["actions"]]
    assert labels and labels == [a["label"] for a in da.allowed_actions(rows[0])]
    assert all(isinstance(a["needs"], list) for a in got["low_gate"]["actions"])
    assert decide_inline("refusal", 1, got["low_gate"]["actions"], False, None) is False


def test_a_decision_with_no_known_agent_is_nobodys():
    """Attribution is the act linked to the decision, its parent, or the step's named captain;
    otherwise None — a card shows BOSS or You, never a guessed face."""
    from boss_engine.services import agents_live_board

    out = cc.assemble(project_id="p", runs=[], steps=[], decisions=[decision("lonely", "pending")],
                      readiness=[], names={}, now=NOW)

    class Conn:
        def execute(self, statement, params=None):
            class _R:
                def mappings(self):
                    return self

                def all(self):
                    return []
            return _R()

    got = agents_live_board.attention_items(Conn(), "p", out["needs_you"], [])
    assert got[0]["role_key"] is None


def test_the_live_readers_are_in_the_scan():
    """agents_live_board and agent_acts read attention; the scan above must cover them."""
    found = {path for base in SCANNED for path in
             (str(p.relative_to(ROOT)) for p in (ROOT / base).rglob("*.py"))}
    assert "engine/boss_engine/services/agents_live_board.py" in found
    assert "engine/boss_engine/services/agent_acts.py" in found
