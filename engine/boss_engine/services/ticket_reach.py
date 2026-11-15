"""THE TICKET SYSTEMS A PROJECT CAN REACH — a ticket system is connected once, for the org.

Operator, 2026-11-15: "I added the Jira ticket system but it does not show up" and "it is
supposed to find the ticket from jira". The connection had been saved under the project the
Setup page happened to show (boss-dogfood), and every reader asked only "this project's
tracker", so from HelpDesk Laptop the card said "No ticket system yet", New work recognised no
key, and the agent answered "BOSS has no connector to pull a ticket by ID".

THE MODEL. A ticket-system connection (a ``work_tracker_providers`` row with
``configuration.ticket_system``, not a mirror) is ORG-LEVEL: it is stored on the project it was
added from (its HOME, where its settings and token live and who may manage it), and it is
visible and usable — read, search, import — from every project of the same organisation. Its
mappings (their project -> BOSS project) say where inbound tickets go and which BOSS project a
key belongs to; they no longer decide whether another project can see the connection. Legacy
tracker rows (the seeded plugin rows, a tracker connected for ticket CREATION) stay per
project, exactly as before.

WRITES KEEP THEIR PROJECT. A binding (work item <-> ticket) keeps a row in its own project, so
importing a ticket into project B through a connection homed in A first makes B a MIRROR row of
it (the B1 mechanism; :func:`project_row_for`), marked ``mirror_reason = "used"`` so a later edit
of A's mappings does not disconnect it. Every existing reader (write-back, the webhook, the
poller) keeps working unchanged.

ONE LOOKUP for every surface (:func:`lookup`): New work's resolver, the agent's ``ticket_get``
and the boss MCP. A key whose prefix a connection knows goes to that connection; an unknown
prefix is tried on every reachable key-based system, so the answer is always "found in <site>,
maps to <BOSS project>" or "not found in <site>" — never "BOSS has no connector".
"""
from __future__ import annotations

import logging
from typing import Any, Optional

from sqlalchemy import text
from sqlalchemy.engine import Connection

from ..connectors.tickets import NotFound, TicketRef
from ..connectors.tickets.refs import KEY_PATTERN, find_urls
from ..connectors.tickets.registry import REGISTRY
from . import ticket_intake as intake

logger = logging.getLogger(__name__)

#: How a mirror made by USING an org connection in another project is marked (sync_mirrors keeps
#: it when the home's mappings change; it is the project's own record of the connection).
USED_MIRROR = "used"


def org_of(db: Connection, project_id: str) -> Optional[str]:
    return db.execute(text("SELECT w.org_id FROM projects p JOIN workspaces w ON "
                           "w.id = p.workspace_id WHERE p.id = :p"), {"p": project_id}).scalar()


def project_name(db: Connection, project_id: str) -> str:
    return str(db.execute(text("SELECT name FROM projects WHERE id = :p"),
                          {"p": project_id}).scalar() or project_id)


def is_org_level(row: dict[str, Any]) -> bool:
    """A ticket-system HOME row: connected once, for the whole org."""
    config = row.get("configuration") or {}
    return bool(config.get("ticket_system")) and not config.get("mirror_of")


def home_id(row: dict[str, Any]) -> str:
    return str((row.get("configuration") or {}).get("mirror_of") or row["id"])


def reachable_rows(db: Connection, project_id: str) -> list[dict[str, Any]]:
    """Every connected ticket system project ``project_id`` can use: its own rows first (as
    before), then the org-level connections homed in the org's other projects. One connection
    appears once (a home that already has a mirror here is that mirror). Each row carries
    ``reach``: ``project`` or ``org``."""
    own = []
    families: set[str] = set()
    for row in intake.project_rows(db, project_id):
        own.append({**row, "reach": "project"})
        families.add(home_id(row))
    org = org_of(db, project_id)
    if org is None:
        return own
    others = db.execute(text(
        "SELECT t.id, t.project_id, t.provider_key, t.display_name, t.status, t.is_default, "
        "t.configuration, t.updated_at FROM work_tracker_providers t "
        "JOIN projects p ON p.id = t.project_id JOIN workspaces w ON w.id = p.workspace_id "
        "WHERE w.org_id = :o AND t.project_id <> :p AND t.status = 'active' "
          "AND p.archived_at IS NULL "
          "AND COALESCE(t.configuration->>'ticket_system', '') = 'true' "
          "AND COALESCE(t.configuration->>'mirror_of', '') = '' "
          "ORDER BY t.updated_at DESC"), {"o": org, "p": project_id}).mappings().all()
    for raw in others:
        if REGISTRY.get(str(raw["provider_key"])) is None or str(raw["id"]) in families:
            continue
        row = dict(raw)
        row["configuration"] = intake._config(raw)
        if intake.is_connected(row):
            own.append({**row, "reach": "org"})
            families.add(str(row["id"]))
    return own


