"""Project Manager agent runtime — operate, test, validate, propose.

Steward for agent-process workflows. Observe → measure → validate →
apply-safe → verify. Never spends, never auto_builds, never silently
rewrites a published template, never merges or deploys.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any, Optional

from sqlalchemy import text
from sqlalchemy.engine import Connection

from ..config import Settings
from .agent_process import AgentProcessService
from .core import emit_event, make_id
from .receipts import ReceiptService


ALLOWED_ACTIONS = frozenset({
    "ensure_default_template",
    "advance_runs",
    "start_simulated_canary",
    "propose_improvement",
    "tick",
    "observe",
    "measure",
})

BLOCKED_ACTIONS = frozenset({
    "auto_build",
    "launch_factory_job",
    "merge",
    "deploy",
    "escalate_decision",
    "resolve_deadletter",
    "apply_decision",
    "deadletter",
    "rewrite_template",
    "mutate_template",
})

CANARY_PRD = (
    "# PM canary fixture\n"
    "Build a documented widget service.\n"
    "This is a simulated process-manager canary. It must never spend.\n"
)

CANARY_WINDOW_SECONDS = 3600
CANARY_ORIGIN = "process_manager_canary"
LEGACY_CANARY_TITLE = "PM simulated canary"

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS process_managers (
  id TEXT PRIMARY KEY,
  project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
  status TEXT NOT NULL DEFAULT 'active',
  last_tick_at TIMESTAMPTZ,
  last_metrics JSONB NOT NULL DEFAULT '{}'::jsonb,
  last_evidence JSONB NOT NULL DEFAULT '{}'::jsonb,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE(project_id)
);
CREATE INDEX IF NOT EXISTS idx_process_managers_project
  ON process_managers(project_id, status);
CREATE TABLE IF NOT EXISTS process_manager_ticks (
  id TEXT PRIMARY KEY,
  project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
  observed JSONB NOT NULL DEFAULT '{}'::jsonb,
  planned JSONB NOT NULL DEFAULT '[]'::jsonb,
  applied JSONB NOT NULL DEFAULT '[]'::jsonb,
  rejected JSONB NOT NULL DEFAULT '[]'::jsonb,
  metrics JSONB NOT NULL DEFAULT '{}'::jsonb,
  receipt_id TEXT,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_process_manager_ticks_project
  ON process_manager_ticks(project_id, created_at DESC);
CREATE TABLE IF NOT EXISTS process_manager_proposals (
  id TEXT PRIMARY KEY,
  project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
  status TEXT NOT NULL DEFAULT 'proposed',
  spec_diff JSONB NOT NULL DEFAULT '{}'::jsonb,
  reason TEXT NOT NULL,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_process_manager_proposals_project
  ON process_manager_proposals(project_id, created_at DESC);
"""


