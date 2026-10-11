"""The LAB PROJECT an insecure_by_design template is applied into (operator decision, 2026-10-09).

An insecure template is a training app with planted, catalogued flaws. It used to apply into
whatever project the person was in — the shared default project, beside real apps. Now it is
applied ONLY as a new app in its own project, made for it here, named so nobody mistakes it:

    "<App> lab (insecure by design)"

The lab is in the same workspace as the project the person started from, with that project's
repository binding, model sources, settings and project-scoped access copied, so a build there
runs the way it would have run at home. `project_settings.template_lab` marks it; the deploy
guard (templates.deploy_guard) refuses every host deploy from a lab, whatever the target says.

`ensure_lab` is idempotent per (source project, app, template): a retry after a consent sheet or
a readiness fix reuses the lab the first attempt made, so the grant the person just gave (which
names the lab project) covers the retry.
"""
from __future__ import annotations

import json
import logging
import re
import secrets as _random
from typing import Any, Optional

from sqlalchemy import text

from ..services.core import emit_event, make_id

log = logging.getLogger("boss.templates.lab")

LAB_SETTING = "template_lab"
LAB_SUFFIX = "lab (insecure by design)"


def lab_name(app_name: str) -> str:
    return f"{(app_name or 'App').strip()[:60]} {LAB_SUFFIX}"


def lab_of(c, project_id: Optional[str]) -> Optional[dict]:
    """The lab marker of `project_id`, or None when it is not a template lab."""
    if not project_id:
        return None
    value = c.execute(text("SELECT value FROM project_settings WHERE project_id = :p AND key = :k"),
                      {"p": project_id, "k": LAB_SETTING}).scalar()
    return value if isinstance(value, dict) and value else None


def find_lab(c, *, source_project_id: str, app_key: str, template_key: str) -> Optional[str]:
    return c.execute(text(
        "SELECT project_id FROM project_settings WHERE key = :k "
        "AND value->>'source_project_id' = :s AND value->>'app_key' = :a "
        "AND value->>'template_key' = :t ORDER BY updated_at LIMIT 1"),
        {"k": LAB_SETTING, "s": source_project_id, "a": app_key, "t": template_key}).scalar()


def _columns(c, table: str) -> list[str]:
    return [r[0] for r in c.execute(text(
        "SELECT column_name FROM information_schema.columns WHERE table_name = :t "
        "AND table_schema = current_schema() ORDER BY ordinal_position"), {"t": table}).all()]


def _copy(c, table: str, *, where: str, params: dict, set_values: dict, id_prefix: Optional[str]) -> int:
    """Copy rows of `table` matching `where`, with `set_values` replacing columns (and a fresh id
    when the table has one). Each table in its own savepoint: a table this install lacks, or a row
    the target already has, is skipped, never a failed apply."""
    cols = _columns(c, table)
    if not cols:
        return 0
    select = []
    for col in cols:
        if col == "id" and id_prefix:
            select.append(f"'{id_prefix}_' || substr(md5(random()::text || clock_timestamp()::text), 1, 20)")
        elif col in set_values:
            select.append(f":set_{col}")
        else:
            select.append(f'"{col}"')
    sql = (f'INSERT INTO {table} ({", ".join(chr(34) + x + chr(34) for x in cols)}) '
           f'SELECT {", ".join(select)} FROM {table} WHERE {where} ON CONFLICT DO NOTHING')
    try:
        with c.begin_nested():
            return int(c.execute(text(sql), {**params, **{f"set_{k}": v for k, v in set_values.items()}}).rowcount or 0)
    except Exception as exc:  # noqa: BLE001
        log.warning("lab project: could not copy %s: %s", table, type(exc).__name__)
        return 0


def ensure_lab(c, settings: Any, *, source_project_id: str, app_key: str, app_name: str,
               template_key: str, actor_id: Optional[str]) -> dict:
    """{project_id, name, created} — the lab for this app and template, made now if missing."""
    found = find_lab(c, source_project_id=source_project_id, app_key=app_key, template_key=template_key)
    if found:
        name = c.execute(text("SELECT name FROM projects WHERE id = :p"), {"p": found}).scalar()
        return {"project_id": found, "name": name, "created": False}
    workspace = c.execute(text("SELECT workspace_id FROM projects WHERE id = :p"),
                          {"p": source_project_id}).scalar()
    if not workspace:
        raise LookupError(source_project_id)
    project_id = make_id("proj")
    name = lab_name(app_name)
    slug = "lab-" + (re.sub(r"[^a-z0-9]+", "-", app_key.lower()).strip("-")[:40] or "app") \
        + "-" + _random.token_hex(3)
    c.execute(text("INSERT INTO projects (id, workspace_id, name, slug) VALUES (:id, :ws, :n, :s)"),
              {"id": project_id, "ws": workspace, "n": name, "s": slug})

    from ..bootstrap import seed_project_defaults
    from ..services.entitlements import EntitlementBrokerService
    from ..services.members import MemberDirectoryService
    from ..services.specpack import SpecPackService

    seed_project_defaults(c, settings, project_id, entitlements=EntitlementBrokerService(settings),
                          members=MemberDirectoryService(settings), specpack=SpecPackService(settings),
                          actor=f"template_lab:{actor_id or 'system'}")
    # Run the way home does: the same repository, models and settings.
    same = {"where": "project_id = :src", "params": {"src": source_project_id},
            "set_values": {"project_id": project_id}}
    _copy(c, "github_repository_bindings", id_prefix="ghrb", **same)
    _copy(c, "entitlement_sources", id_prefix=None, **same)
    _copy(c, "project_settings", id_prefix=None, **{**same, "where": "project_id = :src AND key <> :lab",
                                                     "params": {"src": source_project_id, "lab": LAB_SETTING}})
    # The same people, at the same project roles (org-wide roles reach it already).
    _copy(c, "identity_role_bindings", id_prefix="rbd",
          where="scope_type = 'project' AND scope_id = :src", params={"src": source_project_id},
          set_values={"scope_id": project_id})
    marker = {"insecure_by_design": True, "source_project_id": source_project_id, "app_key": app_key,
              "app_name": app_name, "template_key": template_key, "created_by": actor_id}
    c.execute(text(
        "INSERT INTO project_settings (project_id, key, value, updated_by) VALUES (:p, :k, CAST(:v AS JSONB), :by) "
        "ON CONFLICT (project_id, key) DO UPDATE SET value = EXCLUDED.value"),
        {"p": project_id, "k": LAB_SETTING, "v": json.dumps(marker), "by": actor_id or "system"})
    emit_event(c, source_project_id, "template.lab_created", actor_id or "system", "project", project_id,
               {"name": name, "app_key": app_key, "template_key": template_key})
    return {"project_id": project_id, "name": name, "created": True}


def labs_of(c, source_project_id: str) -> list[dict]:
    """The lab projects made from `source_project_id`, newest first: where each insecure
    template app is built, so the source project's console can link to them."""
    rows = c.execute(text(
        "SELECT s.project_id, p.name, s.value, s.updated_at FROM project_settings s "
        "JOIN projects p ON p.id = s.project_id WHERE s.key = :k "
        "AND s.value->>'source_project_id' = :src ORDER BY s.updated_at DESC LIMIT 50"),
        {"k": LAB_SETTING, "src": source_project_id}).mappings().all()
    return [{"project_id": r["project_id"], "name": r["name"],
             "app_name": (r["value"] or {}).get("app_name"),
             "template_key": (r["value"] or {}).get("template_key")} for r in rows]
