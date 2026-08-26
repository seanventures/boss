"""Workforce, Hive and Org Chart — one query, three screens.

The v1 console reported "Agents active 0/21". That number came from counting roles that owned
a task, when only 23 of 187 tasks had an owner at all — so it described the data model rather
than the agents. Everything here is about not doing that again.
"""

from datetime import datetime, timedelta, timezone

import pytest

from boss_engine.services.console_v2_workforce import (
    IDLE,
    STUCK,
    STUCK_AFTER_MINUTES,
    WORKING,
    org_chart,
    workforce,
)

NOW = datetime(2026, 8, 26, 12, 0, tzinfo=timezone.utc)


class FakeConn:
    """Routes each query to its seeded rows by the table it names."""

    def __init__(self, roles=(), steps=(), handoffs=(), spend=()):
        self.data = {"roles": list(roles), "steps": list(steps),
                     "handoffs": list(handoffs), "spend": list(spend)}
        self.statements = []

    def execute(self, statement, params=None):
        raw = str(statement)
        self.statements.append((raw, dict(params or {})))
        low = raw.lower()
        if "from roles" in low:
            rows = self.data["roles"]
        elif "handoff_from as source" in low:
            rows = self.data["handoffs"]
        elif "from llm_calls" in low or "join llm_calls" in low:
            rows = self.data["spend"]
        else:
            rows = self.data["steps"]
        conn_rows = rows

        class _R:
            def mappings(self):
                return self

            def all(self):
                return [dict(r) for r in conn_rows]

        return _R()


def role(key, name=None, trust=50, violations=0, hidden=0, autonomy=1, runs=0):
    return {"role_key": key, "display_name": name or key.title(),
            "autonomy_level": autonomy, "trust_score": trust, "successful_runs": runs,
            "policy_violation_count": violations, "hidden_failure_count": hidden,
            "scope_violation_count": 0, "last_violation_at": None}


def step(key, *, status="succeeded", action="review_code", age_s=60, handoff=None, run="run_1"):
    return {"role_key": key, "run_id": run, "step_key": action, "action": action,
            "status": status, "handoff_from": handoff,
            "created_at": NOW - timedelta(seconds=age_s), "age_seconds": age_s}


# --------------------------------------------------------------------------- #
# An agent's status describes the agent, not the schema
# --------------------------------------------------------------------------- #
def test_a_role_with_no_recent_work_is_idle_not_active():
    """"Agents active 0/21" counted the data model. An agent with nothing recent is idle, and
    the row says so rather than being silently absent or silently counted."""
    out = workforce(FakeConn(roles=[role("architect")]), "p")
    a = out["agents"][0]
    assert a["status"] == IDLE
    assert "No work" in a["task"]


def test_a_running_step_makes_the_agent_working():
    out = workforce(FakeConn(roles=[role("coder")],
                             steps=[step("coder", status="running", age_s=120)]), "p")
    assert out["agents"][0]["status"] == WORKING


def test_a_step_running_far_too_long_is_stuck_not_working():
    """A step open for half an hour is stuck. Calling it 'working' is how a wedged agent stays
    invisible — which is what happened to a real run that sat for 22 minutes."""
    out = workforce(FakeConn(roles=[role("coder")],
                             steps=[step("coder", status="running",
                                         age_s=(STUCK_AFTER_MINUTES + 5) * 60)]), "p")
    a = out["agents"][0]
    assert a["status"] == STUCK
    assert "no progress" in a["task"]


def test_a_failed_step_is_stuck_and_says_so():
    out = workforce(FakeConn(roles=[role("coder")],
                             steps=[step("coder", status="failed")]), "p")
    assert out["agents"][0]["status"] == STUCK
    assert "failed" in out["agents"][0]["task"]


