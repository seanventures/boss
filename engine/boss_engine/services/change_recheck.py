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
    #: The optional knobs the pipeline SETS on a writing agent (governed: `a.redact = …`,
    #: `a.budget_ceiling = …`, `a.command_runner = …`). Declared, so any other attribute is an
    #: AttributeError (getattr(agent, name, default) still gets its default) — never a None
    #: that a later `agent.something()` would call.
    redact: Any = None
    budget_ceiling: Any = None
    command_runner: Any = None

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


def _bar_asked(value: Any) -> Optional[str]:
    """RE-CHECK AT THE PRODUCTION BAR: a build whose profile never convened every review seat
    (a tier-0 build, a fast fix) is refused for production (quality_bar.production_refusal names
    this action). The re-check is the way to the bar: it raises the run's bar to production, so
    the pipeline convenes EVERY seat whatever the build's original profile. Only a raise is
    honoured; any other value is ignored."""
    from . import quality_bar

    return quality_bar.PRODUCTION if quality_bar.normalize(value) == quality_bar.PRODUCTION else None


def request(engine: Any, settings: Any, jobs: Any, run_id: str, *, project_id: str,
            actor: str, by_person: bool = False, quality_bar: Optional[str] = None) -> dict:
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
                                  by_person=by_person, recheck=True,
                                  recheck_bar=_bar_asked(quality_bar))
    except RetryRefused as exc:
        raise RecheckRefused(str(exc)) from exc
    except Exception as exc:  # noqa: BLE001 — BranchOwned and kin: say why, start nothing
        if type(exc).__name__ in ("BranchOwned", "RetryNeedsPerson"):
            raise RecheckRefused(str(exc)) from exc
        raise
    new_id = str(started.get("run_id") or "")
    with begin(engine) as c:
        # A newer re-check makes every earlier Fix card of this PR a stale snapshot.
        from .run_decisions import withdraw_fix_cards

        withdraw_fix_cards(c, project_id, str(run.get("pull_request_url") or ""),
                           note=f"Superseded: a newer re-check ({new_id or 'queued'}) of this "
                                "change decides what is left to fix.")
        emit_event(c, project_id, "code_task.recheck_requested", actor, "code_task_run",
                   new_id or run_id, {"recheck_of": run_id, "estimate_usd": cost["usd"],
                                      "gates": list(GATES)})
    return {"run_id": new_id, "recheck_of": run_id, "attempt": started.get("attempt"),
            "estimate": cost, "gates": list(GATES)}


# ── what a re-check may not do: commit, or start a build ─────────────────────────────────────

#: Why a re-check holds every push (governed _push_held, the one choke point).
PUSH_HOLD = "a re-check commits nothing: it only judges the change as it stands"


def push_hold_reason(recheck: bool, template_reason: Optional[str]) -> Optional[str]:
    """Why a push is held, or None: a re-check holds EVERY commit and upsert (a checkpoint, a
    rescue, a partial PR, the preview declaration, the PRD record, a stage commit), whatever its
    label; otherwise an applied template's hold, as before (#168 review R2)."""
    return PUSH_HOLD if recheck else template_reason


def seed_acceptance(of_run: Optional[Mapping[str, Any]], history: list, judged: dict) -> None:
    """Start a re-check's acceptance history and judged table from the run it re-checks (#168
    review M1): its history continues, and its last JUDGED table (met / not_met, never a refused
    or unrun one) stands until the re-check's own check judges."""
    state = (of_run or {}).get("state") or {}
    for h in state.get("acceptance_history") or []:
        if isinstance(h, Mapping):
            history.append(dict(h))
    acc = state.get("acceptance")
    if (isinstance(acc, Mapping) and acc.get("verdict") in ("met", "not_met")
            and not acc.get("refused")):
        judged["record"] = dict(acc)


#: The card a re-check raises when it fails or has findings: "Fix these (about $X)".
FIX_ACTION = "start_recheck_followup"
#: A follow-up's cost when the lineage recorded no build spend to go by.
DEFAULT_FIX_ESTIMATE_USD = 8.0


