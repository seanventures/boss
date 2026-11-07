"""Who acts when an always-on squad launches work (IDENTITY.md §6.4 / §7.3, BOSSAPP-908).

A squad launches through the same tools any agent calls (`start_workflow`, `request_meeting`).
It used to call them with no identity of its own, so the registry decided the call as whoever
happened to be on the thread — the worker's `sys_worker` on the ambient tick (which holds only
worker:lease / spend:reserve / secret:lease, so every launch came back `forbidden`), or nobody at
all (`identity_unavailable`). A person's approval could not get a squad's PR opened.

The acting identity is now explicit and verified:

    the agent      the squad lead's Passport (`roles.agent_id` for its role key)
    for whom       the person who approved the card (`on_behalf_of`, the decision's act id as
                   lineage) — or nobody, when the project's autonomy level for the item's class
                   let the squad act on its own (A2+ with the squad's own consensus)

`launch_context` builds that AuthContext from the ledger; `decide` is what authz.check runs for
it, re-reading everything live (the item, the decision, the Passport, the person, the class
level), so the registry verifies it rather than trusting it. Identity enables ONLY what the
class level or the person's approval already allows, and only the two launch permissions:

    * a key other than workflow_run:start / meeting:convene         -> refused
    * a project other than the work item's                            -> refused (out_of_scope)
    * a person's approval: that person must hold the key there, live -> else refused
    * autonomy: the class level must allow the action without a person, now, and the Passport's
      sponsor (the person accountable for the agent) must hold the key there, live -> else refused
    * a stopped project, an ended item, a held decision, a suspended Passport -> refused

Per-rule-only workflows and every other policy refusal stay where they are (event_dispatch,
the start's readiness preflight): this only says who is calling.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional

from sqlalchemy import text

from .principal import AuthContext, AuthMethod, PrincipalKind

#: The only keys an ambient launch may use: start the decided workflow, convene the panel.
LAUNCH_PERMISSIONS: frozenset[str] = frozenset({"workflow_run:start", "meeting:convene"})
#: An item whose squad may still launch.
LIVE_ITEM = ("claimed", "in_progress")

PERSON_APPROVAL = "person_approval"
AUTONOMY = "autonomy"


@dataclass(frozen=True)
class AmbientAuthority:
    """What an ambient launch stands on. Built from the ledger, re-checked at every decision."""
    project_id: str
    work_item_id: str
    act_id: str
    agent_id: str
    role_key: str
    basis: str                          # PERSON_APPROVAL | AUTONOMY
    on_behalf_of: Optional[str] = None  # the approving person's principal (PERSON_APPROVAL)
    #: The launch's own transaction (it holds the decision the launch stands on); authz.check
    #: decides in it. Never serialised.
    connection: Any = field(default=None, compare=False, repr=False)

    def to_json(self) -> dict[str, Any]:
        return {"project_id": self.project_id, "work_item_id": self.work_item_id,
                "decision": self.act_id, "agent": self.agent_id, "role_key": self.role_key,
                "basis": self.basis, "on_behalf_of": self.on_behalf_of}


class LaunchRefused(Exception):
    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


def _org_of_project(db: Any, project_id: str) -> Optional[str]:
    return db.execute(text("SELECT w.org_id FROM projects p JOIN workspaces w ON w.id = p.workspace_id "
                           "WHERE p.id = :p"), {"p": project_id}).scalar()


def _decision(db: Any, act_id: str) -> Optional[dict[str, Any]]:
    row = db.execute(text(
        "SELECT act_id, project_id, work_item_id, decided_by, chosen, action "
        "FROM agent_act_decisions WHERE act_id = :a"), {"a": act_id}).mappings().first()
    return dict(row) if row else None


def _person_principal(db: Any, org_id: str, decided_by: str) -> Optional[str]:
    """'person:ceo' / 'person:usr_x' -> the active user principal it is (legacy member ids map
    through identity_legacy_mappings), else None."""
    from .legacy import resolve_actor

    actor = decided_by.split(":", 1)[1] if decided_by.startswith("person:") else ""
    found = resolve_actor(org_id, actor, db=db) if actor else None
    if not found or found["kind"] != PrincipalKind.USER.value or found["status"] != "active":
        return None
    return str(found["principal_id"])


def launch_context(db: Any, *, project_id: str, work_item_id: str, act_id: Optional[str],
                   role_key: str) -> AuthContext:
    """The verified identity a squad launch runs as. Raises LaunchRefused (with a reason a person
    can read in the squad's memory) when there is nobody it could be."""
    from . import users
    from .runs import agent_for_role

    if not act_id:
        raise LaunchRefused("no_decision: a launch needs the decision that allowed it")
    org = _org_of_project(db, project_id)
    if not org:
        raise LaunchRefused("project_unknown")
    agent_id = agent_for_role(db, project_id, role_key)
    if not agent_id:
        raise LaunchRefused(f"agent_without_passport:{role_key}")
    principal = users.load_principal(db, agent_id)
    if principal is None or principal.kind != PrincipalKind.AGENT or principal.org_id != org:
        raise LaunchRefused(f"agent_without_passport:{role_key}")
    d = _decision(db, act_id)
    if d is None or d["project_id"] != project_id or d["work_item_id"] != work_item_id:
        raise LaunchRefused("decision_not_for_this_item")
    decided_by = str(d["decided_by"] or "")
    if decided_by.startswith("person:"):
        person = _person_principal(db, org, decided_by)
        if person is None:
            raise LaunchRefused("approver_is_not_an_active_person")
        authority = AmbientAuthority(project_id, work_item_id, act_id, agent_id, role_key,
                                     PERSON_APPROVAL, person, connection=db)
    else:
        authority = AmbientAuthority(project_id, work_item_id, act_id, agent_id, role_key, AUTONOMY,
                                     connection=db)
    return AuthContext(principal=principal, method=AuthMethod.SYSTEM, ambient=authority)


def decide(ctx: AuthContext, permission: str, scope: Any, conn: Any):
    """authz.check for an ambient launch context: every fact re-read now (deny by default)."""
    from . import grants, passports
    from .authz import Decision, DenyReason, Why

    a: AmbientAuthority = ctx.ambient
    deny = Decision.deny

    def refused(why: str, reason: DenyReason = DenyReason.MISSING_PERMISSION):
        return deny(permission, scope, reason, (why,))

    if permission not in LAUNCH_PERMISSIONS:
        return refused("ambient_launch")
    if getattr(scope, "project_id", None) != a.project_id:
        return refused("work_item", DenyReason.OUT_OF_SCOPE)
    agent_status = conn.execute(text(
        "SELECT status FROM identity_principals WHERE id = :a AND org_id = :o"),
        {"a": ctx.principal.id, "o": ctx.principal.org_id}).scalar()
    if agent_status != "active":
        return refused(f"agent:{ctx.principal.id}", DenyReason.PRINCIPAL_INACTIVE)
    p = passports.get(a.agent_id, db=conn)
    if p is None or not p.active or p.org_id != ctx.principal.org_id or p.agent_id != ctx.principal.id:
        return refused(f"passport:{a.agent_id}", DenyReason.CHAIN_INACTIVE)
    item = conn.execute(text(
        "SELECT project_id, lifecycle, task_class FROM work_items WHERE id = :w"),
        {"w": a.work_item_id}).mappings().first()
    if item is None or item["project_id"] != a.project_id or item["lifecycle"] not in LIVE_ITEM:
        return refused("work_item", DenyReason.OUT_OF_SCOPE)
    d = _decision(conn, a.act_id)
    if (d is None or d["project_id"] != a.project_id or d["work_item_id"] != a.work_item_id
            or not d["action"] or d["chosen"] != d["action"]):
        return refused(f"decision:{a.act_id}")
    from ..services.ambient import policy

    if policy.is_stopped(conn, a.project_id):
        return refused("ambient_stopped")
    decided_by = str(d["decided_by"] or "")
    if a.basis == PERSON_APPROVAL:
        if not a.on_behalf_of or _person_principal(conn, ctx.principal.org_id, decided_by) != a.on_behalf_of:
            return refused(f"decision:{a.act_id}")
        holder = grants.principal(conn, a.on_behalf_of)
        label = f"person:{a.on_behalf_of}"
    elif a.basis == AUTONOMY:
        if decided_by.startswith("person:"):
            return refused(f"decision:{a.act_id}")
        verdict = policy.gate(conn, a.project_id, item["task_class"], d["action"])
        if not verdict["allowed"] or verdict["needs_person"]:
            return refused(f"autonomy:{item['task_class']}")
        holder = grants.principal(conn, p.sponsor_principal_id)
        label = f"sponsor:{p.sponsor_principal_id}"
    else:
        return refused("ambient_basis")
    if holder is None or not holder.active or holder.org_id != ctx.principal.org_id \
            or permission not in grants.person_live(conn, holder, a.project_id):
        return refused(label)
    return Decision.allow(permission, scope, (Why(role_key=f"ambient:{a.basis}", binding_id=a.act_id,
                                                  binding_scope=f"project:{a.project_id}"),))


def record_launch(ctx: AuthContext, *, tool: str, outcome_ok: bool, ref: Optional[str] = None,
                  error: Optional[str] = None, db: Any) -> None:
    """The audit row for one ambient launch: the agent as actor, the person it acted for, the
    decision it stood on. Written in the caller's transaction (it fails with it)."""
    from . import audit

    a: AmbientAuthority = ctx.ambient
    details = {**a.to_json(), "tool": tool}
    if ref:
        details["ref"] = ref
    if error:
        details["error"] = str(error)[:200]
    audit.record(audit.SecurityEvent.from_context(
        ctx, "agent.ambient_launch",
        audit.Outcome.SUCCESS if outcome_ok else audit.Outcome.DENIED,
        stream=audit.Stream.LINEAGE, target_type="work_item", target_id=a.work_item_id,
        permission="workflow_run:start" if tool == "start_workflow" else "meeting:convene",
        scope=f"project:{a.project_id}", details=details), db=db)


__all__ = ["AmbientAuthority", "LaunchRefused", "LAUNCH_PERMISSIONS", "PERSON_APPROVAL", "AUTONOMY",
           "launch_context", "decide", "record_launch"]
