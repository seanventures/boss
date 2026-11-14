"""Ticket systems (BOSSAPP-1047, Phase B1): connections, browse and read, import and start.

The API design is TICKET_API.md §1–§3; the domain logic is ``services/ticket_intake`` over the
Phase A connector contract, and these routes stay thin.

WHO MAY DO WHAT
  integration:read     list connections, browse projects/epics/tickets, resolve references,
                       read the pick-up inbox
  integration:manage   connect (test first, the token to the vault), re-test, change settings,
                       disconnect — and, for a mapping onto another BOSS project, the same
                       permission AT that project (checked per mapping)
  work_tracker:write   (delegable) import tickets as work items and start work from one; the
                       start also needs workflow_run:start, checked by the handler

A CONNECTION IS A PROJECT'S ROW (``work_tracker_providers.id``): a mapping onto another BOSS
project fans out to a mirror row there (see ticket_intake), so every id here is confined to its
project like any other object. Errors are typed: ``{"detail": {"error", "message"}}`` with
``auth_failed`` (424), ``not_found`` (404), ``rate_limited`` (429), ``host_not_allowed`` /
``capability_not_supported`` / ``invalid_ref`` / ``invalid_settings`` (422), ``conflict`` (409),
``upstream_error`` (502).
"""
from __future__ import annotations

from typing import Any, Optional

from fastapi import APIRouter, Body, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field

from boss_engine.connectors.tickets import STATUS_CATEGORIES, TICKET_TYPES, TicketRef
from boss_engine.identity import P, Scope, permit
from boss_engine.services import ticket_intake as intake

from ..db import begin, get_engine

router = APIRouter(tags=["ticket-systems"])


# ---- bodies ---------------------------------------------------------------------------------

class TicketConnectBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    project_id: str = Field(min_length=1, max_length=200)
    provider: str = Field(min_length=1, max_length=40)
    base_url: str = Field(default="", max_length=500)
    fields: dict[str, Any] = Field(default_factory=dict, description="The connector's non-secret "
                                   "auth fields (catalog `auth_fields`), e.g. email, project_key.")
    secrets: dict[str, str] = Field(default_factory=dict, description="Write-only: the token. "
                                    "Tested first, then stored in the secret backend; never "
                                    "returned (the response carries last4 and a fingerprint).")
    settings: Optional[dict[str, Any]] = Field(default=None, description="Optional initial "
                                               "settings, as PATCH takes them.")
    actor: Optional[str] = None


class TicketActorBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    actor: Optional[str] = None


class TicketImportItem(BaseModel):
    model_config = ConfigDict(extra="forbid")
    key: str = Field(min_length=1, max_length=300, description="A key (KAN-12) or the ticket's URL.")
    with_children: bool = False


class TicketImportBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    connection_id: str = Field(min_length=1, max_length=200)
    items: list[TicketImportItem] = Field(min_length=1, max_length=50)
    actor: Optional[str] = None


class TicketStartBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    project_id: Optional[str] = Field(default=None, max_length=200)
    workflow_key: Optional[str] = Field(default=None, max_length=120, description="Default: from "
                                        "the ticket type — epic → major_feature, story/task → "
                                        "minor_feature, bug → fast_fix.")
    start_anyway: Optional[list[str]] = None
    accept: Optional[list[str]] = None
    dedupe_key: Optional[str] = Field(default=None, max_length=200)
    actor: Optional[str] = None


# ---- helpers --------------------------------------------------------------------------------

def _settings():
    from boss_engine.config import get_settings

    return get_settings()


def _engine():
    return get_engine()


def _refuse(error: BaseException, row: Any = None) -> HTTPException:
    typed = intake.explain(error, label=intake.label_of(row) if row else "the ticket system")
    return HTTPException(status_code=typed.status, detail=typed.body())


def _person() -> Optional[str]:
    """The signed-in person (usr_...), whose own ticket-system sign-in a call may carry."""
    from .. import identity_guard

    ctx = identity_guard.REQUEST_IDENTITY.get()
    principal = getattr(ctx, "principal", None)
    kind = getattr(getattr(principal, "kind", None), "value", None)
    return str(principal.id) if principal is not None and kind == "user" else None


def _reader(connection, project_id: str) -> None:
    from apps.api.app.main import require_project_reader

    require_project_reader(connection, project_id)


def _manager(connection, project_id: str, actor: Optional[str]) -> str:
    from apps.api.app.main import require_project_write_actor

    return require_project_write_actor(connection, project_id, actor, "integration_manage")


def _writer(connection, project_id: str, actor: Optional[str]) -> str:
    from apps.api.app.main import require_project_write_actor

    return require_project_write_actor(connection, project_id, actor, "task_create")