def readable_rows(db: Connection, project_id: str) -> list[dict[str, Any]]:
    """The reachable connections that can READ tickets. GitHub Issues joins only when it was
    connected AS a ticket system (elsewhere it is the repository's own issues)."""
    out = []
    for row in reachable_rows(db, project_id):
        adapter = REGISTRY.get(str(row["provider_key"]))
        if adapter is None or not adapter.can("read"):
            continue
        if row["provider_key"] == "github_issues" and \
                not row["configuration"].get("ticket_system"):
            continue
        out.append(row)
    return out


def reachable_row(db: Connection, project_id: str, connection_id: str) -> dict[str, Any]:
    """A connection by id, if ``project_id`` can reach it (its own row, or an org-level one in
    the same org); else the same not-found a foreign id always got."""
    for row in reachable_rows(db, project_id):
        # By its own id, or by its home's id (the connection as another project knows it).
        if str(connection_id) in (str(row["id"]), home_id(row)):
            return row
    row = intake.get_row(db, connection_id)   # raises not_found for an unknown id
    if row["project_id"] == project_id:       # its own, but not connected right now
        return row
    raise intake.TicketApiError("not_found", "No such ticket-system connection.", status=404)


def site_of(row: dict[str, Any]) -> str:
    adapter = REGISTRY.get(str(row["provider_key"]))
    field = getattr(getattr(adapter, "manifest", None), "base_url_field", "") or "base_url"
    config = row.get("configuration") or {}
    return str(config.get(field) or config.get("base_url") or
               getattr(getattr(adapter, "manifest", None), "default_base_url", "") or "")


def describe(db: Connection, row: dict[str, Any]) -> dict[str, Any]:
    """Which connection, in words an agent can repeat."""
    home_project = str(row["project_id"])
    if row.get("configuration", {}).get("mirror_of"):
        home_project = str(db.execute(text("SELECT project_id FROM work_tracker_providers "
                                           "WHERE id = :h"), {"h": home_id(row)}).scalar()
                           or home_project)
    return {"connection_id": str(row["id"]), "provider": str(row["provider_key"]),
            "label": intake.label_of(row), "site": site_of(row),
            "home_project": home_project, "reach": row.get("reach") or "project"}


def mapped_project(db: Connection, row: dict[str, Any], key: str) -> dict[str, Any]:
    """The BOSS project a ticket belongs to: the mapping for its project key, else the
    connection's default mapping, else the connection's home project."""
    prefix = str(key or "").rsplit("-", 1)[0].upper() if "-" in str(key or "") else ""
    mappings = intake._stored_settings(row.get("configuration") or {}).get("mappings") or []
    target = ""
    for m in mappings:
        if isinstance(m, dict) and prefix and str(m.get("external_project") or "").upper() == prefix:
            target = str(m.get("boss_project_id") or "")
            break
    if not target:
        target = next((str(m.get("boss_project_id") or "") for m in mappings
                       if isinstance(m, dict) and m.get("default")), "")
    how = "mapping" if target else "home"
    if not target:
        target = describe(db, row)["home_project"]
    return {"project_id": target, "name": project_name(db, target), "by": how}


def _key_shaped(ref: str) -> bool:
    return bool(KEY_PATTERN.fullmatch(ref.strip().upper()))


def _owners(db: Connection, rows: list[dict[str, Any]], ref: str, *,
            person_id: Optional[str]) -> list[tuple[dict[str, Any], TicketRef]]:
    """The connections a reference belongs to: those that recognise it (a URL of their site, a
    key whose project they know); for a key no connection knows, every key-based system."""
    owners: list[tuple[dict[str, Any], TicketRef]] = []
    for row in rows:
        adapter = REGISTRY.get(str(row["provider_key"]))
        try:
            conn = intake.ticket_connection(db, row, person_id=person_id)
            found = adapter.parse_ref(ref, conn=conn)
        except Exception:  # noqa: BLE001 — a connection that cannot parse owns nothing
            continue
        if found:
            owners.append((row, found[0]))
    if owners or find_urls(ref) or not _key_shaped(ref):
        return owners
    key = ref.strip().upper()
    for row in rows:
        adapter = REGISTRY.get(str(row["provider_key"]))
        if getattr(adapter.manifest, "key_refs", False):
            owners.append((row, TicketRef(str(row["provider_key"]), key=key)))
    return owners


def not_connected(project_id: str) -> dict[str, Any]:
    return {"ok": False, "error": "not_connected",
            "message": "No ticket system is connected in this organisation, so BOSS cannot read "
                       "tickets yet.",
            "next": "Connect one in Setup › Ticket systems (Jira, Plane, GitHub Issues, Linear, "
                    "Azure DevOps). It is connected once and every project can use it."}


