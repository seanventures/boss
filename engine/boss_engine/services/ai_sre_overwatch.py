"""Always-on AI SRE overwatch — observe only.

Worker / Metatron ticks this path to scan registered reliability sources,
diagnose open or critical incidents that have no investigation yet, and
optionally open a receipted on-call page for new critical incidents.

Hard rules:
  * Never auto-remediate.
  * Never auto-apply Decisions.
  * Never merge PRs.
  * Never invent RCA confidence.
  * Missing sources stay an honest empty, not a fake scan.
  * External notify is dry-run without credentials (force_live=False).
  * Every tick is receipted with ``ai_sre:overwatch:{project}:{window}``.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from sqlalchemy import text
from sqlalchemy.engine import Connection, Engine

from .core import emit_event, now_utc
from .oncall_sre import OncallSreError, OncallSreService
from .receipts import ReceiptService
from .reliability import ReliabilityService


LOGGER = logging.getLogger("boss.ai_sre.overwatch")

OVERWATCH_EFFECT_KIND = "ai_sre.overwatch"
SCAN_LOOKBACK = timedelta(minutes=15)
TERMINAL_INCIDENT_STATUSES = frozenset(
    {"resolved", "dismissed", "recovered", "false_alarm"}
)


def window_id(when: datetime) -> str:
    """Minute bucket used in the overwatch receipt dedupe key."""
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    return when.astimezone(timezone.utc).strftime("%Y%m%d%H%M")


def overwatch_dedupe_key(project_id: str, when: datetime) -> str:
    return f"ai_sre:overwatch:{project_id}:{window_id(when)}"


def empty_result(
    *,
    project_id: str,
    actor: str,
    when: datetime,
    reason: str = "no_registered_sources",
    status: str = "empty",
) -> dict[str, Any]:
    return {
        "status": status,
        "reason": reason,
        "project_id": project_id,
        "actor": actor,
        "window": window_id(when),
        "dedupe_key": overwatch_dedupe_key(project_id, when),
        "observe_only": True,
        "sources": [],
        "scanned": [],
        "incidents": [],
        "diagnosed": [],
        "paged": [],
        "remediated": [],
        "auto_applied_decisions": [],
        "merged_prs": [],
        "receipt_id": None,
        "reused": False,
    }


def _project_scope(connection: Connection, project_id: str) -> Optional[dict[str, str]]:
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
        return None
    return {"org_id": row["org_id"], "workspace_id": row["workspace_id"]}


def _source_spec(source: dict[str, Any]) -> dict[str, Any]:
    config = source.get("config") or {}
    if isinstance(config, str):
        try:
            config = json.loads(config)
        except json.JSONDecodeError:
            config = {}
    if not isinstance(config, dict):
        config = {}
    return {"kind": source.get("kind") or "memory", **config}


def _has_investigation(incident: dict[str, Any]) -> bool:
    for item in incident.get("evidence") or []:
        if not isinstance(item, dict):
            continue
        if item.get("kind") == "rca_hypothesis":
            return True
    return False


def _needs_diagnosis(incident: dict[str, Any]) -> bool:
    status = str(incident.get("status") or "")
    if status in TERMINAL_INCIDENT_STATUSES:
        return False
    if _has_investigation(incident):
        return False
    severity = str(incident.get("severity") or "")
    return status == "open" or severity == "critical"


def _result_from_receipt(
    receipt: dict[str, Any],
    *,
    project_id: str,
    actor: str,
    when: datetime,
    dedupe_key: str,
) -> dict[str, Any]:
    stored = receipt.get("result")
    if isinstance(stored, str):
        try:
            stored = json.loads(stored)
        except json.JSONDecodeError:
            stored = None
    if isinstance(stored, dict) and stored.get("status"):
        out = dict(stored)
        out["reused"] = True
        out["receipt_id"] = receipt.get("id")
        out["dedupe_key"] = receipt.get("dedupe_key") or dedupe_key
        out["observe_only"] = True
        out["remediated"] = []
        out["auto_applied_decisions"] = []
        out["merged_prs"] = []
        return out
    fallback = empty_result(
        project_id=project_id, actor=actor, when=when, reason="in_progress"
    )
    fallback["status"] = "observed" if receipt.get("status") == "succeeded" else "in_progress"
    fallback["receipt_id"] = receipt.get("id")
    fallback["reused"] = True
    fallback["dedupe_key"] = receipt.get("dedupe_key") or dedupe_key
    return fallback


def _safe_list_sources(reliability: ReliabilityService, connection: Connection,
                       project_id: str) -> tuple[list[dict[str, Any]], Optional[str]]:
    try:
        return list(reliability.list_sources(connection, project_id, enabled_only=True)), None
    except Exception as exc:  # noqa: BLE001 — missing table / empty project is honest empty
        LOGGER.debug("ai_sre_overwatch_list_sources_failed", exc_info=True)
        return [], f"sources_unavailable:{exc.__class__.__name__}"


def _observe(
    connection: Connection,
    *,
    settings: Any,
    project_id: str,
    actor: str,
    when: datetime,
    reliability: ReliabilityService,
    oncall: OncallSreService,
    sources: list[dict[str, Any]],
) -> dict[str, Any]:
    scan_window = {
        "start": (when - SCAN_LOOKBACK).timestamp(),
        "end": when.timestamp(),
    }
    scanned: list[dict[str, Any]] = []
    for source in sources:
        spec = _source_spec(source)
        try:
            report = reliability.scan(
                connection,
                project_id,
                spec,
                scan_window,
                source_id=source.get("id"),
                persist=True,
            )
            incidents = report.get("incidents") or []
            scanned.append({
                "source_id": source.get("id"),
                "name": source.get("name"),
                "kind": source.get("kind"),
                "persist": True,
                "signal_count": report.get("signal_count"),
                "incident_ids": [item.get("id") for item in incidents if isinstance(item, dict)],
            })
        except Exception as exc:  # noqa: BLE001 — one broken source must not kill the tick
            LOGGER.debug("ai_sre_overwatch_scan_failed source=%s", source.get("id"), exc_info=True)
            scanned.append({
                "source_id": source.get("id"),
                "name": source.get("name"),
                "kind": source.get("kind"),
                "error": f"{exc.__class__.__name__}: {str(exc)[:200]}",
            })

    try:
        board = reliability.list_incidents(connection, project_id, limit=100)
    except Exception as exc:  # noqa: BLE001
        LOGGER.debug("ai_sre_overwatch_list_incidents_failed", exc_info=True)
        board = []
        scanned.append({"error": f"list_incidents:{exc.__class__.__name__}"})

    diagnosed: list[dict[str, Any]] = []
    paged: list[dict[str, Any]] = []
    for raw in board:
        if not isinstance(raw, dict):
            continue
        incident_id = raw.get("id")
        if not incident_id:
            continue
        try:
            detail = reliability.get_incident(connection, project_id, incident_id) or raw
        except Exception:  # noqa: BLE001
            detail = raw
        if not _needs_diagnosis(detail):
            continue
        try:
            diagnosis = reliability.diagnose(connection, project_id, incident_id)
        except Exception as exc:  # noqa: BLE001
            LOGGER.debug("ai_sre_overwatch_diagnose_failed incident=%s", incident_id, exc_info=True)
            diagnosed.append({
                "incident_id": incident_id,
                "error": f"{exc.__class__.__name__}: {str(exc)[:200]}",
            })
            continue
        if not diagnosis:
            continue
        hypothesis = diagnosis.get("hypothesis") if isinstance(diagnosis, dict) else None
        diagnosed.append({
            "incident_id": incident_id,
            "service": (diagnosis or {}).get("service") or detail.get("service"),
            "hypothesis_basis": (hypothesis or {}).get("basis") if isinstance(hypothesis, dict) else None,
            "cause_key": (hypothesis or {}).get("cause_key") if isinstance(hypothesis, dict) else None,
        })

    for raw in board:
        if not isinstance(raw, dict):
            continue
        if str(raw.get("severity") or "") != "critical":
            continue
        if str(raw.get("status") or "") in TERMINAL_INCIDENT_STATUSES:
            continue
        incident_id = raw.get("id")
        if not incident_id:
            continue
        try:
            page = oncall.page_reliability_incident(
                connection,
                project_id,
                incident_id,
                actor=actor,
                force_live=False,
                dedupe_key=f"oncall.page:{project_id}:{incident_id}:critical",
            )
            notify = (page or {}).get("notify") or {}
            paged.append({
                "incident_id": incident_id,
                "page_id": ((page or {}).get("page") or {}).get("id"),
                "reused": bool((page or {}).get("reused")),
                "simulated": bool((page or {}).get("simulated", True)),
                "notify_reason": notify.get("reason") or notify.get("status"),
            })
        except OncallSreError as exc:
            paged.append({
                "incident_id": incident_id,
                "error": str(exc)[:200],
                "status_code": getattr(exc, "status_code", None),
            })
        except Exception as exc:  # noqa: BLE001
            LOGGER.debug("ai_sre_overwatch_page_failed incident=%s", incident_id, exc_info=True)
            paged.append({
                "incident_id": incident_id,
                "error": f"{exc.__class__.__name__}: {str(exc)[:200]}",
            })

    # Policy is read for visibility only. Overwatch never remediates even if
    # auto_remediate is already true on the project.
    try:
        policy = reliability.get_policy(connection, project_id)
    except Exception:  # noqa: BLE001
        policy = {"auto_remediate": False}

    return {
        "status": "observed",
        "reason": None,
        "project_id": project_id,
        "actor": actor,
        "window": window_id(when),
        "dedupe_key": overwatch_dedupe_key(project_id, when),
        "observe_only": True,
        "sources": [
            {"id": src.get("id"), "kind": src.get("kind"), "name": src.get("name")}
            for src in sources
        ],
        "scanned": scanned,
        "incidents": [
            {
                "id": row.get("id"),
                "title": row.get("title"),
                "service": row.get("service"),
                "severity": row.get("severity"),
                "status": row.get("status"),
            }
            for row in board
            if isinstance(row, dict)
        ],
        "diagnosed": diagnosed,
        "paged": paged,
        "remediated": [],
        "auto_applied_decisions": [],
        "merged_prs": [],
        "policy_auto_remediate": bool((policy or {}).get("auto_remediate")),
        "scan_window": scan_window,
    }


def tick(
    engine: Engine,
    settings: Any,
    *,
    project_id: str,
    actor: str = "metatron",
    now: Optional[datetime] = None,
    reliability: Optional[ReliabilityService] = None,
    oncall: Optional[OncallSreService] = None,
    receipts: Optional[ReceiptService] = None,
    scope: Optional[dict[str, str]] = None,
) -> dict[str, Any]:
    """Deterministic observe-only overwatch tick. Receipted; never remediates."""
    when = now or now_utc()
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    project_id = str(project_id or "").strip() or str(
        getattr(settings, "default_project_id", None) or "boss-dogfood"
    )
    actor = str(actor or "metatron").strip() or "metatron"
    reliability = reliability or ReliabilityService(settings)
    oncall = oncall or OncallSreService(settings)
    receipts = receipts or ReceiptService()
    dedupe_key = overwatch_dedupe_key(project_id, when)

    with engine.begin() as connection:
        resolved_scope = scope or _project_scope(connection, project_id)
        if not resolved_scope:
            result = empty_result(
                project_id=project_id, actor=actor, when=when,
                reason="project_not_found", status="failed",
            )
            return result

        existing = receipts.get_by_dedupe_key(
            connection, resolved_scope["org_id"], dedupe_key
        )
        if existing:
            return _result_from_receipt(
                existing, project_id=project_id, actor=actor,
                when=when, dedupe_key=dedupe_key,
            )

        sources, source_error = _safe_list_sources(reliability, connection, project_id)
        if not sources:
            result = empty_result(
                project_id=project_id, actor=actor, when=when,
                reason=source_error or "no_registered_sources",
            )
        else:
            result = _observe(
                connection,
                settings=settings,
                project_id=project_id,
                actor=actor,
                when=when,
                reliability=reliability,
                oncall=oncall,
                sources=sources,
            )

        receipt, created = receipts.create_or_reuse(
            connection,
            resolved_scope["org_id"],
            resolved_scope["workspace_id"],
            project_id,
            dedupe_key,
            OVERWATCH_EFFECT_KIND,
            {"project_id": project_id, "window": result["window"]},
            {
                "status": result["status"],
                "reason": result.get("reason"),
                "observe_only": True,
                "scanned": len(result.get("scanned") or []),
                "diagnosed": len(result.get("diagnosed") or []),
                "paged": len(result.get("paged") or []),
            },
        )
        if not created:
            return _result_from_receipt(
                receipt, project_id=project_id, actor=actor,
                when=when, dedupe_key=dedupe_key,
            )

        result["receipt_id"] = receipt.get("id")
        result["reused"] = False
        result["dedupe_key"] = dedupe_key
        try:
            receipts.mark_succeeded(
                connection,
                receipt["id"],
                provider_ref=dedupe_key,
                result={
                    k: result[k]
                    for k in (
                        "status", "reason", "project_id", "actor", "window",
                        "dedupe_key", "observe_only", "sources", "scanned",
                        "incidents", "diagnosed", "paged", "remediated",
                        "auto_applied_decisions", "merged_prs",
                    )
                    if k in result
                },
            )
        except Exception:  # noqa: BLE001
            LOGGER.debug("ai_sre_overwatch_receipt_mark_failed", exc_info=True)
            result["receipt_mark_error"] = "mark_succeeded_failed"

        try:
            emit_event(
                connection,
                project_id,
                "ai_sre.overwatch.ticked",
                actor,
                "receipt",
                receipt.get("id") or "none",
                {
                    "status": result["status"],
                    "reason": result.get("reason"),
                    "scanned": len(result.get("scanned") or []),
                    "diagnosed": len(result.get("diagnosed") or []),
                    "paged": len(result.get("paged") or []),
                    "observe_only": True,
                    "receipt_id": receipt.get("id"),
                    "dedupe_key": dedupe_key,
                },
            )
        except Exception as exc:  # noqa: BLE001 — visible, never silent
            result["event_error"] = f"{exc.__class__.__name__}: {str(exc)[:160]}"

        return result


def safe_tick(engine: Engine, settings: Any, **kwargs: Any) -> dict[str, Any]:
    """Worker entry: never raise. Fail-closed empty on unexpected errors."""
    try:
        return tick(engine, settings, **kwargs)
    except Exception as exc:  # noqa: BLE001
        LOGGER.debug("ai_sre_overwatch_tick_failed", exc_info=True)
        project_id = str(kwargs.get("project_id") or getattr(settings, "default_project_id", None)
                         or "boss-dogfood")
        when = kwargs.get("now") or now_utc()
        out = empty_result(
            project_id=project_id,
            actor=str(kwargs.get("actor") or "metatron"),
            when=when,
            reason="tick_failed",
            status="failed",
        )
        out["error"] = f"{exc.__class__.__name__}: {str(exc)[:200]}"
        return out
