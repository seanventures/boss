"""THE WORKFLOW MARKETPLACE: packs of workflows a project installs into its OWN workflows.

    "I want DQ, Helpdesk workflow (SRE workflow) into the marketplace, we dont have to ship by
     default we should have Install button once installed it will be showing in their custom /
     local workflows and not in Standard ones. When people build the workflow, they can publish
     to the Marketplace and they can be installed into the app using Install button."

A PACK is a versioned set of workflows. Two kinds:

  * BOSS's own (`SHIPPED_PACKS`): Banking Data Quality and SRE / Helpdesk. Their workflows are
    catalogue entries (workflow_catalogue.CATALOGUE) that the catalogue seed no longer installs
    by itself; the version is declared here.
  * An org's own: any workflow under "Yours" published from a project, visible to every project
    in the same org (`workflow_packs` + `workflow_pack_versions`). Republishing bumps the version.

INSTALL copies a pack's workflows into the project as its own (is_catalogue false), each carrying
where it came from: `marketplace_pack_id`, `marketplace_version`. The copy keeps the shipped spec
as its BASELINE in the same columns a standard uses (workflow_standards), so:

  * an UNMODIFIED copy takes a newer pack version by itself (at boot for BOSS's packs, on
    republish for an org's);
  * a MODIFIED copy is never overwritten: the newer version waits beside it and is offered
    ("Review update"): take it with my changes (when the three-way apply is clean), take it
    only, or not now;
  * runs never move: a run reads the version it started on.

UNINSTALL removes the pack's workflows (retired when they have runs, so history stays readable)
and is refused while any of them has a run going.

THE LIBRARY (design PR #287) is this list as the console shows it: one list, each item labelled
BOSS or Your team, with tags and search. "Add to project" is Install into any project of the org;
"Edit a copy" is Install then open the copy in the editor; "Share to Library" is Publish, which
runs the same publish-time checks a Save runs (`workflows.spec_problems`) and needs the org-level
`workflow:share` permission at the API.

A TEMPLATE is a pack workflow derived from a standard under its own key ("API gateway
onboarding" from "Onboard a config file"): the standard keeps being seeded everywhere as it was,
and the template installs as an editable copy that follows the standard's steps while it is
unmodified (`PackTemplate`).

A pack workflow whose prerequisites are missing (a connector the DQ pipeline calls) is installed
WAITING: the definition is there, with `marketplace_waiting` saying why, and it is published the
moment the project gains what it lacks (`publish_waiting`, called wherever a project gains it).
"""
from __future__ import annotations

import copy
import json
import re
from dataclasses import dataclass
from typing import Any, Optional

from sqlalchemy import text
from sqlalchemy.engine import Connection

from . import workflows
from .core import emit_event, make_id

BOSS_PUBLISHER = "BOSS"


@dataclass(frozen=True)
class PackTemplate:
    """A pack workflow made from a standard catalogue entry under a key of its own, so the
    standard stays seeded as it is and the template installs as the project's editable copy.
    Its steps ARE the standard's (one construction: a change to the standard reaches an
    unmodified copy at the next sync, and is offered to a modified one)."""
    key: str
    name: str
    description: str
    source_key: str


@dataclass(frozen=True)
class ShippedPack:
    pack_id: str
    name: str
    summary: str
    version: str
    workflow_keys: tuple[str, ...]
    #: The ontology pack its step contracts read kinds from; installing the pack adds it.
    ontology_pack: Optional[str] = None
    #: Library filter words, lower case.
    tags: tuple[str, ...] = ()
    #: Workflows made from a standard under their own key (see PackTemplate).
    templates: tuple[PackTemplate, ...] = ()


#: BOSS's own packs. Bump `version` when a pack's workflows change in a way a person should see.
SHIPPED_PACKS: dict[str, ShippedPack] = {
    "banking-dq": ShippedPack(
        pack_id="banking-dq", name="Banking Data Quality",
        summary="Find the root cause of a data quality exception, fix it, escalate what stays open.",
        version="1.0",
        workflow_keys=("dq_incident_rca", "dq_incident_escalation", "dq_monitor_intake"),
        ontology_pack="banking-dq", tags=("data quality", "banking")),
    "sre-helpdesk": ShippedPack(
        pack_id="sre-helpdesk", name="SRE / Helpdesk",
        summary="Answer an app's alert: investigate, fix, ship, prove recovery.",
        version="1.0",
        # Incident Response carries its investigation step (investigate_incident) with it.
        workflow_keys=("incident_response",),
        tags=("incidents", "sre")),
    # API GATEWAY ONBOARDING (BOSSAPP-1296): "Onboard a config file" as the team's editable
    # template. The standard stays seeded in every project; this is the copy a team changes.
    "api-gateway-onboarding": ShippedPack(
        pack_id="api-gateway-onboarding", name="API gateway onboarding",
        summary="A partner's gateway config: check, fix, review, merge, staging, contract tests.",
        version="1.0", workflow_keys=(),
        tags=("gateway", "onboarding", "config"),
        templates=(PackTemplate(
            key="api_gateway_onboarding", name="API gateway onboarding",
            description="A partner's gateway config arrives. BOSS checks it with the "
                        "repository's own checkers, fixes what it may, leaves you what it may "
                        "not, and ships it to staging once merged. Your team's editable copy "
                        "of Onboard a config file.",
            source_key="onboard_config_file"),)),
}

#: workflow_key -> the BOSS pack it ships in. The catalogue seed skips these keys.
PACK_OF_KEY: dict[str, str] = {key: pack.pack_id for pack in SHIPPED_PACKS.values()
                               for key in pack.workflow_keys}
