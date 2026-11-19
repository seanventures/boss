"""A refused model call is marked failed BEFORE its reservation is released (2026-11-18).

THE CASE (laptop, recipe-box-fd3ab4a3, dec_e77ed3f1). A Setup API-key proof got HTTP 401 from
Anthropic (no use, nothing billed). Settling it released the reservation first; release reads the
project's budget state, which asks legacy_model_reconciliation whether any RELEASED reservation
still has an invocation `in_progress` — and this very invocation still was, because its status
was written after. So a person got "Reconcile historical unresolved model exposure before paid
work" for a call that the same transaction then recorded as failed before billable use. Once the
transaction committed the hold no longer matched: the card was stale from birth.

THE RULE: the invocation's own status is written before the reservation is released, so the
budget read inside release sees the settled invocation.
"""
from __future__ import annotations

import inspect


def test_the_invocation_is_settled_before_its_reservation_is_released():
    from boss_engine.services.model_gateway import ModelGatewayService

    src = inspect.getsource(ModelGatewayService._record_settlement)
    marked = src.index("UPDATE model_gateway_invocations")
    released = src.index('"Model invocation failed before billable use."')
    assert marked < released, "the reservation is released only after the invocation says failed"
