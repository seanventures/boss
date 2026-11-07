"""What a run of each workflow has cost in this project, from its own history.

The Start form asked an operator to choose between a quick change and a nine-step workflow with
nothing to go on but the step count. The real numbers were already recorded, and this adds them
up per run and reports a range (20th to 80th percentile) per workflow: a range, because one run
is an anecdote and a mean is dragged by the one that went round its repair loop five times.

WHAT A RUN COST is everything spent on the work the operator started, wherever it was recorded:

* the builds — every model call (`llm_calls`) of every code task the run governed
  (`finalize_ctx.workflow_run_id`);
* the panel — a meeting's spend lives on the meeting (`meeting_runs.usage.cost_usd_micros`), not
  in `llm_calls`; it belongs to the run that convened it (`finalize_ctx.origin.workflow_run_id`)
  or whose step waited on it (the step's output names it);
* the research — a discovery's spend lives on `discovery_runs.usage`, linked the same way.

Only COMPLETED runs: a run stopped half-way is not the price of the workflow, and one still
running has not finished spending.

NOT the system's own loops. A Metatron pass, a captain swarm build, a shepherd fix task is not
the price of anything an operator starts (work_provenance.AUTONOMOUS_SOURCES); their spend is
reported once, separately, under `autonomous`.

`@change` is the same figure for a quick change — a follow-up, or a retry carrying a person's
feedback, that no workflow governed. An automatic gate or platform retry (a shepherd or
review-repair note, or a job the system queued at BACKGROUND) is not a change anybody asked for.

`@spec` prices "spec it first": a panel convened about an existing app (`constraints.
extends_run_id`, or the console's `[boss:extend_run=…]` marker) plus the build it produced.
Until a project has one, it falls back to PRD panels and the first follow-up build each produced
(`basis` says which).

Subscription-only history still has a number: `cost_usd_micros` is the API-equivalent value of
a call that ran on a subscription (included_spend.py). `metered=False` says nobody was billed it.
"""
from __future__ import annotations

from typing import Any

from sqlalchemy import text

from .jobs import JOB_PRIORITY_BACKGROUND
from .work_provenance import AUTOMATIC_RETRY_NOTE_PREFIXES, AUTONOMOUS_SOURCES

#: The pseudo-workflow for changes made without one.
CHANGE_KEY = "@change"
#: The pseudo-workflow for a panel-specced change to an existing app.
SPEC_KEY = "@spec"

_AUTONOMOUS = sorted(AUTONOMOUS_SOURCES)
_AUTO_NOTES = [prefix.replace("%", r"\%").replace("_", r"\_") + "%"
               for prefix in AUTOMATIC_RETRY_NOTE_PREFIXES]

#: A code task the system started on its own: its recorded source, or the band its job was
#: queued in when it was created.
_STARTED_BY_SYSTEM = """
    (COALESCE(c.finalize_ctx->>'source', '') = ANY(CAST(:auto AS text[]))
     OR EXISTS (SELECT 1 FROM jobs j
                WHERE j.kind = 'code.task' AND j.payload->>'run_id' = c.id
                  AND j.priority >= :background))
"""

#: A meeting's cost is money only when a receipt says it was billed.
_MEETING_METERED = """
    EXISTS (SELECT 1 FROM cost_receipts cr
            WHERE cr.reservation_id = m.finalize_ctx->>'reservation_id'
              AND NOT cr.included_in_subscription
              AND COALESCE(cr.actual_usd_micros, 0) > 0)
"""


def _percentile(sorted_values: list[float], q: float) -> float:
    """Linear interpolation between closest ranks; one value is its own every percentile."""
    if len(sorted_values) == 1:
        return sorted_values[0]
    pos = (len(sorted_values) - 1) * q
    lo = int(pos)
    hi = min(lo + 1, len(sorted_values) - 1)
    return sorted_values[lo] + (sorted_values[hi] - sorted_values[lo]) * (pos - lo)


def _params(project_id: str) -> dict[str, Any]:
    return {"p": project_id, "auto": _AUTONOMOUS, "background": JOB_PRIORITY_BACKGROUND,
            "auto_notes": _AUTO_NOTES}