#: template key -> the BOSS pack it ships in (a template is not a catalogue entry: nothing seeds
#: it, and its standard is seeded as before).
TEMPLATE_OF_KEY: dict[str, str] = {t.key: pack.pack_id for pack in SHIPPED_PACKS.values()
                                   for t in pack.templates}


def pack_of_key(workflow_key: Any) -> Optional[str]:
    """The BOSS pack a workflow key ships in (a catalogue entry or a template), or None."""
    key = str(workflow_key or "")
    return PACK_OF_KEY.get(key) or TEMPLATE_OF_KEY.get(key)

_VERSION = re.compile(r"^\d{1,4}(\.\d{1,4}){0,2}$")
#: A run that has not ended (anything but these) keeps its workflow from being uninstalled.
_ENDED = ("completed", "failed", "deadlettered", "cancelled", "compensated")


class MarketplaceError(workflows.WorkflowConflictError):
    """An act the marketplace refuses, in words (409)."""


class MarketplaceNotFound(workflows.WorkflowNotFoundError):
    """No such pack here, or it is not installed (404)."""


# ── versions ─────────────────────────────────────────────────────────────────────────────────

def version_key(version: Any) -> tuple[int, ...]:
    """Comparable: 1.0 == 1.0.0 < 1.0.1 < 1.1 < 1.10."""
    parts = [int(p) for p in re.findall(r"\d+", str(version or ""))][:3]
    return tuple(parts + [0] * (3 - len(parts)))


def next_version(version: Optional[str]) -> str:
    """1.0 -> 1.1; 0.3 -> 0.4; none -> 1.0."""
    if not version:
        return "1.0"
    parts = list(version_key(version)) + [0]
    return f"{parts[0]}.{parts[1] + 1}"


# ── reading ──────────────────────────────────────────────────────────────────────────────────

def _json(raw: Any, default: Any) -> Any:
    if isinstance(raw, str):
        try:
            return json.loads(raw)
        except ValueError:
            return default
    return raw if raw is not None else default


_TAG = re.compile(r"[^a-z0-9 \-]+")


def _tags(raw: Any) -> list[str]:
    """Library filter words: lower case, letters, digits, spaces and dashes, at most 6 of 24."""
    out: list[str] = []
    for tag in _json(raw, []) if not isinstance(raw, (list, tuple)) else raw:
        word = _TAG.sub("", str(tag or "").strip().lower())[:24].strip()
        if word and word not in out:
            out.append(word)
    return out[:6]


def _org_of(connection: Connection, project_id: str) -> str:
    org = connection.execute(
        text("SELECT w.org_id FROM projects p JOIN workspaces w ON w.id = p.workspace_id "
             "WHERE p.id = :p"), {"p": project_id}).scalar()
    if not org:
        raise MarketplaceNotFound(f"project '{project_id}' was not found.")
    return str(org)


def _present_roles(connection: Connection, project_id: str) -> frozenset[str]:
    return frozenset(str(k) for k in connection.execute(
        text("SELECT role_key FROM roles WHERE project_id = :p"), {"p": project_id}).scalars())


def _entries(pack: ShippedPack) -> list[dict[str, Any]]:
    """The pack's workflows as catalogue entries: its own, then its templates (each the
    standard's entry under the template's key, name and description; `brief_key` keeps the
    standard's start form)."""
    from .workflow_catalogue import CATALOGUE

    by_key = {e["key"]: e for e in CATALOGUE}
    out = [by_key[k] for k in pack.workflow_keys if k in by_key]
    for t in pack.templates:
        source = by_key.get(t.source_key)
        if source is None:
            continue
        out.append({**copy.deepcopy(source), "key": t.key, "name": t.name,
                    "description": t.description, "brief_key": t.source_key,
                    "template_of": t.source_key})
    return out


def _staff(spec: dict[str, Any], present: frozenset[str]) -> dict[str, Any]:
    """Every agent a step names that this project HAS; the rest left to Auto."""
    from .workflow_catalogue import _staffed_for

    staffed = _staffed_for({"steps": [dict(s) for s in spec.get("steps") or []
                                      if isinstance(s, dict)]}, present)
    return {**spec, "steps": staffed["steps"]}


def _org_pack(connection: Connection, org_id: str, pack_id: str) -> Optional[dict[str, Any]]:
    row = connection.execute(
        text("SELECT * FROM workflow_packs WHERE id = :id AND org_id = :o"),
        {"id": pack_id, "o": org_id}).mappings().first()
    return dict(row) if row else None


def _latest_version(connection: Connection, pack_id: str, version: str) -> list[dict[str, Any]]:
    raw = connection.execute(
        text("SELECT workflows FROM workflow_pack_versions WHERE pack_id = :p AND version = :v"),
        {"p": pack_id, "v": version}).scalar()
    items = _json(raw, [])
    return [w for w in items if isinstance(w, dict) and w.get("workflow_key")]


