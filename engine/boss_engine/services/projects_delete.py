"""Deleting a project, with a full cleanup (operator, 2026-11-08: "provide option to delete the
project … it should clean up all workflows, builds and other resources in database").

Archive (services/projects_archive.py) keeps everything and pauses it; DELETE takes it all away.
Only a person deletes (Where people decide › Deleting data): the API needs `project:delete`
(owner, step-up), the confirmation the console's dialog sends and the project's name typed back.

WHAT GOES, FOUND FROM THE LIVE SCHEMA, NOT A HAND LIST. `catalog()` reads the database's own
catalog (pg_class, pg_attribute, pg_constraint) in the current schema:

  * every table with a `project_id` column: its rows for this project;
  * every table reached from those through a foreign key (a run's steps, an incident's evidence,
    a change set's pull requests…): the rows that hang off the rows that go;
  * a few tables that name a project another way, declared in LOGICAL_KEYS (a budget policy's
    `scope_id`, a doctor action's `finding_id`), each checked against the catalog;
  * the project's knowledge-graph projection: its Apache AGE graph (one per project), dropped.

Rows are deleted children first (a topological order of the foreign keys; a cycle is broken by
clearing a nullable reference), all in ONE transaction: a failure anywhere rolls everything back.
A row of ANOTHER project that points at one of these (a repository binding on this project's
GitHub connection) is never taken with it: the shared row moves to the project that uses it, or
the reference is cleared when it may be empty.

WHAT STAYS. The org's security audit log (identity_security_events, hash-chained, append-only)
gets one more row, `project.deleted`, with the counts. Org-level rows stay: the organization, its
people and roles, its GitHub App and repository registry (github_repos), its vault. An access key,
approval delegation or grant naming this project is narrowed to its other projects, or revoked
when this was its only one (kept, revoked, as a security record); the project's Captain authority
is removed. Every table that is neither deleted nor kept by name fails
apps/api/tests/test_projects_delete_db.py, so a new table is classified when it is added.

OUTSIDE THE DATABASE, BEFORE IT. The project's deployed staging apps are removed from their hosts
(the removal code of services/app_removal.py: its compose projects, container and folder under
/opt/boss-apps); its previews are stopped; its queued and running jobs, builds and workflow runs
are cancelled. AFTER IT (services/project_files.py): what BOSS keeps on this machine for the
project — a local project's bare repositories (local_projects.delete_local_repo), its retained
workspaces, preview trees and logs, artifacts, and a GitHub mirror no other project uses — only
under BOSS-owned directories. A production app is never removed silently: the delete is refused ("Retire its
production first") unless the person ticks "also take down production". The GitHub repository,
its branches and pull requests are not touched.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable, Optional

from sqlalchemy import text

log = logging.getLogger(__name__)

__all__ = [
    "DeleteRefused", "Catalog", "catalog", "preview", "delete_project", "purge", "KINDS",
    "LOGICAL_KEYS", "KEPT_TABLES", "KEPT_PREFIXES", "HOST_REMOVER", "PREVIEW_STOPPER",
    "classify_tables",
]


class DeleteRefused(ValueError):
    """A delete this project cannot take, in words. `code` is the API's error code."""

    def __init__(self, code: str, message: str, status: int = 409, **extra: Any) -> None:
        super().__init__(message)
        self.code = code
        self.status = status
        self.extra = extra

    def detail(self) -> dict[str, Any]:
        return {"error": self.code, "message": str(self), **self.extra}


#: Tables that name a project WITHOUT a project_id column or a foreign key: (table, the SQL that
#: picks its rows for :p). Each is used only when the table and its columns exist.
LOGICAL_KEYS: dict[str, tuple[tuple[str, ...], str]] = {
    "budget_policies": (("scope_type", "scope_id"), "scope_type = 'project' AND scope_id = :p"),
    "identity_role_bindings": (("scope_type", "scope_id"), "scope_type = 'project' AND scope_id = :p"),
    # Where people decide: a project's own rules ('install' is the install's), and their history.
    "decide_rules": (("scope",), "scope = :p"),
    "decide_rule_changes": (("scope",), "scope = :p"),
    "experiment_assignments": (("experiment_id",),
                               "experiment_id IN (SELECT id FROM experiments WHERE project_id = :p)"),
    "signal_receipts": (("source_id",),
                        "source_id IN (SELECT id FROM signal_sources WHERE project_id = :p)"),
    "system_doctor_actions": (("finding_id",),
                              "finding_id IN (SELECT id FROM system_findings WHERE project_id = :p)"),
    "sandbox_violation_counts": (("lineage_id",),
                                 "lineage_id IN (SELECT lineage_id FROM lineage_sandboxes "
                                 "WHERE project_id = :p)"),
}