def _workflow_runs(connection: Any, project_id: str) -> list[tuple[str, int, bool]]:
    """(workflow_key, micros, metered) per completed run that spent anything."""
    rows = connection.execute(text(
        f"""
        WITH runs AS (
            SELECT r.id, d.workflow_key AS key
            FROM workflow_runs r
            JOIN workflow_definitions d ON d.id = r.workflow_definition_id
            WHERE r.project_id = :p AND r.status = 'completed' AND r.mode <> 'test'
        ),
        -- Whatever a step started and waited on, by the id its output names.
        step_ids AS (
            SELECT DISTINCT s.workflow_run_id AS run_id, v.id
            FROM workflow_step_runs s
            JOIN runs ON runs.id = s.workflow_run_id
            CROSS JOIN LATERAL (VALUES
                (s.output->>'meeting_run_id'), (s.output->>'discovery_id'),
                (s.output->>'run_id'), (s.output->'meeting'->>'run_id'),
                (s.output->'discovery'->>'run_id'), (s.output->'await'->>'id')
            ) AS v(id)
            WHERE COALESCE(v.id, '') <> ''
        ),
        build AS (
            SELECT runs.id AS run_id, SUM(l.cost_usd_micros) AS micros,
                   BOOL_OR(NOT l.included_in_subscription) AS metered
            FROM runs
            JOIN code_task_runs c ON c.project_id = :p
                                 AND c.finalize_ctx->>'workflow_run_id' = runs.id
            JOIN llm_calls l ON l.code_task_run_id = c.id
            WHERE NOT {_STARTED_BY_SYSTEM}
            GROUP BY runs.id
        ),
        panel AS (
            SELECT runs.id AS run_id,
                   SUM(COALESCE((m.usage->>'cost_usd_micros')::bigint, 0)) AS micros,
                   BOOL_OR({_MEETING_METERED}) AS metered
            FROM runs
            JOIN meeting_runs m
              ON m.project_id = :p
             AND (m.finalize_ctx->'origin'->>'workflow_run_id' = runs.id
                  OR m.id IN (SELECT si.id FROM step_ids si WHERE si.run_id = runs.id))
            GROUP BY runs.id
        ),
        research AS (
            SELECT si.run_id,
                   SUM(COALESCE((dr.usage->>'cost_usd_micros')::bigint, 0)) AS micros
            FROM step_ids si
            JOIN discovery_runs dr ON dr.id = si.id AND dr.project_id = :p
            GROUP BY si.run_id
        )
        SELECT runs.key,
               COALESCE(b.micros, 0) + COALESCE(pn.micros, 0) + COALESCE(rs.micros, 0) AS micros,
               COALESCE(b.metered, false) OR COALESCE(pn.metered, false) AS metered
        FROM runs
        LEFT JOIN build b ON b.run_id = runs.id
        LEFT JOIN panel pn ON pn.run_id = runs.id
        LEFT JOIN research rs ON rs.run_id = runs.id
        """
    ), _params(project_id)).mappings()
    # A run that spent nothing (every build still queued, a dry run) is not evidence of what
    # the workflow costs, and would pull the low end to zero.
    return [(str(r["key"]), int(r["micros"] or 0), bool(r["metered"]))
            for r in rows if int(r["micros"] or 0) > 0]


def _spec_samples(connection: Any, project_id: str, spec_first: bool) -> list[Any]:
    if spec_first:
        which = ("(COALESCE(m.constraints->>'extends_run_id', '') <> '' "
                 " OR m.directive LIKE '%[boss:extend_run=crun%')")
    else:
        which = "COALESCE(m.constraints->>'meeting_type', 'prd_panel') = 'prd_panel'"
    # The build a panel produced: the first build after it that names it, or — for a panel about
    # an existing app — the first follow-up on that app after it. Only the FIRST: a follow-up
    # inherits its parent's origin, so later quick changes on the same app name the same panel
    # and are not its build.
    link = ("(c.finalize_ctx->'origin'->>'meeting_run_id' = m.id"
            " OR c.finalize_ctx->>'meeting_run_id' = m.id")
    if spec_first:
        link += (" OR (c.finalize_ctx->>'is_followup' = 'true'"
                 "     AND c.finalize_ctx->>'parent_run_id' = m.constraints->>'extends_run_id')")
    link += ")"
    if not spec_first:
        link = f"({link} AND c.finalize_ctx->>'is_followup' = 'true')"
    return connection.execute(text(
        f"""
        SELECT m.id AS meeting_id,
               COALESCE((m.usage->>'cost_usd_micros')::bigint, 0) AS panel_micros,
               {_MEETING_METERED} AS panel_metered,
               b.id AS build_id, b.micros AS build_micros, b.metered AS build_metered
        FROM meeting_runs m
        CROSS JOIN LATERAL (
            SELECT c.id,
                   (SELECT COALESCE(SUM(l.cost_usd_micros), 0) FROM llm_calls l
                    WHERE l.code_task_run_id = c.id) AS micros,
                   (SELECT COALESCE(BOOL_OR(NOT l.included_in_subscription), false)
                    FROM llm_calls l WHERE l.code_task_run_id = c.id) AS metered
            FROM code_task_runs c
            WHERE c.project_id = m.project_id
              AND c.created_at >= m.created_at
              AND {link}
              AND NOT {_STARTED_BY_SYSTEM}
            -- A build that names this panel beats one that merely follows it on the app.
            ORDER BY (c.finalize_ctx->'origin'->>'meeting_run_id' = m.id
                      OR c.finalize_ctx->>'meeting_run_id' = m.id) DESC NULLS LAST,
                     c.created_at ASC
            LIMIT 1
        ) AS b
        WHERE m.project_id = :p AND m.status = 'completed'
          AND COALESCE(m.finalize_ctx->'origin'->>'workflow_run_id', '') = ''
          AND {which}
        """
    ), _params(project_id)).mappings().all()


