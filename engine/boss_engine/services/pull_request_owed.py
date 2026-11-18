"""Does a build that passed its gates owe a pull request, and if not, why — in words.

THE CASE (2026-11-18, laptop, crun_5b11643d): an Easy Mode build on a local repository passed
every gate and ended `completed/done` with no pull request, no merge card and nothing on the run
saying why. Easy Mode had started it with `open_pr=False` (meant for evaluation runs), and the
governed lane's no-PR path wrote only an event.

ONE DECISION for every place that asks (the gate disposition and the delivery step):

  owed(spec, finalize_ctx) -> None                 the build opens its PR and its merge card
                           -> {reason, words}      it does not, and the run records why

A completed build with no PR always carries `finalize_ctx.no_pull_request` ({reason, words}).
The paths that skip the PR on purpose write their own; db/schema.sql's trigger
`code_task_runs_no_silent_completion` writes one for any path that did not, so "completed, no
PR, no reason" cannot be stored.
"""
from __future__ import annotations

import json
from typing import Any, Optional

#: finalize_ctx key a completed run without a pull request carries.
KEY = "no_pull_request"


def nonfinal_slice(finalize_ctx: Any) -> Optional[tuple[int, int]]:
    """(index, total) when the run is a non-final slice of a decomposed build, else None."""
    ctx = finalize_ctx if isinstance(finalize_ctx, dict) else {}
    idx, total = ctx.get("decomposition_index"), ctx.get("decomposition_total")
    if isinstance(idx, int) and isinstance(total, int) and total > 1 and idx < total:
        return idx, total
    return None


def owed(spec: Any, finalize_ctx: Any, *, branch: str = "") -> Optional[dict[str, str]]:
    """None when the build owes its pull request; else why it does not, as {reason, words}."""
    on = f" Its work is on branch {branch}." if branch else ""
    sliced = nonfinal_slice(finalize_ctx)
    if sliced is not None:
        return {"reason": "decomposition_slice",
                "words": f"This is part {sliced[0]} of {sliced[1]} of one build: its pull request "
                         f"opens with the last part, on the same branch.{on}"}
    if isinstance(spec, dict) and spec.get("open_pr", True) is False:
        return {"reason": "open_pr_false",
                "words": "This build was started not to open a pull request (an evaluation run), "
                         f"so there is no merge card.{on}"}
    return None


def record_clause(param: str = "no_pr") -> str:
    """The SET fragment that records `owed(...)`'s answer on the run, bound as `:param`."""
    return (f"finalize_ctx = COALESCE(finalize_ctx, '{{}}'::jsonb) || "
            f"jsonb_build_object('{KEY}', CAST(:{param} AS JSONB))")


def record_value(why: dict[str, str]) -> str:
    return json.dumps(why)
