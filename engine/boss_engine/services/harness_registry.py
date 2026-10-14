"""The harness seam: which coding agent BOSS drives, declared rather than hardcoded.

BOSS's bet is "rent the model, own the orchestration". That only holds if the thing being
rented is a **declared, swappable part**. Until now `_build_code_agent` was an ``if/elif``
chain, so adding a harness meant editing core — which is the opposite of the claim.

This module is the registry. A harness is a name plus a builder plus enough metadata for an
operator to see what they are choosing:

    register_harness(
        "my_cli",
        build=lambda settings: MyAgent(settings),
        kind="cli",
        description="…",
        requires=("MY_CLI_TOKEN",),
        verified=False,
    )

Nothing here imports the agent classes: `code_agent` registers the built-ins at import
time, so the registry stays a leaf module and there is no cycle.

Honesty rule: `verified` means *BOSS has actually run a governed code task end to end on
this harness*. A harness that is merely wired is `verified=False` and says so wherever it
is listed. A registry that presents an untested adapter as ready is worse than no registry.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, Optional

#: Builder signature: (settings) -> CodeAgent
HarnessBuilder = Callable[[object], object]


@dataclass(frozen=True)
class HarnessSpec:
    name: str
    build: HarnessBuilder
    #: "native" drives a chat API in-process; "cli" shells out to an external agent binary.
    kind: str = "native"
    description: str = ""
    #: Settings/env names an operator must supply. Listed, not enforced here — the builder
    #: fails closed with its own message, which can be more specific than a name list.
    requires: tuple[str, ...] = ()
    #: Has a governed code task actually completed on this harness?
    verified: bool = False
    aliases: tuple[str, ...] = ()


_REGISTRY: dict[str, HarnessSpec] = {}
_ALIASES: dict[str, str] = {}


class UnknownHarness(NotImplementedError):
    """Raised for a harness name nothing has registered.

    Subclasses NotImplementedError so the historical `except NotImplementedError` behaviour
    of callers is unchanged.
    """


def register_harness(
    name: str,
    build: HarnessBuilder,
    *,
    kind: str = "native",
    description: str = "",
    requires: tuple[str, ...] = (),
    verified: bool = False,
    aliases: tuple[str, ...] = (),
    replace: bool = False,
) -> HarnessSpec:
    key = (name or "").strip().lower()
    if not key:
        raise ValueError("harness name is required")
    if key in _REGISTRY and not replace:
        raise ValueError(
            f"harness {key!r} is already registered. Pass replace=True to override it "
            "deliberately — a silent overwrite would make which agent ran unknowable."
        )
    spec = HarnessSpec(name=key, build=build, kind=kind, description=description,
                       requires=tuple(requires), verified=verified,
                       aliases=tuple(a.lower() for a in aliases))
    _REGISTRY[key] = spec
    for alias in spec.aliases:
        _ALIASES[alias] = key
    return spec


def _ensure_builtins() -> None:
    """Import the module that registers the built-in harnesses, once.

    Registration lives in `code_agent` (it owns the agent classes), so a caller that only
    imports this module would otherwise see an EMPTY registry and conclude BOSS supports no
    harnesses. The import is deferred to call time, so this module stays a leaf and there is
    no cycle at import.
    """
    if _REGISTRY:
        return
    from . import code_agent  # noqa: F401  — imported for its registration side effect


def resolve_name(name: Optional[str], default: str = "claude_code") -> str:
    key = (name or default or "claude_code").strip().lower()
    return _ALIASES.get(key, key)


def get_harness(name: Optional[str]) -> HarnessSpec:
    _ensure_builtins()
    key = resolve_name(name)
    spec = _REGISTRY.get(key)
    if spec is None:
        known = ", ".join(sorted(_REGISTRY)) or "(none registered)"
        raise UnknownHarness(
            f"Unknown code_agent_engine: {key!r}. Registered harnesses: {known}."
        )
    return spec


def build_harness(name: Optional[str], settings) -> object:
    return get_harness(name).build(settings)


def available_harnesses() -> list[dict]:
    """Every registered harness, for the CLI, MCP and console to show an operator."""
    _ensure_builtins()
    import os

    # The conformance engine ("scripted", kind "conformance") plays no model: it is listed only
    # where the conformance gate turned it on, never offered to an operator.
    shown = [s for s in _REGISTRY.values()
             if s.kind != "conformance"
             or (os.environ.get("BOSS_SCRIPTED_ENGINE") or "").strip().lower() in ("1", "true", "yes")]
    return [
        {
            "name": s.name,
            "kind": s.kind,
            "description": s.description,
            "requires": list(s.requires),
            "verified": s.verified,
            "aliases": list(s.aliases),
        }
        for s in sorted(shown, key=lambda x: x.name)
    ]


def clear_registry_for_tests() -> None:
    _REGISTRY.clear()
    _ALIASES.clear()
