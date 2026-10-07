"""(c) Source selection — which source and model takes a tier NOW.

Walks the tier's ordered targets and takes the first one that is connected, allowed, working,
not exhausted and not displaced by a headroom rule (docs/product/MODEL_ROUTER.md, "(c)"). Pure:
the facts arrive as `SourceState`s built once per snapshot.

Two promises:
  * Frontier work is never displaced by a headroom rule — only by exhaustion (Decision 6).
  * Nothing is stranded: a tier with no usable target falls to the same tier on any other
    connected source; a source that is out only because its breaker is open gets ONE half-open
    trial call (breaker.claim_trial); and a refusal that is only temporary carries
    `retry_after_s`, so a stage waits for the source instead of failing (Decision 21).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Mapping, Optional

from .headroom import EXHAUSTED, Headroom, UNKNOWN
from .policy import LOCAL_ENGINES, RoutingPolicy, Target, tier_model
from .tiers import FRONTIER, TIER_LABELS, rank

_KIND_ORDER = {"subscription": 0, "local": 1, "metered": 2, "env": 3, "": 4}


def engine_label(engine: str) -> str:
    try:
        from ..setup_view import ENGINE_LABELS

        return ENGINE_LABELS.get(engine, engine)
    except Exception:  # noqa: BLE001
        return engine


@dataclass(frozen=True)
class SourceState:
    engine: str
    kind: str = "metered"            # subscription | local | metered | env
    connected: bool = True
    working: bool = True
    available: bool = True
    source_id: Optional[str] = None
    headroom: Headroom = UNKNOWN

    @property
    def usable(self) -> bool:
        return self.connected and self.working and self.available \
            and self.headroom.state != EXHAUSTED


@dataclass
class Selection:
    target: Optional[Target]
    source: Optional[SourceState]
    tier: str
    notes: list[str] = field(default_factory=list)
    #: Moved off its first choice by a headroom rule OR by that choice being exhausted, limited
    #: or failing — what "moved" counts in This week.
    moved_by_headroom: bool = False
    fallback: bool = False
    #: Nothing may take this work; `refusal` says why, in words, and what fixes it.
    refusal: str = ""
    #: The refusal is only temporary (a source's breaker is open): try again in this long.
    retry_after_s: Optional[float] = None
    #: The target is a half-open trial on a source whose breaker is open.
    trial: bool = False

    @property
    def found(self) -> bool:
        return self.target is not None


METERED_KINDS = frozenset({"metered", "env"})


def _why_not(target: Target, state: Optional[SourceState], allowed: Optional[set[str]],
             *, no_metered: bool = False, ignore_breaker: bool = False) -> Optional[str]:
    from . import breaker

    name = engine_label(target.engine)
    if state is None or not state.connected:
        return f"{name} not connected"
    if allowed is not None and target.engine not in allowed:
        return f"{name} not allowed here"
    if no_metered and state.kind in METERED_KINDS:
        return f"{name} is metered and the budget is spent"
    tripped = None if ignore_breaker else breaker.open_reason(target.engine)
    if tripped:
        return f"{name} failing ({tripped[:60]})"
    if not state.working:
        return f"{name} failing"
    if not state.available or state.headroom.state == EXHAUSTED:
        return f"{name} exhausted" + (
            f" (resets {state.headroom.resets_at:%a %H:%M} UTC)" if state.headroom.resets_at
            else "")
    if state.headroom.model_exhausted(target.model):
        return f"{name} · {target.model} limit hit"
    return None


def tier_of(policy: RoutingPolicy, engine: str, model: Optional[str],
            asked: Optional[str] = None) -> Optional[str]:
    """The tier a model RUNS at: the operator's tier list first (the tier asked for when the model
    is listed there, else the first tier that lists it), then the name's class (policy.model_tier).
    None when neither knows it."""
    from .policy import model_tier
    from .tiers import TIERS

    for tier in ((asked,) if asked else ()) + TIERS:
        if any(t.engine == engine and t.model == model for t in policy.targets(tier)):
            return tier
    return model_tier(engine, model)


def _model_on(policy: RoutingPolicy, engine: str, tier: str) -> Optional[str]:
    for target in policy.targets(tier):
        if target.engine == engine:
            return target.model
    return tier_model(engine, tier)


def others_tripped(sources: Mapping[str, SourceState], ok) -> list[SourceState]:
    """Sources that would stand in but for an open breaker (half-open candidates)."""
    from . import breaker

    return sorted((s for s in sources.values()
                   if breaker.open_reason(s.engine) and ok(s, True)),
                  key=lambda s: _KIND_ORDER.get(s.kind, 9))


def select(tier: str, policy: RoutingPolicy, sources: Mapping[str, SourceState], *,
           allowed: Optional[set[str]] = None, engine_pin: Optional[str] = None,
           preferred: Optional[Target] = None, no_metered: bool = False,
           strict: bool = False, local_ok: bool = False) -> Selection:
    """The source for `tier`.

    `engine_pin`: a HARD pin to a source (its tier model is used; it is never moved).
    `preferred`: a SOFT pin (a job's choice, a profile's step pin): tried first, but headroom
    rules and exhaustion still move it. `no_metered`: the budget is spent — nothing that costs
    money. `strict`: the allow-list is a promise (Local means local): never step outside it.
    `local_ok`: the operator chose local for this work (an agent's Local policy), so a local
    model may take ANY tier — the tier's minimum applies within the allowed sources."""
    from .policy import model_tier

    if policy.allowed_sources:
        allowed = set(policy.allowed_sources) & allowed if allowed is not None \
            else set(policy.allowed_sources)
    selection = Selection(None, None, tier)
    if engine_pin:
        queue = [Target(engine_pin, _model_on(policy, engine_pin, tier))]
    else:
        queue = list(policy.targets(tier))
        if preferred is not None and preferred.engine:
            want = tier_of(policy, preferred.engine, preferred.model, tier)
            if preferred.model and want is not None and rank(want) < rank(tier):
                raised = Target(preferred.engine, _model_on(policy, preferred.engine, tier))
                selection.notes.append(f"Floor raised your pin: {engine_label(preferred.engine)}"
                                       f" · {preferred.model} → {raised.model or 'its default'}")
                preferred = raised
            elif preferred.model and want is not None and rank(want) > rank(tier):
                # A PREFERENCE STAYS WITHIN THE STEP'S TIER: a soft Opus preference does not
                # put Fast work on Opus — the preferred source's model for this tier runs it.
                within = Target(preferred.engine, _model_on(policy, preferred.engine, tier))
                selection.notes.append(
                    f"Your preference {engine_label(preferred.engine)} · {preferred.model} is "
                    f"{TIER_LABELS[want]}; this {TIER_LABELS[tier]} work uses "
                    f"{within.model or 'its default'}")
                preferred = within
            elif not preferred.model:
                preferred = Target(preferred.engine, _model_on(policy, preferred.engine, tier))
            queue.insert(0, preferred)
    seen: set[tuple[str, Optional[str]]] = set()
    #: Targets out ONLY because their breaker is open — half-open trial candidates.
    tripped: list[tuple[Target, SourceState]] = []
    first = True
    i = 0
    while i < len(queue):
        target = queue[i]
        i += 1
        if (target.engine, target.model) in seen:
            continue
        seen.add((target.engine, target.model))
        state = sources.get(target.engine)
        problem = _why_not(target, state, allowed, no_metered=no_metered)
        if problem:
            selection.notes.append(problem)
            if state is not None and _why_not(target, state, allowed, no_metered=no_metered,
                                              ignore_breaker=True) is None:
                tripped.append((target, state))
            if first and state is not None and state.connected and (
                    allowed is None or target.engine in allowed):
                selection.moved_by_headroom = True       # moved off a source that can't now
            first = False
            continue
        assert state is not None
        rule = next((r for r in policy.headroom
                     if r.source == target.engine and tier in r.tiers
                     and r.fires(state.headroom.state)), None)
        if rule is not None and tier != FRONTIER and not engine_pin:
            dest = Target(rule.to, _model_on(policy, rule.to, tier))
            blocked = _why_not(dest, sources.get(rule.to), allowed, no_metered=no_metered)
            if blocked is None:
                selection.notes.append(
                    f"{engine_label(target.engine)} {state.headroom.describe()} → "
                    f"{TIER_LABELS[tier]} work moves to {engine_label(rule.to)}")
                selection.moved_by_headroom = True
                queue.insert(i, dest)
                first = False
                continue
            selection.notes.append(
                f"{engine_label(target.engine)} {state.headroom.describe()}, but {blocked}; "
                f"stayed")
        selection.target, selection.source = target, state
        return selection
    if engine_pin:
        state = sources.get(engine_pin)
        if no_metered and state is not None and state.kind in METERED_KINDS:
            # NO NEW METERED SPEND WHILE THE BUDGET IS RED — a pin included (Decision 16).
            selection.refusal = (f"Budget spent: this work is pinned to {engine_label(engine_pin)}"
                                 ", which is metered, and there is no new metered spend while "
                                 "the budget is red. Raise the budget or unpin it.")
            return selection
        # A HARD pin that cannot run says so by failing on that source, not elsewhere.
        selection.target, selection.source = queue[0], state
        selection.fallback = True
        selection.notes.append(f"pinned to {engine_label(engine_pin)} anyway")
        return selection
    # Not stranded: the same tier on any other usable source this project holds — but never a
    # local model for Frontier work (unless the operator allowed it), never metered money while
    # the budget is spent, and never outside a strict allow-list.
    from . import breaker

    def ok(s: SourceState, ignore_breaker: bool = False) -> bool:
        if not s.usable or (not ignore_breaker and breaker.open_reason(s.engine)):
            return False
        if allowed is not None and s.engine not in allowed:
            return False
        if no_metered and s.kind in METERED_KINDS:
            return False
        if tier == FRONTIER and s.kind == "local" and not (policy.frontier_allows_local
                                                            or local_ok):
            return False
        if tier == FRONTIER and s.kind != "local":
            # A stand-in for Frontier work must be a Frontier-CLASS model, known by name — a
            # metered key whose default model is unknown is not one.
            from .policy import model_tier

            if model_tier(s.engine, _model_on(policy, s.engine, tier)) != FRONTIER:
                return False
        return (s.engine, _model_on(policy, s.engine, tier)) not in seen

    others = sorted((s for s in sources.values() if ok(s)),
                    key=lambda s: _KIND_ORDER.get(s.kind, 9))
    for state in others:
        model = _model_on(policy, state.engine, tier)
        if state.headroom.model_exhausted(model):
            continue
        selection.target, selection.source = Target(state.engine, model), state
        selection.fallback = selection.moved_by_headroom = True
        selection.notes.append(
            f"{TIER_LABELS[tier]} work stays local by this agent's policy: "
            f"{engine_label(state.engine)} takes it"
            if local_ok and state.kind == "local" and tier == FRONTIER else
            f"no {TIER_LABELS[tier]} target free; {engine_label(state.engine)} stands in")
        return selection
    # HALF-OPEN: a source out only because its breaker is open gets one trial call before
    # anything is refused — a recovered source is found by trying it, not by a timer.
    for state in others_tripped(sources, ok):
        model = _model_on(policy, state.engine, tier)
        tripped.append((Target(state.engine, model), state))
    for target, state in tripped:
        if breaker.claim_trial(target.engine):
            selection.target, selection.source = target, state
            selection.fallback = selection.trial = True
            selection.notes.append(f"{engine_label(target.engine)} was failing; one trial call "
                                   "to see whether it has recovered")
            return selection
    if tripped:
        wait = min(breaker.seconds_until_trial(t.engine) for t, _ in tripped)
        selection.retry_after_s = max(1.0, wait)
    if strict and allowed is not None and allowed <= set(LOCAL_ENGINES):
        held = sorted(e for e, s in sources.items() if e in allowed and s.connected)
        selection.refusal = (
            "No local model connected — connect one or change this agent's policy."
            if not held else
            f"The local model ({', '.join(engine_label(e) for e in held)}) can't take this "
            "now (" + "; ".join(
                _why_not(Target(e, _model_on(policy, e, tier)), sources[e], allowed)
                or f"{engine_label(e)} has no model for {TIER_LABELS[tier]} work"
                for e in held) + ").")
    elif strict and allowed is not None:
        selection.refusal = (f"None of the allowed sources ({', '.join(sorted(allowed))}) can "
                             "take this now.")
    elif no_metered:
        selection.refusal = ("Waiting for budget or limit reset: the budget is spent and no "
                             "included source can take this now.")
    elif tier == FRONTIER:
        selection.refusal = ("No Frontier model can take this now (" + "; ".join(
            selection.notes[-3:]) + "). Connect one, wait for the reset, or allow local "
            "models for Frontier.")
    else:
        selection.refusal = (f"No {TIER_LABELS[tier]} source can take this now ("
                             + "; ".join(selection.notes[-3:]) + ").")
    return selection
