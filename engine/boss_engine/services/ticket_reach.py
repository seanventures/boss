"""THE TICKET SYSTEMS A PROJECT CAN REACH — its own, and the ones SHARED with the whole org.

Operator, 2026-11-15: "I added the Jira ticket system but it does not show up" and "it is
supposed to find the ticket from jira". The connection had been saved under the project the
Setup page happened to show (boss-dogfood), and every reader asked only "this project's
tracker", so from HelpDesk Laptop the card said "No ticket system yet", New work recognised no
key, and the agent answered "BOSS has no connector to pull a ticket by ID".

THE MODEL. A ticket-system connection (a ``work_tracker_providers`` row with
``configuration.ticket_system``, not a mirror) lives on the project it was added from: its HOME,
where its settings and token are and where it is managed (integration:manage there). It reaches
other projects only when a person who manages it there turned on SHARE WITH THE WHOLE ORG
(``settings.share_with_org``), having been told in plain words that everyone in the org and their
agents then read tickets through it with its owner's token. Legacy rows and new connections with
a personal token start unshared: nothing is widened silently. An unshared connection reaches
only its home project (the behaviour before), and another project is told it exists and is not
shared with it — never handed its tickets.

A SHARED READ from another project:
  * prefers the reader's own sign-in to the system when they linked one (Atlassian OAuth), and
    falls back to the connection's token;
  * is receipted (``ticket.cross_project_read``: reader, project, connection, key);
  * never copies the token. A project that IMPORTS through it gets a MIRROR row (the B1
    mechanism, :func:`project_row_for`, ``mirror_reason = "used"``) that carries no secret: every
    read and write-back resolves the credential from the HOME, so rotating or disconnecting it
    there applies everywhere. Making that mirror needs the connection shared, or
    integration:manage on the importing project.

ONE LOOKUP for every surface (:func:`lookup`): New work's resolver, the agent's ``ticket_get``
and the boss MCP. A key whose prefix a connection knows goes to that connection; an unknown
prefix is tried on the reachable key-based systems (in parallel, at most
:data:`MAX_FAN_OUT`, each under :data:`PER_CONNECTION_SECONDS`, the outcome cached briefly per
org and key), so the answer is "found in <site>, maps to <BOSS project>", "not found in <site>",
"not shared with this project", or "nothing connected" — never "BOSS has no connector".
"""
from __future__ import annotations

import logging
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor, wait
from typing import Any, Callable, Optional

from sqlalchemy import text
from sqlalchemy.engine import Connection

from ..connectors.tickets import NotFound, Ticket, TicketRef
from ..connectors.tickets.refs import KEY_PATTERN, find_urls
from ..connectors.tickets.registry import REGISTRY
from . import ticket_intake as intake

logger = logging.getLogger(__name__)

#: How a mirror made by USING a shared connection in another project is marked (sync_mirrors
#: keeps it when the home's mappings change). It carries no secret.
USED_MIRROR = "used"
#: The home row's setting that shares a connection with every project of its org.
SHARE_KEY = "share_with_org"
#: An unknown key prefix is tried on at most this many connections (most recently used first),
#: each read given this long, and the outcome kept this long per (org, key).
MAX_FAN_OUT = 5
PER_CONNECTION_SECONDS = 8.0
CACHE_SECONDS = 180.0
#: Keyed by (project, the reachable connection ids, key): what one project may read — and the
#: sites named in its "not found" — is never served to another project, and a sharing change
#: (a different reachable set) is a different entry.
_CACHE: dict[tuple[str, frozenset, str], tuple[float, str, str]] = {}
_CACHE_LOCK = threading.Lock()


def org_of(db: Connection, project_id: str) -> Optional[str]:
    return db.execute(text("SELECT w.org_id FROM projects p JOIN workspaces w ON "
                           "w.id = p.workspace_id WHERE p.id = :p"), {"p": project_id}).scalar()


def project_name(db: Connection, project_id: str) -> str:
    return str(db.execute(text("SELECT name FROM projects WHERE id = :p"),
                          {"p": project_id}).scalar() or project_id)


def is_shared(row: dict[str, Any]) -> bool:
    """A ticket-system HOME row a person shared with the whole org (settings.share_with_org)."""
    config = row.get("configuration") or {}
    if not config.get("ticket_system") or config.get("mirror_of"):
        return False
    return bool(intake._stored_settings(config).get(SHARE_KEY))


