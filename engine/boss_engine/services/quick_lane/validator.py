"""QL-3 (BOSSAPP-797): Independent validation by a different agent before PR opens.

The builder NEVER validates its own work (docs/product/QUICK_LANE.md §3.4):
- Runs in a fresh context on a different model whenever one is available.
- Review pass: reads the whole diff, verifies every PR claim against evidence, checks blast
  radius callers, catches synthetic fixtures and out-of-scope changes (reproducing the
  2026-10-31 mistakes).
- Independent test pass: runs its own check against real data or replays the failing run;
  never reuses only the builder's tests.
- Bounded revision rounds back into the builder's warm session; unresolved disagreements
  escalate through the decision ladder (coded facts -> decider/Jev -> person decision).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Iterable, Optional

from sqlalchemy.engine import Connection

from .. import fix_lane_triage
from ..core import create_blocking_decision, emit_event
from .context import ContextBundle, ReproductionRecord

#: Maximum builder <-> validator revision rounds before escalating via the decision ladder.
MAX_VALIDATION_ROUNDS = 2

#: Distinct role keys so the validator never shares identity or context with the builder.
DEFAULT_BUILDER_ROLE = "quick_builder"
DEFAULT_VALIDATOR_ROLE = "quick_validator"


@dataclass(frozen=True)
class ValidationFinding:
    """One concrete finding raised by the independent validator."""

    severity: str  # "P1", "P2", "P3"
    code: str
    message: str
    location: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "severity": self.severity,
            "code": self.code,
            "message": self.message,
            "location": self.location,
        }


@dataclass(frozen=True)
class IndependentTestResult:
    """Result of the validator's own independent test / failing-run replay."""

    command: str
    passed: bool
    output: str
    replayed_failing_run: bool = False
    used_real_data_shapes: bool = True
    test_paths: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "command": self.command,
            "passed": self.passed,
            "output": self.output,
            "replayed_failing_run": self.replayed_failing_run,
            "used_real_data_shapes": self.used_real_data_shapes,
            "test_paths": list(self.test_paths),
        }


@dataclass
class ValidationVerdict:
    """Full verdict from one independent validation pass."""

    approved: bool
    validator_role: str
    validator_model: str
    builder_model: str
    fresh_context: bool
    findings: list[ValidationFinding] = field(default_factory=list)
    independent_test: Optional[IndependentTestResult] = None
    round_number: int = 1
    decision_ladder: Optional[dict[str, Any]] = None

    @property
    def blocking_findings(self) -> list[ValidationFinding]:
        return [f for f in self.findings if f.severity == "P1"]

    def to_dict(self) -> dict[str, Any]:
        return {
            "approved": self.approved,
            "validator_role": self.validator_role,
            "validator_model": self.validator_model,
            "builder_model": self.builder_model,
            "fresh_context": self.fresh_context,
            "findings": [f.to_dict() for f in self.findings],
            "independent_test": self.independent_test.to_dict() if self.independent_test else None,
            "round_number": self.round_number,
            "decision_ladder": self.decision_ladder,
        }


def select_validator_model(
    builder_model: str,
    available_models: Optional[Iterable[str]] = None,
) -> str:
    """Pick a DIFFERENT model (preferring a different provider family) for the validator.

    Falls back to ``builder_model`` only when no other model is available.
    """
    candidates = [str(m).strip() for m in (available_models or []) if str(m).strip()]
    if not candidates:
        return builder_model
    b_low = builder_model.strip().lower()
    b_family = b_low.split("/", 1)[0].split("-", 1)[0]

    # Prefer a model from a different provider family first.
    for cand in candidates:
        c_low = cand.lower()
        c_family = c_low.split("/", 1)[0].split("-", 1)[0]
        if c_low != b_low and c_family != b_family:
            return cand
    # Otherwise any distinct model string.
    for cand in candidates:
        if cand.lower() != b_low:
            return cand
    return builder_model


def _caller_name(entry: Any) -> str:
    if isinstance(entry, str):
        return entry.strip()
    if isinstance(entry, dict):
        return str(
            entry.get("symbol_key")
            or entry.get("name")
            or entry.get("caller")
            or entry.get("path")
            or ""
        ).strip()
    return ""


