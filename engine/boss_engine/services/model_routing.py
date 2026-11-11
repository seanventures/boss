"""The ONE way a skill, an executor or an agent step gets a model.

WHY THIS EXISTS
---------------
"Video edit and publish" died at its first step with

    IntegrationConfigurationError: OPENAI_API_KEY is not configured

on an install whose readiness check had just said "a model is connected" — two subscriptions,
Claude Code and Codex, and no OpenAI key, deliberately. The agent step ran the agent loop, and
the agent loop built its model with `make_chat_adapter(settings)`: the DEPLOYMENT's settings,
whose `llm_engine` defaults to "auto", which means the OpenAI adapter. The project's connected
sources were never consulted.

That was the fifth copy of the same defect. `meeting.extract_actions`, `model.generate_text`
and `video.plan_edit` had each been fixed by pasting the same six lines (`chat_engine_for` →
`model_copy` → `make_agent_chat_adapter`) into their own bodies, and `model.generate_code` and
the agent loop never got the paste. Each fix was right; the design — "every caller resolves the
project's model for itself" — guaranteed the next caller would forget.

So the resolution lives here, once:

  * `choose(connection, settings, project_id)` decides WHICH source answers — the console's
    choice, the deployment's environment, else a connected subscription or stored key with
    capacity left. Readiness asks the same function whether a model is connected, so "a model
    is connected" and "the step can get a model" cannot disagree again.

  * `project_chat_adapter(connection, settings, project_id)` builds THAT source's adapter on its
    own stored credential and endpoint (Claude Code, Codex, an OpenAI key, Azure, OpenRouter,
    Gemini, xAI, Groq, DeepSeek, vLLM, Ollama). It is the only place in `boss_engine` outside the
    adapter factories that builds a chat adapter; `test_every_model_call_goes_through_the_router`
    scans the whole package for any other.

  * `governed(...)` wraps an adapter so every call is reserved against the budget, receipted and
    settled — what the agent loop's turns run through.

  * A skill or tool that needs something no chat model can do — image generation,
    speech-to-text — DECLARES it (`ProviderRequirement`, `register_executor(..., requires=...)`,
    `declare("tool:<name>", ...)`). Readiness turns the declaration into a need with its fix
    before the run starts, and the key is read only through `provider_credential`, which
    resolves a stored key as well as the environment's and raises `ProviderRequirementMissing`
    carrying the same fix.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional

from .github_adapter import IntegrationConfigurationError


# ── The project's chat model ───────────────────────────────────────────────────────────────────
#
# ONE DECISION, TWO READERS. `choose` decides which connected source answers; readiness asks it
# whether anything can ("a model is connected"), and `project_chat_adapter` builds THAT source's
# adapter, with its own stored credential and endpoint. They used to be two answers: readiness
# counted any entitlement row holding a key, while the adapter factory knew three engine names
# and built a bare `OpenAIAdapter(settings)` for everything else — so a project whose only model
# was a stored OpenAI, Gemini or OpenRouter key was "connected" and every step died with
# "OPENAI_API_KEY is not configured".

#: Engines a chat adapter can be built for, in the order an unchosen project prefers them.
#: Subscriptions first (flat cost), then the OpenAI-compatible lanes. Every engine
#: `setup_service.connected_engines` can return for a stored key is here, so a connected key is
#: never "connected" for readiness and unreachable for the adapter.
CHAT_ROUTABLE: tuple[str, ...] = (
    "claude_code", "codex", "native", "azure",
    "openrouter", "gemini", "xai", "groq", "deepseek", "vllm", "ollama",
    # Last: on an Anthropic API key, chosen by the operator (agent_sdk_engine).
    "claude_agent_sdk",
)

_ALIASES = {"claude_cli": "claude_code", "claude": "claude_code", "anthropic": "claude_code",
            "codex_cli": "codex", "openai": "native"}

#: `ModelChoice.why` when the deployment's own environment decided — the case every install
#: configured before project routing existed is in, and which must keep behaving as it did.
ENVIRONMENT = "this install's environment"


def _engine_name(value: Any) -> str:
    key = str(value or "").strip().lower()
    return _ALIASES.get(key, key)


@dataclass(frozen=True)
class ModelChoice:
    """Which connected source answers this project's model calls, and why."""

    #: A name from CHAT_ROUTABLE, or "" when nothing this project holds can answer.
    engine: str
    why: str
    source_id: Optional[str] = None

    @property
    def runnable(self) -> bool:
        return bool(self.engine)

    def describe(self) -> str:
        if not self.engine:
            return "no connected model"
        return f"{self.engine} ({self.why})"


def choose(connection: Any, settings: Any, project_id: Optional[str]) -> ModelChoice:
    """The source this project's chat, agent and meeting calls go to — `resolve_job(..., "chat")`.

    Reads; never decrypts, never probes. The precedence is `resolve_job`'s, written down once.
    """
    picked = resolve_job(connection, settings, project_id, "chat")
    if not picked.engine:
        return ModelChoice("", "nothing this project holds can answer")
    return ModelChoice(picked.engine, picked.why, picked.source_id)


# ── Who does what: one resolver for every job ─────────────────────────────────────────────────
#
# TWO RESOLVERS USED TO ANSWER "WHICH MODEL". `choose` (chat) preferred the deployment's
# environment; `setup_service.build_engine_for` (builds) preferred the project's choice and then
# a verified harness. So "which model writes my code, and does it cost money?" had two answers,
# and a stale OPENAI_API_KEY in .env outranked the subscription the operator had just connected
# in the console — Claude Code's own "stale key overrides subscription" trap. Now every job
# resolves here, the console's job pickers show this answer as `Auto · <source>`, and the
# precedence is:
#
#   1. the project's choice (a job picked in Repo and models), or the deployment's deliberate
#      choice of engine, when that engine is connected;
#   2. a working subscription with capacity;
#   3. a working local model;
#   4. a working metered key;
#   5. a key in the install's environment (an install configured the old way);
#   6. nothing.