def _also(permission: str, project_id: str, label: str) -> None:
    from .identity_access import require_permission

    require_permission(None, permission, project_id, label=label)


def _row(connection, connection_id: str) -> dict:
    try:
        return intake.get_row(connection, connection_id)
    except intake.TicketApiError as error:
        raise _refuse(error) from None


def _call(row: dict, fn):
    try:
        return fn()
    except HTTPException:
        raise
    except Exception as error:  # noqa: BLE001 — every connector failure is a typed refusal
        raise _refuse(error, row) from None


# ---- §1 connections -------------------------------------------------------------------------

@router.get("/v1/ticket-systems/catalog")
@permit(P.INTEGRATION_READ, scope=Scope.org())
def ticket_system_catalog() -> dict:
    """The connectors BOSS has: label, icon, auth fields, capabilities."""
    return {"items": intake.catalog()}


@router.get("/v1/ticket-systems")
@permit(P.INTEGRATION_READ, scope=Scope.project(query="project_id"))
def list_ticket_systems(project_id: str) -> dict:
    """The project's connections, each with its status, mappings and settings."""
    with begin(_engine()) as connection:
        _reader(connection, project_id)
        return {"project_id": project_id,
                "connections": intake.list_connections(connection, project_id)}


@router.post("/v1/ticket-systems", status_code=201)
@permit(P.INTEGRATION_MANAGE, scope=Scope.project(body="project_id"))
def connect_ticket_system(payload: TicketConnectBody) -> dict:
    """Connect a ticket system: TEST FIRST (nothing is stored when the test fails), then the
    token goes to the secret backend and the row keeps its reference. Reconnecting without a
    token keeps the stored one."""
    settings = _settings()
    try:
        adapter, config, token = intake.prepare(payload.provider, base_url=payload.base_url,
                                                fields=payload.fields, secrets=payload.secrets)
    except intake.TicketApiError as error:
        raise _refuse(error) from None
    with begin(_engine()) as connection:
        actor = _manager(connection, payload.project_id, payload.actor)
        if not token:
            existing = [r for r in intake.project_rows(connection, payload.project_id,
                                                       include_disconnected=True)
                        if r["provider_key"] == adapter.provider_key
                        and r["configuration"].get("secret_ref_id")]
            if existing:
                token = intake.stored_token(connection, settings, existing[0])
    if not token:
        raise _refuse(intake.TicketApiError("invalid_request", f"{adapter.manifest.label} needs "
                                                               "its token to connect."))
    try:
        outcome = intake.run_test(adapter, {**config, "api_token": token})
    except intake.TicketApiError as error:
        raise _refuse(error) from None
    with begin(_engine()) as connection:
        row = intake.store(connection, settings, project_id=payload.project_id, adapter=adapter,
                           configuration=config, token=token, outcome=outcome, actor=actor)
        if payload.settings:
            try:
                merged = intake.merge_settings(row["configuration"], payload.settings)
            except intake.TicketApiError as error:
                raise _refuse(error) from None
            intake.update_settings_row(connection, row["id"], merged)
            row = intake.get_row(connection, row["id"])
            intake.sync_mirrors(connection, settings, row, actor=actor,
                                may_manage=lambda p: _also("integration:manage", p,
                                                           "ticket_mapping"))
    keys, keys_error = _refresh_keys(row["id"])
    with begin(_engine()) as connection:
        out = intake.serialize(connection, intake.get_row(connection, row["id"]))
    return {"connection": out, "test": _test_view(outcome, adapter),
            **({"project_keys_error": keys_error} if keys_error else {})}


def _refresh_keys(connection_id: str) -> tuple[list[str], Optional[dict]]:
    """Plane bare keys need the system's project keys: filled from list_projects on connect and
    test. Best effort — a connection that cannot list projects is still connected."""
    try:
        with begin(_engine()) as connection:
            row = intake.get_row(connection, connection_id)
            return intake.refresh_project_keys(connection, row, person_id=_person()), None
    except Exception as error:  # noqa: BLE001
        return [], intake.explain(error).body()


def _test_view(outcome: dict, adapter: Any) -> dict:
    return {"ok": bool(outcome.get("ok")), "account": outcome.get("account"),
            "message": outcome.get("message"),
            "capabilities": dict(adapter.ticket_capabilities)}