def _is_non_auth_request(spec: dict[str, Any]) -> bool:
    blob = " ".join(
        str(spec.get(k) or "") for k in ("title", "instructions", "area", "kind")
    ).lower()
    return not any(tok in blob for tok in ("auth", "permission", "rbac", "identity", "security"))


def validate_quick_change(
    *,
    spec: dict[str, Any],
    context_bundle: ContextBundle,
    reproduction: Optional[ReproductionRecord],
    diff_files: dict[str, str],
    pr_claims: Optional[list[dict[str, Any]]] = None,
    builder_test_paths: Optional[Iterable[str]] = None,
    builder_tests_used_synthetic_fixture: bool = False,
    independent_test: Optional[IndependentTestResult] = None,
    builder_role: str = DEFAULT_BUILDER_ROLE,
    validator_role: str = DEFAULT_VALIDATOR_ROLE,
    builder_model: str = "claude-sonnet",
    available_models: Optional[Iterable[str]] = None,
    extra_reviewer: Optional[Callable[[dict[str, Any]], list[ValidationFinding]]] = None,
    round_number: int = 1,
) -> ValidationVerdict:
    """Run independent validation (review + independent test) on a builder's proposed diff.

    Catches the exact failure modes from 2026-10-31 (BOSSAPP-797):
    1. Builder trying to validate its own work (same role).
    2. Unreproduced cause (missing repro, exit_code == 0, or missing file:line cause).
    3. Synthetic fixture in builder tests instead of real system data shapes.
    4. Unread caller in KG blast radius.
    5. Out-of-scope changes (e.g., permission/protected-path edit in a non-auth fix).
    6. Unbacked PR claims (claim with no file:line or evidence).
    7. Missing or non-independent test (reusing only the builder's tests, or failing check).
    """
    findings: list[ValidationFinding] = []
    validator_model = select_validator_model(builder_model, available_models)

    if validator_role == builder_role:
        findings.append(
            ValidationFinding(
                severity="P1",
                code="same_agent_validation",
                message="The builder may never validate its own work; use a distinct validator agent.",
            )
        )

    # 1. Reproduction check
    if reproduction is None or reproduction.exit_code == 0 or not reproduction.causes:
        findings.append(
            ValidationFinding(
                severity="P1",
                code="unreproduced_cause",
                message="Fix lacks a verified failing reproduction with file:line causes.",
            )
        )

    # 2. Synthetic fixture guard
    if builder_tests_used_synthetic_fixture:
        findings.append(
            ValidationFinding(
                severity="P1",
                code="synthetic_fixture",
                message=(
                    "Builder tests used an invented synthetic fixture instead of the real "
                    "data shape produced by the system."
                ),
            )
        )
    else:
        for path, diff_text in (diff_files or {}).items():
            low = diff_text.lower()
            if "test" in path.lower() and (
                "synthetic_fixture" in low or "fake_symbol_shape" in low or "# invented shape" in low
            ):
                findings.append(
                    ValidationFinding(
                        severity="P1",
                        code="synthetic_fixture",
                        message=f"Test diff in {path} introduces a synthetic fixture shape.",
                        location=path,
                    )
                )

    # 3. Unread blast-radius callers guard
    kg_callers = [_caller_name(c) for c in (context_bundle.kg.get("callers") or [])]
    kg_callers = [c for c in kg_callers if c]
    checked = set(reproduction.checked_callers if reproduction else ())
    unread_callers = [c for c in kg_callers if c not in checked]
    if unread_callers:
        findings.append(
            ValidationFinding(
                severity="P1",
                code="unread_caller",
                message=(
                    f"Blast-radius callers were not checked before PR: "
                    f"{', '.join(unread_callers[:5])}"
                ),
                location=unread_callers[0],
            )
        )

    # 4. Out-of-scope changes (e.g., permission/auth change inside a non-auth fix)
    touched = list((diff_files or {}).keys())
    protected = fix_lane_triage._touches_protected_path(touched)
    if protected is not None:
        findings.append(
            ValidationFinding(
                severity="P1",
                code="out_of_scope_change",
                message=f"Diff touches protected/out-of-scope path: {protected}.",
                location=protected,
            )
        )
    elif _is_non_auth_request(spec):
        for path, diff_text in (diff_files or {}).items():
            if "@permit(" in diff_text or "ROLE_PERMISSIONS" in diff_text or "permission=" in diff_text:
                findings.append(
                    ValidationFinding(
                        severity="P1",
                        code="out_of_scope_change",
                        message=f"Out-of-scope permission/auth change in {path} for a non-auth fix.",
                        location=path,
                    )
                )

    # 5. Every PR claim must carry evidence
    for claim in pr_claims or []:
        text_claim = str(claim.get("claim") or "").strip()
        evidence = str(claim.get("evidence") or "").strip()
        if text_claim and not evidence:
            findings.append(
                ValidationFinding(
                    severity="P1",
                    code="unverified_pr_claim",
                    message=f"PR claim has no backing evidence: {text_claim!r}.",
                )
            )

    # 6. Independent test check (must exist, pass, use real data shapes, and NOT merely
    #    re-run the builder's own test files)
    builder_tests = {str(p).strip() for p in (builder_test_paths or []) if str(p).strip()}
    if independent_test is None:
        findings.append(
            ValidationFinding(
                severity="P1",
                code="missing_independent_test",
                message="Validator must run its own independent test or failing-run replay before PR.",
            )
        )
    else:
        ind_paths = {str(p).strip() for p in independent_test.test_paths if str(p).strip()}
        if (
            not independent_test.replayed_failing_run
            and builder_tests
            and ind_paths
            and ind_paths.issubset(builder_tests)
        ):
            findings.append(
                ValidationFinding(
                    severity="P1",
                    code="reused_builder_tests",
                    message=(
                        "Independent test only reused the builder's own test file(s); "
                        "validator must write/run its own check or replay the failing run."
                    ),
                )
            )
        if not independent_test.used_real_data_shapes:
            findings.append(
                ValidationFinding(
                    severity="P1",
                    code="synthetic_fixture",
                    message="Independent check did not use real data shapes.",
                )
            )
        if not independent_test.passed:
            findings.append(
                ValidationFinding(
                    severity="P1",
                    code="independent_test_failed",
                    message=f"Independent check failed: {independent_test.output[:300]}",
                )
            )

    # 7. Optional pluggable validator-agent review (fresh context, no builder transcript)
    if extra_reviewer is not None:
        review_input = {
            "spec": spec,
            "diff_files": diff_files,
            "study_notes": context_bundle.study_notes,
            "kg_callers": kg_callers,
            "reproduction": reproduction.to_dict() if reproduction else None,
            "pr_claims": pr_claims or [],
            "validator_model": validator_model,
            "fresh_context": True,
        }
        findings.extend(extra_reviewer(review_input) or [])

    approved = not any(f.severity == "P1" for f in findings)
    return ValidationVerdict(
        approved=approved,
        validator_role=validator_role,
        validator_model=validator_model,
        builder_model=builder_model,
        fresh_context=True,
        findings=findings,
        independent_test=independent_test,
        round_number=round_number,
    )