#: job -> (engine knob, model knob). Chat's model knob depends on its engine (`chat_model_knob`).
JOB_KNOBS: dict[str, tuple[str, Optional[str]]] = {
    "build": ("code_agent_engine", "code_agent_model"),
    "review": ("code_review_engine", "code_review_model"),
    "chat": ("chat_llm_engine", None),
    "decisions": ("decision_backend", "decision_model"),
    "hard": ("code_agent_strong_engine", "code_agent_strong_model"),
    "routine": ("code_agent_low_cost_engine", "code_agent_low_cost_model"),
}

#: Jobs that, left on Auto, are done by whatever does Build — the runtime's own rule: an empty
#: reviewer, hard-problems or routine lane means the implementer does it.
FOLLOWS_BUILD = frozenset({"review", "hard", "routine"})

#: Why a job resolved as it did.
CHOSEN = "chosen for this project"
AUTO = "auto"

#: Tier order for Auto.
_TIERS = ("subscription", "local", "metered")


def chat_model_knob(engine: str) -> str:
    """Which knob holds the chat job's model for `engine`."""
    return {"claude_code": "claude_cli_model", "codex": "codex_cli_model"}.get(
        _engine_name(engine), "chat_model")


@dataclass(frozen=True)
class JobResolution:
    """Which source does a job, which model, and why."""

    engine: str
    model: Optional[str]
    why: str
    source_id: Optional[str] = None
    #: subscription | local | metered | env | typesafe | "" — what the job costs to run.
    kind: str = ""

    @property
    def metered(self) -> bool:
        return self.kind in ("metered", "env", "typesafe")


def _chosen(connection: Any, project_id: Optional[str], key: str) -> Any:
    from . import project_settings

    if not project_id or connection is None:
        return None
    try:
        return project_settings.get_all(connection, project_id).get(key)
    except Exception:  # noqa: BLE001 — an unreachable settings table is not a choice
        return None


def _env_kind(engine: str, found: dict[str, Any]) -> str:
    if engine in ("ollama", "vllm"):
        return "local"
    if found.get("env_attr") == "claude_code_oauth_token":
        return "subscription"
    return "env"


def _auto(connected: dict[str, dict[str, Any]], routable: Optional[tuple[str, ...]]
          ) -> Optional[tuple[str, dict[str, Any]]]:
    """Auto's pick from what this project has connected, by tier."""
    order = list(CHAT_ROUTABLE) + sorted(e for e in connected if e not in CHAT_ROUTABLE)
    names = [e for e in order if e in connected and (routable is None or e in routable)]
    if not names:
        return None
    # A PROVEN HARNESS BEFORE A DECLARED ONE. `verified` means a governed task has completed
    # on it; choosing an unproven adapter by default would present a hypothesis as a decision.
    try:
        from .harness_registry import available_harnesses

        unproven = {h["name"] for h in available_harnesses() if not h.get("verified")}
    except Exception:  # noqa: BLE001
        unproven = set()
    names = [e for e in names if e not in unproven] + [e for e in names if e in unproven]
    for group in ([e for e in names if e not in unproven], [e for e in names if e in unproven]):
        for tier in _TIERS:
            for engine in group:
                found = connected[engine]
                kind = found.get("kind") or "metered"
                if kind != tier or found.get("working") is False:
                    continue
                if not (found.get("capacity") or {}).get("available", True):
                    continue
                return engine, found
    for engine in names:                                   # 5. the environment
        found = connected[engine]
        if (found.get("kind") or "") == "env":
            return engine, found
    # Everything connected is at capacity or failing: the least-bad, so the failure the job
    # meets names the source rather than "no model".
    for engine in names:
        found = connected[engine]
        if found.get("working") is not False:
            return engine, found
    return names[0], connected[names[0]]


def resolve_job(connection: Any, settings: Any, project_id: Optional[str], job: str
                ) -> JobResolution:
    """Which source does `job` on this project (see the precedence above). Reads only."""
    if job not in JOB_KNOBS:
        raise ValueError(f"unknown job {job!r}; one of: {', '.join(JOB_KNOBS)}")
    if job == "decisions":
        return _resolve_decisions(connection, settings, project_id)
    if job in FOLLOWS_BUILD and not _chosen(connection, project_id, JOB_KNOBS[job][0]):
        built = resolve_job(connection, settings, project_id, "build")
        return JobResolution(built.engine, built.model, built.why if built.why != CHOSEN
                             else "same as Build", built.source_id, built.kind)
    try:
        from . import setup_service

        connected = setup_service.connected_engines(connection, settings, project_id or "")
    except Exception:  # noqa: BLE001 — a lookup failure must not fail the work it serves
        # The database could not say; the install's own settings still can.
        from . import setup_service

        connected = setup_service.environment_engines(settings)
    engine_knob, model_knob = JOB_KNOBS[job]
    routable = CHAT_ROUTABLE if job == "chat" else None

    def kind_of(engine: str) -> str:
        found = connected.get(engine) or {}
        kind = found.get("kind") or ""
        return _env_kind(engine, found) if kind == "env" else kind

    def model_for(engine: str, knob: Optional[str]) -> Optional[str]:
        if knob is None:
            knob = chat_model_knob(engine)
        chosen = _chosen(connection, project_id, knob)
        return str(chosen) if chosen else None

    # 1. The project's choice — or the deployment's own deliberate choice of engine.
    # A choice is honoured while its engine is connected (or nothing is): a job pointed at a
    # source this install holds no credential for would fail inside the run, so Auto stands in.
    picked = _engine_name(_chosen(connection, project_id, engine_knob))
    if picked and (picked in connected or not connected) \
            and (routable is None or picked in routable):
        return JobResolution(picked, model_for(picked, model_knob), CHOSEN,
                             (connected.get(picked) or {}).get("source_id"), kind_of(picked))
    deployment = _engine_name(getattr(settings, JOB_KNOBS[job][0], "") or "")
    from .setup_service import _set_deliberately

    if job == "chat":
        if deployment in CHAT_ROUTABLE and _set_deliberately(settings, "chat_llm_engine"):
            return JobResolution(deployment, model_for(deployment, None), CHOSEN,
                                 (connected.get(deployment) or {}).get("source_id"),
                                 kind_of(deployment))
        legacy = _engine_name(getattr(settings, "llm_engine", ""))
        if legacy in ("claude_code", "codex"):
            return JobResolution(legacy, model_for(legacy, None), ENVIRONMENT,
                                 (connected.get(legacy) or {}).get("source_id"), kind_of(legacy))
    elif deployment and deployment in connected and _set_deliberately(settings, engine_knob):
        return JobResolution(deployment, model_for(deployment, model_knob), ENVIRONMENT,
                             (connected.get(deployment) or {}).get("source_id"),
                             kind_of(deployment))
    # 2-5. Auto.
    found = _auto(connected, routable)
    if found is None:
        if job == "chat" and all(str(getattr(settings, a, "") or "").strip() for a in (
                "azure_openai_key", "azure_openai_endpoint", "azure_openai_deployment")):
            return JobResolution("azure", None, ENVIRONMENT, None, "env")
        return JobResolution("", None, "nothing this project holds can answer")
    engine, detail = found
    why = ENVIRONMENT if (detail.get("kind") == "env") else str(detail.get("why") or "connected")
    return JobResolution(engine, model_for(engine, model_knob), why, detail.get("source_id"),
                         kind_of(engine))


