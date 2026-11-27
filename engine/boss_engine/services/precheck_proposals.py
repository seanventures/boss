"""EVERY RUN FAILURE PROPOSES A PRE-CHECK FOR ITSELF (BOSSAPP-541).

On 2026-10-14 the coded pre-check missed three run-killers:
  1. `socat` missing from the runtime image (sandbox could not initialize);
  2. a disconnected model source reported connected;
  3. spending caps that could not fit metered billing.

Each check was added only after the failure. This module makes the loop systematic and
deterministic: whenever a run fails, BOSS classifies its cause using `harness_failures`,
`model_failures`, and `code_task_service.classify_pipeline_exception`, proposes the concrete
pre-check that would have caught it before spend, links it to issue reporting
(`boss_recommendations`), and persists a deduplicated proposal artifact in `precheck_proposals`.

HARD RULE: Proposals are strictly artifacts (`status = 'proposed'`). BOSS NEVER self-modifies
`workflow_precheck.CHECKS` or any runtime code/policy without explicit human approval.
"""
from __future__ import annotations

import json
import logging
import re
from dataclasses import asdict, dataclass, field
from typing import Any, Mapping, Optional

from sqlalchemy import text
from sqlalchemy.engine import Connection

from .core import emit_event, make_id, now_utc

log = logging.getLogger(__name__)

STATUSES = ("proposed", "approved", "rejected", "implemented")
REVIEW_STATUSES = ("approved", "rejected")
EVENT_RECORDED = "precheck.proposal_recorded"
EVENT_REVIEWED = "precheck.proposal_reviewed"
MAX_RUN_IDS = 25
MAX_EVIDENCE = 8

_BINARY_MISSING = re.compile(
    r"(?:dependencies (?:are )?not available:?\s*[^\n]*?\b(?P<dep>[a-z0-9_.-]+)\s+(?:is )?not installed)"
    r"|\b(?P<bin>socat|bwrap|bubblewrap|git|node|npm|python3?|docker)\s+(?:is )?not installed\b"
    r"|\b(?:command not found|no such file or directory):\s*['\"]?(?P<cmd>[a-z0-9_.-]+)['\"]?",
    re.I,
)
_DISCONNECTED_SOURCE = re.compile(
    r"disconnected|reported connected|not logged in|oauth token has expired|token has expired|"
    r"login was rejected|failed to authenticate|invalid api key|invalid bearer token|"
    r"authentication_error|claude sign-in needed|could not read the login|"
    r"source .* is not connected|no eligible (?:model|source)|no_eligible_source|"
    r"no model is connected|\b[a-z0-9_]+_(?:base_url|api_key)\s+is\s+not\s+configured\b",
    re.I,
)
_CAPS_VS_METERED = re.compile(
    r"metered|budget cap|daily cap|monthly cap|burst cap|spending cap|max[_ ]budget|"
    r"error_max_budget_usd|budget reservation denied|budget denied|budget exhaust|"
    r"insufficient budget|budget is red|budget is orange|credit balance is too low|"
    r"insufficient[_ ]credits?|insufficient_quota|cannot fit .*cap|cap.*cannot fit|"
    r"exceeds (?:the )?(?:daily|monthly|burst|spending) cap",
    re.I,
)
_ENV_MISSING = re.compile(
    r"\b([A-Z][A-Z0-9_]{2,})\b[^\n]{0,80}(?:must be set|is required|not set|missing)",
)
_REPO_ACCESS = re.compile(
    r"repository not found|repo_access_denied|could not read from remote repository|"
    r"permission to \S+ denied|403.*github|github.*403",
    re.I,
)
_BUILD_SERVICES = re.compile(
    r"build services unavailable|build_services_unavailable|postgres.*connection refused",
    re.I,
)
_STACK_UNSUPPORTED = re.compile(
    r"stack_unsupported|which boss cannot build|boss toolchains enable (\w+)",
    re.I,
)


@dataclass(frozen=True)
class PrecheckProposal:
    """Deterministic pre-check proposal produced from a single run failure."""

    cause_class: str
    reason_code: str
    dedupe_key: str
    proposed_check_id: str
    existing_check_id: str
    checkpoint: str
    target_capability: str
    title: str
    summary: str
    why: str
    evidence: tuple[str, ...] = ()
    probe_spec: dict[str, Any] = field(default_factory=dict)
    files: tuple[str, ...] = ()
    acceptance_tests: tuple[str, ...] = ()
    auto_apply: bool = False  # hard rule: never self-modifying without approval

    def as_dict(self) -> dict[str, Any]:
        out = asdict(self)
        out["evidence"] = list(self.evidence)
        out["files"] = list(self.files)
        out["acceptance_tests"] = list(self.acceptance_tests)
        return out


