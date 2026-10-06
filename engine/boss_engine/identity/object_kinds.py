"""Object loaders: how `Scope.of(kind, ...)` finds an object's org and project (IDENTITY.md §6.3).

Stream S2. One entry per object kind a route declares. The loader answers (org_id, project_id) for
an id, or None when the object does not exist, which the enforcement point answers as 404. The org
comes from the object's project (projects -> workspaces -> orgs), never from the request.

Each kind is a table, its id column and the column holding the project (or a SQL expression over
the table aliased `t` for an indirect project). The SQL is built only from this module's constants;
the object id is always a bound parameter.

`test_identity_authz_object_kinds` runs every loader against the real schema (a missing table or
column fails there, not in production), and the coverage test fails on a declared kind with no
loader.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional

from sqlalchemy import text

from . import scope as scope_module
from .epoch import connection


@dataclass(frozen=True)
class ObjectKind:
    kind: str
    table: str
    id_column: str = "id"
    #: a column of `table`, or a SQL expression over alias `t` that yields the project id
    project: str = "t.project_id"

    def sql(self) -> str:
        return (f"SELECT w.org_id, p.id FROM {self.table} t "
                f"JOIN projects p ON p.id = ({self.project}) "
                f"JOIN workspaces w ON w.id = p.workspace_id "
                f"WHERE t.{self.id_column}::text = :id LIMIT 1")

    def load(self, db: Any, object_id: str) -> Optional[tuple[str, str]]:
        with connection(db) as conn:
            row = conn.execute(text(self.sql()), {"id": str(object_id)}).first()
        return (str(row[0]), str(row[1])) if row else None


class UniqueProjectKind(ObjectKind):
    """An object found by a non-unique key (a dedupe key, a sandbox task id): it resolves only when
    every row the key matches is in ONE project; anything else is answered as not found."""

    def __init__(self, kind: str, sql: str) -> None:
        object.__setattr__(self, "kind", kind)
        object.__setattr__(self, "table", "")
        object.__setattr__(self, "id_column", "")
        object.__setattr__(self, "project", "")
        object.__setattr__(self, "_projects_sql", sql)

    def sql(self) -> str:
        return ("SELECT w.org_id, p.id FROM projects p JOIN workspaces w ON w.id = p.workspace_id "
                f"WHERE p.id IN ({self._projects_sql})")

    def load(self, db: Any, object_id: str) -> Optional[tuple[str, str]]:
        with connection(db) as conn:
            rows = conn.execute(text(self.sql()), {"id": str(object_id)}).all()
        return (str(rows[0][0]), str(rows[0][1])) if len(rows) == 1 else None


class SecurityScanRunKind(ObjectKind):
    """Security-scan runs live in the engine's in-process registry, not a table."""

    def __init__(self) -> None:
        object.__setattr__(self, "kind", "security_scan_run")
        object.__setattr__(self, "table", "")
        object.__setattr__(self, "id_column", "")
        object.__setattr__(self, "project", "")

    def sql(self) -> str:
        return ("SELECT w.org_id, p.id FROM projects p JOIN workspaces w ON w.id = p.workspace_id "
                "WHERE p.id = :id")

    def load(self, db: Any, object_id: str) -> Optional[tuple[str, str]]:
        from ..services import security_scan

        run = security_scan.get_run(str(object_id))
        project_id = (run or {}).get("project_id")
        if not project_id:
            return None
        with connection(db) as conn:
            row = conn.execute(text(self.sql()), {"id": str(project_id)}).first()
        return (str(row[0]), str(row[1])) if row else None


