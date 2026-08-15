"""First-class SRE runbooks attached to reliability incidents.

A named runbook is a short, validated step list. Steps execute only after
validate; anything potentially spendy or external creates a hold-default
Decision and returns ``status=held``. Decisions are never auto-applied.

Built-in templates spend nothing:
  * ``restart_preview`` — safe, recorded, no preview mutation
  * ``page_oncall`` — may dry-run the existing on-call page (receipted,
    simulated without credentials)

Receipts use ``dedupe_key = sre_runbook:{run_id}:{step}``. Not public GA.
"""
from __future__ import annotations

import json
from typing import Any, Optional

from sqlalchemy import text
from sqlalchemy.engine import Connection

from .core import create_blocking_decision, emit_event, make_id, now_utc
from .oncall_sre import OncallSreService
from .receipts import ReceiptService


KNOWN_ACTIONS = frozenset({"restart_preview", "page_oncall"})
SAFE_ACTIONS = frozenset({"restart_preview"})
EXTERNAL_ACTIONS = frozenset({"page_oncall"})

BUILTIN_RUNBOOKS: tuple[dict[str, Any], ...] = (
    {
        "name": "restart_preview",
        "steps": [
            {
                "id": "restart",
                "action": "restart_preview",
                "kind": "safe",
                "description": "Validate a preview restart. No spend. No mutation.",
            }
        ],
    },
    {
        "name": "page_oncall",
        "steps": [
            {
                "id": "page",
                "action": "page_oncall",
                "kind": "external",
                "description": "Dry-run the existing on-call page. Live notify stays held.",
            }
        ],
    },
)

_DDL = """
CREATE TABLE IF NOT EXISTS sre_runbooks (
  id TEXT PRIMARY KEY,
  project_id TEXT NOT NULL REFERENCES projects(id),
  name TEXT NOT NULL,
  steps JSONB NOT NULL DEFAULT '[]'::jsonb,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE (project_id, name)
);
CREATE INDEX IF NOT EXISTS idx_sre_runbooks_project
  ON sre_runbooks(project_id, name);

CREATE TABLE IF NOT EXISTS sre_runbook_runs (
  id TEXT PRIMARY KEY,
  project_id TEXT NOT NULL REFERENCES projects(id),
  runbook_id TEXT NOT NULL REFERENCES sre_runbooks(id),
  incident_id TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'queued',
  decision_id TEXT,
  receipt_id TEXT,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_sre_runbook_runs_project
  ON sre_runbook_runs(project_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_sre_runbook_runs_runbook
  ON sre_runbook_runs(project_id, runbook_id, created_at DESC);
"""


class SreRunbookError(Exception):
    def __init__(self, message: str, *, status_code: int = 400, payload: Optional[dict] = None):
        super().__init__(message)
        self.status_code = status_code
        self.payload = payload or {}


def classify_step(step: Any) -> str:
    """Return safe | external | unknown. Unknown is fail-closed."""
    if not isinstance(step, dict):
        return "unknown"
    action = str(step.get("action") or "").strip()
    if action in SAFE_ACTIONS:
        return "safe"
    if action in EXTERNAL_ACTIONS:
        return "external"
    return "unknown"


def receipt_dedupe_key(run_id: str, step: str) -> str:
    return f"sre_runbook:{run_id}:{step}"


def _as_steps(raw: Any) -> list[dict[str, Any]]:
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except json.JSONDecodeError:
            return []
    if not isinstance(raw, list):
        return []
    return [item for item in raw if isinstance(item, dict)]


def _row(row: Any) -> dict[str, Any]:
    data = dict(row)
    steps = data.get("steps")
    if isinstance(steps, str):
        try:
            data["steps"] = json.loads(steps)
        except json.JSONDecodeError:
            data["steps"] = []
    created = data.get("created_at")
    if hasattr(created, "isoformat"):
        data["created_at"] = created.isoformat()
    return data


