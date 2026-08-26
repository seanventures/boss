"""The agents: who they are, what they are doing, and who they hand work to.

Three v2 screens read this — Workforce, Hive and Org Chart — so it is one module and one query
set rather than three that can disagree about how many agents exist.

WHAT THE ARTIFACT ASKS FOR vs WHAT BOSS HAS
-------------------------------------------
The artifact shows agents with a name, a role, a model, a live task, tokens-per-minute and a
last-check-in. BOSS's `roles` table has role_key, display_name, autonomy_level, trust_score and
counters for violations and hidden failures; the live task comes from `agent_process_steps`.

That is a good match in substance and a poor one in vocabulary, so the mapping is written down
here once:

    artifact            BOSS
    ----------------    ------------------------------------------------------
    agent               a ROLE, which is what actually owns work here
    task                the newest agent_process_step for that role
    status              derived from that step: running / stuck / idle
    model               the model its recent llm_calls actually used
    tpm / spend         summed from llm_calls
    handoff             agent_process_steps.handoff_from -> role_key

ORG CHART: OBSERVED, NOT DECLARED
---------------------------------
The artifact draws a hierarchy — a CEO, managers, reports. BOSS has no parent field on a role,
and inventing one would be a picture of an organisation that does not exist.

So the chart is built from HANDOFFS: who actually passes work to whom, counted. That is a real
relationship, it is measured rather than asserted, and it is more useful than an org chart
someone drew once — it shows how the work actually flows. The screen says which it is.
"""

from __future__ import annotations

from typing import Any, Optional

from sqlalchemy import text as sql

#: Step states that mean the agent is mid-task right now.
_STEP_RUNNING = ("running", "in_progress", "started")
#: A step that failed, or one that has been open far too long.
_STEP_FAILED = ("failed", "error", "blocked")

#: How long a step may sit unfinished before the agent counts as stuck rather than working.
#: 20 minutes: long enough for a real code step, short enough that a wedged one is visible in
#: the same session it wedged.
STUCK_AFTER_MINUTES = 20

WORKING, STUCK, IDLE, GATED = "WORKING", "STUCK", "IDLE", "AT GATE"


def workforce(connection, project_id: str, *, hours: int = 24) -> dict[str, Any]:
    """Every agent, its current work, and what it has cost.

    One call for three screens. The counts here are the counts those screens show, so they
    cannot drift apart the way v1's did — "Agents active 0/21" came from counting roles that
    owned a task when only 23 of 187 tasks had an owner at all.
    """
    roles = [dict(r) for r in connection.execute(
        sql(
            """
            SELECT role_key, display_name, autonomy_level, trust_score,
                   successful_runs, policy_violation_count, hidden_failure_count,
                   scope_violation_count, last_violation_at
            FROM roles
            WHERE project_id = :p
            ORDER BY display_name
            """
        ),
        {"p": project_id},
    ).mappings().all()]

    # The newest step per role. DISTINCT ON is exact here and avoids pulling every step for
    # every role just to take the first of each.
    steps = [dict(r) for r in connection.execute(
        sql(
            """
            SELECT DISTINCT ON (role_key)
                   role_key, run_id, step_key, action, status, handoff_from, created_at,
                   EXTRACT(EPOCH FROM (now() - created_at)) AS age_seconds
            FROM agent_process_steps
            WHERE project_id = :p
              AND created_at > now() - make_interval(hours => :h)
            ORDER BY role_key, created_at DESC
            """
        ),
        {"p": project_id, "h": int(hours)},
    ).mappings().all()]
    latest = {s["role_key"]: s for s in steps}

    # Handoffs: who passed work to whom, and how often. This is the org chart's real edge set.
    handoffs = [dict(r) for r in connection.execute(
        sql(
            """
            SELECT handoff_from AS source, role_key AS target,
                   count(*) AS times, max(created_at) AS last_at,
                   (array_agg(action ORDER BY created_at DESC))[1] AS last_action
            FROM agent_process_steps
            WHERE project_id = :p
              AND handoff_from IS NOT NULL
              AND handoff_from <> ''
              AND handoff_from <> role_key
              AND created_at > now() - make_interval(hours => :h)
            GROUP BY handoff_from, role_key
            ORDER BY count(*) DESC
            LIMIT 60
            """
        ),
        {"p": project_id, "h": int(hours)},
    ).mappings().all()]

    # NO PER-AGENT SPEND. NOT BECAUSE IT IS HARD -- BECAUSE IT IS NOT RECORDED.
    #
    # An earlier version of this joined llm_calls to steps on a time window, which attributed
    # every call in a 31-minute window to every role active in it. Shipped, and the live screen
    # immediately showed CTO and QA Lead with exactly $61.3802 each -- the same money, twice.
    #
    # llm_calls carries project_id, code_task_run_id and stage; agent_process_steps carries
    # run_id and role_key. There is no join between them, and the two vocabularies do not even
    # overlap (steps say accept_prd/review_prd/build; calls say spec/implement/review_code). So
    # a per-agent cost here can only be invented.
    #
    # The rule this screen holds every other number to is that a number is either true or
    # absent. This one is absent, and the screen says why rather than showing a zero that reads
    # as "this agent costs nothing".
    spend: dict[str, dict] = {}
    agents = []
    for role in roles:
        key = role["role_key"]
        step = latest.get(key)
        cost = spend.get(key, {})
        status, task = _status_of(step)
        agents.append({
            "id": key,
            "name": role.get("display_name") or key,
            "role": _humanise(key),
            "status": status,
            "task": task,
            # Absent, not zero. See the note above the (empty) spend lookup: BOSS does not
            # record which agent an llm_call belongs to, and a zero would read as "this agent
            # costs nothing" rather than "nobody knows".
            "model": cost.get("model") or None,
            "tokens": None,
            "spend_usd": None,
            "calls": None,
            "autonomy_level": int(role.get("autonomy_level") or 0),
            "trust_score": int(role.get("trust_score") or 0),
            "successful_runs": int(role.get("successful_runs") or 0),
            # Surfaced rather than buried: an agent with violations is one to look at, and v1
            # had this data and never showed it.
            "violations": (
                int(role.get("policy_violation_count") or 0)
                + int(role.get("scope_violation_count") or 0)
            ),
            "hidden_failures": int(role.get("hidden_failure_count") or 0),
            "last_seen_seconds": int(step["age_seconds"]) if step and step.get("age_seconds") else None,
            "run_id": step.get("run_id") if step else None,
        })

    return {
        "project_id": project_id,
        "window_hours": int(hours),
        "agents": agents,
        "handoffs": [
            {
                "source": h["source"],
                "target": h["target"],
                "times": int(h["times"]),
                "last_action": h.get("last_action"),
                "last_at": h["last_at"].isoformat() if h.get("last_at") else None,
            }
            for h in handoffs
        ],
        "counts": _counts(agents),
        # Stated in the payload so a screen cannot quietly render a zero in its place.
        "spend_attributed": False,
        "spend_note": (
            "BOSS does not record which agent an LLM call belongs to, so per-agent cost cannot "
            "be shown. Project spend is on the Projects screen."
        ),
    }