def _pack_contents(connection: Connection, project_id: str, pack_id: str,
                   *, for_install: bool = True) -> dict[str, Any]:
    """{id, source, name, summary, publisher, version, org pack row?, workflows: [{key, name,
    description, spec}]} — the specs staffed for THIS project."""
    present = _present_roles(connection, project_id)
    shipped = SHIPPED_PACKS.get(pack_id)
    if shipped:
        from .workflow_catalogue import entry_spec, _staffed_for

        return {
            "id": shipped.pack_id, "source": "boss", "name": shipped.name,
            "summary": shipped.summary, "publisher": BOSS_PUBLISHER, "version": shipped.version,
            "tags": list(shipped.tags), "shipped": shipped, "row": None,
            "workflows": [{"key": e["key"], "name": e["name"], "description": e["description"],
                           "spec": entry_spec(_staffed_for(e, present)), "entry": e}
                          for e in _entries(shipped)],
        }
    row = _org_pack(connection, _org_of(connection, project_id), pack_id)
    if row is None or (for_install and row["status"] != "published"):
        raise MarketplaceNotFound(f"There is no pack '{pack_id}' in this org's Marketplace.")
    return {
        "id": row["id"], "source": "org", "name": row["name"], "summary": row.get("summary") or "",
        "publisher": row["publisher"], "version": row["version"],
        "tags": _tags(row.get("tags")), "shipped": None, "row": row,
        "workflows": [{"key": w["workflow_key"], "name": w.get("name") or w["workflow_key"],
                       "description": w.get("description") or "",
                       "spec": _staff(_json(w.get("spec"), {}), present), "entry": None}
                      for w in _latest_version(connection, row["id"], row["version"])],
    }


def _installed_rows(connection: Connection, project_id: str,
                    pack_id: Optional[str] = None) -> list[dict[str, Any]]:
    rows = connection.execute(
        text("SELECT * FROM workflow_definitions WHERE project_id = :p "
             "AND marketplace_pack_id IS NOT NULL AND status <> 'retired' "
             "AND (CAST(:pk AS TEXT) IS NULL OR marketplace_pack_id = :pk) "
             "ORDER BY workflow_key"),
        {"p": project_id, "pk": pack_id}).mappings().all()
    return [dict(r) for r in rows]


def update_waiting(definition: dict[str, Any]) -> bool:
    """A newer pack version is waiting beside a MODIFIED copy (the operator's call)."""
    waiting = definition.get("standard_available_version_id")
    return bool(waiting and waiting != definition.get("standard_dismissed_version_id")
                and waiting != definition.get("standard_version_id"))


def _modified(connection: Connection, definition: dict[str, Any]) -> bool:
    baseline = definition.get("standard_content_hash")
    if not baseline or not definition.get("active_version_id"):
        return False
    active = connection.execute(
        text("SELECT content_hash FROM workflow_versions WHERE id = :v"),
        {"v": definition["active_version_id"]}).scalar()
    return bool(active and active != baseline)


def _pack_name(connection: Connection, pack_id: str) -> str:
    shipped = SHIPPED_PACKS.get(pack_id)
    if shipped:
        return shipped.name
    name = connection.execute(text("SELECT name FROM workflow_packs WHERE id = :p"),
                              {"p": pack_id}).scalar()
    return str(name or pack_id)


def _current_version(connection: Connection, pack_id: str) -> Optional[str]:
    shipped = SHIPPED_PACKS.get(pack_id)
    if shipped:
        return shipped.version
    version = connection.execute(text("SELECT version FROM workflow_packs WHERE id = :p"),
                                 {"p": pack_id}).scalar()
    return str(version) if version else None


def install_state(connection: Connection, definition: dict[str, Any],
                  *, detail: bool = False) -> Optional[dict[str, Any]]:
    """Where an installed copy came from, for the Library card and the editor, or None.

    {pack_id, pack_name, source, version, modified, update: None | {version, changes?,
    applies_cleanly?, conflicts?}, waiting}. `detail` adds the update's diff and whether it
    applies cleanly over the operator's changes (the editor; the list keeps it cheap)."""
    pack_id = definition.get("marketplace_pack_id")
    if not pack_id:
        return None
    out: dict[str, Any] = {
        "pack_id": pack_id,
        "pack_name": _pack_name(connection, str(pack_id)),
        "source": "boss" if pack_id in SHIPPED_PACKS else "org",
        "version": definition.get("marketplace_version"),
        "modified": _modified(connection, definition),
        "update": None,
        "waiting": definition.get("marketplace_waiting") or None,
    }
    if update_waiting(definition):
        update: dict[str, Any] = {"version": _current_version(connection, str(pack_id))}
        if detail:
            from .workflow_standards import baseline_state

            offered = (baseline_state(connection, definition) or {}).get("new_standard") or {}
            update.update({"changes": offered.get("changes") or [],
                           "applies_cleanly": bool(offered.get("applies_cleanly")),
                           "conflicts": offered.get("conflicts") or []})
        out["update"] = update
    return out


def _needs(connection: Connection, project_id: str, contents: dict[str, Any]) -> list[dict]:
    """What the pack needs from this project, each said once: {label, ok, note?}."""
    from .mcp_registry import find_mcp_server

    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    shipped: Optional[ShippedPack] = contents.get("shipped")
    if shipped and shipped.ontology_pack:
        from .knowledge.ontology_packs import has_pack

        have = has_pack(connection, project_id=project_id, pack_id=shipped.ontology_pack)
        out.append({"key": f"ontology:{shipped.ontology_pack}", "label": "Knowledge kinds",
                    "ok": True, "note": None if have else "added"})
    for wf in contents["workflows"]:
        entry = wf.get("entry") or {}
        for name in (entry.get("requires") or {}).get("mcp") or ():
            if name in seen:
                continue
            seen.add(name)
            ok = find_mcp_server(connection, project_id, name) is not None
            out.append({"key": f"mcp:{name}", "label": f"Connector {name}", "ok": ok,
                        "note": None if ok else "not set up"})
    return out