@router.post("/v1/ticket-systems/{connection_id}/test")
@permit(P.INTEGRATION_MANAGE, scope=Scope.of("work_tracker_provider", path="connection_id"))
def test_ticket_system(connection_id: str, payload: Optional[TicketActorBody] = None) -> dict:
    """Re-test: who BOSS is there, and what it can do. Refreshes the known project keys."""
    with begin(_engine()) as connection:
        row = _row(connection, connection_id)
        actor = _manager(connection, row["project_id"], (payload or TicketActorBody()).actor)
        adapter = intake.adapter_for(str(row["provider_key"]))
        conn = _call(row, lambda: intake.ticket_connection(connection, row, person_id=_person()))
        config = _call(row, conn.resolved_configuration)
    try:
        outcome = intake.run_test(adapter, config)
    except intake.TicketApiError as error:
        raise _refuse(error, row) from None
    del config  # the token was for that one call
    keys, keys_error = _refresh_keys(connection_id)
    with begin(_engine()) as connection:
        from sqlalchemy import text

        if outcome.get("account"):
            connection.execute(text(
                "UPDATE work_tracker_providers SET configuration = configuration || "
                "jsonb_build_object('acts_as', CAST(:a AS TEXT)), updated_at = now() "
                "WHERE id = :i"), {"a": str(outcome["account"]), "i": connection_id})
        from boss_engine.services.core import emit_event

        emit_event(connection, row["project_id"], "ticket_system.tested", actor,
                   "work_tracker_provider", connection_id, {"ok": True})
        out = intake.serialize(connection, intake.get_row(connection, connection_id))
    return {"connection": out, "test": {**_test_view(outcome, adapter), "project_keys": keys},
            **({"project_keys_error": keys_error} if keys_error else {})}


@router.patch("/v1/ticket-systems/{connection_id}")
@permit(P.INTEGRATION_MANAGE, scope=Scope.of("work_tracker_provider", path="connection_id"))
def update_ticket_system(connection_id: str,
                         payload: dict[str, Any] = Body(..., description=(
                             "Any of: mappings [{external_project, boss_project_id, default}], "
                             "write_back {comment, transition, link_pr}, status_map {todo, "
                             "doing, review, done}, pickup {label, mode: ask|auto_start}, "
                             "inbound {mode: webhook|poll|off, poll_minutes}. Merged with what "
                             "is stored."))) -> dict:
    """Change settings. Only what is named changes. A mapping onto another BOSS project needs
    integration:manage there too, and gives that project a mirror of this connection."""
    body = dict(payload or {})
    actor_claim = body.pop("actor", None)
    settings = _settings()
    with begin(_engine()) as connection:
        row = _row(connection, connection_id)
        actor = _manager(connection, row["project_id"], actor_claim)
        home_id = row["configuration"].get("mirror_of")
        if home_id:
            raise _refuse(intake.TicketApiError(
                "conflict", "This connection is managed from the project it was connected in. "
                            "Change its settings there.", status=409,
                managed_from=str(home_id)), row)
        try:
            merged = intake.merge_settings(row["configuration"], body)
            for mapping in merged.get("mappings") or []:
                if mapping["boss_project_id"] != row["project_id"]:
                    _also("integration:manage", mapping["boss_project_id"], "ticket_mapping")
            intake.update_settings_row(connection, connection_id, merged)
            row = intake.get_row(connection, connection_id)
            mirrors = intake.sync_mirrors(
                connection, settings, row, actor=actor,
                may_manage=lambda p: _also("integration:manage", p, "ticket_mapping"))
        except intake.TicketApiError as error:
            raise _refuse(error, row) from None
        from boss_engine.services.core import emit_event

        emit_event(connection, row["project_id"], "ticket_system.settings_changed", actor,
                   "work_tracker_provider", connection_id, {"changed": sorted(body)})
        return {"connection": intake.serialize(connection, intake.get_row(connection,
                                                                          connection_id)),
                "mirrors": mirrors}


@router.delete("/v1/ticket-systems/{connection_id}")
@permit(P.INTEGRATION_MANAGE, scope=Scope.of("work_tracker_provider", path="connection_id"))
def disconnect_ticket_system(connection_id: str, actor: Optional[str] = None) -> dict:
    """Disconnect. Work items keep their bindings, with sync paused."""
    with begin(_engine()) as connection:
        row = _row(connection, connection_id)
        who = _manager(connection, row["project_id"], actor)
        done = intake.disconnect(connection, row, who)
        return {"connection": intake.serialize(connection, intake.get_row(connection,
                                                                          connection_id)),
                **done}


# ---- §2 browse and read ---------------------------------------------------------------------