def fix_estimate(connection: Any, project_id: str, root: str) -> dict:
    """What fixing the findings is likely to cost: what the lineage's newest coding attempt
    cost in all (llm_calls), else DEFAULT_FIX_ESTIMATE_USD."""
    from sqlalchemy import text

    try:
        with connection.begin_nested():
            row = connection.execute(text(
                "SELECT l.code_task_run_id AS rid, SUM(l.cost_usd_micros) AS micros "
                "FROM llm_calls l JOIN code_task_runs r ON r.id = l.code_task_run_id "
                "WHERE r.project_id = :p AND (r.id = :root OR r.finalize_ctx->>'root_run_id' = :root) "
                "AND COALESCE(r.finalize_ctx->>'recheck_only', '') = '' "
                "AND l.stage IN ('implement', 'solo', 'write_tests') "
                "GROUP BY l.code_task_run_id, r.created_at ORDER BY r.created_at DESC LIMIT 1"),
                {"p": project_id, "root": root}).mappings().first()
            if row and int(row["micros"] or 0) > 0:
                total = connection.execute(text(
                    "SELECT SUM(cost_usd_micros) FROM llm_calls WHERE code_task_run_id = :r"),
                    {"r": row["rid"]}).scalar()
                return {"usd": round(int(total or 0) / 1_000_000, 2),
                        "basis": f"what {row['rid']} cost"}
    except Exception:  # noqa: BLE001 — no ledger to read: the default estimate
        pass
    return {"usd": DEFAULT_FIX_ESTIMATE_USD, "basis": "no earlier build spend recorded"}


def raise_fix_card(engine: Any, *, run_id: str, project_id: str, task_id: Optional[str],
                   title: str, spec: Mapping[str, Any], base_branch: str, parent_root: str,
                   parent_origin: Any, workflow_run_id: Optional[str], findings: list,
                   checks_failing: Optional[Mapping[str, Any]]) -> Optional[str]:
    """The re-check failed or found something: its findings are recorded in the ledger (open
    rows, nobody holds them) and ONE card asks a person to start the fix. Returns the card's id
    (an open card for this re-check is reused), or None when there is nothing to fix."""
    from ..db import begin
    from .checks_failing import followup_findings, headline
    from .core import create_blocking_decision, emit_event

    # The pipeline's findings already lead with the failing checks (checks_failing.followup_findings).
    items = list(findings or []) or (followup_findings(checks_failing) if checks_failing else [])
    if not items:
        return None
    with begin(engine) as c:
        try:
            from .acceptance import ledger as _ledger

            with c.begin_nested():
                lin = _ledger.lineage_of(c, run_id)
                _ledger.observe_findings(c, project_id=project_id,
                                         lineage=(lin or {}).get("lineage") or parent_root,
                                         run_id=run_id, findings=items, stage="recheck",
                                         pr_url=(lin or {}).get("pr_url"))
        except Exception:  # noqa: BLE001 — the card still carries every finding
            pass
        from sqlalchemy import text

        open_card = c.execute(text(
            "SELECT id FROM decisions WHERE project_id = :p AND status = 'pending' "
            "AND recommendation->>'action' = :a AND recommendation->>'run_id' = :r LIMIT 1"),
            {"p": project_id, "a": FIX_ACTION, "r": run_id}).scalar()
        if open_card:
            return str(open_card)
        pr_url = c.execute(text("SELECT pull_request_url FROM code_task_runs WHERE id = :i"),
                           {"i": run_id}).scalar()
        cost = fix_estimate(c, project_id, parent_root)
        what = (f"its checks fail ({headline(checks_failing)})" if checks_failing
                else f"{len(items)} finding{'s' if len(items) != 1 else ''} to fix")
        decision_id = create_blocking_decision(
            c, project_id, task_id,
            f"Fix these (about ${cost['usd']:.2f}): the re-check of “{str(title)[:70]}” says {what}",
            {"action": FIX_ACTION, "run_id": run_id, "estimate": cost,
             # THE PR IT IS ABOUT (BOSSAPP-1328): a newer build's card withdraws it.
             "pull_request_url": pr_url,
             "findings": items[:40], "checks_failing": dict(checks_failing or {}) or None,
             "followup": {"spec": dict(spec or {}), "base_branch": base_branch,
                          "parent_title": title, "parent_root": parent_root,
                          "parent_origin": parent_origin, "workflow_run_id": workflow_run_id},
             "summary": (f"The re-check found {len(items)} open item(s). Nothing is built until "
                         f"you press Fix these (about ${cost['usd']:.2f}, {cost['basis']}).")},
            {"phase": "recheck", "estimate_usd": cost["usd"]},
            "a re-check never starts a build on its own", risk_tier=1)
        c.execute(text("UPDATE decisions SET deadline_at = NULL WHERE id = :d"),
                  {"d": decision_id})
        if pr_url:
            # One Fix card per PR: an older re-check's card speaks for a stale check.
            from .run_decisions import withdraw_superseded_merge_cards

            withdraw_superseded_merge_cards(c, project_id, str(pr_url), str(decision_id),
                                            run_id=run_id, actions=(FIX_ACTION,))
        emit_event(c, project_id, "code_task.recheck_fix_card", "boss", "code_task_run",
                   run_id, {"decision_id": decision_id, "items": len(items),
                            "estimate_usd": cost["usd"]})
    return str(decision_id)


