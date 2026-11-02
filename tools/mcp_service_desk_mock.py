"""The Service Desk mock MCP server: past and open tickets for the history steps, the Compliance
Analyst's citation and the DQ pipeline's incidents. Zero config: in-process, no network, every
fixture deterministic. Run standalone:

    .venv/bin/python -m uvicorn tools.mcp_service_desk_mock:app --port 8766

TWO BOOKS OF TICKETS, ONE SERVICE DESK:

* INC-2088, the regulatory case's cashier story: a $250.00 payment with $31.40 interest due
  should pay down $218.60; the bug paid the full $250.00 down instead.
* NorthPeak Bank's data-quality incidents (tools/dq_sample): ten closed incidents with their
  confirmed cause, fix and how long the fix held, and INC-2301, still being investigated; plus
  the bank's assignment groups (teams, their people).

THE DQ PIPELINE WRITES HERE, as a real service desk is written to: Sentinel opens a ticket
(`create_incident`, assigned to the rule owner's group) or attaches an exception group to
an open one (`attach_to_incident`); stages add work notes, and the Escalation and Close and
Learn agents update it (`update_incident`: severity only rises; status resolved/closed).
A new ticket's number is derived from the exception group that opened it (INC-5nn, nn = the
group's place in the DQ Monitor's list), so opening twice is the same ticket and a restart renumbers
nothing. What was written is kept in memory (a restart forgets it); the investigation's own
record is the Knowledge Graph's Context Graph.

Implements: initialize, notifications/initialized, tools/list, tools/call.
"""
from __future__ import annotations

import copy
import json
import threading
from functools import lru_cache
from typing import Any

from fastapi import FastAPI, Request, Response

app = FastAPI(title="boss-service-desk-mock")

INCIDENTS: dict[str, dict[str, Any]] = {
    "INC-2088": {
        "incident_id": "INC-2088",
        "title": "Payment misapplied to principal instead of interest-then-principal order",
        "summary": "A $250.00 payment with $31.40 interest due should have paid down $218.60 "
                   "of principal; the system paid the full $250.00 down instead, skipping the "
                   "interest-first allocation Reg Z's repayment-order rule requires.",
        "payment_amount": 250.00,
        "interest_due": 31.40,
        "correct_paydown": 218.60,
        "actual_paydown": 250.00,
        "status": "resolved",
        # What a real ticket's "affected component" field carries: the code the fix touched.
        "affected_components": ["LoanTransactionType.isRepaymentType",
                               "AcmeLoanRepaymentScheduleTransactionProcessor"],
    },
}

SEVERITY_ORDER = ("Low", "Medium", "High", "Critical")
_OPEN = ("open", "investigating", "in_progress")
_LOCK = threading.Lock()
#: Tickets the DQ pipeline opened or changed, by id (in memory).
_WRITTEN: dict[str, dict[str, Any]] = {}


@lru_cache(maxsize=1)
def _dq() -> tuple[dict[str, dict[str, Any]], dict[str, dict[str, Any]]]:
    """The bank's DQ tickets and assignment groups, from tools/dq_sample (read once)."""
    from tools.dq_sample import data as D

    ds = D.generate()
    team_of = {o.owner_id: (o.team_id or o.owner_id) for o in ds.owners}
    groups = {o.owner_id: o.service_desk_group for o in ds.owners if o.service_desk_group}
    teams = {}
    for o in ds.owners:
        if o.owner_type == "team":
            teams[o.service_desk_group] = {"assignment_group": o.service_desk_group, "team_id": o.owner_id,
                                   "name": o.name, "remit": o.title,
                                   "members": [{"owner_id": p.owner_id, "name": p.name,
                                                "title": p.title}
                                               for p in ds.owners if p.team_id == o.owner_id]}
    tickets = {}
    for i in ds.incidents:
        rule = ds.rule(i.rule_id)
        tickets[i.incident_id] = {
            "incident_id": i.incident_id, "domain": "data_quality", "title": i.title,
            "summary": i.confirmed_cause or i.title,
            "status": "closed" if i.status == "closed" else "investigating",
            "opened": i.opened, "closed": i.closed, "severity": i.severity,
            "rule_id": i.rule_id, "monitor_rule_id": rule.monitor_id, "cde_ids": list(i.cde_ids),
            "assignment_group": groups.get(team_of.get(i.owner_id, ""), ""),
            "owner_id": i.owner_id, "root_cause_family": i.root_cause_family,
            "confirmed_cause": i.confirmed_cause, "ruled_out": list(i.ruled_out),
            "fix": i.fix, "fix_type": i.fix_type, "fix_durability": i.fix_durability,
            "durability_note": i.durability_note, "correlated_with": list(i.correlated_with),
            "exception_groups": [i.raised_from] if i.raised_from else [],
            "escalation_level": 0, "work_notes": []}
    return tickets, teams