def home_id(row: dict[str, Any]) -> str:
    return str((row.get("configuration") or {}).get("mirror_of") or row["id"])


def _org_homes(db: Connection, project_id: str) -> list[dict[str, Any]]:
    """The connected ticket-system HOME rows of the org's other (unarchived) projects."""
    org = org_of(db, project_id)
    if org is None:
        return []
    found = db.execute(text(
        "SELECT t.id, t.project_id, t.provider_key, t.display_name, t.status, t.is_default, "
        "t.configuration, t.updated_at FROM work_tracker_providers t "
        "JOIN projects p ON p.id = t.project_id JOIN workspaces w ON w.id = p.workspace_id "
        "WHERE w.org_id = :o AND t.project_id <> :p AND t.status = 'active' "
        "AND p.archived_at IS NULL "
        "AND COALESCE(t.configuration->>'ticket_system', '') = 'true' "
        "AND COALESCE(t.configuration->>'mirror_of', '') = '' "
        "ORDER BY t.updated_at DESC"), {"o": org, "p": project_id}).mappings().all()
    out = []
    for raw in found:
        if REGISTRY.get(str(raw["provider_key"])) is None:
            continue
        row = dict(raw)
        row["configuration"] = intake._config(raw)
        if intake.is_connected(row):
            out.append(row)
    return out


def reachable_rows(db: Connection, project_id: str) -> list[dict[str, Any]]:
    """Every connected ticket system project ``project_id`` can use: its own rows first (as
    before), then the connections homed in the org's other projects that are SHARED with the
    whole org. One connection appears once. Each row carries ``reach``: ``project`` or ``org``."""
    own = []
    families: set[str] = set()
    for row in intake.project_rows(db, project_id):
        own.append({**row, "reach": "project"})
        families.add(home_id(row))
    for row in _org_homes(db, project_id):
        if str(row["id"]) not in families and is_shared(row):
            own.append({**row, "reach": "org"})
            families.add(str(row["id"]))
    return own


def unshared_rows(db: Connection, project_id: str) -> list[dict[str, Any]]:
    """Connections of the org's other projects NOT shared with the whole org (and not this
    project's own): named to this project so it can say "not shared with this project", never
    read through."""
    families = {home_id(r) for r in intake.project_rows(db, project_id)}
    return [{**row, "reach": "unshared"} for row in _org_homes(db, project_id)
            if not is_shared(row) and str(row["id"]) not in families]


def readable_rows(db: Connection, project_id: str) -> list[dict[str, Any]]:
    """The reachable connections that can READ tickets. GitHub Issues joins only when it was
    connected AS a ticket system (elsewhere it is the repository's own issues)."""
    return [row for row in reachable_rows(db, project_id) if _reads(row)]


def _reads(row: dict[str, Any]) -> bool:
    adapter = REGISTRY.get(str(row["provider_key"]))
    if adapter is None or not adapter.can("read"):
        return False
    return not (row["provider_key"] == "github_issues"
                and not row["configuration"].get("ticket_system"))


def _project_owner_names(db: Connection, project_id: str, limit: int) -> list[str]:
    return [str(n) for n in db.execute(text(
        "SELECT p.display_name FROM identity_role_bindings b JOIN identity_principals p "
        "ON p.id = b.subject_id WHERE b.subject_type = 'principal' AND b.role_id = 'arl_owner' "
        "AND b.scope_type = 'project' AND b.scope_id = :p AND p.kind = 'user' "
        "AND p.status = 'active' AND NOT p.network_bound "
        "AND p.id NOT IN ('usr_legacy_shared', 'usr_network_operator') "
        "ORDER BY b.granted_at, p.display_name LIMIT :n"),
        {"p": project_id, "n": limit}).scalars().all() if n]