def _spec_first(connection: Any, project_id: str) -> tuple[list[tuple[int, bool]], str, set[str]]:
    """([(micros, metered)] per panel + its build, basis, the build ids priced here)."""
    basis = "spec_first"
    rows = _spec_samples(connection, project_id, True)
    if not rows:
        basis, rows = "prd_panel", _spec_samples(connection, project_id, False)
    out: list[tuple[int, bool]] = []
    builds: set[str] = set()
    for r in rows:
        micros = int(r["panel_micros"] or 0) + int(r["build_micros"] or 0)
        if micros <= 0:
            continue
        out.append((micros, bool(r["panel_metered"]) or bool(r["build_metered"])))
        builds.add(str(r["build_id"]))
    return out, basis, builds


def _changes(connection: Any, project_id: str, exclude: set[str]) -> list[tuple[int, bool]]:
    """A quick change: a follow-up on an app, or a retry carrying a PERSON's feedback, that no
    workflow governed and the system did not start. A retry with no note is a re-run of failed
    work; one with a shepherd or review-repair note is a gate repair; neither is a change."""
    rows = connection.execute(text(
        f"""
        SELECT c.id, SUM(l.cost_usd_micros) AS micros,
               BOOL_OR(NOT l.included_in_subscription) AS metered
        FROM code_task_runs c
        JOIN llm_calls l ON l.code_task_run_id = c.id
        WHERE c.project_id = :p
          AND COALESCE(c.finalize_ctx->>'workflow_run_id', '') = ''
          AND NOT {_STARTED_BY_SYSTEM}
          AND (c.finalize_ctx->>'is_followup' = 'true'
               OR (COALESCE(c.finalize_ctx->>'attempt', '1') ~ '^[0-9]+$'
                   AND (c.finalize_ctx->>'attempt')::int > 1
                   AND COALESCE(c.finalize_ctx->'feedback_history'->-1->>'fix_note', '') <> ''
                   AND NOT (COALESCE(c.finalize_ctx->'feedback_history'->-1->>'fix_note', '')
                            LIKE ANY (CAST(:auto_notes AS text[])))))
        GROUP BY c.id
        """
    ), _params(project_id)).mappings()
    return [(int(r["micros"] or 0), bool(r["metered"])) for r in rows
            if str(r["id"]) not in exclude]


def _autonomous(connection: Any, project_id: str) -> dict[str, Any]:
    """What the system's own loops spent here — reported, never mixed into a price."""
    row = connection.execute(text(
        f"""
        SELECT COUNT(DISTINCT c.id) AS runs, COALESCE(SUM(l.cost_usd_micros), 0) AS micros,
               COALESCE(BOOL_OR(NOT l.included_in_subscription), false) AS metered
        FROM code_task_runs c
        JOIN llm_calls l ON l.code_task_run_id = c.id
        WHERE c.project_id = :p AND {_STARTED_BY_SYSTEM}
        """
    ), _params(project_id)).mappings().first() or {}
    return {"runs": int(row.get("runs") or 0),
            "usd": round(int(row.get("micros") or 0) / 1_000_000, 4),
            "metered": bool(row.get("metered"))}


def _summarise(runs: list[tuple[int, bool]]) -> dict[str, Any]:
    usd = sorted(m / 1_000_000 for m, _ in runs)
    return {
        "runs": len(runs),
        "low_usd": round(_percentile(usd, 0.2), 4),
        "high_usd": round(_percentile(usd, 0.8), 4),
        # The typical run, for a single "≈ $3" (the Work tab's plan): the median.
        "median_usd": round(_percentile(usd, 0.5), 4),
        "metered": any(metered for _, metered in runs),
    }


def workflow_costs(connection: Any, project_id: str) -> dict[str, Any]:
    """{project_id, costs: {workflow_key: {runs, low_usd, high_usd, metered}}, autonomous};
    keys with no history are omitted. A handful of queries, whatever the number of runs."""
    grouped: dict[str, list[tuple[int, bool]]] = {}
    for key, micros, metered in _workflow_runs(connection, project_id):
        grouped.setdefault(key, []).append((micros, metered))
    spec_runs, basis, spec_builds = _spec_first(connection, project_id)
    if spec_runs:
        grouped[SPEC_KEY] = spec_runs
    change_runs = _changes(connection, project_id, spec_builds)
    if change_runs:
        grouped[CHANGE_KEY] = change_runs
    costs = {key: _summarise(runs) for key, runs in grouped.items()}
    if SPEC_KEY in costs:
        costs[SPEC_KEY]["basis"] = basis
    return {"project_id": project_id, "costs": costs,
            "autonomous": _autonomous(connection, project_id)}