def _group_number(group_id: str) -> str:
    from tools.dq_sample import data as D

    ids = [g.group_id for g in D.generate().exception_groups]
    if group_id in ids:
        return f"INC-5{ids.index(group_id) + 1:02d}"
    digest = sum(ord(ch) * (n + 1) for n, ch in enumerate(group_id)) % 900
    return f"INC-6{digest:02d}"


def _ticket(incident_id: str) -> dict[str, Any] | None:
    if incident_id in _WRITTEN:
        return _WRITTEN[incident_id]
    if incident_id in INCIDENTS:
        return INCIDENTS[incident_id]
    return _dq()[0].get(incident_id)


def _all() -> dict[str, dict[str, Any]]:
    out = {**INCIDENTS, **_dq()[0]}
    out.update(_WRITTEN)
    return out


def _writable(incident_id: str) -> dict[str, Any] | None:
    """The ticket as this service desk will change it (a copy of a fixture the first time)."""
    if incident_id in _WRITTEN:
        return _WRITTEN[incident_id]
    found = _ticket(incident_id)
    if found is None:
        return None
    _WRITTEN[incident_id] = copy.deepcopy(found)
    return _WRITTEN[incident_id]


def reset() -> None:
    """Forget everything written (tests; a restart does the same)."""
    with _LOCK:
        _WRITTEN.clear()


# ── reads ─────────────────────────────────────────────────────────────────────────────────────

def _get_incident(args: dict) -> dict:
    incident_id = str(args.get("incident_id") or "").strip()
    incident = _ticket(incident_id)
    if incident is None:
        return {"error": f"no incident '{incident_id}'", "known_incidents": sorted(_all())}
    return copy.deepcopy(incident)


def _list_incidents(args: dict) -> dict:
    """Every ticket, or those matching every filter given (rule, status, group, domain)."""
    rule_id = str(args.get("rule_id") or "").strip()
    status = str(args.get("status") or "").strip().lower()
    group = str(args.get("assignment_group") or "").strip()
    domain = str(args.get("domain") or "").strip()
    out = []
    for incident in _all().values():
        if rule_id and incident.get("rule_id") != rule_id:
            continue
        if status == "open" and incident.get("status") not in _OPEN:
            continue
        if status and status != "open" and incident.get("status") != status:
            continue
        if group and incident.get("assignment_group") != group:
            continue
        if domain and incident.get("domain") != domain:
            continue
        out.append(copy.deepcopy(incident))
    out.sort(key=lambda i: str(i.get("incident_id")))
    return {"incidents": out}


def _list_teams(args: dict) -> dict:
    return {"teams": list(_dq()[1].values())}


# ── writes (the DQ pipeline's) ────────────────────────────────────────────────────────────────

