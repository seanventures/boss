"""RB-17 — change-correlation (spec 12 §1.5).

Most incidents are *triggered by a recent change*, so lining the anomaly onset up against a timeline of
recent changes is often the fastest path to root cause — the "causal signal" that powers the investigation
engine (RB-16). This service reads BOSS's own change timeline (shipped code-task PRs, deployments) within a
lookback window before onset and returns ranked change candidates (closest-to-onset first) in the shape the
engine's `changes` input expects. External change sources (GitHub commits, Unleash flag flips, config drift)
are additive adapters behind the same surface.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
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

        # stack deploys (boss.deploy.yaml) — where the deployed COMMIT is recorded. Read
        # alongside the one-container deployment_runs: an app shipped by its manifest never
        # appears there, so its release was invisible to the correlation that names it.
        for r in connection.execute(
            text(
                """
                SELECT s.id, s.commit_sha, s.ref, s.status, s.target_id, s.created_at,
                       COALESCE(s.finished_at, s.created_at) AS landed, t.app_name,
                       t.environment
                FROM stack_deployments s JOIN deployment_targets t ON t.id = s.target_id
                WHERE s.project_id = :p AND s.status = 'healthy'
                  AND COALESCE(s.finished_at, s.created_at) BETWEEN :lo AND :hi
                ORDER BY s.created_at DESC
                """
            ),
            {"p": project_id, "lo": lo, "hi": onset},
        ).mappings().all():
            changes.append(self._mk(
                kind="deployment", key=f"release-{str(r['commit_sha'])[:12]}",
                name=f"release {str(r['commit_sha'])[:7]} of {r['app_name']} "
                     f"({r['environment']})",
                ts=r["landed"], onset=onset,
                evidence={"stack_deployment_id": r["id"], "commit_sha": r["commit_sha"],
                          "ref": r["ref"], "target_id": r["target_id"]},
            ))

        changes.sort(key=lambda c: c["evidence"]["proximity_minutes"])   # closest to onset first
        return changes[:limit]

    def correlate_request(
        self,
        connection: Connection,
        project_id: str,
        *,
        incident_ref: str | None = None,
        service: str | None = None,
        window: dict[str, Any] | None = None,
        lookback_minutes: int | None = None,
        limit: int = 10,
    ) -> dict[str, Any]:
        """HTTP-facing entry: resolve onset from an incident or a window, then rank changes.

        Requires ``incident_ref`` and/or ``service``. Onset is the incident's ``first_seen`` when
        ``incident_ref`` is set; otherwise ``window.end`` (epoch seconds or ISO datetime).
        """
        if not (incident_ref or service):
            raise ValueError("incident_ref or service is required")

        onset: datetime | None = None
        resolved_service = (service or "").strip() or None
        if incident_ref:
            row = connection.execute(
                text(
                    """
                    SELECT id, service, first_seen
                    FROM incidents
                    WHERE project_id = :p AND id = :id
                    """
                ),
                {"p": project_id, "id": incident_ref},
            ).mappings().first()
            if not row:
                raise LookupError(f"incident {incident_ref!r} not found")
            onset = row["first_seen"]
            if not resolved_service:
                resolved_service = row["service"] or None

        if onset is None:
            if not window or window.get("end") is None:
                raise ValueError("window.end is required when incident_ref is omitted")
            onset = self._as_datetime(window["end"])

        if lookback_minutes is None:
            if window and window.get("start") is not None and window.get("end") is not None:
                start_dt = self._as_datetime(window["start"])
                end_dt = self._as_datetime(window["end"])
                span = max(0.0, (end_dt - start_dt).total_seconds() / 60.0)
                lookback_minutes = int(span) if span > 0 else 60
            else:
                lookback_minutes = 60

        lookback_minutes = max(1, int(lookback_minutes))
        limit = max(1, min(int(limit), 100))
        changes = self.correlate(
            connection, project_id, onset,
            lookback_minutes=lookback_minutes, limit=limit,
        )
        return {
            "project_id": project_id,
            "service": resolved_service,
            "incident_ref": incident_ref,
            "onset": onset.isoformat() if hasattr(onset, "isoformat") else str(onset),
            "lookback_minutes": lookback_minutes,
            "changes": changes,
            "count": len(changes),
        }

    @staticmethod
    def _as_datetime(value: Any) -> datetime:
        if isinstance(value, datetime):
            if value.tzinfo is None:
                return value.replace(tzinfo=timezone.utc)
            return value
        if isinstance(value, (int, float)):
            return datetime.fromtimestamp(float(value), tz=timezone.utc)
        if isinstance(value, str):
            text_v = value.strip()
            if text_v.endswith("Z"):
                text_v = text_v[:-1] + "+00:00"
            try:
                parsed = datetime.fromisoformat(text_v)
            except ValueError:
                return datetime.fromtimestamp(float(text_v), tz=timezone.utc)
            if parsed.tzinfo is None:
                return parsed.replace(tzinfo=timezone.utc)
            return parsed
        raise ValueError(f"cannot parse datetime from {value!r}")

    @staticmethod
    def _mk(*, kind: str, key: str, name: str, ts: datetime, onset: datetime,
            evidence: dict[str, Any]) -> dict[str, Any]:
        proximity = max(0.0, (onset - ts).total_seconds() / 60.0)
        ev = {**evidence, "ts": ts.isoformat(), "proximity_minutes": round(proximity, 2)}
        return {"kind": kind, "key": key, "name": name,
                "rationale": f"Changed {round(proximity)} min before the anomaly onset.",
                "evidence": ev}
