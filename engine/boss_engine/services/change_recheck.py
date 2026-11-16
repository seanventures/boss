"""Re-check this change: every gate again on the change's branch head, no code written
(BOSSAPP-1326).

A finished build (its pull request open) had no way to run its checks again after a platform
fix: "Review this PR again" re-runs only the review seat, and Retry writes code. Northgate's
change #2 kept a false 0 met / 30 missing acceptance table (BOSSAPP-1323) until another build
happened to run.

A re-check is a NEW ATTEMPT of the same lineage (retry_code_task with `recheck=True`), marked
`recheck_only` on the run. The governed pipeline reads the mark and:
  * clones the change's branch head (never a saved workspace) and skips the design chain and
    write_tests;
  * gives every WRITING stage (implement, write_tests, the fix rounds that use them) a
    `NoWriteAgent`, which writes nothing and costs nothing — structural, not a prompt;
  * runs every gate as a build does: the tests, the review panel (review, security,
    consistency), the acceptance check;
  * never starts a repair round (a failing gate goes to stop_or_open: the PR is labelled and
    the existing rules decide about a follow-up — the re-check opens none of its own);
  * commits nothing: the pull request is the same one, its card is the new verdict, and the
    older cards for it are withdrawn (run_decisions.withdraw_superseded_merge_cards).
Its acceptance table joins the run's history like any check's.

`request` is the one door: the API (POST /v1/code-tasks/{id}/recheck), the MCP tool
`recheck_change` and the console's "Re-check this change". It needs the steer permission and
says its estimated cost before it starts (`estimate`).
"""
from __future__ import annotations

from typing import Any, Mapping, Optional

#: The finalize_ctx mark the governed pipeline reads.
KEY = "recheck_only"
#: The run it re-checks.
OF_KEY = "recheck_of"
#: Stages that write code. A re-check's agent for each of them writes nothing.
WRITING_STAGES = frozenset({"implement", "write_tests", "solo", "repair"})
#: What a re-check runs, in the words the console shows.
GATES = ("tests", "security", "review", "consistency", "acceptance")
#: The stages whose recorded spend estimates a re-check (the gates' seats, not the coding).
CHECK_STAGES = ("review_panel", "review_code", "security_check", "consistency_check",
                "acceptance_check", "check_tests", "quality")
#: When the lineage recorded no seat spend to go by.
DEFAULT_ESTIMATE_USD = 3.0
NOTE = ("RE-CHECK: run every check again on this branch's head after a platform fix. Write no "
        "code: this attempt only judges the change as it stands.")


class RecheckRefused(Exception):
    """This build cannot be re-checked, in words."""


class NoWriteAgent:
    """The agent a re-check gives every writing stage: it writes nothing and costs nothing.
    Whatever asks it to build gets "no code was written" — the gates then judge the branch head
    as it is."""

    engine = "recheck"

    def __init__(self, step: str):
        from .engine_contract import EngineCapabilities

        self.step = step
        self.capabilities = EngineCapabilities()
        self.calls = 0

    def run_role(self, prompt: str, cwd: Any, *, read_only: bool = False, **_: Any) -> dict:
        self.calls += 1
        return {"summary": f"Re-check: no code is written ({self.step}).", "usage": {},
                "cost_usd_micros": 0, "turns": 0, "model": None, "session_id": None,
                "stop_reason": "complete"}

    def run(self, spec: Any, repo: Any, sandbox: Any) -> Any:
        raise RecheckRefused("a re-check writes no code")

    def __getattr__(self, name: str) -> Any:
        # Optional knobs the pipeline sets or reads on an agent (redact, budget_ceiling, …).
        if name.startswith("__"):
            raise AttributeError(name)
        return None


def is_recheck(fctx: Optional[Mapping[str, Any]]) -> bool:
    return bool((fctx or {}).get(KEY))


def refusal(run: Optional[Mapping[str, Any]]) -> Optional[str]:
    """Why this run cannot be re-checked, or None: it must have finished with its pull request
    open (a running build is still checking; a build with no PR has no change to re-check)."""
    if not run:
        return "no such build"
    if str(run.get("status") or "") in ("queued", "running", "repairing", "waiting_on_person"):
        return "this build is still running; its checks are still to come"
    if not run.get("pull_request_url"):
        return "this build has no pull request: there is no change to re-check"
    merge = (run.get("finalize_ctx") or {}).get("merge")
    if isinstance(merge, Mapping) and str(merge.get("sha") or "").strip():
        return "this change is already merged"
    return None