def _scrub(value: Any, limit: int = 400) -> str:
    """Scrub secrets and host paths before storing or returning failure evidence."""
    from ..identity.redaction import redact_text
    from .redaction import mask_url_credentials, redact_secrets

    raw = str(value or "").strip()
    if not raw:
        return ""
    try:
        cleaned = redact_text(mask_url_credentials(redact_secrets(raw)), public=True)
    except Exception:  # noqa: BLE001 — fail closed on redaction error
        return "[redacted]"
    return re.sub(r"\s+", " ", cleaned).strip()[:limit]


def _extract_missing_binary(text_in: str) -> str:
    match = _BINARY_MISSING.search(text_in or "")
    if not match:
        return ""
    for group in ("dep", "bin", "cmd"):
        val = match.group(group)
        if val:
            return re.sub(r"[^a-z0-9_.-]+", "", val.lower())
    return ""


def _existing_check_registered(check_id: str) -> bool:
    if not check_id:
        return False
    try:
        from .workflow_precheck import CHECKS

        return check_id in CHECKS
    except Exception:  # noqa: BLE001
        return False


def classify_and_propose(
    *,
    error: Any,
    run_id: str = "",
    run_kind: str = "code_task",
    engine: Optional[str] = None,
    source_id: Optional[str] = None,
    failure_class: Optional[str] = None,
    reason_code: Optional[str] = None,
    stage: Optional[str] = None,
    harness_verdict: Optional[Mapping[str, Any]] = None,
    context: Optional[Mapping[str, Any]] = None,
) -> PrecheckProposal:
    """Classify a run failure and deterministically propose the pre-check that catches it.

    Pure function (no DB writes, no model calls, never self-modifying). Replays of the
    2026-10-14 failures (socat missing, disconnected source reported connected, caps vs
    metered billing) each yield a distinct, actionable proposal.
    """
    from .code_task_service import classify_pipeline_exception
    from .harness_failures import classify as classify_harness, parse_sentence
    from .model_failures import explain as explain_model

    raw_error = str(error or "").strip()
    scrubbed = _scrub(raw_error, 500)
    ctx = dict(context or {})
    eng = str(engine or ctx.get("engine") or (harness_verdict or {}).get("engine") or "").strip()
    src = str(source_id or ctx.get("source_id") or "").strip()
    stage_name = str(stage or ctx.get("stage") or "build").strip() or "build"
    capability = str(ctx.get("capability") or ("deploy" if stage_name == "deploy" else "build"))

    # Run existing classifiers so every proposal agrees with BOSS's canonical taxonomy.
    verdict = None
    if isinstance(harness_verdict, Mapping) and harness_verdict.get("kind"):
        verdict = dict(harness_verdict)
    else:
        parsed_harness = parse_sentence(raw_error)
        if parsed_harness:
            eng = eng or parsed_harness.get("engine", "")
        hv = classify_harness(raw_error, engine=eng or None, where="engine")
        if hv is not None:
            verdict = hv.as_dict()

    explained = explain_model(raw_error, source_id=src or None)
    fclass, rcode = classify_pipeline_exception(raw_error)
    rcode = str(reason_code or rcode or explained.get("kind") or "unknown")
    fclass = str(failure_class or fclass or "unknown")

    evidence_lines = [line for line in (scrubbed,) if line]
    if verdict and verdict.get("sentence"):
        sent = _scrub(verdict["sentence"], 300)
        if sent and sent not in evidence_lines:
            evidence_lines.append(sent)
    if explained.get("headline"):
        head = _scrub(explained["headline"], 300)
        if head and head not in evidence_lines:
            evidence_lines.append(head)
    evidence = tuple(evidence_lines[:MAX_EVIDENCE])

    # ── 1. Missing runtime / sandbox binary (2026-10-14: socat missing) ──────────────────────
    missing_bin = _extract_missing_binary(raw_error)
    if missing_bin or (verdict and verdict.get("kind") in ("missing_runtime", "sandbox_unavailable")):
        binary_slug = missing_bin or "sandbox"
        cause_class = "harness.missing_runtime" if missing_bin or (
            verdict and verdict.get("kind") == "missing_runtime"
        ) else "harness.sandbox_unavailable"
        proposed_id = f"sandbox.engine.binary.{binary_slug}" if missing_bin else "sandbox.engine.startup"
        title = (
            f"Pre-check runtime binary `{missing_bin}` before starting {eng or 'engine'} runs"
            if missing_bin
            else f"Pre-check sandbox startup for {eng or 'engine'} before starting runs"
        )
        summary = (
            f"Verify `{missing_bin}` is installed on PATH (and declared in harness_registry.RuntimeBinary "
            f"for {eng or 'the routed engine'}) during pre_run pre-check; fail with will_fail before spend."
            if missing_bin
            else f"Probe {eng or 'the routed engine'} sandbox initialization during pre_run pre-check "
                 "so a broken sandbox stops before agent turns run blind."
        )
        return PrecheckProposal(
            cause_class=cause_class,
            reason_code=rcode,
            dedupe_key=f"precheck:{cause_class}:{proposed_id}",
            proposed_check_id=proposed_id,
            existing_check_id="sandbox.engine",
            checkpoint="pre_run",
            target_capability=capability,
            title=title,
            summary=summary,
            why="Missing sandbox/runtime dependencies kill runs after spend or leave agents working blind.",
            evidence=evidence,
            probe_spec={
                "kind": "binary_and_sandbox_probe",
                "binary": missing_bin or None,
                "engine": eng or "routed_engine",
                "expected_verdict": "will_fail",
                "extends_existing_check": _existing_check_registered("sandbox.engine"),
                "fix_action": (
                    f"Install `{missing_bin}` in the worker/runtime image and register it in "
                    "harness_registry.RuntimeBinary."
                    if missing_bin
                    else "Verify container seccomp/AppArmor sandbox prerequisites before starting."
                ),
            },
            files=(
                "engine/boss_engine/services/workflow_precheck.py",
                "engine/boss_engine/services/harness_registry.py",
                "engine/boss_engine/services/harness_failures.py",
            ),
            acceptance_tests=(
                f"workflow_precheck returns verdict='will_fail' on pre_run when `{binary_slug}` is absent from PATH.",
                "Run fails fast at the start door with zero model spend.",
            ),
        )

    # ── 2. Caps vs metered billing (2026-10-14: caps cannot fit metered billing) ─────────────
    if (
        explained.get("kind") in ("budget_refused", "budget_hold")
        or (verdict and verdict.get("kind") == "credit_refused")
        or rcode in ("budget_cap", "budget_denied", "budget_exhausted")
        or _CAPS_VS_METERED.search(raw_error)
        or bool(ctx.get("metered_billing") and ctx.get("cap_usd") is not None)
    ):
        cause_class = "budget.cap_vs_metered_billing"
        proposed_id = "budget.run_cost.metered_fit"
        cap_hint = ctx.get("cap_usd")
        est_hint = ctx.get("estimated_cost_usd")
        detail = (
            f" (estimated ${est_hint} vs cap ${cap_hint})"
            if cap_hint is not None and est_hint is not None
            else ""
        )
        return PrecheckProposal(
            cause_class=cause_class,
            reason_code=rcode,
            dedupe_key=f"precheck:{cause_class}:{proposed_id}",
            proposed_check_id=proposed_id,
            existing_check_id="budget.run_cost",
            checkpoint="pre_run",
            target_capability=capability,
            title="Pre-check that project budget caps fit metered billing for routed stages",
            summary=(
                "Price the workflow/code-task stages at metered list rates against remaining daily, "
                f"monthly, and burst caps before reserving budget{detail}; return will_fail when "
                "a metered route cannot fit the configured cap."
            ),
            why="Starting a metered run whose expected cost exceeds daily/monthly/burst caps burns "
                "partial budget before failing mid-run.",
            evidence=evidence,
            probe_spec={
                "kind": "budget_metered_fit",
                "source_id": src or None,
                "cap_usd": cap_hint,
                "estimated_cost_usd": est_hint,
                "expected_verdict": "will_fail",
                "extends_existing_check": _existing_check_registered("budget.run_cost"),
                "fix_action": "Raise the daily/monthly/burst cap in Govern › Limits or route to a "
                              "subscription/lower-cost model tier before starting.",
            },
            files=(
                "engine/boss_engine/services/workflow_precheck.py",
                "engine/boss_engine/services/run_cost_estimate.py",
                "engine/boss_engine/services/model_failures.py",
            ),
            acceptance_tests=(
                "workflow_precheck budget.run_cost returns verdict='will_fail' when metered estimated "
                "run cost exceeds the remaining daily, monthly, or burst cap.",
                "No budget reservation is consumed when the metered cap pre-check fails.",
            ),
        )

    # ── 3. Disconnected source reported connected (2026-10-14) ───────────────────────────────
    if (
        explained.get("kind") in ("login_rejected", "credential_unreadable", "no_model", "credential_misdirected")
        or (verdict and verdict.get("kind") == "auth_refused")
        or rcode in ("model_auth_rejected", "model_not_configured", "credential_plain_http", "credential_misdirected")
        or _DISCONNECTED_SOURCE.search(raw_error)
        or bool(ctx.get("source_reported_connected") and ctx.get("source_usable") is False)
    ):
        cause_class = "model.source_disconnected"
        proposed_id = "sandbox.model.live_source_state"
        src_label = src or eng or "routed model source"
        return PrecheckProposal(
            cause_class=cause_class,
            reason_code=rcode,
            dedupe_key=f"precheck:{cause_class}:{proposed_id}",
            proposed_check_id=proposed_id,
            existing_check_id="sandbox.model",
            checkpoint="pre_run",
            target_capability=capability,
            title=f"Pre-check live connectivity of routed model source ({src_label})",
            summary=(
                f"Verify `engine_startup.source_state` and `model_readiness.usable_model` for every "
                f"stage's actual routed source ({src_label}) rather than trusting a cached connected "
                "flag or chat fallback; return will_fail when the source is disconnected or rejected."
            ),
            why="A source reported connected from stale state or another job's route lets a run "
                "reserve budget and fail immediately on the first model call.",
            evidence=evidence,
            probe_spec={
                "kind": "model_source_live_state",
                "source_id": src or None,
                "engine": eng or None,
                "reported_connected": ctx.get("source_reported_connected"),
                "expected_verdict": "will_fail",
                "extends_existing_check": _existing_check_registered("sandbox.model"),
                "fix_action": f"Reconnect {src_label} in Setup › Models or pin the stage to a "
                              "working connected source.",
            },
            files=(
                "engine/boss_engine/services/workflow_precheck.py",
                "engine/boss_engine/services/model_readiness.py",
                "engine/boss_engine/services/model_failures.py",
            ),
            acceptance_tests=(
                "workflow_precheck sandbox.model returns verdict='will_fail' when the stage's routed "
                "source is disconnected or rejected even if another source appears connected.",
                "model_readiness.usable_model agrees with sandbox.model on disconnected sources.",
            ),
        )

    # ── 4. Additional deterministic run-killer classes ───────────────────────────────────────
    if rcode == "repo_access_denied" or _REPO_ACCESS.search(raw_error):
        cause_class = "github.access_denied"
        proposed_id = "github.access.verify_push"
        return PrecheckProposal(
            cause_class=cause_class,
            reason_code=rcode,
            dedupe_key=f"precheck:{cause_class}:{proposed_id}",
            proposed_check_id=proposed_id,
            existing_check_id="github.access",
            checkpoint="pre_run",
            target_capability=capability,
            title="Pre-check repository read/push access before starting build",
            summary="Verify the run's bound GitHub repository and branch accept read/push before starting.",
            why="Repository access failures waste build setup when permissions are missing.",
            evidence=evidence,
            probe_spec={
                "kind": "github_access_probe",
                "expected_verdict": "will_fail",
                "extends_existing_check": _existing_check_registered("github.access"),
                "fix_action": "Reconnect the repository with push access in Setup.",
            },
            files=("engine/boss_engine/services/workflow_precheck.py",),
            acceptance_tests=("github.access pre-check fails with will_fail when repo access is denied.",),
        )

    if rcode == "build_services_unavailable" or _BUILD_SERVICES.search(raw_error):
        cause_class = "sandbox.build_services_unavailable"
        proposed_id = "sandbox.build_services.reachable"
        return PrecheckProposal(
            cause_class=cause_class,
            reason_code=rcode,
            dedupe_key=f"precheck:{cause_class}:{proposed_id}",
            proposed_check_id=proposed_id,
            existing_check_id="sandbox.build_services",
            checkpoint="pre_run",
            target_capability=capability,
            title="Pre-check build-services database reachability before starting build",
            summary="Ping build-services Postgres before starting builds that declare a database.",
            why="A build that needs Postgres cannot pass tests when build-services is down.",
            evidence=evidence,
            probe_spec={
                "kind": "build_services_ping",
                "expected_verdict": "will_fail",
                "extends_existing_check": _existing_check_registered("sandbox.build_services"),
                "fix_action": "Start build-services on the BOSS host before retrying.",
            },
            files=("engine/boss_engine/services/workflow_precheck.py",),
            acceptance_tests=("sandbox.build_services returns will_fail when Postgres is unreachable.",),
        )

    if rcode == "stack_unsupported" or _STACK_UNSUPPORTED.search(raw_error):
        cause_class = "sandbox.toolchain_unsupported"
        proposed_id = "sandbox.toolchain.stack_match"
        return PrecheckProposal(
            cause_class=cause_class,
            reason_code=rcode,
            dedupe_key=f"precheck:{cause_class}:{proposed_id}",
            proposed_check_id=proposed_id,
            existing_check_id="sandbox.toolchain",
            checkpoint="pre_run",
            target_capability=capability,
            title="Pre-check requested language toolchain is enabled before starting",
            summary="Verify the brief's target stack matches an enabled BOSS toolchain at pre_run.",
            why="Starting a build for a disabled or unsupported toolchain fails deterministically.",
            evidence=evidence,
            probe_spec={
                "kind": "toolchain_stack_check",
                "expected_verdict": "will_fail",
                "extends_existing_check": _existing_check_registered("sandbox.toolchain"),
                "fix_action": "Enable the required toolchain (`boss toolchains enable <lang>`) or update the brief.",
            },
            files=("engine/boss_engine/services/workflow_precheck.py",),
            acceptance_tests=("sandbox.toolchain returns will_fail when the brief requests a disabled stack.",),
        )

    env_match = _ENV_MISSING.search(raw_error)
    if env_match:
        var_name = env_match.group(1)
        cause_class = "app.missing_env"
        proposed_id = f"app_env.required.{var_name.lower()}"
        return PrecheckProposal(
            cause_class=cause_class,
            reason_code=rcode,
            dedupe_key=f"precheck:{cause_class}:{proposed_id}",
            proposed_check_id=proposed_id,
            existing_check_id="app_env",
            checkpoint="pre_deploy" if capability == "deploy" else "pre_run",
            target_capability=capability,
            title=f"Pre-check required environment variable `{var_name}` is bound",
            summary=f"Verify `{var_name}` is bound for the target environment before running the step.",
            why="Unbound required environment variables fail startup only after build/deploy spend.",
            evidence=evidence,
            probe_spec={
                "kind": "app_env_binding",
                "env_var": var_name,
                "expected_verdict": "fixable",
                "extends_existing_check": _existing_check_registered("app_env"),
                "fix_action": f"Bind `{var_name}` in app secrets/environment before retrying.",
            },
            files=("engine/boss_engine/services/workflow_precheck.py",),
            acceptance_tests=(f"app_env pre-check flags missing `{var_name}` before starting.",),
        )

    if explained.get("kind") == "storage":
        scause = str(explained.get("storage_cause") or "other")
        cause_class = f"infra.storage.{scause}"
        proposed_id = "sandbox.disk.headroom" if scause == "disk_full" else f"infra.db.{scause}"
        existing = "sandbox.disk" if scause == "disk_full" else "sandbox.worker"
        return PrecheckProposal(
            cause_class=cause_class,
            reason_code=rcode,
            dedupe_key=f"precheck:{cause_class}:{proposed_id}",
            proposed_check_id=proposed_id,
            existing_check_id=existing,
            checkpoint="pre_run",
            target_capability=capability,
            title=f"Pre-check storage health ({scause}) before starting runs",
            summary=f"Verify database/disk health for `{scause}` during pre_run before starting work.",
            why="Storage/database failures abort runs mid-flight after partial work.",
            evidence=evidence,
            probe_spec={
                "kind": "storage_health_probe",
                "storage_cause": scause,
                "expected_verdict": "will_fail",
                "extends_existing_check": _existing_check_registered(existing),
                "fix_action": str(explained.get("action") or "Run `boss doctor` on the host."),
            },
            files=(
                "engine/boss_engine/services/workflow_precheck.py",
                "engine/boss_engine/services/model_failures.py",
            ),
            acceptance_tests=(f"pre_run detects storage `{scause}` condition before starting.",),
        )

    # ── 5. Fallback: every failed run still classifies & proposes a concrete pre-check ───────
    safe_code = re.sub(r"[^a-z0-9_]+", "_", rcode.lower()).strip("_") or "generic"
    cause_class = f"run.{fclass}.{safe_code}"
    proposed_id = f"run.failure_guard.{safe_code}"
    return PrecheckProposal(
        cause_class=cause_class,
        reason_code=rcode,
        dedupe_key=f"precheck:{cause_class}:{proposed_id}",
        proposed_check_id=proposed_id,
        existing_check_id="",
        checkpoint="pre_run",
        target_capability=capability,
        title=f"Propose pre-check for recurring `{safe_code}` run failure",
        summary=(
            f"Classify and add a pre_run check covering `{safe_code}` failures "
            f"(observed in {run_kind} {run_id or 'run'})."
        ),
        why="Every run failure should either be caught at pre-check or explicitly triaged.",
        evidence=evidence,
        probe_spec={
            "kind": "failure_signature_guard",
            "failure_class": fclass,
            "reason_code": rcode,
            "stage": stage_name,
            "expected_verdict": "unknown",
            "extends_existing_check": False,
            "fix_action": "Review the captured failure evidence and approve a targeted pre-check PR.",
        },
        files=(
            "engine/boss_engine/services/workflow_precheck.py",
            "engine/boss_engine/services/build_failure.py",
        ),
        acceptance_tests=(
            f"Replay of `{safe_code}` failure triggers the new pre-check before model spend.",
        ),
    )