def _agents(connection: Connection, project_id: str, contents: dict[str, Any]) -> list[dict]:
    """The agents the pack's steps are the judgement of: {key, name, present}."""
    from .workflow_catalogue import roles_needed

    present = _present_roles(connection, project_id)
    keys: list[str] = []
    for wf in contents["workflows"]:
        if wf.get("entry"):
            keys += [r for r in roles_needed(wf["entry"]) if r not in keys]
    names = {str(r["role_key"]): str(r["display_name"] or r["role_key"]) for r in connection.execute(
        text("SELECT role_key, display_name FROM roles WHERE project_id = :p "
             "AND role_key = ANY(:k)"),
        {"p": project_id, "k": keys or [""]}).mappings()}
    out = []
    for key in keys:
        name = names.get(key)
        if not name:
            try:
                from ..config import get_settings
                from .specpack import SpecPackService

                spec = SpecPackService(get_settings()).load_role_spec(key)
                name = str((spec or {}).get("display_name") or (spec or {}).get("name") or "")
            except Exception:  # noqa: BLE001 — a name is words; never why a list fails
                name = ""
        out.append({"key": key, "name": name or key.replace("_", " ").title(),
                    "present": key in present})
    return out


def _step_line(spec: dict[str, Any], limit: int = 5) -> list[str]:
    from .command_center import is_marker_step

    steps = [s for s in spec.get("steps") or [] if isinstance(s, dict) and not is_marker_step(s)]
    return [workflows._step_label(s, str(s.get("id") or "")) for s in steps[:limit]]


def _inputs(spec: dict[str, Any]) -> list[str]:
    """What Start asks for, by title (the Library's "Input"): the required fields, or the first
    three when none is required. Short on purpose: the full form is the workflow's own."""
    schema = spec.get("input_schema") or {}
    props = schema.get("properties") if isinstance(schema, dict) else None
    if not isinstance(props, dict):
        return []
    order = [k for k in schema.get("required") or [] if k in props] or list(props)[:3]
    return [str((props[k] or {}).get("title") or k).replace(" (optional)", "")
            for k in order][:6]


def list_packs(connection: Connection, project_id: str, *,
               visible_to: Optional[set[str]] = None) -> list[dict[str, Any]]:
    """Every pack this project can install: BOSS's, then this org's (newest first), each with
    whether it is installed here, at which version, and whether an update waits for a person.
    `visible_to`: the project ids the caller may see; `installed_in` names only those (the
    count, `used_count`, is the whole org's)."""
    installed: dict[str, list[dict[str, Any]]] = {}
    for row in _installed_rows(connection, project_id):
        installed.setdefault(str(row["marketplace_pack_id"]), []).append(row)
    org_id = _org_of(connection, project_id)
    pack_ids = list(SHIPPED_PACKS) + [str(r) for r in connection.execute(
        text("SELECT id FROM workflow_packs WHERE org_id = :o AND status = 'published' "
             "ORDER BY updated_at DESC, id"), {"o": org_id}).scalars()]
    # WHERE EACH PACK IS ALREADY, across the org (the Library's "Added" for the project a person
    # picks under Add to project, and how many projects use it).
    installed_in: dict[str, list[str]] = {}
    for pack_id, where in connection.execute(
            text("SELECT DISTINCT d.marketplace_pack_id, d.project_id FROM workflow_definitions d "
                 "JOIN projects p ON p.id = d.project_id JOIN workspaces w ON w.id = p.workspace_id "
                 "WHERE w.org_id = :o AND d.marketplace_pack_id IS NOT NULL "
                 "AND d.status <> 'retired' ORDER BY d.project_id"), {"o": org_id}).all():
        installed_in.setdefault(str(pack_id), []).append(str(where))
    out = []
    for pack_id in pack_ids:
        contents = _pack_contents(connection, project_id, pack_id)
        rows = installed.get(pack_id, [])
        row = contents.get("row") or {}
        here = bool(row) and row.get("source_project_id") == project_id
        updates = [r for r in rows if update_waiting(r)]
        versions = sorted({str(r.get("marketplace_version") or "") for r in rows}, key=version_key)
        out.append({
            "id": contents["id"],
            "source": contents["source"],
            "name": contents["name"],
            "summary": contents["summary"],
            "publisher": contents["publisher"],
            "version": contents["version"],
            "updated_at": row["updated_at"].isoformat() if row.get("updated_at") else None,
            "workflows": [{"key": w["key"], "name": w["name"], "description": w["description"],
                           "steps": _step_line(w["spec"]),
                           "step_count": len(_step_line(w["spec"], limit=1000)),
                           "inputs": _inputs(w["spec"]),
                           "objective": str(w["spec"].get("objective") or "")[:1200]}
                          for w in contents["workflows"]],
            "tags": contents["tags"],
            # Every org project it is in, counted; the ids are narrowed by the API to the
            # projects the person may add to (`visible_to`).
            "used_count": len(installed_in.get(contents["id"], [])),
            "installed_in": [p for p in installed_in.get(contents["id"], [])
                             if visible_to is None or p in visible_to],
            "installed": bool(rows),
            "installed_version": versions[0] if versions else None,
            "update_available": contents["version"] if updates else None,
            "waiting": [r["marketplace_waiting"] for r in rows if r.get("marketplace_waiting")],
            "published_here": here,
            "needs": _needs(connection, project_id, contents),
            "agents": _agents(connection, project_id, contents),
        })
    return out


# ── installing ───────────────────────────────────────────────────────────────────────────────