def _browse(connection_id: str, fn):
    """Run one read against the connection (inside a short transaction: the credential resolves
    server-side) and mark the tickets BOSS already has."""
    with begin(_engine()) as connection:
        row = _row(connection, connection_id)
        _reader(connection, row["project_id"])
        adapter = intake.adapter_for(str(row["provider_key"]))
        conn = _call(row, lambda: intake.ticket_connection(connection, row, person_id=_person()))
        found = _call(row, lambda: fn(adapter, conn))
        marks = intake.in_boss_map(connection, intake.family_ids(connection, row))
        return row, found, marks


@router.get("/v1/ticket-systems/{connection_id}/projects")
@permit(P.INTEGRATION_READ, scope=Scope.of("work_tracker_provider", path="connection_id"))
def ticket_system_projects(connection_id: str, q: Optional[str] = None) -> dict:
    row, found, _ = _browse(connection_id, lambda a, c: a.list_projects(c, q or None))
    return {"connection_id": connection_id, "projects": [
        {"key": p.key, "name": p.name, "id": p.id, "url": p.url, "open_count": p.open_count,
         "epic_count": p.epic_count} for p in found]}


@router.get("/v1/ticket-systems/{connection_id}/projects/{project_key}/epics")
@permit(P.INTEGRATION_READ, scope=Scope.of("work_tracker_provider", path="connection_id"))
def ticket_system_epics(connection_id: str, project_key: str) -> dict:
    row, found, marks = _browse(connection_id, lambda a, c: a.list_epics(c, project_key))
    return {"connection_id": connection_id, "project": project_key,
            "tickets": intake.mark(found, marks)}


@router.get("/v1/ticket-systems/{connection_id}/projects/{project_key}/unparented")
@permit(P.INTEGRATION_READ, scope=Scope.of("work_tracker_provider", path="connection_id"))
def ticket_system_unparented(connection_id: str, project_key: str) -> dict:
    row, found, marks = _browse(connection_id, lambda a, c: a.list_unparented(c, project_key))
    return {"connection_id": connection_id, "project": project_key,
            "tickets": intake.mark(found, marks)}


@router.get("/v1/ticket-systems/{connection_id}/tickets/{key}")
@permit(P.INTEGRATION_READ, scope=Scope.of("work_tracker_provider", path="connection_id"))
def ticket_system_ticket(connection_id: str, key: str) -> dict:
    row, found, marks = _browse(connection_id, lambda a, c: [a.get_ticket(c, key)])
    return {"connection_id": connection_id, "ticket": intake.mark(found, marks, full=True)[0]}


@router.get("/v1/ticket-systems/{connection_id}/tickets/{key}/children")
@permit(P.INTEGRATION_READ, scope=Scope.of("work_tracker_provider", path="connection_id"))
def ticket_system_children(connection_id: str, key: str) -> dict:
    row, found, marks = _browse(connection_id, lambda a, c: a.list_children(c, key))
    return {"connection_id": connection_id, "parent": key, "tickets": intake.mark(found, marks)}


@router.get("/v1/ticket-systems/{connection_id}/search")
@permit(P.INTEGRATION_READ, scope=Scope.of("work_tracker_provider", path="connection_id"))
def ticket_system_search(connection_id: str, q: str = "", project: Optional[str] = None,
                         status: Optional[str] = None, type: Optional[str] = None,
                         assignee: Optional[str] = None,
                         limit: int = Query(50, ge=1, le=200)) -> dict:
    """Search. ``status`` is a category (todo, doing, review, done) or ``open``; ``type`` one of
    bug, story, task, epic. Only these filters reach the connector (never raw query text)."""
    filters: dict[str, Any] = {}
    if status:
        if status == "open":
            filters["open_only"] = True
        elif status in STATUS_CATEGORIES:
            filters["status_category"] = status
        else:
            raise _refuse(intake.TicketApiError("invalid_request", "status is todo, doing, "
                                                                   "review, done or open."))
    if type:
        if type not in TICKET_TYPES or type == "other":
            raise _refuse(intake.TicketApiError("invalid_request", "type is bug, story, task or "
                                                                   "epic."))
        filters["type"] = type
    if assignee:
        filters["assignee"] = assignee[:200]
    row, found, marks = _browse(connection_id, lambda a, c: a.search(
        c, q[:500], project or None, filters, limit=limit))
    return {"connection_id": connection_id, "tickets": intake.mark(found, marks)}


@router.get("/v1/tickets/resolve")
@permit(P.INTEGRATION_READ, scope=Scope.project(query="project_id"))
def resolve_tickets(project_id: str, text: str = Query(..., max_length=20_000)) -> dict:
    """Ticket keys and links in any text, across the project's connections → previews. A key is
    recognised only for a project a connection knows (``UTF-8`` is never a ticket)."""
    with begin(_engine()) as connection:
        _reader(connection, project_id)
        try:
            return {"project_id": project_id,
                    **intake.resolve_text(connection, project_id, text, person_id=_person())}
        except intake.TicketApiError as error:
            raise _refuse(error) from None