def test_the_counts_match_the_agents_listed():
    """The headline number and the list must come from the same place, or they drift."""
    out = workforce(FakeConn(
        roles=[role("a"), role("b"), role("c")],
        steps=[step("a", status="running"), step("b", status="failed")],
    ), "p")
    assert out["counts"]["total"] == 3
    assert out["counts"][WORKING] == 1
    assert out["counts"][STUCK] == 1
    assert out["counts"][IDLE] == 1
    assert sum(out["counts"][k] for k in (WORKING, STUCK, IDLE)) == len(out["agents"])


# --------------------------------------------------------------------------- #
# Facts, not configuration
# --------------------------------------------------------------------------- #
def test_the_model_shown_is_the_one_actually_used():
    """A configured model that never ran is a claim. A recorded one is a fact."""
    out = workforce(FakeConn(
        roles=[role("coder")], steps=[step("coder")],
        spend=[{"role_key": "coder", "micros": 1_250_000, "tokens": 4200,
                "model": "claude-opus-5", "calls": 3}],
    ), "p")
    a = out["agents"][0]
    assert a["model"] == "claude-opus-5"
    assert a["spend_usd"] == 1.25
    assert a["tokens"] == 4200


def test_an_agent_with_no_recorded_calls_claims_no_model():
    out = workforce(FakeConn(roles=[role("coder")], steps=[step("coder")]), "p")
    assert out["agents"][0]["model"] is None
    assert out["agents"][0]["spend_usd"] == 0


def test_violations_are_surfaced_rather_than_buried():
    """BOSS records policy and scope violations per role and v1 never showed them. An agent
    with violations is precisely the one worth looking at."""
    out = workforce(FakeConn(roles=[role("rogue", violations=3, hidden=2)]), "p")
    a = out["agents"][0]
    assert a["violations"] == 3
    assert a["hidden_failures"] == 2


# --------------------------------------------------------------------------- #
# The org chart is observed, and says so
# --------------------------------------------------------------------------- #
def test_the_chart_is_built_from_real_handoffs():
    out = org_chart(FakeConn(
        roles=[role("architect"), role("coder")],
        handoffs=[{"source": "architect", "target": "coder", "times": 7,
                   "last_at": NOW, "last_action": "implement"}],
    ), "p")
    assert out["edges"][0]["source"] == "architect"
    assert out["edges"][0]["times"] == 7


def test_the_chart_declares_that_it_is_derived():
    """The artifact draws a reporting line. BOSS has none, and presenting a derived shape as a
    declared structure would be a picture of an organisation that does not exist."""
    out = org_chart(FakeConn(roles=[role("a")]), "p")
    assert out["derived_from"] == "observed handoffs"
    assert "no declared reporting line" in out["note"]


def test_an_agent_nobody_works_with_is_flagged_isolated():
    """Either idle by design or forgotten — both worth seeing, and invisible on a tree."""
    out = org_chart(FakeConn(
        roles=[role("architect"), role("hermit")],
        handoffs=[{"source": "architect", "target": "architect", "times": 1,
                   "last_at": NOW, "last_action": "x"}],
    ), "p")
    assert "hermit" in out["isolated"]


def test_the_busiest_agent_sorts_first():
    out = org_chart(FakeConn(
        roles=[role("quiet"), role("hub")],
        handoffs=[{"source": "quiet", "target": "hub", "times": 9,
                   "last_at": NOW, "last_action": "x"}],
    ), "p")
    assert out["nodes"][0]["id"] == "hub"


def test_a_self_handoff_is_not_an_edge():
    """An agent passing work to itself is a step, not a relationship, and drawing it would put
    a loop on every node."""
    conn = FakeConn(roles=[role("a")])
    org_chart(conn, "p")
    handoff_sql = [s for s, _ in conn.statements if "handoff_from as source" in s.lower()][0]
    assert "handoff_from <> role_key" in handoff_sql


def test_the_org_chart_looks_back_further_than_a_day():
    """A day is too short to see the shape of how work flows."""
    conn = FakeConn(roles=[role("a")])
    org_chart(conn, "p")
    # The roles query takes no window; the windowed ones do.
    windows = [p["h"] for _, p in conn.statements if "h" in p]
    assert windows and all(h >= 168 for h in windows)
