"""RB-14 — meta-eval: measure the measurer.

Runs the SRE agent's own detection + RCA against a **labeled synthetic corpus** and reports
precision/recall/F1 (detection) and top-1 accuracy (RCA ranking). Pure evaluation — mine() is
read-only and the RCA leg drives `InvestigationEngine.investigate` directly with synthetic inputs,
so a meta-eval never pollutes incidents. This is the number that tells us whether a detector or
engine change made the agent better or worse — wire it into CI like any other eval.
"""
from __future__ import annotations

from typing import Any

from . import telemetry_mining as tm
from .investigation import InvestigationEngine
from .telemetry_source import LogEvent

# Each scenario: benign noise + injected real anomalies (labeled by a substring of the raw line).
# A detector should flag every `injected` signature and nothing from the noise.
DETECTION_CORPUS: list[dict[str, Any]] = [
    {
        "name": "disk_full_burst",
        "noise": [("GET /health 200 in {i}ms", "web", "info", 120)],
        "injected": [("No space left on device (errno 28) at /var/lib/{i}", "api", "error", 40)],
    },
    {
        "name": "db_refused_low_volume",
        "noise": [("served page {i}", "web", "info", 200), ("cache miss key {i}", "web", "info", 80)],
        "injected": [("db connection refused attempt {i}", "orders", "error", 8)],
    },
    {
        "name": "quiet_system_no_anomaly",
        "noise": [("heartbeat ok {i}", "cron", "info", 150)],
        "injected": [],
    },
    {
        "name": "two_failures_at_once",
        "noise": [("request done in {i}ms", "gw", "info", 100)],
        "injected": [("upstream timeout contacting payments {i}", "checkout", "error", 12),
                     ("OOMKilled container restart {i}", "worker", "error", 6)],
    },
]

# RCA scenarios: a service with graph context + memory; the engine should rank `expected` first.
RCA_CORPUS: list[dict[str, Any]] = [
    {
        "name": "recurring_cause_recalled",
        "service": "checkout",
        "known_causes": [{"kind": "deployment", "node_key": "deploy-1", "name": "deploy 1",
                          "confidence": 0.92}],
        "subgraph_nodes": [("db-main", "datastore", "depends_on")],
        "signals": [],
        "expected": "deploy-1",
    },
    {
        "name": "fresh_change_beats_context",
        "service": "orders",
        "known_causes": [],
        "subgraph_nodes": [("cache-1", "datastore", "depends_on")],
        "changes": [{"kind": "deployment", "key": "pr-9", "name": "PR #9",
                     "evidence": {"proximity_minutes": 2}}],
        "signals": [],
        "expected": "pr-9",
    },
    {
        "name": "corroborated_suspect_wins",
        "service": "api",
        "known_causes": [],
        "subgraph_nodes": [("payments", "service", "depends_on"),
                           ("static-assets", "external", "depends_on")],
        "signals": [{"signature": "payments timeout", "sample": "payments 503", "service": "api"}],
        "expected": "payments",
    },
]


def _mk_events(spec: list[tuple]) -> list[LogEvent]:
    out = []
    for template, service, level, n in spec:
        for i in range(n):
            out.append(LogEvent(ts=float(i), message=template.replace("{i}", str(i)),
                                service=service, level=level))
    return out


def _subgraph(service: str, nodes: list[tuple]) -> dict[str, Any]:
    root = {"id": "n_root", "node_key": service, "kind": "service", "name": service}
    ns, es = [root], []
    for key, kind, edge in nodes:
        nid = f"n_{key}"
        ns.append({"id": nid, "node_key": key, "kind": kind, "name": key})
        es.append({"from": "n_root", "to": nid, "kind": edge, "dir": "out"})
    return {"root": root, "nodes": ns, "edges": es}


def run_meta_eval() -> dict[str, Any]:
    """The full self-eval: detection precision/recall/F1 + RCA top-1 accuracy, per scenario."""
    tp = fp = fn = 0
    per_scenario: list[dict[str, Any]] = []
    for sc in DETECTION_CORPUS:
        events = _mk_events(sc["noise"] + sc["injected"])
        report = tm.mine(events)
        flagged = [s for s in report["signals"]]
        expected_marks = [tm.template_of(t.replace("{i}", "0")) for t, *_ in sc["injected"]]
        hits = set()
        s_fp = 0
        for s in flagged:
            key = s["key"]
            match = next((m for m in expected_marks if m == key or key in m or m in key), None)
            if match:
                hits.add(match)
            elif s["kind"] != "error_surge":       # surge is a correlate of injected errors, not a FP
                s_fp += 1
        s_tp, s_fn = len(hits), len(expected_marks) - len(hits)
        tp, fp, fn = tp + s_tp, fp + s_fp, fn + s_fn
        per_scenario.append({"scenario": sc["name"], "tp": s_tp, "fp": s_fp, "fn": s_fn,
                             "reduction_ratio": report["reduction_ratio"]})

    precision = tp / (tp + fp) if (tp + fp) else 1.0
    recall = tp / (tp + fn) if (tp + fn) else 1.0
    f1 = (2 * precision * recall / (precision + recall)) if (precision + recall) else 0.0

    engine = InvestigationEngine()
    rca_hits = 0
    for sc in RCA_CORPUS:
        res = engine.investigate(
            service=sc["service"], signals=sc.get("signals", []),
            known_causes=sc.get("known_causes", []),
            subgraph=_subgraph(sc["service"], sc.get("subgraph_nodes", [])),
            changes=sc.get("changes", []))
        ok = res["winner"]["cause_key"] == sc["expected"]
        rca_hits += ok
        per_scenario.append({"scenario": sc["name"], "rca_expected": sc["expected"],
                             "rca_got": res["winner"]["cause_key"], "ok": ok})

    return {
        "detection": {"precision": round(precision, 3), "recall": round(recall, 3),
                      "f1": round(f1, 3), "tp": tp, "fp": fp, "fn": fn},
        "rca": {"top1_accuracy": round(rca_hits / len(RCA_CORPUS), 3), "cases": len(RCA_CORPUS)},
        "per_scenario": per_scenario,
    }