def org_owner_names(db: Connection, project_id: str, *, limit: int = 3) -> list[str]:
    """The org's Owners (display names), for "Ask an org owner to share it"."""
    org = org_of(db, project_id)
    if org is None:
        return []
    from ..identity.bindings import OWNER_ROLE_ID

    return [str(n) for n in db.execute(text(
        "SELECT DISTINCT p.display_name FROM identity_role_bindings b "
        "JOIN identity_principals p ON p.id = b.subject_id WHERE b.org_id = :o "
        "AND b.role_id = :owner AND b.scope_type = 'org' AND b.subject_type = 'principal' "
        "AND (b.expires_at IS NULL OR b.expires_at > now()) AND p.kind = 'user' "
        "AND p.status = 'active' AND NOT p.network_bound "
        "AND p.id NOT IN ('usr_legacy_shared', 'usr_network_operator') "
        "ORDER BY p.display_name LIMIT :n"),
        {"o": org, "owner": OWNER_ROLE_ID, "n": limit}).scalars().all() if n]


def owner_names(db: Connection, project_id: str, *, limit: int = 3) -> list[str]:
    """Who to ask about a connection homed in ``project_id``: the people who hold Owner on that
    project, else the org's Owners. Display names only (never an email or an id)."""
    return _project_owner_names(db, project_id, limit) or org_owner_names(db, project_id,
                                                                            limit=limit)


#: How a project the caller may not read is named (never its name or id).
ANOTHER_PROJECT = "another project"


def may_see_project(project_id: str, *, person_id: Optional[str] = None, db: Any = None) -> bool:
    """WHAT A TICKET SYSTEM SAYS ABOUT ITS HOME (PR #140 review): ticket systems are an org-wide
    setting, but a project the caller cannot read (project:read) stays hidden, as /v1/projects
    hides it. ``person_id``: answer for THAT person (an agent's tool acting for them, so the agent
    never tells a person names the console hides). Otherwise the request's principal; when
    identity is claimed and authz is not off, an unknown/agent principal outside an HTTP request
    fails closed (hidden) instead of leaking project names."""
    if person_id and db is not None:
        try:
            from ..identity import authz
            from ..identity.scope import ResolvedScope
            from ..identity.users import load_principal

            person = load_principal(db, person_id)
            if person is not None:
                held = authz.effective_permissions(
                    person, ResolvedScope.in_project(person.org_id, project_id), db=db).permissions
                return "project:read" in held
            # Unknown person_id when identity is claimed: fall through to viewer/fail-closed below.
        except Exception:  # noqa: BLE001 — cannot tell: hidden, never shown
            return False
    from ..identity import viewer

    said = viewer.may(project_id, "project:read", db=db)
    if said is not None:
        return bool(said)
    if db is not None:
        try:
            from ..config import get_settings
            from ..identity import claim

            mode = str(getattr(get_settings(), "authz_mode", "enforce") or "enforce").strip().lower()
            org = org_of(db, project_id)
            if mode != "off" and org and claim.is_claimed(db, org_id=org):
                return False
        except Exception:  # noqa: BLE001 — cannot tell: hidden, never shown
            return False
    return True


def home_view(db: Connection, project_id: str, *, person_id: Optional[str] = None) -> dict[str, Any]:
    """A connection's home project as the console names it, with whom to ask about it — or,
    for a project the caller cannot read, "another project" and the org's Owners."""
    if may_see_project(project_id, person_id=person_id, db=db):
        return {"id": project_id, "name": project_name(db, project_id),
                "owners": owner_names(db, project_id)}
    return {"id": "", "name": ANOTHER_PROJECT, "owners": org_owner_names(db, project_id),
            "hidden": True}


def not_shared_error(db: Connection, row: dict[str, Any], *, person_id: Optional[str] = None
                     ) -> intake.TicketApiError:
    label = intake.label_of(row)
    home = home_view(db, str(row["project_id"]), person_id=person_id)
    if home.get("hidden"):
        return intake.TicketApiError(
            "not_shared", f"The {label} connection was added in another project and is not "
                          "shared with this project. An org owner can share it with the whole "
                          "org in Setup › Ticket systems.",
            status=403, home_project="", home_project_name=ANOTHER_PROJECT,
            connection_id=str(row["id"]), label=label, owners=home["owners"])
    return intake.TicketApiError(
        "not_shared", f"The {label} connection was added in {home['name']} and is not "
                      "shared with this project. A person who manages integrations there can "
                      "turn on \"Share with the whole org\" in Setup › Ticket systems.",
        status=403, home_project=str(row["project_id"]), home_project_name=home["name"],
        connection_id=str(row["id"]), label=label, owners=home["owners"])