def estimate(connection: Any, run: Mapping[str, Any]) -> dict:
    """The estimated cost of a re-check: what the gates' seats cost on the newest attempt of
    this lineage that ran them (llm_calls), else DEFAULT_ESTIMATE_USD. {"usd", "basis"}."""
    from sqlalchemy import bindparam, text

    fctx = run.get("finalize_ctx") or {}
    root = str(fctx.get("root_run_id") or run.get("id") or "")
    try:
        with connection.begin_nested():
            row = connection.execute(text(
                "SELECT l.code_task_run_id AS rid, SUM(l.cost_usd_micros) AS micros "
                "FROM llm_calls l JOIN code_task_runs r ON r.id = l.code_task_run_id "
                "WHERE r.project_id = :p AND (r.id = :root OR r.finalize_ctx->>'root_run_id' = :root) "
                "AND l.stage IN :stages GROUP BY l.code_task_run_id, r.created_at "
                "ORDER BY r.created_at DESC LIMIT 1").bindparams(
                    bindparam("stages", expanding=True)),
                {"p": run.get("project_id"), "root": root,
                 "stages": list(CHECK_STAGES)}).mappings().first()
    except Exception:  # noqa: BLE001 — no ledger to read: the default estimate
        row = None
    if row and int(row["micros"] or 0) > 0:
        return {"usd": round(int(row["micros"]) / 1_000_000, 2),
                "basis": f"what the checks cost on {row['rid']}"}
    return {"usd": DEFAULT_ESTIMATE_USD, "basis": "no earlier check spend recorded"}


def preview(connection: Any, run_id: str, project_id: str) -> dict:
    """What the console shows before the button is pressed: can it, why not, what it costs."""
    from .code_task_service import load_code_task_run

    run = load_code_task_run(connection, run_id)
    if not run or run.get("project_id") != project_id:
        raise RecheckRefused("no such build in this project")
    why = refusal(run)
    return {"run_id": run_id, "can": why is None, "why": why or "", "gates": list(GATES),
            "estimate": estimate(connection, run)}


def request(engine: Any, settings: Any, jobs: Any, run_id: str, *, project_id: str,
            actor: str, by_person: bool = False) -> dict:
    """Start the re-check: a new attempt of the lineage, marked `recheck_only`. Refused, in
    words, when the build cannot be re-checked or another run already works on its branch."""
    from ..db import begin
    from .code_task_service import RetryRefused, load_code_task_run, retry_code_task
    from .core import emit_event

    with begin(engine) as c:
        run = load_code_task_run(c, run_id)
        if not run or run.get("project_id") != project_id:
            raise RecheckRefused("no such build in this project")
        why = refusal(run)
        if why:
            raise RecheckRefused(why)
        cost = estimate(c, run)
    try:
        started = retry_code_task(engine, settings, jobs, run_id, fix_note=NOTE,
                                  by_person=by_person, recheck=True)
    except RetryRefused as exc:
        raise RecheckRefused(str(exc)) from exc
    except Exception as exc:  # noqa: BLE001 — BranchOwned and kin: say why, start nothing
        if type(exc).__name__ in ("BranchOwned", "RetryNeedsPerson"):
            raise RecheckRefused(str(exc)) from exc
        raise
    new_id = str(started.get("run_id") or "")
    with begin(engine) as c:
        emit_event(c, project_id, "code_task.recheck_requested", actor, "code_task_run",
                   new_id or run_id, {"recheck_of": run_id, "estimate_usd": cost["usd"],
                                      "gates": list(GATES)})
    return {"run_id": new_id, "recheck_of": run_id, "attempt": started.get("attempt"),
            "estimate": cost, "gates": list(GATES)}


__all__ = ["CHECK_STAGES", "GATES", "KEY", "NOTE", "NoWriteAgent", "OF_KEY", "RecheckRefused",
           "WRITING_STAGES", "estimate", "is_recheck", "preview", "refusal", "request"]
