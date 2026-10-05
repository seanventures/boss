"""What a run of each workflow has cost in this project, from its own history.

The Start form asked an operator to choose between a quick change and a nine-step workflow with
nothing to go on but the step count. The real numbers were already recorded — every model call
is in `llm_calls`, tied to the code task it served, and every code task a workflow started names
that run in `finalize_ctx.workflow_run_id`. This adds them up per run and reports a range
(20th to 80th percentile) per workflow: a range, because one run is an anecdote and a mean is
dragged by the one that went round its repair loop five times.

`@change` is the same figure for a quick change — a follow-up or a feedback retry that no
workflow governed — so the two choices can be compared on one scale.

Subscription-only history still has a number: `cost_usd_micros` is the API-equivalent value of
a call that ran on a subscription (included_spend.py). `metered=False` says nobody was billed it.
"""
from __future__ import annotations

from typing import Any

from sqlalchemy import text

#: The pseudo-workflow for changes made without one.
CHANGE_KEY = "@change"


def _percentile(sorted_values: list[float], q: float) -> float:
    """Linear interpolation between closest ranks; one value is its own every percentile."""
    if len(sorted_values) == 1:
        return sorted_values[0]
    pos = (len(sorted_values) - 1) * q
    lo = int(pos)
    hi = min(lo + 1, len(sorted_values) - 1)
    return sorted_values[lo] + (sorted_values[hi] - sorted_values[lo]) * (pos - lo)


def workflow_costs(connection: Any, project_id: str) -> dict[str, Any]:
    """{project_id, costs: {workflow_key: {runs, low_usd, high_usd, metered}}}; keys with no
    history are omitted. Two queries, whatever the number of runs."""
    per_run: list[tuple[str, int, bool]] = []
    # One row per workflow run that spent anything: the sum of the calls of every code task
    # it governed. A run with no calls (stopped before the build, or every build still queued)
    # is not evidence of what the workflow costs, and would pull the low end to zero.
    for row in connection.execute(text(
        """
        SELECT d.workflow_key AS key, SUM(l.cost_usd_micros) AS micros,
               BOOL_OR(NOT l.included_in_subscription) AS metered
        FROM workflow_runs r
        JOIN workflow_definitions d ON d.id = r.workflow_definition_id
        JOIN code_task_runs c ON c.project_id = r.project_id
                             AND c.finalize_ctx->>'workflow_run_id' = r.id
        JOIN llm_calls l ON l.code_task_run_id = c.id
        WHERE r.project_id = :p
        GROUP BY r.id, d.workflow_key
        """
    ), {"p": project_id}).mappings():
        per_run.append((str(row["key"]), int(row["micros"] or 0), bool(row["metered"])))
    # A quick change: a follow-up on an app, or a retry carrying someone's feedback, that no
    # workflow governed. A retry with no note is a re-run of failed work, not a change.
    for row in connection.execute(text(
        """
        SELECT SUM(l.cost_usd_micros) AS micros,
               BOOL_OR(NOT l.included_in_subscription) AS metered
        FROM code_task_runs c
        JOIN llm_calls l ON l.code_task_run_id = c.id
        WHERE c.project_id = :p
          AND COALESCE(c.finalize_ctx->>'workflow_run_id', '') = ''
          AND (c.finalize_ctx->>'is_followup' = 'true'
               OR (COALESCE(c.finalize_ctx->>'attempt', '1') ~ '^[0-9]+$'
                   AND (c.finalize_ctx->>'attempt')::int > 1
                   AND COALESCE(c.finalize_ctx->'feedback_history'->-1->>'fix_note', '') <> ''))
        GROUP BY c.id
        """
    ), {"p": project_id}).mappings():
        per_run.append((CHANGE_KEY, int(row["micros"] or 0), bool(row["metered"])))

    grouped: dict[str, list[tuple[int, bool]]] = {}
    for key, micros, metered in per_run:
        grouped.setdefault(key, []).append((micros, metered))
    costs: dict[str, dict[str, Any]] = {}
    for key, runs in grouped.items():
        usd = sorted(m / 1_000_000 for m, _ in runs)
        costs[key] = {
            "runs": len(runs),
            "low_usd": round(_percentile(usd, 0.2), 4),
            "high_usd": round(_percentile(usd, 0.8), 4),
            "metered": any(metered for _, metered in runs),
        }
    return {"project_id": project_id, "costs": costs}
