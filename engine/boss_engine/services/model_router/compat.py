"""WHICH MODEL NAMES AN ENGINE CAN RUN — one rule every route and every launcher applies.

Observed on boss-1, 2026-11-03: the operator set Build to Codex · gpt-6-sol while Routine work
stayed on Claude Code · sonnet. Fixed mode took the ENGINE from the Routine (low-cost) lane and
the MODEL from Build, and launched `claude --model gpt-6-sol` — which fails — on every implement
and write_tests step: four builds failed in three minutes. Nothing anywhere checked that a model
belongs to the engine it is sent to, so any mix of engines across jobs could produce an
impossible pair. Switching models is routine under a budget (the operator moves work between a
subscription and an API key several times a day), so this is checked once, here, and applied
at the routing decision (store.route), in Fixed mode's lane resolution (pipeline_profiles
.materialize_route) and as the last line in the Claude and Codex launchers.

The rule is about FAMILIES, not catalogues: a Claude engine runs Claude models, a Codex/OpenAI
engine runs OpenAI models, and so on. A name whose family cannot be told (a custom deployment,
a gateway alias) is allowed — refusing a real model would be worse than not catching an odd
one — and an engine that fronts many providers (OpenRouter, local servers, custom gateways)
accepts any name.
"""
from __future__ import annotations

from typing import Optional

ANTHROPIC, OPENAI, GOOGLE, XAI = "anthropic", "openai", "google", "xai"

#: The model family an engine runs. An engine not listed fronts many providers (OpenRouter,
#: Ollama, vLLM, Groq, DeepSeek, a custom OpenAI-compatible gateway) and accepts any name.
ENGINE_FAMILY: dict[str, str] = {
    "claude_code": ANTHROPIC, "claude_agent_sdk": ANTHROPIC, "claude": ANTHROPIC,
    "claude_cli": ANTHROPIC, "anthropic": ANTHROPIC,
    "codex": OPENAI, "codex_cli": OPENAI, "native": OPENAI, "openai": OPENAI, "azure": OPENAI,
    "gemini": GOOGLE, "gemini_cli": GOOGLE,
    "xai": XAI, "grok": XAI,
}

_PREFIXES: tuple[tuple[str, tuple[str, ...]], ...] = (
    (ANTHROPIC, ("claude", "haiku", "sonnet", "opus")),
    (OPENAI, ("gpt", "o1", "o3", "o4", "codex", "chatgpt", "davinci")),
    (GOOGLE, ("gemini", "gemma")),
    (XAI, ("grok",)),
)


#: Engines that run on a subscription OR an API key, chosen per job ("sub:"/"key:" on the model).
PICK_ENGINES = frozenset({"claude_code", "claude_cli", "claude", "codex", "codex_cli"})


def engine_family(engine: Optional[str]) -> Optional[str]:
    return ENGINE_FAMILY.get(str(engine or "").strip().lower())


def model_family(model: Optional[str]) -> Optional[str]:
    """The family a model name belongs to, or None when the name does not say. A provider
    prefix ("anthropic/claude-…", "openai/gpt-…") is a gateway id and says nothing about the
    engine that must run it, so it is read from its last segment like any other name."""
    from ..claude_credential import split_pick

    name = str(split_pick(model)[1] or "").lower().rsplit("/", 1)[-1]
    if not name:
        return None
    for family, prefixes in _PREFIXES:
        if name.startswith(prefixes):
            return family
    return None


def fits(engine: Optional[str], model: Optional[str]) -> bool:
    """True when `engine` can run `model` — or when either side cannot tell (no model means the
    engine's own default; an unknown name or a multi-provider engine is allowed)."""
    from ..claude_credential import split_pick

    pick, _ = split_pick(model)
    want, have = engine_family(engine), model_family(model)
    if pick is not None and str(engine or "").strip().lower() not in PICK_ENGINES:
        return False  # a subscription/API-key choice belongs to an engine that has both
    return want is None or have is None or want == have


def fitted(engine: Optional[str], model: Optional[str], tier: Optional[str] = None
           ) -> tuple[Optional[str], Optional[str]]:
    """(model to run, why it changed or None). A model that belongs to another engine's family
    is replaced by `engine`'s own model for `tier` when it has one, else by the engine's
    default (None) — never sent as is."""
    if fits(engine, model):
        return model, None
    from .policy import tier_model

    replacement = tier_model(str(engine or "").lower(), tier) if tier else None
    if replacement is not None and not fits(engine, replacement):
        replacement = None
    why = (f"{model} is a {model_family(model)} model and cannot run on {engine}; "
           f"using {replacement or f'{engine} default'}")
    return replacement, why
