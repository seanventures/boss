"""THE INFRA AGENT'S TOOLS (boss-infra-agent-lld.md §3, §4.3) — typed, receipted, no shell.

Offered to the Infra Agent through the `storage.steward` bundle (agent_baseline.bundles) and to
a client agent / MCP through the default registry. Every write is `safe`: each one either reads,
moves the deterministic executor one notch (which dry-runs, receipts and asks a person for tier
3 by itself), opens a card, or writes words on the incident. Nothing here deletes anything the
executor would not delete on its own; the model never writes a command.
"""
from __future__ import annotations

from typing import Any

from .registry import Exposure, ToolRegistry

STORAGE_TOOLS: tuple[str, ...] = ("storage_inspect", "storage_plan", "storage_run_step",
                                  "storage_propose_rule", "storage_report", "work_hold_set",
                                  "work_hold_release")
STORAGE_READS: tuple[str, ...] = ("storage_inspect", "storage_plan")
STORAGE_WRITES: tuple[str, ...] = ("storage_run_step", "storage_propose_rule", "storage_report",
                                   "work_hold_set", "work_hold_release")


def _obj(props: dict, required: list[str] | None = None) -> dict:
    return {"type": "object", "properties": props, "required": required or [],
            "additionalProperties": False}


def _safe(value: Any) -> Any:
    import json

    return json.loads(json.dumps(value, default=str))


def _caller_scope(connection: Any, project_id: Any) -> tuple[str | None, str | None, bool]:
    """(principal_id, org_id, is_install_operator) for the current tool call."""
    from .registry import CURRENT_TOOL_AUTH
    from ..services import storage_incidents

    auth = CURRENT_TOOL_AUTH.get()
    principal = getattr(auth, "principal", None) if auth is not None else None
    pid = getattr(principal, "id", None) if principal is not None else None
    org_id = getattr(principal, "org_id", None) if principal is not None else None
    if auth is None and not org_id and project_id:
        # Only an unauthenticated (internal/test) call derives its org from the project. An
        # authenticated principal without an org fails closed, in shadow mode too (matching
        # the HTTP routes): it never borrows the org of whatever project_id it passes.
        org_id = storage_incidents.project_org_id(connection, str(project_id))
    is_op = storage_incidents.is_install_operator(connection, pid) if auth is not None else (
        storage_incidents.is_install_operator(connection, None)
    )
    return (str(pid) if pid else None), (str(org_id) if org_id else None), is_op


def _require_operator(connection: Any) -> dict | None:
    from .registry import CURRENT_TOOL_AUTH
    from ..services import storage_incidents

    auth = CURRENT_TOOL_AUTH.get()
    if auth is None:
        return None
    principal = getattr(auth, "principal", None)
    pid = getattr(principal, "id", None) if principal is not None else None
    if not storage_incidents.is_install_operator(connection, pid):
        return {
            "ok": False,
            "error": "forbidden",
            "permission": "instance:manage",
            "message": "Install-wide storage mutations require instance:manage (instance admin).",
        }
    return None


