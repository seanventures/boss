"""RB-16 — the autonomous investigation engine (spec 12 §1.5, Cleric-parity).

Upgrades `diagnose` from a single-suspect guess into a **plan → execute → reflect** loop that enumerates
*multiple* root-cause hypotheses from independent sources, scores each with a calibrated **confidence**, and
ranks them — "testing causation, not correlation" by corroborating each candidate against the incident's own
signals rather than trusting mere adjacency.

THE SCORES ARE EVIDENCE, NOT THE VERDICT (BOSSAPP-598, spec §8.3, G8). `_score`'s priors are BOSS's
statistical guess: every hypothesis carries it as `statistical_guess`, labelled so. The verdict — which
cause it is, or that BOSS does not know — is a MODEL's choice among these candidates (`model_verdict`,
through agent_decision), recorded word for word. With no model there is no verdict, and it says so: the
statistical leader is shown as a guess, never as the finding. Sources of candidates:
* **memory** — prior confirmed causes for the service (`known_causes`), the strongest prior.
* **change** — recent deploys/config/flag flips correlated with onset (RB-17 feeds these; hook is here).
* **context** — suspects from the knowledge-graph subgraph, weighted by edge kind and proximity.
"""
from __future__ import annotations

from collections import deque
from dataclasses import asdict, dataclass, field
from typing import Any, Optional

# prior plausibility that a relationship of this kind carries the fault, before corroboration
_EDGE_PRIOR = {
    "caused_by": 0.90, "runs_on": 0.55, "depends_on": 0.50, "deployed_from": 0.50,
    "correlates_with": 0.45, "owns": 0.40, "emits": 0.40,
}
_BASIS_PRIOR = {"memory": 0.90, "change": 0.62, "context": 0.42}
_PROXIMITY_DECAY = 0.65          # each extra hop from the failing service discounts a suspect
_CORROBORATION_BUMP = 0.12       # candidate token actually appears in the incident's signals
_MAX_HYPOTHESES = 5


@dataclass
class Hypothesis:
    root_cause: str
    cause_kind: str
    cause_key: str
    basis: str                   # memory | change | context
    confidence: float            # calibrated [0,1]
    rationale: str
    corroborated: bool = False
    evidence: dict[str, Any] = field(default_factory=dict)


