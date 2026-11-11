"""SCOPED TOOL EXPOSURE for kernel runs (K5, BOSSAPP-961; HARNESS_KERNEL_DESIGN.md §5).

Today a chat call carries every registry schema (~175) on every call — the cost that broke the
JSON-in-text protocol above ~53 tools and pays prompt tokens on every turn. With
`BOSS_SCOPED_TOOLS` on, a kernel run is offered only its surface's CORE set (`Exposure.core_for`,
chat ≤20) plus `find_tools`; every other surface-eligible tool is DEFERRED (`ToolSchema.deferred`,
marked by RegistryToolSet from each ToolSpec's `Exposure`) and joins the run's tools from the
step after `find_tools` returns it. With the flag off, nothing is deferred and `ScopedTools`
offers exactly what `OfferAll` would: today's full set.

The kernel reads this policy through `ScopedTools`; since BOSSAPP-1006 the OLD chat loop
(EngineAgent on a CLI text brain, which the kernel cannot serve — engine_agent._CLI_TEXT_BRAINS)
scopes with the SAME derivation (`surface_view` + `found_in_trace`), under `cli_text_default`
below: a CLI text protocol re-renders every schema into every prompt, so there the scoped set is
the default rather than opt-in. agent_loop, the pre-check and the decision read phase are
untouched.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any, Iterable, Mapping, Optional

from .state import RunState
from .types import StopReason

FLAG = "BOSS_SCOPED_TOOLS"

#: The registry's tool-search tool (tools/builtin.register_find_tools): core on every surface.
FIND_TOOLS = "find_tools"

#: The explicit "off" words. Anything else set is on; what UNSET means depends on the caller
#: (`enabled` says off, `cli_text_default` says on — see each).
_OFF_WORDS = ("0", "false", "no", "off")


def enabled() -> bool:
    """BOSS_SCOPED_TOOLS: off unless set to a true word. Off = today's full set."""
    return (os.environ.get(FLAG) or "").strip().lower() in ("1", "true", "yes", "on")


def cli_text_default() -> bool:
    """Scoped tools for the CLI TEXT-PROTOCOL chat loop (BOSSAPP-1006): ON unless
    BOSS_SCOPED_TOOLS is explicitly off.

    The kernel's `enabled()` stays opt-in — a native engine sends schemas once per call in the
    provider's own format, where the full set is survivable. The JSON-in-text CLI loop re-renders
    EVERY schema into EVERY prompt (~37k tokens a call, audit BOSSAPP-997 §1a) and its protocol
    demonstrably degrades past ~53 tools (claude_cli_adapter._build_prompt's own history), so
    there the scoped set is the default and the flag's off words are the escape hatch."""
    return (os.environ.get(FLAG) or "").strip().lower() not in _OFF_WORDS


def found_in_trace(trace: Iterable[Mapping]) -> set:
    """Every tool name a `find_tools` call in `trace` returned — the one reading of a trace both
    loops use (the kernel's RunState.trace and EngineAgent's own list carry the same
    tool/result rows)."""
    out: set = set()
    for entry in trace:
        if entry.get("tool") != FIND_TOOLS:
            continue
        result = entry.get("result")
        if isinstance(result, Mapping):
            out.update(str(name) for name in (result.get("names") or []))
    return out


def found_names(state: RunState) -> set:
    """Every tool name a `find_tools` call of THIS run returned so far — read from the run's own
    trace, so a resumed run keeps what it found (the trace rides in the checkpoint) and the
    cache-stable rule of §4.3 holds: found tools are only ever APPENDED to the offered list."""
    return found_in_trace(state.trace)


def surface_view(registry: Any, surface: str) -> Optional[dict]:
    """Each registry tool's standing on `surface` — "core", "deferred" or "hidden" — from its
    ToolSpec's `Exposure`; None when the registry cannot say (a test double with only
    `openai_schema()`), which means nothing is scoped.

    THE ONE DERIVATION. RegistryToolSet (the kernel) and EngineAgent's CLI loop both read this,
    so there is exactly one answer to "what is chat's core set" — a second list would drift. A
    spec with no exposure is core: conservative, nothing a surface has today is lost."""
    specs = getattr(registry, "specs", None)
    if not callable(specs):
        return None
    view: dict = {}
    for spec in specs():
        exposure = getattr(spec, "exposure", None)
        if exposure is None:
            view[spec.name] = "core"
        elif surface not in exposure.surfaces:
            view[spec.name] = "hidden"
        elif surface in exposure.core_for:
            view[spec.name] = "core"
        else:
            view[spec.name] = "deferred"
    return view


@dataclass(frozen=True)
class ScopedTools:
    """The ToolPolicy of design §5: core ∪ found, plus the search tool, when the flag is on;
    everything when it is off. `answer_offer` and `max_parallel_calls` behave exactly as
    policies.OfferAll — a budget-forced answer may still attach its one offer button."""

    answer_offer: tuple = ()
    parallel: int = 8
    version: str = "tools.scoped@1"

    def select(self, state: RunState, available: list) -> list:
        if not enabled():
            return list(available)
        found = found_names(state)
        return [t for t in available if not t.deferred or t.name in found]

    def answer_tools(self, state: RunState, reason: StopReason, available: list) -> list:
        if reason.why not in ("budget", "deadline") or not self.answer_offer:
            return []
        if state.extra.get("_offer_spent"):
            return []
        return [t for t in available if t.name in self.answer_offer]

    def max_parallel_calls(self) -> int:
        return self.parallel


__all__ = ["FIND_TOOLS", "FLAG", "ScopedTools", "cli_text_default", "enabled", "found_in_trace",
           "found_names", "surface_view"]
