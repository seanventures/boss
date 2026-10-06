"""The facade: one `route()` for every model call BOSS makes, at its boundary.

    RouteRequest ─▶ (a) classify ─▶ (b) policy: pins ▸ floors ▸ budget ▸ escalation
                 ─▶ (c) select a source ─▶ RouteDecision (recorded by store.record)

Pure over a `Snapshot` (policy, sources with headroom, budget state, job pins), which `store`
loads once per project per TTL. In Fixed mode the decision IS the legacy resolution the caller
hands in (`RouteRequest.legacy`), recorded with its reason — so an untouched install routes
exactly as before, by construction (Decision 2).
"""
from __future__ import annotations

import threading
import time
import uuid
from dataclasses import dataclass, field, replace
from typing import Any, Callable, Mapping, Optional

from .classifier import CLASSIFIER, Classification, Classifier, TaskSignal
from .escalation import EscalationLedger
from .headroom import Headroom
from .policy import LOCAL_ENGINES, RoutingPolicy, Target, model_tier, tier_model
from .selection import SourceState, engine_label, select
from .tiers import (
    BALANCED, CODING_STEPS, FRONTIER, LEGACY_CLASS, MECHANICAL_STEPS, TIER_LABELS, TIERS,
    at_least, down, job_for, rank, step_label,
)

KINDS = ("build_stage", "workflow_step", "agent", "chat", "decision", "panel", "research")


@dataclass(frozen=True)
class LegacyRoute:
    """What the pre-router code would have chosen — Fixed mode's whole answer."""

    engine: str
    model: Optional[str]
    why: str
    model_class: Optional[str] = None      # low_cost | strong
    flags: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class RouteRequest:
    kind: str
    step: str
    project_id: Optional[str] = None
    unit_id: Optional[str] = None
    role: Optional[str] = None
    text: str = ""
    spec: Optional[dict] = None
    task_kind: Optional[str] = None
    repo_files: Optional[int] = None
    risk_tier: Optional[int] = None
    attempt: int = 0
    #: A pin on the task itself (`model_config`): spend-cap failover and experiments write it.
    task_pin: Optional[Target] = None
    #: An Agent Studio model policy: {"selection_mode": router|pinned|local, "pinned_model"}.
    agent_policy: Optional[Mapping[str, Any]] = None
    #: Fixed mode's answer, computed lazily (only Fixed mode asks).
    legacy: Optional[Callable[[], LegacyRoute]] = None
    #: An experiment arm or an explicit per-run choice of mode.
    mode_override: Optional[str] = None
    #: Step names whose escalation counts also apply here (implement ↔ solo).
    aliases: tuple[str, ...] = ()
    #: A multi-turn session (an agent run) keeps the model it started on: store.route looks up
    #: the unit's last route and passes it here, so a resumed session is never switched.
    sticky: bool = False
    session_pin: Optional[Target] = None
    #: Which workflow step / panel phase this is, for "Why this model" (not used to route).
    ref: Optional[str] = None
    #: Floors this unit adds to the policy's: a build's own profile (a task that asked for
    #: `strict` gets strict's floors, whatever the project's default preset is).
    extra_floors: tuple = ()
    #: A SOFT pin for this step (a profile's explicit step model): tried first within the tier.
    preferred: Optional[Target] = None

    def signal(self) -> TaskSignal:
        return TaskSignal(kind=self.kind, step=self.step, text=self.text,
                          task_kind=self.task_kind, repo_files=self.repo_files,
                          risk_tier=self.risk_tier, spec=self.spec)