def _create_incident(args: dict) -> dict:
    source = str(args.get("source_ref") or "").strip()
    title = str(args.get("title") or "").strip()
    group = str(args.get("assignment_group") or "").strip()
    if not (source and title and group):
        return {"error": "a ticket needs title, assignment_group and source_ref (the exception "
                         "group that raised it)"}
    if group not in _dq()[1]:
        return {"error": f"no assignment group '{group}'", "groups": sorted(_dq()[1])}
    severity = str(args.get("severity") or "Medium").capitalize()
    if severity not in SEVERITY_ORDER:
        return {"error": f"severity is one of {', '.join(SEVERITY_ORDER)}"}
    incident_id = _group_number(source)
    with _LOCK:
        existing = _ticket(incident_id)
        if existing is not None and source in (existing.get("exception_groups") or []):
            return {**copy.deepcopy(existing), "created": False}
        _WRITTEN[incident_id] = {
            "incident_id": incident_id, "domain": "data_quality", "title": title,
            "summary": str(args.get("summary") or title), "status": "open",
            "opened": str(args.get("opened") or ""), "closed": None, "severity": severity,
            "rule_id": str(args.get("rule_id") or ""),
            "monitor_rule_id": str(args.get("monitor_rule_id") or ""),
            "cde_ids": [str(c) for c in args.get("cde_ids") or []],
            "assignment_group": group, "owner_id": str(args.get("owner_id") or ""),
            "root_cause_family": None, "confirmed_cause": None, "ruled_out": [], "fix": None,
            "fix_type": None, "fix_durability": None, "durability_note": None,
            "correlated_with": [], "exception_groups": [source], "escalation_level": 0,
            "work_notes": []}
        return {**copy.deepcopy(_WRITTEN[incident_id]), "created": True}


def _attach_to_incident(args: dict) -> dict:
    incident_id = str(args.get("incident_id") or "").strip()
    source = str(args.get("source_ref") or "").strip()
    with _LOCK:
        ticket = _writable(incident_id)
        if ticket is None:
            return {"error": f"no incident '{incident_id}'"}
        if ticket.get("status") not in _OPEN:
            return {"error": f"{incident_id} is {ticket.get('status')}: attach only to an open "
                             "incident"}
        if source and source not in ticket["exception_groups"]:
            ticket["exception_groups"].append(source)
        if args.get("note"):
            ticket["work_notes"].append({"by": str(args.get("by") or "BOSS"),
                                         "note": str(args["note"])[:2000]})
        return {**copy.deepcopy(ticket), "attached": source or None}


def _update_incident(args: dict) -> dict:
    """Status, severity (only ever up), escalation level, a work note, the cause and fix."""
    incident_id = str(args.get("incident_id") or "").strip()
    with _LOCK:
        ticket = _writable(incident_id)
        if ticket is None:
            return {"error": f"no incident '{incident_id}'"}
        severity = str(args.get("severity") or "").capitalize()
        if severity:
            if severity not in SEVERITY_ORDER:
                return {"error": f"severity is one of {', '.join(SEVERITY_ORDER)}"}
            if SEVERITY_ORDER.index(severity) < SEVERITY_ORDER.index(ticket.get("severity")
                                                                     or "Low"):
                return {"error": f"{incident_id} is {ticket['severity']}: severity only moves "
                                 "up"}
            ticket["severity"] = severity
        status = str(args.get("status") or "").strip().lower()
        if status:
            if status not in ("open", "investigating", "in_progress", "resolved", "closed"):
                return {"error": "status is open, investigating, in_progress, resolved or closed"}
            ticket["status"] = status
            if status in ("resolved", "closed"):
                ticket["closed"] = str(args.get("closed") or ticket.get("closed") or "")
        for field in ("confirmed_cause", "root_cause_family", "fix", "fix_type"):
            if args.get(field):
                ticket[field] = str(args[field])[:2000]
        if args.get("escalation_level") not in (None, ""):
            ticket["escalation_level"] = max(int(ticket.get("escalation_level") or 0),
                                             int(args["escalation_level"]))
        if args.get("note"):
            ticket["work_notes"].append({"by": str(args.get("by") or "BOSS"),
                                         "note": str(args["note"])[:2000]})
        return copy.deepcopy(ticket)