def _resolve_decisions(connection: Any, settings: Any, project_id: Optional[str]
                       ) -> JobResolution:
    """Decisions run on TypeSafe Jev (with a key) or the Claude Code subscription."""
    from . import project_settings

    chosen = str(_chosen(connection, project_id, "decision_backend") or "").strip().lower()
    model = _chosen(connection, project_id, "decision_model")
    try:
        from .typed_decisions import typesafe_decides

        effective = project_settings.applied(connection, project_id, settings)
        jev = bool(typesafe_decides(effective))
    except Exception:  # noqa: BLE001
        jev = False
    why = CHOSEN if chosen else AUTO
    if jev and chosen != "claude":
        return JobResolution("typesafe", None, why, "typesafe_jev", "typesafe")
    try:
        from . import setup_service

        claude = setup_service.connected_engines(connection, settings, project_id or "") \
            .get("claude_code")
    except Exception:  # noqa: BLE001
        claude = None
    if not claude:
        return JobResolution("", None, "nothing this project holds can decide")
    kind = claude.get("kind") or "subscription"
    # An install token (CLAUDE_CODE_OAUTH_TOKEN) is the subscription, not a metered key: the
    # same reading `resolve_job` gives every other job (`_env_kind`).
    kind = _env_kind("claude_code", claude) if kind == "env" else kind
    return JobResolution("claude_code", str(model) if model else None, why,
                         claude.get("source_id"), "metered" if kind == "env" else kind)


def routed_settings(connection: Any, settings: Any, project_id: Optional[str]) -> Any:
    """`settings` with this project's console choices layered on (`project_settings.applied`).

    Never raises: a settings table that cannot be read leaves the deployment's own answer.
    """
    if connection is None or not project_id:
        return settings
    try:
        from . import project_settings

        return project_settings.applied(connection, project_id, settings)
    except Exception:  # noqa: BLE001 — the deployment's settings are a complete answer
        return settings


def _bound(settings: Any, engine: str, connection: Any, project_id: Optional[str]) -> Any:
    """The lane's stored credential, model and endpoint bound onto settings — the same binding a
    build on that lane gets (`code_agent._bind_lane_credentials`): SecretRef first, the
    environment last, and a bound ref that cannot be read fails closed."""
    if connection is None or not project_id or not hasattr(settings, "model_copy"):
        return settings
    from .code_agent import _bind_lane_credentials

    lane = "native" if engine in ("native", "openai") else engine
    return _bind_lane_credentials(settings, lane, connection=connection, project_id=project_id)


def adapter_for(choice: ModelChoice, connection: Any, settings: Any,
                project_id: Optional[str], *, model: Optional[str] = None):
    """The chat adapter for `choice`, on that source's own credential and endpoint.

    No paid fallback is wrapped around a subscription: an agent turn that silently moved from
    a flat-rate CLI to a metered key would spend money nobody chose to spend, and the CLI's own
    error ("not logged in", "usage limit") names its fix.
    """
    from .claude_cli_adapter import ClaudeCliAdapter
    from .openai_adapter import OpenAIAdapter

    engine = choice.engine
    if not engine:
        # Nothing connected. The OpenAI adapter's own "OPENAI_API_KEY is not configured" is
        # what reaches the step, and model_failures explains it as "no connected model".
        return OpenAIAdapter(settings)
    bound = _bound(settings, engine, connection, project_id)
    # The router's model wins (a Smart route names one); otherwise the chat model knob.
    model = model or (getattr(settings, "chat_model", "") or "").strip() or None
    if engine == "claude_code":
        return ClaudeCliAdapter(bound, model=model)
    if engine == "claude_agent_sdk":
        from .agent_sdk_engine import make_chat_adapter

        return make_chat_adapter(bound, model=model)
    if engine == "codex":
        from .codex_cli_adapter import CodexCliAdapter

        return CodexCliAdapter(bound, model=model)
    from .code_agent import _PROVIDER_LANES

    if engine in _PROVIDER_LANES:
        key_field, url_field = _PROVIDER_LANES[engine]
        lane = bound.model_copy(update={
            "openai_api_key": getattr(bound, key_field, None) or None,
            "openai_base_url": getattr(bound, url_field, "") or "",
            # A lane on its own credential pair must not be redirected to Azure.
            "azure_openai_key": None, "azure_openai_endpoint": None,
            "azure_openai_deployment": None,
        })
        if model and engine in ("openrouter", "groq", "deepseek", "gemini", "xai", "vllm",
                                "ollama"):
            lane = lane.model_copy(update={"openai_model": model})
        return OpenAIAdapter(lane, provider=engine)
    if model and engine == "native" and hasattr(bound, "model_copy"):
        bound = bound.model_copy(update={"openai_model": model})
    return OpenAIAdapter(bound)


