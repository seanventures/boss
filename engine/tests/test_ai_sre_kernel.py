"""AI SRE ledger stays honest: missing stays missing."""
from boss_engine.services.ai_sre import (
    AWESOME_AI_SRE,
    capability_by_id,
    capability_ledger,
)


def test_ledger_covers_awesome_categories_without_inventing_shipped():
    ledger = capability_ledger()
    assert ledger["source"] == AWESOME_AI_SRE
    assert ledger["public_ga"] is False
    ids = {item["id"] for item in ledger["capabilities"]}
    assert ids >= {
        "sre_agents",
        "incident_management",
        "observability",
        "aiops",
        "chaos",
        "runbooks",
        "sre_bench",
    }
    assert ledger["counts"]["missing"] >= 1
    assert all(item["status"] in {"shipped", "partial", "missing"} for item in ledger["capabilities"])
    assert capability_by_id("runbooks")["status"] == "missing"
    assert capability_by_id("nope") is None
