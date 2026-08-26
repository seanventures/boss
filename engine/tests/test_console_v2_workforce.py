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
        elif "prev.role_key as source" in low:
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
def test_per_agent_spend_is_absent_rather_than_invented():
    """A number is either true or absent.

    An earlier version joined llm_calls to steps on a TIME WINDOW, which attributed every call
    in a 31-minute window to every role active in it. It shipped, and the live screen
    immediately showed CTO and QA Lead with exactly $61.3802 each -- the same money, twice.

    llm_calls records project_id, code_task_run_id and stage; steps record run_id and role_key.
    There is no join, and the vocabularies do not even overlap. So there is no honest per-agent
    cost, and None says that where 0 would say "this agent costs nothing".
    """
    out = workforce(FakeConn(roles=[role("coder")], steps=[step("coder")]), "p")
    a = out["agents"][0]
    assert a["spend_usd"] is None
    assert a["tokens"] is None
    assert a["calls"] is None


def test_the_payload_says_why_spend_is_missing():
    """A screen cannot quietly render a zero in its place if the payload states the reason."""
    out = workforce(FakeConn(roles=[role("coder")]), "p")
    assert out["spend_attributed"] is False
    assert "does not record which agent" in out["spend_note"]


def test_no_query_joins_calls_to_steps_by_time():
    """The mechanism of the bug, forbidden directly. A window join looks reasonable and is
    guaranteed to double-count whenever two agents work at once -- which is the normal case."""
    conn = FakeConn(roles=[role("coder")])
    workforce(conn, "p")
    for sql_text, _ in conn.statements:
        low = " ".join(sql_text.lower().split())
        assert "join llm_calls" not in low, "per-agent spend cannot be joined by time"


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
    handoff_sql = [s for s, _ in conn.statements if "prev.role_key as source" in s.lower()][0]
    assert "prev.role_key <> s.role_key" in handoff_sql


def test_the_org_chart_looks_back_further_than_a_day():
    """A day is too short to see the shape of how work flows."""
    conn = FakeConn(roles=[role("a")])
    org_chart(conn, "p")
    # The roles query takes no window; the windowed ones do.
    windows = [p["h"] for _, p in conn.statements if "h" in p]
    assert windows and all(h >= 168 for h in windows)


# --------------------------------------------------------------------------- #
# What the card actually needs
#
# The reference's agent card carries the project, the last artefact touched and a check-in
# time. BOSS records all three and the first version simply did not read them, so the card was
# thinner than the data behind it.
# --------------------------------------------------------------------------- #
def _step_with(**over):
    base = step("coder")
    base.update(over)
    return base


def test_the_card_says_which_work_the_agent_is_on():
    out = workforce(FakeConn(
        roles=[role("coder")],
        steps=[_step_with(run_title="Shorty — Re-build PRD", run_status="running")],
    ), "p")
    # Cleaned the same way titles are everywhere else — an artefact type is not a project name.
    assert out["agents"][0]["project"] == "Shorty"


def test_the_card_says_what_was_last_touched():
    out = workforce(FakeConn(
        roles=[role("coder")],
        steps=[_step_with(output={"artifact": "src/routes/shorten.ts"})],
    ), "p")
    assert out["agents"][0]["last_touched"] == "src/routes/shorten.ts"


def test_a_pull_request_counts_as_something_touched():
    out = workforce(FakeConn(
        roles=[role("coder")],
        steps=[_step_with(output={"pull_request_url": "https://github.com/x/y/pull/9"})],
    ), "p")
    assert "pull/9" in out["agents"][0]["last_touched"]


def test_nothing_recorded_means_nothing_shown_not_a_plausible_filename():
    """A plausible-looking path is worse than an admission, because it cannot be told apart
    from a real one."""
    out = workforce(FakeConn(roles=[role("coder")], steps=[_step_with(output={})]), "p")
    assert out["agents"][0]["last_touched"] is None


def test_a_malformed_output_does_not_break_the_card():
    out = workforce(FakeConn(roles=[role("coder")], steps=[_step_with(output="not a dict")]), "p")
    assert out["agents"][0]["last_touched"] is None


def test_the_check_in_time_is_the_step_age():
    out = workforce(FakeConn(roles=[role("coder")], steps=[step("coder", age_s=42)]), "p")
    assert out["agents"][0]["last_seen_seconds"] == 42