from contextlib import contextmanager as _contextmanager
from contextvars import ContextVar as _ContextVar

#: What the work at hand is (a `model_router.RouteRequest`), set around a model call by the code
#: that knows — an agent run, a panel phase, a skill — so `project_chat_adapter`'s signature,
#: and every caller and test double of it, stays exactly as it was.
_PURPOSE: "_ContextVar[Any]" = _ContextVar("model_route_purpose", default=None)


@_contextmanager
def routing_purpose(request: Any):
    """`with routing_purpose(RouteRequest(...)): adapter = project_chat_adapter(...)`."""
    token = _PURPOSE.set(request)
    try:
        yield request
    finally:
        _PURPOSE.reset(token)


def project_chat_adapter(connection: Any, settings: Any, project_id: Optional[str], *,
                         purpose: Any = None):
    """The model this project connected, as a chat adapter.

    Every adapter it returns implements `create_chat_completion` (tool-calling turns, the agent
    loop) and `create_text_response` (one-shot), and reports usage the caller records.

    THE MODEL ROUTER DECIDES (docs/product/MODEL_ROUTER.md). `purpose` is the boundary's
    `model_router.RouteRequest` — what the work is (a chat thread, an agent step, a panel
    seat), so a Smart route can pick its tier. In Fixed mode (the default) the answer is
    exactly `choose`'s, as it always was; a call site that names its purpose has the decision
    recorded either way, so "Why this model" can answer.
    """
    purpose = purpose if purpose is not None else _PURPOSE.get()
    resolved = routed_settings(connection, settings, project_id)
    decision = route_call(connection, settings, project_id, purpose)
    if decision is not None and decision.refused:
        # LOCAL MEANS LOCAL; FRONTIER NEVER SILENTLY GOES LOCAL; NO METERED SPEND WHILE THE
        # BUDGET IS SPENT. A route that may not run anywhere refuses, in words — it never falls
        # through to whatever `choose` would have picked (docs/product/MODEL_ROUTER.md).
        from .model_router.failover import RouteRefused

        raise RouteRefused(decision.refusal)
    if decision is not None and decision.engine and (decision.mode == "smart"
                                                     or decision.binding
                                                     or "asked_tier" in decision.rules):
        # A Smart route, a binding agent policy, or a Fixed route the caller asked a tier of
        # (RouteRequest.tier): the decision's own engine and model.
        choice = ModelChoice(decision.engine, decision.reason, decision.source_id)
        adapter = adapter_for(choice, connection, resolved, project_id, model=decision.model)
        adapter = _failover(adapter, decision, settings, project_id, purpose)
    else:
        choice = choose(connection, resolved, project_id)
        adapter = adapter_for(choice, connection, resolved, project_id)
    try:
        adapter._boss_choice = choice
        adapter._boss_route = decision
    except Exception:  # noqa: BLE001 — a slotted adapter just goes unannotated
        pass
    return adapter


def _failover(adapter: Any, decision: Any, settings: Any, project_id: Optional[str],
              purpose: Any) -> Any:
    """The adapter, recorded against its route and retried on the next source when its own
    fails (model_router.failover). A call that needs a new source builds it on a fresh
    connection: the adapter may outlive the one it was built on (a chat thread's)."""
    from dataclasses import replace

    from .model_router import store
    from .model_router.failover import FailoverChatAdapter

    def reroute(failed_engine: str):
        from ..db import begin

        store.invalidate(project_id)
        again = replace(purpose, session_pin=None, sticky=False) if purpose is not None \
            else None
        with begin() as c:
            nxt = route_call(c, settings, project_id, again)
            if nxt is None or nxt.refused or not nxt.engine or nxt.engine == failed_engine:
                return None, nxt
            resolved = routed_settings(c, settings, project_id)
            inner = adapter_for(ModelChoice(nxt.engine, nxt.reason, nxt.source_id), c,
                                resolved, project_id, model=nxt.model)
        return inner, nxt

    stage = getattr(purpose, "step", None) or "chat"
    # agent_converse records its own turns; everything that names a purpose is recorded here.
    return FailoverChatAdapter(adapter, decision, reroute, stage=stage,
                               record=purpose is not None)


def route_call(connection: Any, settings: Any, project_id: Optional[str], purpose: Any = None):
    """The router's decision for a chat-shaped call, or None when routing has nothing to add
    (Fixed mode with no named purpose: the legacy answer, unrecorded, as before)."""
    try:
        from .model_router import store
        from .model_router.router import LegacyRoute, RouteRequest

        if purpose is None:
            if store.mode_of(connection, settings, project_id) != "smart":
                return None
            purpose = RouteRequest(kind="workflow_step", step="model_call",
                                   project_id=project_id)
        elif not getattr(purpose, "project_id", None):
            from dataclasses import replace

            purpose = replace(purpose, project_id=project_id)
        if purpose.legacy is None:
            from dataclasses import replace

            def legacy() -> LegacyRoute:
                picked = choose(connection, routed_settings(connection, settings, project_id),
                                project_id)
                knob = chat_model_knob(picked.engine)
                model = _chosen(connection, project_id, knob) or getattr(settings, knob, None)
                return LegacyRoute(picked.engine, str(model) if model else None,
                                   f"Chat → {picked.describe()}")
            purpose = replace(purpose, legacy=legacy)
        return store.route(connection, settings, purpose,
                           record_it=purpose.step != "model_call")
    except Exception:  # noqa: BLE001 — routing must never fail the call it serves
        import logging

        logging.getLogger("boss.model_router").warning("route_call failed", exc_info=True)
        return None


# ── Spend: a model turn is reserved against the budget and receipted ─────────────────────────

class ModelSpendRefused(IntegrationConfigurationError):
    """The budget refused a model turn. Settled: retrying cannot raise the cap."""

    MARKER = "the budget refused this model turn"


