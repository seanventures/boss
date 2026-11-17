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


def pinned_engine(spec: dict) -> str:
    """The engine the task's `model_config.default` pins the run to, or "".

    Per-run model_config default (official POST engine=… or spend-cap failover) wins over the
    process default so route_for_step / _route_escalated stay on the pinned engine instead of
    silently falling back to claude_code."""
    mc = spec.get("model_config") if isinstance(spec.get("model_config"), dict) else {}
    default = mc.get("default")
    _pinned_engine = ""
    if isinstance(default, dict):
        _pinned_engine = str(default.get("engine") or "").strip().lower()
    elif isinstance(default, str) and default.strip():
        # Split once. `ollama:llama3.1:8b` pins ollama; `llama3.1:8b` is a model
        # tag and pins nothing (BOSSAPP-1023). An unknown prefix is not a pin.
        from ..harness_registry import is_registered_harness, split_engine_model

        text = default.strip()
        if ":" in text:
            _pinned_engine = split_engine_model(text)[0] or ""
        else:
            _pinned_engine = text.lower() if is_registered_harness(text) else ""
    return _pinned_engine


def lane_engines(connection: Any, settings: Any, project_id: str, spec: dict
                 ) -> tuple[Any, str, str]:
    """(settings with the project's choices applied, build engine, review engine): the lane
    defaults every build stage routes from. The governed pipeline asks this, and so does the
    pre-check (`sandbox.engine`), so "which engine will this build start" has one answer.

    Lane defaults: builder engine for design/tests/implement, adversary engine for
    review/security/consistency/mutation — the model that wrote the code isn't the one judging
    it (removes self-review bias). VAG B2 layers per-STEP (engine, model) routing on top
    (route_for_step)."""
    from ..project_settings import applied as _applied_settings
    from ..setup_service import build_engine_for as _build_engine_for

    _pinned_engine = pinned_engine(spec)
    # The project's choices, layered over the deployment's, ONCE — every `settings.*_engine`
    # read below and in the functions this calls then sees them. Credentials for several
    # providers can be connected at once, so which model fills which role is a decision
    # somebody makes in the console, not one frozen into the container at deploy time.
    settings = _applied_settings(connection, project_id, settings)
    # AND A DEFAULT THAT MATCHES THIS INSTALLATION. The chain used to end at the literal
    # "claude_code", so an install that had connected only an OpenRouter key shelled out to a
    # `claude` binary it does not have. This only speaks when the configured engine cannot run
    # here — see build_engine_for.
    _resolved_engine = _build_engine_for(connection, settings, project_id)
    build_engine = (
        _pinned_engine or _resolved_engine or settings.code_agent_engine or "claude_code"
    ).lower()
    review_engine = (settings.code_review_engine or "").lower()
    if not review_engine:
        # NO REVIEWER PINNED: a different model FAMILY by default (BOSSAPP-960). The adversary
        # lane's whole point is that the model that wrote the code is not the one judging it,
        # but "empty = same as the builder" made self-review the default on every install. When
        # this project holds a credential for an engine in another family, review runs there;
        # with one engine connected (the common install) nothing changes.
        from ..independence import independent_reviewer

        review_engine = independent_reviewer(
            connection, settings, project_id, build_engine,
            getattr(settings, "code_agent_model", None)) or build_engine
    return settings, build_engine, review_engine


#: The Setup jobs that pin a build stage, with their words.
_PIN_JOBS = {"build": "Build", "review": "Review", "hard": "Hard problems"}


def setup_job_pins(connection: Any, project_id: Optional[str]) -> dict[str, tuple[str, Optional[str]]]:
    """job -> (engine, model) for each job the operator PINNED in Setup › Who does what
    (setup_actions.set_job writes the engine knob, org-wide or per project). A job left on Auto
    has no engine knob set: "pinned" is read from the knob, never from the lane's engine
    default (which is non-empty on an install that pinned nothing)."""
    if connection is None or not project_id:
        return {}
    try:
        from ..model_routing import JOB_KNOBS
        from ..project_settings import get_all

        with connection.begin_nested():
            chosen = get_all(connection, project_id)
    except Exception:  # noqa: BLE001 — an unreadable setting pins nothing
        return {}
    pins: dict[str, tuple[str, Optional[str]]] = {}
    for job in _PIN_JOBS:
        engine_knob, model_knob = JOB_KNOBS[job]
        engine = str(chosen.get(engine_knob) or "").strip().lower()
        if engine:
            model = str(chosen.get(model_knob) or "").strip()
            pins[job] = (engine, model or None)
    return pins


