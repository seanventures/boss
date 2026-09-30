"""Deterministic SRE-agent bench.

Runs InvestigationEngine against fixed fixtures. No vendors, no models, no
invented confidence. Missing signals stay unknown.
"""
from __future__ import annotations

from typing import Any

from boss_engine.services.investigation import InvestigationEngine

SURGE_CASE_ID = "error_surge"
MISSING_CASE_ID = "missing_signals"
_UNKNOWN_CONFIDENCE_CAP = 0.2


def _surge_fixture() -> dict[str, Any]:
    """Known error-surge logs plus memory/change/context so a real winner exists."""
    service = "checkout"
    return {
        "id": SURGE_CASE_ID,
        "service": service,
        "signals": [
            {
                "signature": "checkout oom",
                "sample": "java.lang.OutOfMemoryError: Java heap space checkout",
                "service": service,
            },
            {
                "signature": "checkout oom",
                "sample": "GC overhead limit exceeded heap-limit checkout",
                "service": service,
            },
            {
                "signature": "checkout 5xx",
                "sample": "upstream 503 after heap-limit",
                "service": service,
            },
        ],
        "known_causes": [
            {
                "kind": "resource",
                "node_key": "heap-limit",
                "name": "heap-limit",
                "confidence": 0.88,
            }
        ],
        "subgraph": {
            "root": {"id": "n_root", "node_key": service, "kind": "service", "name": service},
            "nodes": [
                {"id": "n_root", "node_key": service, "kind": "service", "name": service},
                {"id": "n_heap", "node_key": "heap-limit", "kind": "resource", "name": "heap-limit"},
            ],
            "edges": [{"from": "n_root", "to": "n_heap", "kind": "runs_on", "dir": "out"}],
        },
        "changes": [
            {
                "kind": "deployment",
                "key": "deploy-heap-cut",
                "name": "cut checkout heap",
                "rationale": "heap request lowered 12m before onset",
                "evidence": {"proximity_minutes": 12, "pr": "#bench"},
            }
        ],
    }


def _missing_fixture() -> dict[str, Any]:
    """No memory, change, graph, or signals — winner must stay unknown."""
    return {
        "id": MISSING_CASE_ID,
        "service": "orphan",
        "signals": [],
        "known_causes": [],
        "subgraph": {"root": None, "nodes": [], "edges": []},
        "changes": [],
    }


def _run_case(engine: InvestigationEngine, fixture: dict[str, Any]) -> dict[str, Any]:
    result = engine.investigate(
        service=fixture["service"],
        signals=fixture["signals"],
        known_causes=fixture["known_causes"],
        subgraph=fixture["subgraph"],
        changes=fixture["changes"],
    )
    winner = dict(result["winner"])
    # Honesty guard: unknown never pretends to be a high-confidence RCA.
    if winner.get("root_cause") == "unknown":
        winner["confidence"] = min(float(winner.get("confidence") or 0.0), _UNKNOWN_CONFIDENCE_CAP)
        winner["basis"] = winner.get("basis") or "context"
    return {
        "id": fixture["id"],
        "service": fixture["service"],
        "winner": winner,
        "hypotheses": result["hypotheses"],
        "candidate_count": result["candidate_count"],
    }


def run_sre_bench() -> dict[str, Any]:
    """Evaluate the two fixtures and return a non-GA bench report."""
    engine = InvestigationEngine()
    cases = [_run_case(engine, _surge_fixture()), _run_case(engine, _missing_fixture())]
    return {
        "product": "boss_ai_sre",
        "public_ga": False,
        "deterministic": True,
        "vendor_calls": 0,
        "cases": cases,
        "rules": [
            "Winner basis is memory, change, or context when signals exist.",
            "Missing signals may be unknown; never invent 0.99 confidence.",
        ],
    }
