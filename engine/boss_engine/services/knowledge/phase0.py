"""Phase 0 adopt-or-native decision. Numbers are recorded only when supplied.

Graphiti is not measured in this pass. A caller that omits an arm, or that
claims a spike other than 200 episodes and 50 labelled facts, is rejected
instead of being turned into a precision figure.
"""
from __future__ import annotations

from dataclasses import dataclass

REQUIRED_EPISODES = 200
REQUIRED_LABELLED_FACTS = 50

_METRIC_FIELDS = (
    "precision_adopt",
    "precision_native",
    "latency_ms_adopt",
    "latency_ms_native",
    "cost_usd_adopt",
    "cost_usd_native",
    "episodes",
    "labelled_facts",
)


@dataclass(frozen=True)
class SpikeEvidence:
    graphiti_has_age_pgvector_driver: bool
    graphiti_model_calls_through_gateway: bool
    tenancy_enforced_in_store: bool
    precision_adopt: float | None = None
    precision_native: float | None = None
    latency_ms_adopt: float | None = None
    latency_ms_native: float | None = None
    cost_usd_adopt: float | None = None
    cost_usd_native: float | None = None
    episodes: int | None = None
    labelled_facts: int | None = None
    age_p95_ms: float | None = None
    cte_p95_ms: float | None = None


def _governance_blockers(evidence: SpikeEvidence) -> list[str]:
    blockers: list[str] = []
    if not evidence.graphiti_has_age_pgvector_driver:
        blockers.append("no_age_pgvector_driver")
    if not evidence.graphiti_model_calls_through_gateway:
        blockers.append("model_calls_not_through_gateway")
    if not evidence.tenancy_enforced_in_store:
        blockers.append("tenancy_not_enforced_in_store")
    return blockers


def _traversal(evidence: SpikeEvidence) -> tuple[str, str, float | None, float | None]:
    age, cte = evidence.age_p95_ms, evidence.cte_p95_ms
    if age is None and cte is None:
        return "recursive_cte", "not_measured", None, None
    if age is None or cte is None:
        raise ValueError("AGE benchmark must include both p95 numbers")
    if age < 0 or cte < 0:
        raise ValueError("p95 must be >= 0")
    projection = "age" if age <= cte else "recursive_cte"
    return projection, "measured", age, cte


def _metrics(evidence: SpikeEvidence) -> bool:
    values = [getattr(evidence, name) for name in _METRIC_FIELDS]
    if all(value is None for value in values):
        return False
    if any(value is None for value in values):
        raise ValueError(
            "spike measurements must include both arms, episodes, and labelled facts"
        )
    for value in (evidence.precision_adopt, evidence.precision_native):
        if value < 0 or value > 1:
            raise ValueError("precision must be between 0 and 1")
    for value in (evidence.latency_ms_adopt, evidence.latency_ms_native):
        if value < 0:
            raise ValueError("latency must be >= 0")
    for value in (evidence.cost_usd_adopt, evidence.cost_usd_native):
        if value < 0:
            raise ValueError("cost must be >= 0")
    if evidence.episodes != REQUIRED_EPISODES:
        raise ValueError(f"spike episodes must be {REQUIRED_EPISODES}")
    if evidence.labelled_facts != REQUIRED_LABELLED_FACTS:
        raise ValueError(f"labelled facts must be {REQUIRED_LABELLED_FACTS}")
    return True


def _blank_numbers() -> dict:
    return {
        "precision_adopt": None,
        "precision_native": None,
        "latency_ms_adopt": None,
        "latency_ms_native": None,
        "cost_usd_adopt": None,
        "cost_usd_native": None,
        "episodes": None,
        "labelled_facts": None,
    }


def _copied_numbers(evidence: SpikeEvidence) -> dict:
    return {name: getattr(evidence, name) for name in _METRIC_FIELDS}


