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

    def __init__(self, roles=(), steps=(), handoffs=(), spend=(), step_work=(),
                 live_runs=(), live_steps=(), meetings=(), discoveries=(), agent_runs=()):
        self.data = {"roles": list(roles), "steps": list(steps),
                     "handoffs": list(handoffs), "spend": list(spend),
                     "step_work": list(step_work),
                     "live_runs": list(live_runs), "live_steps": list(live_steps),
                     # Work that is NOT the coding pipeline. An agent on a panel or running a
                     # research sweep is an agent working, and none of it was read here.
                     "meetings": list(meetings), "discoveries": list(discoveries),
                     "agent_runs": list(agent_runs)}
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
        elif "count(*) filter (where status = 'succeeded')" in low:
            rows = self.data["step_work"]
        elif "from code_task_runs" in low:
            rows = self.data.get("live_runs", [])
        elif "status in ('running', 'in_progress', 'pending')" in low:
            rows = self.data.get("live_steps", [])
        elif "from meeting_runs" in low:
            rows = self.data.get("meetings", [])
        elif "from discovery_runs" in low:
            rows = self.data.get("discoveries", [])
        elif "from agent_runs" in low:
            rows = self.data.get("agent_runs", [])
        # An UNROUTED query used to fall through to `steps`, so any new table this module reads
        # silently got the step rows — which is how "is anyone researching" would have answered
        # "yes, once per finished step". Unknown means empty, loudly named in the failure.
        elif "from agent_process_steps" in low:
            rows = self.data["steps"]
        else:
            raise AssertionError(f"FakeConn has no rows for this query: {raw[:120]}")
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


# --------------------------------------------------------------------------- #
# The org chart as a tree
#
# BOSS has no parent field on a role, so a hierarchy from imagination would be an organisation
# that does not exist. The handoff graph IS directed, so a tree is derivable: a root is an agent
# nobody hands to, children are who it hands to, depth is distance from the root.
# --------------------------------------------------------------------------- #
def _edge(source, target, times=1):
    return {"source": source, "target": target, "times": times,
            "last_at": NOW, "last_action": "review"}


def test_the_root_is_an_agent_nobody_hands_to():
    out = org_chart(FakeConn(
        roles=[role("pm"), role("captain"), role("qa")],
        handoffs=[_edge("pm", "captain"), _edge("captain", "qa")],
    ), "p")
    assert out["roots"] == ["pm"]
    assert out["root_inferred"] is False
    by_id = {n["id"]: n for n in out["tree"]}
    assert by_id["pm"]["depth"] == 0
    assert by_id["captain"]["depth"] == 1
    assert by_id["qa"]["depth"] == 2


def test_children_hang_from_their_parent():
    out = org_chart(FakeConn(
        roles=[role("pm"), role("a"), role("b")],
        handoffs=[_edge("pm", "a"), _edge("pm", "b")],
    ), "p")
    by_id = {n["id"]: n for n in out["tree"]}
    assert by_id["a"]["parent"] == "pm"
    assert by_id["b"]["parent"] == "pm"
    assert out["depth"] == 2


def test_an_agent_reachable_by_several_paths_appears_once():
    """Work fans out and rejoins. Drawing an agent several times would inflate the org."""
    out = org_chart(FakeConn(
        roles=[role("pm"), role("a"), role("b"), role("join")],
        handoffs=[_edge("pm", "a"), _edge("pm", "b"),
                  _edge("a", "join"), _edge("b", "join")],
    ), "p")
    assert [n["id"] for n in out["tree"]].count("join") == 1


def test_a_cycle_falls_back_to_the_busiest_and_says_so():
    """"Nobody hands to this agent" and "everything cycles so we picked the busiest" are
    different facts, and the payload must be able to say which it drew."""
    out = org_chart(FakeConn(
        roles=[role("a"), role("b")],
        handoffs=[_edge("a", "b"), _edge("b", "a")],
    ), "p")
    assert out["root_inferred"] is True
    assert len(out["tree"]) == 2


def test_an_agent_with_no_handoffs_is_not_in_the_tree():
    """It belongs in "working alone", not hanging off a root it has no relationship with."""
    out = org_chart(FakeConn(
        roles=[role("pm"), role("hermit")],
        handoffs=[_edge("pm", "pm")],
    ), "p")
    assert "hermit" not in {n["id"] for n in out["tree"]}
    assert "hermit" in out["isolated"]