#: Every object kind a route declares with Scope.of(...). Filled from the retrofit review: each
#: entry was checked against the route's handler and db/schema.sql.
KINDS: tuple[ObjectKind, ...] = (
    ObjectKind("app_preview", "app_previews"),
    ObjectKind("artifact", "artifacts"),
    ObjectKind("break_glass_grant", "break_glass_grants"),
    ObjectKind("changeset", "changesets"),
    ObjectKind("code_task", "code_task_runs"),
    ObjectKind("deadletter", "deadletters"),
    ObjectKind("decision", "decisions"),
    ObjectKind("deployment_host", "deployment_hosts"),
    ObjectKind("deployment_promotion", "deployment_promotions"),
    ObjectKind("deployment_run", "deployment_runs"),
    ObjectKind("deployment_target", "deployment_targets"),
    ObjectKind("discovery_run", "discovery_runs"),
    ObjectKind("feedback_thread", "feedback_threads"),
    ObjectKind("improvement_program", "improvement_programs"),
    ObjectKind("improvement_trial", "improvement_trials"),
    ObjectKind("job", "jobs"),
    ObjectKind("secret_ref", "secret_refs"),
    ObjectKind("shepherd_escalation", "shepherd_escalations"),
    ObjectKind("stack_deployment", "stack_deployments"),
    ObjectKind("task", "tasks"),
    ObjectKind("workflow_run", "workflow_runs"),
    # int-b: ids that ride in bodies, queries and tool arguments (object_rules.Confine)
    ObjectKind("agent_definition", "agent_definitions"),
    ObjectKind("agent_memory", "agent_memory"),
    ObjectKind("agent_wakeup", "agent_wakeups"),
    ObjectKind("budget_reservation", "budget_reservations"),
    ObjectKind("github_connection", "github_connections"),
    ObjectKind("investigation", "investigations"),
    ObjectKind("meeting_run", "meeting_runs"),
    ObjectKind("plan", "plans"),
    #: a body's own project_id, confined to the project the scope resolved (Confine.body)
    ObjectKind("project", "projects", project="t.id"),
    ObjectKind("resource_allocation", "resource_allocations"),
    ObjectKind("work_item", "work_items"),
    ObjectKind("work_tracker_binding", "work_tracker_bindings"),
    ObjectKind("workflow_version", "workflow_versions"),
    # a meeting route's id may also name one of its runs (meetings.load_meeting_run)
    UniqueProjectKind("meeting", "SELECT project_id FROM meetings WHERE id = :id "
                                 "UNION SELECT project_id FROM meeting_runs WHERE id = :id"),
    # main.require_agent_thread_reader: the thread, else its ledger rows
    UniqueProjectKind("agent_thread", "SELECT project_id FROM agent_threads WHERE id = :id "
                                      "UNION SELECT project_id FROM agent_session_ledger "
                                      "WHERE thread_id = :id AND NOT EXISTS "
                                      "(SELECT 1 FROM agent_threads WHERE id = :id)"),
    # main.require_sandbox_reader: a host sandbox is known by the build or preview that made it
    UniqueProjectKind("sandbox", "SELECT project_id FROM code_task_runs "
                                 "WHERE finalize_ctx->>'sandbox_task_id' = :id "
                                 "UNION SELECT project_id FROM app_previews WHERE sandbox_task_id = :id"),
    # a bake-off batch is the builds that share finalize_ctx.bakeoff.batch_id
    UniqueProjectKind("bakeoff_batch", "SELECT project_id FROM code_task_runs "
                                       "WHERE finalize_ctx->'bakeoff'->>'batch_id' = :id"),
    # receipts.get_by_id_or_dedupe_key: by id, else by dedupe key (which must be in one project)
    UniqueProjectKind("receipt", "SELECT project_id FROM receipts WHERE id = :id "
                                 "UNION SELECT project_id FROM receipts WHERE dedupe_key = :id "
                                 "AND NOT EXISTS (SELECT 1 FROM receipts WHERE id = :id)"),
    SecurityScanRunKind(),
)

_BY_KIND: dict[str, ObjectKind] = {}


def register_all(kinds: tuple[ObjectKind, ...] = KINDS) -> None:
    for spec in kinds:
        if spec.kind in _BY_KIND:
            continue
        _BY_KIND[spec.kind] = spec
        scope_module.register_object_kind(spec.kind, spec.load)


def get(kind: str) -> ObjectKind:
    return _BY_KIND[kind]


def all_kinds() -> tuple[ObjectKind, ...]:
    return tuple(_BY_KIND.values())


register_all()