#: Tables kept as they are, by name, with why. Org-level, install-level or the security log.
KEPT_TABLES: dict[str, str] = {
    "projects": "the project row itself goes last, by its id",
    "orgs": "the organization", "workspaces": "the organization's workspaces",
    "org_members": "the organization's people", "org_settings": "the organization's settings",
    "github_repos": "the organization's repository registry (the repo itself is on GitHub)",
    "app_templates": "the organization's app templates", "app_template_files": "the same",
    "api_idempotency_keys": "the API's replay cache, expires on its own",
    "boss_starts": "this install's start log", "system_state": "this install's state",
    "infra_plugin_state": "install plugins", "kg_kind_layers": "the knowledge graph's kind table",
    "person_prompt_state": "a person's prompts, not a project's",
    "provider_adapters": "install model providers", "provider_model_catalog": "model catalogue",
    "subscription_accounts": "install model subscriptions", "secret_material": "the install vault",
    "signal_adapter_cursors": "install signal adapters", "workload_profiles": "install profiles",
    "schema_apply_runs": "schema history", "schema_once": "schema history",
}
#: Kept by prefix: identity (people, roles, keys, the security audit log), BOSS as an OIDC
#: provider, and the install's telemetry.
KEPT_PREFIXES: tuple[str, ...] = ("identity_", "oidc_", "tm_", "telemetry_")

#: What the dialog counts, by kind: (key, label, tables). The rest is "Other records".
KINDS: tuple[tuple[str, str, tuple[str, ...]], ...] = (
    ("workflow_runs", "Workflow runs", ("workflow_runs",)),
    ("builds", "Builds", ("code_task_runs",)),
    ("jobs", "Jobs", ("jobs",)),
    ("decisions", "Decisions", ("decisions",)),
    ("work_items", "Work items", ("work_items",)),
    ("conversations", "Conversations", ("agent_conversations", "agent_threads")),
    ("meetings", "Meetings", ("meeting_runs",)),
    ("previews", "Previews", ("app_previews",)),
    ("deploys", "Deploys", ("deployment_runs",)),
    ("apps", "Apps on servers", ("deployment_targets",)),
    ("alerts", "Alerts", ("app_alerts",)),
    ("incidents", "Incidents", ("incidents",)),
    ("kg_facts", "Knowledge-graph facts", ("kg_nodes", "kg_edges")),
    ("repositories", "Repository links", ("github_repository_bindings",)),
    ("workflows", "Workflows", ("workflow_definitions",)),
    ("events", "Activity events", ("events",)),
)
_KIND_OF = {t: k for k, _l, ts in KINDS for t in ts}
_LABEL = {k: label for k, label, _ in KINDS}
#: The same, for one ("1 build", never "1 builds").
_ONE = {"workflow_runs": "Workflow run", "builds": "Build", "jobs": "Job", "decisions": "Decision",
        "work_items": "Work item", "conversations": "Conversation", "meetings": "Meeting",
        "previews": "Preview", "deploys": "Deploy", "apps": "App on a server", "alerts": "Alert",
        "incidents": "Incident", "kg_facts": "Knowledge-graph fact",
        "repositories": "Repository link", "workflows": "Workflow", "events": "Activity event",
        "other": "Other record"}


# ── the catalog ──────────────────────────────────────────────────────────────────────────────

@dataclass
class Fk:
    child: str
    parent: str
    cols: tuple[str, ...]
    pcols: tuple[str, ...]
    on_delete: str          # a = no action, r = restrict, c = cascade, n = set null, d = default
    nullable: bool


@dataclass
class Catalog:
    tables: set[str]
    columns: dict[str, dict[str, tuple[bool, str]]]     # table -> column -> (not null, type)
    fks: list[Fk]
    direct: set[str] = field(default_factory=set)        # has project_id
    reached: set[str] = field(default_factory=set)       # through a foreign key only
    logical: dict[str, str] = field(default_factory=dict)

    @property
    def deleted(self) -> set[str]:
        return self.direct | self.reached | set(self.logical)

    def has(self, table: str, *cols: str) -> bool:
        return table in self.tables and all(c in self.columns.get(table, {}) for c in cols)


def catalog(connection: Any) -> Catalog:
    """What the live schema holds: tables, columns and foreign keys of the current schema."""
    rows = connection.execute(text(
        "SELECT c.relname, a.attname, a.attnotnull, format_type(a.atttypid, a.atttypmod) "
        "FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
        "JOIN pg_attribute a ON a.attrelid = c.oid AND a.attnum > 0 AND NOT a.attisdropped "
        "WHERE n.nspname = current_schema() AND c.relkind IN ('r', 'p') AND NOT c.relispartition"
    )).all()
    columns: dict[str, dict[str, tuple[bool, str]]] = {}
    for table, col, notnull, typ in rows:
        columns.setdefault(table, {})[col] = (bool(notnull), str(typ))
    tables = set(columns)
    fk_rows = connection.execute(text(
        "SELECT ch.relname, pa.relname, con.confdeltype, "
        "  ARRAY(SELECT a.attname FROM unnest(con.conkey) WITH ORDINALITY k(n, i) "
        "        JOIN pg_attribute a ON a.attrelid = con.conrelid AND a.attnum = k.n ORDER BY k.i), "
        "  ARRAY(SELECT a.attname FROM unnest(con.confkey) WITH ORDINALITY k(n, i) "
        "        JOIN pg_attribute a ON a.attrelid = con.confrelid AND a.attnum = k.n ORDER BY k.i) "
        "FROM pg_constraint con JOIN pg_class ch ON ch.oid = con.conrelid "
        "JOIN pg_class pa ON pa.oid = con.confrelid JOIN pg_namespace n ON n.oid = ch.relnamespace "
        "WHERE con.contype = 'f' AND n.nspname = current_schema()")).all()
    fks = []
    for child, parent, deltype, cols, pcols in fk_rows:
        if child not in tables or parent not in tables:
            continue
        nullable = all(not columns[child].get(c, (True, ""))[0] for c in cols)
        fks.append(Fk(child, parent, tuple(cols), tuple(pcols), str(deltype), nullable))
    cat = Catalog(tables=tables, columns=columns, fks=fks)
    cat.direct = {t for t in tables if "project_id" in columns[t] and t != "projects"}
    # Through foreign keys, from the project's rows: a table every row of which belongs to a
    # parent that goes. Kept tables never join (an access key's delegation is not the project's).
    by_parent: dict[str, list[Fk]] = {}
    for fk in fks:
        by_parent.setdefault(fk.parent, []).append(fk)
    seen = set(cat.direct) | {"projects"}
    frontier = list(seen)
    while frontier:
        t = frontier.pop()
        for fk in by_parent.get(t, []):
            if fk.child in seen or _kept_by_name(fk.child):
                continue
            seen.add(fk.child)
            cat.reached.add(fk.child)
            frontier.append(fk.child)
    for table, (cols, where) in LOGICAL_KEYS.items():
        if cat.has(table, *cols) and table not in seen:
            cat.logical[table] = where
    return cat