def register_storage_tools(reg: ToolRegistry) -> None:
    exposure = Exposure(surfaces=("chat", "agent_step", "mcp"), tags=("infra", "storage"),
                        search_hint="disk full storage cleanup infra agent backups docker")

    @reg.tool(
        "storage_inspect",
        "Storage on a BOSS host as the Infra Agent sees it: free space, usage by type (backups, "
        "images, build cache, volumes, logs, database, Docker VM unreclaimed), growth per day and "
        "the 'full in N days' forecast, thresholds, retention rules, the open incident (with its "
        "plan and flow) and the install-wide work hold. Read this before explaining a cause.",
        _obj({"project_id": {"type": "string"}, "host_id": {"type": "string"},
              "incident_id": {"type": "string"}}),
        kind="read", permission="system:read", org_scope=True, exposure=exposure)
    def storage_inspect(ctx, *, project_id, actor, args):
        from ..services import storage_incidents, storage_watch, work_hold

        with ctx.begin() as c:
            _pid, org_id, is_op = _caller_scope(c, project_id)
            incident = None
            if args.get("incident_id"):
                row = storage_incidents.get(c, str(args["incident_id"]))
                if row is not None:
                    if is_op:
                        incident = storage_incidents.public(c, row)
                    elif org_id and storage_incidents.incident_visible_to_org(c, row, org_id):
                        incident = storage_incidents.org_scoped_summary_incident(c, row, org_id)
            if is_op:
                summary = storage_watch.summary(c, args.get("host_id") or (incident or {}).get("host_id"))
            else:
                summary = storage_incidents.scoped_summary(
                    c, org_id, is_operator=False,
                    host_id=args.get("host_id") or (incident or {}).get("host_id"),
                )
            if incident is None and summary.get("open_incident_id"):
                row = storage_incidents.get(c, str(summary["open_incident_id"]))
                if row is not None:
                    if is_op:
                        incident = storage_incidents.public(c, row)
                    elif org_id and storage_incidents.incident_visible_to_org(c, row, org_id):
                        incident = storage_incidents.org_scoped_summary_incident(c, row, org_id)
            hold = (
                work_hold.status(c)
                if is_op
                else storage_incidents.scoped_hold_status(c, org_id, is_operator=False)
            )
            return _safe({"summary": summary, "incident": incident, "hold": hold})

    @reg.tool(
        "storage_plan",
        "The ordered cleanup plan of a storage incident: each step's tool, tier (1 rebuildable, "
        "2 labelled past an approved rule, 3 needs a person), state, dry-run item count and GB, "
        "GB freed, receipt and decision ids. Tier 3 steps are never run by an agent: they wait "
        "on the card the executor opened.",
        _obj({"project_id": {"type": "string"}, "incident_id": {"type": "string"}}, ["incident_id"]),
        kind="read", permission="system:read", org_scope=True, exposure=exposure)
    def storage_plan(ctx, *, project_id, actor, args):
        from ..services import storage_incidents

        with ctx.begin() as c:
            _pid, org_id, is_op = _caller_scope(c, project_id)
            row = storage_incidents.get(c, str(args["incident_id"]))
            if row is None or (not is_op and (not org_id or not storage_incidents.incident_visible_to_org(c, row, org_id))):
                return {"ok": False, "error": "no such incident"}
            inc = (
                storage_incidents.public(c, row)
                if is_op
                else storage_incidents.org_scoped_summary_incident(c, row, org_id)
            )
            return _safe({"incident_id": inc["id"], "status": inc["status"], "plan": inc["plan"],
                          "flow": inc["flow"], "freed_gb": inc["freed_gb"],
                          "target_pct": inc["target_pct"], "free_pct_now": inc["free_pct_now"]})

    @reg.tool(
        "storage_run_step",
        "Move the incident's deterministic cleanup one notch now instead of waiting for the next "
        "30-second tick: dry-run the next step, hand a host step to the helper, run a Postgres "
        "step, or raise the tier-3 card. Tiers 1–2 only; tier 3 always goes to a person. Stops "
        "by itself once free space is above the target.",
        _obj({"project_id": {"type": "string"}, "incident_id": {"type": "string"}}, ["incident_id"]),
        kind="write", reversibility="safe", permission="instance:manage", org_scope=True,
        exposure=exposure, idempotency="idempotent")
    def storage_run_step(ctx, *, project_id, actor, args):
        from ..services import storage_cleanup

        with ctx.begin() as c:
            refused = _require_operator(c)
            if refused is not None:
                return refused
        return _safe(storage_cleanup.advance(ctx.engine, str(args["incident_id"]),
                                             actor=actor or "agent:infra"))

    @reg.tool(
        "storage_propose_rule",
        "Propose a permanent Layer 1 retention rule from what this incident taught (a cause seen "
        "again): a change-consent card a person approves. kind: backups | images | build_cache | "
        "logs | telemetry | scratch_db | docker_trim. rule: the typed numbers, e.g. "
        "{keep_newest: 10, keep_days: 7, cap_gb: 20}.",
        _obj({"project_id": {"type": "string"}, "incident_id": {"type": "string"},
              "kind": {"type": "string"}, "rule": {"type": "object"}, "why": {"type": "string"}},
             ["incident_id", "kind", "rule", "why"]),
        kind="write", reversibility="safe", permission="instance:manage", org_scope=True,
        exposure=exposure)
    def storage_propose_rule(ctx, *, project_id, actor, args):
        from ..services import storage_incidents, storage_rules

        kind = str(args["kind"])
        if kind not in storage_rules.KINDS or kind == "threshold":
            return {"ok": False, "error": f"kind must be one of {', '.join(k for k in storage_rules.KINDS if k != 'threshold')}"}
        rule = dict(args.get("rule") or {})
        if storage_incidents.duplicates_shipped_default(kind, rule):
            return {"ok": False, "error": "that rule duplicates the shipped default; it is already in force"}
        with ctx.begin() as c:
            refused = _require_operator(c)
            if refused is not None:
                return refused
            inc = storage_incidents.get(c, str(args["incident_id"]))
            if inc is None:
                return {"ok": False, "error": "no such incident"}
            decision_id = storage_incidents.propose_rule(c, inc, kind=kind, rule=rule,
                                                         why=str(args.get("why") or "")[:500],
                                                         actor=actor or "agent:infra")
        if not decision_id:
            return {"ok": False, "error": "that rule duplicates the shipped default; it is already in force"}
        return {"ok": True, "decision_id": decision_id}

    @reg.tool(
        "storage_report",
        "Write the Infra Agent's words on the incident: the cause in one plain sentence (shown on "
        "the strip, the card and the report) and optional notes for the report. Words only; it "
        "changes no plan and deletes nothing.",
        _obj({"project_id": {"type": "string"}, "incident_id": {"type": "string"},
              "cause_text": {"type": "string"}, "notes": {"type": "string"}}, ["incident_id"]),
        kind="write", reversibility="safe", permission="instance:manage", org_scope=True,
        exposure=exposure)
    def storage_report(ctx, *, project_id, actor, args):
        from sqlalchemy import text

        from ..services import storage_incidents

        with ctx.begin() as c:
            refused = _require_operator(c)
            if refused is not None:
                return refused
            inc = storage_incidents.get(c, str(args["incident_id"]))
            if inc is None:
                return {"ok": False, "error": "no such incident"}
            cause = str(args.get("cause_text") or "").strip()[:300]
            notes = str(args.get("notes") or "").strip()[:2000]
            if cause:
                c.execute(text("UPDATE storage_incidents SET cause_text = :t, updated_at = now() WHERE id = :id"),
                          {"t": cause, "id": inc["id"]})
            if notes:
                c.execute(text("UPDATE storage_incidents SET report = COALESCE(report, '{}'::jsonb) || "
                               "jsonb_build_object('agent_notes', CAST(:n AS text)), updated_at = now() "
                               "WHERE id = :id"), {"n": notes, "id": inc["id"]})
            storage_incidents.refresh_flow(c, inc["id"])
        return {"ok": True, "cause_text": cause or inc.get("cause_text"), "notes_recorded": bool(notes)}

    @reg.tool(
        "work_hold_set",
        "Pause all new agent work on the install for a storage incident: nothing new is claimed, "
        "running steps hand back at their next checkpoint, nothing is killed, interactive chat "
        "keeps going. The executor sets this itself at critical; use it for a warn incident "
        "whose forecast you judge too short.",
        _obj({"project_id": {"type": "string"}, "incident_id": {"type": "string"}}, ["incident_id"]),
        kind="write", reversibility="safe", permission="instance:manage", org_scope=True,
        exposure=exposure, idempotency="idempotent")
    def work_hold_set(ctx, *, project_id, actor, args):
        from sqlalchemy import text

        from ..services import storage_incidents, work_hold

        with ctx.begin() as c:
            refused = _require_operator(c)
            if refused is not None:
                return refused
            inc = storage_incidents.get(c, str(args["incident_id"]))
            if inc is None or inc["status"] == "resolved":
                return {"ok": False, "error": "no open incident by that id"}
            state = work_hold.set_hold(c, incident_id=inc["id"], host_id=str(inc["host_id"]),
                                       actor=actor or "agent:infra", project_id=inc.get("project_id"))
            c.execute(text("UPDATE storage_incidents SET hold_since = COALESCE(hold_since, now()), "
                           "hold_released_at = NULL, updated_at = now() WHERE id = :id"), {"id": inc["id"]})
            storage_incidents.refresh_flow(c, inc["id"])
        return {"ok": True, "hold": _safe(state)}

    @reg.tool(
        "work_hold_release",
        "Release the install-wide work hold once free space is back above the incident's target; "
        "paused work resumes oldest first from its checkpoint. Refused while the host is still "
        "below target — the executor releases it the moment the target is reached.",
        _obj({"project_id": {"type": "string"}, "incident_id": {"type": "string"},
              "reason": {"type": "string"}}, ["incident_id"]),
        kind="write", reversibility="safe", permission="instance:manage", org_scope=True,
        exposure=exposure, idempotency="idempotent")
    def work_hold_release(ctx, *, project_id, actor, args):
        from ..services import storage_cleanup, storage_incidents, work_hold

        with ctx.begin() as c:
            refused = _require_operator(c)
            if refused is not None:
                return refused
            inc = storage_incidents.get(c, str(args["incident_id"]))
            if inc is None:
                return {"ok": False, "error": "no such incident"}
            if not storage_cleanup.target_reached(c, inc):
                return {"ok": False, "error": "free space is still below the target; the hold stays "
                                              "until the executor reaches it"}
            out = work_hold.release(c, actor=actor or "agent:infra",
                                    reason=str(args.get("reason") or "target reached")[:300],
                                    project_id=inc.get("project_id"))
        return {"ok": True, **_safe(out)}


__all__ = ["STORAGE_READS", "STORAGE_TOOLS", "STORAGE_WRITES", "register_storage_tools"]