def _escalate_via_decision_ladder(
    connection: Optional[Connection],
    *,
    project_id: str,
    run_id: str,
    actor: str,
    verdict: ValidationVerdict,
    decider: Optional[Callable[[dict[str, Any]], dict[str, Any]]] = None,
) -> dict[str, Any]:
    """Escalate an unresolved builder/validator disagreement through the decision ladder.

    1. Coded facts (if protected path -> escalate directly to full workflow).
    2. Automated decider (Jev / decision model) if supplied and confident.
    3. Person blocking decision (create_blocking_decision) so nothing silently drops.
    """
    codes = {f.code for f in verdict.blocking_findings}
    if "out_of_scope_change" in codes:
        return {
            "tier": "coded_facts",
            "outcome": "escalate_full_workflow",
            "reason": "Out-of-scope or protected path change requires full workflow.",
        }

    if decider is not None:
        answer = decider(
            {
                "question": "quick_lane_validation_disagreement",
                "run_id": run_id,
                "findings": [f.to_dict() for f in verdict.blocking_findings],
            }
        )
        confidence = float(answer.get("confidence") or 0.0)
        if answer.get("action") in ("escalate_full_workflow", "accept_with_note") and confidence >= 0.8:
            return {
                "tier": "decider",
                "outcome": answer["action"],
                "confidence": confidence,
                "reason": str(answer.get("reason") or "Decider settled disagreement."),
            }

    decision_id: Optional[str] = None
    if connection is not None:
        decision_id = create_blocking_decision(
            connection,
            project_id,
            None,
            f"Quick-lane validation disagreement on {run_id}",
            {
                "action": "escalate_full_workflow",
                "run_id": run_id,
                "findings": [f.to_dict() for f in verdict.blocking_findings],
            },
            {"rounds": verdict.round_number},
            (
                f"Builder and independent validator could not resolve "
                f"{len(verdict.blocking_findings)} P1 finding(s) after {verdict.round_number} round(s)."
            ),
        )
        emit_event(
            connection,
            project_id,
            "quick_lane.validation_escalated",
            actor,
            "quick_lane_run",
            run_id,
            {"decision_id": decision_id, "rounds": verdict.round_number},
        )
    return {
        "tier": "person",
        "outcome": "escalate_full_workflow",
        "decision_id": decision_id,
        "reason": "Unresolved disagreement escalated to decision ladder / full workflow.",
    }