def lookup(db: Connection, project_id: str, ref: str, *, person_id: Optional[str] = None,
           full: bool = True, connection_id: Optional[str] = None,
           provider: Optional[str] = None) -> dict[str, Any]:
    """ONE TICKET by key or link, from every ticket system ``project_id`` can reach.

    Found: ``{ok, ticket, connection, maps_to, in_this_project, searched}``. Absent:
    ``{ok: False, error: "not_found", message: "LEND-142 was not found in <site> (Jira)."}``.
    Nothing connected: ``not_connected`` with the next step. Credentials resolve server-side
    (the person's own sign-in when they linked one, else the connection's)."""
    said = str(ref or "").strip()
    if not said:
        return {"ok": False, "error": "invalid_ref", "message": "Which ticket? Give its key "
                                                                "(KAN-12) or its link."}
    rows = readable_rows(db, project_id)
    if not rows:
        return not_connected(project_id)
    cid = str(connection_id or "").strip()
    wanted = str(provider or "").strip().lower()
    if cid.startswith("kit:tickets:"):
        wanted, cid = wanted or cid.rsplit(":", 1)[-1], ""
    if cid or wanted:
        rows = [r for r in rows if (not cid or str(r["id"]) == cid)
                and (not wanted or str(r["provider_key"]) == wanted)]
        if not rows:
            return {"ok": False, "error": "not_connected",
                    "message": f"No connected ticket system {cid or wanted} is reachable from "
                               "this project."}
    owners = _owners(db, rows, said, person_id=person_id)
    if not owners:
        sites = ", ".join(sorted({f"{site_of(r) or intake.label_of(r)} ({intake.label_of(r)})"
                                  for r in rows}))
        return {"ok": False, "error": "not_found",
                "message": f"{said} is not a ticket key or link any connected ticket system "
                           f"owns. Connected: {sites}.",
                "searched": [describe(db, r) for r in rows]}
    searched: list[dict[str, Any]] = []
    for row, tref in owners:
        where = describe(db, row)
        adapter = REGISTRY.get(str(row["provider_key"]))
        try:
            conn = intake.ticket_connection(db, row, person_id=person_id)
            ticket = adapter.get_ticket(conn, tref)
        except NotFound:
            searched.append({**where, "result": "not_found"})
            continue
        except Exception as error:  # noqa: BLE001 — typed into words
            typed = intake.explain(error, label=intake.label_of(row))
            searched.append({**where, "result": typed.code, "message": str(typed)})
            continue
        view = intake.ticket_view(ticket, full=full)
        view["connection_id"] = str(row["id"])
        view["in_boss"] = intake.mark([ticket], intake.in_boss_map(
            db, intake.family_ids(db, row)))[0]["in_boss"]
        maps_to = mapped_project(db, row, ticket.key or tref.label())
        return {"ok": True, "ticket": view, "connection": where, "maps_to": maps_to,
                "this_project": project_id,
                "in_this_project": maps_to["project_id"] == project_id,
                "searched": searched}
    label = tref.label() if owners else said
    places = ", ".join(f"{s['site'] or s['label']} ({s['label']})" for s in searched) or "—"
    failed = [s for s in searched if s["result"] != "not_found"]
    if not failed:
        return {"ok": False, "error": "not_found",
                "message": f"{label} was not found in {places}.", "searched": searched}
    first = failed[0]
    return {"ok": False, "error": first["result"],
            "message": f"{label} could not be read: {first.get('message') or first['result']}"
                       + (f" (also tried {places})" if len(searched) > 1 else ""),
            "searched": searched}


def project_row_for(db: Connection, app_settings: Any, project_id: str, row: dict[str, Any], *,
                    actor: str) -> dict[str, Any]:
    """The connection's row IN ``project_id``, made when this project first uses an org-level
    connection homed elsewhere (a ``used`` mirror: same site, same settings, the token copied
    into this project's vault like every B1 mirror). Refused when the project already has its
    own, different connection to that system."""
    found = intake._project_row(db, project_id, row)
    if found is not None and intake.is_connected(found):
        return found
    home = intake.get_row(db, home_id(row))
    if home["project_id"] == project_id:
        return home
    if org_of(db, home["project_id"]) != org_of(db, project_id):
        raise intake.TicketApiError("not_found", "No such ticket-system connection.", status=404)
    provider = str(home["provider_key"])
    existing = db.execute(text(intake._ROW + " WHERE project_id = :p AND provider_key = :k"),
                          {"p": project_id, "k": provider}).mappings().first()
    if existing is not None:
        existing = dict(existing, configuration=intake._config(existing))
        if intake.is_connected(existing) and existing["configuration"].get("mirror_of") != home["id"]:
            raise intake.TicketApiError(
                "conflict", f"This project has its own {intake.label_of(provider)} connection; "
                            "import through that one.", status=409)
    if app_settings is None:
        from ..config import get_settings

        app_settings = get_settings()
    config = home["configuration"]
    mirrored = {k: v for k, v in config.items()
                if k not in ("secret_ref_id", "last4", "fingerprint", "mirror_of",
                              "webhook_secret_ref", "webhook")}
    mirrored["mirror_reason"] = USED_MIRROR
    token = (intake.stored_token(db, app_settings, home) if config.get("secret_ref_id")
             else str(config.get("api_token") or ""))
    return intake.store(db, app_settings, project_id=project_id,
                        adapter=intake.adapter_for(provider), configuration=mirrored,
                        token=token, outcome={"account": config.get("acts_as")}, actor=actor,
                        mirror_of=str(home["id"]))