class ModelSpendDeferred(RuntimeError):
    """The per-minute (burst) budget is full. NOT settled: it frees itself within a minute, so
    the step retries — model_failures explains it as "waiting for the per-minute budget".
    `retry_after_seconds` is the budget's own answer to "how long until there is room"."""

    def __init__(self, message: str = "", retry_after_seconds: Optional[float] = None):
        super().__init__(message)
        self.retry_after_seconds = retry_after_seconds


#: Engines whose turns are not metered per token: subscriptions and local models. Their turns
#: reserve nothing — the reservation is then a GATE (refused once the cap is spent) rather than
#: a hold, so a flat-rate agent cannot trip the $/minute burst cap by talking.
UNMETERED = frozenset({"claude_code", "codex", "ollama", "vllm"})


def turn_is_included(choice: ModelChoice, usage: Optional[dict] = None) -> bool:
    """Whether a turn on `choice` cost no money (included_spend.py).

    The adapter's own word wins when it gives one (`usage["billing"]`: the Claude CLI on an
    ANTHROPIC_API_KEY is metered even though its engine is claude_code); otherwise the engine
    decides — a subscription CLI or a local model is included."""
    from .included_spend import call_is_included as _one_decision

    return _one_decision(choice.engine, usage)


def call_is_included(connection: Any, settings: Any, project_id: Optional[str],
                     usage: Optional[dict]) -> bool:
    """Whether a call this project's model answered cost no money — for a caller that settles
    a call's cost itself. The usage's `billing` stamp wins; otherwise the source `choose` picks
    for the project decides. Never raises: unknown is money, the safe side."""
    if isinstance(usage, dict) and usage.get("billing"):
        return turn_is_included(ModelChoice("", ""), usage)
    try:
        choice = choose(connection, routed_settings(connection, settings, project_id),
                        project_id)
    except Exception:  # noqa: BLE001 — a lookup failure is not a reason to call it free
        return False
    return turn_is_included(choice, usage)


def turn_estimate_usd_micros(choice: ModelChoice, settings: Any) -> int:
    """What one model turn on `choice` reserves before it is sent."""
    if not choice.engine or choice.engine in UNMETERED:
        return 0
    return int(getattr(settings, "agent_turn_budget_usd_micros", 0) or 20_000)


def not_dispatched(error: BaseException) -> bool:
    """True when `error` was raised before any request left BOSS: a configuration the adapter
    refused (IntegrationConfigurationError — no key, no endpoint, an outbound check), or an
    error that says so itself (`provider_not_dispatched = True`). Such a call spent nothing."""
    if getattr(error, "provider_not_dispatched", False) is True:
        return True
    return isinstance(error, IntegrationConfigurationError)


