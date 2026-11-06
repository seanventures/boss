"""What an accepted recommendation BECOMES — through the normal paths only (design §14.4).

    ticket / epic  a work item on the work board (work_orchestration.upsert_work_item: the same
                   scoring, the release board picks it up), plus an issue in the project's
                   connected work tracker (Plane by default) when one is connected
    prd            a governed PRD panel (meeting_service.convene_prd_meeting_core — the path
                   request_meeting takes): agenda, minutes and a PRD for a person to approve

Nothing here builds anything: an accepted recommendation is roadmap work a person chose, and it
enters BOSS the same way a person's own idea would.
"""
from __future__ import annotations

from typing import Any, Mapping

from sqlalchemy.engine import Connection

from ._common import clean_text


def _description(rec: Mapping[str, Any]) -> str:
    lines = [clean_text(rec["why"], 600), "", "Evidence:"]
    for e in (rec.get("evidence") or [])[:12]:
        note = f" — {e['note']}" if e.get("note") else ""
        lines.append(f"- {e['kind']}:{e['ref']}{note}")
    lines += ["", f"Recommended by BOSS ({rec['detector']}); roadmap item {rec['work_item_id']}."]
    return "\n".join(lines)


def create(connection: Connection, rec: Mapping[str, Any], *, shape: str, actor: str,
           settings: Any = None, plane: bool = True) -> dict[str, Any]:
    from ...config import get_settings

    settings = settings or get_settings()
    if shape == "prd":
        return {"meeting_run_id": _prd_meeting(connection, rec, actor=actor,
                                               settings=settings)}
    from ..work_orchestration import WorkOrchestrationService

    item = WorkOrchestrationService(settings).upsert_work_item(
        connection, project_id=rec["project_id"], subject_type="recommendation",
        subject_id=rec["work_item_id"], title=clean_text(rec["title"], 300),
        description=_description(rec), strategic_value=int(float(rec.get("value") or 50)),
        urgency=int(100 * float(rec.get("freshness") or 0.5)),
        confidence=int(100 * float(rec.get("evidence_strength") or 0.75)),
        rationale=f"Accepted from the Recommended roadmap as {shape}",
        workload_class=f"roadmap_{shape}", estimated_units=int(rec.get("effort_units") or 1),
        evidence=[{"kind": e["kind"], "ref": e["ref"]} for e in (rec.get("evidence") or [])[:20]],
        source="roadmap", actor=actor.split(":", 1)[-1], owner_actor=actor)
    out: dict[str, Any] = {"work_item_id": item["id"]}
    if plane:
        issue = _tracker_issue(connection, rec, item["id"], shape)
        if issue:
            out.update(issue)
    return out


def _prd_meeting(connection: Connection, rec: Mapping[str, Any], *, actor: str,
                 settings: Any) -> str:
    from ..budget import BudgetGovernor
    from ..intake import CommandIntakeService
    from ..jobs import JobLeaseService
    from ..meeting_service import convene_prd_meeting_core

    directive = f"{clean_text(rec['title'], 300)}\n\n{_description(rec)}"
    result = convene_prd_meeting_core(
        connection, settings=settings, budget=BudgetGovernor(settings),
        intake=CommandIntakeService(settings), jobs=JobLeaseService(settings),
        project_id=rec["project_id"], directive=directive,
        constraints={"source": "roadmap", "recommendation_id": rec["work_item_id"],
                     "detector": rec["detector"], "accepted_by": actor},
        role_keys=[], title=clean_text(rec["title"], 200))
    return str(result["run_id"])


def _tracker_issue(connection: Connection, rec: Mapping[str, Any], work_item_id: str,
                   shape: str) -> dict[str, Any]:
    """Best effort: a connected tracker gets the issue; no tracker, no issue, no error."""
    from ..work_orchestration import connected_tracker, with_credential
    from ..work_tracker_adapters import get_adapter

    row = connected_tracker(connection, rec["project_id"])
    if row is None:
        return {}
    try:
        config = with_credential(connection, rec["project_id"], dict(row["configuration"] or {}))
        body = _description(rec)
        made = get_adapter(str(row["provider_key"])).create_issue(
            config, title=f"[{shape}] {clean_text(rec['title'], 190)}",
            description_html=body.replace("\n", "<br>"), description_text=body)
    except Exception as exc:  # noqa: BLE001 — the work item stands; the tracker is a mirror
        return {"tracker_error": f"{type(exc).__name__}: {exc}"[:200]}
    if made is None:
        return {"tracker_error": "the tracker did not create the issue"}
    return {"plane_issue": made.external_key or made.external_issue_id,
            "tracker": str(row["provider_key"]), "tracker_url": made.external_url,
            "tracker_work_item_id": work_item_id}