def test_an_edge_to_an_unknown_agent_is_ignored():
    """A handoff naming a role that no longer exists must not create a phantom node."""
    out = org_chart(FakeConn(
        roles=[role("pm")],
        handoffs=[_edge("pm", "deleted_role")],
    ), "p")
    assert {n["id"] for n in out["tree"]} <= {"pm"}


def test_the_tree_is_empty_when_nothing_was_handed_over():
    out = org_chart(FakeConn(roles=[role("a")]), "p")
    assert out["tree"] == []
    assert out["depth"] == 0


# --------------------------------------------------------------------------- #
# The agents that do the building
#
# The operator's complaint was that the Workforce page looked dead: "no info for software
# engineer", every card grey and scoreless. It was accurate. Every agent that builds --
# software_engineer, qa, security_reviewer, reviewer, architect -- does its work through
# llm_calls, and this screen only ever read agent_process_steps.
# --------------------------------------------------------------------------- #
def lcall(stage, *, model="claude-sonnet-5", calls=1, turns=10, tokens=4000,
          micros=250_000, retries=0, ok_calls=None, last_at=None):
    """One GROUP BY (stage, model) row, with the real column names."""
    from datetime import datetime, timezone
    return {"stage": stage, "model": model, "calls": calls, "turns": turns,
            "tokens": tokens, "micros": micros, "retries": retries,
            "ok_calls": calls if ok_calls is None else ok_calls,
            "last_at": last_at or datetime(2026, 8, 26, 12, 0, tzinfo=timezone.utc)}


def by_id(out):
    return {a["id"]: a for a in out["agents"]}


def test_an_agent_that_only_works_through_model_calls_is_no_longer_invisible():
    """software_engineer owns `implement` and `solo`. It did 58 turns of work and the card said
    "No work in this window. no calls recorded"."""
    out = workforce(FakeConn(roles=[role("software_engineer")],
                             spend=[lcall("implement", turns=12), lcall("solo", turns=46)]),
                    "proj")
    a = by_id(out)["software_engineer"]
    assert a["calls"] == 2
    assert a["turns"] == 58
    assert a["spend_usd"] == 0.5
    assert "no calls recorded" not in (a["task"] or "")


def test_per_agent_spend_uses_the_same_map_boss_attributes_with():
    """Not a time-window join. That version showed CTO and QA Lead $61.3802 EACH -- the same
    money, twice -- because every call in a 31-minute window went to every active role."""
    out = workforce(FakeConn(
        roles=[role("software_engineer"), role("security_reviewer")],
        spend=[lcall("implement", micros=800_000), lcall("security_check", micros=300_000)],
    ), "proj")
    a = by_id(out)
    assert a["software_engineer"]["spend_usd"] == 0.8
    assert a["security_reviewer"]["spend_usd"] == 0.3
    # The whole point: the two do not both get the whole bill.
    assert a["software_engineer"]["spend_usd"] != a["security_reviewer"]["spend_usd"]


def test_a_score_is_observed_and_kept_apart_from_the_configured_trust_score():
    out = workforce(FakeConn(roles=[role("qa", trust=50)],
                             spend=[lcall("check_tests", calls=10, ok_calls=8)]), "proj")
    a = by_id(out)["qa"]
    assert a["success_rate"] == 0.8
    assert a["trust_score"] == 50          # configured, unchanged, and separate
    assert a["ok_calls"] == 8 and a["failed_calls"] == 2


def test_an_agent_with_no_calls_has_no_score_rather_than_zero_or_a_hundred():
    out = workforce(FakeConn(roles=[role("qa")]), "proj")
    a = by_id(out)["qa"]
    assert a["success_rate"] is None
    assert a["spend_usd"] is None


def test_the_busiest_stages_say_what_the_agent_actually_does():
    out = workforce(FakeConn(roles=[role("chief_architect")],
                             spend=[lcall("plan", calls=7), lcall("spec", calls=2)]), "proj")
    assert [s["stage"] for s in by_id(out)["chief_architect"]["top_stages"]] == ["plan", "spec"]


def test_a_stage_no_agent_owns_is_credited_to_nobody():
    """An agent credited with work it did not do is worse than a gap."""
    out = workforce(FakeConn(roles=[role("software_engineer")],
                             spend=[lcall("implement", micros=100_000),
                                    lcall("some_unmapped_stage", micros=999_000)]), "proj")
    assert by_id(out)["software_engineer"]["spend_usd"] == 0.1


