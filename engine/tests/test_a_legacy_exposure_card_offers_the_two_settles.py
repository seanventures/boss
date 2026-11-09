"""A legacy-exposure card offers settle-at-$0 and settle-at-the-estimate, and nothing else does.

The engine can already close these holds (`BudgetGovernor.resolve_legacy_exposure`). The card
still offered Approve, which records a go-ahead and starts nothing, so the hold never settled.
"""
from __future__ import annotations

import pytest

from boss_engine.services import decision_actions as da

LEGACY = (
    "reconcile_legacy_model_exposure",
    "reconcile_legacy_provider_exposure",
)


def _offers(action: str, status: str = "pending") -> list[dict]:
    return da.allowed_actions({
        "status": status,
        "default_action": "hold",
        "recommendation": {"action": action},
    })


@pytest.mark.parametrize("action", LEGACY)
def test_an_open_legacy_exposure_offers_exactly_the_two_settles(action):
    offers = _offers(action)
    assert [(a["action"], a.get("settle_at"), a["label"]) for a in offers] == [
        ("settle", "zero", "Settle at $0 (provider refused)"),
        ("settle", "estimate", "Settle at the estimate"),
    ]
    for offer in offers:
        da.require_allowed(
            {"status": "pending", "recommendation": {"action": action}},
            offer["action"],
        )


def test_another_decision_does_not_offer_settle():
    offers = _offers("provide_github_token")
    assert "settle" not in {a["action"] for a in offers}
    assert any(a["action"] == "approve" for a in offers)


def test_a_resolved_legacy_exposure_offers_nothing():
    assert _offers("reconcile_legacy_model_exposure", status="resolved") == []
    assert _offers("reconcile_legacy_provider_exposure", status="resolved") == []
