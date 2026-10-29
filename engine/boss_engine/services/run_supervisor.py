"""L2a: the run supervisor.

Watches the code-task repair loop and workflow-step recovery windows for stalled progress.
The progress signal is deterministic and cheap (no DB, no model call); when it trips, or a
hard invariant forces it, exactly one governed judgement call picks one of the five fixed
actions in run_supervisor_types.ACTIONS. Judgement can never override the invariants: the
3-round cap, the zero-caused-failure rule, and the refusal allow-list.

L1 (classification: WORK/HARNESS/PERSON) is not merged yet, so `evaluate_signal` is coded
against the narrow signal this module needs — a repeated/identical failure signature across
rounds, no shrink in the failing set — rather than against L1's classifier directly. Once L1
lands, callers can keep passing the same `faults` mapping; nothing here needs to change.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Mapping, Optional

from .run_supervisor_types import (
    ACTION_SETTING,
    ACTIONS,
    DECIDED_EVENT,
    DEFAULT_STALL_MINUTES,
    DEGRADED,
    JUDGEMENT_SETTING,
    MAX_ROUNDS_WITHOUT_JUDGEMENT,
    PLATFORM_FIX,
    ProgressSignal,
    REACH_OUT,
    SIGNAL_SETTING,
    STEER,
    STOP_REPORT,
    CHANGE_APPROACH,
)

WORK = "WORK"
HARNESS = "HARNESS"
PERSON = "PERSON"

#: The run supervisor's own identity for the one governed judgement call it makes.
AGENT_NAME = "boss run supervisor"
MAX_JUDGEMENT_TURNS = 1
MAX_JUDGEMENT_USD_MICROS = 150_000
MAX_PACK_CHARS = 8_000

SITUATION = ("{agent} watches a run's repair loop for stalled progress: BOSS carries out the "
             "action it chooses through its governed paths.")
REQUEST = ("Run {run_id} has stopped making progress ({reason}). Choose the ONE next action "
           "that gets it going again, or stops it cleanly, from the evidence in the context.")
SYSTEM_PROMPT = """You are {agent}. A run's repair loop is not making progress and you must \
choose ONE next action from a fixed set, using only the structured evidence you are given.

Reason with these principles, not a rules table:
- establish a baseline before fixing: tell caused failures (this change's fault) apart from \
environment failures (not this change's fault);
- if two attempts gave the same result, change approach rather than repeating it;
- every claim needs evidence — cite the gate names/signatures you were given;
- when there is no real progress, stop spending rather than let the loop continue;
- ask a person only for what BOSS cannot do itself, as one concrete question.
"""

_NOTE = {"type": "string", "description": "what's caused, what to ignore, and what to try next"}
MEANS = {
    STEER: "name what's caused, what to ignore as environment/pre-existing, and what to try "
           "in the next round",
    CHANGE_APPROACH: "two attempts gave the same result; reproduce in isolation, split the "
                      "task, switch model/engine, or narrow the scope",
    PLATFORM_FIX: "a repeated environment-failure signature; route to a platform-level fix",
    STOP_REPORT: "no progress; stop and report, naming both the caused and environment sets "
                 "when both are non-empty",
    REACH_OUT: "the blocker needs a person; ask one concrete question",
}
ARGUMENTS: dict[str, dict[str, Any]] = {
    STEER: {"properties": {"note": _NOTE}},
    CHANGE_APPROACH: {"properties": {"note": _NOTE}},
    PLATFORM_FIX: {"properties": {"note": _NOTE}},
    STOP_REPORT: {"properties": {"summary": {
        "type": "string", "description": "names both caused and environment sets when mixed"}}},
    REACH_OUT: {"properties": {
        "question": {"type": "string", "description": "the one concrete ask"},
        "after": {"type": "string", "description": "what unblocks the run once answered"}}},
}


def seconds_since(started_at: Optional[str]) -> int:
    """Wall-clock seconds since an ISO-8601 timestamp; 0 if unknown/unparseable."""
    if not started_at:
        return 0
    try:
        started = datetime.fromisoformat(str(started_at).replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return 0
    if started.tzinfo is None:
        started = started.replace(tzinfo=timezone.utc)
    return max(0, int((datetime.now(timezone.utc) - started).total_seconds()))


def _flag(connection: Any, settings: Any, project_id: Optional[str], setting: str) -> bool:
    """One project_settings-backed on/off flag (default on). Never raises: a setting that
    cannot be read is the default, same pattern as checks_failing.enabled."""
    from . import project_settings

    value: Any = None
    try:
        if connection is not None and project_id:
            with connection.begin_nested():
                value = project_settings.effective(connection, project_id, settings, setting)
        else:
            value = getattr(settings, setting, 1)
    except Exception:  # noqa: BLE001
        value = getattr(settings, setting, 1)
    try:
        return int(1 if value in (None, "") else value) == 1
    except (TypeError, ValueError):
        return True


def signal_enabled(connection: Any, settings: Any, project_id: Optional[str]) -> bool:
    """Whether the signal (evaluate_signal) should even be computed for this round."""
    return _flag(connection, settings, project_id, SIGNAL_SETTING)


def judgement_enabled(connection: Any, settings: Any, project_id: Optional[str]) -> bool:
    """Whether a tripped/forced signal may call the governed judgement seam."""
    return _flag(connection, settings, project_id, JUDGEMENT_SETTING)


def action_enabled(connection: Any, settings: Any, project_id: Optional[str]) -> bool:
    """Whether the decision is enforced on the caller's control flow."""
    return _flag(connection, settings, project_id, ACTION_SETTING)