def test_work_is_credited_to_the_role_the_registry_actually_holds():
    """BOSS attributed review work to "reviewer" while its registry held
    "adversarial_reviewer". Nothing joined them, so that spend could never be matched to an
    agent -- and the registered reviewer sat idle beside the unregistered one doing its job."""
    out = workforce(FakeConn(roles=[role("adversarial_reviewer")],
                             spend=[lcall("review_code", micros=500_000)]), "proj")
    a = by_id(out)
    assert a["adversarial_reviewer"]["spend_usd"] == 0.5
    assert a["adversarial_reviewer"]["registered"] is True
    assert "reviewer" not in a


def test_an_agent_genuinely_absent_from_the_registry_is_still_listed_and_flagged():
    """The alias fixes two known names. It must not become a way to hide the next gap: an agent
    running work with no registered role has no trust score, no autonomy level and no violation
    tracking, and hiding it would hide that."""
    out = workforce(FakeConn(roles=[], spend=[lcall("implement", micros=500_000)]), "proj")
    a = by_id(out)
    assert a["software_engineer"]["registered"] is False
    assert a["software_engineer"]["spend_usd"] == 0.5


def test_the_model_shown_is_the_one_the_agent_mostly_used():
    """A single stray call on another model must not relabel the agent."""
    out = workforce(FakeConn(roles=[role("software_engineer")], spend=[
        lcall("implement", model="claude-sonnet-5", calls=9),
        lcall("solo", model="gpt-4o", calls=1),
    ]), "proj")
    assert by_id(out)["software_engineer"]["model"] == "claude-sonnet-5"


def test_the_score_looks_further_back_than_the_live_status():
    """Live on goldeneye: 26 agent cards, ONE with a scoreboard, because only one stage had run
    that day. A card reading "no calls recorded" for an agent that reviewed a hundred changes
    this month is not a quiet day, it is a wrong answer.

    The page measures agents, which is a question about a track record, not about this
    afternoon -- so the score window is 30 days while status stays live.
    """
    from boss_engine.services.console_v2_workforce import SCORE_WINDOW_HOURS

    conn = FakeConn(roles=[role("qa")], spend=[lcall("check_tests")])
    workforce(conn, "proj", hours=24)
    windows = [p.get("h") for _, p in conn.statements if "h" in p]
    assert SCORE_WINDOW_HOURS in windows, windows        # the score reaches back
    assert 24 in windows, windows                        # status still does not
    assert SCORE_WINDOW_HOURS > 24 * 7


def test_an_idle_agent_does_not_claim_its_month_of_work_happened_today():
    """"103 calls in this window" beside a screen headed "last 24h" reads as 103 calls today."""
    out = workforce(FakeConn(roles=[role("software_engineer")],
                             spend=[lcall("solo", calls=103)]), "proj")
    task = by_id(out)["software_engineer"]["task"]
    assert "30d" in task
    assert "in this window" not in task


# --------------------------------------------------------------------------- #
# Ordering, and the agents whose work is not model calls
# --------------------------------------------------------------------------- #
def pstep(role_key, *, steps=1, ok=None, failed=0, runs=1, last=None):
    """One GROUP BY role_key row from agent_process_steps."""
    from datetime import datetime, timezone
    return {"role_key": role_key, "steps": steps,
            "ok_steps": steps if ok is None else ok, "failed_steps": failed, "runs": runs,
            "last_at": last or datetime(2026, 8, 26, 12, 0, tzinfo=timezone.utc)}


def test_the_agents_that_did_the_work_come_first():
    """Alphabetical put `cao` -- which has never made a call -- above the agent that did 16,000
    turns, and the operator had to hunt for the ones that had done anything."""
    out = workforce(FakeConn(
        roles=[role("cao"), role("software_engineer"), role("qa")],
        spend=[lcall("implement", calls=400), lcall("check_tests", calls=100)],
    ), "proj")
    assert [a["id"] for a in out["agents"]][:2] == ["software_engineer", "qa"]
    assert out["agents"][-1]["id"] == "cao"


def test_pipeline_work_counts_toward_the_ranking_too():
    """Six agents run the PRD pipeline and never touch llm_calls under their own role. Scoring
    only model calls left them visibly doing work and credited with none of it."""
    out = workforce(FakeConn(
        roles=[role("cao"), role("product_manager")],
        step_work=[pstep("product_manager", steps=12)],
    ), "proj")
    a = {x["id"]: x for x in out["agents"]}
    assert a["product_manager"]["steps"] == 12
    assert out["agents"][0]["id"] == "product_manager"


