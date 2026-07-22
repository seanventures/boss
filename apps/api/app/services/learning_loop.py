"""RB-6 — the native `LearningLoop` primitive (spec 12 §4).

A BOSS-wide capability, not SRE-specific: *any* agent records labeled examples + outcomes into a growing
corpus, and the primitive turns that corpus into (a) metrics and (b) concrete tuning suggestions. It
generalizes the VAG verifier ratchet (#107) — the verifier and the SRE agent are two consumers of one
primitive.

Four moves:
* `record()`  — append one labeled example (an incident verdict, a remediation outcome, …).
* `corpus()`  — load the accumulated examples for an agent (the dataset).
* `evaluate()`— roll the corpus into metrics (false-alarm rate, fix-success rate, counts).
* `suggest_tuning()` — read the corpus and propose changes: raise thresholds for repeatedly-dismissed
  signatures, and surface confirmed examples as few-shot candidates.
"""
from __future__ import annotations

import json
from collections import Counter
from typing import Any, Optional

from sqlalchemy import text
from sqlalchemy.engine import Connection

from .core import make_id, now_utc

# how many independent false-alarm dismissals of the same signature before we suggest raising its threshold
_DISMISS_TO_TUNE = 2


class LearningLoopService:
    def __init__(self, settings=None):
        self.settings = settings

    def record(self, connection: Connection, project_id: str, agent: str, kind: str, *,
               label: str = "", outcome: str = "", ref: str = "", source: str = "manual",
               payload: Optional[dict] = None) -> str:
        example_id = make_id("lex")
        connection.execute(
            text(
                """
                INSERT INTO learning_examples (id, project_id, agent, kind, label, outcome, ref,
                                               source, payload, created_at)
                VALUES (:id, :p, :agent, :kind, :label, :outcome, :ref, :source,
                        CAST(:payload AS JSONB), :now)
                """
            ),
            {"id": example_id, "p": project_id, "agent": agent, "kind": kind, "label": label,
             "outcome": outcome, "ref": ref, "source": source,
             "payload": json.dumps(payload or {}, default=str), "now": now_utc()},
        )
        return example_id

    def corpus(self, connection: Connection, project_id: str, agent: str, *,
               kind: Optional[str] = None, limit: int = 500) -> list[dict[str, Any]]:
        clause = " AND kind = :kind" if kind else ""
        params: dict[str, Any] = {"p": project_id, "agent": agent, "limit": limit}
        if kind:
            params["kind"] = kind
        rows = connection.execute(
            text(
                f"""
                SELECT id, agent, kind, label, outcome, ref, source, payload, created_at
                FROM learning_examples
                WHERE project_id = :p AND agent = :agent{clause}
                ORDER BY created_at DESC LIMIT :limit
                """
            ),
            params,
        ).mappings().all()
        return [dict(r) for r in rows]

    def evaluate(self, connection: Connection, project_id: str, agent: str) -> dict[str, Any]:
        """Roll the corpus into metrics. `false_alarm_rate` = dismissed / (confirmed + dismissed);
        `fix_success_rate` = fix_good / (fix_good + fix_bad)."""
        rows = self.corpus(connection, project_id, agent, limit=5000)
        outcomes = Counter(r["outcome"] for r in rows)
        confirmed, dismissed = outcomes.get("confirmed", 0), outcomes.get("dismissed", 0)
        good, bad = outcomes.get("fix_good", 0), outcomes.get("fix_bad", 0)
        triaged = confirmed + dismissed
        fixes = good + bad
        return {
            "agent": agent, "total": len(rows),
            "by_outcome": dict(outcomes),
            "by_label": dict(Counter(r["label"] for r in rows)),
            "false_alarm_rate": round(dismissed / triaged, 3) if triaged else None,
            "fix_success_rate": round(good / fixes, 3) if fixes else None,
        }

    def suggest_tuning(self, connection: Connection, project_id: str, agent: str) -> dict[str, Any]:
        """Concrete tuning from the corpus: signatures dismissed as false alarms >= _DISMISS_TO_TUNE
        times → raise their detection threshold; confirmed examples → few-shot candidates."""
        rows = self.corpus(connection, project_id, agent, kind="incident_feedback", limit=5000)
        dismissed_sigs: Counter = Counter()
        few_shot: list[dict[str, Any]] = []
        for r in rows:
            payload = r["payload"] or {}
            if r["outcome"] == "dismissed":
                for sig in payload.get("signatures", []):
                    dismissed_sigs[sig] += 1
            elif r["outcome"] == "confirmed":
                few_shot.append({"ref": r["ref"], "label": r["label"],
                                 "signatures": payload.get("signatures", [])})
        raise_threshold = [{"signature": s, "dismissals": n}
                           for s, n in dismissed_sigs.items() if n >= _DISMISS_TO_TUNE]
        return {"agent": agent, "raise_threshold": raise_threshold,
                "few_shot_candidates": few_shot[:20]}