def test_an_agent_with_no_step_has_no_project_or_artefact():
    out = workforce(FakeConn(roles=[role("idle_one")]), "p")
    agent = out["agents"][0]
    assert agent["project"] is None
    assert agent["last_touched"] is None
    assert agent["last_seen_seconds"] is None


# --------------------------------------------------------------------------- #
# handoff_from is a STEP key, not a role key
#
# It is written as run.current_steps[0], so it names the step the work came FROM. Reading it as
# a role produced edges from a non-existent agent called "intake_prd": the Hive dropped every
# edge because the id matched no node, and the Org Chart drew them anyway, confidently, from an
# agent that does not exist.
# --------------------------------------------------------------------------- #
def test_the_handoff_query_resolves_the_step_to_the_role_that_ran_it():
    conn = FakeConn(roles=[role("a")])
    workforce(conn, "p")
    handoff_sql = " ".join(
        s for s, _ in conn.statements if "prev.role_key as source" in s.lower()
    )
    assert handoff_sql, "no handoff query at all"
    # The self-join, which is the only correct reading of this column.
    assert "prev.step_key = s.handoff_from" in handoff_sql
    assert "prev.run_id = s.run_id" in handoff_sql


def test_the_handoff_query_never_reads_the_column_as_a_role():
    """The bug, forbidden directly: `handoff_from AS source` names a step and would put a step
    key where an agent id belongs."""
    conn = FakeConn(roles=[role("a")])
    workforce(conn, "p")
    for sql_text, _ in conn.statements:
        normalised = " ".join(sql_text.lower().split())
        assert "handoff_from as source" not in normalised


def test_a_handoff_stays_inside_its_own_run():
    """Two runs can use the same step keys. Joining without run_id would invent handoffs
    between agents that never worked together."""
    conn = FakeConn(roles=[role("a")])
    workforce(conn, "p")
    handoff_sql = [s for s, _ in conn.statements if "prev.role_key as source" in s.lower()][0]
    assert "prev.run_id = s.run_id" in handoff_sql


# --------------------------------------------------------------------------- #
# Agents that work without a registered role
#
# Found by the Hive having no edges: five of the seven role keys the process template actually
# runs are not in the roles table at all. The screen was listing 21 registered roles, nearly
# all idle, while the agents doing the work were invisible.
# --------------------------------------------------------------------------- #
def test_an_agent_that_runs_work_appears_even_without_a_registered_role():
    """Listing only registered roles is a view of the CONFIGURATION. This screen is supposed to
    show the workforce."""
    out = workforce(FakeConn(
        roles=[role("cto")],
        steps=[step("cto"), step("product_manager", status="running")],
    ), "p")
    ids = {a["id"] for a in out["agents"]}
    assert "product_manager" in ids


def test_an_unregistered_agent_is_marked_not_disguised():
    """It has no trust score, no autonomy level and no violation tracking. Hiding the agent
    would hide the governance gap too — and an ungoverned agent is precisely the one to know
    about."""
    out = workforce(FakeConn(
        roles=[role("cto")],
        steps=[step("cto"), step("product_manager")],
    ), "p")
    by_id = {a["id"]: a for a in out["agents"]}
    assert by_id["cto"]["registered"] is True
    assert by_id["product_manager"]["registered"] is False
    assert out["unregistered"] == ["product_manager"]


def test_an_unregistered_agent_claims_no_trust_score():
    """Zero, not fifty. Inheriting a default would present an ungoverned agent as an average
    one."""
    out = workforce(FakeConn(roles=[], steps=[step("ghost_role")]), "p")
    assert out["agents"][0]["trust_score"] == 0
    assert out["agents"][0]["autonomy_level"] == 0


def test_its_status_comes_from_its_work_like_any_other_agent():
    out = workforce(FakeConn(roles=[], steps=[step("product_manager", status="running")]), "p")
    assert out["agents"][0]["status"] == WORKING


def test_a_registered_role_is_never_duplicated_by_its_own_steps():
    """The union must not list an agent twice because it both exists and works."""
    out = workforce(FakeConn(roles=[role("cto")], steps=[step("cto")]), "p")
    assert [a["id"] for a in out["agents"]] == ["cto"]


def test_a_fully_registered_workforce_reports_no_gap():
    out = workforce(FakeConn(roles=[role("cto"), role("qa")], steps=[step("cto")]), "p")
    assert out["unregistered"] == []