@dataclass(frozen=True)
class RouteDecision:
    id: str
    mode: str
    kind: str
    step: str
    tier: str
    engine: str
    model: Optional[str]
    reason: str
    why: tuple[str, ...]
    classified_tier: Optional[str] = None
    classification: Optional[Classification] = None
    rules: tuple[str, ...] = ()
    source_id: Optional[str] = None
    source_kind: str = ""
    escalation: Optional[str] = None
    escalation_signal: Optional[str] = None
    role: Optional[str] = None
    unit_id: Optional[str] = None
    project_id: Optional[str] = None
    ref: Optional[str] = None
    fallback: bool = False
    moved_by_headroom: bool = False
    budget_lite: bool = False
    latency_ms: float = 0.0
    headroom: Optional[Mapping[str, Any]] = None
    #: Nothing may take this work; `refusal` says why and what fixes it. Callers refuse the call.
    refused: bool = False
    refusal: str = ""
    #: Obeyed even in Fixed mode (an Agent Studio Local/Pinned policy).
    binding: bool = False

    @property
    def label(self) -> str:
        name = engine_label(self.engine) if self.engine else "no model"
        return f"{name} · {self.model}" if self.model else name

    @property
    def metered(self) -> bool:
        return self.source_kind in ("metered", "env", "typesafe")

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id, "mode": self.mode, "kind": self.kind, "step": self.step,
            "step_label": step_label(self.step), "tier": self.tier,
            "tier_label": TIER_LABELS.get(self.tier, self.tier),
            "classified_tier": self.classified_tier,
            "classifier": self.classification.classifier if self.classification else None,
            "confidence": round(self.classification.confidence, 3)
            if self.classification else None,
            "classification": self.classification.to_dict() if self.classification else None,
            "engine": self.engine, "model": self.model, "label": self.label,
            "source_id": self.source_id, "source_kind": self.source_kind,
            "rules": list(self.rules), "reason": self.reason, "why": list(self.why),
            "escalation": self.escalation, "escalation_signal": self.escalation_signal,
            "role": self.role, "unit_id": self.unit_id, "ref": self.ref,
            "fallback": self.fallback,
            "moved_by_headroom": self.moved_by_headroom, "budget_lite": self.budget_lite,
            "latency_ms": round(self.latency_ms, 3), "headroom": dict(self.headroom or {}),
            "refused": self.refused, "refusal": self.refusal, "binding": self.binding,
        }


@dataclass
class Snapshot:
    """Everything a route reads, loaded once per project per TTL (store.snapshot)."""

    project_id: Optional[str]
    policy: RoutingPolicy
    sources: Mapping[str, SourceState]
    budget_state: str = "green"
    #: job -> (engine, model or None): the "Who does what" rows the operator set.
    job_pins: Mapping[str, tuple[str, Optional[str]]] = field(default_factory=dict)
    jev_state: str = "absent"
    settings: Any = None
    loaded_at: float = field(default_factory=time.monotonic)
    #: Builds the Jev decider / cheap-model decider on demand (store wires them).
    jev_factory: Optional[Callable[[], Any]] = None
    model_factory: Optional[Callable[[], Any]] = None


def _parse_pin(value: Any, default_engine: Optional[str]) -> Optional[Target]:
    text = str(value or "").strip()
    if not text:
        return None
    if ":" in text:
        engine, _, model = text.partition(":")
        return Target(engine.strip().lower(), model.strip() or None)
    return Target((default_engine or "").lower(), text) if default_engine else None