def org_connections(db: Connection, project_id: str) -> list[dict[str, Any]]:
    """EVERY ticket-system connection of ``project_id``'s org, whatever project it was added
    from (Setup › Ticket systems › All): the HOME rows (connected, or disconnected there), never
    a mirror. Each carries ``usable_here`` (its own, or shared with the org, or mirrored into
    ``project_id``) so the console can filter to the project. Never a token."""
    org = org_of(db, project_id)
    if org is None:
        return []
    found = db.execute(text(
        "SELECT t.id, t.project_id, t.provider_key, t.display_name, t.status, t.is_default, "
        "t.configuration, t.updated_at FROM work_tracker_providers t "
        "JOIN projects p ON p.id = t.project_id JOIN workspaces w ON w.id = p.workspace_id "
        "WHERE w.org_id = :o AND p.archived_at IS NULL AND t.status IN ('active', 'disconnected') "
        "AND COALESCE(t.configuration->>'ticket_system', '') = 'true' "
        "AND COALESCE(t.configuration->>'mirror_of', '') = '' "
        "ORDER BY t.updated_at DESC"), {"o": org}).mappings().all()
    mirrored = {str(m) for m in db.execute(text(
        "SELECT configuration->>'mirror_of' FROM work_tracker_providers WHERE project_id = :p "
        "AND status = 'active' AND COALESCE(configuration->>'mirror_of', '') <> ''"),
        {"p": project_id}).scalars().all()}
    out = []
    for raw in found:
        if REGISTRY.get(str(raw["provider_key"])) is None:
            continue
        row = dict(raw)
        row["configuration"] = intake._config(raw)
        if row["status"] == "active" and not intake.is_connected(row):
            continue
        out.append({**row, "shared": is_shared(row),
                    "usable_here": str(row["project_id"]) == project_id
                    or str(row["id"]) in mirrored
                    or (is_shared(row) and row["status"] == "active")})
    return out


def unshared_ref(db: Connection, project_id: str, said: str,
                 *, person_id: Optional[str] = None) -> Optional[dict[str, Any]]:
    """The first ticket key in ``said`` that only a connection NOT shared with this project
    knows (its project keys), as that connection's ``not_shared`` refusal with the key as
    ``ref``. Nothing is read from the ticket system: the key's prefix is matched locally."""
    hidden = [r for r in unshared_rows(db, project_id) if _reads(r)]
    if not hidden:
        return None
    for token in dict.fromkeys(_KEY_IN_TEXT.findall(str(said or "")[:20_000])):
        owners = _recognised(db, hidden, token, person_id=None)
        if owners:
            return {"ref": token, **not_shared_error(db, owners[0][0], person_id=person_id).body()}
    return None


_KEY_IN_TEXT = re.compile(r"\b[A-Z][A-Z0-9_]{1,9}-\d+\b")


def reachable_row(db: Connection, project_id: str, connection_id: str) -> dict[str, Any]:
    """A connection by id (its own, or its home's), if ``project_id`` can reach it: its own row,
    or one shared with the org. An unshared one of another project is refused as not shared."""
    for row in reachable_rows(db, project_id):
        if str(connection_id) in (str(row["id"]), home_id(row)):
            return row
    row = intake.get_row(db, connection_id)   # raises not_found for an unknown id
    if row["project_id"] == project_id:       # its own, but not connected right now
        return row
    for other in unshared_rows(db, project_id):
        if str(other["id"]) == str(connection_id):
            raise not_shared_error(db, other)
    raise intake.TicketApiError("not_found", "No such ticket-system connection.", status=404)


def audit_read(db: Connection, project_id: str, row: dict[str, Any], what: str, *,
               reader: Optional[str]) -> None:
    """A receipt for every read through a connection homed in ANOTHER project: who read, in
    which project, through which connection, what (a key, or a search)."""
    if row.get("reach") != "org":
        return
    from .core import make_id
    from .receipts import ReceiptService

    tenant = db.execute(text("SELECT w.org_id, p.workspace_id FROM projects p JOIN workspaces w "
                             "ON w.id = p.workspace_id WHERE p.id = :p"),
                        {"p": project_id}).first()
    if tenant is None:
        return
    record = {"reader": str(reader or "agent"), "project_id": project_id,
              "connection_id": str(row["id"]), "home_project": str(row["project_id"]),
              "provider": str(row["provider_key"]), "what": str(what)[:300]}
    with db.begin_nested():
        ReceiptService().record_completed(
            db, str(tenant[0]), str(tenant[1]), project_id,
            f"ticket_read:{make_id('tkr')}", "ticket.cross_project_read",
            {"connection_id": str(row["id"]), "home_project": str(row["project_id"])}, record)


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