def _offer(connection: Connection, project_id: str, definition_id: str, spec: dict[str, Any],
           actor: str) -> dict[str, Any]:
    """Publish a newer pack version beside the copy WITHOUT making it run: offered ("Review
    update"), whether or not the copy was changed here. For an org pack republished from another
    project: that project's publisher does not decide what runs here."""
    definition = connection.execute(
        text("SELECT * FROM workflow_definitions WHERE id = :id FOR UPDATE"),
        {"id": definition_id}).mappings().one()
    version = workflows.publish_version(connection, project_id=project_id,
                                        definition_id=definition_id, spec=spec, actor=actor,
                                        activate=False)
    if version["content_hash"] == definition.get("standard_content_hash"):
        return {**version, "offered": False}
    if definition.get("standard_available_version_id") != version["id"]:
        connection.execute(
            text("UPDATE workflow_definitions SET standard_available_version_id = :v, "
                 "updated_at = now() WHERE id = :id"), {"v": version["id"], "id": definition_id})
        emit_event(connection, project_id, "workflow.pack_update_available", actor,
                   "workflow_definition", definition_id,
                   {"workflow_key": definition["workflow_key"], "version": version["version"],
                    "pack_id": definition.get("marketplace_pack_id")})
    return {**version, "offered": True, "duplicate": False}


def _ship(connection: Connection, project_id: str, definition_id: str, spec: dict[str, Any],
          version: str, actor: str, *, wait_when_blocked: bool,
          offer_only: bool = False) -> dict[str, Any]:
    """Publish a pack version onto an installed copy through the standard baseline door
    (workflow_standards.record_shipped): unmodified takes it, modified is offered it.
    `offer_only`: offered either way (an org pack republished from another project)."""
    from .workflow_standards import record_shipped

    try:
        with connection.begin_nested():
            published = (_offer(connection, project_id, definition_id, spec, actor) if offer_only
                         else record_shipped(connection, definition_id, spec, actor))
    except (workflows.WorkflowBindingBlockedError, workflows.WorkflowValidationError) as blocked:
        if not wait_when_blocked:
            raise
        runnable = connection.execute(
            text("SELECT active_version_id FROM workflow_definitions WHERE id = :id"),
            {"id": definition_id}).scalar()
        if runnable:
            # A NEWER SPEC THAT CANNOT PUBLISH HERE never marks a runnable workflow as waiting:
            # the version it has keeps running; the next sync tries again.
            return {"version_id": None, "waiting": None, "offered": False, "changed": False}
        # NOT RUNNABLE HERE YET, AND SAID SO: published when the project gains what it lacks.
        connection.execute(
            text("UPDATE workflow_definitions SET marketplace_waiting = :w, updated_at = now() "
                 "WHERE id = :id"), {"w": str(blocked)[:1000], "id": definition_id})
        return {"version_id": None, "waiting": str(blocked)[:1000], "offered": False}
    offered = bool(published.get("offered"))
    # Written only when it changes: a boot sync of an unchanged pack touches nothing.
    connection.execute(
        text("UPDATE workflow_definitions SET marketplace_waiting = NULL, updated_at = now()"
             + ("" if offered else ", marketplace_version = :v") + " WHERE id = :id "
             "AND (marketplace_waiting IS NOT NULL"
             + ("" if offered else " OR marketplace_version IS DISTINCT FROM :v") + ")"),
        {"id": definition_id, **({} if offered else {"v": version})})
    return {"version_id": published["id"], "version": published["version"],
            "offered": offered, "waiting": None,
            "changed": not published.get("duplicate") or bool(published.get("reactivated"))}


def install(connection: Connection, project_id: str, pack_id: str,
            actor: str) -> dict[str, Any]:
    """Copy a pack's workflows into the project's own workflows. Idempotent: an installed pack
    is brought to its current version (a modified copy is offered it), a waiting one retried."""
    contents = _pack_contents(connection, project_id, pack_id)
    if contents["source"] == "org" and contents["row"]["source_project_id"] == project_id:
        raise MarketplaceError(f"'{contents['name']}' was published from this project, so its "
                               "workflow is already here.")
    if not contents["workflows"]:
        raise MarketplaceError(f"'{contents['name']}' has no workflows to install.")
    # REFUSE BEFORE WRITING: a workflow of the same key that is not this pack's copy.
    existing: dict[str, dict[str, Any]] = {}
    for wf in contents["workflows"]:
        row = connection.execute(
            text("SELECT * FROM workflow_definitions WHERE project_id = :p AND workflow_key = :k"),
            {"p": project_id, "k": wf["key"]}).mappings().first()
        if row is None:
            continue
        row = dict(row)
        if row.get("marketplace_pack_id") != contents["id"]:
            raise MarketplaceError(
                f"This project already has a workflow '{row['name']}' ({wf['key']}), so "
                f"'{contents['name']}' cannot be installed beside it. Rename or delete yours first.")
        existing[wf["key"]] = row
    shipped: Optional[ShippedPack] = contents.get("shipped")
    if shipped:
        # WHAT THE PACK'S STEPS STAND ON comes with it: the ontology kinds its contracts read,
        # and the role agents whose judgement its steps are (added if missing; a deleted one
        # stays deleted).
        if shipped.ontology_pack:
            from .knowledge.ontology_packs import has_pack, set_project_pack

            if not has_pack(connection, project_id=project_id, pack_id=shipped.ontology_pack):
                set_project_pack(connection, project_id=project_id,
                                 pack_id=shipped.ontology_pack, set_by=actor)
        from .shipped_cast import install_roles
        from .workflow_catalogue import roles_needed

        wanted = [r for wf in contents["workflows"] for r in roles_needed(wf["entry"])]
        if wanted:
            install_roles(connection, project_id, wanted, actor=actor)
        contents = _pack_contents(connection, project_id, pack_id)   # staffed with them
    results = []
    for wf in contents["workflows"]:
        row = existing.get(wf["key"])
        if row is None:
            definition_id = workflows.create_definition(
                connection, project_id=project_id, workflow_key=wf["key"], name=wf["name"],
                description=wf["description"], actor=actor,
                namespace=workflows.PRODUCT_NAMESPACE, is_catalogue=False)["id"]
            connection.execute(
                text("UPDATE workflow_definitions SET marketplace_pack_id = :pk, "
                     "marketplace_version = :v WHERE id = :id"),
                {"pk": contents["id"], "v": contents["version"], "id": definition_id})
        else:
            definition_id = str(row["id"])
            if row["status"] == "retired":
                # INSTALLED AGAIN after an uninstall that kept its history: the same workflow.
                connection.execute(
                    text("UPDATE workflow_definitions SET status = 'active', updated_at = now() "
                         "WHERE id = :id"), {"id": definition_id})
        shipped_result = _ship(connection, project_id, definition_id, wf["spec"],
                               contents["version"], actor, wait_when_blocked=bool(shipped))
        results.append({"key": wf["key"], "name": wf["name"], "definition_id": definition_id,
                        **shipped_result})
    emit_event(connection, project_id, "workflow.pack_installed", actor, "workflow_pack",
               contents["id"], {"pack_id": contents["id"], "version": contents["version"],
                                "workflows": [r["key"] for r in results]})
    return {"pack_id": contents["id"], "name": contents["name"], "version": contents["version"],
            "installed": results}


