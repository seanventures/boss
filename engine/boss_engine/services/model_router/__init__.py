"""BOSS's model router — which model and source runs each piece of work.

See docs/product/MODEL_ROUTER.md. The concerns live in separate modules:

  tiers        Fast / Balanced / Frontier and the step vocabulary
  classifier   (a) task → tier: cache ▸ Jev ▸ rules ▸ cheap model
  policy       (b) floors, presets, headroom/budget/escalation rules, seeding from legacy
  headroom     subscription windows → plenty / tight / near limit / exhausted
  selection    (c) tier → the source and model that can take it now
  escalation   (d) signals → a higher tier at the next boundary
  router       the facade: RouteRequest + Snapshot → RouteDecision
  store        snapshots, the policy row, route records, headroom observations (the DB side)
"""
from .classifier import CLASSIFIER, Classification, TaskSignal, rules_classify
from .escalation import EscalationLedger
from .policy import RoutingPolicy, Target
from .router import ROUTER, LegacyRoute, RouteDecision, RouteRequest, Router, Snapshot
from .selection import SourceState
from .tiers import BALANCED, FAST, FRONTIER, TIERS

__all__ = [
    "BALANCED", "CLASSIFIER", "Classification", "EscalationLedger", "FAST", "FRONTIER",
    "LegacyRoute", "ROUTER", "RouteDecision", "RouteRequest", "Router", "RoutingPolicy",
    "Snapshot", "SourceState", "TIERS", "Target", "TaskSignal", "rules_classify",
]