def run_validation_loop(
    *,
    connection: Optional[Connection],
    project_id: str,
    run_id: str,
    actor: str,
    spec: dict[str, Any],
    context_bundle: ContextBundle,
    initial_state: dict[str, Any],
    builder_revise: Optional[Callable[[list[ValidationFinding], int, str], dict[str, Any]]] = None,
    independent_tester: Optional[Callable[[dict[str, Any], int], IndependentTestResult]] = None,
    extra_reviewer: Optional[Callable[[dict[str, Any]], list[ValidationFinding]]] = None,
    decider: Optional[Callable[[dict[str, Any]], dict[str, Any]]] = None,
    builder_model: str = "claude-sonnet",
    available_models: Optional[Iterable[str]] = None,
    max_rounds: int = MAX_VALIDATION_ROUNDS,
    session_id: str = "warm_session",
) -> tuple[ValidationVerdict, dict[str, Any], list[ValidationVerdict]]:
    """Run bounded builder <-> independent-validator rounds in the builder's warm session."""
    state = dict(initial_state)
    history: list[ValidationVerdict] = []
    last_verdict: Optional[ValidationVerdict] = None

    for round_no in range(1, max(1, max_rounds) + 1):
        ind_test = (
            independent_tester(state, round_no)
            if independent_tester is not None
            else state.get("independent_test")
        )
        verdict = validate_quick_change(
            spec=spec,
            context_bundle=context_bundle,
            reproduction=state.get("reproduction"),
            diff_files=state.get("diff_files") or {},
            pr_claims=state.get("pr_claims"),
            builder_test_paths=state.get("builder_test_paths"),
            builder_tests_used_synthetic_fixture=bool(
                state.get("builder_tests_used_synthetic_fixture", False)
            ),
            independent_test=ind_test,
            builder_model=builder_model,
            available_models=available_models,
            extra_reviewer=extra_reviewer,
            round_number=round_no,
        )
        history.append(verdict)
        last_verdict = verdict
        if verdict.approved:
            return verdict, state, history
        if round_no < max_rounds and builder_revise is not None:
            revised = builder_revise(verdict.blocking_findings, round_no, session_id)
            if isinstance(revised, dict):
                state.update(revised)

    assert last_verdict is not None
    ladder = _escalate_via_decision_ladder(
        connection,
        project_id=project_id,
        run_id=run_id,
        actor=actor,
        verdict=last_verdict,
        decider=decider,
    )
    last_verdict.decision_ladder = ladder
    return last_verdict, state, history