def _kept_by_name(table: str) -> bool:
    return table in KEPT_TABLES or table.startswith(KEPT_PREFIXES)


def classify_tables(cat: Catalog) -> dict[str, list[str]]:
    """Every table, as deleted (with how) or kept; `unclassified` must stay empty."""
    out: dict[str, list[str]] = {"direct": [], "reached": [], "logical": [], "kept": [],
                                 "unclassified": []}
    for t in sorted(cat.tables):
        if t in cat.direct:
            out["direct"].append(t)
        elif t in cat.reached:
            out["reached"].append(t)
        elif t in cat.logical:
            out["logical"].append(t)
        elif _kept_by_name(t):
            out["kept"].append(t)
        else:
            out["unclassified"].append(t)
    return out


def _q(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def _cols(cols: Iterable[str], alias: str = "") -> str:
    pre = f"{alias}." if alias else ""
    cs = [pre + _q(c) for c in cols]
    return cs[0] if len(cs) == 1 else "(" + ", ".join(cs) + ")"


class _Preds:
    """The WHERE clause that picks each table's rows for the project (`:p`)."""

    def __init__(self, cat: Catalog) -> None:
        self.cat = cat
        self.memo: dict[str, str] = {}
        self.into: dict[str, list[Fk]] = {}
        for fk in cat.fks:
            self.into.setdefault(fk.child, []).append(fk)

    def of(self, table: str, depth: int = 0) -> str:
        if table in self.memo:
            return self.memo[table]
        if table == "projects":
            pred = "id = :p"
        elif table in self.cat.direct:
            pred = "project_id = :p"
        elif table in self.cat.logical:
            pred = self.cat.logical[table]
        elif table in self.cat.reached and depth < 12:
            self.memo[table] = "FALSE"          # a cycle back to itself adds nothing
            parts = []
            for fk in self.into.get(table, []):
                if fk.parent == table or fk.parent not in self.cat.deleted | {"projects"}:
                    continue
                parent_pred = self.of(fk.parent, depth + 1)
                if parent_pred == "FALSE":
                    continue
                parts.append(f"{_cols(fk.cols)} IN (SELECT {_cols(fk.pcols)} FROM {_q(fk.parent)} "
                             f"WHERE {parent_pred})")
            pred = " OR ".join(f"({x})" for x in parts) if parts else "FALSE"
        else:
            pred = "FALSE"
        self.memo[table] = pred
        return pred


# ── counting (the dialog) ────────────────────────────────────────────────────────────────────

def _project(connection: Any, project_id: str) -> Optional[dict[str, Any]]:
    row = connection.execute(text(
        "SELECT p.id, p.name, p.slug, p.workspace_id, p.archived_at, w.org_id, o.name AS org_name "
        "FROM projects p JOIN workspaces w ON w.id = p.workspace_id JOIN orgs o ON o.id = w.org_id "
        "WHERE p.id = :p"), {"p": project_id}).mappings().first()
    return dict(row) if row else None


def _direct_counts(connection: Any, cat: Catalog, project_id: str) -> dict[str, int]:
    """Rows per table with this project's id, in one statement."""
    tables = sorted(cat.direct)
    out: dict[str, int] = {}
    for i in range(0, len(tables), 120):
        chunk = tables[i:i + 120]
        sql = " UNION ALL ".join(
            f"SELECT '{t}' AS t, count(*) AS n FROM {_q(t)} WHERE project_id = :p" for t in chunk)
        for t, n in connection.execute(text(sql), {"p": project_id}).all():
            if n:
                out[str(t)] = int(n)
    return out


def _by_kind(per_table: dict[str, int]) -> list[dict[str, Any]]:
    sums: dict[str, int] = {}
    other = 0
    for t, n in per_table.items():
        k = _KIND_OF.get(t)
        if k:
            sums[k] = sums.get(k, 0) + n
        else:
            other += n
    out = [{"key": k, "label": _ONE[k] if sums[k] == 1 else _LABEL[k], "count": sums[k]}
           for k, _l, _t in KINDS if sums.get(k)]
    if other:
        out.append({"key": "other", "label": _ONE["other"] if other == 1 else "Other records",
                    "count": other})
    return out


def _apps(connection: Any, project_id: str) -> list[dict[str, Any]]:
    """The project's apps on servers that are still up: name, environment, host."""
    rows = connection.execute(text(
        "SELECT t.id, t.app_name, t.environment, t.status, t.target_kind, t.target_ref, t.host_id, "
        "       h.name AS host_name FROM deployment_targets t "
        "LEFT JOIN deployment_hosts h ON h.id = t.host_id "
        "WHERE t.project_id = :p AND t.status = 'active' ORDER BY t.environment, t.app_name"),
        {"p": project_id}).mappings().all()
    out = []
    for r in rows:
        host = r["host_name"] or (str(r["target_ref"] or "").split("@")[-1].split(":")[0] or None)
        out.append({"target_id": r["id"], "app_name": r["app_name"], "environment": r["environment"],
                    "host": host, "production": _is_production(r["environment"])})
    return out


def _is_production(environment: Any) -> bool:
    return str(environment or "").strip().lower() in ("production", "prod", "live")


def _home(settings: Any) -> str:
    return str(getattr(settings, "default_project_id", "") or "").strip()


KEEPS = [
    "The repository on GitHub, its branches and pull requests: nothing there is touched.",
    "The organization's security audit log, with one more entry saying who deleted it.",
    "Your organization, its people, its GitHub connection and repository list.",
]


def preview(connection: Any, settings: Any, project_id: str) -> Optional[dict[str, Any]]:
    """What deleting the project would remove, counted by kind, and what stays. None when there is
    no such project. `refused` says in words why it cannot be deleted (the home project)."""
    row = _project(connection, project_id)
    if row is None:
        return None
    cat = catalog(connection)
    per_table = _direct_counts(connection, cat, project_id)
    apps = _apps(connection, project_id)
    staging = [a for a in apps if not a["production"]]
    production = [a for a in apps if a["production"]]
    kinds = _by_kind(per_table)
    kg = _age_counts(connection, project_id)
    refused = None
    if project_id == _home(settings):
        refused = _home_sentence(row["name"])
    return {
        "id": row["id"], "name": row["name"], "slug": row["slug"], "org_id": row["org_id"],
        "archived": row["archived_at"] is not None,
        "home": project_id == _home(settings), "refused": refused,
        "counts": kinds, "total": sum(per_table.values()),
        "tables": len(per_table), "staging_apps": staging, "production_apps": production,
        "previews_running": int(connection.execute(text(
            "SELECT count(*) FROM app_previews WHERE project_id = :p AND status NOT IN "
            "('stopped', 'failed', 'expired')"), {"p": project_id}).scalar() or 0)
        if cat.has("app_previews", "status") else 0,
        "graph": kg,
        "keeps": KEEPS,
        # No project export exists yet (there is no GET /v1/projects/{id}/export): said, not linked.
        "export": {"available": False,
                   "note": "There is no project export yet, so there is nothing to download first."},
    }


def _home_sentence(name: str) -> str:
    return (f"{name} is this install's home project, where BOSS's own agent and the worker's "
            "schedules live, so it cannot be deleted.")


# ── the knowledge graph's AGE projection ─────────────────────────────────────────────────────

def _age_graph(connection: Any, project_id: str) -> Optional[str]:
    from .knowledge.age import graph_name

    name = graph_name(project_id)
    try:
        with connection.begin_nested():
            found = connection.execute(text(
                "SELECT 1 FROM ag_catalog.ag_graph WHERE name = :n"), {"n": name}).first()
    except Exception:  # noqa: BLE001 - no AGE on this database: no graph to drop
        return None
    return name if found else None


def _age_counts(connection: Any, project_id: str) -> dict[str, int]:
    name = _age_graph(connection, project_id)
    if not name:
        return {"nodes": 0, "edges": 0}
    try:
        with connection.begin_nested():
            from .knowledge.age import projection_counts

            return projection_counts(connection, project_id)
    except Exception:  # noqa: BLE001
        return {"nodes": 0, "edges": 0}


def _drop_age(connection: Any, project_id: str) -> bool:
    name = _age_graph(connection, project_id)
    if not name:
        return False
    connection.execute(text("LOAD 'age'"))
    connection.execute(text("SELECT ag_catalog.drop_graph(:g, true)"), {"g": name})
    return True


# ── the one transaction ──────────────────────────────────────────────────────────────────────

def _order(cat: Catalog, tables: set[str]) -> tuple[list[str], list[Fk]]:
    """Children before parents among `tables`; the edges a cycle forces to be cleared first."""
    edges = [fk for fk in cat.fks if fk.child in tables and fk.parent in tables
             and fk.child != fk.parent]
    remaining = set(tables)
    order: list[str] = []
    broken: list[Fk] = []
    live = list(edges)
    while remaining:
        has_child = {fk.parent for fk in live if fk.child in remaining}
        ready = sorted(t for t in remaining if t not in has_child)
        if not ready:
            # A cycle. A reference the database follows itself (ON DELETE CASCADE / SET NULL)
            # needs no order; otherwise clear one nullable reference on a table whose rows are
            # picked by their own project_id (clearing it cannot lose a row) and go on.
            inside = [fk for fk in live if fk.child in remaining and fk.parent in remaining]
            follows = next((fk for fk in inside if fk.on_delete in ("c", "n")), None)
            if follows is not None:
                live.remove(follows)
                continue
            cand = next((fk for fk in inside if fk.nullable and fk.child in cat.direct), None)
            if cand is None:
                cyc = sorted(remaining)[:6]
                raise RuntimeError(f"the foreign keys among {cyc} form a cycle with no column "
                                   "that may be cleared")
            broken.append(cand)
            live.remove(cand)
            continue
        for t in ready:
            order.append(t)
            remaining.discard(t)
        live = [fk for fk in live if fk.child in remaining]
    return order, broken


def _rehome_target(connection: Any, settings: Any, project: dict) -> Optional[str]:
    home = _home(settings)
    row = connection.execute(text(
        "SELECT p.id FROM projects p JOIN workspaces w ON w.id = p.workspace_id "
        "WHERE w.org_id = :o AND p.id <> :p ORDER BY (p.id = :h) DESC, p.archived_at IS NOT NULL, "
        "p.created_at LIMIT 1"), {"o": project["org_id"], "p": project["id"], "h": home}).scalar()
    return str(row) if row else None


def _move(connection: Any, table: str, where: str, params: dict[str, Any]) -> None:
    """Move rows of `table` from project :p to :to. When the other project already holds a row
    with the same unique name (a GitHub connection labelled "default"), the moved one's text
    columns in that unique key say where it came from, so both stay."""
    sql = f"UPDATE {_q(table)} SET project_id = :to WHERE project_id = :p AND {where}"
    try:
        with connection.begin_nested():
            connection.execute(text(sql), params)
        return
    except Exception as exc:  # noqa: BLE001 - only a unique violation is handled below
        if "unique" not in str(exc).lower():
            raise
    cols = connection.execute(text(
        "SELECT DISTINCT a.attname FROM pg_index i JOIN pg_class c ON c.oid = i.indrelid "
        "JOIN pg_namespace n ON n.oid = c.relnamespace "
        "JOIN pg_attribute a ON a.attrelid = c.oid AND a.attnum = ANY(i.indkey) "
        "WHERE i.indisunique AND n.nspname = current_schema() AND c.relname = :t "
        "AND format_type(a.atttypid, a.atttypmod) = 'text' AND a.attname NOT IN ('id', 'project_id') "
        "AND EXISTS (SELECT 1 FROM pg_attribute pa WHERE pa.attrelid = c.oid "
        "            AND pa.attname = 'project_id' AND pa.attnum = ANY(i.indkey))"),
        {"t": table}).scalars().all()
    if not cols:
        raise DeleteRefused("shared_rows", f"A {table} row this project shares with another "
                                           "could not be moved to it, so nothing was deleted.")
    sets = ", ".join(f"{_q(c)} = {_q(c)} || ' (moved ' || substr(md5(random()::text), 1, 4) || ')'"
                     for c in cols)
    connection.execute(text(
        f"UPDATE {_q(table)} SET project_id = :to, {sets} WHERE project_id = :p AND {where}"),
        params)


def _keep_others(connection: Any, cat: Catalog, preds: _Preds, project_id: str,
                 fallback: Optional[str]) -> list[dict[str, Any]]:
    """Rows OUTSIDE this project that point at rows about to go are never taken with them: a row
    this project owns that another project uses moves to that project (a GitHub connection, a
    server); a reference that may be empty is cleared. Repeated until nothing points in."""
    kept: list[dict[str, Any]] = []
    deleted = cat.deleted
    for _round in range(10):
        changed = False
        for fk in cat.fks:
            if fk.parent not in deleted:
                continue
            parent_pred = preds.of(fk.parent)
            if parent_pred == "FALSE":
                continue
            child_pred = preds.of(fk.child) if fk.child in deleted else "FALSE"
            outside = (f"({child_pred}) IS NOT TRUE AND {_cols(fk.cols, 'x')} IN "
                       f"(SELECT {_cols(fk.pcols)} FROM {_q(fk.parent)} WHERE {parent_pred})")
            count = f"SELECT count(*) FROM {_q(fk.child)} x WHERE {outside}"
            n = connection.execute(text(count), {"p": project_id}).scalar()
            if not n:
                continue
            changed = True
            movable = fk.parent in cat.direct
            if movable and "project_id" in cat.columns.get(fk.child, {}):
                # The shared row moves to the project that uses it.
                for row in connection.execute(text(
                        "SELECT DISTINCT x.project_id AS other, "
                        + ", ".join(f"x.{_q(c)} AS k{i}" for i, c in enumerate(fk.cols))
                        + f" FROM {_q(fk.child)} x WHERE {outside} AND x.project_id IS NOT NULL"),
                        {"p": project_id}).mappings().all():
                    where = " AND ".join(f"{_q(c)} = :k{i}" for i, c in enumerate(fk.pcols))
                    keys = {f"k{i}": row[f"k{i}"] for i in range(len(fk.cols))}
                    _move(connection, fk.parent, where, {"to": row["other"], "p": project_id, **keys})
                    kept.append({"table": fk.parent, "to": row["other"], "used_by": fk.child,
                                 "key": "/".join(str(v) for v in keys.values())})
                n = connection.execute(text(count), {"p": project_id}).scalar()
                if not n:
                    continue
            if fk.nullable:
                sets = ", ".join(f"{_q(c)} = NULL" for c in fk.cols)
                connection.execute(text(f"UPDATE {_q(fk.child)} x SET {sets} WHERE {outside}"),
                                   {"p": project_id})
                kept.append({"table": fk.child, "cleared": list(fk.cols), "rows": int(n)})
            elif movable and fallback:
                _move(connection, fk.parent,
                      f"{_cols(fk.pcols)} IN (SELECT {_cols(fk.cols, 'x')} FROM {_q(fk.child)} x "
                      f"WHERE {outside})", {"to": fallback, "p": project_id})
                kept.append({"table": fk.parent, "to": fallback, "used_by": fk.child})
            else:
                raise DeleteRefused(
                    "shared_rows",
                    f"{n} record(s) in {fk.child} outside this project still depend on its "
                    f"{fk.parent}, so nothing was deleted.")
        if not changed:
            return kept
    raise DeleteRefused("shared_rows", "Rows shared with other projects could not be separated "
                                       "from this one, so nothing was deleted.")


def _identity_scopes(connection: Any, cat: Catalog, project_id: str, actor: str) -> dict[str, int]:
    """Access keys, approval delegations and grants that name this project: narrowed to their
    other projects, or revoked (kept as security records) when it was their only one. The
    project's Captain authority goes."""
    out = {"narrowed": 0, "revoked": 0, "captain": 0}
    if cat.has("identity_delegation_grants", "authority_kind", "project_ids"):
        ids = connection.execute(text(
            "SELECT id FROM identity_delegation_grants WHERE authority_kind = 'captain' "
            "AND project_ids[1] = :p"), {"p": project_id}).scalars().all()
        if ids:
            if cat.has("identity_delegation_grant_permissions", "grant_id"):
                connection.execute(text(
                    "DELETE FROM identity_delegation_grant_permissions WHERE grant_id = ANY(:i)"),
                    {"i": list(ids)})
            if cat.has("identity_delegation_grants", "parent_grant_id"):
                connection.execute(text(
                    "UPDATE identity_delegation_grants SET parent_grant_id = NULL "
                    "WHERE parent_grant_id = ANY(:i)"), {"i": list(ids)})
            connection.execute(text("DELETE FROM identity_delegation_grants WHERE id = ANY(:i)"),
                               {"i": list(ids)})
            out["captain"] = len(ids)
    for table in sorted(cat.tables):
        typ = cat.columns[table].get("project_ids")
        if not typ or not typ[1].startswith("text[]") or not table.startswith("identity_"):
            continue
        cols = cat.columns[table]
        n = connection.execute(text(
            f"UPDATE {_q(table)} SET project_ids = array_remove(project_ids, :p) "
            "WHERE :p = ANY(project_ids) AND cardinality(project_ids) > 1"),
            {"p": project_id}).rowcount
        out["narrowed"] += int(n or 0)
        sets = []
        if "revoked_at" in cols:
            sets.append("revoked_at = COALESCE(revoked_at, now())")
        if "revoked_by" in cols:
            sets.append("revoked_by = COALESCE(revoked_by, :a)")
        if "status" in cols:
            sets.append("status = 'revoked'")
        if not sets:
            continue
        n = connection.execute(text(
            f"UPDATE {_q(table)} SET {', '.join(sets)} WHERE project_ids = ARRAY[:p]::text[] "
            + ("AND status <> 'revoked'" if "status" in cols else "AND revoked_at IS NULL")),
            {"p": project_id, "a": actor}).rowcount
        out["revoked"] += int(n or 0)
    return out


def purge(connection: Any, settings: Any, project_id: str, *, actor: str,
          auth: Any = None, extra: Optional[dict[str, Any]] = None) -> dict[str, Any]:
    """THE ONE TRANSACTION: every row of the project, its AGE graph, its identity scopes, then the
    project row and the audit entry. Runs in the caller's transaction; raises on any failure so
    the caller's rollback takes it all back."""
    project = _project(connection, project_id)
    if project is None:
        raise LookupError(f"There is no project {project_id}.")
    # Nothing else writes this project while it goes.
    connection.execute(text("SELECT id FROM projects WHERE id = :p FOR UPDATE"), {"p": project_id})
    connection.execute(text("SET LOCAL boss.ledger_purge = 'on'"))
    cat = catalog(connection)
    preds = _Preds(cat)
    kept = _keep_others(connection, cat, preds, project_id, _rehome_target(connection, settings, project))
    per_table: dict[str, int] = {}
    # The logical tables first: they name their rows through another table's columns, with no
    # foreign key to order them by.
    for t in sorted(cat.logical):
        n = connection.execute(text(f"DELETE FROM {_q(t)} WHERE {cat.logical[t]}"),
                               {"p": project_id}).rowcount
        if n:
            per_table[t] = int(n)
    tables = {t for t in cat.direct | cat.reached if preds.of(t) != "FALSE"}
    order, broken = _order(cat, tables)
    for fk in broken:
        sets = ", ".join(f"{_q(c)} = NULL" for c in fk.cols)
        connection.execute(text(f"UPDATE {_q(fk.child)} SET {sets} WHERE {preds.of(fk.child)}"),
                           {"p": project_id})
    # Children first: a child's rows are picked through its parents, which are still there.
    for t in order:
        n = connection.execute(text(f"DELETE FROM {_q(t)} WHERE {preds.of(t)}"),
                               {"p": project_id}).rowcount
        if n:
            per_table[t] = int(n)
    graph_dropped = _drop_age(connection, project_id)
    scopes = _identity_scopes(connection, cat, project_id, actor)
    connection.execute(text("DELETE FROM projects WHERE id = :p"), {"p": project_id})
    counts = _by_kind(per_table)
    summary = {
        "name": project["name"], "org_id": project["org_id"],
        "counts": {c["key"]: c["count"] for c in counts}, "rows": sum(per_table.values()),
        "tables": len(per_table), "graph_dropped": graph_dropped, "identity": scopes,
        "moved_to_other_projects": len([m for m in kept if "to" in m]), "actor": actor,
        **(extra or {}),
    }
    _audit(connection, auth, project, summary)
    return {"id": project_id, "name": project["name"], "org_id": project["org_id"],
            "counts": counts, "per_table": per_table, "rows": summary["rows"],
            "graph_dropped": graph_dropped, "identity": scopes, "kept_for_others": kept}


def _audit(connection: Any, auth: Any, project: dict, details: dict[str, Any]) -> None:
    from ..identity import audit
    from ..identity.permissions_gen import P

    fields = dict(target_type="project", target_id=project["id"],
                  permission=P.PROJECT_DELETE.value, scope=f"org:{project['org_id']}",
                  details=details)
    if auth is not None:
        event = audit.SecurityEvent.from_context(auth, "project.deleted", audit.Outcome.SUCCESS,
                                                 **fields)
    else:
        event = audit.SecurityEvent(org_id=str(project["org_id"]), event_type="project.deleted",
                                    outcome=audit.Outcome.SUCCESS, **fields)
    audit.record(event, db=connection)


# ── before the transaction: work, previews, apps on servers ──────────────────────────────────

def _remove_on_host(engine: Any, settings: Any, target: dict) -> dict[str, Any]:
    """Remove one app from its server with services/app_removal.py's own scope and script (its
    compose projects, its container, its folder under /opt/boss-apps; data kept)."""
    from ..db import begin
    from .app_removal import RemovalRefused, removal_scope, removal_script
    from .deploy_service import _discard_key_file, _materialise_key, _ssh, _ssh_key_file
    from .deployment_connectors import _split_ref
    from .secret_backends import resolve_target_secret

    with begin(engine) as c:
        row = c.execute(text("SELECT * FROM deployment_targets WHERE id = :t"),
                        {"t": target["target_id"]}).mappings().first()
        if row is None:
            return {"status": "gone"}
        trow = dict(row)
        try:
            scope = removal_scope(c, trow)
        except RemovalRefused as refused:
            return {"status": "failed", "reason": str(refused)}
        if not any(scope.values()):
            return {"status": "nothing", "reason": "nothing BOSS deployed is on the server"}
        material = resolve_target_secret(c, trow, "ssh_key", settings)
    key = _materialise_key(material) if material else _ssh_key_file(settings)
    user, host, port = _split_ref(trow.get("target_ref") or "")
    try:
        res = _ssh(key, host, user, port, removal_script(scope, delete_data=False), timeout=300)
    finally:
        if material:
            _discard_key_file(key)
    if res.returncode != 0:
        return {"status": "failed", "reason": f"the server answered exit {res.returncode}"}
    return {"status": "removed", "removed": scope}


#: The seam a test (or the dev API) replaces: (engine, settings, app) -> {"status": …}.
HOST_REMOVER: Callable[[Any, Any, dict], dict[str, Any]] = _remove_on_host


def _stop_preview(engine: Any, preview_id: str, settings: Any) -> Any:
    from .preview_service import stop_preview

    return stop_preview(engine, preview_id, settings)


#: The seam a test (or the dev API) replaces: (engine, preview_id, settings).
PREVIEW_STOPPER: Callable[[Any, str, Any], Any] = _stop_preview


def _stop_work(engine: Any, settings: Any, project_id: str, actor: str) -> dict[str, int]:
    """Cancel the project's queued and running jobs, builds and workflow runs, and stop its
    previews, through each one's own cancel path. Best effort per item: the rows go next."""
    from ..db import begin

    reason = "the project is being deleted"
    out = {"builds": 0, "workflow_runs": 0, "jobs": 0, "previews": 0}
    with begin(engine) as c:
        cat = catalog(c)
        if cat.has("code_task_runs", "status"):
            from .code_task_service import TERMINAL_RUN_STATUSES, cancel_run

            for rid in c.execute(text(
                    "SELECT id FROM code_task_runs WHERE project_id = :p AND status <> ALL(:t)"),
                    {"p": project_id, "t": list(TERMINAL_RUN_STATUSES)}).scalars().all():
                try:
                    with c.begin_nested():
                        cancel_run(c, rid, project_id, actor=actor, reason=reason)
                        out["builds"] += 1
                except Exception:  # noqa: BLE001
                    log.info("delete %s: build %s did not cancel", project_id, rid, exc_info=True)
        if cat.has("workflow_runs", "status"):
            from .workflows import TERMINAL_RUN_STATES, cancel_run as cancel_workflow

            for rid in c.execute(text(
                    "SELECT id FROM workflow_runs WHERE project_id = :p AND status <> ALL(:t)"),
                    {"p": project_id, "t": sorted(TERMINAL_RUN_STATES)}).scalars().all():
                try:
                    with c.begin_nested():
                        cancel_workflow(c, project_id, rid, actor, reason=reason, compensate=False)
                        out["workflow_runs"] += 1
                except Exception:  # noqa: BLE001
                    log.info("delete %s: run %s did not cancel", project_id, rid, exc_info=True)
        if cat.has("jobs", "status"):
            out["jobs"] = int(c.execute(text(
                "UPDATE jobs SET status = 'cancelled', updated_at = now() WHERE project_id = :p "
                "AND status IN ('queued', 'failed_retryable', 'leased', 'running')"),
                {"p": project_id}).rowcount or 0)
        previews = c.execute(text(
            "SELECT id FROM app_previews WHERE project_id = :p AND status NOT IN "
            "('stopped', 'failed', 'expired')"), {"p": project_id}).scalars().all() \
            if cat.has("app_previews", "status") else []
    for pid in previews:
        try:
            PREVIEW_STOPPER(engine, pid, settings)
            out["previews"] += 1
        except Exception:  # noqa: BLE001 - the row goes next; a preview process dies with its rows
            log.info("delete %s: preview %s did not stop", project_id, pid, exc_info=True)
    return out


# ── the whole act ────────────────────────────────────────────────────────────────────────────

def check(connection: Any, settings: Any, project_id: str, *, confirm_name: Optional[str],
          also_production: bool = False) -> dict[str, Any]:
    """Every refusal, before anything is touched. Returns the preview."""
    seen = preview(connection, settings, project_id)
    if seen is None:
        raise LookupError(f"There is no project {project_id}.")
    if seen["home"]:
        raise DeleteRefused("delete_refused", _home_sentence(seen["name"]))
    if (confirm_name or "").strip() != seen["name"].strip():
        raise DeleteRefused("name_mismatch",
                            f"Type the project's name, {seen['name']}, exactly to delete it. "
                            "Nothing was deleted.", status=422)
    if seen["production_apps"] and not also_production:
        apps = ", ".join(f"{a['app_name']} on {a['host'] or 'its server'}"
                         for a in seen["production_apps"])
        raise DeleteRefused("production_running",
                            f"Retire its production first: {apps} is live. Or tick \"Also take "
                            "down production\" to remove it with the project. Nothing was deleted.",
                            production_apps=seen["production_apps"])
    return seen


def delete_project(engine: Any, settings: Any, project_id: str, *, actor: str,
                   confirm_name: Optional[str], auth: Any = None,
                   also_production: bool = False) -> dict[str, Any]:
    """Delete the project: refusals first (nothing written), then its apps off their servers, its
    work and previews stopped, then every row in ONE transaction with the audit entry."""
    from ..db import begin

    with begin(engine) as c:
        seen = check(c, settings, project_id, confirm_name=confirm_name,
                     also_production=also_production)
    apps = seen["staging_apps"] + (seen["production_apps"] if also_production else [])
    removed: list[dict[str, Any]] = []
    for app in apps:
        got = HOST_REMOVER(engine, settings, app) or {}
        if got.get("status") == "failed":
            raise DeleteRefused(
                "host_cleanup_failed",
                f"Could not remove {app['app_name']} from {app['host'] or 'its server'}: "
                f"{got.get('reason') or 'the server did not answer'}. Nothing was deleted; "
                "try again, or remove the app from Infra first.", status=502,
                removed_apps=removed)
        if got.get("status") == "removed":
            with begin(engine) as c:
                c.execute(text("UPDATE deployment_targets SET status = 'retired', updated_at = now() "
                               "WHERE id = :t"), {"t": app["target_id"]})
        removed.append({**app, "result": got.get("status") or "nothing"})
    stopped = _stop_work(engine, settings, project_id, actor)
    from . import project_files

    with begin(engine) as c:
        # What the project keeps on this machine, read while its rows still say where.
        files_plan = project_files.collect(c, settings, project_id)
    with begin(engine) as c:
        done = purge(c, settings, project_id, actor=actor, auth=auth,
                     extra={"apps_removed": [f"{a['app_name']} ({a['environment']})" for a in removed
                                             if a["result"] == "removed"],
                            "stopped": stopped})
    # The rows are gone (committed): now its files — a local project's bare repositories, its
    # retained workspaces, preview trees and logs, artifacts, and a mirror no one else uses.
    files = project_files.remove(settings, files_plan)
    return {**done, "removed_apps": removed, "stopped": stopped, "deleted": True,
            "files": project_files.describe(files), "files_refused": files["refused"]}
