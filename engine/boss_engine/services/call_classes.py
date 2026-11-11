"""Every model call has a CLASS, and a project can govern each class (BOSSAPP-999, audit §7.2).

The recording seam (llm_telemetry.record_llm_call) stamps `llm_calls.call_class` on every row
it writes, derived from the stage the call's boundary named — so "what is chat costing us
versus builds?" is a GROUP BY, not an archaeology dig. The mapping is TOTAL: every known stage
maps, an unknown stage lands in 'other', and nothing here can raise out of the seam.

The same classes are the unit of governance. A per-project setting (`call_classes`, the
ordinary project_settings knob) can deny a class outright or cap it by calls-per-day and
tokens-per-day — VOLUME caps, deliberately: a $0-settling subscription call cannot be
money-capped today, but it is still a call and still tokens. The gate (`authorize`) runs where
calls are authorized — GovernedChatAdapter's reserve, the model gateway's lease, and the chat
adapter seam from BOSSAPP-998 — and a refusal is WORDED: a chat turn answers with it, a build
fails its start with it, and an audit event says what was refused and why.

Defaults are inert: no setting means everything allowed, no caps, zero behaviour change.
"""
from __future__ import annotations

import json
import logging
from typing import Any, Optional

from sqlalchemy import text

log = logging.getLogger("boss.call_classes")

#: Every class a model call can belong to. The set is closed on purpose: a governance surface
#: over an open set is a surface nobody can reason about. New stages land in an existing class
#: (usually by an entry in STAGE_CLASSES), not in a new class.
CALL_CLASSES: tuple[str, ...] = (
    "build_stage",  # the governed pipeline's stages (implement, review_code, …)
    "chat",         # chat turns, agent converse/drive turns
    "panel",        # meeting panel seats and facilitators
    "decision",     # agent decisions, typed decisions, architecture rulings
    "proof",        # connection proofs (setup's "does this credential work?")
    "kg",           # knowledge-graph work (rule scans, knowledge pipelines)
    "bench",        # benchmark/bake-off runs
    "gateway",      # gateway-settled calls with no more specific boundary (research, settlements)
    "other",        # the total mapping's floor — never NULL, never a crash
)

#: Stage → class, exact and lowercase. Sources: llm_telemetry.STAGE_ROLES (the build pipeline),
#: token_efficiency.KNOWN_STAGES, the BOSSAPP-998 seam surfaces (chat / panel_member /
#: agent_decision / connection_proof / model_call), and the stage literals the services set via
#: set_llm_context. A stage absent here is 'other' — visible, counted, governable as 'other'.
STAGE_CLASSES: dict[str, str] = {
    # The governed build pipeline.
    "standards": "build_stage", "spec": "build_stage", "plan": "build_stage",
    "review_plan": "build_stage", "design": "build_stage", "write_tests": "build_stage",
    "implement": "build_stage", "repair": "build_stage", "review_code": "build_stage",
    "consistency_check": "build_stage", "security_check": "build_stage",
    # The merged review panel: one session, every LLM seat (BOSSAPP-1000).
    "review_panel": "build_stage",
    "check_tests": "build_stage", "run_all_tests": "build_stage",
    "test_arbitration": "build_stage", "guest": "build_stage", "solo": "build_stage",
    "quality": "build_stage", "precheck": "build_stage", "workflow_precheck": "build_stage",
    "regression": "build_stage", "pull_request": "build_stage", "spec_review": "build_stage",
    "prd": "build_stage", "docs": "build_stage", "work": "build_stage", "build": "build_stage",
    # Chat-shaped turns: the operator talking, an agent conversing, a drive investigating.
    "chat": "chat", "converse": "chat", "model_call": "chat", "onboarding": "chat",
    "triage": "chat", "investigate": "chat",
    # Panels.
    "panel_member": "panel", "panel_facilitator": "panel", "meeting": "panel",
    # Decisions.
    "agent_decision": "decision", "decision": "decision", "decide": "decision",
    "typed_decision": "decision", "typed_decision_batch": "decision",
    "architecture": "decision",
    # Connection proofs.
    "connection_proof": "proof",
    # Knowledge-graph work.
    "rule_scan": "kg", "knowledge": "kg", "kg": "kg", "embedding": "kg", "scan": "kg",
    # Benchmarks.
    "bench": "bench", "benchmark": "bench",
    # Gateway-settled calls with no better boundary name.
    "model_gateway": "gateway", "research": "gateway", "research_synthesis": "gateway",
    "discovery": "gateway",
}


def class_of(stage: Optional[str]) -> str:
    """The call class a stage belongs to. TOTAL: any input — None, '', an unknown name, a
    non-string — comes back as a class, 'other' at worst. Never raises."""
    try:
        name = str(stage or "").strip().lower()
    except Exception:  # noqa: BLE001 — a stage that cannot even stringify is 'other'
        return "other"
    if not name:
        return "other"
    exact = STAGE_CLASSES.get(name)
    if exact:
        return exact
    if name.startswith("kg_"):
        return "kg"
    if name.startswith("bench"):
        return "bench"
    if name.startswith("panel"):
        return "panel"
    return "other"