def visible_maps_to(maps_to: dict[str, Any], *, person_id: Optional[str] = None,
                    db: Any = None) -> dict[str, Any]:
    """``maps_to`` for a person: a BOSS project they cannot read is "another project"."""
    if maps_to.get("project_id") and not may_see_project(str(maps_to["project_id"]),
                                                         person_id=person_id, db=db):
        return {**maps_to, "project_id": "", "name": ANOTHER_PROJECT}
    return maps_to


def _key_shaped(ref: str) -> bool:
    return bool(KEY_PATTERN.fullmatch(ref.strip().upper()))


def _recognised(db: Connection, rows: list[dict[str, Any]], ref: str, *,
                person_id: Optional[str]) -> list[tuple[dict[str, Any], TicketRef]]:
    """The connections that RECOGNISE a reference: a link of their site, or a key whose project
    they know."""
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
    return owners


def _key_based(rows: list[dict[str, Any]], key: str) -> list[tuple[dict[str, Any], TicketRef]]:
    """For a key no connection knows the project of: the key-based systems, most recently used
    first, at most MAX_FAN_OUT."""
    picked = [r for r in rows
              if getattr(REGISTRY.get(str(r["provider_key"])).manifest, "key_refs", False)]
    picked.sort(key=lambda r: str(r.get("updated_at") or ""), reverse=True)
    return [(r, TicketRef(str(r["provider_key"]), key=key)) for r in picked[:MAX_FAN_OUT]]


def not_connected(project_id: str) -> dict[str, Any]:
    return {"ok": False, "error": "not_connected",
            "message": "No ticket system is connected to this project, and none is shared with "
                       "the whole organisation, so BOSS cannot read tickets here yet.",
            "next": "Connect one in Setup › Ticket systems (Jira, Plane, GitHub Issues, Linear, "
                    "Azure DevOps), or ask the person who connected one in another project to "
                    "share it with the whole org."}


def nothing_reachable(db: Connection, project_id: str, *, provider: Optional[str] = None,
                      person_id: Optional[str] = None) -> dict[str, Any]:
    """Why no connection can serve this project: ``not_shared`` when another project of the org
    has one that is not shared with it (so search, browse, comment and import say the same as
    ticket_get), else ``not_connected``."""
    wanted = str(provider or "").strip().lower()
    if wanted.startswith("kit:tickets:"):
        wanted = wanted.rsplit(":", 1)[-1]
    hidden = [r for r in unshared_rows(db, project_id)
              if _reads(r) and (not wanted or str(r["provider_key"]) == wanted)]
    if hidden:
        return {"ok": False, **not_shared_error(db, hidden[0], person_id=person_id).body()}
    return not_connected(project_id)


def _cache_key(project_id: str, rows: list[dict[str, Any]], key: str
               ) -> tuple[str, frozenset, str]:
    return (project_id, frozenset(str(r["id"]) for r in rows), key)


def _cached(where: tuple[str, frozenset, str]) -> Optional[tuple[str, str]]:
    with _CACHE_LOCK:
        hit = _CACHE.get(where)
        if hit is None or hit[0] < time.monotonic():
            _CACHE.pop(where, None)
            return None
        return hit[1], hit[2]


def _remember(where: tuple[str, frozenset, str], connection_id: str, message: str) -> None:
    with _CACHE_LOCK:
        _CACHE[where] = (time.monotonic() + CACHE_SECONDS, connection_id, message)


def clear_cache() -> None:
    with _CACHE_LOCK:
        _CACHE.clear()


def _read_one(db: Connection, row: dict[str, Any], tref: TicketRef, person_id: Optional[str]
              ) -> tuple[str, Optional[Ticket], str]:
    """("found", ticket, "") | ("not_found", None, "") | (<code>, None, <words>)."""
    adapter = REGISTRY.get(str(row["provider_key"]))
    try:
        conn = intake.ticket_connection(db, row, person_id=person_id)
        return "found", adapter.get_ticket(conn, tref), ""
    except NotFound:
        return "not_found", None, ""
    except Exception as error:  # noqa: BLE001 — typed into words
        typed = intake.explain(error, label=intake.label_of(row))
        return typed.code, None, str(typed)