class SreRunbookService:
    def __init__(self, settings=None):
        self.settings = settings
        self.receipts = ReceiptService()
        self.oncall = OncallSreService(settings)
        self._tables_ready = False

    def ensure_tables(self, connection: Connection) -> None:
        if self._tables_ready:
            return
        for stmt in _DDL.split(";"):
            s = stmt.strip()
            if s:
                connection.execute(text(s))
        self._tables_ready = True

    def ensure_builtins(self, connection: Connection, project_id: str) -> list[dict[str, Any]]:
        """Idempotent seed of the two no-spend templates."""
        self.ensure_tables(connection)
        seeded: list[dict[str, Any]] = []
        for template in BUILTIN_RUNBOOKS:
            existing = self.get_runbook_by_name(connection, project_id, template["name"])
            if existing:
                seeded.append(existing)
                continue
            seeded.append(
                self.create_runbook(
                    connection,
                    project_id,
                    name=template["name"],
                    steps=template["steps"],
                    actor="system",
                )
            )
        return seeded

    def create_runbook(
        self,
        connection: Connection,
        project_id: str,
        *,
        name: str,
        steps: list[dict[str, Any]],
        actor: str = "system",
    ) -> dict[str, Any]:
        self.ensure_tables(connection)
        name = (name or "").strip()
        if not name:
            raise SreRunbookError("name is required")
        if not isinstance(steps, list) or not steps:
            raise SreRunbookError("steps must be a non-empty list")
        normalized: list[dict[str, Any]] = []
        seen: set[str] = set()
        for i, raw in enumerate(steps):
            if not isinstance(raw, dict):
                raise SreRunbookError(f"steps[{i}] must be an object")
            action = str(raw.get("action") or "").strip()
            if not action:
                raise SreRunbookError(f"steps[{i}] needs an action")
            step_id = str(raw.get("id") or action).strip()
            if step_id in seen:
                raise SreRunbookError(f"duplicate step id {step_id!r}")
            seen.add(step_id)
            kind = classify_step({"action": action})
            if kind == "unknown":
                # Persist unknown steps so execute can fail closed visibly.
                kind = "unknown"
            normalized.append({
                "id": step_id,
                "action": action,
                "kind": str(raw.get("kind") or kind),
                "description": str(raw.get("description") or "")[:400],
            })
        runbook_id = make_id("srb")
        connection.execute(
            text(
                """
                INSERT INTO sre_runbooks (id, project_id, name, steps, created_at)
                VALUES (:id, :p, :name, CAST(:steps AS JSONB), :now)
                """
            ),
            {
                "id": runbook_id,
                "p": project_id,
                "name": name,
                "steps": json.dumps(normalized),
                "now": now_utc(),
            },
        )
        emit_event(
            connection, project_id, "sre_runbook.created", actor,
            "sre_runbook", runbook_id, {"name": name, "steps": len(normalized)},
        )
        row = self.get_runbook(connection, project_id, runbook_id)
        if not row:
            raise SreRunbookError("failed to persist runbook", status_code=500)
        return row

    def list_runbooks(self, connection: Connection, project_id: str) -> list[dict[str, Any]]:
        self.ensure_builtins(connection, project_id)
        rows = connection.execute(
            text(
                """
                SELECT id, project_id, name, steps, created_at
                FROM sre_runbooks WHERE project_id = :p
                ORDER BY name
                """
            ),
            {"p": project_id},
        ).mappings().all()
        return [_row(r) for r in rows]

    def get_runbook(
        self, connection: Connection, project_id: str, runbook_id: str
    ) -> Optional[dict[str, Any]]:
        self.ensure_tables(connection)
        row = connection.execute(
            text(
                """
                SELECT id, project_id, name, steps, created_at
                FROM sre_runbooks WHERE project_id = :p AND id = :id
                """
            ),
            {"p": project_id, "id": runbook_id},
        ).mappings().first()
        return _row(row) if row else None

    def get_runbook_by_name(
        self, connection: Connection, project_id: str, name: str
    ) -> Optional[dict[str, Any]]:
        self.ensure_tables(connection)
        row = connection.execute(
            text(
                """
                SELECT id, project_id, name, steps, created_at
                FROM sre_runbooks WHERE project_id = :p AND name = :name
                """
            ),
            {"p": project_id, "name": name},
        ).mappings().first()
        return _row(row) if row else None

    def list_runs(
        self,
        connection: Connection,
        project_id: str,
        *,
        runbook_id: Optional[str] = None,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        self.ensure_tables(connection)
        clause = " AND runbook_id = :runbook_id" if runbook_id else ""
        params: dict[str, Any] = {"p": project_id, "limit": max(1, min(int(limit), 200))}
        if runbook_id:
            params["runbook_id"] = runbook_id
        rows = connection.execute(
            text(
                f"""
                SELECT id, project_id, runbook_id, incident_id, status,
                       decision_id, receipt_id, created_at
                FROM sre_runbook_runs
                WHERE project_id = :p{clause}
                ORDER BY created_at DESC
                LIMIT :limit
                """
            ),
            params,
        ).mappings().all()
        return [_row(r) for r in rows]

    def execute_run(
        self,
        connection: Connection,
        project_id: str,
        runbook_id: str,
        incident_id: str,
        *,
        actor: str = "system",
        force: bool = False,
        adapter: str = "slack_webhook",
    ) -> dict[str, Any]:
        """Attach a runbook to an incident and execute after validate.

        ``force`` never auto-applies a Decision. Spendy/external steps stay
        ``held`` until a human-approved Decision already exists.
        """
        self.ensure_builtins(connection, project_id)
        incident_id = (incident_id or "").strip()
        if not incident_id:
            raise SreRunbookError("incident_id is required")
        runbook = self.get_runbook(connection, project_id, runbook_id)
        if not runbook:
            raise SreRunbookError(f"runbook {runbook_id} not found", status_code=404)
        steps = _as_steps(runbook.get("steps"))
        if not steps:
            raise SreRunbookError("runbook has no steps")

        run = self._insert_run(
            connection, project_id, runbook["id"], incident_id, status="running",
        )
        run_id = run["id"]
        del force  # reserved; never a Decision bypass

        first = steps[0]
        step_id = str(first.get("id") or first.get("action") or "step")
        kind = classify_step(first)
        if kind == "unknown":
            emit_event(
                connection, project_id, "sre_runbook.run.failed", actor,
                "sre_runbook_run", run_id,
                {"reason": "unknown_step", "action": first.get("action"), "public_ga": False},
            )
            return self._finish(
                connection, run,
                status="failed",
                extra={"reason": "unknown_step", "action": first.get("action")},
            )

        if kind == "external" and not self._approved_decision(
            connection, project_id, runbook["id"], incident_id,
        ):
            decision_id = self._hold_decision(
                connection, project_id, runbook, incident_id, step_id, actor=actor,
            )
            receipt_id = None
            extra: dict[str, Any] = {
                "held": True,
                "reason": "decision_required",
                "action": first.get("action"),
                "public_ga": False,
            }
            if first.get("action") == "page_oncall":
                receipt_id, notify = self._page_oncall_dry_run(
                    connection, project_id, run_id, step_id, incident_id,
                    adapter=adapter, actor=actor,
                )
                extra["notify"] = notify
                extra["simulated"] = True
            emit_event(
                connection, project_id, "sre_runbook.run.held", actor,
                "sre_runbook_run", run_id,
                {"decision_id": decision_id, "step": step_id, "public_ga": False},
            )
            return self._finish(
                connection, run,
                status="held",
                decision_id=decision_id,
                receipt_id=receipt_id,
                extra=extra,
            )

        if first.get("action") == "restart_preview":
            receipt_id = self._record_receipt(
                connection, project_id, run_id, step_id,
                payload={
                    "action": "restart_preview",
                    "applied": False,
                    "simulated": True,
                    "reason": "safe_validate_only",
                    "incident_id": incident_id,
                },
            )
            emit_event(
                connection, project_id, "sre_runbook.run.completed", actor,
                "sre_runbook_run", run_id,
                {"step": step_id, "applied": False, "public_ga": False},
            )
            return self._finish(
                connection, run,
                status="completed",
                receipt_id=receipt_id,
                extra={"applied": False, "simulated": True, "action": "restart_preview"},
            )

        if first.get("action") == "page_oncall":
            receipt_id, notify = self._page_oncall_dry_run(
                connection, project_id, run_id, step_id, incident_id,
                adapter=adapter, actor=actor,
            )
            emit_event(
                connection, project_id, "sre_runbook.run.completed", actor,
                "sre_runbook_run", run_id,
                {"step": step_id, "simulated": True, "public_ga": False},
            )
            return self._finish(
                connection, run,
                status="completed",
                receipt_id=receipt_id,
                extra={"notify": notify, "simulated": True, "action": "page_oncall"},
            )

        return self._finish(
            connection, run,
            status="failed",
            extra={"reason": "unhandled_step", "action": first.get("action")},
        )

    # ── internals ──────────────────────────────────────────────────────────
    def _insert_run(
        self,
        connection: Connection,
        project_id: str,
        runbook_id: str,
        incident_id: str,
        *,
        status: str,
    ) -> dict[str, Any]:
        run_id = make_id("srrr")
        connection.execute(
            text(
                """
                INSERT INTO sre_runbook_runs
                  (id, project_id, runbook_id, incident_id, status, created_at)
                VALUES
                  (:id, :p, :runbook_id, :incident_id, :status, :now)
                """
            ),
            {
                "id": run_id,
                "p": project_id,
                "runbook_id": runbook_id,
                "incident_id": incident_id,
                "status": status,
                "now": now_utc(),
            },
        )
        return {
            "id": run_id,
            "project_id": project_id,
            "runbook_id": runbook_id,
            "incident_id": incident_id,
            "status": status,
            "decision_id": None,
            "receipt_id": None,
            "created_at": now_utc().isoformat(),
        }

    def _finish(
        self,
        connection: Connection,
        run: dict[str, Any],
        *,
        status: str,
        decision_id: Optional[str] = None,
        receipt_id: Optional[str] = None,
        extra: Optional[dict[str, Any]] = None,
    ) -> dict[str, Any]:
        connection.execute(
            text(
                """
                UPDATE sre_runbook_runs
                SET status = :status, decision_id = :decision_id, receipt_id = :receipt_id
                WHERE id = :id
                """
            ),
            {
                "status": status,
                "decision_id": decision_id,
                "receipt_id": receipt_id,
                "id": run["id"],
            },
        )
        out = dict(run)
        out["status"] = status
        out["decision_id"] = decision_id
        out["receipt_id"] = receipt_id
        out["public_ga"] = False
        if extra:
            out.update(extra)
        return out

    def _tenant(self, connection: Connection, project_id: str) -> dict[str, str]:
        row = connection.execute(
            text(
                """
                SELECT w.org_id, p.workspace_id
                FROM projects p
                JOIN workspaces w ON w.id = p.workspace_id
                WHERE p.id = :project_id
                """
            ),
            {"project_id": project_id},
        ).mappings().first()
        if not row:
            raise SreRunbookError("Project not found", status_code=404)
        return {"org_id": row["org_id"], "workspace_id": row["workspace_id"]}

    def _record_receipt(
        self,
        connection: Connection,
        project_id: str,
        run_id: str,
        step: str,
        *,
        payload: dict[str, Any],
    ) -> str:
        tenant = self._tenant(connection, project_id)
        key = receipt_dedupe_key(run_id, step)
        receipt, _created = self.receipts.create_or_reuse(
            connection,
            tenant["org_id"],
            tenant["workspace_id"],
            project_id,
            key,
            "sre_runbook.step",
            {"run_id": run_id, "step": step},
            payload,
        )
        self.receipts.mark_succeeded(
            connection, receipt["id"],
            provider_ref=f"sre_runbook:{step}",
            result=payload,
        )
        return receipt["id"]

    def _page_oncall_dry_run(
        self,
        connection: Connection,
        project_id: str,
        run_id: str,
        step: str,
        incident_id: str,
        *,
        adapter: str,
        actor: str,
    ) -> tuple[str, dict[str, Any]]:
        notify = self.oncall.page(
            connection,
            project_id,
            title=f"SRE runbook page ({incident_id})",
            reliability_incident_id=incident_id,
            adapter=adapter,
            force_live=False,
            actor=actor,
            dedupe_key=f"{receipt_dedupe_key(run_id, step)}:oncall",
        )
        payload = {
            "action": "page_oncall",
            "simulated": True,
            "posted": False,
            "incident_id": incident_id,
            "oncall": {
                "simulated": bool(notify.get("simulated", True)),
                "page_id": (notify.get("page") or {}).get("id"),
                "receipt_id": (notify.get("notify") or {}).get("receipt_id"),
            },
        }
        receipt_id = self._record_receipt(
            connection, project_id, run_id, step, payload=payload,
        )
        return receipt_id, payload

    def _approved_decision(
        self,
        connection: Connection,
        project_id: str,
        runbook_id: str,
        incident_id: str,
    ) -> bool:
        rows = connection.execute(
            text(
                """
                SELECT id, status, recommendation
                FROM decisions
                WHERE project_id = :project_id AND status = :status
                """
            ),
            {"project_id": project_id, "status": "approved"},
        ).mappings().all()
        for row in rows:
            rec = row.get("recommendation") or {}
            if isinstance(rec, str):
                try:
                    rec = json.loads(rec)
                except json.JSONDecodeError:
                    rec = {}
            if (
                rec.get("runbook_id") == runbook_id
                and rec.get("incident_id") == incident_id
                and rec.get("kind") == "sre_runbook"
            ):
                return True
        return False

    def _hold_decision(
        self,
        connection: Connection,
        project_id: str,
        runbook: dict[str, Any],
        incident_id: str,
        step_id: str,
        *,
        actor: str,
    ) -> str:
        existing = connection.execute(
            text(
                """
                SELECT id, status, recommendation
                FROM decisions
                WHERE project_id = :project_id
                """
            ),
            {"project_id": project_id},
        ).mappings().all()
        for row in existing:
            rec = row.get("recommendation") or {}
            if isinstance(rec, str):
                try:
                    rec = json.loads(rec)
                except json.JSONDecodeError:
                    rec = {}
            if (
                rec.get("kind") == "sre_runbook"
                and rec.get("runbook_id") == runbook["id"]
                and rec.get("incident_id") == incident_id
                and row.get("status") == "pending"
            ):
                return row["id"]
        return create_blocking_decision(
            connection,
            project_id,
            None,
            f"Approve SRE runbook {runbook['name']!r} for incident {incident_id}?",
            {
                "kind": "sre_runbook",
                "runbook_id": runbook["id"],
                "runbook_name": runbook["name"],
                "incident_id": incident_id,
                "step": step_id,
                "actor": actor,
            },
            {"estimated_cost": 0, "spend": False},
            "sre_runbook_external",
            risk_tier=2,
            default_action="hold",
        )


__all__ = [
    "BUILTIN_RUNBOOKS",
    "SreRunbookError",
    "SreRunbookService",
    "classify_step",
    "receipt_dedupe_key",
]
