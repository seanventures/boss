"""Ticket systems after a run starts (BOSSAPP-1047, Phase B2): write-back, mid-run change
detection, inbound pick-up (webhook and poller) and the import door the agent tools use.

WRITE-BACK. A run started from a ticket carries ``input.ticket = {provider, connection_id, key,
url, ...}`` (B1 puts it there; a code task inherits it through its workflow run, or carries it in
``finalize_ctx.ticket``). As the run moves, the ticket follows it, through the connector contract
(``connectors/tickets``) and never a provider client:

    run started        -> transition ``doing`` + a comment
    pr_opened          -> link the PR + transition ``review``
    decision pending   -> a comment
    merged             -> transition ``done``
    deployed           -> a comment with the preview URL
    failed / blocked   -> a comment

The connection's ``settings.write_back`` switches each kind of write off, and ``status_map``
(category -> status name) says where a transition lands. Every effect is a RECEIPT whose dedupe
key is the ticket, the run, the action and the effect, so replaying an event (a redelivery, the
catch-up sweep, a retry) never comments twice. A failed effect never fails the run: the receipt
is ``failed``, the binding's ``sync_status`` says ``failed`` and ``POST
/v1/work-items/{id}/ticket-sync/retry`` sends it again.

This generalises ``plane_sync`` and does not replace it: a run ``plane_sync`` already mirrors (a
Plane-webhook run, ``finalize_ctx.source == "plane"``) is left to it, so Plane is never written
twice.

WHERE IT HOOKS. ``core.emit_event`` calls :func:`notify` for the events above. The events are
emitted inside their writer's transaction, so the effect runs AFTER that transaction commits:
a small worker waits until the event row is visible (a rolled-back event never is) and then
syncs. The scheduler's tick sweeps the last hours' ticket-run events as a catch-up for a process
that stopped between commit and sync; the receipts make the replay harmless.

MID-RUN CHANGES. At build start (``code_task.started``) the ticket is read again. If it changed
since the run started and its acceptance criteria differ, a decision card asks the person:
"Include it" (the run's ticket snapshot takes the new criteria) or "Next ticket" (this build
finishes as asked; the change is recorded for the next one).

INBOUND. ``POST /v1/webhooks/tickets/{connection_id}`` is verified by the connector's own
``verify_webhook``; a ticket that carries the connection's ``pickup.label`` and is not in BOSS
yet is either offered (``ask``: a decision card, "Start it" / "Not now") or imported and started
(``auto_start``). ``inbound.mode = poll`` does the same from a search every ``poll_minutes``, on
the extension scheduler's DB lease table (``extension_job_leases``), so exactly one process fires.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import queue
import socket
import threading
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Mapping, Optional

from sqlalchemy import text

logger = logging.getLogger(__name__)

PREFIX = "ticket-sync"

#: event -> the write-back action it means.
EVENT_ACTIONS: dict[str, str] = {
    "workflow.run_started": "started",
    # A code task outside any workflow run starts its own ticket's work; inside one, the
    # workflow's run_started already said so (see on_event).
    "code_task.started": "started",
    "code_task.pr_opened": "pr_opened",
    "workflow.step_waiting_decision": "decision_pending",
    "software_factory.pull_request_merged": "merged",
    "deployment.stack_healthy": "deployed",
    "deployment.rollout_completed": "deployed",
    "code_task.failed": "failed",
    "workflow.step_deadlettered": "failed",
    "workflow.run_blocked": "blocked",
}
#: Events that also re-read the ticket for a mid-run change (build start).
CHANGE_CHECK_EVENTS = frozenset({"code_task.started"})
HOOKED_EVENTS = frozenset(EVENT_ACTIONS) | CHANGE_CHECK_EVENTS

#: action -> the effects it writes, in order: (operation, target category for a transition).
ACTION_EFFECTS: dict[str, tuple[tuple[str, Optional[str]], ...]] = {
    "started": (("transition", "doing"), ("comment", None)),
    "pr_opened": (("link", None), ("transition", "review")),
    "decision_pending": (("comment", None),),
    "merged": (("transition", "done"),),
    "deployed": (("comment", None),),
    "failed": (("comment", None),),
    "blocked": (("comment", None),),
    # Answers to the mid-run change card (apply_answers).
    "change_included": (("comment", None),),
    "change_deferred": (("comment", None),),
}
#: operation -> the write_back switch that turns it off, and the capability it needs.
OP_SWITCH = {"comment": "comment", "transition": "transition", "link": "link_pr"}
OP_CAPABILITY = {"comment": "write_comment", "transition": "transition", "link": "link"}

WRITE_BACK_DEFAULTS = {"comment": True, "transition": True, "link_pr": True}
PICKUP_MODES = ("ask", "auto_start")
INBOUND_MODES = ("webhook", "poll", "off")

#: Decision actions this module raises (decision_actions.RECORDED_ON_APPROVAL): the answer is
#: read back by apply_answers on the scheduler's tick.
PICKUP_ACTION = "ticket_pickup"
CHANGED_ACTION = "ticket_changed_mid_run"

#: The extension scheduler's lease table, under BOSS's own name.
LEASE_EXTENSION = "boss.tickets"
HOLDER = f"{socket.gethostname()}:{os.getpid()}:{uuid.uuid4().hex[:8]}"
TICK_SECONDS = 30.0
LEASE_TTL = timedelta(minutes=5)
ANSWERS_EVERY = timedelta(seconds=30)
SWEEP_EVERY = timedelta(minutes=2)
SWEEP_WINDOW = timedelta(hours=6)


class TicketSyncError(ValueError):
    """A request the ticket door refuses, in words a person can act on."""

    def __init__(self, message: str, *, status: int = 422, code: str = "invalid"):
        super().__init__(message)
        self.status = status
        self.code = code


# ---------------------------------------------------------------------------------------------
# small helpers
# ---------------------------------------------------------------------------------------------


def _json(value: Any, default: Any) -> Any:
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError:
            return default
    return default if value is None else value


def _short(value: Any, limit: int = 300) -> str:
    out = " ".join(str(value or "").split())
    return out if len(out) <= limit else out[: limit - 1].rstrip() + "…"


def _iso(value: Any) -> Any:
    return value.isoformat() if hasattr(value, "isoformat") else value


def _digest(*parts: Any) -> str:
    raw = json.dumps(parts, sort_keys=True, default=str, ensure_ascii=False)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:20]


def _person(actor: Any) -> Optional[str]:
    actor = str(actor or "").strip()
    return actor if actor.startswith("usr_") else None


def _plain_error(error: BaseException) -> str:
    """A failure in words, never a credential (the connector's messages carry none)."""
    from ..connectors.tickets import AuthFailed, CapabilityNotSupported, NotFound, RateLimited

    if isinstance(error, AuthFailed):
        if error.reason == "needs_person_sign_in":
            return ("the ticket system needs the person this run acts for to sign in with their "
                    "own account")
        return "the ticket system refused BOSS's credential"
    if isinstance(error, NotFound):
        return "the ticket was not found (deleted, moved, or not visible to BOSS)"
    if isinstance(error, RateLimited):
        return "the ticket system is rate-limiting BOSS; retry in a minute"
    if isinstance(error, CapabilityNotSupported):
        return str(error)
    from ..connectors.tickets import TicketSystemError

    if isinstance(error, TicketSystemError):
        return _short(str(error), 300)
    return f"the call failed ({type(error).__name__})"


# ---------------------------------------------------------------------------------------------
# settings: the API's shape (write_back, status_map, pickup, inbound), read from what B1 stores
# (ticket_intake.public_settings over settings.write_back / status_mapping / pickup_label /
# auto_start / inbound_mode / poll_interval_s)
# ---------------------------------------------------------------------------------------------


def sync_settings(configuration: Optional[Mapping[str, Any]]) -> dict[str, Any]:
    """The write-back and pick-up settings of one stored configuration, read through B1's
    ``ticket_intake.public_settings`` (the API's shape, drawn from the contract's stored keys):
    ``write_back``, ``status_map``, ``pickup`` and ``inbound``. One reader, so what the
    Settings page saves is exactly what the write-back, the webhook and the poller obey."""
    from .ticket_intake import public_settings

    shown = public_settings(dict(configuration or {}))
    return {"write_back": dict(shown["write_back"]), "status_map": dict(shown["status_map"]),
            "pickup": dict(shown["pickup"]), "inbound": dict(shown["inbound"])}


def _apply_status_map(conn: Any) -> None:
    """The adapters read ``settings.status_mapping``; ``status_map`` is the API's name for it."""
    effective = sync_settings(conn.configuration)["status_map"]
    saved = conn.configuration.get("settings")
    saved = dict(saved) if isinstance(saved, dict) else {}
    saved["status_mapping"] = effective
    conn.configuration["settings"] = saved


# ---------------------------------------------------------------------------------------------
# connections
# ---------------------------------------------------------------------------------------------


def provider_row(db: Any, row_id: str) -> Optional[dict[str, Any]]:
    row = db.execute(text(
        "SELECT id, project_id, provider_key, display_name, status, configuration, created_by "
        "FROM work_tracker_providers WHERE id = :id"), {"id": str(row_id or "")}).mappings().first()
    if row is None:
        return None
    out = dict(row)
    out["configuration"] = dict(_json(out.get("configuration"), {}) or {})
    return out


def home_row(db: Any, row: Optional[Mapping[str, Any]]) -> Optional[dict[str, Any]]:
    """The HOME connection of a row. B1 maps one ticket-system connection onto many BOSS
    projects by MIRROR rows (``configuration.mirror_of = <home row id>``); settings are edited
    on the home row and its secrets (the token, the webhook secret) are the home row's. So
    write-back, the webhook and the poller read settings and secrets from the home, and a
    mirror never acts as a second connection to the same site. A home that is gone or
    disconnected leaves the row as it is."""
    if row is None:
        return None
    home_id = str((row.get("configuration") or {}).get("mirror_of") or "")
    if not home_id or home_id == str(row["id"]):
        return dict(row)
    home = provider_row(db, home_id)
    return home if home is not None and home["status"] == "active" else dict(row)


def is_mirror(row: Mapping[str, Any]) -> bool:
    return bool((row.get("configuration") or {}).get("mirror_of"))


def connection_from_row(db: Any, row: Mapping[str, Any], *, person_id: Optional[str] = None,
                        app_settings: Any = None) -> Any:
    from ..connectors.tickets import TicketConnection

    return TicketConnection(str(row["provider_key"]), dict(row["configuration"] or {}),
                            project_id=str(row["project_id"]), db=db, app_settings=app_settings,
                            person_id=_person(person_id))


def open_connection(db: Any, project_id: str, *, connection_id: Optional[str] = None,
                    provider: Optional[str] = None, person_id: Optional[str] = None,
                    app_settings: Any = None) -> Any:
    """The connection a ticket names: a work_tracker_providers row id (B1's connection id), a
    Kit id (``kit:tickets:<provider>``) or just the provider. None when it is not connected."""
    from ..connectors.tickets import connection_for

    cid = str(connection_id or "").strip()
    wanted = str(provider or "").strip().lower()
    if cid.startswith("kit:tickets:"):
        wanted = wanted or cid.rsplit(":", 1)[-1]
    elif cid:
        row = provider_row(db, cid)
        if row is not None and str(row["project_id"]) == str(project_id) \
                and row["status"] == "active":
            # A mirror's settings and token are its home's (home_row).
            return connection_from_row(db, home_row(db, row), person_id=person_id,
                                       app_settings=app_settings)
        if not wanted:
            return None
    if not wanted:
        return None
    own = provider_row(db, _provider_row_id(db, project_id, wanted) or "")
    if own is not None and own["status"] == "active" and is_mirror(own):
        return connection_from_row(db, home_row(db, own), person_id=person_id,
                                   app_settings=app_settings)
    return connection_for(db, project_id, wanted, person_id=_person(person_id),
                          app_settings=app_settings)


def _provider_row_id(db: Any, project_id: str, provider: str) -> Optional[str]:
    return db.execute(text(
        "SELECT id FROM work_tracker_providers WHERE project_id = :p AND provider_key = :k"),
        {"p": project_id, "k": provider}).scalar()


# ---------------------------------------------------------------------------------------------
# who and what a run's event is about
# ---------------------------------------------------------------------------------------------


@dataclass
class Subject:
    project_id: str
    entity_type: str
    entity_id: str
    root_run_id: str
    ticket: dict[str, Any]
    actor: Optional[str] = None
    work_item_id: Optional[str] = None
    title: str = ""
    pull_request_url: Optional[str] = None
    plane_handled: bool = False
    workflow_run_id: Optional[str] = None
    code_task_run_id: Optional[str] = None
    extra: dict[str, Any] = field(default_factory=dict)

    @property
    def provider(self) -> str:
        return str(self.ticket.get("provider") or "").strip().lower()

    @property
    def key(self) -> str:
        return str(self.ticket.get("key") or self.ticket.get("id") or "")


def _ticket_of(value: Any) -> Optional[dict[str, Any]]:
    ticket = _json(value, None)
    if isinstance(ticket, dict) and (ticket.get("key") or ticket.get("id")) \
            and ticket.get("provider"):
        return dict(ticket)
    return None


def _ticket_of_bound_run(db: Any, project_id: str, run_id: str, root_run_id: str
                         ) -> Optional[dict[str, Any]]:
    """The ticket a run is bound to by B1's convention: the binding whose ``metadata.run_ids``
    names the run (or its root). For a run whose input carries no ticket of its own."""
    row = db.execute(text(
        "SELECT b.provider_id, b.work_item_id, b.external_key, b.external_issue_id, "
        "b.external_url, p.provider_key FROM work_tracker_bindings b "
        "JOIN work_tracker_providers p ON p.id = b.provider_id "
        "WHERE b.project_id = :p AND (COALESCE(b.metadata->'run_ids', '[]'::jsonb) ? :r "
        "OR COALESCE(b.metadata->'run_ids', '[]'::jsonb) ? :root) "
        "ORDER BY b.updated_at DESC LIMIT 1"),
        {"p": project_id, "r": run_id, "root": root_run_id}).mappings().first()
    if row is None or not (row["external_key"] or row["external_issue_id"]):
        return None
    return {"provider": row["provider_key"], "connection_id": row["provider_id"],
            "key": row["external_key"], "id": row["external_issue_id"],
            "url": row["external_url"], "work_item_id": row["work_item_id"]}


def _workflow_subject(db: Any, run_id: str) -> Optional[Subject]:
    row = db.execute(text(
        "SELECT id, project_id, actor, input, root_run_id, work_item_id FROM workflow_runs "
        "WHERE id = :id"), {"id": run_id}).mappings().first()
    if row is None:
        return None
    data = _json(row["input"], {}) or {}
    ticket = _ticket_of(data.get("ticket"))
    root = str(row["root_run_id"] or row["id"])
    if ticket is None and root != row["id"]:
        parent = db.execute(text("SELECT input FROM workflow_runs WHERE id = :id"),
                            {"id": root}).scalar()
        ticket = _ticket_of((_json(parent, {}) or {}).get("ticket"))
    if ticket is None:
        ticket = _ticket_of_bound_run(db, str(row["project_id"]), str(row["id"]), root)
    if ticket is None:
        return None
    return Subject(project_id=str(row["project_id"]), entity_type="workflow_run",
                   entity_id=str(row["id"]), root_run_id=root, ticket=ticket,
                   actor=str(row["actor"] or "") or None,
                   work_item_id=row["work_item_id"] or data.get("work_item_id")
                   or ticket.get("work_item_id"),
                   title=str(data.get("title") or ticket.get("title") or ""),
                   workflow_run_id=str(row["id"]))


def _code_task_subject(db: Any, run_id: str) -> Optional[Subject]:
    row = db.execute(text(
        "SELECT id, project_id, title, finalize_ctx, pull_request_url, work_item_id "
        "FROM code_task_runs WHERE id = :id"), {"id": run_id}).mappings().first()
    if row is None:
        return None
    ctx = _json(row["finalize_ctx"], {}) or {}
    workflow_run = str(ctx.get("workflow_run_id")
                       or (ctx.get("origin") or {}).get("workflow_run_id") or "") or None
    subject = None
    own = _ticket_of(ctx.get("ticket"))
    if own is not None:
        subject = Subject(project_id=str(row["project_id"]), entity_type="code_task_run",
                          entity_id=str(row["id"]), root_run_id=workflow_run or str(row["id"]),
                          ticket=own, actor=str(ctx.get("actor") or "") or None,
                          title=str(row["title"] or own.get("title") or ""),
                          workflow_run_id=workflow_run)
    elif workflow_run:
        subject = _workflow_subject(db, workflow_run)
    if subject is None:
        return None
    subject.entity_type = "code_task_run"
    subject.entity_id = str(row["id"])
    subject.code_task_run_id = str(row["id"])
    subject.pull_request_url = row["pull_request_url"] or ctx.get("pull_request_url")
    subject.work_item_id = subject.work_item_id or row["work_item_id"]
    subject.plane_handled = ctx.get("source") == "plane"
    if row["title"]:
        subject.title = subject.title or str(row["title"])
    return subject


def ticket_of_code_task(db: Any, run_id: str) -> Optional[dict[str, Any]]:
    """The ticket a build (code-task run) works on, for its cockpit's badge: the build's own
    ``finalize_ctx.ticket``, its workflow run's ticket, or the binding whose ``metadata.run_ids``
    names it. With the binding's sync state when there is one. None for a build not from a
    ticket (BOSSAPP-1047 Phase C)."""
    subject = _code_task_subject(db, run_id)
    if subject is None or not subject.ticket:
        return None
    t = subject.ticket
    binding = db.execute(text(
        "SELECT b.sync_status, b.external_state, b.work_item_id FROM work_tracker_bindings b "
        "WHERE b.project_id = :p AND ((:k <> '' AND b.external_key = :k) "
        "OR (:w <> '' AND b.work_item_id = :w)) ORDER BY b.updated_at DESC LIMIT 1"),
        {"p": subject.project_id, "k": str(t.get("key") or ""),
         "w": str(subject.work_item_id or "")}).mappings().first()
    from .ticket_intake import category_of_state

    state = (binding or {}).get("external_state") or t.get("status")
    return {"provider": t.get("provider"), "key": t.get("key"), "url": t.get("url"),
            "connection_id": t.get("connection_id"), "status": state,
            "status_category": category_of_state(state),
            "sync_status": (binding or {}).get("sync_status"),
            "work_item_id": subject.work_item_id or (binding or {}).get("work_item_id")}


def resolve(db: Any, entity_type: str, entity_id: str,
            data: Optional[Mapping[str, Any]] = None) -> Optional[Subject]:
    """The ticket this event is about, or None (a run not started from a ticket)."""
    data = dict(data or {})
    if entity_type == "workflow_run":
        return _workflow_subject(db, entity_id)
    if entity_type == "code_task_run":
        return _code_task_subject(db, entity_id)
    if entity_type == "artifact" and data.get("pull_request_url"):
        run_id = db.execute(text(
            "SELECT id FROM code_task_runs WHERE pull_request_url = :u "
            "ORDER BY updated_at DESC LIMIT 1"), {"u": str(data["pull_request_url"])}).scalar()
        subject = _code_task_subject(db, run_id) if run_id else None
        if subject is not None:
            subject.pull_request_url = str(data["pull_request_url"])
        return subject
    if entity_type == "stack_deployment":
        row = db.execute(text(
            "SELECT workflow_run_id, code_task_run_id FROM stack_deployments WHERE id = :id"),
            {"id": entity_id}).mappings().first()
        if row is None:
            return None
        if row["code_task_run_id"]:
            found = _code_task_subject(db, str(row["code_task_run_id"]))
            if found is not None:
                return found
        return _workflow_subject(db, str(row["workflow_run_id"])) if row["workflow_run_id"] \
            else None
    if entity_type == "deployment_rollout":
        if data.get("code_task_run_id"):
            return _code_task_subject(db, str(data["code_task_run_id"]))
        if data.get("workflow_run_id"):
            return _workflow_subject(db, str(data["workflow_run_id"]))
    return None


def binding_for(db: Any, project_id: str, provider: str, key: str = "", *,
                external_id: str = "", work_item_id: str = "") -> Optional[dict[str, Any]]:
    """The work_item <-> ticket binding: by the ticket's key or id, else by the work item."""
    row = db.execute(text(
        "SELECT b.id, b.work_item_id, b.external_key, b.external_issue_id, b.external_url, "
        "b.sync_status, b.sync_error, b.last_synced_at, b.metadata, p.provider_key, "
        "p.id AS provider_id "
        "FROM work_tracker_bindings b JOIN work_tracker_providers p ON p.id = b.provider_id "
        "WHERE b.project_id = :p AND p.provider_key = :k AND ("
        "  (:key <> '' AND b.external_key = :key) OR (:eid <> '' AND b.external_issue_id = :eid)"
        "  OR (:wi <> '' AND b.work_item_id = :wi)) "
        "ORDER BY (b.external_key = :key) DESC, b.updated_at DESC LIMIT 1"),
        {"p": project_id, "k": provider, "key": key or "", "eid": external_id or "",
         "wi": work_item_id or ""}).mappings().first()
    return dict(row) if row else None


def _binding_of_item(db: Any, project_id: str, work_item_id: str) -> Optional[dict[str, Any]]:
    row = db.execute(text(
        "SELECT b.id, b.work_item_id, b.external_key, b.external_issue_id, b.external_url, "
        "b.sync_status, b.sync_error, b.last_synced_at, b.metadata, p.provider_key, "
        "p.id AS provider_id "
        "FROM work_tracker_bindings b JOIN work_tracker_providers p ON p.id = b.provider_id "
        "WHERE b.project_id = :p AND b.work_item_id = :w ORDER BY b.updated_at DESC LIMIT 1"),
        {"p": project_id, "w": work_item_id}).mappings().first()
    return dict(row) if row else None


# ---------------------------------------------------------------------------------------------
# write-back: what each effect says, and each effect once (a receipt)
# ---------------------------------------------------------------------------------------------


def comment_for(action: str, subject: Subject, data: Optional[Mapping[str, Any]] = None
                ) -> Optional[str]:
    """The comment BOSS posts on the ticket for an action (markdown; the connector converts)."""
    data = dict(data or {})
    run = subject.root_run_id
    if action == "started":
        return (f"BOSS started work on this ticket (run `{run}`). It will link the pull request "
                "here when one opens.")
    if action == "decision_pending":
        return (f"BOSS is waiting for a decision on this work (run `{run}`). Someone on the team "
                "answers it in BOSS, and the work carries on.")
    if action == "deployed":
        url = str(data.get("open_url") or data.get("preview_url") or "").strip()
        return (f"BOSS deployed this change. Preview: {url}" if url
                else "BOSS deployed this change.")
    if action == "failed":
        reason = _short(data.get("error") or data.get("reason") or "", 240)
        return ("BOSS could not finish this work" + (f": {reason}" if reason else ".")
                + f" Run `{run}` has the details in BOSS.")
    if action == "blocked":
        reason = _short(data.get("reason") or "", 240)
        return ("BOSS's run is blocked" + (f": {reason}" if reason else ".")
                + f" Run `{run}` shows what it is waiting for.")
    if action == "change_included":
        return ("BOSS will include the updated acceptance criteria in this build "
                f"(run `{run}`).")
    if action == "change_deferred":
        return ("BOSS will finish this build as it was asked; the changed acceptance criteria "
                f"are left for the next ticket (run `{run}`).")
    return None


def _discriminator(action: str, subject: Subject, data: Mapping[str, Any],
                   event_type: str) -> str:
    """What makes one occurrence of an action different from the next (the dedupe key)."""
    if action == "started":
        return ""
    if action in ("pr_opened", "merged"):
        return str(data.get("pull_request_url") or subject.pull_request_url or "")
    if action == "decision_pending":
        return str(data.get("decision_id") or subject.entity_id)
    if action == "deployed":
        return str(data.get("open_url") or "") + "|" + subject.entity_id
    if action in ("change_included", "change_deferred"):
        return str(data.get("decision_id") or "")
    return f"{event_type}|{subject.entity_id}"


def _scope_row(db: Any, project_id: str) -> Mapping[str, Any]:
    scope = db.execute(text(
        "SELECT p.workspace_id, w.org_id FROM projects p JOIN workspaces w "
        "ON w.id = p.workspace_id WHERE p.id = :p"), {"p": project_id}).mappings().first()
    if not scope:
        raise ValueError("ticket sync: the run has no project scope")
    return scope


def _mark_binding(db: Any, binding_id: Optional[str], ok: bool,
                  error: Optional[dict[str, Any]]) -> None:
    if not binding_id:
        return
    db.execute(text(
        "UPDATE work_tracker_bindings SET sync_status = :s, sync_error = CAST(:e AS JSONB), "
        "last_synced_at = CASE WHEN :ok THEN now() ELSE last_synced_at END, updated_at = now() "
        "WHERE id = :id"),
        {"s": "synced" if ok else "failed", "e": json.dumps(error or {}), "ok": ok,
         "id": binding_id})


def _perform(conn: Any, op: str, ticket: Mapping[str, Any], args: Mapping[str, Any]
             ) -> dict[str, Any]:
    from ..connectors.tickets import TicketRef, get_ticket_adapter

    adapter = get_ticket_adapter(conn.provider)
    ref = TicketRef(conn.provider, key=ticket.get("key") or None, url=ticket.get("url") or None,
                    id=ticket.get("id") or None)
    if op == "comment":
        made = adapter.comment(conn, ref, str(args["markdown"]))
        return {"commented_at": made.at}
    if op == "transition":
        moved = adapter.transition(conn, ref, str(args["to_category"]))
        return {"from": moved.from_status, "to": moved.to_status, "changed": moved.changed}
    if op == "link":
        linked = adapter.link(conn, ref, str(args["url"]), str(args.get("title") or "Pull request"))
        return {"url": linked.url, "created": linked.created}
    raise ValueError(f"unknown ticket operation {op}")


def _sender(engine: Any, settings: Any, scope: Mapping[str, Any]) -> Callable[[], dict]:
    """One effect, sent with a connection opened for it (short transaction; the credential is
    resolved inside the shared HTTP helper, as the acting person or the bot)."""

    def send() -> dict[str, Any]:
        from ..connectors.tickets import TicketSystemError

        with engine.begin() as db:
            conn = open_connection(db, scope["project_id"], connection_id=scope.get("connection_id"),
                                   provider=scope["provider"], person_id=scope.get("acting_as"),
                                   app_settings=settings)
            if conn is None:
                raise TicketSystemError("the ticket system is not connected to this project",
                                        provider=scope["provider"])
            _apply_status_map(conn)
            result = _perform(conn, scope["op"], scope["ticket"], scope["args"])
            if conn.attribution:
                result["acted_as"] = {k: conn.attribution[-1].get(k)
                                      for k in ("auth_kind", "subject_kind", "subject_id")}
            return result

    return send


def _settle(engine: Any, receipt_id: str, scope: Mapping[str, Any],
            send: Callable[[], dict]) -> dict[str, Any]:
    from ..connectors.tickets import CapabilityNotSupported
    from .core import emit_event
    from .receipts import ReceiptService

    receipts = ReceiptService()
    error: Optional[dict[str, Any]] = None
    result: dict[str, Any] = {}
    try:
        result = dict(send() or {})
    except Exception as failure:  # noqa: BLE001 — a write-back never fails the run
        error = {"reason": _plain_error(failure), "kind": type(failure).__name__,
                 "retryable": not isinstance(failure, CapabilityNotSupported),
                 "op": scope["op"], "action": scope["action"]}
    with engine.begin() as db:
        if error is None:
            receipts.mark_succeeded(db, receipt_id, str(scope["ticket"].get("key") or ""),
                                    {**result, "op": scope["op"], "action": scope["action"]})
        else:
            receipts.mark_failed(db, receipt_id, error)
        _mark_binding(db, scope.get("binding_id"), error is None,
                      None if error is None else {**error, "receipt_id": receipt_id})
        try:
            with db.begin_nested():
                emit_event(db, scope["project_id"],
                           "ticket_sync.effect_succeeded" if error is None
                           else "ticket_sync.effect_failed", "ticket_sync", "receipt", receipt_id,
                           {"op": scope["op"], "action": scope["action"],
                            "key": scope["ticket"].get("key"), "provider": scope["provider"],
                            "work_item_id": scope.get("work_item_id"),
                            **({"reason": error["reason"]} if error else {})})
        except Exception:  # noqa: BLE001 — the receipt is the record
            logger.warning("ticket sync event failed for %s", receipt_id, exc_info=True)
    if error is not None:
        logger.warning("ticket sync %s/%s on %s failed: %s", scope["action"], scope["op"],
                       scope["ticket"].get("key"), error["reason"])
        return {"op": scope["op"], "status": "failed", "receipt_id": receipt_id,
                "reason": error["reason"], "retryable": error["retryable"]}
    return {"op": scope["op"], "status": "succeeded", "receipt_id": receipt_id, **result}


def record_effect(engine: Any, settings: Any, subject: Subject, *, action: str, event_type: str,
                  op: str, args: dict[str, Any], discriminator: str,
                  binding_id: Optional[str] = None,
                  send: Optional[Callable[[], dict]] = None) -> dict[str, Any]:
    """One write to the ticket, at most once: a receipt keyed by ticket, run, action and effect,
    committed BEFORE the call (so a crash after the provider answered cannot lose the record).
    A receipt that already exists is never sent again here; a failed one is sent again only by
    :func:`retry_failed`."""
    from .receipts import ReceiptService

    dedupe = ":".join((PREFIX, subject.project_id, subject.provider, subject.key,
                       subject.root_run_id, action, op, _digest(discriminator)))
    scope = {"project_id": subject.project_id, "provider": subject.provider,
             "connection_id": subject.ticket.get("connection_id"),
             "ticket": {k: subject.ticket.get(k) for k in ("key", "id", "url", "title")
                        if subject.ticket.get(k)},
             "work_item_id": subject.work_item_id, "binding_id": binding_id,
             "root_run_id": subject.root_run_id, "entity_type": subject.entity_type,
             "entity_id": subject.entity_id, "action": action, "event_type": event_type,
             "op": op, "args": args, "acting_as": _person(subject.actor)}
    with engine.begin() as db:
        where = _scope_row(db, subject.project_id)
        receipt, created = ReceiptService().create_or_reuse(
            db, where["org_id"], where["workspace_id"], subject.project_id, dedupe,
            f"ticket.{op}", scope, {"args": args})
    if not created:
        return {"op": op, "status": receipt["status"], "receipt_id": receipt["id"],
                "replayed": True}
    return _settle(engine, receipt["id"], scope, send or _sender(engine, settings, scope))


def write_back(engine: Any, settings: Any, subject: Subject, action: str, event_type: str,
               data: Optional[Mapping[str, Any]] = None) -> dict[str, Any]:
    """Every effect an action means, as the connection's settings allow."""
    from ..connectors.tickets import get_ticket_adapter

    data = dict(data or {})
    with engine.begin() as db:
        conn = open_connection(db, subject.project_id,
                               connection_id=subject.ticket.get("connection_id"),
                               provider=subject.provider, app_settings=settings)
        if conn is None:
            return {"synced": False, "reason": "the ticket's system is not connected"}
        options = sync_settings(conn.configuration)
        binding = binding_for(db, subject.project_id, conn.provider, subject.key,
                              external_id=str(subject.ticket.get("id") or ""),
                              work_item_id=str(subject.work_item_id or ""))
    if binding is not None:
        subject.work_item_id = subject.work_item_id or binding["work_item_id"]
    adapter = get_ticket_adapter(conn.provider)
    effects: list[dict[str, Any]] = []
    for op, category in ACTION_EFFECTS.get(action, ()):
        if not options["write_back"].get(OP_SWITCH[op], True):
            effects.append({"op": op, "status": "off", "reason": "write_back is off for this"})
            continue
        if not adapter.can(OP_CAPABILITY[op]):
            effects.append({"op": op, "status": "skipped",
                            "reason": f"{conn.provider} does not support it"})
            continue
        if op == "comment":
            markdown = comment_for(action, subject, data)
            if not markdown:
                continue
            args: dict[str, Any] = {"markdown": markdown}
        elif op == "link":
            url = str(data.get("pull_request_url") or subject.pull_request_url or "")
            if not url:
                continue
            args = {"url": url, "title": _short(f"BOSS pull request: {subject.title or url}", 200)}
        else:
            args = {"to_category": category}
        effects.append(record_effect(
            engine, settings, subject, action=action, event_type=event_type, op=op, args=args,
            discriminator=_discriminator(action, subject, data, event_type),
            binding_id=binding["id"] if binding else None))
    return {"synced": all(e.get("status") in ("succeeded", "off", "skipped") for e in effects),
            "ticket": subject.key, "action": action, "effects": effects}


def on_event(engine: Any, settings: Any, event_type: str, entity_type: str, entity_id: str,
             data: Optional[Mapping[str, Any]] = None, *, check_changes: bool = True
             ) -> dict[str, Any]:
    """The wiring entry point. Never raises: a ticket system's trouble never touches the run."""
    try:
        if event_type not in HOOKED_EVENTS:
            return {"synced": False, "reason": "unmapped"}
        with engine.begin() as db:
            subject = resolve(db, entity_type, entity_id, data)
        if subject is None:
            return {"synced": False, "reason": "not a ticket run"}
        if subject.plane_handled:
            # plane_sync mirrors this run already (a Plane-webhook code task).
            return {"synced": False, "reason": "plane_sync handles this run"}
        out: dict[str, Any] = {"ticket": subject.key}
        if check_changes and event_type in CHANGE_CHECK_EVENTS:
            out["change"] = check_ticket_changed(engine, settings, subject)
        action = EVENT_ACTIONS.get(event_type)
        if action is None or (event_type == "code_task.started" and subject.workflow_run_id):
            return {**out, "synced": False, "reason": "nothing to write"}
        return {**out, **write_back(engine, settings, subject, action, event_type, data)}
    except Exception as error:  # noqa: BLE001
        logger.warning("ticket sync on_event %s %s/%s failed: %s", event_type, entity_type,
                       entity_id, error, exc_info=True)
        return {"synced": False, "reason": type(error).__name__}


# ---------------------------------------------------------------------------------------------
# the hook: after the writer's transaction commits
# ---------------------------------------------------------------------------------------------

_QUEUE: "queue.Queue[tuple[str, str, str, str, str, dict]]" = queue.Queue(maxsize=5000)
_WORKER_LOCK = threading.Lock()
_WORKER: Optional[threading.Thread] = None
#: Seconds the worker waits for an event's transaction to commit before treating it as rolled
#: back (the catch-up sweep still finds a slow commit).
COMMIT_WAIT_SECONDS = 30.0


def hook_enabled() -> bool:
    return str(os.environ.get("BOSS_TICKET_SYNC", "on")).strip().lower() not in (
        "0", "off", "false", "no")


def notify(connection: Any, project_id: str, event_id: str, event_type: str, entity_type: str,
           entity_id: str, data: Optional[Mapping[str, Any]]) -> None:
    """core.emit_event calls this for every event; only the hooked ones are queued. Cheap and
    never raising: it runs inside the writer's transaction."""
    if event_type not in HOOKED_EVENTS or not hook_enabled():
        return
    if getattr(getattr(connection, "dialect", None), "name", "") != "postgresql":
        return
    try:
        _QUEUE.put_nowait((str(event_id or ""), str(project_id), event_type, entity_type,
                           str(entity_id), dict(data or {})))
        _ensure_worker()
    except queue.Full:
        logger.warning("ticket sync queue is full; the sweep will catch %s", event_type)
    except Exception:  # noqa: BLE001
        logger.warning("ticket sync could not queue %s", event_type, exc_info=True)


def _committed(engine: Any, event_id: str) -> bool:
    if not event_id:
        return True
    deadline = time.monotonic() + COMMIT_WAIT_SECONDS
    pause = 0.05
    while time.monotonic() < deadline:
        with engine.connect() as db:
            if db.execute(text("SELECT 1 FROM events WHERE id = :id"), {"id": event_id}).first():
                return True
        time.sleep(pause)
        pause = min(pause * 2, 1.0)
    return False


def _ensure_worker() -> None:
    global _WORKER
    if _WORKER is not None and _WORKER.is_alive():
        return
    with _WORKER_LOCK:
        if _WORKER is not None and _WORKER.is_alive():
            return

        def work() -> None:
            from ..config import get_settings
            from ..db import get_engine

            while True:
                event_id, _project, event_type, entity_type, entity_id, data = _QUEUE.get()
                try:
                    engine = get_engine()
                    if _committed(engine, event_id):
                        on_event(engine, get_settings(), event_type, entity_type, entity_id,
                                 data)
                except Exception:  # noqa: BLE001
                    logger.warning("ticket sync worker failed on %s", event_type, exc_info=True)
                finally:
                    _QUEUE.task_done()

        _WORKER = threading.Thread(target=work, daemon=True, name="boss-ticket-sync")
        _WORKER.start()


def sweep(engine: Any, settings: Any, *, window: timedelta = SWEEP_WINDOW,
          limit: int = 500) -> int:
    """Catch-up: recent hooked events of ticket runs, synced again (receipts make it a no-op
    for what already went out). Change checks are not repeated here."""
    types = sorted(set(EVENT_ACTIONS))
    with engine.begin() as db:
        rows = db.execute(text(
            "SELECT e.event_type, e.entity_type, e.entity_id, e.data FROM events e "
            "WHERE e.created_at > now() - make_interval(secs => :w) "
            "AND e.event_type = ANY(:types) AND ("
            "  (e.entity_type = 'workflow_run' AND EXISTS (SELECT 1 FROM workflow_runs r "
            "     WHERE r.id = e.entity_id AND (r.input ? 'ticket' OR EXISTS ("
            "       SELECT 1 FROM workflow_runs root WHERE root.id = r.root_run_id "
            "       AND root.input ? 'ticket'))))"
            "  OR (e.entity_type = 'code_task_run' AND EXISTS (SELECT 1 FROM code_task_runs c "
            "     LEFT JOIN workflow_runs r ON r.id = c.finalize_ctx->>'workflow_run_id' "
            "     WHERE c.id = e.entity_id AND (c.finalize_ctx ? 'ticket' OR r.input ? 'ticket')))"
            "  OR e.entity_type IN ('artifact', 'stack_deployment', 'deployment_rollout')"
            ") ORDER BY e.created_at LIMIT :n"),
            {"w": int(window.total_seconds()), "types": types, "n": limit}).mappings().all()
    done = 0
    for row in rows:
        result = on_event(engine, settings, row["event_type"], row["entity_type"],
                          row["entity_id"], _json(row["data"], {}) or {}, check_changes=False)
        done += 1 if result.get("effects") else 0
    return done


# ---------------------------------------------------------------------------------------------
# the timeline T6 shows, and retry
# ---------------------------------------------------------------------------------------------


def _receipts_of_item(db: Any, project_id: str, work_item_id: str,
                      binding: Optional[Mapping[str, Any]]) -> list[dict[str, Any]]:
    rows = db.execute(text(
        "SELECT id, effect_kind, effect_scope, status, result, error, created_at, updated_at "
        "FROM receipts WHERE project_id = :p AND dedupe_key LIKE :prefix AND ("
        "  effect_scope->>'work_item_id' = :wi OR (:key <> '' AND "
        "  effect_scope->>'provider' = :prov AND effect_scope->'ticket'->>'key' = :key) "
        "  OR effect_scope->>'root_run_id' = ANY(:runs)) "
        "ORDER BY created_at, id"),
        {"p": project_id, "prefix": f"{PREFIX}:{project_id}:%", "wi": work_item_id,
         # the runs B1 recorded on the binding (metadata.run_ids)
         "runs": [str(r) for r in ((_json((binding or {}).get("metadata"), {}) or {})
                                   .get("run_ids") or []) if r],
         "key": str((binding or {}).get("external_key") or ""),
         "prov": str((binding or {}).get("provider_key") or "")}).mappings().all()
    return [dict(r) for r in rows]


_OP_WORDS = {"comment": "Commented", "transition": "Moved", "link": "Linked"}


def _entry(row: Mapping[str, Any]) -> dict[str, Any]:
    scope = _json(row["effect_scope"], {}) or {}
    result = _json(row["result"], {}) or {}
    error = _json(row["error"], {}) or {}
    op = str(scope.get("op") or str(row["effect_kind"]).split(".", 1)[-1])
    args = scope.get("args") or {}
    if op == "transition":
        what = f"Moved to {result.get('to') or args.get('to_category')}"
    elif op == "link":
        what = f"Linked {args.get('url')}"
    else:
        what = _short(args.get("markdown"), 160)
    return {"receipt_id": row["id"], "at": _iso(row["updated_at"]), "action": scope.get("action"),
            "op": op, "label": _OP_WORDS.get(op, op), "summary": what, "status": row["status"],
            "run_id": scope.get("root_run_id"), "event": scope.get("event_type"),
            "acted_as": result.get("acted_as"),
            **({"reason": error.get("reason"),
                "retryable": bool(error.get("retryable", True))} if row["status"] == "failed"
               else {})}


def timeline(db: Any, project_id: str, work_item_id: str) -> dict[str, Any]:
    binding = _binding_of_item(db, project_id, work_item_id)
    entries = [_entry(r) for r in _receipts_of_item(db, project_id, work_item_id, binding)]
    status = (binding or {}).get("sync_status")
    failed = [e for e in entries if e["status"] == "failed"]
    return {"work_item_id": work_item_id,
            "ticket": None if binding is None else {
                "provider": binding["provider_key"], "key": binding["external_key"],
                "url": binding["external_url"], "connection_id": binding["provider_id"]},
            "sync_status": "failed" if failed else (status or ("synced" if entries else "idle")),
            "sync_error": _json((binding or {}).get("sync_error"), {}) or None,
            "last_synced_at": _iso((binding or {}).get("last_synced_at")),
            "entries": entries, "failed": len(failed)}


def retry_failed(engine: Any, settings: Any, project_id: str, work_item_id: str, *,
                 actor: Optional[str] = None) -> dict[str, Any]:
    """Send every FAILED effect of this work item's ticket again (the provider said no, so
    nothing landed). An in-progress one — an outcome nobody saw — is never resent."""
    with engine.begin() as db:
        binding = _binding_of_item(db, project_id, work_item_id)
        rows = [r for r in _receipts_of_item(db, project_id, work_item_id, binding)
                if r["status"] == "failed"]
    retried = []
    for row in rows:
        scope = dict(_json(row["effect_scope"], {}) or {})
        error = _json(row["error"], {}) or {}
        if error.get("retryable") is False:
            retried.append({"receipt_id": row["id"], "status": "failed",
                            "reason": error.get("reason"), "retryable": False})
            continue
        if _person(actor):
            scope["acting_as"] = actor
        with engine.begin() as db:
            claimed = db.execute(text(
                "UPDATE receipts SET status = 'in_progress', error = NULL, updated_at = now() "
                "WHERE id = :id AND status = 'failed' RETURNING id"), {"id": row["id"]}).first()
        if claimed is None:
            continue                          # someone else is retrying it
        retried.append(_settle(engine, row["id"], scope, _sender(engine, settings, scope)))
    return {"work_item_id": work_item_id, "retried": retried,
            "ok": all(r.get("status") == "succeeded" for r in retried)}


# ---------------------------------------------------------------------------------------------
# a ticket's snapshot, and the mid-run change card
# ---------------------------------------------------------------------------------------------


def snapshot(ticket: Any, connection_id: Optional[str]) -> dict[str, Any]:
    """The ``input.ticket`` shape (B1's): what a run is told about its ticket."""
    return {"provider": ticket.provider, "connection_id": connection_id, "key": ticket.key,
            "id": ticket.id, "url": ticket.url, "title": ticket.title,
            "body": ticket.body_markdown, "acceptance_criteria": list(ticket.acceptance_criteria),
            "comments": [{"author": c.author, "at": c.at, "body": c.body}
                         for c in ticket.comments[-5:]],
            "parent": ticket.parent_key, "type": ticket.type, "status": ticket.status,
            "labels": list(ticket.labels), "updated_at": ticket.updated_at,
            "screen": dict(ticket.screen or {})}


def _criteria(values: Any) -> list[str]:
    return [" ".join(str(v).split()) for v in (values or []) if str(v).strip()]


def check_ticket_changed(engine: Any, settings: Any, subject: Subject) -> dict[str, Any]:
    """Re-read the ticket. A new ``updated_at`` with different acceptance criteria raises ONE
    decision card per change: "Include it" / "Next ticket"."""
    from ..connectors.tickets import TicketRef, get_ticket_adapter
    from .core import create_blocking_decision, emit_event

    snap = subject.ticket
    try:
        with engine.begin() as db:
            conn = open_connection(db, subject.project_id,
                                   connection_id=snap.get("connection_id"),
                                   provider=subject.provider, person_id=subject.actor,
                                   app_settings=settings)
            if conn is None:
                return {"changed": False, "reason": "not connected"}
            fresh = get_ticket_adapter(conn.provider).get_ticket(
                conn, TicketRef(conn.provider, key=snap.get("key") or None,
                                id=snap.get("id") or None, url=snap.get("url") or None))
    except Exception as error:  # noqa: BLE001 — a check that cannot read never blocks the build
        return {"changed": False, "reason": _plain_error(error)}
    if snap.get("updated_at") and fresh.updated_at == snap.get("updated_at"):
        return {"changed": False}
    old, new = _criteria(snap.get("acceptance_criteria")), _criteria(fresh.acceptance_criteria)
    if old == new:
        return {"changed": False, "reason": "acceptance criteria unchanged"}
    marker = _digest(subject.root_run_id, new)
    with engine.begin() as db:
        existing = db.execute(text(
            "SELECT id FROM decisions WHERE project_id = :p AND recommendation->>'action' = :a "
            "AND recommendation->>'marker' = :m LIMIT 1"),
            {"p": subject.project_id, "a": CHANGED_ACTION, "m": marker}).scalar()
        if existing:
            return {"changed": True, "decision_id": existing, "duplicate": True}
        added = [c for c in new if c not in old]
        removed = [c for c in old if c not in new]
        decision_id = create_blocking_decision(
            db, subject.project_id, None,
            f"{subject.key} changed while BOSS was building it (updated "
            f"{fresh.updated_at or 'just now'}). Include the new acceptance criteria in this "
            "build, or leave them for the next ticket?",
            {"action": CHANGED_ACTION, "marker": marker, "provider": subject.provider,
             "connection_id": snap.get("connection_id"), "key": subject.key,
             "url": snap.get("url"), "root_run_id": subject.root_run_id,
             # NOT "workflow_run_id": that key makes a card a workflow gate (decision_actions.
             # kind_of), whose buttons resume a step; this card is answered by on_card_answered.
             "run_workflow_id": subject.workflow_run_id,
             "code_task_run_id": subject.code_task_run_id, "work_item_id": subject.work_item_id,
             "acceptance_criteria": new, "added": added, "removed": removed,
             "updated_at": fresh.updated_at, "screen": dict(fresh.screen or {}),
             "untrusted": fresh.untrusted},
            {}, CHANGED_ACTION)
        emit_event(db, subject.project_id, "ticket_sync.ticket_changed", "ticket_sync",
                   "decision", decision_id, {"key": subject.key, "added": len(added),
                                             "removed": len(removed),
                                             "run_id": subject.root_run_id})
    return {"changed": True, "decision_id": decision_id, "added": added, "removed": removed}


# ---------------------------------------------------------------------------------------------
# import: B1's (ticket_intake.import_by_keys, the logic of POST /v1/projects/{id}/tickets/import)
# ---------------------------------------------------------------------------------------------


def _intake_row(db: Any, project_id: str, *, connection_id: Optional[str] = None,
                provider: Optional[str] = None) -> Optional[dict[str, Any]]:
    """The project's ticket-system row a tool names: its id (B1's connection id), a Kit id
    (``kit:tickets:<provider>``), a provider, or none (the project's default connection)."""
    from . import ticket_intake as intake

    cid = str(connection_id or "").strip()
    wanted = str(provider or "").strip().lower()
    if cid.startswith("kit:tickets:"):
        wanted, cid = wanted or cid.rsplit(":", 1)[-1], ""
    if cid:
        try:
            return intake.get_row(db, cid, project_id=project_id)
        except intake.TicketApiError:
            return None
    rows = [r for r in intake.project_rows(db, project_id)
            if r.get("status") == "active" and (not wanted or r["provider_key"] == wanted)]
    rows.sort(key=lambda r: not r.get("is_default"))
    return rows[0] if rows else None


def ticket_import_items(db: Any, settings: Any, *, project_id: str, items: list[dict[str, Any]],
                        actor: str, connection_id: Optional[str] = None,
                        provider: Optional[str] = None) -> dict[str, Any]:
    """The ``ticket_import`` tool's door: a thin wrapper over B1's import
    (``ticket_intake.import_by_keys``): work items + bindings (status todo), deduped on the
    binding; an epic ``with_children`` brings its children under it."""
    from . import ticket_intake as intake

    row = _intake_row(db, project_id, connection_id=connection_id, provider=provider)
    if row is None:
        raise TicketSyncError("No ticket system is connected to this project.", status=409,
                              code="not_connected")
    done = intake.import_by_keys(db, settings, project_id=project_id, row=row, items=items,
                                 actor=actor, person_id=_person(actor))
    return {"connection_id": str(row["id"]), "provider": str(row["provider_key"]), **done}


# ---------------------------------------------------------------------------------------------
# inbound: a webhook delivery or a poll finds a ticket carrying the pick-up label
# ---------------------------------------------------------------------------------------------


def webhook_secret(db: Any, settings: Any, row: Mapping[str, Any]) -> str:
    """The connection's inbound secret: the stored secret reference
    (``configuration.webhook_secret_ref``, written by ``POST /v1/ticket-systems/{id}/webhook``
    when the webhook is created or rotated), else the operator's BOSS_<PROVIDER>_WEBHOOK_SECRET.
    Pass the HOME row (home_row): a mirror carries no secret of its own."""
    config = row.get("configuration") or {}
    hook = config.get("webhook") if isinstance(config.get("webhook"), dict) else {}
    ref = str(config.get("webhook_secret_ref") or hook.get("secret_ref") or "").strip()
    if ref:
        from ..config import get_settings
        from .secret_backends import resolve_secret_ref

        try:
            return str(resolve_secret_ref(db, ref, settings or get_settings(),
                                          project_id=str(row["project_id"])) or "")
        except Exception:  # noqa: BLE001 — an unresolvable secret verifies nothing
            logger.warning("ticket webhook secret for %s could not be resolved", row["id"])
            return ""
    return str(os.environ.get(f"BOSS_{str(row['provider_key']).upper()}_WEBHOOK_SECRET") or "")


WEBHOOK_PATH = "/v1/webhooks/tickets/{connection_id}"


def create_webhook(db: Any, settings: Any, connection_id: str, *, actor: str,
                   public_base: str) -> dict[str, Any]:
    """``POST /v1/ticket-systems/{id}/webhook``: mint the connection's inbound secret (create or
    rotate), write it THROUGH to the secret store and keep only its reference
    (``configuration.webhook_secret_ref``). The URL and the secret are returned ONCE; the old
    secret stops verifying the moment the new one is stored. Only a HOME row has one (a mirror's
    deliveries are its home's — home_row)."""
    import secrets as _secrets

    from .core import emit_event
    from .secret_backends import default_secret_backend
    from .secrets_service import put_secret

    row = provider_row(db, connection_id)
    if row is None or row["status"] != "active":
        raise TicketSyncError("No such ticket-system connection.", status=404, code="not_found")
    if is_mirror(row):
        raise TicketSyncError("This connection is managed from the project it was connected "
                              "in. Create its webhook there.", status=409, code="conflict")
    from ..connectors.tickets import get_ticket_adapter

    if not get_ticket_adapter(str(row["provider_key"])).can("webhook"):
        raise TicketSyncError(f"{row['display_name'] or row['provider_key']} does not send "
                              "webhooks; set inbound to poll instead.", status=422,
                              code="capability_not_supported")
    rotated = bool((row["configuration"] or {}).get("webhook_secret_ref"))
    secret = "whsec_" + _secrets.token_urlsafe(32)
    stored = put_secret(db, settings, project_id=str(row["project_id"]),
                        name=f"ticket_webhook_{row['id']}", value=secret,
                        secret_kind="api_token", backend=default_secret_backend(settings),
                        actor=actor)
    db.execute(text(
        "UPDATE work_tracker_providers SET configuration = COALESCE(configuration, '{}'::jsonb) "
        "|| jsonb_build_object('webhook_secret_ref', CAST(:ref AS TEXT), 'webhook', "
        "jsonb_build_object('last4', CAST(:last4 AS TEXT), 'rotated_at', now()::text)), "
        "updated_at = now() WHERE id = :id"),
        {"ref": stored["secret_ref_id"], "last4": secret[-4:], "id": row["id"]})
    emit_event(db, str(row["project_id"]),
               "ticket_system.webhook_rotated" if rotated else "ticket_system.webhook_created",
               actor, "work_tracker_provider", str(row["id"]), {"provider": row["provider_key"]})
    return {"connection_id": str(row["id"]), "provider": str(row["provider_key"]),
            "url": public_base.rstrip("/") + WEBHOOK_PATH.format(connection_id=row["id"]),
            "secret": secret, "last4": secret[-4:], "rotated": rotated,
            "note": "Shown once. Paste the URL and the secret into the ticket system's webhook "
                    "settings; rotating makes the old secret stop working at once."}


def verify_inbound_webhook(db: Any, settings: Any, connection_id: str,
                           headers: Mapping[str, str], body: bytes
                           ) -> tuple[Optional[dict[str, Any]], bool]:
    """THE verifier of ``POST /v1/webhooks/tickets/{connection_id}``: the connection exists,
    takes webhooks, and its connector's ``verify_webhook`` accepts the delivery under the
    connection's own secret. Fail-closed."""
    from ..connectors.tickets import get_ticket_adapter

    row = provider_row(db, connection_id)
    if row is None or row["status"] != "active":
        return None, False
    # A mirror's delivery is its home's: verified under the home's secret, read through the
    # home, routed by the home's mappings (pickup) — one site, one connection, never twice.
    row = home_row(db, row)
    try:
        adapter = get_ticket_adapter(str(row["provider_key"]))
    except Exception:  # noqa: BLE001
        return row, False
    if not adapter.can("webhook"):
        return row, False
    secret = webhook_secret(db, settings, row)
    if not secret:
        return row, False
    return row, bool(adapter.verify_webhook(dict(headers), body or b"", secret))


def _pickup_card(db: Any, project_id: str, provider: str, key: str) -> Optional[str]:
    return db.execute(text(
        "SELECT id FROM decisions WHERE project_id = :p AND recommendation->>'action' = :a "
        "AND recommendation->>'provider' = :k AND recommendation->>'key' = :key "
        "ORDER BY created_at DESC LIMIT 1"),
        {"p": project_id, "a": PICKUP_ACTION, "k": provider, "key": key}).scalar()


def pickup(engine: Any, settings: Any, row: Mapping[str, Any], ticket: Any, *,
           source: str) -> dict[str, Any]:
    """A ticket that carries the pick-up label: offered (ask) or imported and started
    (auto_start). Deduped on the binding (already in BOSS) and on an earlier card for it."""
    from .core import create_blocking_decision, emit_event

    with engine.begin() as db:
        home = home_row(db, row) or dict(row)
    options = sync_settings(home["configuration"])          # the HOME's settings, never a mirror's
    label = options["pickup"]["label"].lower()
    if not label or label not in {str(lb).lower() for lb in ticket.labels}:
        return {"status": "ignored", "reason": "no pick-up label", "key": ticket.key}
    with engine.begin() as db:
        row = _target_row(db, home, ticket)                  # the mapped project's row
        project_id, provider = str(row["project_id"]), str(row["provider_key"])
        found = binding_for(db, project_id, provider, ticket.key, external_id=str(ticket.id or ""))
        if found is not None:
            return {"status": "already_in_boss", "key": ticket.key,
                    "work_item_id": found["work_item_id"]}
        card = _pickup_card(db, project_id, provider, ticket.key)
        if card:
            return {"status": "already_asked", "key": ticket.key, "decision_id": card}
        if options["pickup"]["mode"] == "ask":
            decision_id = create_blocking_decision(
                db, project_id, None,
                f"{ticket.key} “{_short(ticket.title, 120)}” is labelled “{label}”. Start it in "
                "BOSS?",
                {"action": PICKUP_ACTION, "provider": provider, "connection_id": row["id"],
                 "key": ticket.key, "id": ticket.id, "url": ticket.url,
                 "title": _short(ticket.title, 200), "type": ticket.type, "source": source,
                 "screen": dict(ticket.screen or {}), "untrusted": ticket.untrusted},
                {}, PICKUP_ACTION, risk_tier=1)
            emit_event(db, project_id, "ticket_sync.pickup_waiting", "ticket_sync", "decision",
                       decision_id, {"key": ticket.key, "provider": provider, "source": source})
            return {"status": "waiting", "key": ticket.key, "decision_id": decision_id}
        return _import_and_start(db, settings, row, ticket, source=source)


def _target_row(db: Any, home: Mapping[str, Any], ticket: Any) -> dict[str, Any]:
    """Where a picked-up ticket lands: the BOSS project the home connection's ``mappings`` give
    the ticket's external project (else the default mapping), as that family's row in it (the
    home itself or its mirror). One site is read once, by its home; the mappings fan it out."""
    from . import ticket_intake as intake

    home = dict(home)
    mappings = (intake.public_settings(dict(home.get("configuration") or {})).get("mappings")
                or [])
    key = str(getattr(ticket, "key", "") or "")
    external = key.rsplit("-", 1)[0].upper() if "-" in key else ""
    chosen = next((m for m in mappings if str(m.get("external_project") or "").upper()
                   == external), None) or next((m for m in mappings if m.get("default")), None)
    target = str((chosen or {}).get("boss_project_id") or "")
    if not target or target == str(home["project_id"]):
        return home
    for rid in intake.family_ids(db, {"id": home["id"], "configuration": {}}):
        candidate = provider_row(db, rid)
        if candidate is not None and str(candidate["project_id"]) == target \
                and candidate["status"] == "active":
            return candidate
    return home


def _import_and_start(db: Any, settings: Any, row: Mapping[str, Any], ticket: Any, *,
                      source: str, decision_id: Optional[str] = None) -> dict[str, Any]:
    """Import the ticket and start its work through B1: ``ticket_intake.import_tickets`` (the
    import route's logic) and ``ticket_intake.start_from_ticket`` (the start-from-a-ticket
    logic: the workflow its type calls for, ``input.ticket``, the binding's ``run_ids``)."""
    from . import ticket_intake as intake
    from .core import emit_event

    actor = str(row.get("created_by") or "system")
    project_id = str(row["project_id"])
    irow = intake.get_row(db, str(row["id"]))
    imported = intake.import_tickets(db, settings, project_id=project_id, row=irow,
                                     items=[(ticket, [])], actor=actor)
    entry = (imported["imported"] or imported["already_in_boss"])[0]
    run, why = None, None
    try:
        with db.begin_nested():
            started = intake.start_from_ticket(db, settings, project_id=project_id,
                                               work_item_id=str(entry["work_item_id"]),
                                               actor=actor, person_id=_person(actor),
                                               ticket=ticket)
            run = started.get("run")
    except Exception as error:  # noqa: BLE001 — the import stands; a person starts it
        why = _short(str(error), 200) or type(error).__name__
        logger.warning("ticket %s imported but not started: %s", ticket.key, why)
    emit_event(db, project_id, "ticket_sync.pickup_started", "ticket_sync",
               "work_item", entry["work_item_id"],
               {"key": ticket.key, "provider": row["provider_key"], "source": source,
                "run_id": (run or {}).get("id"), "decision_id": decision_id})
    return {"status": "started" if run else "imported", "key": ticket.key,
            "work_item_id": entry["work_item_id"], "run_id": (run or {}).get("id"),
            **({"not_started": why} if why else {})}


def ingest_webhook(engine: Any, settings: Any, connection_id: str, headers: Mapping[str, str],
                   body: bytes) -> dict[str, Any]:
    """A verified delivery -> the ticket it names -> pickup. Statuses: rejected | not_found |
    ignored | waiting | started | imported | already_in_boss | already_asked."""
    from ..connectors.tickets import TicketRef, get_ticket_adapter

    with engine.begin() as db:
        row, ok = verify_inbound_webhook(db, settings, connection_id, headers, body)
    if row is None:
        return {"status": "not_found"}
    if not ok:
        return {"status": "rejected"}
    options = sync_settings(row["configuration"])
    if options["inbound"]["mode"] == "off":
        return {"status": "ignored", "reason": "inbound is off for this connection"}
    adapter = get_ticket_adapter(str(row["provider_key"]))
    try:
        event = adapter.parse_webhook(body)
    except Exception:  # noqa: BLE001
        return {"status": "invalid"}
    if event.ref is None or event.kind not in ("created", "updated"):
        return {"status": "ignored", "reason": f"{event.kind} events are not pick-ups"}
    if event.kind == "updated" and event.changed and \
            not any(str(c).lower() in ("labels", "label", "labels_list") for c in event.changed):
        return {"status": "ignored", "reason": "the labels did not change"}
    try:
        with engine.begin() as db:
            conn = connection_from_row(db, row, app_settings=settings)
            ticket = adapter.get_ticket(conn, TicketRef(adapter.provider_key, key=event.ref.key,
                                                        id=event.ref.id, url=event.ref.url))
    except Exception as error:  # noqa: BLE001
        return {"status": "unreadable", "reason": _plain_error(error)}
    return pickup(engine, settings, row, ticket, source="webhook")


# ---------------------------------------------------------------------------------------------
# answers to the two cards, and the scheduler (one firing per job, on the DB lease)
# ---------------------------------------------------------------------------------------------


def on_card_answered(connection: Any, settings: Any, row: Mapping[str, Any], *, approved: bool,
                     actor: str) -> dict[str, Any]:
    """A person answered a ticket card (DecisionService's follow-on, inside the answer's
    transaction). "Start it" imports the ticket and starts its build now — a ticket that can no
    longer be read refuses the approval. "Include it" gives the run's ticket snapshot the new
    acceptance criteria; "Next ticket" records the change on the binding for the next one. The
    comment on the ticket follows from the scheduler's tick (apply_answers)."""
    from ..connectors.tickets import TicketRef, get_ticket_adapter
    from .core import emit_event

    rec = _json(row.get("recommendation"), {}) or {}
    project_id = str(row["project_id"])
    if rec.get("action") == PICKUP_ACTION:
        if not approved:
            return {"status": "declined", "key": rec.get("key")}
        prow = provider_row(connection, str(rec.get("connection_id") or ""))
        if prow is None or prow["status"] != "active" or str(prow["project_id"]) != project_id:
            raise TicketSyncError("That ticket connection is gone; reconnect it and import the "
                                  "ticket from Tickets.", status=409, code="not_connected")
        if _person(actor):
            prow = {**prow, "created_by": actor}
        conn = connection_from_row(connection, home_row(connection, prow), person_id=actor,
                                   app_settings=settings)
        try:
            ticket = get_ticket_adapter(conn.provider).get_ticket(
                conn, TicketRef(conn.provider, key=rec.get("key") or None,
                                id=rec.get("id") or None, url=rec.get("url") or None))
        except Exception as error:  # noqa: BLE001 — the approval is refused, in words
            raise TicketSyncError(f"{rec.get('key')} could not be read: {_plain_error(error)}.",
                                  status=409, code="unreadable") from error
        return _import_and_start(connection, settings, prow, ticket, source="card",
                                 decision_id=str(row["id"]))
    if rec.get("action") != CHANGED_ACTION:
        return {"status": "not_a_ticket_card"}
    run_id = str(rec.get("run_workflow_id") or rec.get("root_run_id") or "")
    if approved and run_id:
        connection.execute(text(
            "UPDATE workflow_runs SET input = jsonb_set(input, '{ticket}', "
            "COALESCE(input->'ticket', '{}'::jsonb) || CAST(:patch AS JSONB)), "
            "updated_at = now() WHERE id = :id AND project_id = :p"),
            {"id": run_id, "p": project_id, "patch": json.dumps({
                "acceptance_criteria": rec.get("acceptance_criteria") or [],
                "updated_at": rec.get("updated_at"), "changed_mid_run": True})})
    elif not approved:
        binding = binding_for(connection, project_id, str(rec.get("provider") or ""),
                              str(rec.get("key") or ""),
                              work_item_id=str(rec.get("work_item_id") or ""))
        if binding is not None:
            connection.execute(text(
                "UPDATE work_tracker_bindings SET metadata = metadata || "
                "jsonb_build_object('deferred_change', CAST(:c AS JSONB)), updated_at = now() "
                "WHERE id = :id"),
                {"id": binding["id"], "c": json.dumps({
                    "decision_id": row["id"], "updated_at": rec.get("updated_at"),
                    "acceptance_criteria": rec.get("acceptance_criteria") or []})})
    emit_event(connection, project_id, "ticket_sync.change_answered", actor, "decision",
               str(row["id"]), {"key": rec.get("key"), "included": approved,
                                "run_id": run_id or None})
    return {"status": "included" if approved else "deferred", "key": rec.get("key")}


def apply_answers(engine: Any, settings: Any) -> list[dict[str, Any]]:
    """The comment that follows an answered mid-run change card ("BOSS will include it" / "left
    for the next ticket"), once per card (an event marks it)."""
    from .core import emit_event, has_event

    out: list[dict[str, Any]] = []
    with engine.begin() as db:
        rows = db.execute(text(
            "SELECT id, project_id, status, recommendation FROM decisions "
            "WHERE recommendation->>'action' = :a "
            "AND status IN ('approved', 'executed', 'rejected') "
            "AND updated_at > now() - interval '7 days' ORDER BY updated_at LIMIT 100"),
            {"a": CHANGED_ACTION}).mappings().all()
    for row in rows:
        rec = _json(row["recommendation"], {}) or {}
        approved = row["status"] in ("approved", "executed")
        with engine.begin() as db:
            if has_event(db, row["id"], "ticket_sync.answer_commented"):
                continue
            emit_event(db, row["project_id"], "ticket_sync.answer_commented", "ticket_sync",
                       "decision", row["id"], {"approved": approved})
            run_id = str(rec.get("run_workflow_id") or rec.get("root_run_id") or "")
            subject = resolve(db, "workflow_run", run_id) if run_id else None
        if subject is None:
            continue
        out.append({"decision_id": row["id"], **write_back(
            engine, settings, subject, "change_included" if approved else "change_deferred",
            "decision.answered", {"decision_id": row["id"]})})
    return out


def poll_connection(engine: Any, settings: Any, row: Mapping[str, Any]) -> list[dict[str, Any]]:
    """One poll: open tickets carrying the label, each through pickup (deduped on the binding)."""
    from ..connectors.tickets import get_ticket_adapter

    with engine.begin() as db:
        row = home_row(db, row) or dict(row)      # a mirror is polled once, by its home
    options = sync_settings(row["configuration"])
    label = options["pickup"]["label"]
    if not label:
        return []
    adapter = get_ticket_adapter(str(row["provider_key"]))
    if not adapter.can("browse"):
        return []
    with engine.begin() as db:
        conn = connection_from_row(db, row, app_settings=settings)
        tickets = adapter.search(conn, "", None, {"labels": [label], "open_only": True}, limit=50)
    return [pickup(engine, settings, row, t, source="poll") for t in tickets]


def _lease(engine: Any, job: str, holder: str, now: datetime) -> bool:
    from ..extensions.jobs import claim, ensure_row

    with engine.begin() as db:
        ensure_row(db, LEASE_EXTENSION, job, now)
        return claim(db, LEASE_EXTENSION, job, holder, now, LEASE_TTL)


def _release(engine: Any, job: str, holder: str, *, next_run_at: datetime, ok: bool,
             error: Optional[str], now: datetime) -> None:
    from ..extensions.jobs import finish

    with engine.begin() as db:
        finish(db, LEASE_EXTENSION, job, holder, next_run_at=next_run_at,
               status="ok" if ok else "failed", error=error, now=now)


def run_job(engine: Any, job: str, holder: str, now: datetime, every: timedelta,
            fn: Callable[[], Any]) -> Optional[dict[str, Any]]:
    """Run ``fn`` if this process wins the job's lease; None when another holds it or it is not
    due. The next firing is ``now + every``."""
    if not _lease(engine, job, holder, now):
        return None
    ok, error, result = True, None, None
    try:
        result = fn()
    except Exception as failure:  # noqa: BLE001 — the next tick tries again
        ok, error = False, _plain_error(failure)
        logger.warning("ticket job %s failed", job, exc_info=True)
    finally:
        _release(engine, job, holder, next_run_at=now + every, ok=ok, error=error, now=now)
    return {"job": job, "ok": ok, "error": error, "result": result}


def tick(engine: Any, settings: Any, *, holder: str = HOLDER,
         now: Optional[datetime] = None) -> list[dict[str, Any]]:
    """One scheduler tick: each poll-mode connection's poll, the card answers and the catch-up
    sweep — every job on its own lease, so exactly one process fires each."""
    now = now or datetime.now(timezone.utc)
    ran: list[dict[str, Any]] = []
    with engine.begin() as db:
        rows = db.execute(text(
            "SELECT id, project_id, provider_key, display_name, status, configuration, created_by "
            "FROM work_tracker_providers WHERE status = 'active'")).mappings().all()
    for raw in rows:
        row = dict(raw)
        row["configuration"] = dict(_json(row.get("configuration"), {}) or {})
        if is_mirror(row):
            continue        # its home polls the site once; pickup routes by the mappings
        options = sync_settings(row["configuration"])
        if options["inbound"]["mode"] != "poll":
            continue
        from ..connectors.tickets import REGISTRY

        if REGISTRY.get(str(row["provider_key"])) is None:
            continue
        done = run_job(engine, f"poll:{row['id']}", holder, now,
                       timedelta(minutes=options["inbound"]["poll_minutes"]),
                       lambda row=row: poll_connection(engine, settings, row))
        if done is not None:
            ran.append(done)
    for job, every, fn in (("answers", ANSWERS_EVERY, lambda: apply_answers(engine, settings)),
                           ("sweep", SWEEP_EVERY, lambda: sweep(engine, settings))):
        done = run_job(engine, job, holder, now, every, fn)
        if done is not None:
            ran.append(done)
    return ran


_STARTED = threading.Event()


def start(engine: Any, settings: Any) -> bool:
    """The scheduler thread (system_threads.start_all). Every API process ticks; the lease lets
    one of them fire each job."""
    if _STARTED.is_set():
        return False
    _STARTED.set()
    interval = float(getattr(settings, "ticket_sync_tick_seconds", TICK_SECONDS) or TICK_SECONDS)

    def loop() -> None:
        stop = threading.Event()
        while not stop.wait(interval):
            try:
                tick(engine, settings)
            except Exception:  # noqa: BLE001
                logger.warning("ticket sync tick failed", exc_info=True)

    threading.Thread(target=loop, daemon=True, name="boss-ticket-scheduler").start()
    return True


# ---------------------------------------------------------------------------------------------
# a person's own ticket accounts (Connect with Atlassian)
# ---------------------------------------------------------------------------------------------


def provider_identity(conn: Any, person: str) -> dict[str, Any]:
    """Who the person's own sign-in is at the provider: ``{email, name, account_id}`` from the
    manifest's ``identity_url`` (Atlassian ``/me``), called with THEIR token. Empty when the
    connector has no identity endpoint."""
    from ..connectors.tickets import auth as ticket_auth
    from ..connectors.tickets import get_ticket_adapter

    adapter = get_ticket_adapter(conn.provider)
    spec = ticket_auth.oauth_spec(adapter) or {}
    url = str(spec.get("identity_url") or "")
    if not url:
        return {}
    conn.configuration["credential_policy"] = "per_user"
    conn.person_id = person
    conn.principal = None
    credential = ticket_auth.select_credential(conn, adapter)
    status, payload = ticket_auth._oauth_request(adapter, "GET", url, bearer=credential.token)
    if status >= 300 or not isinstance(payload, dict):
        return {}
    return {"email": str(payload.get("email") or "").strip().lower() or None,
            "name": payload.get("name") or payload.get("nickname"),
            "account_id": payload.get("account_id") or payload.get("accountId")}


def remember_identity(db: Any, project_id: str, connection_kit_id: str, person: str,
                      identity: Mapping[str, Any]) -> None:
    """Who the provider says signed in, kept as the credential's ``connected_as`` (the console's
    column for exactly this)."""
    shown = identity.get("email") or identity.get("name")
    if not shown:
        return
    db.execute(text(
        "UPDATE connection_credentials SET connected_as = :a, updated_at = now() "
        "WHERE project_id = :p AND connection_id = :c AND subject_kind = 'user' "
        "AND subject_id = :s"),
        {"a": str(shown)[:300], "p": project_id, "c": connection_kit_id, "s": person})


def confirm_identity(db: Any, person: str, connection_id: str) -> Optional[dict[str, Any]]:
    """The person says the ticket-system account they signed in as is theirs (the console asks
    when its email is not their verified BOSS email). Kept on their own credential row, naming
    the account confirmed, with an audit event. ``email_matches`` stays false; the identity reads
    ``confirmed_by_person: true``. None when the person has no sign-in on that connection."""
    from .core import emit_event

    row = db.execute(text(
        "SELECT c.id, c.project_id, c.connected_as FROM connection_credentials c "
        "JOIN work_tracker_providers p ON p.project_id = c.project_id "
        "AND 'kit:tickets:' || p.provider_key = c.connection_id "
        "WHERE p.id = :cid AND c.subject_kind = 'user' AND c.subject_id = :s "
        "AND c.status = 'active'"), {"cid": connection_id, "s": person}).mappings().first()
    if row is None:
        return None
    db.execute(text(
        "UPDATE connection_credentials SET confirmed_as = connected_as, confirmed_at = now(), "
        "updated_at = now() WHERE id = :id"), {"id": row["id"]})
    emit_event(db, str(row["project_id"]), "ticket_identity.confirmed", person,
               "connection_credential", str(row["id"]),
               {"connection_id": connection_id, "account": row["connected_as"]})
    mine = [i for i in my_identities(db, person, str(row["project_id"]))
            if i["credential_id"] == row["id"]]
    return mine[0] if mine else None


def my_identities(db: Any, person: str, project_id: Optional[str] = None) -> list[dict[str, Any]]:
    """The person's linked ticket accounts, each with ``email_matches``: the provider's email
    equals the person's VERIFIED BOSS email (when they differ, the console asks them to confirm
    the account is theirs)."""
    me = db.execute(text(
        "SELECT lower(email) AS email, email_verified_at FROM identity_users "
        "WHERE principal_id = :p"), {"p": person}).mappings().first()
    verified = str(me["email"]) if me and me["email_verified_at"] is not None else ""
    rows = db.execute(text(
        "SELECT c.id, c.project_id, c.connection_id, c.connected_as, c.status, c.scopes, "
        "c.created_at, c.updated_at, c.confirmed_as, c.confirmed_at, "
        "p.id AS ticket_connection_id, p.display_name "
        "FROM connection_credentials c LEFT JOIN work_tracker_providers p "
        "ON p.project_id = c.project_id AND 'kit:tickets:' || p.provider_key = c.connection_id "
        "WHERE c.subject_kind = 'user' AND c.subject_id = :s "
        "AND c.connection_id LIKE 'kit:tickets:%' AND (:pid = '' OR c.project_id = :pid) "
        "ORDER BY c.updated_at DESC"), {"s": person, "pid": project_id or ""}).mappings().all()
    out = []
    for row in rows:
        shown = str(row["connected_as"] or "")
        email = shown.lower() if "@" in shown else None
        matches = bool(email and verified and email == verified)
        # A person's own word that the account is theirs, for the account shown NOW only.
        confirmed = bool(shown and row["confirmed_as"] and row["confirmed_as"] == shown)
        out.append({"credential_id": row["id"], "project_id": row["project_id"],
                    "provider": str(row["connection_id"]).rsplit(":", 1)[-1],
                    "connection_id": row["ticket_connection_id"],
                    "display_name": row["display_name"], "account": shown or None,
                    "email": email, "email_matches": matches,
                    "confirmed_by_person": confirmed, "confirmed_at": _iso(row["confirmed_at"])
                    if confirmed else None,
                    "needs_confirmation": not matches and not confirmed, "status": row["status"],
                    "scopes": list(row["scopes"] or []), "linked_at": _iso(row["created_at"]),
                    "updated_at": _iso(row["updated_at"])})
    return out


__all__ = [
    "ACTION_EFFECTS", "CHANGED_ACTION", "EVENT_ACTIONS", "HOOKED_EVENTS", "PICKUP_ACTION",
    "Subject", "TicketSyncError", "apply_answers", "binding_for", "check_ticket_changed",
    "comment_for", "confirm_identity", "ticket_of_code_task", "ingest_webhook", "my_identities", "notify", "on_card_answered", "on_event",
    "open_connection",
    "pickup", "poll_connection", "provider_identity", "provider_row", "record_effect",
    "remember_identity", "resolve", "retry_failed", "run_job", "snapshot", "start", "sweep",
    "sync_settings", "tick", "ticket_import_items", "timeline", "verify_inbound_webhook",
    "write_back", "create_webhook", "home_row",
]