class Router:
    """Stateless apart from a per-unit memo (so a step routed twice with the same answer keeps
    one route id) and the shared classifier cache."""

    def __init__(self, classifier: Classifier = CLASSIFIER):
        self.classifier = classifier
        self._memo: dict[tuple[str, str, str], RouteDecision] = {}
        self._lock = threading.Lock()

    # ── public ──────────────────────────────────────────────────────────────────────────────
    def route(self, request: RouteRequest, snapshot: Snapshot, *,
              ledger: Optional[EscalationLedger] = None) -> RouteDecision:
        started = time.perf_counter()
        mode = (request.mode_override or snapshot.policy.mode or "fixed").lower()
        agent_mode = _agent_mode(request)
        if mode != "smart" and agent_mode not in ("local", "pinned"):
            decision = self._fixed(request, snapshot)
        else:
            decision = self._smart(request, snapshot, ledger)
            if mode != "smart":
                # LOCAL MEANS LOCAL, IN FIXED MODE TOO: an Agent Studio Local/Pinned policy is
                # obeyed whatever the routing mode (Decision 15).
                decision = _with(decision, mode="fixed", binding=True,
                                 rules=decision.rules + ("agent_policy",))
        decision = _with(decision, latency_ms=(time.perf_counter() - started) * 1000,
                         ref=request.ref)
        return self._remember(decision)

    def forget(self, unit_id: Optional[str]) -> None:
        with self._lock:
            for key in [k for k in self._memo if k[0] == (unit_id or "")]:
                self._memo.pop(key, None)

    # ── Fixed: the legacy answer, recorded ─────────────────────────────────────────────────
    def _fixed(self, request: RouteRequest, snapshot: Snapshot) -> RouteDecision:
        legacy = request.legacy() if request.legacy else LegacyRoute("", None, "no resolver")
        tier = model_tier(legacy.engine, legacy.model) \
            or LEGACY_CLASS.get(legacy.model_class or "", None) or BALANCED
        state = snapshot.sources.get(legacy.engine)
        why = [f"Fixed: {legacy.why}"]
        if legacy.flags.get("budget_lite"):
            why.append("Budget pressure: strong → low-cost lane")
        if legacy.flags.get("escalated"):
            why.append("Escalated to the hard-problems lane")
        return RouteDecision(
            id=_new_id(), mode="fixed", kind=request.kind, step=request.step, tier=tier,
            engine=legacy.engine, model=legacy.model, reason=why[0], why=tuple(why),
            rules=("fixed",), source_id=state.source_id if state else None,
            source_kind=state.kind if state else "", role=request.role,
            unit_id=request.unit_id, project_id=request.project_id or snapshot.project_id,
            budget_lite=bool(legacy.flags.get("budget_lite")),
            escalation="hard-problems lane" if legacy.flags.get("escalated") else None,
            escalation_signal="final_attempt" if legacy.flags.get("escalated") else None,
            headroom=state.headroom.to_dict() if state else None)

    # ── Smart ──────────────────────────────────────────────────────────────────────────────
    def _smart(self, request: RouteRequest, snapshot: Snapshot,
               ledger: Optional[EscalationLedger]) -> RouteDecision:
        policy = snapshot.policy
        why: list[str] = []
        rules: list[str] = []
        job = job_for(request.kind, request.step)

        # (a) what the task needs
        jev = snapshot.jev_factory() if (snapshot.jev_state == "ready"
                                          and snapshot.jev_factory) else None
        c = self.classifier.classify(request.signal(), policy.classifier, jev=jev,
                                     jev_state=snapshot.jev_state,
                                     model=snapshot.model_factory)
        tier = c.tier
        judge = {"rules": "built-in rules", "jev": "Jev", "model": "a Fast model"}.get(
            c.classifier, c.classifier)
        source = f"{judge}, {round(c.confidence * 100)}% sure"
        why.append(f"Task: {c.reason} → {TIER_LABELS[c.tier]} ({source})"
                   + (f" — {c.fallback_note}" if c.fallback_note else ""))
        rules.append(f"classified:{c.classifier}")
        if request.step in MECHANICAL_STEPS and rank(tier) > 0 and not c.sensitive:
            tier = down(tier)
            why.append(f"{step_label(request.step)} is mechanical → {TIER_LABELS[tier]}")
            rules.append("mechanical")

        # (b) floors
        floor, floors = policy.floor_for(kind=request.kind, step=request.step, role=request.role,
                                         job=job, task_kind=request.task_kind)
        extra = [replace(f, label=f"{f.describe()} (this build's profile)")
                 for f in request.extra_floors
                 if f.matches(kind=request.kind, step=request.step, role=request.role, job=job,
                              task_kind=request.task_kind)]
        if extra:
            floors = list(floors) + extra
            floor = max((f.tier for f in floors), key=rank)
        if c.sensitive and request.kind != "chat":
            floor = FRONTIER if not floor else max((floor, FRONTIER), key=rank)
        if floor and rank(floor) > rank(tier):
            top = max(floors, key=lambda f: rank(f.tier)) if floors else None
            why.append(f"Floor: {top.describe()} ≥ {TIER_LABELS[floor]}" if top is not None
                       else f"Floor: sensitive work ≥ {TIER_LABELS[floor]}")
            rules.append("floor")
            tier = floor

        # (b) budget pressure — never below a floor
        budget_lite = False
        drop = int(policy.budget.get(snapshot.budget_state, 0) or 0)
        if drop and not request.task_pin:
            lowered = at_least(down(tier, drop), floor)
            if lowered != tier:
                why.append(f"Budget {snapshot.budget_state}: {TIER_LABELS[tier]} → "
                           f"{TIER_LABELS[lowered]}")
                rules.append("budget")
                tier, budget_lite = lowered, True

        # (d) escalation, at this boundary
        escalation = None
        if ledger is not None:
            aliases = request.aliases or (tuple(CODING_STEPS - {request.step})
                                          if request.step in CODING_STEPS else ())
            escalation = ledger.escalate(request.step, tier, policy.escalation,
                                         steps_alias=aliases)
            if escalation is not None:
                why.append(f"Escalated: {escalation.describe()}")
                rules.append(f"escalation:{escalation.signal}")
                tier = escalation.tier

        # Pins (Decision 5). HARD: the session's own model, a task pin, an agent pin, a job
        # marked "Always use". SOFT (a preference): any other job pin, a profile's step pin —
        # tried first, but headroom and exhaustion can move it, and a floor raises it.
        hard: Optional[Target] = None
        engine_pin: Optional[str] = None
        preferred: Optional[Target] = request.preferred
        allowed: Optional[set[str]] = None
        strict = False
        hard_is_job = False
        session = request.session_pin
        if session and session.engine:
            problem = _blocked(session, snapshot)
            if problem is None:
                hard = session
                why.append("Same model for the whole session (prompt cache)")
                rules.append("pin:session")
            else:
                why.append(f"The session's model can't run now ({problem}); a new session")
                rules.append("session_broken")
        agent_mode = _agent_mode(request)
        if hard is not None:
            pass
        elif request.task_pin and request.task_pin.engine:
            hard = request.task_pin if request.task_pin.model else None
            engine_pin = None if hard else request.task_pin.engine
            why.append(f"Pinned on the task: {engine_label(request.task_pin.engine)}")
            rules.append("pin:task")
        elif agent_mode == "pinned":
            pinned = _parse_pin((request.agent_policy or {}).get("pinned_model"),
                                (snapshot.job_pins.get(job) or ("claude_code", None))[0])
            if pinned and pinned.engine:
                hard = pinned if pinned.model else None
                engine_pin = None if hard else pinned.engine
                why.append("Pinned on the agent")
                rules.append("pin:agent")
        elif agent_mode == "local":
            allowed, strict = set(LOCAL_ENGINES), True
            pinned = _parse_pin((request.agent_policy or {}).get("pinned_model"), None)
            if pinned and pinned.engine in LOCAL_ENGINES:
                hard = pinned if pinned.model else None
                engine_pin = None if hard else pinned.engine
            why.append("The agent runs on local models only")
            rules.append("pin:agent_local")
        if hard is None and engine_pin is None and allowed is None and agent_mode != "pinned":
            pin = snapshot.job_pins.get(job)
            if pin and pin[0]:
                label = engine_label(pin[0]) + (f" · {pin[1]}" if pin[1] else "")
                if job in policy.hard_pins:
                    hard_is_job = True
                    if pin[1]:
                        hard = Target(pin[0], pin[1])
                    else:
                        engine_pin = pin[0]
                    why.append(f"Job pin (always): {job.capitalize()} → {label}")
                    rules.append("pin:job_hard")
                elif preferred is None:
                    preferred = Target(pin[0], pin[1])
                    why.append(f"Job preference: {job.capitalize()} → {label}")
                    rules.append("prefer:job")
        if preferred is not None and "prefer:job" not in rules and "pin:agent_local" not in rules:
            rules.append("prefer:profile")

        # A floor beats every job pin, hard or soft: the review pinned to haiku under a
        # Frontier floor runs Frontier, and says so.
        if hard is not None and hard_is_job and floor:
            have = model_tier(hard.engine, hard.model)
            if have is not None and rank(have) < rank(floor):
                from .selection import _model_on

                raised = Target(hard.engine, _model_on(policy, hard.engine, tier))
                why.append(f"Floor raised your pin: {hard.model} → {raised.model or 'default'}")
                hard = raised

        # (c) a source for the tier
        fallback = moved = False
        refusal = ""
        no_metered = snapshot.budget_state == "red" and not request.task_pin
        if hard is not None:
            state = snapshot.sources.get(hard.engine)
            target = hard
        else:
            chosen = select(tier, policy, snapshot.sources, allowed=allowed,
                            engine_pin=engine_pin, preferred=preferred,
                            no_metered=no_metered, strict=strict)
            if chosen.target is None and no_metered:
                # BUDGET RED: no new metered spend. An included source's lower tier, down to
                # the floor, before a refusal (Decision 16).
                for lower in (t for t in reversed(TIERS[:rank(tier)])
                              if rank(t) >= rank(floor or "fast")):
                    retry = select(lower, policy, snapshot.sources, allowed=allowed,
                                   no_metered=True, strict=strict)
                    if retry.target is not None:
                        chosen.notes.append(f"Budget spent: {TIER_LABELS[lower]} on an "
                                            "included source instead")
                        retry.notes[:0] = chosen.notes
                        chosen, tier = retry, lower
                        break
            why.extend(chosen.notes)
            if chosen.moved_by_headroom:
                rules.append("headroom")
            if chosen.fallback:
                rules.append("fallback")
            fallback, moved = chosen.fallback, chosen.moved_by_headroom
            target, state = chosen.target, chosen.source
            refusal = chosen.refusal
        if target is None:
            refusal = refusal or "Nothing connected can take this work."
            why.append(refusal)
            return RouteDecision(
                id=_new_id(), mode="smart", kind=request.kind, step=request.step, tier=tier,
                engine="", model=None, reason=refusal, why=tuple(why), classified_tier=c.tier,
                classification=c, rules=tuple(rules) + ("refused",), role=request.role,
                unit_id=request.unit_id,
                project_id=request.project_id or snapshot.project_id, fallback=True,
                refused=True, refusal=refusal)
        # THE RECORD NEVER DISAGREES WITH THE MODEL: a pinned or session model is recorded at
        # its own tier, whatever tier the task asked for.
        if hard is not None:
            tier = model_tier(target.engine, target.model) or tier
        picked = f"{TIER_LABELS[tier]} · {engine_label(target.engine)}" + (
            f" · {target.model}" if target.model else "")
        reason = picked + " — " + _headline(why, c)
        return RouteDecision(
            id=_new_id(), mode="smart", kind=request.kind, step=request.step, tier=tier,
            engine=target.engine, model=target.model, reason=reason, why=tuple(why),
            classified_tier=c.tier, classification=c, rules=tuple(rules),
            source_id=state.source_id if state else None,
            source_kind=state.kind if state else "",
            escalation=escalation.describe() if escalation else None,
            escalation_signal=escalation.signal if escalation else None,
            role=request.role, unit_id=request.unit_id,
            project_id=request.project_id or snapshot.project_id, fallback=fallback,
            moved_by_headroom=moved, budget_lite=budget_lite,
            headroom=state.headroom.to_dict() if state else None)

    def _remember(self, decision: RouteDecision) -> RouteDecision:
        """The same answer for the same unit and step keeps its route id: one record per
        distinct decision, however often a pipeline asks (arbitration re-routes review_code)."""
        key = (decision.unit_id or "", decision.kind, f"{decision.step}|{decision.ref or ''}")
        with self._lock:
            prior = self._memo.get(key)
            if prior is not None and decision.unit_id and (
                    prior.engine, prior.model, prior.tier, prior.mode) == (
                    decision.engine, decision.model, decision.tier, decision.mode):
                return _with(decision, id=prior.id)
            if decision.unit_id:
                self._memo[key] = decision
                if len(self._memo) > 20_000:
                    self._memo.pop(next(iter(self._memo)))
        return decision


def _agent_mode(request: RouteRequest) -> str:
    return str((request.agent_policy or {}).get("selection_mode") or "router").lower()


def _blocked(target: Target, snapshot: Snapshot) -> Optional[str]:
    """Why a pinned session's source can't run now (exhausted, limited, failing), or None."""
    from .selection import _why_not

    return _why_not(target, snapshot.sources.get(target.engine), None)


def _headline(why: list[str], c: Classification) -> str:
    for line in reversed(why):
        if line.startswith(("Escalated", "Floor", "Budget", "Pinned", "Job pin", "Job pref",
                            "The session", "Waiting", "No ")) or any(
                word in line for word in ("moves to", "exhausted", "failing", "limit hit",
                                          "stands in")):
            return line
    return c.reason


def _new_id() -> str:
    return f"route_{uuid.uuid4().hex[:16]}"


def _with(decision: RouteDecision, **changes: Any) -> RouteDecision:
    from dataclasses import replace

    return replace(decision, **changes)


ROUTER = Router()


def headroom_of(snapshot: Snapshot, engine: str) -> Optional[Headroom]:
    state = snapshot.sources.get(engine)
    return state.headroom if state else None