def test_steps_and_calls_are_not_added_into_one_meaningless_total():
    """A step and a model call are different units."""
    out = workforce(FakeConn(roles=[role("qa")], spend=[lcall("check_tests", calls=3)],
                             step_work=[pstep("qa", steps=5)]), "proj")
    a = {x["id"]: x for x in out["agents"]}["qa"]
    assert a["calls"] == 3 and a["steps"] == 5


def test_a_step_still_running_is_not_counted_as_a_failure():
    """Counting in-flight steps as failures makes a busy agent look like a broken one."""
    out = workforce(FakeConn(roles=[role("qa")],
                             step_work=[pstep("qa", steps=10, ok=6, failed=1)]), "proj")
    a = {x["id"]: x for x in out["agents"]}["qa"]
    assert a["step_success_rate"] == round(6 / 7, 3)   # 3 in flight are excluded, not failed


def test_the_order_is_stable_when_two_agents_have_done_the_same_amount():
    """A list that reshuffles between renders is a list nobody can scan."""
    conn = lambda: FakeConn(roles=[role("qa"), role("architect")],
                            spend=[lcall("check_tests", calls=5), lcall("plan", calls=5)])
    first = [a["id"] for a in workforce(conn(), "proj")["agents"]]
    second = [a["id"] for a in workforce(conn(), "proj")["agents"]]
    assert first == second


# --------------------------------------------------------------------------- #
# Working NOW, not "worked recently"
#
# The operator launched a project, watched the build run through `implement`, and every one of
# 26 agents read IDLE. It was not a display lag: llm_calls rows are written when a call
# COMPLETES, and an implement call runs ten to thirty minutes, so for the whole time the agent
# is working hardest there is no row for it at all.
# --------------------------------------------------------------------------- #
def live_run(phase="implement", rid="crun_1", title="CSV-to-Chart Explorer"):
    return {"id": rid, "title": title, "phase": phase}


def test_an_agent_is_working_while_its_run_is_in_flight():
    """Not after the call it is making finally returns."""
    out = workforce(FakeConn(roles=[role("software_engineer")],
                             live_runs=[live_run("implement")]), "proj")
    a = by_id(out)["software_engineer"]
    assert a["status"] == "WORKING"
    assert "CSV-to-Chart" in a["task"]


def test_a_run_in_flight_beats_a_stale_inference_from_finished_calls():
    """The agent's last COMPLETED call is 22 minutes old — which is exactly what a long call in
    progress looks like."""
    from datetime import datetime, timedelta, timezone
    old = datetime.now(timezone.utc) - timedelta(minutes=22)
    out = workforce(FakeConn(roles=[role("software_engineer")],
                             spend=[lcall("solo", last_at=old)],
                             live_runs=[live_run("implement")]), "proj")
    assert by_id(out)["software_engineer"]["status"] == "WORKING"


def test_the_phase_decides_which_agent_is_working():
    out = workforce(FakeConn(roles=[role("qa"), role("software_engineer")],
                             live_runs=[live_run("write_tests")]), "proj")
    a = by_id(out)
    assert a["qa"]["status"] == "WORKING"
    assert a["software_engineer"]["status"] == "IDLE"


def test_a_queued_run_puts_nobody_to_work():
    """A phase no agent owns — queued, awaiting approval. Crediting the nearest role would put
    an agent to work that is not working."""
    out = workforce(FakeConn(roles=[role("software_engineer")],
                             live_runs=[live_run("awaiting_plan_approval")]), "proj")
    assert by_id(out)["software_engineer"]["status"] == "IDLE"


def test_an_unresolved_process_step_also_counts_as_working():
    out = workforce(FakeConn(roles=[role("product_manager")],
                             live_steps=[{"role_key": "product_manager", "action": "intake_prd",
                                          "step_key": "intake_prd", "run_id": "aprun_1"}]), "proj")
    a = by_id(out)["product_manager"]
    assert a["status"] == "WORKING"
    assert "intake_prd" in a["task"]


def test_nothing_in_flight_leaves_everyone_idle():
    """The guard must not mark agents busy on a quiet system."""
    out = workforce(FakeConn(roles=[role("qa")], spend=[lcall("check_tests")]), "proj")
    assert by_id(out)["qa"]["status"] == "IDLE"


