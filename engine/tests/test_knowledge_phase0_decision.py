"""Phase 0 spike decision for the knowledge graph.

The break each test names is a wrong recommendation: inventing unmeasured
precision, adopting Graphiti when its model calls or tenancy are outside
BOSS, or treating an unfinished AGE benchmark as a reason to leave kg_edges.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from boss_engine.services.knowledge.phase0 import (
    SpikeEvidence,
    decide_phase0,
    recorded_phase0_decision,
    render_spike_markdown,
)

ROOT = Path(__file__).resolve().parents[2]
SPIKE_DOC = ROOT / "docs" / "product" / "KNOWLEDGE_GRAPH_SPIKE.md"


def _measured(**overrides) -> SpikeEvidence:
    base = dict(
        graphiti_has_age_pgvector_driver=True,
        graphiti_model_calls_through_gateway=True,
        tenancy_enforced_in_store=True,
        precision_adopt=0.9,
        precision_native=0.5,
        latency_ms_adopt=10.0,
        latency_ms_native=20.0,
        cost_usd_adopt=0.01,
        cost_usd_native=0.02,
        episodes=200,
        labelled_facts=50,
    )
    base.update(overrides)
    return SpikeEvidence(**base)


def test_unmeasured_constraints_recommend_native_without_inventing_numbers():
    record = decide_phase0(
        SpikeEvidence(
            graphiti_has_age_pgvector_driver=False,
            graphiti_model_calls_through_gateway=False,
            tenancy_enforced_in_store=False,
        )
    )
    assert record["recommendation"] == "native"
    assert record["code"] == "B"
    assert record["status"] == "provisional"
    assert record["basis"] == "governance"
    assert record["numbers_measured"] is False
    assert record["precision_adopt"] is None
    assert record["precision_native"] is None
    assert record["latency_ms_adopt"] is None
    assert record["latency_ms_native"] is None
    assert record["cost_usd_adopt"] is None
    assert record["cost_usd_native"] is None
    assert record["episodes"] is None
    assert record["labelled_facts"] is None
    assert record["blockers"] == [
        "no_age_pgvector_driver",
        "model_calls_not_through_gateway",
        "tenancy_not_enforced_in_store",
    ]
    assert record["traversal_sor"] == "kg_edges"
    assert record["traversal_projection"] == "recursive_cte"
    assert record["age_benchmark"] == "not_measured"
    assert record["age_p95_ms"] is None
    assert record["cte_p95_ms"] is None


def test_recorded_decision_is_that_unmeasured_native_recommendation():
    assert recorded_phase0_decision() == decide_phase0(
        SpikeEvidence(
            graphiti_has_age_pgvector_driver=False,
            graphiti_model_calls_through_gateway=False,
            tenancy_enforced_in_store=False,
        )
    )


def test_open_governance_without_measurements_stays_undecided():
    record = decide_phase0(
        SpikeEvidence(
            graphiti_has_age_pgvector_driver=True,
            graphiti_model_calls_through_gateway=True,
            tenancy_enforced_in_store=True,
        )
    )
    assert record["recommendation"] == "undecided"
    assert record["code"] is None
    assert record["status"] == "provisional"
    assert record["basis"] == "insufficient"
    assert record["numbers_measured"] is False
    assert record["blockers"] == ["spike_not_measured"]


def test_gateway_block_beats_a_better_adopt_measurement():
    record = decide_phase0(
        _measured(graphiti_model_calls_through_gateway=False)
    )
    assert record["recommendation"] == "native"
    assert record["code"] == "B"
    assert record["status"] == "decided"
    assert record["basis"] == "governance"
    assert record["numbers_measured"] is True
    assert record["precision_adopt"] == 0.9
    assert record["precision_native"] == 0.5
    assert record["blockers"] == ["model_calls_not_through_gateway"]


def test_missing_driver_beats_a_better_adopt_measurement():
    record = decide_phase0(_measured(graphiti_has_age_pgvector_driver=False))
    assert record["recommendation"] == "native"
    assert record["blockers"] == ["no_age_pgvector_driver"]
    assert record["basis"] == "governance"


def test_tenancy_block_beats_a_better_adopt_measurement():
    record = decide_phase0(_measured(tenancy_enforced_in_store=False))
    assert record["recommendation"] == "native"
    assert record["blockers"] == ["tenancy_not_enforced_in_store"]


def test_adopt_wins_only_when_governance_and_measurements_agree():
    record = decide_phase0(_measured())
    assert record["recommendation"] == "adopt"
    assert record["code"] == "A"
    assert record["status"] == "decided"
    assert record["basis"] == "measured"
    assert record["numbers_measured"] is True
    assert record["blockers"] == []
    assert record["episodes"] == 200
    assert record["labelled_facts"] == 50


def test_better_precision_at_a_higher_cost_stays_native():
    record = decide_phase0(
        _measured(precision_adopt=0.95, precision_native=0.5, cost_usd_adopt=0.08, cost_usd_native=0.01)
    )
    assert record["recommendation"] == "native"
    assert record["code"] == "B"
    assert record["basis"] == "measured"
    assert record["blockers"] == []


def test_equal_measurements_stay_with_native():
    record = decide_phase0(
        _measured(
            precision_adopt=0.5,
            precision_native=0.5,
            latency_ms_adopt=10.0,
            latency_ms_native=10.0,
            cost_usd_adopt=0.02,
            cost_usd_native=0.02,
        )
    )
    assert record["recommendation"] == "native"
    assert record["basis"] == "measured"


def test_partial_measurements_are_rejected():
    with pytest.raises(ValueError, match="both arms"):
        decide_phase0(_measured(precision_native=None))


def test_precision_above_one_is_rejected():
    with pytest.raises(ValueError, match="precision"):
        decide_phase0(_measured(precision_adopt=1.2))


def test_negative_cost_is_rejected():
    with pytest.raises(ValueError, match="cost"):
        decide_phase0(_measured(cost_usd_native=-0.01))


def test_wrong_spike_size_is_rejected():
    with pytest.raises(ValueError, match="200"):
        decide_phase0(_measured(episodes=199))


def test_age_projection_when_p95_is_not_worse_than_the_cte():
    record = decide_phase0(
        SpikeEvidence(
            graphiti_has_age_pgvector_driver=False,
            graphiti_model_calls_through_gateway=False,
            tenancy_enforced_in_store=False,
            age_p95_ms=40.0,
            cte_p95_ms=40.0,
        )
    )
    assert record["traversal_sor"] == "kg_edges"
    assert record["traversal_projection"] == "age"
    assert record["age_benchmark"] == "measured"
    assert record["age_p95_ms"] == 40.0
    assert record["cte_p95_ms"] == 40.0
    assert record["recommendation"] == "native"
    assert record["numbers_measured"] is False


def test_slower_age_keeps_the_recursive_cte():
    record = decide_phase0(
        SpikeEvidence(
            graphiti_has_age_pgvector_driver=False,
            graphiti_model_calls_through_gateway=False,
            tenancy_enforced_in_store=False,
            age_p95_ms=120.0,
            cte_p95_ms=30.0,
        )
    )
    assert record["traversal_projection"] == "recursive_cte"
    assert record["traversal_sor"] == "kg_edges"
    assert record["age_benchmark"] == "measured"


def test_one_sided_benchmark_is_rejected():
    with pytest.raises(ValueError, match="both"):
        decide_phase0(
            SpikeEvidence(
                graphiti_has_age_pgvector_driver=False,
                graphiti_model_calls_through_gateway=False,
                tenancy_enforced_in_store=False,
                age_p95_ms=10.0,
            )
        )


def test_spike_doc_is_the_rendered_recorded_decision():
    record = recorded_phase0_decision()
    text = SPIKE_DOC.read_text(encoding="utf-8")
    assert text == render_spike_markdown(record)
    assert "not measured" in text
    assert "provisional" in text
    assert "native" in text