def _status_of(step: Optional[dict]) -> tuple[str, str]:
    """What an agent is doing, and how to say it.

    An agent with no recent step is IDLE — not "active with nothing to show". v1 reported
    "Agents active 0/21" by counting roles that owned a task, when only 23 of 187 tasks had an
    owner at all, so the number described the data model rather than the agents.
    """
    if step is None:
        return IDLE, "No work in this window."

    status = str(step.get("status") or "")
    action = str(step.get("action") or step.get("step_key") or "work")
    age = float(step.get("age_seconds") or 0)

    if status in _STEP_FAILED:
        return STUCK, f"{action} — failed"
    if status in _STEP_RUNNING:
        if age > STUCK_AFTER_MINUTES * 60:
            # Not "working". A step open for half an hour is stuck, and calling it working is
            # how a wedged agent stays invisible.
            return STUCK, f"{action} — running {int(age // 60)}m with no progress"
        return WORKING, action
    return IDLE, f"last: {action}"


def _humanise(role_key: str) -> str:
    return " ".join(word.capitalize() for word in str(role_key or "").replace("-", "_").split("_"))


def _counts(agents: list[dict]) -> dict[str, int]:
    out = {WORKING: 0, STUCK: 0, IDLE: 0, GATED: 0}
    for a in agents:
        out[a["status"]] = out.get(a["status"], 0) + 1
    out["total"] = len(agents)
    return out


def org_chart(connection, project_id: str, *, hours: int = 168) -> dict[str, Any]:
    """Who hands work to whom, counted — an observed chart, not a declared one.

    BOSS has no parent field on a role. Inventing a hierarchy would draw an organisation that
    does not exist; deriving it from handoffs shows how the work actually flows, which is both
    true and more useful.

    A week by default, because a day is too short to see the shape.
    """
    data = workforce(connection, project_id, hours=hours)
    agents = {a["id"]: a for a in data["agents"]}
    edges = data["handoffs"]

    # Who receives from the most distinct sources: the closest thing to a hub this data has.
    incoming: dict[str, int] = {}
    outgoing: dict[str, int] = {}
    for e in edges:
        incoming[e["target"]] = incoming.get(e["target"], 0) + e["times"]
        outgoing[e["source"]] = outgoing.get(e["source"], 0) + e["times"]

    nodes = []
    for key, agent in agents.items():
        nodes.append({
            **agent,
            "handoffs_in": incoming.get(key, 0),
            "handoffs_out": outgoing.get(key, 0),
            # An agent that neither gives nor receives work is isolated, which is worth seeing:
            # it is either idle by design or forgotten.
            "connected": bool(incoming.get(key) or outgoing.get(key)),
        })
    nodes.sort(key=lambda n: (-(n["handoffs_in"] + n["handoffs_out"]), n["name"]))

    return {
        "project_id": project_id,
        "window_hours": int(hours),
        "nodes": nodes,
        "edges": edges,
        # Said in the payload so the screen cannot present this as a declared structure.
        "derived_from": "observed handoffs",
        "note": (
            "BOSS has no declared reporting line. This is who actually passed work to whom in "
            f"the last {int(hours)}h."
        ),
        "isolated": [n["id"] for n in nodes if not n["connected"]],
    }