_STR = {"type": "string"}
TOOLS = [
    {
        "name": "get_incident",
        "description": "Get a Service Desk incident/ticket by id: its detail, status, assignment group, "
                       "and for a DQ ticket the confirmed cause, fix and how long it held.",
        "inputSchema": {"type": "object",
                        "properties": {"incident_id": {"type": "string"}},
                        "required": ["incident_id"]},
    },
    {
        "name": "list_incidents",
        "description": "List tickets; filter by rule_id, status (open = not yet resolved), "
                       "assignment_group or domain (data_quality).",
        "inputSchema": {"type": "object", "properties": {
            "rule_id": _STR, "status": _STR, "assignment_group": _STR, "domain": _STR}},
    },
    {
        "name": "list_teams",
        "description": "The assignment groups (teams) and their people.",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "create_incident",
        "description": "Open a ticket for an exception group (source_ref), assigned to a group. "
                       "Opening the same group twice returns the same ticket.",
        "inputSchema": {"type": "object", "properties": {
            "title": _STR, "summary": _STR, "assignment_group": _STR, "severity": _STR,
            "source_ref": _STR, "rule_id": _STR, "monitor_rule_id": _STR, "owner_id": _STR,
            "opened": _STR, "cde_ids": {"type": "array", "items": _STR}},
            "required": ["title", "assignment_group", "source_ref"]},
    },
    {
        "name": "attach_to_incident",
        "description": "Attach another exception group to an OPEN ticket, with a work note.",
        "inputSchema": {"type": "object", "properties": {
            "incident_id": _STR, "source_ref": _STR, "note": _STR, "by": _STR},
            "required": ["incident_id", "source_ref"]},
    },
    {
        "name": "update_incident",
        "description": "Update a ticket: status, severity (only ever up), escalation level, a "
                       "work note, the confirmed cause and the fix.",
        "inputSchema": {"type": "object", "properties": {
            "incident_id": _STR, "status": _STR, "severity": _STR, "note": _STR, "by": _STR,
            "escalation_level": {"type": "integer"}, "confirmed_cause": _STR,
            "root_cause_family": _STR, "fix": _STR, "fix_type": _STR, "closed": _STR},
            "required": ["incident_id"]},
    },
]
TOOL_FNS = {"get_incident": _get_incident, "list_incidents": _list_incidents,
            "list_teams": _list_teams, "create_incident": _create_incident,
            "attach_to_incident": _attach_to_incident, "update_incident": _update_incident}


@app.post("/mcp")
async def mcp(request: Request):
    body = await request.json()
    method = body.get("method")
    rid = body.get("id")
    params = body.get("params") or {}

    if method == "initialize":
        return {"jsonrpc": "2.0", "id": rid,
                "result": {"protocolVersion": "2024-11-05", "capabilities": {"tools": {}},
                           "serverInfo": {"name": "boss-service-desk-mock", "version": "0.2.0"}}}
    if method == "notifications/initialized":
        return Response(status_code=202)
    if method == "tools/list":
        return {"jsonrpc": "2.0", "id": rid, "result": {"tools": TOOLS}}
    if method == "tools/call":
        name = params.get("name")
        args = params.get("arguments") or {}
        fn = TOOL_FNS.get(name)
        if fn is None:
            return {"jsonrpc": "2.0", "id": rid,
                    "error": {"code": -32602, "message": f"unknown tool {name}"}}
        result = fn(args)
        return {"jsonrpc": "2.0", "id": rid,
                "result": {"content": [{"type": "text", "text": json.dumps(result)}],
                           "isError": bool(isinstance(result, dict) and result.get("error"))}}
    return {"jsonrpc": "2.0", "id": rid,
            "error": {"code": -32601, "message": f"method not found: {method}"}}


@app.get("/health")
def health():
    return {"status": "ok", "server": "boss-service-desk-mock", "tools": [t["name"] for t in TOOLS]}
