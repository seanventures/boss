"""Team templates: a pack that installs a working team — roles, reporting lines and starter work
— in one governed apply, with provenance digests for drift (BOSSAPP-894).

A pack could install roles, workflows, policies and skills. Installing a team gave a set of roles
with no structure and nothing to do. A pack may now also carry (both optional; a pack without them
installs exactly as before):

    org:                         # reporting lines (org_assignments; org_chain reads them)
      - {role: engineering_captain, display_name: Delivery lead}       # a root: reports to the
      - {role: qa, reports_to: engineering_captain}                    #   manager chosen at apply
    starter_tasks:               # work items (work_items, P1-5), assigned to the pack's roles
      - {key: ci, title: Set up CI, assignee: qa, description: ...}
      - {key: ci-gate, title: Gate merges on CI, assignee: qa, parent: ci}

THE APPLY, AS PORTABILITY DOES IT (BOSSAPP-837). :func:`plan` is the dry run: it replays the whole
apply through the same services the console uses (specpack.install_pack, org_assignments.set_one,
work_items.create_item) inside a transaction it rolls back, and reports create / update /
unchanged per item plus a ``plan_digest``. :func:`apply` replays it for real in ONE transaction,
and only when the plan digest it recomputes equals the one the person looked at — a project
changed in between refuses (:class:`PlanStale`), nothing half-applied. No raw INSERT writes a role,
a line or an item.

PROVENANCE. Every applied item gets a ``pack_provenance`` row: its digest as installed and what it
became (the role key, the work item id). :func:`drift` compares the digest of what is there NOW:
``in_sync``, ``modified`` (someone changed it since), or ``missing``.

Starter items are idempotent per (pack, key): applying again finds them by subject
``pack_starter`` / ``<pack_id>:<key>`` and leaves them alone.
"""
from __future__ import annotations

import hashlib
import json
from typing import Any, Optional

from sqlalchemy import text
from sqlalchemy.engine import Connection, Engine

from .core import emit_event, make_id

STARTER_SUBJECT = "pack_starter"
MAX_STARTER_TASKS = 50


class TeamTemplateError(ValueError):
    status = 422


class PlanStale(TeamTemplateError):
    status = 409


def canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)


def digest(value: Any) -> str:
    return "sha256:" + hashlib.sha256(canonical(value).encode("utf-8")).hexdigest()


def has_team(pack_spec: dict[str, Any]) -> bool:
    return bool(pack_spec.get("org") or pack_spec.get("starter_tasks"))


def validate_team(pack_spec: dict[str, Any]) -> list[str]:
    """The team sections' problems, in words ([] when there are none or no team sections)."""
    errors: list[str] = []
    roles = set((pack_spec.get("installs") or {}).get("roles") or [])
    org = pack_spec.get("org") or []
    if not isinstance(org, list):
        return ["org must be a list"]
    placed: dict[str, Optional[str]] = {}
    for i, line in enumerate(org):
        if not isinstance(line, dict) or not str(line.get("role") or "").strip():
            errors.append(f"org[{i}] needs a role")
            continue
        role = str(line["role"]).strip()
        if role not in roles:
            errors.append(f"org[{i}]: {role!r} is not one of the roles this pack installs")
        if role in placed:
            errors.append(f"org[{i}]: {role!r} is placed twice")
        parent = line.get("reports_to")
        if parent is not None and str(parent).strip() not in roles and not str(
                parent).startswith("person:"):
            errors.append(f"org[{i}]: reports_to {parent!r} is neither a role of this pack nor "
                          "person:<id>")
        placed[role] = str(parent).strip() if parent else None
    for role in placed:                      # a loop inside the template
        seen, cursor = {role}, placed.get(role)
        while cursor and cursor in placed:
            if cursor in seen:
                errors.append(f"org: {role!r} reports to itself through the chain")
                break
            seen.add(cursor)
            cursor = placed.get(cursor)
    tasks = pack_spec.get("starter_tasks") or []
    if not isinstance(tasks, list):
        return errors + ["starter_tasks must be a list"]
    if len(tasks) > MAX_STARTER_TASKS:
        errors.append(f"starter_tasks holds at most {MAX_STARTER_TASKS} items")
    keys: set[str] = set()
    for i, task in enumerate(tasks):
        if not isinstance(task, dict):
            errors.append(f"starter_tasks[{i}] must be an object")
            continue
        key, title = str(task.get("key") or "").strip(), str(task.get("title") or "").strip()
        if not key or not title:
            errors.append(f"starter_tasks[{i}] needs a key and a title")
        if key in keys:
            errors.append(f"starter_tasks[{i}]: key {key!r} is used twice")
        assignee = task.get("assignee")
        if assignee and str(assignee) not in roles:
            errors.append(f"starter_tasks[{i}]: assignee {assignee!r} is not a role of this pack")
        parent = task.get("parent")
        if parent and str(parent) not in keys:
            errors.append(f"starter_tasks[{i}]: parent {parent!r} must be an earlier task's key")
        keys.add(key)
    return errors