class GovernedChatAdapter:
    """Reserve, call, settle — around every turn of an adapter `project_chat_adapter` built.

    The agent loop's turns spent with no reservation and no receipt, so a capped project could
    run an agent step past its cap and the cost appeared nowhere. Each call here reserves
    `estimate_usd_micros` with the BudgetGovernor (refused → `ModelSpendRefused`), writes a
    receipt, and settles the reservation with the turn's real cost; a failed call releases it.
    """

    def __init__(self, inner: Any, connection: Any, settings: Any, project_id: str, *,
                 skill_id: str, ref: str, estimate_usd_micros: int,
                 choice: Optional[ModelChoice] = None, category: str = "model"):
        self._inner = inner
        #: The budget category the call spends: "model" for a turn, "knowledge" for an embedding.
        self._category = category
        self._connection = connection
        self._settings = settings
        self._project_id = project_id
        self._skill_id = skill_id
        self._ref = ref
        self._estimate = max(0, int(estimate_usd_micros))
        self._choice = choice or ModelChoice("", "unknown")
        self._turn = 0

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)

    @property
    def stream_text_response(self) -> Any:
        """The inner adapter's stream, governed like every other call: reserved before the
        first word, settled with the real usage after the last, released if it fails or is
        cut off. None when the inner adapter cannot stream (the caller then asks for the whole
        answer) — `__getattr__` would otherwise hand out the inner stream ungoverned."""
        inner = getattr(self._inner, "stream_text_response", None)
        if not callable(inner):
            return None

        def stream(*args: Any, **kwargs: Any):
            held = self._open()
            usage: dict = {}
            try:
                for piece in inner(*args, **kwargs):
                    if isinstance(piece, dict):
                        usage = piece
                    yield piece
            except BaseException as error:  # GeneratorExit too: a reply cut off spends nothing
                self._fail(held, error)
                raise
            self._settle(held, usage)

        return stream

    # THE GATE, WITHOUT THE CALL (kernel/governance.py). The harness kernel makes the model call
    # itself and wraps it in these three, so a kernel run is reserved, receipted and settled by
    # exactly this code rather than by a second copy of it.
    def reserve_turn(self) -> tuple[str, str]:
        """Reserve one turn and open its receipt (raises ModelSpendRefused/Deferred)."""
        return self._open()

    def settle_turn(self, held: tuple[str, str], usage: Any) -> None:
        self._settle(held, usage)

    def release_turn(self, held: tuple[str, str], error: BaseException) -> None:
        self._fail(held, error)

    def create_chat_completion(self, *args: Any, **kwargs: Any):
        return self._governed("create_chat_completion", args, kwargs, usage_at=1)

    def create_text_response(self, *args: Any, **kwargs: Any):
        return self._governed("create_text_response", args, kwargs, usage_at=2)

    @property
    def create_embeddings(self) -> Any:
        """The inner adapter's embeddings call, governed like a turn: `(vectors, usage)`. None when
        the inner adapter has no embeddings — `__getattr__` would otherwise hand out the inner
        call ungoverned."""
        if not callable(getattr(self._inner, "create_embeddings", None)):
            return None
        return lambda *args, **kwargs: self._governed("create_embeddings", args, kwargs, usage_at=1)

    def _governed(self, method: str, args: tuple, kwargs: dict, *, usage_at: int):
        held = self._open()
        try:
            result = getattr(self._inner, method)(*args, **kwargs)
        except Exception as error:
            self._fail(held, error)
            raise
        usage = result[usage_at] if isinstance(result, tuple) and len(result) > usage_at else {}
        self._settle(held, usage)
        return result

    def _open(self) -> tuple[str, str]:
        """Reserve one turn and open its receipt: (reservation_id, receipt_id), or raise.

        RESERVE IN ITS OWN SHORT TRANSACTION, committed before the provider call, as
        model_gateway.acquire's caller does ("The caller committed this reservation before the
        provider attempt" / "Never wait while holding budget locks"). `budget.reserve` takes
        `boss-budget:<project>` with pg_try_advisory_xact_lock — a TRANSACTION-scoped lock. The
        caller's `self._connection` is long-lived (an agent run's whole tick is one transaction),
        and the 12-78s inner provider call runs on it next; leaving the reservation on it would
        hold the lock across that call, so the connection sits "idle in transaction" and every
        concurrent admission for the project is starved with ADMISSION_BUSY_MESSAGE.

        So the reservation runs on a FRESH connection from the caller's own engine and is
        committed there — the lock is gone before any model I/O, and the caller's transaction is
        never committed out from under its step (brec_042fa4c58dbb40259009; diagnosed by BOSS,
        sfx_3ae11453 / sfx_be271156). It reserves in place, as before, only when a fresh
        connection cannot see the project yet (a project created in the caller's own open
        transaction — its first turn, or a test) or the connection has no engine (a double).
        """
        engine = getattr(self._connection, "engine", None)
        if engine is not None:
            from sqlalchemy import text as _sql

            from ..db import begin

            refused: Optional[BaseException] = None
            with begin(engine) as connection:
                if connection.execute(_sql("SELECT 1 FROM projects WHERE id = :p"),
                                      {"p": self._project_id}).first() is not None:
                    try:
                        return self._reserve(connection)
                    except (ModelSpendRefused, ModelSpendDeferred) as error:
                        # Raised AFTER this transaction commits: the refusal's own records
                        # (policy evaluation, refusal) are kept, as they were on the caller's.
                        refused = error
            if refused is not None:
                raise refused
        return self._reserve(self._connection)

    def _reserve(self, connection: Any) -> tuple[str, str]:
        """Reserve one turn and open its receipt on `connection`; the caller commits it."""
        import uuid

        from sqlalchemy import text

        from .budget import BudgetGovernor
        from .receipts import ReceiptService

        project_id = self._project_id
        budget = BudgetGovernor(self._settings)
        reservation = budget.reserve(
            connection, project_id, self._category, self._estimate,
            f"{self._skill_id}: one model turn ({self._ref})", 15)
        if reservation.get("status") != "reserved":
            message = str(reservation.get("message") or "")
            if "burst" in message.lower():
                raise ModelSpendDeferred(
                    f"waiting for the per-minute budget: {message} (burst cap)",
                    retry_after_seconds=reservation.get("retry_after_seconds"))
            if reservation.get("transient"):
                # A TRANSIENT REFUSAL IS A WAIT, NOT A CAP: "admission is busy" was raised as
                # ModelSpendRefused "(budget cap …)" — terminal — so a scan colliding with
                # another for a second stopped "budget refused" (brec_6025901347b6445eb9c1).
                raise ModelSpendDeferred(
                    f"waiting for the budget: {message}",
                    retry_after_seconds=reservation.get("retry_after_seconds"))
            raise ModelSpendRefused(
                f"{ModelSpendRefused.MARKER}: "
                f"{reservation.get('message') or 'Budget reservation denied.'} "
                f"(budget cap; decision {reservation.get('decision_id') or 'none'})")
        scope = connection.execute(
            text("SELECT w.org_id, p.workspace_id FROM projects p "
                 "JOIN workspaces w ON w.id = p.workspace_id WHERE p.id = :p"),
            {"p": project_id},
        ).mappings().first()
        if scope is None:
            budget.release(connection, project_id, reservation["reservation_id"],
                           "project has no tenant scope for a receipt")
            raise IntegrationConfigurationError(
                f"project {project_id!r} has no tenant scope; a model turn cannot be receipted")
        self._turn += 1
        receipt, _ = ReceiptService().create_or_reuse(
            connection, str(scope["org_id"]), str(scope["workspace_id"]), project_id,
            f"{self._skill_id}:{self._ref}:{self._turn}:{uuid.uuid4().hex[:12]}",
            # LINKED TO ITS RESERVATION, so an expired hold can be reconciled from this receipt
            # alone (budget.reconcile_expired_reservations) instead of becoming a person's card.
            self._skill_id, {"data_boundary": "internal", "ref": self._ref,
                             "budget_reservation_id": reservation["reservation_id"]},
            {"turn": self._turn}, data_boundary="internal",
        )
        return reservation["reservation_id"], receipt["id"]

    def _ledger(self):
        """The connection the turn's outcome is written on: the caller's, or — when the caller's
        connection is already closed or invalid — a fresh transaction on its engine. An outcome
        written on a closed connection is lost, and its reservation expires into a person's
        decision (51cd3c2ba: every knowledge.embed hold on northpeak-lending)."""
        import contextlib

        connection = self._connection
        engine = getattr(connection, "engine", None)
        unusable = (getattr(connection, "closed", False) is True
                    or getattr(connection, "invalidated", False) is True)
        if unusable and engine is not None:
            from ..db import begin

            return begin(engine)
        return contextlib.nullcontext(connection)

    def _fail(self, held: tuple[str, str], error: BaseException) -> None:
        from .budget import BudgetGovernor
        from .receipts import ReceiptService

        reservation_id, receipt_id = held
        failure = {"message": str(error)[:500] or type(error).__name__, "skill_id": self._skill_id}
        if not_dispatched(error):
            failure["code"] = "provider_not_dispatched"
        with self._ledger() as connection:
            ReceiptService().mark_failed(connection, receipt_id, failure)
            BudgetGovernor(self._settings).release(connection, self._project_id,
                                                   reservation_id, f"{self._skill_id} turn failed")

    def _settle(self, held: tuple[str, str], usage: Any) -> None:
        with self._ledger() as connection:
            self._settle_on(connection, held, usage)

    def _settle_on(self, connection: Any, held: tuple[str, str], usage: Any) -> None:
        from .budget import BudgetGovernor
        from .openai_adapter import estimate_cost_usd_micros
        from .receipts import ReceiptService

        reservation_id, receipt_id = held
        usage = usage if isinstance(usage, dict) else {}
        # What the turn is worth at list price; on a subscription that is not money
        # (included_spend.py) — settled $0, the value kept as its API equivalent.
        worth = estimate_cost_usd_micros(usage)
        included = turn_is_included(self._choice, usage)
        ReceiptService().mark_succeeded(
            connection, receipt_id, str(usage.get("model") or ""),
            {"usage": usage, "actual_usd_micros": 0 if included else worth,
             **({"included_in_subscription": True,
                 "equivalent_usd_micros": worth} if included else {})})
        BudgetGovernor(self._settings).settle(connection, self._project_id,
                                              reservation_id, receipt_id, self._category,
                                              self._skill_id, self._estimate, worth,
                                              included_in_subscription=included)