def is_installed(connection: Connection, project_id: str, pack_id: str) -> bool:
    return bool(_installed_rows(connection, project_id, pack_id))


def ensure_installed(connection: Connection, project_id: str, pack_id: str,
                     actor: str) -> Optional[dict[str, Any]]:
    """Install the pack where an explicit act needs it (loading the DQ platform, switching on
    Respond automatically), and nothing when it is here. Never at boot or on project create."""
    if is_installed(connection, project_id, pack_id):
        return None
    return install(connection, project_id, pack_id, actor)


def uninstall(connection: Connection, project_id: str, pack_id: str,
              actor: str) -> dict[str, Any]:
    """Remove the pack's workflows from the project. Refused while any of them has a run going
    (nothing is removed then); one that has run is retired, so its runs stay readable."""
    rows = _installed_rows(connection, project_id, pack_id)
    if not rows:
        raise MarketplaceNotFound(f"'{_pack_name(connection, pack_id)}' is not installed here.")
    live = connection.execute(
        text("SELECT d.name, count(*) AS n FROM workflow_runs r "
             "JOIN workflow_definitions d ON d.id = r.workflow_definition_id "
             "WHERE r.workflow_definition_id = ANY(:ids) AND NOT (r.status = ANY(:ended)) "
             "GROUP BY d.name ORDER BY d.name"),
        {"ids": [r["id"] for r in rows], "ended": list(_ENDED)}).mappings().all()
    if live:
        said = ", ".join(f"{r['name']} ({int(r['n'])})" for r in live)
        raise MarketplaceError(f"Runs are still going: {said}. Uninstall when they finish, or "
                               "stop them first.")
    removed = []
    for row in rows:
        result = workflows.delete_definition(connection, project_id, str(row["id"]), actor)
        removed.append({"key": row["workflow_key"], "outcome": result["outcome"]})
    # A schedule names its workflow by key: it would fire at nothing.
    connection.execute(
        text("UPDATE workflow_schedules SET enabled = false, updated_at = now() "
             "WHERE project_id = :p AND workflow_key = ANY(:k) AND enabled"),
        {"p": project_id, "k": [r["workflow_key"] for r in rows]})
    emit_event(connection, project_id, "workflow.pack_uninstalled", actor, "workflow_pack",
               pack_id, {"pack_id": pack_id, "workflows": [r["key"] for r in removed]})
    return {"pack_id": pack_id, "removed": removed}


def answer_update(connection: Connection, project_id: str, definition_id: str, actor: str,
                  act: str) -> dict[str, Any]:
    """"Review update" on a MODIFIED copy: `take` (the new version only), `take_keep` (with my
    changes applied, only when that is clean) or `dismiss` ("Not now")."""
    from . import workflow_standards as standards

    definition = workflows.require_namespace(connection, project_id, definition_id,
                                             workflows.PRODUCT_NAMESPACE)
    pack_id = definition.get("marketplace_pack_id")
    if not pack_id:
        raise MarketplaceError(f"'{definition['name']}' did not come from the Marketplace.")
    if act == "dismiss":
        standards.dismiss_new_standard(connection, project_id, definition_id, actor)
    elif act in ("take", "take_keep"):
        standards.take_new_standard(connection, project_id, definition_id, actor,
                                    keep_changes=act == "take_keep")
        connection.execute(
            text("UPDATE workflow_definitions SET marketplace_version = :v WHERE id = :id"),
            {"v": _current_version(connection, str(pack_id)), "id": definition_id})
    else:
        raise workflows.WorkflowValidationError("act is take, take_keep or dismiss")
    row = connection.execute(text("SELECT * FROM workflow_definitions WHERE id = :id"),
                             {"id": definition_id}).mappings().one()
    return {"definition_id": definition_id,
            "marketplace": install_state(connection, dict(row), detail=True)}