def _classify(faults: Mapping[str, str]) -> tuple[tuple[str, ...], tuple[str, ...], tuple[str, ...]]:
    caused = tuple(sorted(g for g, k in faults.items() if k == WORK))
    environment = tuple(sorted(g for g, k in faults.items() if k == HARNESS))
    refusals = tuple(sorted(g for g, k in faults.items() if k == PERSON))
    return caused, environment, refusals


def _repeated_signature(faults: Mapping[str, str], history: list[dict[str, Any]]) -> Optional[str]:
    """A WORK gate whose signature recurs, identically, in the immediately preceding round."""
    caused = {g for g, k in faults.items() if k == WORK}
    if not caused or not history:
        return None
    previous = history[-1].get("faults") or {}
    previous_caused = {g for g, k in previous.items() if k == WORK}
    repeated = sorted(caused & previous_caused)
    return repeated[0] if repeated else None


def evaluate_signal(
    *,
    faults: Mapping[str, str],
    rounds_used: int,
    max_repair_rounds: int,
    history: list[dict[str, Any]],
    spend_usd_micros: int,
    started_at: Optional[str],
    stall_minutes: int = DEFAULT_STALL_MINUTES,
    elapsed_seconds: int = 0,
) -> ProgressSignal:
    caused, environment, refusals = _classify(faults)
    repeated = _repeated_signature(faults, history)
    if not elapsed_seconds and started_at:
        elapsed_seconds = seconds_since(started_at)

    forced = rounds_used >= MAX_ROUNDS_WITHOUT_JUDGEMENT or (not caused and bool(faults))
    stalled = elapsed_seconds >= stall_minutes * 60 and not caused

    if repeated:
        reason, tripped = "repeated_failure", True
    elif not caused and (environment or refusals):
        reason, tripped = ("refusal" if refusals and not environment else "environment_only"), True
    elif stalled:
        reason, tripped = "stalled", True
    elif forced:
        reason, tripped = "round_cap", True
    else:
        reason, tripped = "", False

    return ProgressSignal(
        tripped=tripped,
        reason=reason,
        forced=forced,
        caused=caused,
        environment=environment,
        refusals=refusals,
        repeated_signature=repeated,
        rounds_used=rounds_used,
        spend_usd_micros=spend_usd_micros,
        elapsed_seconds=elapsed_seconds,
    )


def build_evidence_digest(*, run_id: str, project_id: str, signal: ProgressSignal,
                           history: list[dict[str, Any]]) -> dict[str, Any]:
    """Structured fields only — gate names, signatures, counts. Never raw log/stdout text."""
    return {
        "run_id": run_id,
        "project_id": project_id,
        "reason": signal.reason,
        "caused": list(signal.caused),
        "environment": list(signal.environment),
        "refusals": list(signal.refusals),
        "repeated_signature": signal.repeated_signature,
        "rounds_used": signal.rounds_used,
        "spend_usd_micros": signal.spend_usd_micros,
        "elapsed_seconds": signal.elapsed_seconds,
        "prior_rounds": len(history),
    }


def validate_decision(raw: Mapping[str, Any], *, allowed: tuple[str, ...]) -> tuple[Optional[dict], str]:
    action = raw.get("action") if isinstance(raw, Mapping) else None
    if action not in ACTIONS or action not in allowed:
        return None, f"action {action!r} not in allowed set {allowed}"
    return dict(raw), ""


def _model_adapter(*, connection: Any, settings: Any, project_id: str, run_id: str,
                    allowed: tuple[str, ...]) -> Any:
    """The real judgement call (AgentService.decide — the same seam step_recovery.ask_model
    uses), built lazily so a test's injected adapter never has to import agent_service."""
    def _adapter(evidence: Mapping[str, Any]) -> dict[str, Any]:
        from .agent_service import AgentService, Choice

        choices = [Choice(a, MEANS[a], arguments=ARGUMENTS[a]) for a in ACTIONS if a in allowed]
        got = AgentService(settings).decide(
            connection, project_id=project_id, actor=AGENT_NAME, agent=AGENT_NAME,
            external_ref=f"run:{run_id}", ref=f"supervisor:{run_id}",
            situation=SITUATION.format(agent=AGENT_NAME),
            message=REQUEST.format(run_id=run_id, reason=evidence.get("reason") or "no progress"),
            choices=choices, context=dict(evidence), hints=[],
            brief=SYSTEM_PROMPT.format(agent=AGENT_NAME),
            # No read phase: the supervisor reads nothing (no tools), and a read phase routes
            # the decision away from Jev, the decision model (agent_decision: structured + read=None).
            read=None,
            min_confidence=0.0, context_limit=MAX_PACK_CHARS)
        if not got.decided:
            raise ValueError(got.error or got.refused or "no decision")
        return {"action": got.action, **dict(got.arguments or {})}
    return _adapter