def _read_many(db: Connection, owners: list[tuple[dict[str, Any], TicketRef]],
               person_id: Optional[str]) -> list[tuple[str, Optional[Ticket], str]]:
    """Each owner read once: in this transaction when there is one, else in parallel, each on
    its own connection and under PER_CONNECTION_SECONDS (a slow site is a timeout, not a
    hang)."""
    if len(owners) <= 1:
        return [_read_one(db, row, tref, person_id) for row, tref in owners]
    engine = db.engine

    def one(row: dict[str, Any], tref: TicketRef) -> tuple[str, Optional[Ticket], str]:
        with engine.begin() as own:
            return _read_one(own, row, tref, person_id)

    pool = ThreadPoolExecutor(max_workers=min(len(owners), MAX_FAN_OUT))
    try:
        futures = [pool.submit(one, row, tref) for row, tref in owners]
        wait(futures, timeout=PER_CONNECTION_SECONDS)
        out = []
        for future in futures:
            if future.done() and future.exception() is None:
                out.append(future.result())
            elif future.done():
                out.append(("ticket_system_error", None, "the read failed"))
            else:
                out.append(("timeout", None, f"no answer within {PER_CONNECTION_SECONDS:.0f}s"))
        return out
    finally:
        pool.shutdown(wait=False, cancel_futures=True)


def lookup(db: Connection, project_id: str, ref: str, *, person_id: Optional[str] = None,
           full: bool = True, connection_id: Optional[str] = None,
           provider: Optional[str] = None, reader: Optional[str] = None) -> dict[str, Any]:
    """ONE TICKET by key or link, from the ticket systems ``project_id`` can reach (its own, and
    ones shared with the whole org).

    Found: ``{ok, ticket, connection, maps_to, in_this_project, searched}``. Absent:
    ``{ok: False, error: "not_found", message: "LEND-142 was not found in <site> (Jira)."}``.
    Owned by a connection of another project that is not shared: ``not_shared``. Nothing
    connected: ``not_connected`` with the next step. Credentials resolve server-side (the
    reader's own sign-in when they linked one, else the connection's). A read through another
    project's connection is receipted."""
    said = str(ref or "").strip()
    if not said:
        return {"ok": False, "error": "invalid_ref", "message": "Which ticket? Give its key "
                                                                "(KAN-12) or its link."}
    rows = readable_rows(db, project_id)
    cid = str(connection_id or "").strip()
    wanted = str(provider or "").strip().lower()
    if cid.startswith("kit:tickets:"):
        wanted, cid = wanted or cid.rsplit(":", 1)[-1], ""
    if cid or wanted:
        # By its own id or its home's (as reachable_row), or by its system.
        rows = [r for r in rows if (not cid or cid in (str(r["id"]), home_id(r)))
                and (not wanted or str(r["provider_key"]) == wanted)]
    owners = _recognised(db, rows, said, person_id=person_id)
    if not owners:
        # Before guessing: does a connection NOT shared with this project own it?
        hidden = [r for r in unshared_rows(db, project_id) if _reads(r)
                  and (not wanted or str(r["provider_key"]) == wanted)]
        if _recognised(db, hidden, said, person_id=None):
            row = _recognised(db, hidden, said, person_id=None)[0][0]
            error = not_shared_error(db, row, person_id=person_id)
            return {"ok": False, **error.body()}
        if not rows:
            if cid or wanted:
                return {"ok": False, "error": "not_connected",
                        "message": f"No connected ticket system {cid or wanted} is reachable "
                                   "from this project."}
            if hidden:
                return {"ok": False, **not_shared_error(db, hidden[0], person_id=person_id).body()}
            return not_connected(project_id)
        if find_urls(said) or not _key_shaped(said):
            sites = ", ".join(sorted({f"{site_of(r) or intake.label_of(r)} "
                                      f"({intake.label_of(r)})" for r in rows}))
            return {"ok": False, "error": "not_found",
                    "message": f"{said} is not a ticket key or link any connected ticket system "
                               f"owns. Connected: {sites}.",
                    "searched": [describe(db, r) for r in rows]}
        key = said.upper()
        slot = _cache_key(project_id, rows, key)
        hit = _cached(slot)
        if hit is not None and not hit[0]:
            return {"ok": False, "error": "not_found", "message": hit[1], "cached": True}
        owners = _key_based(rows, key)
        if hit is not None:
            owners = [o for o in owners if str(o[0]["id"]) == hit[0]] or owners
        guessed = True
    else:
        guessed, slot = False, None
    searched: list[dict[str, Any]] = []
    for (row, tref), (result, ticket, words) in zip(owners, _read_many(db, owners, person_id)):
        where = describe(db, row)
        if ticket is None:
            searched.append({**where, "result": result, **({"message": words} if words else {})})
            continue
        if guessed:
            _remember(slot, str(row["id"]), "")
        audit_read(db, project_id, row, ticket.key or tref.label(), reader=reader or person_id)
        view = intake.ticket_view(ticket, full=full)
        view["connection_id"] = str(row["id"])
        view["in_boss"] = intake.mark([ticket], intake.in_boss_map(
            db, intake.family_ids(db, row)))[0]["in_boss"]
        maps_to = visible_maps_to(mapped_project(db, row, ticket.key or tref.label()),
                                  person_id=person_id, db=db)
        return {"ok": True, "ticket": view, "connection": where, "maps_to": maps_to,
                "this_project": project_id,
                "in_this_project": maps_to["project_id"] == project_id,
                "searched": searched}
    label = owners[0][1].label() if owners else said
    places = ", ".join(f"{s['site'] or s['label']} ({s['label']})" for s in searched) or "—"
    failed = [s for s in searched if s["result"] != "not_found"]
    if not failed:
        message = f"{label} was not found in {places}."
        if guessed:
            _remember(slot, "", message)
        return {"ok": False, "error": "not_found", "message": message, "searched": searched}
    first = failed[0]
    return {"ok": False, "error": first["result"],
            "message": f"{label} could not be read: {first.get('message') or first['result']}"
                       + (f" (also tried {places})" if len(searched) > 1 else ""),
            "searched": searched}