def _json(value: Any, default: Any) -> Any:
    if isinstance(value, str):
        try:
            return json.loads(value)
        except ValueError:
            return default
    return default if value is None else value


def record_failure_proposal(
    connection: Connection,
    settings: Any,
    *,
    project_id: str,
    run_id: str,
    error: Any,
    run_kind: str = "code_task",
    engine: Optional[str] = None,
    source_id: Optional[str] = None,
    failure_class: Optional[str] = None,
    reason_code: Optional[str] = None,
    stage: Optional[str] = None,
    harness_verdict: Optional[Mapping[str, Any]] = None,
    context: Optional[Mapping[str, Any]] = None,
    actor: str = "boss",
) -> dict[str, Any]:
    """Classify a failed run, link to boss_recommendations, and persist a deduplicated proposal.

    Never self-modifies runtime checks and never raises to mask the caller's failure handling.
    """
    proposal = classify_and_propose(
        error=error,
        run_id=run_id,
        run_kind=run_kind,
        engine=engine,
        source_id=source_id,
        failure_class=failure_class,
        reason_code=reason_code,
        stage=stage,
        harness_verdict=harness_verdict,
        context=context,
    )
    if connection is None or not project_id:
        return {"id": None, "created": False, "recommendation_id": None, "proposal": proposal.as_dict()}

    # 1. Link to BOSS issue reporting (boss_recommendations) with matching signature.
    rec_id: Optional[str] = None
    try:
        from . import boss_recommendations

        rec = boss_recommendations.record(
            connection,
            settings,
            project_id=project_id,
            signature=proposal.dedupe_key,
            title=proposal.title,
            symptom=proposal.evidence[0] if proposal.evidence else proposal.summary,
            evidence=list(proposal.evidence),
            root_cause=f"Cause class `{proposal.cause_class}` ({proposal.reason_code}): {proposal.why}",
            proposed_change=f"Add/extend pre-check `{proposal.proposed_check_id}` ({proposal.checkpoint}): {proposal.summary}",
            files=list(proposal.files),
            ref={"run_id": run_id, "run_kind": run_kind, "cause_class": proposal.cause_class},
            actor=actor,
            run_id=run_id if run_kind == "workflow_run" else None,
            acceptance_tests=list(proposal.acceptance_tests),
        )
        rec_id = rec.get("id")
    except Exception as exc:  # noqa: BLE001 — recommendation failure must not block proposal persistence
        log.warning("precheck proposal recommendation link skipped: %s", exc)

    # 2. Upsert deduplicated proposal artifact in precheck_proposals.
    point = connection.begin_nested()
    try:
        row = connection.execute(
            text(
                "SELECT id, run_ids, occurrences, status, recommendation_id "
                "FROM precheck_proposals "
                "WHERE project_id = :p AND dedupe_key = :k FOR UPDATE"
            ),
            {"p": project_id, "k": proposal.dedupe_key},
        ).mappings().first()
        if row:
            existing_runs = [str(r) for r in _json(row["run_ids"], []) if r]
            seen = bool(run_id and run_id in existing_runs)
            next_runs = (existing_runs + ([] if (seen or not run_id) else [run_id]))[-MAX_RUN_IDS:]
            occurrences = int(row["occurrences"] or 1) + (0 if seen else 1)
            linked_rec = row["recommendation_id"] or rec_id
            connection.execute(
                text(
                    "UPDATE precheck_proposals "
                    "SET run_ids = CAST(:runs AS JSONB), occurrences = :occ, "
                    "    last_run_id = COALESCE(NULLIF(:rid, ''), last_run_id), "
                    "    recommendation_id = COALESCE(:rec, recommendation_id), "
                    "    evidence = CAST(:ev AS JSONB), updated_at = now() "
                    "WHERE id = :id"
                ),
                {
                    "runs": json.dumps(next_runs),
                    "occ": occurrences,
                    "rid": run_id or "",
                    "rec": linked_rec,
                    "ev": json.dumps(list(proposal.evidence)),
                    "id": row["id"],
                },
            )
            prop_id = str(row["id"])
            created = False
            status = str(row["status"])
        else:
            prop_id = make_id("pcp")
            run_ids = [run_id] if run_id else []
            connection.execute(
                text(
                    "INSERT INTO precheck_proposals ("
                    "  id, project_id, dedupe_key, run_id, last_run_id, run_kind, run_ids, "
                    "  cause_class, reason_code, proposed_check_id, existing_check_id, "
                    "  checkpoint, target_capability, title, summary, why, evidence, "
                    "  probe_spec, files, acceptance_tests, recommendation_id, status, "
                    "  occurrences, created_by"
                    ") VALUES ("
                    "  :id, :p, :dk, :rid, :rid, :rk, CAST(:rids AS JSONB), "
                    "  :cc, :rc, :pcid, :ecid, :cp, :cap, :title, :summary, :why, "
                    "  CAST(:ev AS JSONB), CAST(:ps AS JSONB), CAST(:files AS JSONB), "
                    "  CAST(:tests AS JSONB), :rec, 'proposed', 1, :by"
                    ")"
                ),
                {
                    "id": prop_id,
                    "p": project_id,
                    "dk": proposal.dedupe_key[:300],
                    "rid": run_id or "",
                    "rk": run_kind[:40],
                    "rids": json.dumps(run_ids),
                    "cc": proposal.cause_class[:120],
                    "rc": proposal.reason_code[:120],
                    "pcid": proposal.proposed_check_id[:160],
                    "ecid": proposal.existing_check_id[:160],
                    "cp": proposal.checkpoint[:40],
                    "cap": proposal.target_capability[:80],
                    "title": proposal.title[:300],
                    "summary": proposal.summary[:2000],
                    "why": proposal.why[:1000],
                    "ev": json.dumps(list(proposal.evidence)),
                    "ps": json.dumps(proposal.probe_spec),
                    "files": json.dumps(list(proposal.files)),
                    "tests": json.dumps(list(proposal.acceptance_tests)),
                    "rec": rec_id,
                    "by": actor[:120],
                },
            )
            occurrences = 1
            linked_rec = rec_id
            created = True
            status = "proposed"

        emit_event(
            connection,
            project_id,
            EVENT_RECORDED,
            actor,
            "precheck_proposal",
            prop_id,
            {
                "proposal_id": prop_id,
                "dedupe_key": proposal.dedupe_key,
                "cause_class": proposal.cause_class,
                "reason_code": proposal.reason_code,
                "proposed_check_id": proposal.proposed_check_id,
                "recommendation_id": linked_rec,
                "run_id": run_id,
                "created": created,
                "occurrences": occurrences,
                "status": status,
                "auto_apply": False,
            },
        )
        point.commit()
    except Exception as exc:  # noqa: BLE001 — never mask original run failure
        point.rollback()
        log.warning("precheck proposal persistence failed for %s: %s", run_id, exc)
        return {
            "id": None,
            "created": False,
            "recommendation_id": rec_id,
            "proposal": proposal.as_dict(),
            "error": str(exc)[:300],
        }

    return {
        "id": prop_id,
        "created": created,
        "occurrences": occurrences,
        "status": status,
        "recommendation_id": linked_rec,
        "proposal": proposal.as_dict(),
    }


