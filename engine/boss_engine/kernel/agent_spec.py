"""THE NARROW ADAPTER FROM A PUBLISHED AGENT VERSION TO A KERNEL RUN (AP-P1b ↔ AP-P1a).

AP-P1a owns `boss.agent/v1` (engine/boss_engine/agents/spec.py: `AgentSpec`, `compile_spec()`
→ `CompiledAgent`). The kernel reads a compiled agent through THIS module only, and only the
fields below, so the two can move independently: a field P1a adds is invisible here until the
kernel has a use for it, and nothing here imports P1a (a Protocol, not the class).

What a compiled agent changes about a run:

  * its LIMITS tighten the preset — never loosen it: `max_steps`, `run_usd` and `max_retries`
    each take the smaller of the preset's cap and the agent's; `timeout_seconds` becomes the
    run's CancelToken deadline;
  * the run is named for the agent and the version it ran (`actor`, `extra["agent_spec"]` —
    the content hash, so a ledger row says which definition produced it).

What it can NOT change is governance: `CompiledAgent.governance` names the implicit layers
(authz, approval, budget, taint, ledger), and `check_governance` holds each one to a kernel
middleware that `build_chain` appends to every stack — a spec has no field that removes one.
"""
from __future__ import annotations

import dataclasses
import time
from typing import Any, Callable, Optional, Protocol

from .engine import CancelToken
from .governance import GOVERNANCE_NAMES
from .policies import Policies

#: The spec's names for its implicit governance → the kernel middleware that IS each one.
SPEC_GOVERNANCE: dict = {
    "authz": "authz",          # the permission/identity gate
    "approval": "authz",       # a hold is a Decision bound to its arguments
    "budget": "budget",        # the gate reserved and settled per attempt
    "taint": "tool_screen",    # tool-result screening and low_trust
    "ledger": "telemetry",     # receipts: model.request/response → llm_calls, run_events
}


class LimitsLike(Protocol):
    max_steps: Optional[int]
    max_retries: Optional[int]
    timeout_seconds: Optional[int]
    run_usd: Optional[float]


class CompiledAgentLike(Protocol):
    """The fields of P1a's CompiledAgent this kernel reads."""

    key: str
    content_hash: str
    limits: Any
    governance: tuple


class SpecGovernanceError(ValueError):
    """A compiled agent names governance the kernel does not provide (the two have drifted)."""


def check_governance(compiled: Any) -> list:
    """The kernel middleware covering each governance layer the agent names. Raises when a
    layer has no middleware — the drift test between P1a's list and the kernel's."""
    covered: list = []
    for layer in tuple(getattr(compiled, "governance", ()) or ()):
        middleware = SPEC_GOVERNANCE.get(str(layer))
        if middleware is None or middleware not in GOVERNANCE_NAMES:
            raise SpecGovernanceError(f"agent {getattr(compiled, 'key', '?')!r} names "
                                      f"governance {layer!r}, which no kernel middleware is")
        covered.append(middleware)
    return covered


def _tighter(current: Optional[int], wanted: Optional[int]) -> Optional[int]:
    if wanted is None or int(wanted) <= 0:
        return current
    return int(wanted) if current is None else min(int(current), int(wanted))


def policies_for(policies: Policies, compiled: Any) -> Policies:
    """The preset's policies with the agent's limits applied — each one only ever tighter."""
    limits = getattr(compiled, "limits", None)
    if limits is None:
        return policies
    stop, recovery = policies.stop, policies.recovery
    changes: dict = {}
    steps = _tighter(getattr(stop, "max_steps", None), getattr(limits, "max_steps", None))
    if steps is not None and steps != getattr(stop, "max_steps", None):
        changes["max_steps"] = steps
    run_usd = getattr(limits, "run_usd", None)
    if run_usd is not None and float(run_usd) > 0:
        micros = _tighter(getattr(stop, "max_usd_micros", None), int(float(run_usd) * 1_000_000))
        if micros != getattr(stop, "max_usd_micros", None):
            changes["max_usd_micros"] = micros
    if changes and dataclasses.is_dataclass(stop):
        stop = dataclasses.replace(stop, **changes)
    retries = getattr(limits, "max_retries", None)
    if retries is not None and dataclasses.is_dataclass(recovery) \
            and hasattr(recovery, "transient_tries"):
        tries = _tighter(recovery.transient_tries, int(retries) + 1)
        if tries != recovery.transient_tries:
            recovery = dataclasses.replace(recovery, transient_tries=tries)
    return dataclasses.replace(policies, stop=stop, recovery=recovery)


def run_fields(compiled: Any, *, clock: Callable[[], float] = time.monotonic) -> dict:
    """RunSpec fields for a run of this agent version: who it is, which definition, and the
    agent's time limit as the run's cancel deadline."""
    check_governance(compiled)
    fields: dict = {"actor": f"agent:{compiled.key}",
                    "extra": {"agent_spec": {"key": compiled.key,
                                             "content_hash": compiled.content_hash}}}
    timeout = getattr(getattr(compiled, "limits", None), "timeout_seconds", None)
    if timeout is not None and int(timeout) > 0:
        fields["cancel"] = CancelToken(deadline=clock() + int(timeout), clock=clock)
    return fields


__all__ = ["CompiledAgentLike", "SPEC_GOVERNANCE", "SpecGovernanceError", "check_governance",
           "policies_for", "run_fields"]