# --------------------------------------------------------------------------- #
# Working is working, whatever the work is
# --------------------------------------------------------------------------- #
#
# "the Agents weren't showing Working when they are doing research or meeting panel, only when
#  they are doing the coding task ... any Agents working should show up like that"
#
# `_live_work` read `code_task_runs` and `agent_process_steps` — both parts of the CODING
# pipeline. A panel debating a PRD and a discovery sweep gathering evidence are agents doing
# work, and neither writes to either table, so the screen showed IDLE for the whole time they
# ran. Three more sources, on the same rule: a run in flight is an agent working.

def meeting_run(*, run="mrun_1", phase="round1", seats=("product_manager", "engineer"),
                directive="Build a changelog composer."):
    return {"id": run, "phase": phase, "directive": directive, "role_keys": list(seats)}


def discovery_run(*, run="disc_1", phase="signal_sweep", title="On-call tooling"):
    return {"id": run, "phase": phase, "title": title}


def test_a_panel_in_session_is_its_seats_working():
    out = workforce(
        FakeConn(roles=[role("product_manager"), role("qa")],
                 meetings=[meeting_run(seats=("product_manager",))]),
        "p1")
    by = {a["id"]: a for a in out["agents"]}
    assert by["product_manager"]["status"] == WORKING
    # And it says WHAT it is doing, not just that it is busy.
    assert "panel" in by["product_manager"]["task"]
    assert "round1" in by["product_manager"]["task"]
    # An agent not in the room is not working because someone else is.
    assert by["qa"]["status"] == IDLE


def test_every_seat_counts_not_just_the_registered_ones():
    # The default panel is product_manager, engineer, ux_designer, privacy_safety, operations —
    # and only the first is a registered role. Rendering one of five would be the old bug in a
    # new place: the screen would say one agent is in a meeting of five.
    seats = ("product_manager", "engineer", "ux_designer", "privacy_safety", "operations")
    out = workforce(FakeConn(roles=[role("product_manager")],
                             meetings=[meeting_run(seats=seats)]), "p1")
    by = {a["id"]: a for a in out["agents"]}
    for seat in seats:
        assert seat in by, f"{seat} is in the room and not on the screen"
        assert by[seat]["status"] == WORKING
    # The unregistered ones are marked rather than hidden — an agent doing work with nothing
    # governing it is precisely the one to know about.
    assert by["engineer"]["registered"] is False
    assert by["product_manager"]["registered"] is True


def test_research_in_flight_is_an_agent_working():
    out = workforce(FakeConn(roles=[role("qa")], discoveries=[discovery_run()]), "p1")
    by = {a["id"]: a for a in out["agents"]}
    assert "researcher" in by, "a discovery sweep is running and nobody is shown doing it"
    assert by["researcher"]["status"] == WORKING
    assert "research" in by["researcher"]["task"]
    # Credited to `researcher`, NOT to a registered agent picked by resemblance — that would
    # report a trust score and an autonomy level for work they did not do.
    assert by["researcher"]["registered"] is False
    assert by["qa"]["status"] == IDLE


def test_the_agent_answering_the_operator_is_working_too():
    out = workforce(FakeConn(roles=[role("metatron")],
                             agent_runs=[{"id": "ar_1", "step": 3}]), "p1")
    by = {a["id"]: a for a in out["agents"]}
    assert by["metatron"]["status"] == WORKING
    assert "answering" in by["metatron"]["task"]


def test_nothing_running_still_means_idle():
    # The failure mode of this fix is the opposite one: a screen where everything says WORKING
    # says nothing. Empty sources must leave every agent alone.
    out = workforce(FakeConn(roles=[role("qa"), role("pm")]), "p1")
    assert {a["status"] for a in out["agents"]} == {IDLE}


def test_a_finished_meeting_does_not_keep_its_seats_busy():
    # Only `status = 'running'` is read; a completed panel is history. Asserted on the SQL
    # because the fake cannot filter — and this is the clause that stops every agent that has
    # ever sat on a panel from reading WORKING forever.
    conn = FakeConn(roles=[role("product_manager")], meetings=[meeting_run()])
    workforce(conn, "p1")
    meeting_sql = [s for s, _ in conn.statements if "from meeting_runs" in s.lower()]
    assert meeting_sql, "meeting runs are never queried"
    assert "status = 'running'" in meeting_sql[0]