def validate_action(
    kind: str,
    payload: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    """Fail-closed validation. Pure: never spends, never applies."""
    payload = payload or {}
    action = str(kind or payload.get("kind") or "")
    checks: list[dict[str, Any]] = []

    def check(name: str, ok: bool, detail: str) -> None:
        checks.append({"name": name, "ok": ok, "detail": detail})

    check("has_kind", bool(action), "action declares a kind" if action else "action has no kind")
    check(
        "known_kind",
        action in ALLOWED_ACTIONS,
        f"kind={action}" if action in ALLOWED_ACTIONS else f"unknown kind {action!r}",
    )
    check(
        "not_auto_build",
        action != "auto_build" and payload.get("auto_build") is not True,
        "PM tick must never auto_build or spend",
    )
    check(
        "not_human_only",
        action not in BLOCKED_ACTIONS
        and action not in {"escalate_decision", "resolve_deadletter", "apply_decision", "deadletter"},
        "decisions and DLQ never apply; merge/deploy/rewrite stay human",
    )
    if action == "start_simulated_canary":
        check(
            "simulated_only",
            payload.get("simulated") is True,
            "canary must be simulated",
        )
        check(
            "no_spend",
            payload.get("auto_build") is False,
            "canary auto_build must be false",
        )
    ok = all(item["ok"] for item in checks)
    return {
        "ok": ok,
        "checks": checks,
        "failed": [item["name"] for item in checks if not item["ok"]],
    }


def compute_metrics(runs: list[dict[str, Any]]) -> dict[str, Any]:
    """Counts only. No invented quality scores."""
    run_count = len(runs)
    completed = sum(1 for run in runs if run.get("status") == "completed")
    failed = sum(1 for run in runs if run.get("status") == "failed")
    certified = 0
    revise_findings = 0
    for run in runs:
        for step in run.get("steps") or []:
            output = _as_obj(step.get("output"))
            action = str(step.get("action") or "")
            verdict = str(output.get("verdict") or "")
            if action == "certify_spec" and verdict in {"certified", "certified_planned"}:
                certified += 1
            if action == "review_prd" and verdict == "revise":
                revise_findings += len(output.get("findings") or output.get("failed") or [verdict])
    return {
        "run_count": run_count,
        "completed": completed,
        "failed": failed,
        "accept_rate": (completed / run_count) if run_count else 0.0,
        "panel_certified_rate": (certified / run_count) if run_count else 0.0,
        "revise_findings_count": revise_findings,
        "avg_score": None,
    }


def _as_obj(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            return {}
        return parsed if isinstance(parsed, dict) else {}
    return {}


def _parse_dt(value: Any) -> Optional[datetime]:
    if value is None:
        return None
    if isinstance(value, datetime):
        dt = value
    else:
        try:
            dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except ValueError:
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


class ProcessManagerService:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.processes = AgentProcessService(settings)
        self.receipts = ReceiptService()
        self._schema_ready = False

    def ensure_schema(self, connection: Connection) -> None:
        if self._schema_ready:
            return
        for statement in (chunk.strip() for chunk in SCHEMA_SQL.split(";")):
            if statement:
                connection.exec_driver_sql(statement)
        self._schema_ready = True

    def ensure_for_project(self, connection: Connection, project_id: str, actor: str) -> dict[str, Any]:
        self.ensure_schema(connection)
        existing = connection.execute(
            text("SELECT * FROM process_managers WHERE project_id=:p"),
            {"p": project_id},
        ).mappings().first()
        if existing:
            return self._row(existing)
        row = connection.execute(
            text(
                """
                INSERT INTO process_managers (id, project_id, status)
                VALUES (:id, :p, 'active')
                RETURNING *
                """
            ),
            {"id": make_id("pmgr"), "p": project_id},
        ).mappings().one()
        emit_event(
            connection, project_id, "process_manager.ensured", actor,
            "process_manager", row["id"], {"status": "active"},
        )
        return self._row(row)

    def list_ticks(self, connection: Connection, project_id: str, limit: int = 20) -> list[dict[str, Any]]:
        self.ensure_schema(connection)
        rows = connection.execute(
            text(
                """
                SELECT * FROM process_manager_ticks
                WHERE project_id=:p
                ORDER BY created_at DESC LIMIT :n
                """
            ),
            {"p": project_id, "n": limit},
        ).mappings().all()
        return [self._row(row) for row in rows]

    def ensure(self, connection: Connection, project_id: str, actor: str) -> dict[str, Any]:
        return self.ensure_for_project(connection, project_id, actor)

    def ensure_steward(self, connection: Connection, project_id: str, actor: str) -> dict[str, Any]:
        return self.ensure_for_project(connection, project_id, actor)

    def get(self, connection: Connection, project_id: str) -> dict[str, Any]:
        self.ensure_schema(connection)
        steward = connection.execute(
            text("SELECT * FROM process_managers WHERE project_id=:p"),
            {"p": project_id},
        ).mappings().first()
        ticks = self.list_ticks(connection, project_id, limit=1)
        proposals = self.list_proposals(connection, project_id, limit=10)
        last = ticks[0] if ticks else None
        metrics = (steward or {}).get("last_metrics") if steward else {}
        if isinstance(metrics, str):
            try:
                metrics = json.loads(metrics)
            except json.JSONDecodeError:
                metrics = {}
        return {
            "project_id": project_id,
            "steward": self._row(steward) if steward else None,
            "last_tick": last,
            "metrics": metrics or {},
            "proposals": proposals,
        }

    def snapshot(self, connection: Connection, project_id: str) -> dict[str, Any]:
        return self.get(connection, project_id)

    def status(self, connection: Connection, project_id: str) -> dict[str, Any]:
        return self.get(connection, project_id)

    def metrics(self, connection: Connection, project_id: str) -> dict[str, Any]:
        snap = self.get(connection, project_id)
        return {"project_id": project_id, "metrics": snap.get("metrics") or {}}

    def get_metrics(self, connection: Connection, project_id: str) -> dict[str, Any]:
        return self.metrics(connection, project_id)

    def proposals(self, connection: Connection, project_id: str) -> dict[str, Any]:
        return {
            "project_id": project_id,
            "proposals": self.list_proposals(connection, project_id),
        }

    def list_proposals(self, connection: Connection, project_id: str, limit: int = 20) -> list[dict[str, Any]]:
        self.ensure_schema(connection)
        rows = connection.execute(
            text(
                """
                SELECT * FROM process_manager_proposals
                WHERE project_id=:p
                ORDER BY created_at DESC LIMIT :n
                """
            ),
            {"p": project_id, "n": limit},
        ).mappings().all()
        return [self._row(row) for row in rows]

    def validate_action(self, kind: str, payload: Optional[dict[str, Any]] = None) -> dict[str, Any]:
        return validate_action(kind, payload)

    def tick(self, connection: Connection, *, project_id: str, actor: str) -> dict[str, Any]:
        """Observe → measure → validate → apply-safe → verify."""
        self.ensure_schema(connection)
        steward = self.ensure_for_project(connection, project_id, actor)
        observed = self._observe(connection, project_id)
        metrics = compute_metrics(observed["runs"])
        planned = self._plan(observed, metrics)
        applied: list[dict[str, Any]] = []
        rejected: list[dict[str, Any]] = []
        for move in planned:
            verdict = validate_action(str(move.get("kind") or ""), move)
            move["validation"] = verdict
            if not (move.get("apply") and verdict["ok"]):
                move["status"] = "rejected"
                rejected.append(move)
                continue
            result = self._apply(connection, project_id=project_id, actor=actor, move=move)
            move.update(result)
            if result.get("status") == "applied":
                applied.append(move)
            else:
                rejected.append(move)

        observed_after = self._observe(connection, project_id)
        metrics = compute_metrics(observed_after["runs"])
        proposal_move = self._proposal_move(observed_after, metrics, project_id, connection)
        if proposal_move:
            verdict = validate_action(proposal_move["kind"], proposal_move)
            proposal_move["validation"] = verdict
            planned.append(proposal_move)
            if proposal_move.get("apply") and verdict["ok"]:
                result = self._apply(
                    connection, project_id=project_id, actor=actor, move=proposal_move,
                )
                proposal_move.update(result)
                if result.get("status") == "applied":
                    applied.append(proposal_move)
                else:
                    rejected.append(proposal_move)
            else:
                proposal_move["status"] = "rejected"
                rejected.append(proposal_move)

        verify = self._verify(connection, project_id, applied, observed_after)
        receipt, _created = self._ensure_receipt(
            connection, project_id, "tick",
            {"steward_id": steward["id"]},
            {"applied": len(applied), "rejected": len(rejected), "verify_ok": bool(verify.get("ok"))},
        )
        for move in applied:
            self._ensure_receipt(
                connection, project_id, str(move.get("kind") or "action"),
                {"steward_id": steward["id"], "kind": move.get("kind")},
                {"status": move.get("status")},
            )

        tick_row = connection.execute(
            text(
                """
                INSERT INTO process_manager_ticks (
                  id, project_id, observed, planned, applied, rejected, metrics, receipt_id
                )
                VALUES (
                  :id, :p, CAST(:obs AS JSONB), CAST(:plan AS JSONB), CAST(:app AS JSONB),
                  CAST(:rej AS JSONB), CAST(:met AS JSONB), :receipt
                )
                RETURNING *
                """
            ),
            {
                "id": make_id("pmtick"),
                "p": project_id,
                "obs": json.dumps({
                    "template": bool(observed.get("template")),
                    "run_count": len(observed.get("runs") or []),
                    "active": observed.get("active_ids") or [],
                    "recent_canary_id": (observed.get("recent_canary") or {}).get("id"),
                }),
                "plan": json.dumps(planned, default=str),
                "app": json.dumps(applied, default=str),
                "rej": json.dumps(rejected, default=str),
                "met": json.dumps(metrics),
                "receipt": receipt.get("id"),
            },
        ).mappings().one()
        evidence = {
            "applied": [item.get("kind") for item in applied],
            "rejected": [item.get("kind") for item in rejected],
            "verify_ok": bool(verify.get("ok")),
            "receipt_id": receipt.get("id"),
            "canary_run_id": (observed_after.get("recent_canary") or {}).get("id"),
        }
        connection.execute(
            text(
                """
                UPDATE process_managers
                SET last_tick_at=now(),
                    last_metrics=CAST(:met AS JSONB),
                    last_evidence=CAST(:ev AS JSONB)
                WHERE project_id=:p
                """
            ),
            {"met": json.dumps(metrics), "ev": json.dumps(evidence), "p": project_id},
        )
        emit_event(
            connection, project_id, "process_manager.ticked", actor,
            "process_manager", steward["id"],
            {
                "applied": len(applied),
                "rejected": len(rejected),
                "verify_ok": bool(verify.get("ok")),
                "receipt_id": receipt.get("id"),
            },
        )
        return {
            "status": "verified" if verify.get("ok") else "needs_attention",
            "project_id": project_id,
            "steward": self.ensure_for_project(connection, project_id, actor),
            "tick": self._row(tick_row),
            "observed": observed,
            "planned": planned,
            "applied": applied,
            "rejected": rejected,
            "metrics": metrics,
            "verify": verify,
            "receipt_id": receipt.get("id"),
            "proposals": self.list_proposals(connection, project_id, limit=5),
        }

    def _observe(self, connection: Connection, project_id: str) -> dict[str, Any]:
        template = self.processes.get_template_by_key(connection, project_id, "prd_to_accepted_work")
        runs = []
        for run in self.processes.list_runs(connection, project_id, limit=100):
            item = dict(run)
            item["steps"] = self.processes.list_steps(connection, run["id"])
            runs.append(item)
        active = self.processes.list_active(connection, project_id)
        recent = self._recent_simulated_canary(runs)
        return {
            "template": template,
            "runs": runs,
            "active": active,
            "active_ids": [item["id"] for item in active],
            "recent_canary": recent,
        }

    def _plan(self, observed: dict[str, Any], metrics: dict[str, Any]) -> list[dict[str, Any]]:
        planned: list[dict[str, Any]] = []
        if not observed.get("template"):
            planned.append({
                "kind": "ensure_default_template",
                "apply": True,
                "process_key": "prd_to_accepted_work",
            })
        if observed.get("active_ids"):
            planned.append({
                "kind": "advance_runs",
                "apply": True,
                "run_ids": list(observed.get("active_ids") or []),
            })
        # Simulated canaries are explicit QA fixtures. Ordinary scheduler ticks must
        # not manufacture completed delivery runs when a project has no real work.
        return planned

    def _proposal_move(
        self,
        observed: dict[str, Any],
        metrics: dict[str, Any],
        project_id: str,
        connection: Connection,
    ) -> Optional[dict[str, Any]]:
        reason = self._gap_reason(observed, metrics)
        if not reason:
            return None
        existing = [
            item for item in self.list_proposals(connection, project_id)
            if item.get("status") == "proposed" and item.get("reason") == reason
        ]
        if existing:
            return None
        return {
            "kind": "propose_improvement",
            "apply": True,
            "reason": reason,
            "spec_diff": {
                "process_key": "prd_to_accepted_work",
                "op": "propose",
                "apply": False,
                "merge": False,
                "deploy": False,
                "changes": [
                    {
                        "path": "steps.review_prd",
                        "add": "require success metric and acceptance language before approve",
                    }
                ],
            },
        }

    @staticmethod
    def _gap_reason(observed: dict[str, Any], metrics: dict[str, Any]) -> Optional[str]:
        if not observed.get("template"):
            return "no_template"
        reviews: list[str] = []
        for run in observed.get("runs") or []:
            for step in run.get("steps") or []:
                if step.get("action") != "review_prd":
                    continue
                output = _as_obj(step.get("output"))
                reviews.append(str(output.get("verdict") or ""))
        if reviews and all(verdict == "revise" for verdict in reviews):
            return "all_reviews_revise"
        if metrics.get("run_count") and metrics.get("accept_rate") == 0:
            return "zero_accept_rate"
        return None

    def _apply(
        self, connection: Connection, *, project_id: str, actor: str, move: dict[str, Any],
    ) -> dict[str, Any]:
        kind = str(move.get("kind") or "")
        if kind == "ensure_default_template":
            template = self.processes.ensure_default_template(
                connection, project_id=project_id, actor=actor,
            )
            emit_event(
                connection, project_id, "process_manager.template_ensured", actor,
                "agent_process_template", template["id"],
                {"process_key": template.get("process_key")},
            )
            return {"status": "applied", "template_id": template["id"]}
        if kind == "advance_runs":
            result = self.processes.tick_due(connection, project_id=project_id, actor=actor)
            return {"status": "applied", "ran": result.get("ran"), "runs": result.get("runs")}
        if kind == "start_simulated_canary":
            run = self.processes.start_run(
                connection,
                project_id=project_id,
                actor=actor,
                prd=CANARY_PRD,
                title=str(move.get("title") or LEGACY_CANARY_TITLE)[:200],
                simulated=True,
                auto_build=False,
                origin=CANARY_ORIGIN,
            )
            emit_event(
                connection, project_id, "process_manager.canary_started", actor,
                "agent_process_run", run["id"],
                {"simulated": True, "auto_build": False, "status": run.get("status")},
            )
            return {"status": "applied", "run_id": run["id"], "run_status": run.get("status")}
        if kind == "propose_improvement":
            row = connection.execute(
                text(
                    """
                    INSERT INTO process_manager_proposals
                      (id, project_id, status, spec_diff, reason)
                    VALUES (:id, :p, 'proposed', CAST(:diff AS JSONB), :reason)
                    RETURNING *
                    """
                ),
                {
                    "id": make_id("pmprop"),
                    "p": project_id,
                    "diff": json.dumps(move.get("spec_diff") or {}),
                    "reason": str(move.get("reason") or "gap")[:300],
                },
            ).mappings().one()
            emit_event(
                connection, project_id, "process_manager.proposal_created", actor,
                "process_manager_proposal", row["id"],
                {"reason": row["reason"], "status": "proposed"},
            )
            return {"status": "applied", "proposal_id": row["id"], "reason": row["reason"]}
        return {"status": "rejected", "error": f"no apply path for {kind}"}

    def _verify(
        self,
        connection: Connection,
        project_id: str,
        applied: list[dict[str, Any]],
        observed: dict[str, Any],
    ) -> dict[str, Any]:
        checks: list[dict[str, Any]] = []

        def check(name: str, ok: bool, detail: str) -> None:
            checks.append({"name": name, "ok": ok, "detail": detail})

        kinds = {item.get("kind") for item in applied}
        if "ensure_default_template" in kinds:
            check("template_present", bool(observed.get("template")), "default template exists")
        if "start_simulated_canary" in kinds:
            check(
                "canary_visible",
                bool(observed.get("recent_canary")),
                "simulated canary recorded",
            )
        if "propose_improvement" in kinds:
            check(
                "proposal_stored",
                bool(self.list_proposals(connection, project_id, limit=1)),
                "proposal persisted",
            )
        if not applied:
            check("observe_only", True, "no apply this cycle")
        return {
            "ok": all(item["ok"] for item in checks),
            "checks": checks,
            "failed": [item["name"] for item in checks if not item["ok"]],
        }

    def _recent_simulated_canary(self, runs: list[dict[str, Any]]) -> Optional[dict[str, Any]]:
        now = datetime.now(timezone.utc)
        for run in runs:
            if run.get("status") != "completed":
                continue
            inp = _as_obj(run.get("input"))
            # A customer's simulated process is not this steward's health check. Older
            # canaries had no origin, so recognize only the exact system-authored title.
            origin = str(inp.get("origin") or "")
            is_canary = origin == CANARY_ORIGIN or (
                not origin
                and run.get("actor") == "system"
                and run.get("title") == LEGACY_CANARY_TITLE
            )
            if not is_canary or inp.get("simulated") is not True or inp.get("auto_build") is not False:
                continue
            created = _parse_dt(run.get("created_at") or run.get("updated_at"))
            if created is None:
                continue
            if (now - created).total_seconds() <= CANARY_WINDOW_SECONDS:
                return run
        return None

    def _ensure_receipt(
        self,
        connection: Connection,
        project_id: str,
        action: str,
        scope: dict[str, Any],
        payload: dict[str, Any],
    ) -> tuple[dict[str, Any], bool]:
        ids = self._scope(connection, project_id)
        minute = datetime.now(timezone.utc).strftime("%Y%m%d%H%M")
        dedupe = f"process_manager:{project_id}:{minute}:{action}"
        # RECORDED, NOT STARTED. By the time this is called the tick has already run and the
        # payload carries its outcome, so the receipt is closed in the same step. Leaving it
        # open recorded finished work as permanently in progress -- see receipts.record_completed.
        return self.receipts.record_completed(
            connection,
            ids["org_id"],
            ids["workspace_id"],
            project_id,
            dedupe,
            f"process_manager.{action}",
            {**scope, "action": action, "minute": minute},
            payload,
        )

    def _scope(self, connection: Connection, project_id: str) -> dict[str, str]:
        row = connection.execute(
            text(
                """
                SELECT p.id AS project_id, w.id AS workspace_id, o.id AS org_id
                FROM projects p
                JOIN workspaces w ON w.id = p.workspace_id
                JOIN orgs o ON o.id = w.org_id
                WHERE p.id = :project_id
                """
            ),
            {"project_id": project_id},
        ).mappings().first()
        if row is None:
            return {
                "project_id": project_id,
                "workspace_id": "workspace_unknown",
                "org_id": "org_unknown",
            }
        return dict(row)

    @staticmethod
    def _row(row) -> dict[str, Any]:
        out = dict(row)
        for key, value in list(out.items()):
            if hasattr(value, "isoformat"):
                out[key] = value.isoformat()
        for key in ("last_metrics", "last_evidence", "observed", "planned", "applied",
                    "rejected", "metrics", "spec_diff"):
            if isinstance(out.get(key), str):
                try:
                    out[key] = json.loads(out[key])
                except json.JSONDecodeError:
                    pass
        return out