def governed(adapter: Any, connection: Any, settings: Any, project_id: str, *,
             skill_id: str, ref: str, category: str = "model",
             estimate_usd_micros: Optional[int] = None) -> Any:
    """`adapter` with every call reserved, receipted and settled (GovernedChatAdapter).

    `category` is the budget category spent (an embedding's is "knowledge");
    `estimate_usd_micros` is what one call reserves, else the source's turn estimate.

    The estimate follows the source `choose` picks for this project: a metered key reserves
    `agent_turn_budget_usd_micros` per turn, a subscription or local model reserves nothing but
    is still refused once the cap is spent.
    """
    if connection is None or not project_id:
        raise IntegrationConfigurationError(
            f"{skill_id} needs a project and a connection to reserve its model spend")
    # The source the adapter was built for (a Smart route), else the project's chat source.
    choice = getattr(adapter, "_boss_choice", None)
    if not isinstance(choice, ModelChoice):
        choice = choose(connection, routed_settings(connection, settings, project_id), project_id)
    return GovernedChatAdapter(adapter, connection, settings, project_id,
                               skill_id=skill_id, ref=ref,
                               estimate_usd_micros=(turn_estimate_usd_micros(choice, settings)
                                                    if estimate_usd_micros is None
                                                    else estimate_usd_micros),
                               choice=choice, category=category)


# ── The embedding model ────────────────────────────────────────────────────────────────────────
#
# ITS OWN SLOT, one per org (operator decisions 2026-10-22/23): Setup › Repo and models ›
# Embedding model stores a provider, a model and its dimension (knowledge/embedding_slot.py). Every embedding
# call is built HERE from that slot and governed: reserved in the budget category `knowledge`,
# receipted and settled. Nothing else builds an EmbeddingAdapter
# (engine/tests/test_embedding_model_slot.py).

#: What one embedding call reserves before it is sent: a probe or a batch of summaries is a
#: fraction of a cent on any hosted embedding model.
EMBEDDING_ESTIMATE_USD_MICROS = 2_000
EMBEDDING_CATEGORY = "knowledge"


class ProjectEmbedder:
    """`embed(texts) -> vectors` on the project's embedding slot, every call governed.
    `model_id` keys the vector space: provider, model and dimension, so spaces never mix."""

    def __init__(self, governed_adapter: Any, model_id: str, dimension: int):
        self._adapter = governed_adapter
        self.model_id = model_id
        self.dimension = int(dimension)
        self.last_usage: dict = {}

    def embed(self, texts: list[str]) -> list[list[float]]:
        call = self._adapter.create_embeddings
        if call is None:
            raise IntegrationConfigurationError("this provider has no embeddings call")
        vectors, usage = call(list(texts))
        self.last_usage = usage if isinstance(usage, dict) else {}
        return [list(v) for v in vectors]


def project_embedder(connection: Any, settings: Any, project_id: str, slot: dict[str, Any], *,
                     ref: str = "embed", skill_id: str = "knowledge.embed") -> ProjectEmbedder:
    """The governed embedder for `slot` (embedding_slot.read): its key resolved from the vault
    for this project, never from the environment."""
    from .embedding_adapter import LOCAL_PROVIDERS, EmbeddingAdapter
    from .knowledge.embedding_slot import model_id_of
    from .secret_backends import resolve_secret_ref

    provider = str(slot.get("provider") or "")
    key = None
    if slot.get("secret_ref_id"):
        key = resolve_secret_ref(connection, str(slot["secret_ref_id"]), settings, project_id=project_id)
    # Resolve the Ollama host at call time, like every other Ollama lane
    # (`found.base_url or setup_service.ollama_base_url(settings)`): an empty slot URL means the
    # setting, so inside a Community container the embedder reaches the host's Ollama rather than
    # the adapter's hardcoded localhost. The host only — the key still comes from the vault.
    base_url = str(slot.get("base_url") or "")
    if provider == "ollama" and not base_url:
        from . import setup_service

        base_url = setup_service.ollama_base_url(settings)
    adapter = EmbeddingAdapter(provider, str(slot.get("model") or ""), int(slot.get("dimension") or 0),
                               base_url=base_url,
                               endpoint=str(slot.get("endpoint") or ""),
                               deployment=str(slot.get("deployment") or ""),
                               api_version=str(slot.get("api_version") or ""), api_key=key,
                               request_dimensions=(int(slot.get("dimension") or 0) or None)
                               if slot.get("dimension_set") else None)
    adapter._boss_choice = ModelChoice(provider if provider in LOCAL_PROVIDERS else f"embedding:{provider}",
                                       "the project's embedding model")
    wrapped = governed(adapter, connection, settings, project_id, skill_id=skill_id, ref=ref,
                       category=EMBEDDING_CATEGORY,
                       estimate_usd_micros=0 if provider in LOCAL_PROVIDERS else EMBEDDING_ESTIMATE_USD_MICROS)
    return ProjectEmbedder(wrapped, model_id_of(slot), int(slot.get("dimension") or 0))