# ── The per-project policy (project_settings knob `call_classes`) ─────────────────────────────

KNOB_KEY = "call_classes"

_RULE_FIELDS = ("allow", "calls_per_day", "tokens_per_day")


def coerce_policy(value: Any) -> dict[str, dict[str, Any]]:
    """Validate + normalise a `call_classes` knob value, or say exactly why it will not do.

    Shape: {"<class>": {"allow": bool, "calls_per_day": int|null, "tokens_per_day": int|null}}.
    Unknown classes and unknown fields are refused rather than stored — a typo'd class name
    that is silently kept is governance that silently never happens."""
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError as error:
            raise ValueError(f"Model call classes has to be JSON: {error}") from error
    if not isinstance(value, dict):
        raise ValueError("Model call classes has to be an object of "
                         '{"<class>": {"allow", "calls_per_day", "tokens_per_day"}}.')
    out: dict[str, dict[str, Any]] = {}
    for cls, rules in value.items():
        name = str(cls or "").strip().lower()
        if name not in CALL_CLASSES:
            raise ValueError(f"'{cls}' is not a call class. Classes: {', '.join(CALL_CLASSES)}.")
        if not isinstance(rules, dict):
            raise ValueError(f"The rules for '{name}' have to be an object "
                             "(allow, calls_per_day, tokens_per_day).")
        unknown = sorted(set(rules) - set(_RULE_FIELDS))
        if unknown:
            raise ValueError(f"'{name}' has fields BOSS does not govern: {', '.join(unknown)}. "
                             f"Known: {', '.join(_RULE_FIELDS)}.")
        clean: dict[str, Any] = {"allow": bool(rules.get("allow", True))}
        for cap in ("calls_per_day", "tokens_per_day"):
            raw = rules.get(cap)
            if raw is None:
                clean[cap] = None
                continue
            try:
                number = int(raw)
            except (TypeError, ValueError) as error:
                raise ValueError(f"{name}.{cap} has to be a whole number.") from error
            if number < 0:
                raise ValueError(f"{name}.{cap} has to be 0 or more.")
            clean[cap] = number
        out[name] = clean
    return out


def policy_for(connection: Any, settings: Any, project_id: Optional[str]) -> dict:
    """The project's effective call-class policy, {} when none is set. Never raises."""
    try:
        from . import project_settings

        stored = project_settings.effective(connection, project_id, settings, KNOB_KEY)
        if not stored:
            return {}
        return coerce_policy(stored)
    except Exception:  # noqa: BLE001 — a policy that cannot be read governs nothing
        log.warning("call-class policy could not be read; allowing", exc_info=True)
        return {}


# ── The gate ──────────────────────────────────────────────────────────────────────────────────


def _refusal_base():
    from .github_adapter import IntegrationConfigurationError

    return IntegrationConfigurationError


class CallClassRefused(_refusal_base()):  # type: ignore[misc]
    """The project's call-class policy refused this call BEFORE anything was sent to a model.
    Settled: retrying does not change the answer (a daily cap frees itself at midnight UTC,
    and the message says so). The message is the operator-facing sentence."""


def _used_today(connection: Any, project_id: str, call_class: str) -> tuple[int, int]:
    """(calls, total tokens) this project's class has used since midnight UTC. A store that
    predates the call_class column answers (0, 0) — the gate opens rather than crashes."""
    try:
        with connection.begin_nested():
            row = connection.execute(
                text("SELECT count(*) AS calls, COALESCE(sum(total_tokens), 0) AS tokens "
                     "FROM llm_calls WHERE project_id = :p AND call_class = :c "
                     "AND created_at >= date_trunc('day', now())"),
                {"p": project_id, "c": call_class},
            ).mappings().first()
        return int(row["calls"] or 0), int(row["tokens"] or 0)
    except Exception:  # noqa: BLE001 — usage that cannot be read caps nothing
        log.warning("call-class usage could not be read; allowing", exc_info=True)
        return 0, 0


def _audit(connection: Any, project_id: str, call_class: str, stage: Optional[str],
           reason: str, rule: dict) -> None:
    """An audit event for every refusal, best-effort: the record of a refusal is not the
    refusal. ON ITS OWN SHORT TRANSACTION when the caller's connection has an engine — the
    refusal is about to be RAISED, and most callers' transactions roll back with it, which
    would roll the audit back too."""
    try:
        from .core import emit_event

        def _emit(c: Any) -> None:
            emit_event(c, project_id, "model_call.class_refused", "boss",
                       "call_class", call_class,
                       {"call_class": call_class, "stage": stage, "reason": reason,
                        "rule": rule})

        engine = getattr(connection, "engine", None)
        if engine is not None:
            from ..db import begin

            with begin(engine) as c:
                _emit(c)
        else:
            with connection.begin_nested():
                _emit(connection)
    except Exception:  # noqa: BLE001
        log.warning("could not audit a call-class refusal", exc_info=True)


