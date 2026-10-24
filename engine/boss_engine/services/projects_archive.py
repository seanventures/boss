"""Archiving a project (BOSSAPP-768 S5; KNOWLEDGE_GRAPH_PRODUCT_SPEC.md §2.7).

An archived project keeps everything it holds and takes no new work:

* it is hidden from GET /v1/projects unless the caller asks for archived ones;
* a new workflow run, code task, schedule, index request or job is refused with
  `ProjectArchived` (the API answers 409 project_archived, in words);
* every scheduler skips it: the job lease (`jobs.claim_job`: queued jobs wait), repository indexing, lifecycle ingest and re-embedding, workflow schedules, agent wakeups, the
  monitor scheduler and the deployment health sweep. Queued work stays queued and resumes on
  restore; nothing is cancelled or deleted.

THE ONE PATH. The API route (apps/api/app/routers/projects_archive.py) and the agent's
archive_project / restore_project tools both call `archive` / `restore` here, so a press and an
ask are the same act, audited and evented the same way.

THE ONE GUARD. Schedulers call `is_archived` / `drop_archived`, or put `live_sql()` in the query
that picks their work. `engine/tests/test_every_scheduler_skips_archived_projects.py` reads every
scheduler entry point and every route the worker calls, so a new one cannot forget.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Iterable, Optional

from sqlalchemy import text

from .core import emit_event

__all__ = [
    "ProjectArchived", "ArchiveRefused", "live_sql", "is_archived", "drop_archived",
    "refuse_new_work", "skipped", "live_ids", "state", "what_stops", "archive", "restore",
]


class ProjectArchived(Exception):
    """New work asked of an archived project. Not a ValueError on purpose: a route that maps
    ValueError to 422 must not swallow it; the API answers 409 with `detail()`."""

    def __init__(self, project_id: str, name: Optional[str] = None, what: str = "new work",
                 archived_at: Optional[datetime] = None) -> None:
        self.project_id = project_id
        self.name = name or project_id
        self.what = what
        self.archived_at = archived_at
        super().__init__(
            f"Project {self.name} is archived, so it takes no {what}. Restore it from the "
            "project menu (Show archived) to start work in it again.")

    def detail(self) -> dict[str, Any]:
        return {"error": "project_archived", "message": str(self), "project_id": self.project_id,
                "archived_at": self.archived_at.isoformat() if self.archived_at else None}


class ArchiveRefused(ValueError):
    """An archive this project cannot take (the install's home project, the org's last one)."""


def live_sql(column: str = "project_id") -> str:
    """A WHERE fragment true when `column` names a project that is not archived (null-safe: a row
    with no project is never treated as archived). For the query a scheduler picks work with."""
    return (f"NOT EXISTS (SELECT 1 FROM projects archived_p WHERE archived_p.id = {column} "
            "AND archived_p.archived_at IS NOT NULL)")


def is_archived(connection: Any, project_id: Optional[str]) -> bool:
    if not project_id:
        return False
    return bool(connection.execute(text(
        "SELECT archived_at IS NOT NULL FROM projects WHERE id = :p"), {"p": project_id}).scalar())


def drop_archived(connection: Any, rows: Iterable[Any], key: str = "project_id") -> list:
    """`rows` without those whose project is archived (one query for the lot)."""
    rows = list(rows)
    ids = sorted({str(r[key]) for r in rows if r[key]})
    if not ids:
        return rows
    archived = set(connection.execute(text(
        "SELECT id FROM projects WHERE id = ANY(:ids) AND archived_at IS NOT NULL"),
        {"ids": ids}).scalars().all())
    return [r for r in rows if str(r[key]) not in archived] if archived else rows


def live_ids(connection: Any, project_ids: Iterable[Optional[str]]) -> list[str]:
    """`project_ids` (order kept) without the archived ones."""
    return [r["project_id"] for r in drop_archived(
        connection, [{"project_id": p} for p in project_ids if p])]


def state(connection: Any, project_id: str) -> Optional[dict[str, Any]]:
    """The project's row with its archive columns, or None when there is no such project."""
    row = connection.execute(text(
        "SELECT p.id, p.name, p.slug, p.workspace_id, p.created_at, p.archived_at, p.archived_by, "
        "w.name AS workspace_name, o.id AS org_id, o.name AS org_name FROM projects p "
        "JOIN workspaces w ON w.id = p.workspace_id JOIN orgs o ON o.id = w.org_id "
        "WHERE p.id = :p"), {"p": project_id}).mappings().first()
    return dict(row) if row else None


def refuse_new_work(connection: Any, project_id: Optional[str], what: str = "new work") -> None:
    """Raise ProjectArchived when `project_id` is archived. Call it before creating new work."""
    if not project_id:
        return
    row = connection.execute(text(
        "SELECT name, archived_at FROM projects WHERE id = :p"), {"p": project_id}).mappings().first()
    if row and row["archived_at"] is not None:
        raise ProjectArchived(project_id, row["name"], what, row["archived_at"])


def skipped(project_id: str) -> dict[str, Any]:
    """What a per-project tick answers for an archived project (merged into its usual shape)."""
    return {"project_id": project_id, "archived": True,
            "note": f"project {project_id} is archived; nothing ran"}


def _count(connection: Any, sql: str, project_id: str) -> int:
    try:
        with connection.begin_nested():
            return int(connection.execute(text(sql), {"p": project_id}).scalar() or 0)
    except Exception:  # noqa: BLE001 - a table an older install lacks counts as none
        return 0