def pin_for_step(pins: dict, step: str, *, kind: str = "build_stage"
                 ) -> Optional[tuple[str, Optional[str], str]]:
    """(engine, model, words) when the operator pinned the job `step` belongs to: Review
    covers the judging seats, Build everything else that builds."""
    from .tiers import job_for

    job = job_for(kind, step)
    pin = pins.get(job)
    if not pin:
        return None
    engine, model = pin
    return engine, model, f"{_PIN_JOBS[job]} → {engine}" + (f" · {model}" if model else "")


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
        self._pins: Optional[dict] = None

    def _setup_pins(self, connection: Any) -> dict:
        if self._pins is None:
            self._pins = setup_job_pins(connection, self.project_id)
        return self._pins

    # ── the legacy answer (Fixed mode) ─────────────────────────────────────────────────────
    def legacy(self, step: str, *, escalated: bool = False, budget: bool = True,
               previous: Optional[dict] = None) -> LegacyRoute:
        from ..pipeline_profiles import apply_budget_pressure, materialize_route, route_for_step

        s = self.settings
        if escalated:
            prior = previous or {}
            hard_pin = self._pins.get("hard") if self._pins else None
            if hard_pin:
                words = f"Hard problems → {hard_pin[0]}" + (f" · {hard_pin[1]}" if hard_pin[1] else "")
                return LegacyRoute(hard_pin[0], hard_pin[1], f"pinned in Setup: {words}", "strong",
                                   {"escalated": True})
            prior_engine = str(prior.get("engine") or "").strip().lower()
            engine = (s.code_agent_strong_engine or prior.get("engine") or self.build_engine)
            chosen = str(engine or "").strip().lower()
            strong_model = str(s.code_agent_strong_model or "").strip() or None
            # The previous stage's model belongs to the previous engine. A move
            # onto the hard-problems lane must not carry it across (BOSSAPP-1025).
            if strong_model:
                model = strong_model
            elif prior_engine and chosen == prior_engine:
                model = prior.get("model") or None
            else:
                model = None
            return LegacyRoute(engine, model, "escalated to the hard-problems lane", "strong",
                               {"escalated": True})
        r = route_for_step(self.profile, step, self.spec.get("model_config"))
        # THE OPERATOR'S JOB PIN beats the profile's lane for every stage the job covers. Only a
        # task's own pin (spend-cap failover, an experiment) is above it.
        job_pin = None if (r.get("engine") or r.get("model")) else \
            pin_for_step(self._pins or {}, step)
        if job_pin:
            from .compat import fits

            engine, model, words = job_pin
            return LegacyRoute(engine, model if fits(engine, model) else None,
                               f"pinned in Setup: {words}", r.get("model_class"), {})
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
        with self._tx() as connection:
            pins = self._setup_pins(connection)
            job_pin = None if task_pin(self.spec, step) else pin_for_step(pins, step)
            hard = None if task_pin(self.spec, step) else pins.get("hard")
        request = RouteRequest(
            kind="build_stage", step=step, project_id=self.project_id, unit_id=self.run_id,
            role=self.spec.get("role_key") or None,
            text=self.title, spec={**self.spec, "title": self.title},
            task_kind=self.spec.get("task_kind"), repo_files=self.repo_files,
            risk_tier=self.spec.get("risk_tier") if isinstance(self.spec.get("risk_tier"), int)
            else None,
            task_pin=task_pin(self.spec, step),
            job_pin=Target(job_pin[0], job_pin[1]) if job_pin else None,
            job_pin_label=job_pin[2] if job_pin else "",
            hard_pin=Target(hard[0], hard[1]) if hard else None,
            hard_pin_label=(f"Hard problems → {hard[0]}" + (f" · {hard[1]}" if hard[1] else "")
                            if hard else ""),
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