def _token_policy_refusal(connection: Any, project_id: str,
                          stage: Optional[str]) -> Optional[str]:
    """A STORED token-budget policy's daily token cap, enforced (BOSSAPP-999 §3).

    Govern's token guides (token_budget_policies) were advisory while looking editable. The
    per-day volume half is enforceable exactly like a class cap, so it is — HERE, through the
    same gate. Stored, enabled rows only: the documented defaults the console displays are
    guides, and must never become a surprise cap on an install nobody configured."""
    name = str(stage or "").strip().lower()
    if not name:
        return None
    try:
        with connection.begin_nested():
            rows = connection.execute(
                text("SELECT stage, max_tokens_per_day FROM token_budget_policies "
                     "WHERE project_id = :p AND enabled AND stage IN (:s, '*') "
                     "AND max_tokens_per_day IS NOT NULL ORDER BY stage = '*'"),
                {"p": project_id, "s": name},
            ).mappings().all()
            if not rows:
                return None
            policy = rows[0]  # the stage's own row wins over the '*' project default
            scope = "AND stage = :s " if policy["stage"] != "*" else ""
            used = connection.execute(
                text("SELECT COALESCE(sum(total_tokens), 0) FROM llm_calls "
                     f"WHERE project_id = :p {scope}"
                     "AND created_at >= date_trunc('day', now())"),
                {"p": project_id, "s": name},
            ).scalar() or 0
    except Exception:  # noqa: BLE001 — a policy that cannot be read caps nothing
        log.warning("token-budget policy could not be read; allowing", exc_info=True)
        return None
    cap = int(policy["max_tokens_per_day"])
    if int(used) < cap:
        return None
    which = f"the '{policy['stage']}' stage" if policy["stage"] != "*" else "this project"
    return (f"The token budget for {which} is spent: {int(used):,} of {cap:,} tokens today "
            f"(Govern › Token guides). The cap resets at midnight UTC. "
            f"Nothing was sent to a model.")


def authorize(connection: Any, settings: Any, project_id: Optional[str], *,
              stage: Optional[str] = None, call_class: Optional[str] = None) -> None:
    """Allow this call, or raise CallClassRefused with the operator-facing sentence.

    Runs where calls are authorized, BEFORE anything reaches a provider: a refused call spends
    nothing, records no llm_calls row, and emits one audit event. Infrastructure failures
    (no table yet, a read that errors) ALLOW — governance that silently fails closed would
    turn a migration hiccup into a dead install — but an explicit deny or a met cap refuses."""
    if not project_id or connection is None:
        return
    cls = (call_class or "").strip().lower() or class_of(stage)
    policy = policy_for(connection, settings, project_id)
    rule = policy.get(cls) or {}
    refusal: Optional[str] = None
    if rule and rule.get("allow") is False:
        refusal = (f"This project's call-class policy denies '{cls}' model calls "
                   f"(project setting '{KNOB_KEY}'). Nothing was sent to a model.")
    elif rule.get("calls_per_day") is not None or rule.get("tokens_per_day") is not None:
        calls, tokens = _used_today(connection, project_id, cls)
        cap_calls = rule.get("calls_per_day")
        cap_tokens = rule.get("tokens_per_day")
        if cap_calls is not None and calls >= cap_calls:
            refusal = (f"'{cls}' model calls have used their daily volume cap for this project "
                       f"({calls} of {cap_calls} calls today; project setting '{KNOB_KEY}'). "
                       f"The cap resets at midnight UTC. Nothing was sent to a model.")
        elif cap_tokens is not None and tokens >= cap_tokens:
            refusal = (f"'{cls}' model calls have used their daily token cap for this project "
                       f"({tokens:,} of {cap_tokens:,} tokens today; project setting "
                       f"'{KNOB_KEY}'). The cap resets at midnight UTC. "
                       f"Nothing was sent to a model.")
    if refusal is None:
        refusal = _token_policy_refusal(connection, project_id, stage)
        if refusal is None:
            return
    _audit(connection, project_id, cls, stage, refusal, rule)
    raise CallClassRefused(refusal)


def gate_call(settings: Any, project_id: Optional[str], stage: Optional[str]) -> None:
    """`authorize` for a caller that holds no connection (the chat adapter seam): a short
    transaction on the ambient context's engine, exactly as the recording seam writes. A gate
    that cannot even open a connection allows (and says so at warning) — but a refusal is a
    refusal, re-raised to the caller in words."""
    if not project_id:
        return
    try:
        from ..db import begin
        from .llm_telemetry import get_llm_context

        with begin(get_llm_context().get("db_engine")) as connection:
            authorize(connection, settings, project_id, stage=stage)
    except CallClassRefused:
        raise
    except Exception:  # noqa: BLE001 — see authorize: infrastructure never fails closed
        log.warning("call-class gate could not run; allowing", exc_info=True)