def what_stops(connection: Any, project_id: str) -> dict[str, Any]:
    """What archiving pauses, counted, with the sentences the confirm dialog shows."""
    counts = {
        "schedules": _count(connection, "SELECT count(*) FROM workflow_schedules "
                                        "WHERE project_id = :p AND enabled", project_id),
        "wakeups": _count(connection, "SELECT count(*) FROM agent_wakeups "
                                      "WHERE project_id = :p AND status = 'active'", project_id),
        "monitors": _count(connection, "SELECT count(*) FROM rule_groups WHERE project_id = :p",
                           project_id),
        "repositories": _count(connection, "SELECT count(*) FROM github_repository_bindings "
                                           "WHERE project_id = :p AND status = 'active'", project_id),
        "queued_jobs": _count(connection, "SELECT count(*) FROM jobs WHERE project_id = :p "
                                          "AND status IN ('queued', 'failed_retryable')", project_id),
        "running_jobs": _count(connection, "SELECT count(*) FROM jobs WHERE project_id = :p "
                                           "AND status IN ('leased', 'running')", project_id),
    }

    def n(k: int, one: str, many: str) -> str:
        return f"{k} {one if k == 1 else many}"

    stops = ["New builds, workflow runs, code tasks and schedules are refused."]
    if counts["schedules"]:
        stops.append(f"{n(counts['schedules'], 'workflow schedule', 'workflow schedules')} stop firing.")
    if counts["wakeups"]:
        stops.append(f"{n(counts['wakeups'], 'agent watch or schedule', 'agent watches and schedules')} "
                     "stop waking.")
    if counts["monitors"]:
        stops.append(f"Monitoring stops for {n(counts['monitors'], 'app', 'apps')}; no alerts are raised.")
    if counts["repositories"]:
        stops.append(f"Code indexing stops for {n(counts['repositories'], 'repository', 'repositories')}.")
    if counts["queued_jobs"]:
        stops.append(f"{n(counts['queued_jobs'], 'queued job waits', 'queued jobs wait')} until you restore it.")
    if counts["running_jobs"]:
        stops.append(f"{n(counts['running_jobs'], 'job already running finishes', 'jobs already running finish')}; "
                     "nothing new starts after it.")
    keeps = ["Everything stays readable: runs, apps, the knowledge graph, decisions and costs.",
             "Nothing is deleted. Restore brings it all back."]
    return {"project_id": project_id, "counts": counts, "stops": stops, "keeps": keeps}


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _home_project(settings: Any) -> str:
    return str(getattr(settings, "default_project_id", "") or "").strip()


def _audit(connection: Any, auth: Any, event_type: str, row: dict, permission: str) -> None:
    if auth is None:
        return
    from ..identity import audit

    audit.record(audit.SecurityEvent.from_context(
        auth, event_type, audit.Outcome.SUCCESS, target_type="project", target_id=row["id"],
        permission=permission, scope=f"org:{row['org_id']}",
        details={"name": row["name"], "org_id": row["org_id"]}), db=connection)


def archive(connection: Any, settings: Any, *, project_id: str, actor: str, auth: Any = None,
            note: Optional[str] = None) -> dict[str, Any]:
    """Archive the project. Idempotent: an archived project is returned as it is. Raises
    LookupError (no such project) or ArchiveRefused (the install's home project, or the last
    project of its organization that is not archived)."""
    row = state(connection, project_id)
    if row is None:
        raise LookupError(f"There is no project {project_id}.")
    if row["archived_at"] is not None:
        return {**row, "changed": False, **what_stops(connection, project_id)}
    if project_id == _home_project(settings):
        raise ArchiveRefused(
            f"{row['name']} is this install's home project, where BOSS's own agent and the worker's "
            "schedules live, so it cannot be archived.")
    others = connection.execute(text(
        "SELECT count(*) FROM projects p JOIN workspaces w ON w.id = p.workspace_id "
        "WHERE w.org_id = :o AND p.id <> :p AND p.archived_at IS NULL"),
        {"o": row["org_id"], "p": project_id}).scalar()
    if not others:
        raise ArchiveRefused(
            f"{row['name']} is the only open project in {row['org_name']}. Create or restore "
            "another project first, so the organization still has one to work in.")
    stops = what_stops(connection, project_id)
    when = _now()
    connection.execute(text(
        "UPDATE projects SET archived_at = :t, archived_by = :a WHERE id = :p AND archived_at IS NULL"),
        {"t": when, "a": actor, "p": project_id})
    emit_event(connection, project_id, "project.archived", actor, "project", project_id,
               {"name": row["name"], "org_id": row["org_id"], "note": note,
                "paused": stops["counts"],
                "principal_id": getattr(getattr(auth, "principal", None), "id", None)})
    from ..identity.permissions_gen import P

    _audit(connection, auth, "project.archived", row, P.PROJECT_DELETE.value)
    return {**row, "archived_at": when, "archived_by": actor, "changed": True, **stops}


def restore(connection: Any, settings: Any, *, project_id: str, actor: str,
            auth: Any = None) -> dict[str, Any]:
    """Restore an archived project: its schedules, monitors and queued work resume on the next
    tick. Idempotent. Raises LookupError for no such project."""
    row = state(connection, project_id)
    if row is None:
        raise LookupError(f"There is no project {project_id}.")
    if row["archived_at"] is None:
        return {**row, "changed": False}
    connection.execute(text(
        "UPDATE projects SET archived_at = NULL, archived_by = NULL WHERE id = :p"), {"p": project_id})
    emit_event(connection, project_id, "project.restored", actor, "project", project_id,
               {"name": row["name"], "org_id": row["org_id"],
                "archived_at": row["archived_at"].isoformat(), "archived_by": row["archived_by"],
                "principal_id": getattr(getattr(auth, "principal", None), "id", None)})
    from ..identity.permissions_gen import P

    _audit(connection, auth, "project.restored", row, P.PROJECT_DELETE.value)
    return {**row, "archived_at": None, "archived_by": None, "changed": True}
