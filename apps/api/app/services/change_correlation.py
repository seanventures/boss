"""RB-17 — change-correlation (spec 12 §1.5).

Most incidents are *triggered by a recent change*, so lining the anomaly onset up against a timeline of
recent changes is often the fastest path to root cause — the "causal signal" that powers the investigation
engine (RB-16). This service reads BOSS's own change timeline (shipped code-task PRs, deployments) within a
lookback window before onset and returns ranked change candidates (closest-to-onset first) in the shape the
engine's `changes` input expects. External change sources (GitHub commits, Unleash flag flips, config drift)
are additive adapters behind the same surface.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import text
from sqlalchemy.engine import Connection


class ChangeCorrelationService:
    def __init__(self, settings=None):
        self.settings = settings

    def correlate(self, connection: Connection, project_id: str, onset: datetime, *,
                  lookback_minutes: int = 60, limit: int = 10) -> list[dict[str, Any]]:
        """Recent changes in [onset - lookback, onset], closest-to-onset first. Each carries a
        `proximity_minutes` (how long before onset it landed) that the engine turns into confidence."""
        lo = onset - timedelta(minutes=lookback_minutes)
        changes: list[dict[str, Any]] = []

        # shipped code-task PRs — a code change is the classic trigger
        for r in connection.execute(
            text(
                """
                SELECT id, title, pull_request_url, pull_request_number, created_at
                FROM code_task_runs
                WHERE project_id = :p AND created_at BETWEEN :lo AND :hi
                ORDER BY created_at DESC
                """
            ),
            {"p": project_id, "lo": lo, "hi": onset},
        ).mappings().all():
            changes.append(self._mk(
                kind="deployment",
                key=(f"pr-{r['pull_request_number']}" if r["pull_request_number"] else r["id"]),
                name=(f"PR #{r['pull_request_number']}: {r['title']}" if r["pull_request_number"]
                      else f"code task: {r['title']}"),
                ts=r["created_at"], onset=onset,
                evidence={"pull_request_url": r["pull_request_url"], "code_task_run_id": r["id"]},
            ))

        # infra/service deployments
        for r in connection.execute(
            text(
                """
                SELECT id, environment, target_kind, target_ref, created_at
                FROM deployment_runs
                WHERE project_id = :p AND created_at BETWEEN :lo AND :hi
                ORDER BY created_at DESC
                """
            ),
            {"p": project_id, "lo": lo, "hi": onset},
        ).mappings().all():
            changes.append(self._mk(
                kind="deployment", key=f"deploy-{r['id']}",
                name=f"deploy {r['environment']}/{r['target_ref']}",
                ts=r["created_at"], onset=onset,
                evidence={"environment": r["environment"], "target_kind": r["target_kind"],
                          "target_ref": r["target_ref"]},
            ))

        changes.sort(key=lambda c: c["evidence"]["proximity_minutes"])   # closest to onset first
        return changes[:limit]

    @staticmethod
    def _mk(*, kind: str, key: str, name: str, ts: datetime, onset: datetime,
            evidence: dict[str, Any]) -> dict[str, Any]:
        proximity = max(0.0, (onset - ts).total_seconds() / 60.0)
        ev = {**evidence, "ts": ts.isoformat(), "proximity_minutes": round(proximity, 2)}
        return {"kind": kind, "key": key, "name": name,
                "rationale": f"Changed {round(proximity)} min before the anomaly onset.",
                "evidence": ev}
