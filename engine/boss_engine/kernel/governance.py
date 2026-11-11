"""Every model call a kernel run makes is reserved, receipted and settled — by the kernel.

Today governance is applied per call site: agent_loop and step_recovery wrap their adapter in
`model_routing.governed`, the router's answer agent and meetings do not, NativeCodeAgent and
TypeSafeDecider build adapters that nothing governs (inventory §1.2). A run on the kernel cannot
forget: `RunSpec.gate` wraps the engine here, once, before the first call (design §2.3).

The gate is BOSS's existing one (GovernedChatAdapter's reserve / settle / release): the budget,
the receipt and the burst cap are decided by exactly the code that decides them now. A refusal
surfaces as ModelSpendRefused (the run stops: a person must lift the cap) and a burst deferral as
ModelSpendDeferred (the RecoveryPolicy waits or parks the run).
"""
from __future__ import annotations

from typing import Any, Callable, Optional, Protocol

from .engine import CancelToken, ModelRequest
from .types import ModelTurn


class BudgetGate(Protocol):
    def open(self) -> Any: ...

    def settle(self, held: Any, usage: dict) -> None: ...

    def fail(self, held: Any, error: BaseException) -> None: ...


class GovernedEngine:
    """`engine` with every `complete` reserved before and settled after (released on failure).
    Retries happen OUTSIDE it, so each attempt is its own reservation and receipt: a retried
    call is visible cost, not a hidden one."""

    def __init__(self, engine: Any, gate: BudgetGate):
        self.inner = engine
        self.gate = gate
        self.name = getattr(engine, "name", "engine")
        self.traits = getattr(engine, "traits", None)

    def complete(self, req: ModelRequest, *, on_delta: Optional[Callable[[str], None]] = None,
                 cancel: Optional[CancelToken] = None) -> ModelTurn:
        held = self.gate.open()
        try:
            turn = self.inner.complete(req, on_delta=on_delta, cancel=cancel)
        except BaseException as error:
            self.gate.fail(held, error)
            raise
        self.gate.settle(held, turn.usage.as_adapter_usage())
        return turn

    def count(self, req: ModelRequest) -> int:
        return self.inner.count(req)


class AdapterGate:
    """A GovernedChatAdapter (model_routing.governed) used as a gate only."""

    def __init__(self, governed_adapter: Any):
        self.governed = governed_adapter

    def open(self) -> Any:
        return self.governed.reserve_turn()

    def settle(self, held: Any, usage: dict) -> None:
        self.governed.settle_turn(held, usage)

    def fail(self, held: Any, error: BaseException) -> None:
        self.governed.release_turn(held, error)


def project_gate(connection: Any, settings: Any, project_id: str, *, skill_id: str, ref: str,
                 adapter: Any = None) -> AdapterGate:
    """The project's budget gate for a run: the same reservation size, category and receipts as
    `model_routing.governed(adapter, ...)`. `adapter` is the routed adapter the engine wraps (its
    source decides what a turn reserves)."""
    from ..services.model_routing import governed

    return AdapterGate(governed(adapter, connection, settings, project_id,
                                skill_id=skill_id, ref=ref))


__all__ = ["AdapterGate", "BudgetGate", "GovernedEngine", "project_gate"]
