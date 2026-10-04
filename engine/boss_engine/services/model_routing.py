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
    """The source this project's model calls go to. Reads; never decrypts, never probes.

    Order, most deliberate first:
      1. the engine chosen for this project in the console (or `BOSS_CHAT_LLM_ENGINE`),
      2. a subscription CLI the deployment named in `BOSS_LLM_ENGINE`,
      3. an API key in the deployment's environment (an install configured the old way),
      4. a source this project connected — a subscription or a stored key — preferring one
         whose recorded capacity is not exhausted,
      5. nothing: `engine == ""`.
    """
    from . import project_settings

    try:
        chosen = project_settings.effective(connection, project_id or "", settings,
                                            "chat_llm_engine")
    except Exception:  # noqa: BLE001 — an unreachable settings table is not an answer
        chosen = getattr(settings, "chat_llm_engine", "")
    chosen = _engine_name(chosen)
    if chosen in CHAT_ROUTABLE:
        return ModelChoice(chosen, "chosen for this project")
    legacy = _engine_name(getattr(settings, "llm_engine", ""))
    if legacy in ("claude_code", "codex"):
        return ModelChoice(legacy, ENVIRONMENT)
    if str(getattr(settings, "openai_api_key", "") or "").strip():
        return ModelChoice("native", ENVIRONMENT)
    if all(str(getattr(settings, a, "") or "").strip() for a in (
            "azure_openai_key", "azure_openai_endpoint", "azure_openai_deployment")):
        return ModelChoice("azure", ENVIRONMENT)
    try:
        # Through the module: one lookup, patched in one place.
        from . import setup_service

        connected = setup_service.connected_engines(connection, settings, project_id or "")
    except Exception:  # noqa: BLE001 — a lookup failure must not fail the work it serves
        connected = {}
    candidates = [e for e in CHAT_ROUTABLE if e in connected]
    # CAPACITY. A subscription recorded as exhausted (provider_limits) is connected and cannot
    # answer until its window resets; another connected source can. Used only when one is.
    ready = [e for e in candidates
             if (connected[e].get("capacity") or {}).get("available", True)]
    for engine in ready or candidates:
        found = connected[engine]
        return ModelChoice(engine, str(found.get("why") or "connected"),
                           found.get("source_id"))
    return ModelChoice("", "nothing this project holds can answer")


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
                project_id: Optional[str]):
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
    model = (getattr(settings, "chat_model", "") or "").strip() or None
    if engine == "claude_code":
        return ClaudeCliAdapter(bound, model=model)
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
        return OpenAIAdapter(lane, provider=engine)
    return OpenAIAdapter(bound)


def project_chat_adapter(connection: Any, settings: Any, project_id: Optional[str]):
    """The model this project connected, as a chat adapter.

    Every adapter it returns implements `create_chat_completion` (tool-calling turns, the agent
    loop) and `create_text_response` (one-shot), and reports usage the caller records.
    """
    resolved = routed_settings(connection, settings, project_id)
    return adapter_for(choose(connection, resolved, project_id), connection, resolved,
                       project_id)


# ── Spend: a model turn is reserved against the budget and receipted ─────────────────────────

class ModelSpendRefused(IntegrationConfigurationError):
    """The budget refused a model turn. Settled: retrying cannot raise the cap."""

    MARKER = "the budget refused this model turn"


class ModelSpendDeferred(RuntimeError):
    """The per-minute (burst) budget is full. NOT settled: it frees itself within a minute, so
    the step retries — model_failures explains it as "waiting for the per-minute budget"."""


#: Engines whose turns are not metered per token: subscriptions and local models. Their turns
#: reserve nothing — the reservation is then a GATE (refused once the cap is spent) rather than
#: a hold, so a flat-rate agent cannot trip the $/minute burst cap by talking.
UNMETERED = frozenset({"claude_code", "codex", "ollama", "vllm"})


def turn_estimate_usd_micros(choice: ModelChoice, settings: Any) -> int:
    """What one model turn on `choice` reserves before it is sent."""
    if not choice.engine or choice.engine in UNMETERED:
        return 0
    return int(getattr(settings, "agent_turn_budget_usd_micros", 0) or 20_000)


