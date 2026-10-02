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
    """Roles with observed customer work, their current work, and what it has cost.

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
    # The newest step per role, WITH what it touched and which run it belongs to.
    #
    # The reference's card shows the project, the last artefact touched and a check-in time.
    # BOSS records all three -- run title, step output, step timestamp -- and the first version
    # of this simply did not read them, so the card was thinner than the data.
    steps = [dict(r) for r in connection.execute(
        sql(
            """
            SELECT DISTINCT ON (s.role_key)
                   s.role_key, s.run_id, s.step_key, s.action, s.status,
                   s.handoff_from, s.output, s.created_at,
                   EXTRACT(EPOCH FROM (now() - s.created_at)) AS age_seconds,
                   r.title AS run_title, r.status AS run_status
            FROM agent_process_steps s
            LEFT JOIN agent_process_runs r ON r.id = s.run_id
            WHERE s.project_id = :p
              AND s.created_at > now() - make_interval(hours => :h)
              -- The always-on process manager runs a simulated health check. Its steps
              -- remain in the audit tables, but they are not customer agent work.
              AND NOT (
                COALESCE(r.input->>'simulated', 'false') = 'true'
                AND COALESCE(r.input->>'auto_build', 'false') = 'false'
                AND (
                  COALESCE(r.input->>'origin', '') = 'process_manager_canary'
                  OR (COALESCE(r.input->>'origin', '') = ''
                      AND r.actor = 'system' AND r.title = 'PM simulated canary')
                )
              )
            ORDER BY s.role_key, s.created_at DESC
            """
        ),
        {"p": project_id, "h": int(hours)},
    ).mappings().all()]
    latest = {s["role_key"]: s for s in steps}

    # Handoffs: who passed work to whom, and how often. The org chart's real edge set.
    #
    # `handoff_from` HOLDS A STEP KEY, NOT A ROLE KEY.
    #
    # It is written as run.current_steps[0] (agent_process.py), so it names the step the work
    # came FROM. Reading it as a role produced edges from a non-existent agent called
    # "intake_prd" -- the Hive dropped every edge because the id matched no node, and the Org
    # Chart drew them anyway, confidently, from an agent that does not exist.
    #
    # So it is resolved through the step it names, in the same run, to the role that actually
    # ran it. A self-join, and the only correct reading of this column.
    handoffs = [dict(r) for r in connection.execute(
        sql(
            """
            SELECT prev.role_key AS source, s.role_key AS target,
                   count(*) AS times, max(s.created_at) AS last_at,
                   (array_agg(s.action ORDER BY s.created_at DESC))[1] AS last_action
            FROM agent_process_steps s
            JOIN agent_process_steps prev
              ON prev.run_id = s.run_id
             AND prev.step_key = s.handoff_from
             AND prev.project_id = s.project_id
            JOIN agent_process_runs r ON r.id = s.run_id
            WHERE s.project_id = :p
              AND s.handoff_from IS NOT NULL
              AND s.handoff_from <> ''
              -- An agent handing work to itself is a step, not a relationship.
              AND prev.role_key <> s.role_key
              AND s.created_at > now() - make_interval(hours => :h)
              AND NOT (
                COALESCE(r.input->>'simulated', 'false') = 'true'
                AND COALESCE(r.input->>'auto_build', 'false') = 'false'
                AND (
                  COALESCE(r.input->>'origin', '') = 'process_manager_canary'
                  OR (COALESCE(r.input->>'origin', '') = ''
                      AND r.actor = 'system' AND r.title = 'PM simulated canary')
                )
              )
            GROUP BY prev.role_key, s.role_key
            ORDER BY count(*) DESC
            LIMIT 60
            """
        ),
        {"p": project_id, "h": int(hours)},
    ).mappings().all()]

    # PER-AGENT WORK AND SPEND -- now real, and worth explaining why.
    #
    # An earlier version joined llm_calls to steps on a TIME WINDOW, which attributed every call
    # in a 31-minute window to every role active in it. Shipped, and the live screen immediately
    # showed CTO and QA Lead with exactly $61.3802 each -- the same money, twice. It was removed
    # and the screen said "not recorded", which was true of the join being attempted.
    #
    # It was not true of BOSS. llm_telemetry.STAGE_ROLES maps every pipeline stage to the agent
    # that owns it, and BOSS uses that map AT WRITE TIME to attribute spend. So the attribution
    # already exists and is authoritative; this screen simply was not reading it. Using the same
    # map means the workforce page and the cost ledger cannot disagree about who spent what.
    #
    # This is what made the page look dead: every agent that does the actual building --
    # software_engineer, qa, security_reviewer, reviewer, architect -- works through llm_calls,
    # and the screen only ever looked at agent_process_steps.
    spend = _llm_work(connection, project_id, SCORE_WINDOW_HOURS)
    step_work = _step_work(connection, project_id, SCORE_WINDOW_HOURS)
    live = _live_work(connection, project_id)
    # AGENTS THAT WORK BUT ARE NOT REGISTERED ROLES.
    #
    # Found by the Hive having no edges: five of the seven role keys the process template
    # actually runs -- product_manager, engineering_captain, chief_architect, ux_engineer,
    # vp_product -- are not in the roles table at all. Only cto and qa overlap.
    #
    # So the screen was listing 21 registered roles, nearly all idle, while the agents doing
    # the work were invisible. Listing only registered roles is a view of the CONFIGURATION;
    # this screen is supposed to show the workforce.
    #
    # They are included and MARKED, because an agent running work without a registered role has
    # no trust score, no autonomy level and no violation tracking — a governance gap that
    # hiding the agent would also hide.
    registered = {r["role_key"] for r in roles}
    # Working = seen in the process steps OR credited with model calls. The second half is what
    # was missing: architect and reviewer do all their work through llm_calls and appeared
    # nowhere, while BOSS was busy attributing spend to them.
    # `live` BELONGS IN HERE. Without it an agent whose ONLY evidence is work in flight — a
    # panel seat, the research agent — is computed as working and then never rendered, because
    # the roster is built from finished work and this set is what adds the unregistered ones.
    # That is the difference between "shows IDLE" and "does not appear at all", and the panel
    # seats (engineer, ux_designer, privacy_safety, operations) are in the second category.
    working = ({s["role_key"] for s in steps if s.get("role_key")}
               | set(spend) | set(step_work) | set(live)
               | {h["source"] for h in handoffs if h.get("source")}
               | {h["target"] for h in handoffs if h.get("target")})
    for extra in sorted(working - registered):
        roles.append({
            "role_key": extra,
            "display_name": _humanise(extra),
            "autonomy_level": 0,
            "trust_score": 0,
            "successful_runs": 0,
            "policy_violation_count": 0,
            "hidden_failure_count": 0,
            "scope_violation_count": 0,
            "last_violation_at": None,
            "_unregistered": True,
        })

    agents = []
    for role in roles:
        key = role["role_key"]
        # A seeded role is a definition of who may work, not evidence that anyone has.
        # Keep it in the registry and Agent Studio; this feed describes actual work.
        if key not in working:
            continue
        step = latest.get(key)
        work = spend.get(key) or {}
        sw = step_work.get(key) or {}
        now_on = live.get(key)
        status, task = _status_of(step)
        # An agent with no process step but real model calls is not "no work in this window" --
        # it is the agent that did the building. Say what it did.
        if step is None and work.get("calls"):
            status, task = _status_from_work(work)
        # A run in flight beats every inference from finished work. This is the only signal that
        # is true WHILE the agent is busy rather than after it stops.
        if now_on:
            status = WORKING
            task = (f"{now_on['phase']} — {now_on['title']}" if now_on.get("title")
                    else now_on["phase"])
        agents.append({
            "id": key,
            "name": role.get("display_name") or key,
            "role": _humanise(key),
            "status": status,
            "task": task,
            # Absent, not zero. See the note above the (empty) spend lookup: BOSS does not
            # record which agent an llm_call belongs to, and a zero would read as "this agent
            # costs nothing" rather than "nobody knows".
            "model": work.get("model") or None,
            # Real numbers now, on the same basis as the cost ledger -- see the note above.
            "tokens": work.get("tokens"),
            "spend_usd": work.get("spend_usd"),
            "calls": work.get("calls"),
            "turns": work.get("turns"),
            "retries": work.get("retries"),
            "ok_calls": work.get("ok_calls"),
            "failed_calls": work.get("failed_calls"),
            # OBSERVED, and distinct from trust_score, which is a configured number that starts
            # at 50 and has never moved. One is what the agent has done; the other is what
            # someone decided about it.
            "success_rate": work.get("success_rate"),
            "top_stages": work.get("top_stages") or [],
            # Pipeline work, kept as its own figures rather than added to the call counts: a
            # step and a model call are different units and summing them would invent a total
            # that means nothing.
            "steps": sw.get("steps"),
            "steps_ok": sw.get("steps_ok"),
            "steps_failed": sw.get("steps_failed"),
            "runs": sw.get("runs"),
            "step_success_rate": sw.get("step_success_rate"),
            # WHEN this agent last did anything, of either kind. The screen needs one answer,
            # not two nullable ones, and it is what tells a busy agent apart from a retired
            # one -- both of which otherwise render as IDLE with a good score.
            "last_activity_at": _latest_activity(work, sw),
            "last_call_at": (work["last_at"].isoformat()
                             if work.get("last_at") else None),
            "last_stage": work.get("last_stage"),
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
            # Runs work, but has no registered role — so nothing governs it. Shown rather than
            # hidden: an ungoverned agent is precisely the one to know about.
            "registered": not role.get("_unregistered"),
            "last_seen_seconds": int(step["age_seconds"]) if step and step.get("age_seconds") else None,
            "run_id": step.get("run_id") if step else None,
            # WHICH PIECE OF WORK. The reference calls this the project; here it is the run the
            # agent's newest step belongs to, which is the same question.
            "project": _run_label(step) if step else None,
            # WHAT IT LAST TOUCHED. Read out of the step's own output rather than guessed, and
            # absent when the step recorded nothing -- "no artefact recorded" is a fact, and a
            # plausible filename would not be.
            "last_touched": _touched(step) if step else None,
        })

    # BUSIEST FIRST.
    #
    # Alphabetical put `cao` -- which has never made a call -- above the agent that did 16,000
    # turns of work, and the operator had to hunt for the agents that had actually done
    # something. Ordering by volume makes the page answer "who is doing the work" at a glance,
    # which is what it is for.
    #
    # Calls and steps are different units, so neither is converted into the other: an agent is
    # ranked on whichever kind of work it does. Ties fall back to spend, then turns, then name,
    # so the order is stable between renders rather than jittering as counts move.
    def _rank(a: dict) -> tuple:
        return (
            -(int(a.get("calls") or 0) + int(a.get("steps") or 0)),
            -(float(a.get("spend_usd") or 0.0)),
            -(int(a.get("turns") or 0)),
            str(a.get("name") or a.get("id") or ""),
        )

    agents.sort(key=_rank)

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
        "unregistered": sorted(a["id"] for a in agents if not a["registered"]),
        # Stated in the payload so a screen cannot quietly render a zero in its place.
        "spend_attributed": False,
        "spend_note": (
            "BOSS does not record which agent an LLM call belongs to, so per-agent cost cannot "
            "be shown. Project spend is on the Projects screen."
        ),
    }


def _latest_activity(call_work: dict, step_work: dict) -> Optional[str]:
    """The more recent of an agent's last model call and last pipeline step.

    An agent that worked ten minutes ago and one that stopped a month ago both render IDLE with
    a full scoreboard, and on a page about who is doing the work that is the difference that
    matters most.
    """
    stamps = [t for t in (call_work.get("last_at"), step_work.get("last_step_at")) if t]
    return max(stamps).isoformat() if stamps else None


def _step_work(connection, project_id: str, hours: float) -> dict[str, dict]:
    """What each agent did in the PROCESS pipeline, as opposed to through model calls.

    Two vocabularies, two tables, one workforce. The PRD pipeline -- product_manager,
    chief_architect, engineering_captain, ux_engineer, vp_product, cto -- records its work as
    agent_process_steps and never touches llm_calls under its own role, so scoring only model
    calls left six agents with a visible task and no score at all: doing work, credited with
    none of it.
    """
    rows = [dict(r) for r in connection.execute(
        sql(
            """
            SELECT s.role_key,
                   count(*)                                        AS steps,
                   count(*) FILTER (WHERE s.status = 'succeeded')  AS ok_steps,
                   count(*) FILTER (WHERE s.status IN ('failed', 'error')) AS failed_steps,
                   count(DISTINCT s.run_id)                        AS runs,
                   max(s.created_at)                               AS last_at
            FROM agent_process_steps s
            JOIN agent_process_runs r ON r.id = s.run_id
            WHERE s.project_id = :p
              AND s.created_at > now() - make_interval(hours => :h)
              AND s.role_key IS NOT NULL
              AND NOT (
                COALESCE(r.input->>'simulated', 'false') = 'true'
                AND COALESCE(r.input->>'auto_build', 'false') = 'false'
                AND (
                  COALESCE(r.input->>'origin', '') = 'process_manager_canary'
                  OR (COALESCE(r.input->>'origin', '') = ''
                      AND r.actor = 'system' AND r.title = 'PM simulated canary')
                )
              )
            GROUP BY s.role_key
            """
        ),
        {"p": project_id, "h": int(hours)},
    ).mappings().all()]

    out: dict[str, dict] = {}
    for row in rows:
        steps = int(row.get("steps") or 0)
        ok = int(row.get("ok_steps") or 0)
        failed = int(row.get("failed_steps") or 0)
        out[str(row["role_key"])] = {
            "steps": steps,
            "steps_ok": ok,
            "steps_failed": failed,
            "runs": int(row.get("runs") or 0),
            "last_step_at": row.get("last_at"),
            # Over COMPLETED steps only. A step still running is not a failure, and counting it
            # as one would make a busy agent look like a broken one.
            "step_success_rate": (round(ok / (ok + failed), 3) if (ok + failed) else None),
        }
    return out


#: A run's PHASE, in the vocabulary llm_calls uses for stages. The two lists overlap but are not
#: the same: a phase is where the pipeline is, a stage is what was billed.
_PHASE_STAGE = {
    "design": "plan", "write_tests": "write_tests", "implement": "implement",
    "run_tests": "check_tests", "review_panel": "review_code",
    "check_tests": "check_tests", "provisioning": "implement",
}


def _live_work(connection, project_id: str) -> dict[str, dict]:
    """Who is working RIGHT NOW, from runs in flight rather than from calls that finished.

    THE BUG THIS FIXES
    ------------------
    llm_calls rows are written when a call COMPLETES. An `implement` call runs for ten to thirty
    minutes, so for the whole time the agent is working hardest there is no row for it -- and a
    screen that infers "working" from completed calls therefore shows IDLE precisely then. The
    operator watched a build run through `implement` while all 26 agents read IDLE, which is the
    exact opposite of the truth.

    A run that is `running` in phase `implement` is an agent working, and BOSS knows that
    without waiting for anything to finish.
    """
    out: dict[str, dict] = {}

    for row in connection.execute(
        sql(
            """
            SELECT id, title, phase
            FROM code_task_runs
            WHERE project_id = :p AND status IN ('running', 'queued')
            ORDER BY updated_at DESC
            LIMIT 40
            """
        ),
        {"p": project_id},
    ).mappings().all():
        from .llm_telemetry import STAGE_ROLES, canonical_role

        stage = _PHASE_STAGE.get(str(row.get("phase") or "").strip().lower())
        role = canonical_role(STAGE_ROLES.get(stage or "")) if stage else None
        if not role:
            # A phase no agent owns -- queued, awaiting approval. Nobody is working on it, and
            # crediting the nearest role would put an agent to work that is not.
            continue
        out.setdefault(role, {
            "phase": str(row.get("phase") or ""),
            "run_id": row["id"],
            "title": str(row.get("title") or "")[:80],
        })

    # Process steps that have been started and not resolved.
    for row in connection.execute(
        sql(
            """
            SELECT DISTINCT ON (s.role_key) s.role_key, s.action, s.step_key, s.run_id
            FROM agent_process_steps s
            JOIN agent_process_runs r ON r.id = s.run_id
            WHERE s.project_id = :p
              AND s.status IN ('running', 'in_progress', 'pending')
              AND s.created_at > now() - interval '2 hours'
              AND NOT (
                COALESCE(r.input->>'simulated', 'false') = 'true'
                AND COALESCE(r.input->>'auto_build', 'false') = 'false'
                AND (
                  COALESCE(r.input->>'origin', '') = 'process_manager_canary'
                  OR (COALESCE(r.input->>'origin', '') = ''
                      AND r.actor = 'system' AND r.title = 'PM simulated canary')
                )
              )
            ORDER BY s.role_key, s.created_at DESC
            """
        ),
        {"p": project_id},
    ).mappings().all():
        out.setdefault(str(row["role_key"]), {
            "phase": str(row.get("action") or row.get("step_key") or "work"),
            "run_id": row.get("run_id"),
            "title": "",
        })

    # A PANEL IN SESSION IS FIVE AGENTS WORKING.
    #
    # Everything above reads `code_task_runs` and `agent_process_steps` — the CODING pipeline.
    # So an agent showed WORKING while it was writing code and IDLE while it sat on a panel
    # arguing a PRD, which is the operator's report: "the Agents weren't showing Working when
    # they are doing research or meeting panel, only when they are doing the coding task".
    #
    # A meeting run carries its seats in `role_keys`, so the panel already knows exactly who is
    # in the room. Every one of them is an agent doing work right now.
    for row in connection.execute(
        sql(
            """
            SELECT id, phase, directive, role_keys
            FROM meeting_runs
            WHERE project_id = :p AND status = 'running'
            ORDER BY updated_at DESC
            LIMIT 20
            """
        ),
        {"p": project_id},
    ).mappings().all():
        seats = row.get("role_keys")
        seats = seats if isinstance(seats, list) else []
        for seat in seats:
            key = str(seat or "").strip()
            if not key:
                continue
            out.setdefault(key, {
                "phase": f"panel · {str(row.get('phase') or 'in session')}",
                "run_id": row["id"],
                "title": str(row.get("directive") or "")[:80],
            })

    # RESEARCH IS AN AGENT WORKING TOO, and this one has no role recorded anywhere: discovery
    # runs a single research agent with no role key, which is why nothing could attribute it.
    #
    # Credited to `researcher` rather than to a registered agent chosen by resemblance. It will
    # appear as an agent with no registered role, which is exactly what it is — BOSS runs a
    # research agent that nothing in the registry governs. Picking the nearest registered role
    # instead would report a trust score and an autonomy level for work they did not do.
    for row in connection.execute(
        sql(
            """
            SELECT id, phase, title
            FROM discovery_runs
            WHERE project_id = :p AND status = 'running'
            ORDER BY updated_at DESC
            LIMIT 10
            """
        ),
        {"p": project_id},
    ).mappings().all():
        out.setdefault("researcher", {
            "phase": f"research · {str(row.get('phase') or 'running')}",
            "run_id": row["id"],
            "title": str(row.get("title") or "")[:80],
        })

    # THE AGENT ANSWERING THE OPERATOR. A dock turn that reads the database, the host and the
    # code is the same agent doing the same kind of work as any other; it was invisible here
    # because it belongs to none of the pipelines above.
    for row in connection.execute(
        sql(
            """
            SELECT id, step
            FROM agent_runs
            WHERE project_id = :p AND status = 'running'
              AND updated_at > now() - interval '30 minutes'
            ORDER BY updated_at DESC
            LIMIT 10
            """
        ),
        {"p": project_id},
    ).mappings().all():
        out.setdefault("metatron", {
            "phase": f"answering · step {row.get('step') or 0}",
            "run_id": row["id"],
            "title": "",
        })

    return out


def _status_from_work(work: dict) -> tuple[str, str]:
    """Status for an agent whose work is model calls rather than process steps.

    Recency decides WORKING vs IDLE, because a call that finished an hour ago is not work in
    progress -- and a screen where everything says WORKING says nothing.
    """
    import datetime as _dt

    last = work.get("last_at")
    stage = work.get("last_stage") or "work"
    if last is not None:
        try:
            age = (_dt.datetime.now(_dt.timezone.utc) - last).total_seconds()
        except TypeError:
            age = None
        if age is not None and age < 300:
            return WORKING, stage
    n = work.get("calls") or 0
    # "in this window" was misleading once the scoring window widened to 30 days -- the reader
    # takes "window" to mean the 24h the rest of the screen talks about.
    return IDLE, f"last: {stage} · {n} call{'' if n == 1 else 's'} in 30d"


#: How far back an agent's SCORE looks. Deliberately much longer than the live-status window.
#:
#: With both at 24h the live page showed one agent with a scoreboard and twenty-five blank
#: cards, because only one stage had run that day. A card reading "no calls recorded" for an
#: agent that has reviewed a hundred changes this month is not a quiet day, it is a wrong
#: answer -- and this page exists to measure agents, which is a question about a track record
#: rather than about this afternoon.
SCORE_WINDOW_HOURS = 720   # 30 days


def _llm_work(connection, project_id: str, hours: float) -> dict[str, dict]:
    """What each agent actually did, from the model calls attributed to its stages.

    Grouped by stage and then folded into roles through STAGE_ROLES, so a role that owns four
    stages shows one total rather than four rows the reader has to add up.

    `hours` is the SCORING window, not the status window. Status still comes from how recently
    the agent last ran -- an agent that worked all month and stopped an hour ago is idle now,
    and its card should say both things.
    """
    from .llm_telemetry import STAGE_ROLES, canonical_role

    rows = [dict(r) for r in connection.execute(
        sql(
            """
            SELECT stage, model,
                   count(*)                        AS calls,
                   COALESCE(sum(turns), 0)         AS turns,
                   COALESCE(sum(total_tokens), 0)  AS tokens,
                   COALESCE(sum(cost_usd_micros),0) AS micros,
                   COALESCE(sum(retries), 0)       AS retries,
                   count(*) FILTER (WHERE ok)      AS ok_calls,
                   max(created_at)                 AS last_at
            FROM llm_calls
            WHERE project_id = :p
              AND created_at > now() - make_interval(hours => :h)
            GROUP BY stage, model
            """
        ),
        {"p": project_id, "h": int(hours)},
    ).mappings().all()]

    out: dict[str, dict] = {}
    for row in rows:
        stage = str(row.get("stage") or "").strip().lower()
        role = canonical_role(STAGE_ROLES.get(stage))
        if not role:
            # A stage nobody owns. Counted nowhere rather than folded into a neighbour: an
            # agent credited with work it did not do is worse than a gap.
            continue
        agg = out.setdefault(role, {
            "calls": 0, "turns": 0, "tokens": 0, "micros": 0, "retries": 0, "ok_calls": 0,
            "last_at": None, "last_stage": None, "stages": {}, "_models": {},
        })
        agg["calls"] += int(row["calls"] or 0)
        agg["turns"] += int(row["turns"] or 0)
        agg["tokens"] += int(row["tokens"] or 0)
        agg["micros"] += int(row["micros"] or 0)
        agg["retries"] += int(row["retries"] or 0)
        agg["ok_calls"] += int(row["ok_calls"] or 0)
        agg["stages"][stage] = agg["stages"].get(stage, 0) + int(row["calls"] or 0)
        if row.get("model"):
            agg["_models"][row["model"]] = agg["_models"].get(row["model"], 0) + int(row["calls"] or 0)
        last = row.get("last_at")
        if last and (agg["last_at"] is None or last > agg["last_at"]):
            agg["last_at"] = last
            agg["last_stage"] = stage

    for role, agg in out.items():
        agg["spend_usd"] = round(agg["micros"] / 1_000_000, 4)
        agg["failed_calls"] = agg["calls"] - agg["ok_calls"]
        # None over zero calls: a rate with no denominator is not a score.
        agg["success_rate"] = (round(agg["ok_calls"] / agg["calls"], 3) if agg["calls"] else None)
        # The model it used MOST, not the first seen -- a single stray call on another model
        # should not relabel the agent.
        agg["model"] = (max(agg["_models"].items(), key=lambda kv: kv[1])[0]
                        if agg["_models"] else None)
        # Busiest stages first: what this agent mostly does.
        agg["top_stages"] = [
            {"stage": k, "calls": v}
            for k, v in sorted(agg["stages"].items(), key=lambda kv: -kv[1])[:4]
        ]
        agg.pop("_models", None)
        agg.pop("stages", None)
    return out


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


def _run_label(step: dict) -> Optional[str]:
    """The work this agent is on, named the way the operator would name it."""
    from .console_v2 import _clean_title

    title = _clean_title(str(step.get("run_title") or ""))
    return title or None


#: Keys a step's output may use for the thing it produced or changed. Checked in order; the
#: first that holds a string wins.
_TOUCHED_KEYS = (
    "artifact", "artifact_path", "path", "file", "document", "pr_url",
    "pull_request_url", "output_path", "target",
)


def _touched(step: dict) -> Optional[str]:
    """The last artefact this agent touched, if the step recorded one.

    None rather than a guess. A plausible-looking filename is worse than an admission, because
    it cannot be told apart from a real one.
    """
    output = step.get("output")
    if not isinstance(output, dict):
        return None
    for key in _TOUCHED_KEYS:
        value = output.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()[:80]
    return None


def _humanise(role_key: str) -> str:
    return " ".join(word.capitalize() for word in str(role_key or "").replace("-", "_").split("_"))


def _counts(agents: list[dict]) -> dict[str, int]:
    out = {WORKING: 0, STUCK: 0, IDLE: 0, GATED: 0}
    for a in agents:
        out[a["status"]] = out.get(a["status"], 0) + 1
    out["total"] = len(agents)
    return out


def _build_tree(nodes: list[dict], edges: list[dict]) -> list[dict]:
    """Lay the handoff graph out as a tree: roots, then who they hand to, breadth first.

    THE HIERARCHY IS OBSERVED, NOT DECLARED. BOSS has no parent field on a role, so a tree drawn
    from imagination would be an organisation that does not exist. This one is drawn from who
    actually passed work to whom — which is a real relationship, and arguably the more useful
    one: it shows how work flows rather than who someone once said reports to whom.

    A ROOT IS AN AGENT NOBODY HANDS TO. That is the top of a flow. Where a cycle means nothing
    qualifies, the busiest agent is used and the payload says so, because refusing to draw
    anything would be worse than drawing something explained.

    Each agent appears ONCE, at its shallowest depth. Work fans out and rejoins, so an agent can
    be reached by several paths; drawing it several times would inflate the org.
    """
    by_id = {n["id"]: n for n in nodes}
    children: dict[str, list[str]] = {}
    incoming: dict[str, int] = {}
    for e in edges:
        source, target = e["source"], e["target"]
        if source not in by_id or target not in by_id:
            continue
        children.setdefault(source, []).append(target)
        incoming[target] = incoming.get(target, 0) + 1

    connected = {e["source"] for e in edges if e["source"] in by_id}
    connected |= {e["target"] for e in edges if e["target"] in by_id}

    roots = sorted(
        (n for n in connected if not incoming.get(n)),
        key=lambda n: (-len(children.get(n, [])), by_id[n]["name"]),
    )
    inferred_root = False
    if not roots and connected:
        # Every agent is handed to by someone: a cycle. Take the busiest and say so, rather
        # than drawing nothing.
        roots = [max(connected, key=lambda n: len(children.get(n, [])))]
        inferred_root = True

    placed: set[str] = set()
    tree: list[dict] = []
    queue = [(r, 0, None) for r in roots]
    while queue:
        key, depth, parent = queue.pop(0)
        if key in placed:
            continue
        placed.add(key)
        tree.append({**by_id[key], "depth": depth, "parent": parent})
        for child in sorted(children.get(key, []), key=lambda c: by_id[c]["name"]):
            if child not in placed:
                queue.append((child, depth + 1, key))

    return tree, roots, inferred_root


def org_chart(connection, project_id: str, *, hours: int = 168) -> dict[str, Any]:
    """Who hands work to whom, laid out as a tree.

    BOSS has no declared reporting line. Inventing a hierarchy would draw an organisation that
    does not exist; deriving it from handoffs shows how work actually flows, which is both true
    and more useful. The screen repeats which it is.

    A week by default, because a day is too short to see the shape.
    """
    data = workforce(connection, project_id, hours=hours)
    agents = {a["id"]: a for a in data["agents"]}
    edges = data["handoffs"]

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

    tree, roots, inferred_root = _build_tree(nodes, edges)

    return {
        "project_id": project_id,
        "window_hours": int(hours),
        "nodes": nodes,
        "tree": tree,
        "roots": roots,
        # Said in the payload: a root chosen because everything cycles is not the same as a root
        # nobody hands to, and the screen must be able to tell the operator which it drew.
        "root_inferred": inferred_root,
        "depth": max((n["depth"] for n in tree), default=0) + 1 if tree else 0,
        "edges": edges,
        # Said in the payload so the screen cannot present this as a declared structure.
        "derived_from": "observed handoffs",
        "note": (
            "BOSS has no declared reporting line. This is who actually passed work to whom in "
            f"the last {int(hours)}h."
        ),
        "isolated": [n["id"] for n in nodes if not n["connected"]],
    }


def agent_detail(connection, project_id: str, role_key: str, *,
                 hours: float = SCORE_WINDOW_HOURS) -> dict[str, Any]:
    """One agent: what it is, what it has worked on, and what governs it.

    THE PAGE THAT DID NOT EXIST. An agent card in the roster was an `<article>` — not a link,
    not addressable, with no detail view behind it. Everything below was already in the database
    and none of it was reachable from the agent it describes:

      * which APPS it worked on, from the model calls attributed to its stages. The roster shows
        one `run_id` — the newest process step — for 7 of 26 agents; this is the whole history.
      * its SPEC: mission, boundaries, allowed and forbidden skills, output contracts, eval
        rubric. Twenty-two role YAMLs are installed into `roles.spec` and the console's
        `RoleFeedItem` drops the column entirely, so the richest capability model BOSS has was
        invisible.
      * what it is ASSIGNED to, which is a different question from what it has touched — and the
        gap between the two is worth looking at.
    """
    from .llm_telemetry import STAGE_ROLES, canonical_role
    from . import agent_assignment, skill_definitions

    role_key = canonical_role(role_key) or role_key
    role = connection.execute(
        sql("SELECT role_key, display_name, spec, autonomy_level, trust_score, successful_runs, "
            "policy_violation_count, scope_violation_count, hidden_failure_count, reports_to, "
            "last_reviewed_at, last_violation_at "
            "FROM roles WHERE project_id = :p AND role_key = :r"),
        {"p": project_id, "r": role_key},
    ).mappings().first()

    # THE STAGES THIS AGENT OWNS, which is what makes the app query answerable at all: llm_calls
    # records a stage, never a role, so the join runs through the same map BOSS attributes spend
    # with. Using anything else here would put an agent's work under a different agent's name.
    stages = sorted(s for s, r in STAGE_ROLES.items() if canonical_role(r) == role_key)

    apps: list[dict[str, Any]] = []
    if stages:
        rows = connection.execute(
            sql("""
                SELECT COALESCE(NULLIF(t.title, ''), 'untitled')   AS app_name,
                       count(*)                                    AS calls,
                       COALESCE(sum(c.total_tokens), 0)            AS tokens,
                       COALESCE(sum(c.cost_usd_micros), 0)         AS micros,
                       count(*) FILTER (WHERE c.ok)                AS ok_calls,
                       max(c.created_at)                           AS last_at,
                       array_agg(DISTINCT c.stage)                 AS stages
                FROM llm_calls c
                JOIN code_task_runs t ON t.id = c.code_task_run_id
                WHERE c.project_id = :p
                  AND c.stage = ANY(:stages)
                  AND c.created_at > now() - make_interval(hours => :h)
                GROUP BY 1
                ORDER BY max(c.created_at) DESC
                LIMIT 40
                """),
            {"p": project_id, "stages": stages, "h": int(hours)},
        ).mappings().all()
        from .naming import project_name
        for row in rows:
            # THE APP'S NAME, not the run's title. A title is the operator's whole directive —
            # "Build an incident timeline builder.\nWho it is for: …" — and a list of those is
            # unreadable. Same namer the rest of the console titles cards with.
            display = project_name(str(row["app_name"] or ""))[0].strip().rstrip(".").strip()
            apps.append({
                "app": display or str(row["app_name"] or "untitled")[:60],
                "raw_title": row["app_name"],
                "calls": int(row["calls"] or 0),
                "tokens": int(row["tokens"] or 0),
                "spend_usd": round(int(row["micros"] or 0) / 1_000_000, 4),
                "ok_calls": int(row["ok_calls"] or 0),
                "last_at": row["last_at"].isoformat() if row["last_at"] else None,
                "stages": sorted(s for s in (row["stages"] or []) if s),
            })

    # PANELS ARE WORK TOO. An agent that sat on six PRD panels and wrote no code would otherwise
    # show an empty history, which is the same blindness the workforce screen had.
    panels = [dict(r) for r in connection.execute(
        sql("""
            SELECT id, directive, status, phase, updated_at
            FROM meeting_runs
            WHERE project_id = :p AND role_keys ? :r
            ORDER BY updated_at DESC LIMIT 20
            """),
        {"p": project_id, "r": role_key},
    ).mappings().all()]

    spec = role["spec"] if role and isinstance(role["spec"], dict) else {}
    return {
        "project_id": project_id,
        "role_key": role_key,
        "name": (role["display_name"] if role else None) or _humanise(role_key),
        # An agent doing work with no `roles` row has no trust score, no autonomy level and
        # nothing governing it. Said plainly rather than rendered as zeros.
        "registered": bool(role),
        "autonomy_level": int(role["autonomy_level"] or 0) if role else None,
        "trust_score": int(role["trust_score"] or 0) if role else None,
        "successful_runs": int(role["successful_runs"] or 0) if role else None,
        "violations": ((int(role["policy_violation_count"] or 0)
                        + int(role["scope_violation_count"] or 0)) if role else None),
        "hidden_failures": int(role["hidden_failure_count"] or 0) if role else None,
        "reports_to": role["reports_to"] if role else None,
        "last_reviewed_at": (role["last_reviewed_at"].isoformat()
                             if role and role["last_reviewed_at"] else None),
        # THE CAPABILITY MODEL, surfaced. Written by spec packs, never once displayed.
        "spec": {
            "mission": spec.get("mission") or "",
            "primary_outcomes": spec.get("primary_outcomes") or [],
            "boundaries": spec.get("boundaries") or {},
            "decision_rights": spec.get("decision_rights") or [],
            "allowed_skills": ((spec.get("permissions") or {}).get("allowed_skills") or []),
            "forbidden_skills": ((spec.get("permissions") or {}).get("forbidden_skills") or []),
            "output_contracts": spec.get("output_contracts") or [],
            "eval_rubric": spec.get("eval_rubric") or {},
            "knowledge": spec.get("knowledge") or "",
        },
        "owns_stages": stages,
        "apps": apps,
        "panels": [{"id": p["id"], "subject": str(p["directive"] or "")[:120],
                    "status": p["status"], "phase": p["phase"],
                    "at": p["updated_at"].isoformat() if p["updated_at"] else None}
                   for p in panels],
        # What it is SUPPOSED to be on, which is not the same as what it has touched.
        "assigned_to": agent_assignment.apps_for_role(connection, project_id, role_key),
        # WHAT IT CAN ACTUALLY DO, which is not `spec.allowed_skills`.
        #
        # THE DIFFERENCE MATTERS AND THE PAGE USED TO SHOW ONLY THE WRONG ONE. `allowed_skills`
        # is prose in a spec pack: it says what an agent MAY be given. `role_skills` is the
        # grant: it is what the agent HOLDS, and it is what skill_runtime checks before letting
        # the agent call anything. An operator reading a page full of allowed skills over an
        # empty `role_skills` table sees a capable agent that can do nothing.
        "skills": skill_definitions.role_skill_detail(connection, project_id, role_key),
        # THE REST OF THE ONE AGENT MODEL (services/agents.py): the MCP servers it holds, the
        # workflow steps that name it — which is why it may not be deletable — and the Studio
        # profiles that sit on it. Read here so the detail page needs one request, not four.
        **_agent_model_extras(connection, project_id, role_key, registered=bool(role)),
        "window_hours": hours,
    }


def _agent_model_extras(connection, project_id: str, role_key: str, *,
                        registered: bool) -> dict:
    """MCP holdings, the steps naming the agent, and its Studio profiles — see agent_detail."""
    from . import agents

    if not registered:
        return {"mcp": {"role_key": role_key, "holds": [], "available": []},
                "named_on_steps": [], "studio_profiles": []}
    return {
        "mcp": agents.role_mcp_detail(connection, project_id, role_key),
        "named_on_steps": agents.steps_naming(connection, project_id, role_key),
        "studio_profiles": [
            {"id": p["id"], "agent_key": p["agent_key"], "name": p["name"],
             "status": p["status"]}
            for p in agents.studio_profiles(connection, project_id, role_key)
        ],
    }