# ── the items, and their digests ────────────────────────────────────────────────────────────


def _org_items(pack_spec: dict[str, Any], manager: Optional[str]) -> list[dict[str, Any]]:
    items = []
    for sort, line in enumerate(pack_spec.get("org") or []):
        parent = str(line.get("reports_to") or "").strip() or (manager or None)
        items.append({"role": str(line["role"]).strip(), "reports_to": parent,
                      "display_name": str(line.get("display_name") or "").strip() or None,
                      "sort": sort})
    return items


def _task_items(pack_spec: dict[str, Any]) -> list[dict[str, Any]]:
    return [{"key": str(t["key"]).strip(), "title": str(t["title"]).strip(),
             "description": str(t.get("description") or ""),
             "assignee": f"agent:{t['assignee']}" if t.get("assignee") else None,
             "parent": str(t.get("parent") or "").strip() or None}
            for t in pack_spec.get("starter_tasks") or []]


def _org_digest(item: dict[str, Any]) -> str:
    return digest({"reports_to": item.get("reports_to"),
                   "display_name": item.get("display_name") or None})


def _task_digest(item: dict[str, Any]) -> str:
    return digest({"title": item.get("title"), "description": item.get("description") or "",
                   "assignee": item.get("assignee")})


def _starter_row(connection: Connection, project_id: str, pack_id: str,
                 key: str) -> Optional[dict[str, Any]]:
    row = connection.execute(
        text("SELECT id, title, description, assignee FROM work_items WHERE project_id = :p "
             "AND subject_type = :st AND subject_id = :sid"),
        {"p": project_id, "st": STARTER_SUBJECT, "sid": f"{pack_id}:{key}"},
    ).mappings().first()
    return dict(row) if row else None


def _current(connection: Connection, project_id: str, pack_id: str,
             pack_spec: dict[str, Any]) -> dict[str, Any]:
    from . import org_assignments

    roles = {r for r in connection.execute(
        text("SELECT role_key FROM roles WHERE project_id = :p"), {"p": project_id}).scalars()}
    org = org_assignments.get_all(connection, project_id)
    tasks = {t["key"]: _starter_row(connection, project_id, pack_id, t["key"])
             for t in _task_items(pack_spec)}
    return {"roles": roles, "org": org, "tasks": tasks}


def _diff(pack_spec: dict[str, Any], manager: Optional[str],
          now: dict[str, Any]) -> list[dict[str, Any]]:
    ops: list[dict[str, Any]] = []
    for role in (pack_spec.get("installs") or {}).get("roles") or []:
        ops.append({"kind": "role", "key": role,
                    "action": "unchanged" if role in now["roles"] else "create"})
    for item in _org_items(pack_spec, manager):
        have = now["org"].get(item["role"])
        if have is None:
            action = "create"
        elif _org_digest({"reports_to": have.get("reports_to"),
                          "display_name": have.get("display_name")}) == _org_digest(item):
            action = "unchanged"
        else:
            action = "update"
        ops.append({"kind": "org", "key": item["role"], "action": action, "data": item})
    for item in _task_items(pack_spec):
        ops.append({"kind": "task", "key": item["key"],
                    "action": "unchanged" if now["tasks"].get(item["key"]) else "create",
                    "data": item})
    return ops