class GovernedChatAdapter:
    """Reserve, call, settle — around every turn of an adapter `project_chat_adapter` built.

    The agent loop's turns spent with no reservation and no receipt, so a capped project could
    run an agent step past its cap and the cost appeared nowhere. Each call here reserves
    `estimate_usd_micros` with the BudgetGovernor (refused → `ModelSpendRefused`), writes a
    receipt, and settles the reservation with the turn's real cost; a failed call releases it.
    """

    def __init__(self, inner: Any, connection: Any, settings: Any, project_id: str, *,
                 skill_id: str, ref: str, estimate_usd_micros: int):
        self._inner = inner
        self._connection = connection
        self._settings = settings
        self._project_id = project_id
        self._skill_id = skill_id
        self._ref = ref
        self._estimate = max(0, int(estimate_usd_micros))
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

    def create_chat_completion(self, *args: Any, **kwargs: Any):
        return self._governed("create_chat_completion", args, kwargs, usage_at=1)

    def create_text_response(self, *args: Any, **kwargs: Any):
        return self._governed("create_text_response", args, kwargs, usage_at=2)

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
        """Reserve one turn and open its receipt: (reservation_id, receipt_id), or raise."""
        import uuid

        from sqlalchemy import text

        from .budget import BudgetGovernor
        from .receipts import ReceiptService

        connection, project_id = self._connection, self._project_id
        budget = BudgetGovernor(self._settings)
        reservation = budget.reserve(
            connection, project_id, "model", self._estimate,
            f"{self._skill_id}: one model turn ({self._ref})", 15)
        if reservation.get("status") != "reserved":
            message = str(reservation.get("message") or "")
            if "burst" in message.lower():
                raise ModelSpendDeferred(
                    f"waiting for the per-minute budget: {message} (burst cap)")
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
            self._skill_id, {"data_boundary": "internal", "ref": self._ref},
            {"turn": self._turn}, data_boundary="internal",
        )
        return reservation["reservation_id"], receipt["id"]

    def _fail(self, held: tuple[str, str], error: BaseException) -> None:
        from .budget import BudgetGovernor
        from .receipts import ReceiptService

        reservation_id, receipt_id = held
        ReceiptService().mark_failed(self._connection, receipt_id,
                                     {"message": str(error)[:500] or type(error).__name__,
                                      "skill_id": self._skill_id})
        BudgetGovernor(self._settings).release(self._connection, self._project_id,
                                               reservation_id, f"{self._skill_id} turn failed")

    def _settle(self, held: tuple[str, str], usage: Any) -> None:
        from .budget import BudgetGovernor
        from .openai_adapter import estimate_cost_usd_micros
        from .receipts import ReceiptService

        reservation_id, receipt_id = held
        usage = usage if isinstance(usage, dict) else {}
        actual = estimate_cost_usd_micros(usage)
        ReceiptService().mark_succeeded(self._connection, receipt_id,
                                        str(usage.get("model") or ""),
                                        {"usage": usage, "actual_usd_micros": actual})
        BudgetGovernor(self._settings).settle(self._connection, self._project_id,
                                              reservation_id, receipt_id, "model",
                                              self._skill_id, self._estimate, actual)


def governed(adapter: Any, connection: Any, settings: Any, project_id: str, *,
             skill_id: str, ref: str) -> Any:
    """`adapter` with every call reserved, receipted and settled (GovernedChatAdapter).

    The estimate follows the source `choose` picks for this project: a metered key reserves
    `agent_turn_budget_usd_micros` per turn, a subscription or local model reserves nothing but
    is still refused once the cap is spent.
    """
    if connection is None or not project_id:
        raise IntegrationConfigurationError(
            f"{skill_id} needs a project and a connection to reserve its model spend")
    choice = choose(connection, routed_settings(connection, settings, project_id), project_id)
    return GovernedChatAdapter(adapter, connection, settings, project_id,
                               skill_id=skill_id, ref=ref,
                               estimate_usd_micros=turn_estimate_usd_micros(choice, settings))


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
        return (f"Connect an {self.provider} key for {self.capability} in Repo and model. "
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
