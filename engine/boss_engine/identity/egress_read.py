"""A project-scoped read of egress decisions.

identity_egress_log also stores the run's jti and chain, so an agent never reads that
table. The columns here — when, which run, provider, method, path template, status,
decision, deny reason — hold no credential. The reason is still passed through the
agent redactor, and a row is returned only when its run belongs to this project.
"""
from __future__ import annotations

from typing import Any, Optional

from sqlalchemy import text

_COLUMNS = ("ts", "run_id", "provider", "method", "path_template", "status",
            "decision", "deny_reason")


def for_project(connection: Any, project_id: str,
                run_id: Optional[str] = None) -> list[dict]:
    """Decisions for this project's runs, newest first. `run_id` narrows to one run."""
    from ..services.agent_baseline import redact_for_agent

    rows = connection.execute(
        text(
            "SELECT e.ts AS ts, e.run_id AS run_id, e.provider AS provider, "
            "e.method AS method, e.path_template AS path_template, e.status AS status, "
            "e.decision AS decision, e.deny_reason AS deny_reason "
            "FROM identity_egress_log e "
            "JOIN code_task_runs r ON r.id = e.run_id "
            "WHERE r.project_id = :p AND (:run = '' OR e.run_id = :run) "
            "ORDER BY e.ts DESC LIMIT 50"
        ),
        {"p": project_id, "run": run_id or ""},
    ).mappings().all()
    out: list[dict] = []
    for row in rows:
        item = {key: row[key] for key in _COLUMNS}
        reason = item.get("deny_reason")
        if reason:
            item["deny_reason"] = redact_for_agent(str(reason))
        out.append(item)
    return out


def finding(row: Optional[dict]) -> Optional[str]:
    """One sentence for a denial. An allow is silence."""
    if not row:
        return None
    decision = str(row.get("decision") or "")
    reason = row.get("deny_reason")
    if decision == "allowed" or (decision != "denied" and not reason):
        return None
    method = str(row.get("method") or "").strip()
    path = str(row.get("path_template") or "").strip()
    where = f"{method} {path}".strip()
    status = row.get("status")
    why = reason or decision or "denied"
    return f"Egress {decision or 'denied'} {where} ({status}): {why}".strip()