# ── Provider-specific capabilities ─────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class ProviderRequirement:
    """Something only one provider's API does, which a skill needs on top of a chat model."""

    key: str
    #: What the skill does with it, as an operator says it: "image generation".
    capability: str
    #: Who provides it: "OpenAI".
    provider: str
    #: `secret_refs.provider` for a key stored through the console.
    secret_provider: str
    #: The environment fallback on Settings, for an install configured the old way.
    settings_attr: str

    @property
    def need_key(self) -> str:
        return f"provider:{self.key}"

    @property
    def label(self) -> str:
        return f"An {self.provider} key for {self.capability}"

    @property
    def fix(self) -> str:
        return (f"Connect an {self.provider} key for {self.capability} in Repo and models. "
                f"A subscription or another provider's key does not do {self.capability}.")


#: Every provider-specific capability a skill may declare. A skill naming one not in here is
#: refused at registration, so readiness can always say what it is and how to get it.
PROVIDER_REQUIREMENTS: dict[str, ProviderRequirement] = {
    r.key: r for r in (
        ProviderRequirement("openai_images", "image generation", "OpenAI", "openai",
                            "openai_api_key"),
        ProviderRequirement("openai_speech", "speech-to-text", "OpenAI", "openai",
                            "openai_api_key"),
    )
}

#: Agent tools (tools/builtin.py, the chat agent's and the MCP surface's catalogue) that need a
#: provider capability. Declared here rather than at registration so readiness and the
#: enforcing test can read them without building a tool registry.
TOOL_REQUIREMENTS: dict[str, tuple[str, ...]] = {
    "generate_image": ("openai_images",),
}


def tool_requirement_id(tool_name: str) -> str:
    """The declaration id a tool's requirement is recorded under: `tool:<name>`."""
    return f"tool:{tool_name}"


#: skill_id (or `tool:<name>`) -> the requirement keys it declared.
_DECLARED: dict[str, tuple[str, ...]] = {
    tool_requirement_id(name): keys for name, keys in TOOL_REQUIREMENTS.items()
}


class ProviderRequirementMissing(IntegrationConfigurationError):
    """A declared provider requirement is not met. The message carries the fix."""

    MARKER = "needs a provider capability this project has not connected"

    def __init__(self, skill_id: str, requirement: ProviderRequirement):
        self.skill_id = skill_id
        self.requirement = requirement
        super().__init__(
            f"{skill_id} {self.MARKER}: {requirement.label}. {requirement.fix}")


def requirement(key: str) -> ProviderRequirement:
    try:
        return PROVIDER_REQUIREMENTS[key]
    except KeyError:
        raise ValueError(
            f"unknown provider requirement {key!r}; declare it in "
            f"model_routing.PROVIDER_REQUIREMENTS so readiness can name its fix") from None


def declare(skill_id: str, keys: Any) -> None:
    """Record what `skill_id` needs beyond a chat model. Called by `register_executor`."""
    if isinstance(keys, str):
        keys = (keys,)
    wanted = tuple(dict.fromkeys(requirement(str(k)).key for k in (keys or ())))
    if wanted:
        _DECLARED[skill_id.strip()] = wanted
    else:
        _DECLARED.pop(skill_id.strip(), None)


def declared_requirements(skill_id: str, spec: Optional[dict[str, Any]] = None
                          ) -> list[ProviderRequirement]:
    """What `skill_id` declared: on its executor, or as `policy.requires_provider` in its spec."""
    keys = list(_DECLARED.get(str(skill_id).strip(), ()))
    policy = (spec or {}).get("policy") if isinstance((spec or {}).get("policy"), dict) else {}
    named = policy.get("requires_provider") if policy else None
    if isinstance(named, str):
        named = [named]
    for key in named or []:
        if str(key) in PROVIDER_REQUIREMENTS and str(key) not in keys:
            keys.append(str(key))
    return [PROVIDER_REQUIREMENTS[k] for k in keys]


def requirement_for_need(need_key: str) -> Optional[ProviderRequirement]:
    if not need_key.startswith("provider:"):
        return None
    return PROVIDER_REQUIREMENTS.get(need_key.split(":", 1)[1])


def provider_connected(connection: Any, settings: Any, project_id: str,
                       req: ProviderRequirement) -> bool:
    """Whether this project holds the key `req` needs — counted, never decrypted."""
    if str(getattr(settings, req.settings_attr, "") or "").strip():
        return True
    if connection is None or not project_id:
        return False
    from sqlalchemy import text

    try:
        with connection.begin_nested():
            found = connection.execute(
                text("SELECT count(*) FROM secret_refs WHERE project_id = :p "
                     "AND provider = :provider "
                     "AND status IN ('active', 'rotating', 'rotation_due')"),
                {"p": project_id, "provider": req.secret_provider},
            ).scalar()
    except Exception:  # noqa: BLE001 — no table means no stored key
        return False
    return bool(found)


def provider_credential(context: Any, skill_id: str, key: str) -> str:
    """The key a DECLARED requirement needs, or `ProviderRequirementMissing` with its fix.

    The only sanctioned way for an executor to read a provider key. Refuses a requirement the
    skill did not declare, because an undeclared one is a need readiness cannot see.
    """
    req = requirement(key)
    if req.key not in _DECLARED.get(skill_id, ()):
        raise ValueError(
            f"{skill_id} reads the {req.provider} key for {req.capability} without declaring "
            f"it; pass requires={req.key!r} to register_executor so readiness can check it")
    value: Optional[str] = None
    connection = getattr(context, "connection", None)
    project_id = getattr(context, "project_id", "") or ""
    settings = getattr(context, "settings", None)
    if connection is not None and project_id:
        try:
            from .credential_resolve import resolve_project_credential

            value = resolve_project_credential(
                connection, settings, project_id=project_id,
                provider=req.secret_provider, env_fallback_attr=req.settings_attr)
        except Exception:  # noqa: BLE001 — unresolvable is missing
            value = None
    if not value:
        value = str(getattr(settings, req.settings_attr, "") or "").strip() or None
    if not value:
        raise ProviderRequirementMissing(skill_id, req)
    return value