# ---- §3 import and start --------------------------------------------------------------------

@router.post("/v1/projects/{project_id}/tickets/import")
@permit(P.WORK_TRACKER_WRITE, scope=Scope.project(path="project_id"))
def import_tickets(project_id: str, payload: TicketImportBody) -> dict:
    """Tickets → work items + bindings, status Ready (``todo``). An epic ``with_children`` brings
    its children as items under it. A ticket already in BOSS is not imported again."""
    settings = _settings()
    with begin(_engine()) as connection:
        actor = _writer(connection, project_id, payload.actor)
        try:
            row = intake.get_row(connection, payload.connection_id, project_id=project_id)
        except intake.TicketApiError as error:
            raise _refuse(error) from None
        items = _call(row, lambda: intake.read_for_import(connection, row, payload.items,
                                                          person_id=_person()))
    with begin(_engine()) as connection:
        done = intake.import_tickets(connection, settings, project_id=project_id, row=row,
                                     items=items, actor=actor)
    return {"project_id": project_id, "connection_id": payload.connection_id, **done}


@router.get("/v1/projects/{project_id}/tickets/waiting")
@permit(P.INTEGRATION_READ, scope=Scope.project(path="project_id"))
def waiting_tickets(project_id: str, limit: int = Query(50, ge=1, le=200)) -> dict:
    """The pick-up inbox: open tickets with the connection's pickup label, not yet in BOSS."""
    with begin(_engine()) as connection:
        _reader(connection, project_id)
        return intake.waiting(connection, project_id, person_id=_person(), limit=limit)


@router.post("/v1/work-items/{work_item_id}/start-from-ticket")
@permit(P.WORK_TRACKER_WRITE, scope=Scope.of("work_item", path="work_item_id"))
def start_from_ticket(work_item_id: str, payload: Optional[TicketStartBody] = None) -> dict:
    """Start the work an imported ticket became, on the workflow its type calls for (or the one
    named). The ticket is read fresh from its system, so the run starts from what it says NOW,
    and it travels as ``input.ticket``. Also needs workflow_run:start.

    (Not ``POST /v1/work-items/{id}/start``: that route already exists — a policy-aware start by
    workflow version, permission work_item:manage — and keeps its contract.)"""
    from sqlalchemy import text

    from apps.api.app.main import start_delivery_workflow
    from apps.api.app.request_bodies import WorkflowStartBody
    body = payload or TicketStartBody()
    with begin(_engine()) as connection:
        item = connection.execute(text(
            "SELECT id, project_id, title FROM work_items WHERE id = :i"),
            {"i": work_item_id}).mappings().first()
        if item is None or (body.project_id and item["project_id"] != body.project_id):
            raise HTTPException(status_code=404, detail={"error": "not_found",
                                                         "message": "No such work item."})
        project_id = str(item["project_id"])
        actor = _writer(connection, project_id, body.actor)
        _also("workflow_run:start", project_id, "ticket_start")
        binding = intake.binding_of_item(connection, project_id, work_item_id)
        if binding is None:
            raise HTTPException(status_code=409, detail={
                "error": "conflict", "message": "This work item did not come from a ticket."})
        row = _row(connection, str(binding["provider_id"]))
        ref = TicketRef(str(row["provider_key"]), key=binding.get("external_key"),
                        id=binding.get("external_issue_id"), url=binding.get("external_url"))
        try:
            ticket = intake.fetch(connection, row, ref, person_id=_person())
        except intake.TicketApiError as error:
            raise _refuse(error, row) from None
        try:
            plan = intake.start_plan(connection, row, ticket, project_id=project_id,
                                     work_item_id=work_item_id, workflow_key=body.workflow_key)
        except intake.TicketApiError as error:
            raise _refuse(error, row) from None
        info, wanted, note = plan["ticket"], plan["workflow_key"], plan["note"]
    run_input = dict(plan["input"])
    start: dict[str, Any] = {"project_id": project_id, "workflow_key": wanted,
                             "input": run_input, "actor": actor}
    for name in ("start_anyway", "accept", "dedupe_key"):
        if getattr(body, name):
            start[name] = getattr(body, name)
    started = start_delivery_workflow(WorkflowStartBody(**start), ticket=info)
    return {"work_item_id": work_item_id, "workflow_key": wanted, "ticket": {
        k: info[k] for k in ("provider", "connection_id", "key", "url", "title", "type",
                             "untrusted")},
        **({"note": note} if note else {}), **started}