#: What a USED mirror copies from its home: where the site is and how tickets map — never a
#: credential (a token, a secret reference, a host credential reference, OAuth app secrets).
_MIRROR_KEEPS = ("base_url", "api_base_url", "web_base_url", "email", "project_key",
                 "project_identifier", "workspace_slug", "organization", "project", "repository",
                 "team_key", "issue_type", "settings", "ticket_system", "acts_as")


def project_row_for(db: Connection, app_settings: Any, project_id: str, row: dict[str, Any], *,
                    actor: str, may_manage: Optional[Callable[[str], None]] = None
                    ) -> dict[str, Any]:
    """The connection's row IN ``project_id`` (bindings keep a row in their own project), made
    the first time this project imports through a connection homed elsewhere: a ``used``
    mirror that carries NO secret — reads and write-back resolve the credential from the home.
    Allowed when the home is shared with the whole org, or the caller manages integrations
    here (``may_manage`` raises otherwise). Refused when this project has its own, different
    connection to that system."""
    found = intake._project_row(db, project_id, row)
    if found is not None and found.get("status") == "active" and (
            intake.is_connected(found) or found["configuration"].get("mirror_of")):
        return found
    home = intake.get_row(db, home_id(row))
    if home["project_id"] == project_id:
        return home
    if org_of(db, home["project_id"]) != org_of(db, project_id):
        raise intake.TicketApiError("not_found", "No such ticket-system connection.", status=404)
    if not is_shared(home):
        if may_manage is None:
            raise not_shared_error(db, home)
        may_manage(project_id)
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
    adapter = intake.adapter_for(provider)
    keeps = set(_MIRROR_KEEPS) | {str(f["name"]) for f in adapter.manifest.auth_fields
                                  if not f.get("secret")}
    mirrored = {k: v for k, v in home["configuration"].items() if k in keeps}
    mirrored["mirror_reason"] = USED_MIRROR
    return intake.store(db, app_settings, project_id=project_id, adapter=adapter,
                        configuration=mirrored, token="",
                        outcome={"account": home["configuration"].get("acts_as")}, actor=actor,
                        mirror_of=str(home["id"]))
