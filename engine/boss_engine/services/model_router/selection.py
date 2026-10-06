"""(c) Source selection — which source and model takes a tier NOW.

Walks the tier's ordered targets and takes the first one that is connected, allowed, working,
not exhausted and not displaced by a headroom rule (docs/product/MODEL_ROUTER.md, "(c)"). Pure:
the facts arrive as `SourceState`s built once per snapshot.

Two promises:
  * Frontier work is never displaced by a headroom rule — only by exhaustion (Decision 6).
  * Nothing is stranded: a tier with no usable target falls to the same tier on any other
    connected source, then to the least-bad target, so a failure names a real source.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Mapping, Optional

from .headroom import EXHAUSTED, Headroom, UNKNOWN
from .policy import LOCAL_ENGINES, RoutingPolicy, Target, tier_model
from .tiers import FRONTIER, TIER_LABELS

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
    moved_by_headroom: bool = False
    fallback: bool = False

    @property
    def found(self) -> bool:
        return self.target is not None


def _why_not(target: Target, state: Optional[SourceState], allowed: Optional[set[str]]
             ) -> Optional[str]:
    name = engine_label(target.engine)
    if state is None or not state.connected:
        return f"{name} not connected"
    if allowed is not None and target.engine not in allowed:
        return f"{name} not allowed here"
    if not state.working:
        return f"{name} failing"
    if not state.available or state.headroom.state == EXHAUSTED:
        return f"{name} exhausted" + (
            f" (resets {state.headroom.resets_at:%a %H:%M} UTC)" if state.headroom.resets_at
            else "")
    if state.headroom.model_exhausted(target.model):
        return f"{name} · {target.model} limit hit"
    return None


def _model_on(policy: RoutingPolicy, engine: str, tier: str) -> Optional[str]:
    for target in policy.targets(tier):
        if target.engine == engine:
            return target.model
    return tier_model(engine, tier)


def select(tier: str, policy: RoutingPolicy, sources: Mapping[str, SourceState], *,
           allowed: Optional[set[str]] = None, engine_pin: Optional[str] = None) -> Selection:
    """The source for `tier`. `engine_pin` (a job pinned to a source without a model) keeps the
    tier's model on that source; `allowed` is the agent's/policy's allow-list."""
    if policy.allowed_sources:
        allowed = set(policy.allowed_sources) & allowed if allowed is not None \
            else set(policy.allowed_sources)
    if engine_pin:
        queue = [Target(engine_pin, _model_on(policy, engine_pin, tier))]
    else:
        queue = list(policy.targets(tier))
    selection = Selection(None, None, tier)
    seen: set[tuple[str, Optional[str]]] = set()
    i = 0
    while i < len(queue):
        target = queue[i]
        i += 1
        if (target.engine, target.model) in seen:
            continue
        seen.add((target.engine, target.model))
        state = sources.get(target.engine)
        problem = _why_not(target, state, allowed)
        if problem:
            selection.notes.append(problem)
            continue
        assert state is not None
        rule = next((r for r in policy.headroom
                     if r.source == target.engine and tier in r.tiers
                     and r.fires(state.headroom.state)), None)
        if rule is not None and tier != FRONTIER and not engine_pin:
            dest = Target(rule.to, _model_on(policy, rule.to, tier))
            blocked = _why_not(dest, sources.get(rule.to), allowed)
            if blocked is None:
                selection.notes.append(
                    f"{engine_label(target.engine)} {state.headroom.describe()} → "
                    f"{TIER_LABELS[tier]} work moves to {engine_label(rule.to)}")
                selection.moved_by_headroom = True
                queue.insert(i, dest)
                continue
            selection.notes.append(
                f"{engine_label(target.engine)} {state.headroom.describe()}, but {blocked}; "
                f"stayed")
        selection.target, selection.source = target, state
        return selection
    if engine_pin:
        # A pinned source that cannot run says so by failing on that source, not elsewhere.
        state = sources.get(engine_pin)
        selection.target, selection.source = queue[0], state
        selection.fallback = True
        selection.notes.append(f"pinned to {engine_label(engine_pin)} anyway")
        return selection
    # Not stranded: the same tier on any other usable source this project holds.
    others = sorted((s for s in sources.values()
                     if s.usable and (allowed is None or s.engine in allowed)
                     and (s.engine, _model_on(policy, s.engine, tier)) not in seen),
                    key=lambda s: _KIND_ORDER.get(s.kind, 9))
    for state in others:
        model = _model_on(policy, state.engine, tier)
        if state.headroom.model_exhausted(model):
            continue
        selection.target, selection.source = Target(state.engine, model), state
        selection.fallback = True
        selection.notes.append(f"no {TIER_LABELS[tier]} target free; "
                               f"{engine_label(state.engine)} stands in")
        return selection
    # The least-bad: the first connected target, so the failure that follows names it.
    for target in policy.targets(tier):
        state = sources.get(target.engine)
        if state is not None and state.connected and (allowed is None or target.engine in allowed):
            selection.target, selection.source = target, state
            selection.fallback = True
            selection.notes.append(f"every {TIER_LABELS[tier]} source is unavailable; "
                                   f"trying {engine_label(target.engine)}")
            return selection
    if allowed is not None and allowed <= set(LOCAL_ENGINES):
        selection.notes.append("no local model connected")
    return selection