class InvestigationEngine:
    def investigate(self, *, service: str, signals: list[dict[str, Any]],
                    known_causes: list[dict[str, Any]], subgraph: dict[str, Any],
                    changes: Optional[list[dict[str, Any]]] = None) -> dict[str, Any]:
        """Return ranked, confidence-scored hypotheses + the winner. Candidates are enumerated from all
        sources (the 'parallel' branches), scored independently, then reflected into a ranking."""
        candidates = self._plan(service, known_causes, subgraph, changes or [])
        scored = [self._score(c, signals) for c in candidates]          # execute: independent scoring
        ranked = self._reflect(scored)                                  # reflect: rank + dedupe
        winner = ranked[0] if ranked else self._unknown(service)
        label = lambda h: {**asdict(h), "statistical_guess": h.confidence,  # noqa: E731
                           "decided_by": "statistics",
                           "basis_word": f"BOSS's statistical guess: {h.confidence}"}
        return {"hypotheses": [label(h) for h in ranked[:_MAX_HYPOTHESES]],
                "winner": label(winner), "candidate_count": len(candidates)}

    # --- plan: enumerate candidates from every source ------------------------
    def _plan(self, service, known_causes, subgraph, changes) -> list[Hypothesis]:
        out: list[Hypothesis] = []
        for kc in known_causes:
            out.append(Hypothesis(
                root_cause=kc["name"], cause_kind=kc["kind"], cause_key=kc["node_key"],
                basis="memory", confidence=float(kc.get("confidence", 0.9)),
                rationale=f"Recurring cause for '{service}' — matches a prior confirmed root cause.",
                evidence={"known_cause": True}))
        for ch in changes:                       # RB-17 change-correlation feeds these
            out.append(Hypothesis(
                root_cause=ch.get("name", ch.get("key", "change")), cause_kind=ch.get("kind", "deployment"),
                cause_key=ch.get("key", ""), basis="change", confidence=0.0,
                rationale=ch.get("rationale", "A change correlates with the anomaly onset."),
                evidence=ch.get("evidence", {})))
        out.extend(self._context_candidates(service, subgraph))
        return out

    def _context_candidates(self, service, subgraph) -> list[Hypothesis]:
        root = subgraph.get("root") or {}
        root_id = root.get("id")
        nodes = {n["id"]: n for n in subgraph.get("nodes", [])}
        edges = subgraph.get("edges", [])
        if not root_id:
            return []
        # BFS depth + best connecting edge-kind per node over the undirected edge set
        adj: dict[str, list[tuple[str, str]]] = {}
        for e in edges:
            adj.setdefault(e["from"], []).append((e["to"], e["kind"]))
            adj.setdefault(e["to"], []).append((e["from"], e["kind"]))
        depth = {root_id: 0}
        best_edge = {root_id: ""}
        q = deque([root_id])
        while q:
            cur = q.popleft()
            for nxt, kind in adj.get(cur, []):
                if nxt not in depth:
                    depth[nxt] = depth[cur] + 1
                    best_edge[nxt] = kind
                    q.append(nxt)
        out: list[Hypothesis] = []
        for nid, n in nodes.items():
            if nid == root_id:
                continue
            d = depth.get(nid, 3)
            kind = best_edge.get(nid, "")
            prior = _EDGE_PRIOR.get(kind, 0.35) * (_PROXIMITY_DECAY ** max(0, d - 1))
            out.append(Hypothesis(
                root_cause=n.get("name", n.get("node_key", "")), cause_kind=n["kind"],
                cause_key=n["node_key"], basis="context", confidence=prior,
                rationale=f"Graph suspect {d} hop(s) from '{service}' via '{kind or 'link'}'.",
                evidence={"hops": d, "edge": kind}))
        return out

    # --- execute: score one candidate (causation test = corroboration) -------
    def _score(self, c: Hypothesis, signals: list[dict[str, Any]]) -> Hypothesis:
        if c.basis == "memory":
            base = min(1.0, _BASIS_PRIOR["memory"] * c.confidence / 0.9) if c.confidence else _BASIS_PRIOR["memory"]
        elif c.basis == "change":
            # a change right at onset is a strong suspect; the signal decays over the lookback window
            prox = c.evidence.get("proximity_minutes")
            base = _BASIS_PRIOR["change"] if prox is None else max(0.50, 0.80 - 0.30 * min(prox, 60) / 60)
        else:
            # context prior already folded edge+proximity in; scale (don't add wholesale) so an
            # uncorroborated static dependency can NOT outrank a fresh change near onset — the
            # meta-eval (RB-14) pins this ordering.
            base = min(_BASIS_PRIOR["context"] + 0.5 * c.confidence, 0.75)
        corroborated = self._corroborates(c, signals)
        score = min(1.0, base + (_CORROBORATION_BUMP if corroborated else 0.0))
        c.confidence = round(score, 3)
        c.corroborated = corroborated
        if corroborated:
            c.rationale += " Corroborated: it appears in the incident's own signals."
        return c

    @staticmethod
    def _corroborates(c: Hypothesis, signals: list[dict[str, Any]]) -> bool:
        needles = [t.lower() for t in (c.cause_key, c.root_cause) if t]
        for s in signals:
            hay = f"{s.get('signature', '')} {s.get('sample', '')} {s.get('service', '')}".lower()
            if any(n in hay for n in needles):
                return True
        return False

    # --- reflect: rank + dedupe by cause identity ----------------------------
    def _reflect(self, scored: list[Hypothesis]) -> list[Hypothesis]:
        best: dict[tuple, Hypothesis] = {}
        for h in scored:
            key = (h.cause_kind, h.cause_key)
            if key not in best or h.confidence > best[key].confidence:
                best[key] = h
        return sorted(best.values(), key=lambda h: h.confidence, reverse=True)

    @staticmethod
    def _unknown(service: str) -> Hypothesis:
        return Hypothesis(root_cause="unknown", cause_kind="unknown", cause_key="", basis="context",
                          confidence=0.1,
                          rationale=f"No memory, change, or graph context for '{service}' yet — "
                                    f"connect topology/walkers (RB-15) or record a confirmed cause.")


VERDICT_MESSAGE = ("Which of these is the incident's root cause? Choose `unknown` when the evidence "
                   "does not support one. BOSS's statistical guesses are hints, not findings.")


def model_verdict(connection: Any, settings: Any, project_id: str, *, incident: dict[str, Any],
                  hypotheses: list[dict[str, Any]], signals: list[dict[str, Any]],
                  adapter: Any = None, decider: Any = None) -> dict[str, Any]:
    """The model's verdict among the hypotheses (agent_decision): {status, cause_key, reason,
    confidence, by}. Signal samples are third-party text: evidence, never instructions."""
    from . import agent_decision as ad

    cands = [ad.Candidate(f"h{i}", f"The cause is {h.get('root_cause')} ({h.get('cause_kind')}): "
                                   f"{h.get('rationale')}") for i, h in enumerate(hypotheses)]
    cands.append(ad.Candidate("unknown", "The evidence does not support any of these yet",
                              acts=False))
    decision = ad.decide(
        connection, settings, project_id,
        situation=f"Investigating the incident '{incident.get('title')}' on "
                  f"{incident.get('service') or 'a service'}.",
        message=VERDICT_MESSAGE, candidates=cands, agent="BOSS's investigation agent",
        ref=str(incident.get("id") or ""), adapter=adapter, decider=decider,
        context={"signals": signals[:10], "hypotheses": hypotheses},
        hints=[f"BOSS's statistical guess for {h.get('root_cause')}: {h.get('statistical_guess')}"
               for h in hypotheses])
    chosen = None
    if decision.decided and decision.action and decision.action.startswith("h"):
        try:
            chosen = hypotheses[int(decision.action[1:])]
        except (ValueError, IndexError):
            chosen = None
    return {"status": decision.status, "cause_key": (chosen or {}).get("cause_key"),
            "root_cause": (chosen or {}).get("root_cause") if chosen else (
                "unknown" if decision.decided else None),
            "reason": decision.reason, "confidence": decision.confidence, "by": decision.by,
            "word": ("No model is answering: no verdict, only BOSS's statistical guesses"
                     if decision.status == ad.NO_MODEL else decision.reason)}