def _row_to_dict(row: Mapping[str, Any]) -> dict[str, Any]:
    item = dict(row)
    for key in ("run_ids", "evidence", "files", "acceptance_tests"):
        item[key] = _json(item.get(key), [])
    item["probe_spec"] = _json(item.get("probe_spec"), {})
    for key in ("created_at", "updated_at", "reviewed_at"):
        val = item.get(key)
        item[key] = val.isoformat() if hasattr(val, "isoformat") else val
    item["auto_apply"] = False
    return item


def list_proposals(
    connection: Connection,
    project_id: str,
    *,
    status: Optional[str] = None,
    run_id: Optional[str] = None,
    cause_class: Optional[str] = None,
) -> list[dict[str, Any]]:
    """List pre-check proposals for a project (tenant-scoped)."""
    clauses = ["project_id = :p"]
    params: dict[str, Any] = {"p": project_id}
    if status:
        if status not in STATUSES:
            raise ValueError(f"status must be one of {', '.join(STATUSES)}")
        clauses.append("status = :s")
        params["s"] = status
    if cause_class:
        clauses.append("cause_class = :cc")
        params["cc"] = cause_class
    if run_id:
        clauses.append("(run_id = :rid OR last_run_id = :rid OR run_ids ? :rid)")
        params["rid"] = run_id
    rows = connection.execute(
        text(
            "SELECT id, project_id, dedupe_key, run_id, last_run_id, run_kind, run_ids, "
            "  cause_class, reason_code, proposed_check_id, existing_check_id, checkpoint, "
            "  target_capability, title, summary, why, evidence, probe_spec, files, "
            "  acceptance_tests, recommendation_id, status, occurrences, created_by, "
            "  reviewed_by, reviewed_at, review_note, created_at, updated_at "
            f"FROM precheck_proposals WHERE {' AND '.join(clauses)} "
            "ORDER BY updated_at DESC, created_at DESC"
        ),
        params,
    ).mappings().all()
    return [_row_to_dict(r) for r in rows]