# ── keeping installed copies current ─────────────────────────────────────────────────────────

def _resync(connection: Connection, row: dict[str, Any], actor: str,
            offer_only: bool = False) -> Optional[dict]:
    contents = _pack_contents(connection, str(row["project_id"]), str(row["marketplace_pack_id"]),
                              for_install=False)
    wf = next((w for w in contents["workflows"] if w["key"] == row["workflow_key"]), None)
    if wf is None:
        return None
    return _ship(connection, str(row["project_id"]), str(row["id"]), wf["spec"],
                 contents["version"], actor, wait_when_blocked=contents["source"] == "boss",
                 offer_only=offer_only)


def projects_with_shipped_installs(connection: Connection) -> list[str]:
    return [str(p) for p in connection.execute(
        text("SELECT DISTINCT project_id FROM workflow_definitions WHERE marketplace_pack_id = "
             "ANY(:p) AND status <> 'retired' ORDER BY project_id"),
        {"p": list(SHIPPED_PACKS)}).scalars()]


def sync_shipped(connection: Connection, actor: str = "boss",
                 project_ids: Optional[list[str]] = None) -> dict[str, list[str]]:
    """BOSS's packs, as this code ships them, onto every installed copy (called at boot):
    unmodified copies take a newer version by themselves, modified ones are offered it.
    Idempotent: an unchanged pack publishes nothing."""
    rows = connection.execute(
        text("SELECT * FROM workflow_definitions WHERE marketplace_pack_id = ANY(:p) "
             "AND status <> 'retired' AND (CAST(:only AS TEXT[]) IS NULL "
             "OR project_id = ANY(CAST(:only AS TEXT[]))) ORDER BY project_id, workflow_key"),
        {"p": list(SHIPPED_PACKS), "only": project_ids}).mappings().all()
    out: dict[str, list[str]] = {}
    for row in rows:
        try:
            with connection.begin_nested():
                result = _resync(connection, dict(row), actor)
        except Exception:  # noqa: BLE001 — one project's copy must not stop the rest
            continue
        if result and result.get("changed"):
            out.setdefault(str(row["project_id"]), []).append(str(row["workflow_key"]))
    return out


def publish_waiting(connection: Connection, project_id: str, actor: str = "boss") -> list[str]:
    """Publish every installed copy that was waiting for something this project now has."""
    rows = connection.execute(
        text("SELECT * FROM workflow_definitions WHERE project_id = :p "
             "AND marketplace_pack_id IS NOT NULL AND marketplace_waiting IS NOT NULL "
             "AND status <> 'retired'"), {"p": project_id}).mappings().all()
    done = []
    for row in rows:
        result = _resync(connection, dict(row), actor)
        if result and not result.get("waiting"):
            done.append(str(row["workflow_key"]))
    return done


# ── publishing an org's own ──────────────────────────────────────────────────────────────────

def _pack_out(connection: Connection, row: dict[str, Any]) -> dict[str, Any]:
    return {"id": row["id"], "name": row["name"], "summary": row.get("summary") or "",
            "publisher": row["publisher"], "version": row["version"], "status": row["status"],
            "tags": _tags(row.get("tags")),
            "source_definition_id": row["source_definition_id"],
            "updated_at": row["updated_at"].isoformat() if row.get("updated_at") else None}


def published_from(connection: Connection, project_id: str,
                   definition_id: str) -> Optional[dict[str, Any]]:
    """The pack this workflow is published as, if any (for the Publish form and the card)."""
    row = connection.execute(
        text("SELECT * FROM workflow_packs WHERE source_definition_id = :d "
             "AND source_project_id = :p"), {"d": definition_id, "p": project_id}).mappings().first()
    return _pack_out(connection, dict(row)) if row else None


def publish_problems(connection: Connection, project_id: str,
                     spec: dict[str, Any]) -> list[dict[str, Any]]:
    """THE PUBLISH-TIME CHECKS, as a Save runs them (`workflows.spec_problems`): shape, binding,
    and whether each step's agent can do its work (a step whose agent holds no skill that can
    run here is refused). Run again at share time: what was saved weeks ago may not run now,
    and a shared workflow that cannot run is copied into every project that adds it."""
    _normal, shape, blocked = workflows.spec_problems(
        connection, project_id, spec, namespace=workflows.PRODUCT_NAMESPACE)
    return list(shape) + list(blocked)