# ── replay through the services ─────────────────────────────────────────────────────────────


def _replay(connection: Connection, settings: Any, *, project_id: str, pack_id: str,
            pack_spec: dict[str, Any], manager: Optional[str], actor: str) -> dict[str, Any]:
    from . import org_assignments, work_items
    from .specpack import SpecPackService

    installed = SpecPackService(settings).install_pack(connection, project_id, pack_id, actor)
    lines = []
    for item in _org_items(pack_spec, manager):
        org_assignments.set_one(connection, project_id, item["role"],
                                reports_to=item["reports_to"],
                                display_name=item["display_name"], sort=item["sort"],
                                actor=actor)
        lines.append(item)
    created: dict[str, str] = {}
    tasks = []
    for item in _task_items(pack_spec):
        existing = _starter_row(connection, project_id, pack_id, item["key"])
        if existing is None:
            parent_id = created.get(item["parent"]) if item["parent"] else None
            if item["parent"] and parent_id is None:
                parent_row = _starter_row(connection, project_id, pack_id, item["parent"])
                parent_id = parent_row["id"] if parent_row else None
            made = work_items.create_item(
                connection, settings, project_id=project_id, title=item["title"],
                description=item["description"], parent_id=parent_id,
                assignee=item["assignee"], status="todo", subject_type=STARTER_SUBJECT,
                subject_id=f"{pack_id}:{item['key']}", actor=actor)
            created[item["key"]] = made["id"]
            tasks.append({**item, "work_item_id": made["id"], "result": "created"})
        else:
            created[item["key"]] = existing["id"]
            tasks.append({**item, "work_item_id": existing["id"], "result": "unchanged"})
    return {"install_status": installed.get("install_status"), "org": lines, "tasks": tasks}


def _load(settings: Any, pack_id: str) -> dict[str, Any]:
    from .specpack import SpecPackService

    spec = SpecPackService(settings).load_pack_spec(pack_id)
    problems = validate_team(spec)
    if problems:
        raise TeamTemplateError(f"pack {pack_id} has an invalid team: {'; '.join(problems)}")
    if not has_team(spec):
        raise TeamTemplateError(f"pack {pack_id} carries no org or starter_tasks; install it "
                                "with Packs › Install")
    return spec


def _check_manager(connection: Connection, project_id: str, manager: Optional[str]) -> None:
    if not manager or manager.startswith("person:"):
        return
    exists = connection.execute(text("SELECT 1 FROM roles WHERE project_id = :p AND role_key = :r"),
                                {"p": project_id, "r": manager}).first()
    if exists is None:
        raise TeamTemplateError(f"the manager {manager!r} is not an agent in this project; name "
                                "an agent role key or person:<id>")


def plan(engine: Engine, settings: Any, *, project_id: str, pack_id: str,
         manager: Optional[str], actor: str) -> dict[str, Any]:
    """THE DRY RUN: the diff, validated by replaying the apply and rolling it back."""
    spec = _load(settings, pack_id)
    manager = (manager or "").strip() or None
    with engine.connect() as connection:
        trans = connection.begin()
        try:
            _check_manager(connection, project_id, manager)
            ops = _diff(spec, manager, _current(connection, project_id, pack_id, spec))
            error = None
            try:
                with connection.begin_nested():
                    _replay(connection, settings, project_id=project_id, pack_id=pack_id,
                            pack_spec=spec, manager=manager, actor=actor)
            except Exception as exc:  # noqa: BLE001 — every service raises its own type
                error = f"{type(exc).__name__}: {str(exc)[:300]}"
        finally:
            trans.rollback()
    summary: dict[str, dict[str, int]] = {}
    for op in ops:
        summary.setdefault(op["kind"], {}).setdefault(op["action"], 0)
        summary[op["kind"]][op["action"]] += 1
    return {"pack_id": pack_id, "pack_version": spec.get("version"), "manager": manager,
            "ops": ops, "summary": summary, "error": error, "dry_run": True,
            "plan_digest": digest({"pack": spec, "manager": manager, "ops": ops})}