def review_proposal(
    connection: Connection,
    *,
    project_id: str,
    proposal_id: str,
    status: str,
    actor: str,
    note: str = "",
) -> dict[str, Any]:
    """Explicit human review of a pre-check proposal (approve/reject). Never auto-applies code."""
    if status not in REVIEW_STATUSES:
        raise ValueError(f"status must be one of {', '.join(REVIEW_STATUSES)}")
    who = str(actor or "").strip()
    if not who or who in ("boss", "system", "sys_worker", "agent"):
        raise ValueError("a human actor is required to review a pre-check proposal")
    row = connection.execute(
        text(
            "UPDATE precheck_proposals "
            "SET status = :s, reviewed_by = :by, reviewed_at = :at, review_note = :note, "
            "    updated_at = now() "
            "WHERE id = :id AND project_id = :p "
            "RETURNING id, project_id, dedupe_key, run_id, last_run_id, run_kind, run_ids, "
            "  cause_class, reason_code, proposed_check_id, existing_check_id, checkpoint, "
            "  target_capability, title, summary, why, evidence, probe_spec, files, "
            "  acceptance_tests, recommendation_id, status, occurrences, created_by, "
            "  reviewed_by, reviewed_at, review_note, created_at, updated_at"
        ),
        {
            "s": status,
            "by": who[:120],
            "at": now_utc(),
            "note": _scrub(note, 500),
            "id": proposal_id,
            "p": project_id,
        },
    ).mappings().first()
    if row is None:
        raise LookupError(f"pre-check proposal {proposal_id} not found in project {project_id}")
    out = _row_to_dict(row)
    emit_event(
        connection,
        project_id,
        EVENT_REVIEWED,
        who,
        "precheck_proposal",
        proposal_id,
        {
            "proposal_id": proposal_id,
            "status": status,
            "cause_class": out["cause_class"],
            "proposed_check_id": out["proposed_check_id"],
            "recommendation_id": out["recommendation_id"],
            "note": out["review_note"],
        },
    )
    return out


def proposal_card(proposal: Mapping[str, Any]) -> dict[str, Any]:
    """UI/API card representation for a pre-check proposal."""
    return {
        "id": proposal.get("id") or proposal.get("dedupe_key"),
        "kind": "precheck_proposal",
        "status": proposal.get("status", "proposed"),
        "cause_class": proposal.get("cause_class"),
        "reason_code": proposal.get("reason_code"),
        "proposed_check_id": proposal.get("proposed_check_id"),
        "existing_check_id": proposal.get("existing_check_id") or "",
        "checkpoint": proposal.get("checkpoint", "pre_run"),
        "title": proposal.get("title"),
        "summary": proposal.get("summary"),
        "why": proposal.get("why"),
        "recommendation_id": proposal.get("recommendation_id"),
        "occurrences": int(proposal.get("occurrences") or 1),
        "auto_apply": False,
    }


__all__ = [
    "EVENT_RECORDED",
    "EVENT_REVIEWED",
    "PrecheckProposal",
    "REVIEW_STATUSES",
    "STATUSES",
    "classify_and_propose",
    "list_proposals",
    "proposal_card",
    "record_failure_proposal",
    "review_proposal",
]
