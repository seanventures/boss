"""RB-16 — the autonomous investigation engine: parallel candidate enumeration, confidence scoring
(corroboration = causation test), ranking + dedupe. Pure (no DB)."""
from apps.api.app.services.investigation import InvestigationEngine

ENG = InvestigationEngine()


def _sub(service, others, edges):
    """Build a subgraph dict: root service + `others` nodes + `edges` (from_key,to_key,kind)."""
    ids = {service: "n_root"}
    nodes = [{"id": "n_root", "node_key": service, "kind": "service", "name": service}]
    for key, kind in others:
        nid = f"n_{key}"
        ids[key] = nid
        nodes.append({"id": nid, "node_key": key, "kind": kind, "name": key})
    edge_objs = [{"from": ids[a], "to": ids[b], "kind": k, "dir": "out"} for a, b, k in edges]
    return {"root": nodes[0], "nodes": nodes, "edges": edge_objs}


def test_memory_candidate_outranks_context():
    sub = _sub("checkout", [("db", "datastore")], [("checkout", "db", "depends_on")])
    known = [{"kind": "deployment", "node_key": "deploy-9", "name": "deploy 9", "confidence": 0.92}]
    res = ENG.investigate(service="checkout", signals=[], known_causes=known, subgraph=sub)
    assert res["winner"]["basis"] == "memory" and res["winner"]["cause_key"] == "deploy-9"
    assert res["winner"]["confidence"] >= 0.9
    # both candidates present, ranked desc
    assert [h["confidence"] for h in res["hypotheses"]] == sorted(
        (h["confidence"] for h in res["hypotheses"]), reverse=True)
    assert res["candidate_count"] == 2


def test_closer_stronger_edge_scores_higher():
    # web --runs_on--> host (1 hop, strong) ; web --depends_on--> svc --owns--> lib (2 hops, weak)
    sub = _sub("web", [("host", "resource"), ("svc", "service"), ("lib", "repo")],
               [("web", "host", "runs_on"), ("web", "svc", "depends_on"), ("svc", "lib", "owns")])
    res = ENG.investigate(service="web", signals=[], known_causes=[], subgraph=sub)
    keys = [h["cause_key"] for h in res["hypotheses"]]
    assert keys[0] in ("host", "svc")                 # 1-hop suspects rank above the 2-hop lib
    assert keys.index("lib") == len(keys) - 1         # deepest/weakest last


def test_corroboration_bumps_confidence():
    sub = _sub("api", [("payments", "service")], [("api", "payments", "depends_on")])
    base = ENG.investigate(service="api", signals=[], known_causes=[], subgraph=sub)["winner"]["confidence"]
    # a signal that names the suspect -> corroborated -> higher confidence
    sigs = [{"signature": "payments timeout", "sample": "payments upstream 503", "service": "api"}]
    corro = ENG.investigate(service="api", signals=sigs, known_causes=[], subgraph=sub)["winner"]
    assert corro["corroborated"] is True and corro["confidence"] > base


def test_dedupe_keeps_highest_confidence():
    # same cause reachable as both a memory cause and a graph node -> one hypothesis, the stronger score
    sub = _sub("svc", [("deploy-x", "deployment")], [("svc", "deploy-x", "caused_by")])
    known = [{"kind": "deployment", "node_key": "deploy-x", "name": "deploy x", "confidence": 0.95}]
    res = ENG.investigate(service="svc", signals=[], known_causes=known, subgraph=sub)
    deploys = [h for h in res["hypotheses"] if h["cause_key"] == "deploy-x"]
    assert len(deploys) == 1 and deploys[0]["basis"] == "memory"


def test_no_context_yields_unknown():
    empty = {"root": None, "nodes": [], "edges": []}
    res = ENG.investigate(service="orphan", signals=[], known_causes=[], subgraph=empty)
    assert res["winner"]["root_cause"] == "unknown" and res["winner"]["confidence"] == 0.1


def test_change_candidate_included_and_scored():
    sub = _sub("svc", [], [])
    changes = [{"kind": "deployment", "key": "d-42", "name": "deploy 42",
                "rationale": "shipped 3m before onset", "evidence": {"pr": "#9"}}]
    res = ENG.investigate(service="svc", signals=[], known_causes=[], subgraph=sub, changes=changes)
    top = res["winner"]
    assert top["basis"] == "change" and top["cause_key"] == "d-42" and top["confidence"] > 0.5
