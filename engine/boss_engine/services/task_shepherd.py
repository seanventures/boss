"""Task Shepherd — the bounded core.

Design: docs/product/TASK_SHEPHERD_DESIGN.md

This module is the half that must NEVER be left to a model's judgement: the failure taxonomy,
the remedy ledger, the termination conditions, and the cost arithmetic. A model chooses among
what this module says is permissible, and must cite evidence; it cannot invent an action or
repeat a spent one.

Written from the 2026-08-03/04 session, where 13 governed runs cost $66.65 and 58% of that
produced nothing. Every one of those failures was operational rather than a coding failure —
a reclaimed lease, a provider spend cap, a merge needing a rebase, an approval needing a
human. All recoverable by someone who knew what the error meant. This encodes that knowing.

The functions here are pure and side-effect free so they can be tested without a database or
a model. Persistence lives in the service layer above.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional

from ..ports import sql as _sql

# --------------------------------------------------------------------------- #
# Failure classes
# --------------------------------------------------------------------------- #
# Numbered to match the design doc's remedy table, so a reasoning record, a ledger row and
# the doc all refer to the same thing.
SPEND_CAP = 1
TURN_CAP = 2
STALE_BASE = 3
MERGE_CONFLICT = 4
APPROVAL_REQUIRED = 5
GATES_FAILED = 6
NO_CODE = 7
DUPLICATE_EXECUTION = 8
INFRASTRUCTURE = 9
UNKNOWN = 10
PR_STEP_FAILED = 11
NOT_A_FAILURE = 12
# Delivery classes. A merge now continues into a deployment, so the shepherd's ledger has
# to cover what happens AFTER the PR closes — otherwise the one part of the pipeline that
# touches a real host is the one part with no failure taxonomy.
DEPLOY_FAILED = 13
HEALTH_CHECK_FAILED = 14
CANARY_REGRESSED = 15
PROMOTION_BLOCKED = 16
# A preview is where an operator first SEES the thing BOSS built, so a preview that fails is a
# delivery failure like any other. It had no class at all: five consecutive failures on one run
# produced no classification, no remedy and no ledger entry, and the operator was left to
# report it as "stuck".
PREVIEW_FAILED = 17
# Starvation is not a failure — it is the ABSENCE of progress. Every other class here is
# triggered by something going wrong: an error string, a failed gate, a red health check. A
# job that is simply never claimed produces none of those, so nothing in this taxonomy could
# see it. A PRD meeting sat queued for two hours behind benchmark work and every check in the
# system reported green.
WORK_STARVED = 18
# THE APP EXITED WHEN BOSS STARTED IT TO CHECK IT (the serve proof, a multi-service backend in
# verify or the template gate). HelpDesk AI (crun_f246d330) ended "exited (code 1) before it
# answered HTTP" and was filed UNKNOWN and escalated, although the app had said why on stderr.
# The app's own error is the evidence (services.app_start), and the fix is the build's.
APP_START_FAILED = 19

CLASS_NAMES = {
    SPEND_CAP: "provider spend/rate cap",
    TURN_CAP: "turn cap exhausted",
    STALE_BASE: "stale merge base",
    MERGE_CONFLICT: "true merge conflict",
    APPROVAL_REQUIRED: "approval required",
    GATES_FAILED: "quality gates failed",
    NO_CODE: "no code produced",
    DUPLICATE_EXECUTION: "duplicate execution",
    INFRASTRUCTURE: "infrastructure",
    UNKNOWN: "unknown",
    PR_STEP_FAILED: "pull-request step failed",
    NOT_A_FAILURE: "not a failure",
    DEPLOY_FAILED: "deployment failed",
    HEALTH_CHECK_FAILED: "health check failed",
    CANARY_REGRESSED: "canary regressed",
    PROMOTION_BLOCKED: "promotion blocked",
    PREVIEW_FAILED: "preview failed to start",
    WORK_STARVED: "work queued but never claimed",
    APP_START_FAILED: "app exited at start",
}

# Exact phrases the engine itself writes. Deliberately NOT loose tokens: a bare "timeout"
# matched the sentence "review found a missing timeout on the fetch call" during the session,
# which would have classified a genuine code finding as a platform fault. Reviewer prose is
# not a status code.
_SIGNATURES: list[tuple[int, tuple[str, ...]]] = [
    # Delivery first: these are precise strings emitted by deploy_service / the rollout
    # engine, and matching them before the generic INFRASTRUCTURE phrases keeps a failed
    # health gate from being filed as "infrastructure" and retried blindly.
    (HEALTH_CHECK_FAILED, ("health check failed", "health gate failed",
                           "unhealthy after deploy", "rolled_back")),
    (CANARY_REGRESSED, ("canary regressed", "canary verdict: regression",
                        "telemetry regression")),
    (PROMOTION_BLOCKED, ("promotion blocked", "no active production deployment_target",
                         "must reference a production target")),
    (DEPLOY_FAILED, ("deployment failed", "deploy failed", "docker run failed",
                     "rsync failed", "no connector for target_kind")),
    # Before INFRASTRUCTURE: a preview that never served HTTP is a property of the BUILD, and
    # filing it as infrastructure would prescribe a blind retry of a build that cannot succeed.
    (PREVIEW_FAILED, ("did not serve http", "could not resolve entry",
                      "preview failed to start", "boot_crash", "port_timeout",
                      # An app that bound its port and serves nothing at "/" is the same class
                      # of fault as one that never built: the repair is in the BUILD, not in a
                      # retry. Without these it fell through to the generic buckets below and
                      # would have been prescribed an infrastructure retry that cannot add a UI.
                      "no page at the site root", "no_page_at_root",
                      # The same fault for an app that declares its health path: it bound, and
                      # the route it named is not there.
                      "no health route at the path it declares", "no_health_route")),
    # After the preview's own phrases, before INFRASTRUCTURE: an app BOSS started to check it
    # that exited is a property of the build (its own stderr says why), never a platform retry.
    (APP_START_FAILED, ("before it answered http", "before serving http",
                        "the app did not start", "the app exited at start")),
    (SPEND_CAP, ("spend limit", "usage limit", "rate limit exceeded", "quota exceeded",
                 "too many requests")),
    (TURN_CAP, ("error_max_turns", "max_turns_exhausted", "ran out of turns")),
    (STALE_BASE, ("405", "method not allowed", "not mergeable", "base branch was modified")),
    (MERGE_CONFLICT, ("merge conflict", "conflicting", "needs_conflict_resolution")),
    (APPROVAL_REQUIRED, ("not allowed to approve", "projectmemberpermissionerror",
                         "awaiting_plan_approval", "awaiting_gate_override")),
    (NO_CODE, ("code_written", "no test files", "produced no test files")),
    (DUPLICATE_EXECUTION, ("lease reassigned", "runfenced", "owned by another execution",
                           "duplicate concurrent")),
    (INFRASTRUCTURE, ("connection refused", "connection reset", "executable is not allowlisted",
                      "infrastructure error", "timed out after", "lease expired",
                      "budget reservation denied",
                      # SIGTERM. During the session this was a sibling run's process killer
                      # reaching outside its own sandbox; it is a platform fault, never the
                      # task's, and re-running the task unchanged is the correct remedy.
                      "exited 143",
                      # SIGKILL — almost always the OOM killer. A web build runs npm install,
                      # vite and playwright inside the agent's process tree, and the kernel
                      # takes the whole thing. It was NOT matched here, so an OOM-killed run
                      # fell through to UNKNOWN and escalated to a human instead of simply
                      # being re-run. Same class as 143: the machine killed it, the task did
                      # nothing wrong.
                      "exited -9", "exited 137", "signal 9", "sigkill", "killed",
                      "error_during_execution")),
    # The run did all the work and the PR CALL failed. Emphatically NOT a stale base: the
    # branch is mergeable and there is nothing to rebase. Prescribing a rebase here would send
    # an agent to resolve conflicts that do not exist. Two runs ended here.
    (PR_STEP_FAILED, ("pr not created",)),
    # Terminal states that are not failures at all. An auto-repair round is BOSS narrating its
    # own progress, and an operator note is an operator note. Escalating these as problems is
    # how a shepherd trains its operator to ignore it.
    (NOT_A_FAILURE, ("auto review-repair round", "lineage abandoned",
                     "cancelled by operator", "cancelled:")),
]


def classify(error: Optional[str], *, gates_failed: Optional[list[str]] = None,
             has_pr: bool = False, recorded_class: Optional[int] = None) -> int:
    """Map observed evidence onto a failure class.

    `recorded_class` is what the PIPELINE decided at failure time and wins outright. That
    matters: the pipeline holds the live exception, the substrate, and the stage, while this
    function gets a string that may have been truncated or reformatted on the way here. String
    matching is the FALLBACK, for rows written before the pipeline recorded a class.

    This ordering exists because the first cut of this module built a second, independent
    taxonomy alongside the one BOSS already recorded — the same "two copies of one decision"
    defect that produced the preview-path and repo-tests bugs in the same session. One
    decision, computed where the evidence is richest, read everywhere else.

    UNKNOWN is a real answer, not a fallback for "probably infra". Every expensive mistake of
    that session came from treating an unclassified failure as a familiar one.
    """
    if recorded_class is not None and recorded_class in CLASS_NAMES:
        return recorded_class
    text = (error or "").lower()
    if gates_failed and has_pr:
        # The PR shipped with findings attached; the follow-up task already exists. There is
        # nothing for a shepherd to remedy here.
        return GATES_FAILED
    for cls, phrases in _SIGNATURES:
        if any(p in text for p in phrases):
            return cls
    return UNKNOWN


# --------------------------------------------------------------------------- #
# Remedies
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class Remedy:
    id: str
    describe: str
    # COLD-START PRIOR ONLY — never a fixed price.
    # These numbers came from one operator's 13-run local sample. An enterprise repo, a
    # different model tier, or a larger codebase moves them by an order of magnitude, and a
    # shepherd that budgets against this constant would refuse affordable work or start work
    # it cannot finish. estimate_cost() replaces it with that PROJECT's measured history as
    # soon as any exists; this value is only what to assume before the first observation.
    prior_cost_usd: float
    # An action the shepherd may take, or None when the remedy IS escalation.
    action: Optional[str] = None
    # True when the engine already handles this without a shepherd. Kept in the table so the
    # ledger stays honest about what it actually owns rather than claiming credit.
    automatic: bool = False


ESCALATE = Remedy("escalate", "ask the operator one decision-shaped question", 0.0, None)

REMEDIES: dict[int, tuple[Remedy, ...]] = {
    DEPLOY_FAILED: (
        Remedy("redeploy_same_commit",
               "redeploy the same commit — transfer and docker steps are idempotent",
               0.0, "redeploy_deployment"),
        Remedy("preflight_target",
               "run preflight: a missing credential or absent docker explains most of "
               "these, and reports it without a second failed deploy",
               0.0, "preflight_target"),
        ESCALATE,
    ),
    HEALTH_CHECK_FAILED: (
        # NOT a retry-first class. deploy_service already restored the previous build, so
        # the host is serving and nothing is on fire; redeploying the same broken commit
        # would simply fail the same gate again and churn the rollback.
        Remedy("open_fix_task", "open a task against the failing health check", 6.0,
               "create_code_task"),
        ESCALATE,
    ),
    CANARY_REGRESSED: (
        # The rollout engine rolls back on its own; the shepherd's job is the follow-up,
        # not the reaction.
        Remedy("open_regression_task",
               "open a task with the telemetry delta as evidence", 6.0,
               "create_code_task"),
        ESCALATE,
    ),
    PROMOTION_BLOCKED: (
        # Usually configuration, not code: no production target registered for this app.
        # There is nothing to retry, so this escalates by design.
        ESCALATE,
    ),
    WORK_STARVED: (
        # Demote what is IN FRONT of it, rather than promoting the starved job. Promotion is
        # an arms race — the next starved job needs to outrank this one — and it was exactly
        # how the ranking got inverted in the first place: each caller nudged its own work up
        # until benchmark jobs sat at priority_score 9000, above every band at once.
        Remedy("demote_blockers",
               "move the lower-value work ahead of it into a lower band, so the queue drains "
               "in the order a person would expect",
               0.0, "demote_blocking_jobs"),
        # Only if it is not merely queued behind something, but behind something WEDGED.
        Remedy("cancel_wedged_blocker",
               "stop a job that has held a worker slot far past any plausible runtime and is "
               "blocking work someone is waiting on",
               0.0, "cancel_wedged_jobs"),
        ESCALATE,
    ),
    PREVIEW_FAILED: (
        # Retry FIRST and cheaply. A preview clones, installs and boots; a registry blip or a
        # slow first install is a real and common cause, and re-running costs nothing but time.
        Remedy("retry_preview",
               "start the preview again — clone/install/boot are idempotent and a slow first "
               "install is the commonest cause",
               0.0, "restart_preview"),
        # Then, and only then, treat it as a code defect. The evidence is the build log, which
        # is why this remedy passes it to the task rather than restating the symptom.
        #
        # GUARD, learned the hard way: an agent told only "the preview will not start" invents a
        # preview command to satisfy the harness. On a vanilla-JS + Python repo it added a Vite
        # `preview` script and ten devDependencies, and rollup then failed on an entry module
        # that had never existed — the tooling broke the repo trying to please the tooling. The
        # task must say: fix the app, never invent a build system it does not have.
        Remedy("open_preview_fix_task",
               "open a task with the preview build log; fix the app, do not invent a build "
               "system the repo does not use",
               6.0, "create_code_task"),
        ESCALATE,
    ),
    SPEND_CAP: (
        Remedy("wait_resume", "wait for the cap to reset, resume from the checkpoint", 3.0,
               "retry_code_task"),
        Remedy("reissue_lean", "re-issue on the lean profile (no adversarial panel)", 3.0,
               "retry_code_task"),
        ESCALATE,
    ),
    TURN_CAP: (
        Remedy("raise_turns", "retry on the same branch with a raised turn cap", 3.0,
               "retry_code_task"),
        Remedy("split_task", "split the task; the scope exceeds one agent session", 6.0,
               "create_code_task"),
        ESCALATE,
    ),
    STALE_BASE: (
        Remedy("update_branch", "merge the base into the head branch and retry", 0.0,
               None, automatic=True),
        Remedy("rebase_via_agent", "have BOSS rebase the branch onto the new base", 5.0,
               "extend_code_task"),
        ESCALATE,
    ),
    MERGE_CONFLICT: (
        Remedy("resolve_via_agent", "have BOSS resolve the conflicts on the branch", 5.0,
               "extend_code_task"),
        ESCALATE,
    ),
    # No automated remedy exists or should. Tier 2 requires the ceo role, and an agent
    # approving its own merge into main is what a governance product exists to prevent.
    APPROVAL_REQUIRED: (ESCALATE,),
    GATES_FAILED: (
        Remedy("already_shipped", "PR shipped with findings; follow-up task exists", 0.0,
               None, automatic=True),
    ),
    NO_CODE: (
        Remedy("rerun_implement", "re-run implement on the checkpointed branch", 3.0,
               "retry_code_task"),
        Remedy("rescope", "re-scope the task; the brief was not buildable as written", 6.0,
               "extend_code_task"),
        ESCALATE,
    ),
    DUPLICATE_EXECUTION: (
        Remedy("fencing", "fencing rejects the stale writer", 0.0, None, automatic=True),
        Remedy("reissue_clean", "re-issue once, resuming from the checkpoint", 3.0,
               "retry_code_task"),
        ESCALATE,
    ),
    INFRASTRUCTURE: (
        Remedy("substrate_fallback", "re-run on the local substrate", 3.0, "retry_code_task"),
        ESCALATE,
    ),
    UNKNOWN: (ESCALATE,),
    APP_START_FAILED: (
        # The repair round reads the verify output, which now carries the app's own error lines
        # and the variables BOSS set by name (app_start) — a retry is a fix with evidence, not a
        # blind re-run.
        Remedy("repair_with_app_output",
               "retry the build: its repair round is handed the app's own start error and the "
               "environment names BOSS set", 3.0, "retry_code_task"),
        ESCALATE,
    ),
    PR_STEP_FAILED: (
        # The engine already retries the PR call three times before giving up, so a fourth
        # attempt from here would be the retry loop this design exists to avoid.
        Remedy("pr_retry", "the engine retries the PR call 3x", 0.0, None, automatic=True),
        Remedy("reissue_pr", "re-issue the task; the branch and its work are intact", 3.0,
               "retry_code_task"),
        ESCALATE,
    ),
    # Nothing to remedy. Recorded so the shepherd stays quiet rather than escalating noise.
    NOT_A_FAILURE: (
        Remedy("no_action", "not a failure; no remedy needed", 0.0, None, automatic=True),
    ),
}




# --------------------------------------------------------------------------- #
# Cost estimation — measured, not assumed
# --------------------------------------------------------------------------- #
def estimate_cost(connection, project_id: str, remedy: "Remedy",
                  failure_class: int, *, percentile: float = 0.75) -> float:
    """What this remedy actually costs IN THIS PROJECT, from its own history.

    A prior baked into the code is a guess about someone else's repository. Cost per run
    varies with codebase size, model tier, profile and task shape — the same remedy measured
    between $0.88 and $12.47 within a single evening on one repo. So the estimate comes from
    that project's `llm_calls`, and the constant is only the cold start.

    Uses a high percentile rather than the mean on purpose: the shepherd must not start work
    it cannot finish, so it should budget for a bad case, not a typical one. Under-estimating
    is the expensive direction.
    """
    try:
        row = connection.execute(
            _sql(
                """
                SELECT percentile_cont(:pct) WITHIN GROUP (ORDER BY per_run) AS est,
                       count(*) AS n
                FROM (
                    SELECT l.code_task_run_id, SUM(l.cost_usd_micros)/1e6 AS per_run
                    FROM llm_calls l
                    JOIN code_task_runs r ON r.id = l.code_task_run_id
                    WHERE r.project_id = :p
                      AND l.created_at > now() - interval '30 days'
                    GROUP BY 1
                ) s
                """
            ),
            {"p": project_id, "pct": percentile},
        ).mappings().first()
    except Exception:  # noqa: BLE001 — an estimate must never break the shepherd
        return remedy.prior_cost_usd
    if not row or not row["n"] or row["est"] is None:
        return remedy.prior_cost_usd
    measured = float(row["est"])
    # A remedy that resumes from a checkpoint skips the design chain and the test authoring,
    # so it costs a fraction of a full run. Scale the measured run cost by the remedy's prior
    # relative to a full run's prior rather than inventing a second set of constants.
    full_run_prior = 6.0
    scale = remedy.prior_cost_usd / full_run_prior if full_run_prior else 1.0
    return round(max(0.0, measured * scale), 2)


def task_budget(connection, project_id: str, *, default_usd: float = 25.0) -> float:
    """The budget a shepherd may spend on one task, from the project's OWN policy.

    Not a constant. `budget_policies` already carries per-project daily and monthly caps that
    differ by orders of magnitude between a dogfood project and an enterprise one; a shepherd
    that assumed $25 would be uselessly timid for the latter and reckless for a trial.

    Derived as a fraction of the daily cap so a single task cannot consume the day.
    """
    try:
        row = connection.execute(
            _sql(
                "SELECT daily_cap_usd_micros FROM budget_policies "
                "WHERE scope_type='project' AND scope_id=:p "
                "ORDER BY created_at DESC LIMIT 1"
            ),
            {"p": project_id},
        ).mappings().first()
    except Exception:  # noqa: BLE001
        return default_usd
    if not row or not row["daily_cap_usd_micros"]:
        return default_usd
    daily = float(row["daily_cap_usd_micros"]) / 1e6
    # At most a tenth of the day's allowance on any one task, and never below the default —
    # a tiny cap should slow a task down, not make every remedy unaffordable.
    return max(default_usd, round(daily * 0.10, 2))


# --------------------------------------------------------------------------- #
# Termination
# --------------------------------------------------------------------------- #
@dataclass
class ShepherdState:
    """Everything needed to decide whether to act again. Deliberately small and explicit."""
    task_ref: str
    budget_usd: float
    spent_usd: float = 0.0
    used_remedies: set[tuple[int, str]] = field(default_factory=set)
    # (failure_class, remedy_id) -> how many times this pair was followed by the SAME failure
    attempt_history: list[tuple[int, str, str]] = field(default_factory=list)
    # How long THIS SHEPHERD has been working the task — not how old the run is.
    # First run against real data stopped every proposal with "deadline" because run age was
    # passed here: a failure from yesterday is not a shepherd that has been trying for a day,
    # it is a shepherd that has not started. The deadline bounds effort, not staleness.
    shepherd_minutes: float = 0.0
    deadline_minutes: float = 240.0

    @property
    def remaining_usd(self) -> float:
        return max(0.0, self.budget_usd - self.spent_usd)


@dataclass(frozen=True)
class Verdict:
    """What the shepherd should do next, and why. `stop` is a first-class success."""
    action: str                 # "act" | "stop"
    remedy: Optional[Remedy] = None
    reason: str = ""
    stopped_because: Optional[str] = None   # success|ledger|loop|budget|human|deadline


def next_step(state: ShepherdState, failure_class: int, *, succeeded: bool = False,
              failure_signature: str = "") -> Verdict:
    """The single decision point. Cheap, deterministic, and checked BEFORE any spend.

    Order is not arbitrary: success first (never act on a finished task), then the bounds that
    cost nothing to evaluate, and only then the ledger. A model is consulted afterwards to
    choose among what this returns as permissible — never to decide whether to continue.
    """
    if succeeded:
        return Verdict("stop", None, "the task reached its terminal condition", "success")

    if state.shepherd_minutes >= state.deadline_minutes:
        return Verdict("stop", None,
                       f"wall-clock deadline reached ({state.deadline_minutes:.0f} min)",
                       "deadline")

    remedies = REMEDIES.get(failure_class, (ESCALATE,))

    # Loop detection: the SAME remedy followed by the SAME failure, twice. One repeat is
    # coincidence; the second is proof the remedy does not work here. During the session a run
    # executed three identical passes and nothing was counting.
    for cls, rid, sig in state.attempt_history:
        if cls == failure_class and sig == failure_signature:
            repeats = sum(1 for c, r, s in state.attempt_history
                          if c == cls and r == rid and s == sig)
            if repeats >= 2:
                return Verdict("stop", None,
                               f"remedy {rid!r} produced the same failure twice; it does not "
                               "work here", "loop")

    for remedy in remedies:
        if (failure_class, remedy.id) in state.used_remedies:
            continue
        if remedy.automatic:
            # The engine handles it; the shepherd records it and moves to the next remedy
            # rather than claiming work it did not do.
            continue
        if remedy is ESCALATE or remedy.action is None:
            return Verdict("stop", ESCALATE,
                           "no automated remedy remains for this class", "human")
        if remedy.prior_cost_usd > state.remaining_usd:
            return Verdict("stop", None,
                           f"next remedy needs ${remedy.prior_cost_usd:.2f} but only "
                           f"${state.remaining_usd:.2f} remains; not starting what it "
                           "cannot finish", "budget")
        return Verdict("act", remedy, remedy.describe)

    return Verdict("stop", None, "every remedy for this class has been spent", "ledger")


def needs_operator_confirmation(remedy: Remedy, state: ShepherdState,
                                threshold: float = 0.4) -> bool:
    """Spend that is large relative to the budget is the operator's call, not the shepherd's.

    Not a safety rail against runaway cost — the ledger and budget already bound that. This is
    about a single expensive action deserving a human's assent even when it is affordable.
    """
    if state.budget_usd <= 0:
        return True
    return remedy.prior_cost_usd > threshold * state.budget_usd


def escalation(task_ref: str, failure_class: int, evidence: dict[str, Any],
               options: Optional[list[dict[str, Any]]] = None,
               costs: Optional[dict[str, float]] = None) -> dict[str, Any]:
    """Build a decision-shaped question.

    Never a log dump. During the session the operator was repeatedly handed "here are five
    defects, what would you like to do?" — which is not a question, it is a transfer of work.
    Each option states what it costs.
    """
    remedies = REMEDIES.get(failure_class, (ESCALATE,))
    # Each option states its cost, and whether that cost was MEASURED for this project or is
    # only the cold-start prior. Quoting a constant to an operator as though it were a
    # measurement is how a shepherd loses their trust the first time an enterprise repo costs
    # ten times the number it printed.
    auto = [
        {
            "label": r.describe,
            "action": r.action,
            "est_cost_usd": (costs or {}).get(r.id, r.prior_cost_usd),
            "estimate_source": "measured" if (costs or {}).get(r.id) is not None else "prior",
        }
        for r in remedies if r.action and not r.automatic
    ]
    return {
        "task_ref": task_ref,
        "failure_class": failure_class,
        "question": f"{CLASS_NAMES.get(failure_class, 'unknown')} — how should this proceed?",
        "options": options or (auto + [{"label": "I'll handle it", "action": None,
                                        "est_cost_usd": 0.0, "estimate_source": "n/a"}]),
        "evidence": evidence,
    }