def judge(adapter: Any, *, evidence: Mapping[str, Any], allowed: tuple[str, ...],
          connection: Any = None, settings: Any = None, project_id: str = "",
          run_id: str = "") -> dict[str, Any]:
    """One governed call through the injected adapter — or, when none is injected (the real
    production path), through the governed model seam itself. Never raises — callers still
    wrap this, but keeping the try here means a bad adapter never has to be trusted by its
    caller."""
    if adapter is None:
        adapter = _model_adapter(connection=connection, settings=settings,
                                  project_id=project_id, run_id=run_id, allowed=allowed)
    raw = adapter(evidence) if callable(adapter) else adapter
    decision, error = validate_decision(raw or {}, allowed=allowed)
    if decision is None:
        raise ValueError(error)
    return decision


def decide(
    connection: Any,
    settings: Any,
    project_id: str,
    *,
    run_id: str,
    signal: ProgressSignal,
    history: list[dict[str, Any]],
    adapter: Any = None,
) -> dict[str, Any]:
    """Orchestrator: enforces the invariants, then (if needed) calls judgement. Never raises."""
    allowed = signal.allowed_actions

    # A refusal with nothing caused/environment to act on can be decided deterministically —
    # cheaper than a model call, and removes a whole failure mode.
    if allowed == (REACH_OUT, STOP_REPORT) and not signal.caused and not signal.environment:
        if adapter is None:
            return {"action": REACH_OUT, "reason": "refusal needs a person"}

    try:
        evidence = build_evidence_digest(run_id=run_id, project_id=project_id, signal=signal, history=history)
        decision = judge(adapter, evidence=evidence, allowed=allowed, connection=connection,
                          settings=settings, project_id=project_id, run_id=run_id)
    except Exception as exc:  # judgement endpoint down, malformed answer, anything else
        return {"action": STOP_REPORT, "reason": f"judgement failed closed: {exc}", "degraded": False}

    return decision


def record_decision(
    connection: Any,
    *,
    run_id: str,
    project_id: str,
    signal: ProgressSignal,
    decision: Mapping[str, Any],
    outcome: str = "pending",
) -> None:
    """Append-only decision record on the run, plus the matching event for metrics-on-read."""
    from .run_supervisor_types import REACH_OUT as _REACH_OUT

    record = {
        "signal": {
            "reason": signal.reason,
            "caused": list(signal.caused),
            "environment": list(signal.environment),
            "refusals": list(signal.refusals),
            "rounds_used": signal.rounds_used,
        },
        "decision": dict(decision),
        "outcome": outcome,
    }
    existing = []
    getter = getattr(connection, "get_supervisor_history", None)
    if callable(getter):
        try:
            existing = list(getter(run_id) or [])
        except Exception:
            existing = []
    if decision.get("action") == _REACH_OUT and any(
        r.get("decision", {}).get("action") == _REACH_OUT and r.get("outcome") == "pending"
        for r in existing
    ):
        return  # one open reach_out per run

    appender = getattr(connection, "append_supervisor_decision", None)
    if callable(appender):
        appender(run_id, record)

    try:
        from . import core
        core.emit_event(
            connection, project_id, DECIDED_EVENT, "run_supervisor", "run", run_id,
            {"action": decision.get("action"), "reason": signal.reason, "outcome": outcome},
        )
    except Exception:
        pass


def write_lesson(
    connection: Any,
    *,
    project_id: str,
    run_id: str,
    signal: ProgressSignal,
    decision: Mapping[str, Any],
) -> None:
    """One lesson candidate in the KG learning layer. A write failure is logged and swallowed —
    a lesson that can't be written is never a reason to fail the run."""
    try:
        from .knowledge import learning_writeback
        from ..identity.redaction import redact_text

        action = str(decision.get("action") or "")
        question = f"run {run_id}: {signal.reason or 'no-progress'} signature {signal.repeated_signature or 'n/a'}"
        learning_writeback.record(
            connection,
            project_id=project_id,
            learning_kind="decision",
            title=redact_text(f"supervisor chose {action} for run {run_id}"),
            statement=redact_text(decision.get("note") or decision.get("summary") or action),
            detail={"question": question, "chosen": action, "reasons": [], "alternatives": []},
            about={"areas": ["run_supervisor"], "symbols": [], "rules": []},
            evidence=[{
                "type": "metric",
                "ref": f"run:{run_id}",
                "excerpt": redact_text(
                    f"{len(signal.caused)} caused / {len(signal.environment)} environment / "
                    f"{len(signal.refusals)} refusal"
                ),
            }],
            confidence=0.5,
            observed={"run_id": run_id, "step": "supervisor"},
        )
    except Exception:
        pass