def decide_phase0(evidence: SpikeEvidence) -> dict:
    """Pick adopt (A) or native (B). Governance blocks outrank a better score."""
    projection, benchmark, age, cte = _traversal(evidence)
    measured = _metrics(evidence)
    blockers = _governance_blockers(evidence)
    record = {
        "traversal_sor": "kg_edges",
        "traversal_projection": projection,
        "age_benchmark": benchmark,
        "age_p95_ms": age,
        "cte_p95_ms": cte,
    }
    if not measured:
        if blockers:
            return {
                **record,
                "recommendation": "native",
                "code": "B",
                "status": "provisional",
                "basis": "governance",
                "numbers_measured": False,
                "blockers": blockers,
                **_blank_numbers(),
            }
        return {
            **record,
            "recommendation": "undecided",
            "code": None,
            "status": "provisional",
            "basis": "insufficient",
            "numbers_measured": False,
            "blockers": ["spike_not_measured"],
            **_blank_numbers(),
        }

    adopt_wins = (
        evidence.precision_adopt > evidence.precision_native
        and evidence.cost_usd_adopt <= evidence.cost_usd_native
        and evidence.latency_ms_adopt <= evidence.latency_ms_native
    )
    if blockers or not adopt_wins:
        recommendation, code = "native", "B"
        basis = "governance" if blockers else "measured"
    else:
        recommendation, code, basis = "adopt", "A", "measured"
    return {
        **record,
        "recommendation": recommendation,
        "code": code,
        "status": "decided",
        "basis": basis,
        "numbers_measured": True,
        "blockers": blockers,
        **_copied_numbers(evidence),
    }


def recorded_phase0_decision() -> dict:
    """The decision this pass actually records: Graphiti is not adoptable yet.

    No extraction numbers and no AGE timings were produced. Native is provisional,
    kg_edges stays the system of record, and traversal stays a recursive CTE.
    """
    return decide_phase0(
        SpikeEvidence(
            graphiti_has_age_pgvector_driver=False,
            graphiti_model_calls_through_gateway=False,
            tenancy_enforced_in_store=False,
        )
    )


def render_spike_markdown(record: dict) -> str:
    code = record["code"] if record["code"] else "none"
    blockers = ", ".join(record["blockers"]) if record["blockers"] else "none"
    lines = [
        "# Knowledge graph Phase 0 spike record",
        "",
        "BOSSAPP-622. This is the decision record for the open Graphiti question.",
        "It is not a claim that the 200-episode extraction was run.",
        "",
        f"Status: {record['status']}",
        f"Recommendation: {record['recommendation']} ({code})",
        f"Basis: {record['basis']}",
        f"Blockers: {blockers}",
        "",
        f"Traversal system of record: {record['traversal_sor']}",
        f"Traversal projection: {record['traversal_projection']}",
        f"AGE benchmark: {record['age_benchmark']}",
        "",
    ]
    if record["numbers_measured"]:
        lines.append(
            "Measured precision adopt/native: "
            f"{record['precision_adopt']} / {record['precision_native']}"
        )
        lines.append(
            "Measured latency_ms adopt/native: "
            f"{record['latency_ms_adopt']} / {record['latency_ms_native']}"
        )
        lines.append(
            "Measured cost_usd adopt/native: "
            f"{record['cost_usd_adopt']} / {record['cost_usd_native']}"
        )
    else:
        lines.append("Extraction precision, latency, and cost were not measured.")
    lines.extend(
        [
            "",
            "A one-row Alpine to Debian dump/restore passed, and CREATE EXTENSION vector",
            "succeeded on boss-postgres-kg:16. Apache AGE was not installed. Not run:",
            "the 200-episode extraction, the 1M-node traversal benchmark, and the",
            "case-study smoke stage. The harness refuses to spend. No model credit was",
            "spent. HNSW is not created yet. Apache AGE is not the system of record;",
            "kg_edges is.",
            "",
        ]
    )
    return "\n".join(lines)
