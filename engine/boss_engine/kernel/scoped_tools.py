"""SCOPED TOOL EXPOSURE for kernel runs (K5, BOSSAPP-961; HARNESS_KERNEL_DESIGN.md §5).

Today a chat call carries every registry schema (~175) on every call — the cost that broke the
JSON-in-text protocol above ~53 tools and pays prompt tokens on every turn. With
`BOSS_SCOPED_TOOLS` on, a kernel run is offered only its surface's CORE set (`Exposure.core_for`,
chat ≤20) plus `find_tools`; every other surface-eligible tool is DEFERRED (`ToolSchema.deferred`,
marked by RegistryToolSet from each ToolSpec's `Exposure`) and joins the run's tools from the
step after `find_tools` returns it. With the flag off, nothing is deferred and `ScopedTools`
offers exactly what `OfferAll` would: today's full set.

ONLY the kernel reads this policy. The old loops (EngineAgent, agent_loop, the pre-check, the
decision read phase) are untouched: chat starts seeing the scoped set when K6 moves it onto the
kernel, and a decision's read tools stay the caller's ReadPhase either way.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Mapping

from .state import RunState
from .types import StopReason

FLAG = "BOSS_SCOPED_TOOLS"

#: The registry's tool-search tool (tools/builtin.register_find_tools): core on every surface.
FIND_TOOLS = "find_tools"


def enabled() -> bool:
    """BOSS_SCOPED_TOOLS: off unless set to a true word. Off = today's full set."""
    return (os.environ.get(FLAG) or "").strip().lower() in ("1", "true", "yes", "on")


def found_names(state: RunState) -> set:
    """Every tool name a `find_tools` call of THIS run returned so far — read from the run's own
    trace, so a resumed run keeps what it found (the trace rides in the checkpoint) and the
    cache-stable rule of §4.3 holds: found tools are only ever APPENDED to the offered list."""
    out: set = set()
    for entry in state.trace:
        if entry.get("tool") != FIND_TOOLS:
            continue
        result = entry.get("result")
        if isinstance(result, Mapping):
            out.update(str(name) for name in (result.get("names") or []))
    return out


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


__all__ = ["FIND_TOOLS", "FLAG", "ScopedTools", "enabled", "found_names"]
