"""Build stages through the router — what the governed pipeline's `_route` calls.

Fixed mode is the pre-router code, moved here verbatim (`route_for_step` → `apply_budget_pressure`
→ `materialize_route`; the escalated route on the hard-problems lane), so a build on an
untouched install picks exactly what it picked before. Smart mode routes each stage through
`router.route`, with the run's escalation ledger.
"""
from __future__ import annotations

from typing import Any, Callable, Optional

from .escalation import EscalationLedger, seed_from_lineage
from .policy import Target
from .router import LegacyRoute, RouteDecision, RouteRequest
from .tiers import CODING_STEPS


def task_pin(spec: dict, step: str) -> Optional[Target]:
    """An explicit engine/model on the task's `model_config` (a step pin, else the default).
    Class-only values ("strong") are profile vocabulary, not pins."""
    from ..pipeline_profiles import _parse_route

    mc = spec.get("model_config") if isinstance(spec.get("model_config"), dict) else {}
    value = (mc.get("steps") or {}).get(step)
    if value is None:
        value = mc.get("default")
    if value is None:
        return None
    parsed = _parse_route(value)
    if parsed.get("engine"):
        return Target(parsed["engine"], parsed.get("model"))
    return None


class StageRouter:
    """One per governed run. Holds the run's ledger; every stage boundary asks `route`."""

    def __init__(self, *, transaction: Callable[[], Any], settings: Any, project_id: str,
                 run_id: str, spec: dict, title: str, profile: dict, build_engine: str,
                 review_engine: str, budget_state: str, fctx: Optional[dict] = None,
                 risk_tier: Optional[int] = None, repo_files: Optional[int] = None):
        self._tx = transaction
        self.settings = settings
        self.project_id, self.run_id = project_id, run_id
        self.spec, self.title, self.profile = spec, title, profile
        self.build_engine, self.review_engine = build_engine, review_engine
        self.budget_state = budget_state
        self.risk_tier = risk_tier
        self.repo_files = repo_files
        self.ledger = EscalationLedger(seed_from_lineage(fctx))
        self.mode_override = str(spec.get("routing_mode") or "").strip().lower() or None
        self.decisions: dict[str, RouteDecision] = {}

    # ── the legacy answer (Fixed mode) ─────────────────────────────────────────────────────
    def legacy(self, step: str, *, escalated: bool = False, budget: bool = True,
               previous: Optional[dict] = None) -> LegacyRoute:
        from ..pipeline_profiles import apply_budget_pressure, materialize_route, route_for_step

        s = self.settings
        if escalated:
            prior = previous or {}
            engine = (s.code_agent_strong_engine or prior.get("engine") or self.build_engine)
            model = (s.code_agent_strong_model or prior.get("model"))
            return LegacyRoute(engine, model, "escalated to the hard-problems lane", "strong",
                               {"escalated": True})
        r = route_for_step(self.profile, step, self.spec.get("model_config"))
        lite = False
        if budget:
            r, lite = apply_budget_pressure(r, step, self.budget_state)
        engine, model = materialize_route(
            r, build_engine=self.build_engine, review_engine=self.review_engine,
            strong_engine=s.code_agent_strong_engine,
            low_cost_engine=s.code_agent_low_cost_engine,
            strong_model=s.code_agent_strong_model,
            low_cost_model=s.code_agent_low_cost_model,
            build_model=getattr(s, "code_agent_model", "") or "",
            review_model=getattr(s, "code_review_model", "") or "")
        cls = r.get("model_class")
        pinned = r.get("engine") or r.get("model")
        why = (f"the task pins {step}" if pinned else
               f"the {self.profile.get('name') or 'default'} profile puts {step} on the "
               f"{'strong' if cls == 'strong' else 'low-cost'} lane")
        return LegacyRoute(engine, model, why, cls, {"budget_lite": True} if lite else {})

    # ── the boundary ───────────────────────────────────────────────────────────────────────
    def route(self, step: str, *, signal: Optional[str] = None, budget: bool = True,
              previous: Optional[dict] = None) -> RouteDecision:
        """The route for `step` at this boundary. `signal` is the evidence that brought the
        pipeline here (no_output, final_attempt…): recorded first, so Smart mode escalates on
        it; in Fixed mode the legacy escalation applies, as before."""
        from . import store

        if signal:
            self.ledger.record(step, signal)
        escalated_legacy = signal in ("no_output", "final_attempt")
        request = RouteRequest(
            kind="build_stage", step=step, project_id=self.project_id, unit_id=self.run_id,
            role=self.spec.get("role_key") or None,
            text=self.title, spec={**self.spec, "title": self.title},
            task_kind=self.spec.get("task_kind"), repo_files=self.repo_files,
            risk_tier=self.spec.get("risk_tier") if isinstance(self.spec.get("risk_tier"), int)
            else None,
            task_pin=task_pin(self.spec, step),
            legacy=lambda: self.legacy(step, escalated=escalated_legacy, budget=budget,
                                       previous=previous),
            mode_override=self.mode_override,
            aliases=tuple(CODING_STEPS - {step}) if step in CODING_STEPS else (),
            extra_floors=self._profile_floors(),
            preferred=self._profile_preferences().get(step))
        with self._tx() as connection:
            decision = store.route(connection, self.settings, request, ledger=self.ledger)
        self.decisions[step] = decision
        return decision

    def _profile_floors(self) -> tuple:
        """The run's own profile, as floors (Decision 4) — a task that asked for `balanced`
        keeps its adversarial review on Frontier even on a project whose preset is `lean`."""
        if not hasattr(self, "_floors"):
            from .policy import floors_from_profile

            try:
                self._floors = floors_from_profile(self.profile or {})
            except Exception:  # noqa: BLE001
                self._floors = ()
        return self._floors

    def _profile_preferences(self) -> dict:
        """The profile's explicit step pins (`implement: claude_code:sonnet`): preferences
        within the tier on Smart — the floor still raises them (Decision 4)."""
        if not hasattr(self, "_prefs"):
            from .policy import profile_preferences

            try:
                self._prefs = profile_preferences(self.profile or {})
            except Exception:  # noqa: BLE001
                self._prefs = {}
        return self._prefs

    def signal(self, step: str, signal: str, n: int = 1) -> int:
        """Evidence seen during a stage (tests failed, reviewers asked for changes…). It moves
        the tier at the NEXT boundary, never mid-session."""
        return self.ledger.record(step, signal, n)

    def is_smart(self) -> bool:
        if self.mode_override:
            return self.mode_override == "smart"
        from . import store

        with self._tx() as connection:
            return store.mode_of(connection, self.settings, self.project_id) == "smart"

    def evidence(self) -> dict[str, Any]:
        """What the run's evidence keeps: every stage's route, compactly, plus the ledger."""
        return {
            "mode": next((d.mode for d in self.decisions.values()), None),
            "stages": {step: {"route_id": d.id, "tier": d.tier, "engine": d.engine,
                              "model": d.model, "reason": d.reason,
                              **({"escalation": d.escalation} if d.escalation else {})}
                       for step, d in self.decisions.items()},
            "signals": self.ledger.snapshot(),
        }