def publish(connection: Connection, project_id: str, definition_id: str, actor: str, *,
            name: Optional[str] = None, summary: Optional[str] = None,
            version: Optional[str] = None, publisher: Optional[str] = None,
            tags: Optional[list[str]] = None) -> dict[str, Any]:
    """Publish one of this project's own workflows as a pack every project in the org can
    install. Republishing bumps the version (it must be later than the last) and offers it to
    every installed copy (each project takes it when it chooses). Refused, with every problem,
    when the publish-time checks fail (`publish_problems`)."""
    from .workflow_standards import _clean

    definition = workflows.require_namespace(connection, project_id, definition_id,
                                             workflows.PRODUCT_NAMESPACE)
    if definition.get("is_catalogue"):
        raise MarketplaceError(f"'{definition['name']}' is a standard BOSS ships. Save your own "
                               "copy to publish it.")
    if definition.get("marketplace_pack_id"):
        raise MarketplaceError(f"'{definition['name']}' came from the Marketplace, so it is "
                               "already there.")
    if definition.get("status") == "retired":
        raise MarketplaceError(f"'{definition['name']}' is retired.")
    active = connection.execute(
        text("SELECT spec, content_hash FROM workflow_versions WHERE id = :v"),
        {"v": definition.get("active_version_id")}).mappings().first()
    if not active:
        raise MarketplaceError(f"Save '{definition['name']}' first: it has no version to publish.")
    spec = _clean(_json(active["spec"], {}))
    problems = publish_problems(connection, project_id, spec)
    if problems:
        raise workflows.WorkflowBindingBlockedError(
            f"'{definition['name']}' cannot be shared: " + workflows.problems_sentence(problems),
            problems=problems)
    org_id = _org_of(connection, project_id)
    existing = connection.execute(
        text("SELECT * FROM workflow_packs WHERE source_definition_id = :d FOR UPDATE"),
        {"d": definition_id}).mappings().first()
    existing = dict(existing) if existing else None
    pack_name = (name or "").strip()[:120] or (existing or {}).get("name") or definition["name"]
    pack_summary = ((summary if summary is not None else (existing or {}).get("summary"))
                    or definition.get("description") or "").strip()[:300]
    wanted = (version or "").strip() or next_version((existing or {}).get("version"))
    if not _VERSION.match(wanted):
        raise workflows.WorkflowValidationError("A version is numbers and dots, like 1.0 or 0.3.")
    who = (publisher or "").strip()[:120] or actor
    pack_tags = _tags(tags if tags is not None else (existing or {}).get("tags"))
    item = {"workflow_key": definition["workflow_key"], "name": definition["name"],
            "description": definition.get("description") or "", "spec": spec,
            "content_hash": active["content_hash"]}
    if existing:
        if version_key(wanted) <= version_key(existing["version"]):
            raise MarketplaceError(f"It is at v{existing['version']}: publish a later version.")
        pack_id = str(existing["id"])
        connection.execute(
            text("UPDATE workflow_packs SET name = :n, summary = :s, version = :v, "
                 "status = 'published', publisher = :pub, published_by = :a, "
                 "tags = CAST(:t AS JSONB), updated_at = now() WHERE id = :id"),
            {"n": pack_name, "s": pack_summary, "v": wanted, "pub": who, "a": actor,
             "t": json.dumps(pack_tags), "id": pack_id})
    else:
        pack_id = make_id("wpk")
        connection.execute(
            text("INSERT INTO workflow_packs (id, org_id, name, summary, publisher, version, "
                 "source_project_id, source_definition_id, published_by, tags) VALUES (:id, :o, "
                 ":n, :s, :pub, :v, :p, :d, :a, CAST(:t AS JSONB))"),
            {"id": pack_id, "o": org_id, "n": pack_name, "s": pack_summary, "pub": who,
             "v": wanted, "p": project_id, "d": definition_id, "a": actor,
             "t": json.dumps(pack_tags)})
    connection.execute(
        text("INSERT INTO workflow_pack_versions (id, pack_id, version, workflows, published_by) "
             "VALUES (:id, :p, :v, CAST(:w AS JSONB), :a)"),
        {"id": make_id("wpv"), "p": pack_id, "v": wanted, "w": json.dumps([item]), "a": actor})
    emit_event(connection, project_id, "workflow.pack_published", actor, "workflow_pack",
               pack_id, {"pack_id": pack_id, "version": wanted,
                         "workflow_key": definition["workflow_key"]})
    # EVERY INSTALLED COPY IN THE ORG IS OFFERED IT ("Review update"): what runs in another
    # project is that project's call, never this publisher's (review of #301).
    updated = []
    for row in connection.execute(
            text("SELECT * FROM workflow_definitions WHERE marketplace_pack_id = :p "
                 "AND status <> 'retired'"), {"p": pack_id}).mappings().all():
        try:
            with connection.begin_nested():
                result = _resync(connection, dict(row), actor, offer_only=True)
        except workflows.WorkflowError as blocked:
            # SAID, NEVER DROPPED: that project keeps running the version it has, and the person
            # sharing is told which project could not take this one, and why.
            updated.append({"project_id": row["project_id"], "offered": False,
                            "blocked": str(blocked)[:500]})
            continue
        if result:
            updated.append({"project_id": row["project_id"], "offered": result.get("offered")})
    pack = connection.execute(text("SELECT * FROM workflow_packs WHERE id = :id"),
                              {"id": pack_id}).mappings().one()
    return {"pack": _pack_out(connection, dict(pack)), "installed_copies": updated,
            "blocked": [u for u in updated if u.get("blocked")]}


def unpublish(connection: Connection, project_id: str, pack_id: str,
              actor: str) -> dict[str, Any]:
    """Take a pack out of the org's Marketplace. Copies already installed stay where they are."""
    row = _org_pack(connection, _org_of(connection, project_id), pack_id)
    if row is None or row["source_project_id"] != project_id:
        raise MarketplaceNotFound("Only the project it was published from can unpublish it.")
    if row["status"] != "published":
        raise MarketplaceError(f"'{row['name']}' is not published.")
    connection.execute(
        text("UPDATE workflow_packs SET status = 'unpublished', updated_at = now() "
             "WHERE id = :id"), {"id": pack_id})
    emit_event(connection, project_id, "workflow.pack_unpublished", actor, "workflow_pack",
               pack_id, {"pack_id": pack_id})
    row = connection.execute(text("SELECT * FROM workflow_packs WHERE id = :id"),
                             {"id": pack_id}).mappings().one()
    return {"pack": _pack_out(connection, dict(row))}
