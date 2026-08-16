"""Factory flow debug + governed improve loop.

Operators could not see *where* an official run was stuck: live feed empty,
process swarm waiting, verify looping, or a crash after green tests. This
module builds one flow snapshot from existing code_task / process / job
rows and classifies stalls from observed evidence only.

The improve tick records findings and can attach a captain swarm to an
official run that has none. It never edits BOSS itself, never invents a
pass, and never auto-applies a code change.
"""
from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from typing import Any, Optional


SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS factory_flow_findings (
  id TEXT PRIMARY KEY,
  project_id TEXT NOT NULL,
  run_id TEXT,
  process_run_id TEXT,
  klass TEXT NOT NULL,
  severity TEXT NOT NULL,
  title TEXT NOT NULL,
  detail TEXT NOT NULL,
  recommended_fix TEXT NOT NULL,
  dedupe_key TEXT NOT NULL UNIQUE,
  status TEXT NOT NULL DEFAULT 'open',
  evidence JSONB NOT NULL DEFAULT '{}'::jsonb,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_factory_flow_findings_project
  ON factory_flow_findings(project_id, status, updated_at DESC);
"""

HEALTHY = "healthy"
WAITING = "waiting"
STALLED = "stalled"
FAILED = "failed"

_CRASH = re.compile(
    r"UnboundLocalError|NameError|TypeError|AttributeError|Traceback"
    r"|cannot access local variable",
    re.I,
)
_STUB_LOG = re.compile(r"process\.exit\(0\)|^\s*>\s*exit\s+0\b|webServer was not able to start", re.I | re.M)

PHASE_STALL_MINUTES = 25
HEARTBEAT_STALE_MINUTES = 22
PROCESS_WAIT_LONG_MINUTES = 45


def _minutes_since(value: Any) -> Optional[float]:
    if value is None:
        return None
    if isinstance(value, datetime):
        then = value
    else:
        text_value = str(value).strip()
        if not text_value:
            return None
        try:
            then = datetime.fromisoformat(text_value.replace("Z", "+00:00"))
        except ValueError:
            return None
    if then.tzinfo is None:
        then = then.replace(tzinfo=timezone.utc)
    now = datetime.now(timezone.utc)
    return max(0.0, (now - then).total_seconds() / 60.0)


def diagnose_flow(snap: dict[str, Any]) -> dict[str, Any]:
    """Classify one factory+swarm snapshot. Never invents a pass."""
    status = str(snap.get("status") or "")
    phase = str(snap.get("phase") or "")
    error = str(snap.get("error") or "")
    verify = str(snap.get("last_verify_output") or "")
    process_status = str(snap.get("process_status") or "")
    minutes_phase = snap.get("minutes_in_phase")
    minutes_hb = snap.get("minutes_since_heartbeat")
    try:
        minutes_phase = float(minutes_phase) if minutes_phase is not None else None
    except (TypeError, ValueError):
        minutes_phase = None
    try:
        minutes_hb = float(minutes_hb) if minutes_hb is not None else None
    except (TypeError, ValueError):
        minutes_hb = None

    if error and _CRASH.search(error):
        return _finding(
            "factory_crash",
            "high",
            FAILED,
            "Factory crashed after work started",
            error[:300],
            "Keep evidence/gates writes after the evidence dict exists. "
            "Add a regression test for the crashing line. Do not invent a pass.",
        )
    if error:
        return _finding(
            "factory_error",
            "high",
            FAILED,
            "Factory run recorded an error",
            error[:300],
            "Read the error, fix the factory or the app, and re-run through BOSS.",
        )
    if _STUB_LOG.search(verify):
        return _finding(
            "verify_dishonest",
            "high",
            FAILED if status in {"completed", "failed", "blocked"} else STALLED,
            "Verify log is a stub or swallowed failure",
            verify[-240:],
            "Fail-closed on exit 0 / || true / webServer failures. Seed real e2e.",
        )
    if status in {"failed", "blocked"}:
        return _finding(
            "factory_failed",
            "high",
            FAILED,
            f"Factory {status}",
            f"phase={phase}",
            "Inspect the run report and last_verify_output. Retry through BOSS.",
        )
    if process_status == "waiting" and status in {"failed", "blocked"}:
        return _finding(
            "process_orphaned",
            "high",
            STALLED,
            "Captain swarm waiting on a dead factory run",
            f"process={snap.get('process_run_id')} factory={snap.get('id')}",
            "Tick should refresh the build; bind a new official follow-up if the run is terminal.",
        )
    if status == "running" and phase in {"queued", ""}:
        return _finding(
            "queued_serial",
            "medium",
            WAITING,
            "Official run is queued behind the single worker lease",
            "Worker is single-lease; later official jobs wait.",
            "Keep engine=openai. Do not start duplicate official jobs. Optional: more workers.",
        )
    if (
        process_status == "waiting"
        and status == "running"
        and minutes_phase is not None
        and minutes_phase >= PROCESS_WAIT_LONG_MINUTES
    ):
        return _finding(
            "process_waiting_long",
            "medium",
            STALLED,
            "Captain swarm has been waiting on a live build too long",
            f"phase={phase} minutes={round(minutes_phase, 1)}",
            "Check verify/review hang. Heartbeat should still renew. Do not invent 5/5.",
        )
    if (
        status == "running"
        and phase not in {"queued", ""}
        and minutes_phase is not None
        and minutes_phase >= PHASE_STALL_MINUTES
    ):
        return _finding(
            "phase_stall",
            "medium",
            STALLED,
            f"Stuck in phase {phase}",
            f"{round(minutes_phase, 1)} minutes in {phase}",
            "Look at /live and last_verify_output. Kill only if heartbeat is also stale.",
        )
    if (
        status == "running"
        and minutes_hb is not None
        and minutes_hb >= HEARTBEAT_STALE_MINUTES
    ):
        return _finding(
            "heartbeat_stale",
            "high",
            STALLED,
            "Worker heartbeat is stale while the run is still marked running",
            f"{round(minutes_hb, 1)} minutes since heartbeat",
            "Shepherd should reclaim. Confirm /v1/shepherd/watch is not 500.",
        )
    if status in {"running", "queued"} or process_status in {"running", "waiting", "queued"}:
        return {
            "klass": "in_flight",
            "severity": "info",
            "state": WAITING if phase == "queued" or process_status == "waiting" else HEALTHY,
            "title": "In flight",
            "detail": f"factory={status}/{phase} process={process_status or 'none'}",
            "recommended_fix": "",
        }
    return {
        "klass": "idle",
        "severity": "info",
        "state": HEALTHY,
        "title": "Idle or complete",
        "detail": f"factory={status}/{phase}",
        "recommended_fix": "",
    }


def _finding(
    klass: str, severity: str, state: str, title: str, detail: str, fix: str,
) -> dict[str, Any]:
    return {
        "klass": klass,
        "severity": severity,
        "state": state,
        "title": title,
        "detail": detail,
        "recommended_fix": fix,
    }


def flow_steps(snap: dict[str, Any]) -> list[dict[str, Any]]:
    """Named pipeline steps from observed timings + process handoffs."""
    steps: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in snap.get("phase_timings") or []:
        if not isinstance(item, dict):
            continue
        name = str(item.get("phase") or "").strip()
        if not name:
            continue
        key = f"factory:{name}:{len(seen)}"
        seen.add(name)
        steps.append({
            "id": key,
            "lane": "factory",
            "name": name,
            "status": "done",
            "ms": item.get("ms"),
        })
    current = str(snap.get("phase") or "").strip()
    if current and snap.get("status") == "running":
        steps.append({
            "id": f"factory:{current}:current",
            "lane": "factory",
            "name": current,
            "status": "running",
            "ms": None,
        })
    for item in snap.get("process_steps") or []:
        if not isinstance(item, dict):
            continue
        steps.append({
            "id": f"process:{item.get('step_key')}",
            "lane": "swarm",
            "name": item.get("step_key") or item.get("action"),
            "status": item.get("status"),
            "role": item.get("role_key") or item.get("role"),
        })
    return steps


class FactoryFlowService:
    def __init__(self, settings: Any):
        self.settings = settings
        self._schema_ready = False

    def ensure_schema(self, connection: Any) -> None:
        if self._schema_ready:
            return
        for statement in (chunk.strip() for chunk in SCHEMA_SQL.split(";")):
            if statement:
                connection.exec_driver_sql(statement)
        self._schema_ready = True

    def board(self, connection: Any, project_id: str, *, limit: int = 20) -> dict[str, Any]:
        from sqlalchemy import text
        self.ensure_schema(connection)
        rows = connection.execute(
            text(
                """
                SELECT id, status, phase, title, error, state, finalize_ctx, updated_at
                FROM code_task_runs
                WHERE project_id=:p
                  AND (
                    status IN ('running','queued','failed','blocked')
                    OR updated_at > now() - interval '12 hours'
                  )
                ORDER BY updated_at DESC
                LIMIT :n
                """
            ),
            {"p": project_id, "n": limit},
        ).mappings().all()
        items = [self.snapshot(connection, project_id, dict(row)) for row in rows]
        findings = self.list_findings(connection, project_id, limit=20)
        stalled = [item for item in items if item.get("diagnosis", {}).get("state") in {STALLED, FAILED}]
        return {
            "project_id": project_id,
            "items": items,
            "findings": findings,
            "stalled": len(stalled),
            "in_flight": sum(1 for item in items if item.get("status") in {"running", "queued"}),
        }

    def snapshot(
        self, connection: Any, project_id: str, row: dict[str, Any],
    ) -> dict[str, Any]:
        state = _as_dict(row.get("state"))
        fctx = _as_dict(row.get("finalize_ctx"))
        run_id = row.get("id")
        process = self._attached_process(connection, project_id, str(run_id or ""))
        job = self._job_for_run(connection, project_id, str(run_id or ""))
        phase_started = None
        if isinstance(fctx.get("phase_started_at"), str):
            phase_started = fctx.get("phase_started_at")
        elif job.get("updated_at"):
            phase_started = job.get("updated_at")
        snap = {
            "id": run_id,
            "title": row.get("title"),
            "status": row.get("status"),
            "phase": row.get("phase"),
            "error": row.get("error"),
            "official": bool(fctx.get("official") or fctx.get("qa_benchmark")),
            "last_verify_output": state.get("last_verify_output") or "",
            "tests_passed": state.get("tests_passed"),
            "test_exit_code": state.get("test_exit_code"),
            "phase_timings": fctx.get("phase_timings") or [],
            "minutes_in_phase": _minutes_since(phase_started or row.get("updated_at")),
            "minutes_since_heartbeat": _minutes_since(job.get("heartbeat_at") or job.get("updated_at")),
            "job_status": job.get("status"),
            "process_run_id": (process or {}).get("id"),
            "process_status": (process or {}).get("status"),
            "process_steps": (process or {}).get("steps") or [],
        }
        diagnosis = diagnose_flow(snap)
        return {
            **{k: snap[k] for k in (
                "id", "title", "status", "phase", "error", "official",
                "tests_passed", "test_exit_code", "job_status",
                "process_run_id", "process_status", "minutes_in_phase",
                "minutes_since_heartbeat",
            )},
            "steps": flow_steps(snap),
            "diagnosis": diagnosis,
            "verify_tail": str(snap.get("last_verify_output") or "")[-240:],
        }

    def list_findings(
        self, connection: Any, project_id: str, *, limit: int = 20,
    ) -> list[dict[str, Any]]:
        from sqlalchemy import text

        self.ensure_schema(connection)
        rows = connection.execute(
            text(
                """
                SELECT id, run_id, process_run_id, klass, severity, title, detail,
                       recommended_fix, status, created_at
                FROM factory_flow_findings
                WHERE project_id=:p
                ORDER BY updated_at DESC
                LIMIT :n
                """
            ),
            {"p": project_id, "n": limit},
        ).mappings().all()
        return [dict(row) for row in rows]

    def tick(
        self, connection: Any, project_id: str, *, actor: str = "factory_flow",
    ) -> dict[str, Any]:
        """Observe stalls, record findings, attach missing official swarms."""
        from .core import emit_event
        board = self.board(connection, project_id)
        recorded = 0
        attached = 0
        for item in board.get("items") or []:
            diagnosis = item.get("diagnosis") or {}
            if diagnosis.get("state") in {STALLED, FAILED} and diagnosis.get("recommended_fix"):
                if self._record_finding(connection, project_id, item, diagnosis, actor=actor):
                    recorded += 1
            if (
                item.get("official")
                and item.get("status") in {"running", "queued"}
                and not item.get("process_run_id")
            ):
                if self._attach_missing_swarm(connection, project_id, item, actor=actor):
                    attached += 1
        emit_event(
            connection, project_id, "factory_flow.ticked", actor, "project", project_id,
            {"recorded": recorded, "attached": attached, "stalled": board.get("stalled")},
        )
        return {
            "project_id": project_id,
            "recorded": recorded,
            "attached": attached,
            "stalled": board.get("stalled"),
            "in_flight": board.get("in_flight"),
        }

    def _record_finding(
        self,
        connection: Any,
        project_id: str,
        item: dict[str, Any],
        diagnosis: dict[str, Any],
        *,
        actor: str,
    ) -> bool:
        from sqlalchemy import text

        from .core import emit_event, make_id

        dedupe = f"factory_flow:{project_id}:{diagnosis.get('klass')}:{item.get('id')}"
        existing = connection.execute(
            text("SELECT id FROM factory_flow_findings WHERE dedupe_key=:d"),
            {"d": dedupe},
        ).scalar_one_or_none()
        if existing:
            return False
        finding_id = make_id("fflow")
        connection.execute(
            text(
                """
                INSERT INTO factory_flow_findings
                  (id, project_id, run_id, process_run_id, klass, severity, title,
                   detail, recommended_fix, dedupe_key, evidence)
                VALUES
                  (:id, :p, :rid, :pr, :k, :sev, :title, :detail, :fix, :d, CAST(:ev AS JSONB))
                """
            ),
            {
                "id": finding_id,
                "p": project_id,
                "rid": item.get("id"),
                "pr": item.get("process_run_id"),
                "k": diagnosis.get("klass"),
                "sev": diagnosis.get("severity"),
                "title": diagnosis.get("title"),
                "detail": (diagnosis.get("detail") or "")[:1000],
                "fix": diagnosis.get("recommended_fix") or "",
                "d": dedupe,
                "ev": json.dumps({
                    "phase": item.get("phase"),
                    "status": item.get("status"),
                    "error": item.get("error"),
                }),
            },
        )
        emit_event(
            connection, project_id, "factory_flow.finding", actor,
            "factory_flow_finding", finding_id,
            {"klass": diagnosis.get("klass"), "run_id": item.get("id")},
        )
        return True

    def _attach_missing_swarm(
        self, connection: Any, project_id: str, item: dict[str, Any], *, actor: str,
    ) -> bool:
        try:
            from .agent_process import bind_captain_swarm
        except Exception:  # noqa: BLE001
            return False
        try:
            bind_captain_swarm(
                connection,
                settings=self.settings,
                project_id=project_id,
                actor=actor,
                prd=str(item.get("title") or "Official factory run"),
                title=f"Captain swarm — {item.get('title') or item.get('id')}",
                attach_run_id=str(item.get("id")),
                official=True,
                engine="openai",
                review_panel_size=5,
            )
        except Exception:  # noqa: BLE001 — tick must never crash the worker
            return False
        return True

    def _attached_process(
        self, connection: Any, project_id: str, run_id: str,
    ) -> Optional[dict[str, Any]]:
        from sqlalchemy import text

        if not run_id:
            return None
        try:
            row = connection.execute(
                text(
                    """
                    SELECT id, status, title, context, current_steps
                    FROM agent_process_runs
                    WHERE project_id=:p
                      AND (
                        context->>'run_id' = :r
                        OR input->>'attach_run_id' = :r
                      )
                    ORDER BY updated_at DESC
                    LIMIT 1
                    """
                ),
                {"p": project_id, "r": run_id},
            ).mappings().first()
        except Exception:  # noqa: BLE001
            return None
        if row is None:
            return None
        steps = []
        try:
            steps = [
                dict(item)
                for item in connection.execute(
                    text(
                        """
                        SELECT step_key, role_key, action, status
                        FROM agent_process_steps
                        WHERE run_id=:id
                        ORDER BY created_at
                        """
                    ),
                    {"id": row["id"]},
                ).mappings().all()
            ]
        except Exception:  # noqa: BLE001
            steps = []
        return {**dict(row), "steps": steps}

    def _job_for_run(
        self, connection: Any, project_id: str, run_id: str,
    ) -> dict[str, Any]:
        from sqlalchemy import text

        try:
            row = connection.execute(
                text(
                    """
                    SELECT status, updated_at, lease_expires_at
                    FROM jobs
                    WHERE project_id=:p AND kind='code.task'
                      AND payload->>'run_id' = :r
                    ORDER BY updated_at DESC
                    LIMIT 1
                    """
                ),
                {"p": project_id, "r": run_id},
            ).mappings().first()
        except Exception:  # noqa: BLE001
            return {}
        return dict(row) if row else {}


def _as_dict(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if isinstance(value, str) and value.strip():
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            return {}
        return parsed if isinstance(parsed, dict) else {}
    return {}