def apply(engine: Engine, settings: Any, *, project_id: str, pack_id: str,
          manager: Optional[str], actor: str, plan_digest: str) -> dict[str, Any]:
    """Apply what the dry run showed, all of it in one transaction, or nothing."""
    looked_at = plan(engine, settings, project_id=project_id, pack_id=pack_id, manager=manager,
                     actor=actor)
    if looked_at["error"]:
        raise TeamTemplateError(f"the dry run fails: {looked_at['error']}")
    if looked_at["plan_digest"] != plan_digest:
        raise PlanStale("the project or the pack changed since that dry run; run the dry run "
                        "again and apply what it shows")
    spec = _load(settings, pack_id)
    manager = looked_at["manager"]
    with engine.begin() as connection:
        result = _replay(connection, settings, project_id=project_id, pack_id=pack_id,
                         pack_spec=spec, manager=manager, actor=actor)
        version = str(spec.get("version") or "0.0.0")
        rows = ([("role", r, digest({"role": r}), r)
                 for r in (spec.get("installs") or {}).get("roles") or []]
                + [("org", i["role"], _org_digest(i), i["role"]) for i in result["org"]]
                + [("task", t["key"], _task_digest(t), t["work_item_id"])
                   for t in result["tasks"]])
        for kind, key, item_digest, target in rows:
            connection.execute(
                text("""
                    INSERT INTO pack_provenance (id, project_id, pack_id, pack_version, item_kind,
                                                 item_key, digest, target_ref, installed_by)
                    VALUES (:i, :p, :pk, :v, :k, :key, :d, :t, :by)
                    ON CONFLICT (project_id, pack_id, item_kind, item_key) DO UPDATE SET
                      pack_version = EXCLUDED.pack_version, digest = EXCLUDED.digest,
                      target_ref = EXCLUDED.target_ref, installed_by = EXCLUDED.installed_by,
                      installed_at = now()
                """),
                {"i": make_id("ppv"), "p": project_id, "pk": pack_id, "v": version, "k": kind,
                 "key": key, "d": item_digest, "t": target, "by": actor})
        emit_event(connection, project_id, "pack.team_applied", actor, "pack", pack_id,
                   {"manager": manager, "summary": looked_at["summary"],
                    "plan_digest": plan_digest})
    return {**{k: v for k, v in looked_at.items() if k != "dry_run"}, "dry_run": False,
            "applied": result}


def drift(connection: Connection, project_id: str, pack_id: str) -> dict[str, Any]:
    """Each applied item against what is there now: in_sync, modified or missing."""
    from . import org_assignments

    rows = connection.execute(
        text("SELECT item_kind, item_key, digest, target_ref, pack_version, installed_at "
             "FROM pack_provenance WHERE project_id = :p AND pack_id = :pk "
             "ORDER BY item_kind, item_key"),
        {"p": project_id, "pk": pack_id},
    ).mappings().all()
    org = org_assignments.get_all(connection, project_id)
    roles = {r for r in connection.execute(
        text("SELECT role_key FROM roles WHERE project_id = :p"), {"p": project_id}).scalars()}
    items = []
    for row in rows:
        kind, key = row["item_kind"], row["item_key"]
        if kind == "role":
            now = digest({"role": key}) if key in roles else None
        elif kind == "org":
            have = org.get(key)
            now = None if have is None else _org_digest(
                {"reports_to": have.get("reports_to"), "display_name": have.get("display_name")})
        else:
            have = connection.execute(
                text("SELECT title, description, assignee FROM work_items WHERE id = :i"),
                {"i": row["target_ref"]}).mappings().first()
            now = None if have is None else _task_digest(
                {"title": have["title"], "description": have["description"] or "",
                 "assignee": have["assignee"]})
        status = "missing" if now is None else ("in_sync" if now == row["digest"] else "modified")
        items.append({"kind": kind, "key": key, "status": status, "target": row["target_ref"],
                      "installed_digest": row["digest"], "current_digest": now,
                      "pack_version": row["pack_version"],
                      "installed_at": row["installed_at"].isoformat()
                      if row["installed_at"] else None})
    return {"pack_id": pack_id, "items": items,
            "drifted": sum(1 for i in items if i["status"] != "in_sync"),
            "applied": bool(items)}