def may_start_fix(connection: Any, project_id: str, actor: Optional[str] = None) -> bool:
    """Whether `actor` may start the paid fix: a PERSON (BOSSAPP-1328 — never an agent, a worker
    or a system actor, inside a request or not) who, inside an enforced request, is a user
    principal holding code_task:steer on the project."""
    from sqlalchemy import text

    from ..identity import authz, lineage
    from ..identity.permissions_gen import P
    from ..identity.scope import ResolvedScope
    from .agent_identity import is_person

    if not is_person(actor, connection, project_id):
        return False
    if not lineage.request_enforced():
        return True
    ctx = lineage.current_context()
    org_id = connection.execute(text(
        "SELECT w.org_id FROM projects p JOIN workspaces w ON w.id = p.workspace_id "
        "WHERE p.id = :p"), {"p": project_id}).scalar()
    if (ctx is None or not org_id or ctx.principal.org_id != org_id
            or not ctx.principal.is_person):
        return False
    return authz.check(ctx, P.CODE_TASK_STEER, ResolvedScope.in_project(org_id, project_id),
                       db=connection).allowed


def fix_refusal(connection: Any, rec: Mapping[str, Any], project_id: str) -> Optional[str]:
    """Why a Fix card must not start anything now, or None (BOSSAPP-1328):
      * a newer build of the change exists (a re-check, a retry, a follow-up): the card is a
        stale snapshot, and that build's own checks decide;
      * nothing it listed is still open in the ledger (fixed since): there is nothing to fix."""
    from sqlalchemy import text

    from .acceptance import ledger as _ledger

    run_id = str(rec.get("run_id") or "")
    root = str((rec.get("followup") or {}).get("parent_root") or run_id)
    newer = connection.execute(text(
        "SELECT r.id FROM code_task_runs r, code_task_runs me WHERE me.id = :me "
        "AND r.project_id = :p AND r.id <> :me AND r.created_at > me.created_at "
        "AND (r.id = :root OR r.finalize_ctx->>'root_run_id' = :root) "
        "ORDER BY r.created_at DESC LIMIT 1"),
        {"me": run_id, "p": project_id, "root": root}).scalar()
    if newer:
        return (f"A newer build of this change ({newer}) exists; its own checks decide what is "
                "left to fix. Nothing was started.")
    lin = _ledger.lineage_of(connection, run_id)
    rows = _ledger.open_for(connection, project_id=project_id,
                            lineage=(lin or {}).get("lineage") or root, include_held=False)
    if not rows:
        return ("Nothing is open: the findings this card listed were fixed since. Nothing "
                "was started.")
    return None


def start_fix(engine: Any, settings: Any, jobs: Any, rec: Mapping[str, Any], *,
              project_id: str, task_id: Optional[str]) -> Optional[str]:
    """A person pressed Fix these: the follow-up that fixes the re-check's findings, on the
    change's branch, like any checks-failing follow-up. Returns its run id."""
    from .governed_code_task_service import _spawn_deferred_findings_task

    from ..db import begin

    with begin(engine) as c:
        why = fix_refusal(c, rec, project_id)
    if why:
        raise RecheckRefused(why)
    f = dict(rec.get("followup") or {})
    # THE LEDGER'S OPEN ROWS NOW, NEVER THE CARD'S SNAPSHOT (BOSSAPP-1328): handing the snapshot
    # back to the ledger reopened rows fixed since as "regressions". No findings are observed
    # here; the follow-up is built from what is open (governed._ledger_followup).
    return _spawn_deferred_findings_task(
        engine, settings, jobs, project_id=project_id, task_id=task_id,
        spec=dict(f.get("spec") or {}), base_branch=str(f.get("base_branch") or ""),
        findings=[], parent_run_id=str(rec.get("run_id") or ""),
        parent_title=str(f.get("parent_title") or ""),
        parent_root=str(f.get("parent_root") or "") or None,
        parent_origin=f.get("parent_origin"),
        checks_failing=rec.get("checks_failing") or None,
        parent_workflow_run_id=f.get("workflow_run_id"))


__all__ = ["CHECK_STAGES", "GATES", "KEY", "NOTE", "NoWriteAgent", "OF_KEY", "RecheckRefused",
           "WRITING_STAGES", "estimate", "is_recheck", "preview", "refusal", "request",
           "FIX_ACTION", "PUSH_HOLD", "fix_estimate", "may_start_fix", "push_hold_reason",
           "raise_fix_card", "seed_acceptance", "start_fix", "fix_refusal"]
