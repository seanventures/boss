"""The project's Captain authority: always-on builds start without a person's grant (operator, 2026-11-07).

WHY
---
An always-on squad (the CVE / security squad, …) decides work on its own, within its class's
autonomy level, and hands it to a workflow whose build step runs as the project's Captain. Nobody
is behind that build, so under identity enforcement it was refused `NoAuthority: no_grant` and
paused on a card asking a person for a standing grant (services/person_wait). The operator's
decision: "by default let the Captain take action; expose the flag in case a user wants to review
those builds". So:

THE AUTHORITY
-------------
Every project has ONE standing authority BOSS keeps for it — a row in identity_delegation_grants
with `authority_kind = 'captain'` — created by default (at project creation; existing projects are
backfilled at boot and on first use) and SPONSORED by the project's Owner, the person accountable
for it (`user_principal_id`). It names the project's Captain and BOSS's default builder (the agents
always-on builds run as) and carries ONLY the build permissions (consent.CODE_TASK_PERMISSIONS) and
build GitHub access (push a `boss/*` branch, open a PR). It never carries merge, production deploy,
secrets or admin: those keys are not delegable at all (catalogue.NEVER_DELEGABLE), `FORBIDDEN`
below refuses them at import, and egress refuses a merge endpoint whatever a token holds. Merges
and production deploys still need a person exactly as before; the PR review gates are unchanged.

Because it is an ordinary grant, everything that guards a person's grant guards it: tokens are
minted OBO the sponsor (live ∩ the sponsor's own access ∩ the agent's ceiling), it can be revoked
in My account › Agents acting for me (a revoked one is never re-created: the project then pauses
always-on builds on the card again), and a cascade on the sponsor ends it. No person-facing path
uses it: consent, schedules and `grants.find_covering` skip it.

WHEN IT APPLIES (re-checked live at every build start, `evaluate`)
-----------------------------------------------------------------
  * the build belongs to a workflow run an always-on squad launched (its input names the ambient
    work item and the ledger decision), and no person's authority is recorded on that run;
  * the work item is still live, ambient work is not stopped, and the decision stands: a person
    approved it, or the class's autonomy level allows the action without a person (policy.gate);
  * the build runs as an agent the authority names, in a repository bound to the project;
  * "Review always-on builds before they start" (class → project → install default; off) is off,
    or a person approved this build's review card.

LINEAGE
-------
Every build that starts on it records (code_task_runs.finalize_ctx.authority, an
`agent.captain_authority` lineage event, a `code_task.captain_authority` product event and a note
on the squad's board): the acting agent, "on the project's Captain authority", the sponsor, the
triggering ambient decision and who decided it.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Mapping, Optional

from sqlalchemy import text

from . import catalogue

AUTHORITY_KIND = "captain"
#: The agents always-on builds run as: the project's Captain, and BOSS's default builder.
CAPTAIN_ROLE_KEYS: tuple[str, ...] = ("engineering_captain",)
#: The GitHub access a build needs: push its `boss/*` branch and open its PR. Never admin.
GITHUB_ACCESS = {"contents": "write", "pull_requests": "write", "checks": "read", "statuses": "read"}
#: Never on this authority, whatever a later edit says.
FORBIDDEN = frozenset(catalogue.NEVER_DELEGABLE) | frozenset({
    "deployment:production", "deployment:manage", "decision:approve", "secret:manage", "secret:read",
    "code_task:override", "code_task:merge", "pull_request:merge", "host:exec",
    "security_policy:manage", "access_role:manage", "role_binding:assign", "delegation:grant",
})
#: Words in a key that this authority never holds (merge, deploy, secrets, admin).
_FORBIDDEN_WORDS = ("merge", "deploy", "secret", "admin", "approve", "override")
#: What builds a person's consent elsewhere asks for — and all this authority ever carries.
from .consent import CODE_TASK_PERMISSIONS as _BUILD  # noqa: E402


def _allowed(key: str) -> bool:
    return (key not in FORBIDDEN and not any(w in key for w in _FORBIDDEN_WORDS)
            and catalogue.is_known(key) and catalogue.meta(key).delegable)


PERMISSIONS: frozenset[str] = frozenset(k for k in _BUILD if _allowed(k))
assert not PERMISSIONS & FORBIDDEN, "the Captain authority never holds merge, deploy, secrets or admin"

PURPOSE = "Project Captain authority: always-on builds and their PRs (never merge or deploy)"
#: How far back through retries/follow-ups BOSS looks for the build's always-on origin.
LINEAGE_LIMIT = 12

BASIS_AUTONOMY = "autonomy"
BASIS_PERSON = "person_approval"


# ---- the authority itself -----------------------------------------------------------------------------

def _org_of_project(db: Any, project_id: str) -> Optional[str]:
    return db.execute(text("SELECT w.org_id FROM projects p JOIN workspaces w ON w.id = p.workspace_id "
                           "WHERE p.id = :p"), {"p": project_id}).scalar()


def sponsor_for(db: Any, project_id: str) -> Optional[str]:
    """The project's Owner who sponsors it: an active PERSON with an Owner binding on the project,
    else the org's first active Owner person (the same default passports use). Never a service
    account or a bridge principal."""
    row = db.execute(text(
        "SELECT b.subject_id FROM identity_role_bindings b JOIN identity_principals p ON p.id = b.subject_id "
        "WHERE b.subject_type = 'principal' AND b.role_id = 'arl_owner' AND b.scope_type = 'project' "
        "AND b.scope_id = :p AND p.kind = 'user' AND p.status = 'active' AND NOT p.network_bound "
        "AND p.id NOT IN ('usr_legacy_shared', 'usr_network_operator') "
        "ORDER BY b.granted_at, b.subject_id LIMIT 1"), {"p": project_id}).scalar()
    if row:
        return str(row)
    org = _org_of_project(db, project_id)
    if not org:
        return None
    from . import users

    for pid in users.active_owner_ids(db, org):
        ok = db.execute(text(
            "SELECT 1 FROM identity_principals WHERE id = :i AND kind = 'user' AND status = 'active' "
            "AND NOT network_bound AND id NOT IN ('usr_legacy_shared', 'usr_network_operator')"),
            {"i": pid}).scalar()
        if ok:
            return str(pid)
    return None


def _agent_ids(db: Any, project_id: str) -> list[str]:
    """The Passports of the agents always-on builds run as, here (active ones only)."""
    from . import passports
    from .consent import DEFAULT_BUILDER
    from .runs import agent_for_role

    out: list[str] = []
    for role_key in (*CAPTAIN_ROLE_KEYS, DEFAULT_BUILDER):
        aid = agent_for_role(db, project_id, role_key)
        p = passports.get(aid, db=db) if aid else None
        if p is not None and p.active and aid not in out:
            out.append(aid)
    return out


def bound_repos(db: Any, project_id: str) -> list[str]:
    """The GitHub repositories an Owner or Admin bound to the project, now."""
    if not db.execute(text("SELECT to_regclass('github_repository_bindings') IS NOT NULL")).scalar():
        return []
    rows = db.execute(text(
        "SELECT full_name FROM github_repository_bindings WHERE project_id = :p "
        "AND COALESCE(status, 'active') = 'active'"), {"p": project_id}).scalars().all()
    return sorted({r for r in rows if r and r.count("/") == 1})


def _project_of(grant: Any) -> Optional[str]:
    ids = list(getattr(grant, "project_ids", ()) or ())
    return ids[0] if len(ids) == 1 else None


def require_repos_bound(db: Any, grant: Any, repos: list[str]) -> None:
    """grants.check_grant_repo_access for this authority: every repository must still be bound to
    its project (an Owner/Admin choice, read live). Raises GrantRefused."""
    from .grants import GrantRefused

    project_id = _project_of(grant)
    bound = {r.lower() for r in bound_repos(db, project_id)} if project_id else set()
    outside = sorted(r for r in repos if r.lower() not in bound)
    if not project_id or outside:
        raise GrantRefused("repo_not_bound_to_project", 403, {"repo": outside[0] if outside else ""})


def get(db: Any, project_id: str):
    """The project's Captain authority (any status), or None."""
    from . import grants

    if not _has_column(db):
        return None
    gid = db.execute(text(
        "SELECT id FROM identity_delegation_grants WHERE authority_kind = :k AND project_ids[1] = :p "
        "ORDER BY created_at LIMIT 1"), {"k": AUTHORITY_KIND, "p": project_id}).scalar()
    return grants.load(gid, db=db) if gid else None


def _has_column(db: Any) -> bool:
    return bool(db.execute(text(
        "SELECT 1 FROM information_schema.columns WHERE table_name = 'identity_delegation_grants' "
        "AND column_name = 'authority_kind'")).scalar())


def _resources(repos: list[str]) -> dict[str, Any]:
    return {"github": {"repos": sorted(set(repos)), **GITHUB_ACCESS}}


def ensure_for_project(db: Any, project_id: str):
    """The project's Captain authority, created when it has none (sponsored by its Owner), and kept
    to its definition: the agents and repositories it names follow the project's, its permissions
    never exceed PERMISSIONS. One a person revoked (or a cascade ended) stays as it is.

    None when it cannot exist yet: no Owner person, or no Captain/builder Passport here."""
    from . import grants, lineage
    from .ids import new_id

    if not _has_column(db):
        return None
    existing = get(db, project_id)
    if existing is not None:
        if existing.status.value == "active":
            _sync(db, existing, project_id)
            return grants.load(existing.id, db=db)
        return existing
    sponsor = sponsor_for(db, project_id)
    agents = _agent_ids(db, project_id)
    org = _org_of_project(db, project_id)
    if not sponsor or not agents or not org:
        return None
    gid = new_id("dlg")
    made = db.execute(text(
        "INSERT INTO identity_delegation_grants (id, org_id, user_principal_id, agent_ids, project_ids, "
        " resources, allow_subdelegation, allowed_children, max_depth, purpose, status, expires_at, "
        " authority_kind) VALUES (:id, :org, :u, :agents, :projects, CAST(:res AS JSONB), false, '{}', 1, "
        " :purpose, 'active', NULL, :kind) ON CONFLICT DO NOTHING RETURNING id"),
        {"id": gid, "org": org, "u": sponsor, "agents": agents, "projects": [project_id],
         "res": json.dumps(_resources(bound_repos(db, project_id))), "purpose": PURPOSE,
         "kind": AUTHORITY_KIND}).scalar()
    if not made:
        return get(db, project_id)                       # another creator won the race
    for key in sorted(PERMISSIONS):
        db.execute(text("INSERT INTO identity_delegation_grant_permissions (grant_id, permission_key) "
                        "VALUES (:g, :k)"), {"g": gid, "k": key})
    grant = grants.load(gid, db=db)
    lineage.grant_event(db, grant, "delegation.created", details={
        "authority": "captain", "sponsor": sponsor, "agents": agents, "projects": [project_id],
        "permissions": sorted(PERMISSIONS), "reason": "created by default for always-on builds"})
    return grant


def _sync(db: Any, grant: Any, project_id: str) -> None:
    """Keep a live authority to its definition (never wider than PERMISSIONS)."""
    agents = _agent_ids(db, project_id) or list(grant.agent_ids)
    repos = bound_repos(db, project_id)
    current_repos = sorted((grant.resources or {}).get("github", {}).get("repos") or [])
    if sorted(agents) != sorted(grant.agent_ids) or repos != current_repos:
        db.execute(text("UPDATE identity_delegation_grants SET agent_ids = :a, resources = CAST(:r AS JSONB) "
                        "WHERE id = :g"), {"a": agents, "r": json.dumps(_resources(repos)), "g": grant.id})
    extra = sorted(set(grant.permissions) - PERMISSIONS)
    if extra:
        db.execute(text("DELETE FROM identity_delegation_grant_permissions WHERE grant_id = :g "
                        "AND permission_key = ANY(:k)"), {"g": grant.id, "k": extra})


def backfill(db: Any) -> int:
    """Every live project gets its Captain authority (idempotent). Returns how many were made."""
    if not _has_column(db):
        return 0
    from ..services import projects_archive

    made = 0
    ids = db.execute(text("SELECT pr.id FROM projects pr WHERE " + projects_archive.live_sql("pr.id")
                          + " ORDER BY pr.id")).scalars().all()
    for pid in ids:
        try:
            with db.begin_nested():
                if get(db, pid) is None and ensure_for_project(db, pid) is not None:
                    made += 1
        except Exception:  # noqa: BLE001 — one project's gap never stops the others
            continue
    return made


def is_captain_grant(db: Any, grant_id: Optional[str]) -> bool:
    from . import grants

    return bool(grant_id) and grants.is_captain_authority(grants.load(grant_id, db=db))


# ---- does it cover this build? ------------------------------------------------------------------------

@dataclass
class Verdict:
    """What `evaluate` found for one build. `covered`: the authority lets this build start (or,
    with `review_pending`, would once a person approves the review card)."""
    ambient: bool = False                 # the build is an always-on squad's
    covered: bool = False
    review_required: bool = False
    review_pending: bool = False
    reason: str = ""
    project_id: Optional[str] = None
    org_id: Optional[str] = None
    grant: Any = None
    sponsor: Optional[str] = None
    agent_id: Optional[str] = None
    role_key: Optional[str] = None
    workflow_run_id: Optional[str] = None
    work_item_id: Optional[str] = None
    task_class: Optional[str] = None
    act_id: Optional[str] = None
    action: Optional[str] = None
    basis: Optional[str] = None           # autonomy | person_approval
    decided_by: Optional[str] = None
    reviewed_by: Optional[str] = None     # the person who approved the review card
    review_source: Optional[str] = None
    extra: dict[str, Any] = field(default_factory=dict)

    @property
    def grant_id(self) -> Optional[str]:
        return getattr(self.grant, "id", None)

    def lineage(self) -> dict[str, Any]:
        """The run's recorded basis (code_task_runs.finalize_ctx.authority)."""
        return {"basis": "captain_authority", "label": "on the project's Captain authority",
                "grant_id": self.grant_id, "sponsor": self.sponsor, "agent": self.agent_id,
                "role_key": self.role_key, "work_item_id": self.work_item_id,
                "decision": self.act_id, "action": self.action, "task_class": self.task_class,
                "decided_by": self.decided_by, "decision_basis": self.basis,
                "reviewed_by": self.reviewed_by, "workflow_run_id": self.workflow_run_id}


def _as_dict(value: Any) -> dict[str, Any]:
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError:
            return {}
    return value if isinstance(value, dict) else {}


def _run(db: Any, run_id: str) -> Optional[dict[str, Any]]:
    row = db.execute(text("SELECT id, project_id, spec, finalize_ctx, decision_id FROM code_task_runs "
                          "WHERE id = :i"), {"i": run_id}).mappings().first()
    return dict(row) if row else None


def _ambient_origin(db: Any, run: Mapping[str, Any]) -> Optional[dict[str, Any]]:
    """The always-on workflow run behind this build (or the build it retries/continues), as
    {workflow_run_id, work_item_id, act_id, person_authority}; None when it is not one."""
    seen: set[str] = set()
    current: Optional[Mapping[str, Any]] = run
    for _ in range(LINEAGE_LIMIT):
        if current is None or current["id"] in seen:
            return None
        seen.add(current["id"])
        fctx = _as_dict(current.get("finalize_ctx"))
        wfr = str(fctx.get("workflow_run_id") or (fctx.get("origin") or {}).get("workflow_run_id") or "")
        if wfr:
            row = db.execute(text("SELECT input, on_behalf_of, grant_id FROM workflow_runs WHERE id = :w "
                                  "AND project_id = :p"), {"w": wfr, "p": run["project_id"]}).mappings().first()
            if row is not None:
                data = _as_dict(row["input"])
                item = str(data.get("ambient_work_item_id") or "")
                if item:
                    decided = _as_dict(_as_dict(data.get("handoff")).get("decided"))
                    return {"workflow_run_id": wfr, "work_item_id": item,
                            "act_id": str(decided.get("act_id") or "") or None,
                            "person_authority": bool(row["on_behalf_of"] and row["grant_id"])}
                return None                      # a workflow a person (or a schedule) started
        nxt = str(fctx.get("parent_run_id") or fctx.get("root_run_id") or "")
        current = _run(db, nxt) if nxt else None
        if current is not None and current["project_id"] != run["project_id"]:
            return None
    return None


def _review_card(db: Any, run: Mapping[str, Any]) -> Optional[dict[str, Any]]:
    fctx = _as_dict(run.get("finalize_ctx"))
    did = (fctx.get("person_wait") or {}).get("decision_id") or run.get("decision_id")
    if not did:
        return None
    row = db.execute(text("SELECT id, status, approved_by, recommendation FROM decisions WHERE id = :d"),
                     {"d": did}).mappings().first()
    if row is None or _as_dict(row["recommendation"]).get("action") != REVIEW_ACTION:
        return None
    return dict(row)


#: The card a person answers when "Review always-on builds before they start" is on (person_wait).
REVIEW_ACTION = "review_always_on_build"
#: Actors that are BOSS answering itself, never a person's review.
_SYSTEM_ACTORS = frozenset({"auto_approval", "boss", "system", "sys_worker", ""})


def evaluate(db: Any, *, run_id: str, project_id: Optional[str] = None, ensure: bool = True) -> Verdict:
    """Does the project's Captain authority let this build start now? Everything is read live.
    `ensure=False` reads only (never creates the authority)."""
    from . import grants
    from .runs import agent_for_role, resolved_builder

    v = Verdict()
    run = _run(db, run_id)
    if run is None or (project_id and run["project_id"] != project_id):
        v.reason = "run_unknown"
        return v
    v.project_id = project_id = run["project_id"]
    origin = _ambient_origin(db, run)
    if origin is None:
        v.reason = "not_always_on"
        return v
    if origin["person_authority"]:
        v.reason = "a_person_started_it"
        return v
    v.ambient = True
    v.workflow_run_id, v.work_item_id, v.act_id = (origin["workflow_run_id"], origin["work_item_id"],
                                                   origin["act_id"])
    v.org_id = _org_of_project(db, project_id)
    item = db.execute(text("SELECT project_id, lifecycle, task_class FROM work_items WHERE id = :w"),
                      {"w": v.work_item_id}).mappings().first()
    from ..services.ambient import board, policy

    if item is None or item["project_id"] != project_id or item["lifecycle"] not in board.LIVE:
        v.reason = "work_item_ended"
        return v
    v.task_class = item["task_class"]
    if not v.act_id:
        v.reason = "no_decision"
        return v
    d = db.execute(text("SELECT project_id, work_item_id, decided_by, chosen, action FROM agent_act_decisions "
                        "WHERE act_id = :a"), {"a": v.act_id}).mappings().first()
    if (d is None or d["project_id"] != project_id or d["work_item_id"] != v.work_item_id
            or not d["action"] or d["chosen"] != d["action"]):
        v.reason = "decision_does_not_stand"
        return v
    v.action, v.decided_by = d["action"], str(d["decided_by"] or "")
    if policy.is_stopped(db, project_id):
        v.reason = "ambient_stopped"
        return v
    if v.decided_by.startswith("person:"):
        from .ambient_launch import _person_principal

        if not v.org_id or _person_principal(db, v.org_id, v.decided_by) is None:
            v.reason = "approver_is_not_an_active_person"
            return v
        v.basis = BASIS_PERSON
    else:
        verdict = policy.gate(db, project_id, v.task_class, v.action)
        if not verdict["allowed"] or verdict["needs_person"]:
            v.reason = f"autonomy:{v.task_class}"
            return v
        v.basis = BASIS_AUTONOMY
    spec, fctx = _as_dict(run.get("spec")), _as_dict(run.get("finalize_ctx"))
    v.role_key = resolved_builder(spec, fctx)
    v.agent_id = agent_for_role(db, project_id, v.role_key)
    grant = ensure_for_project(db, project_id) if ensure else get(db, project_id)
    v.grant = grant
    if grant is None or not grants.is_live(grant):
        v.reason = "captain_authority_off" if grant is not None else "no_captain_authority"
        return v
    v.sponsor = grant.user_principal_id
    if not v.agent_id or not grants.names_agent(grant, v.agent_id):
        v.reason = f"builder_not_named:{v.role_key}"
        return v
    from ..services.code_task_service import _consent_repos

    repos = _consent_repos(spec.get("owner"), spec.get("repo"), spec.get("repo_host"))
    if not grants.covers(grant, agent_ids=[v.agent_id], permissions=PERMISSIONS, project_id=project_id,
                         repos=repos, run_id=None):
        v.reason = "repo_not_bound_to_project"
        return v
    v.covered = True
    v.review_required, v.review_source = policy.review_builds_for(db, project_id, v.task_class)
    if v.review_required:
        card = _review_card(db, run)
        approved = (card is not None and card["status"] in ("approved", "executed")
                    and str(card["approved_by"] or "") not in _SYSTEM_ACTORS)
        if approved:
            v.reviewed_by = str(card["approved_by"])
        else:
            v.review_pending = True
            v.reason = "review_pending"
    return v


def review_declined(db: Any, run_id: str) -> Optional[dict[str, Any]]:
    """The review card for this build, when a person declined it."""
    run = _run(db, run_id)
    card = _review_card(db, run) if run else None
    return card if card is not None and card["status"] == "rejected" else None


def acting_for(v: Verdict, run_id: str):
    """The authority a covered build runs under: OBO the sponsor, on the Captain authority."""
    from .lineage import Acting

    return Acting(principal_id=str(v.sponsor), org_id=str(v.org_id or v.grant.org_id), grant_id=v.grant_id,
                  run_id=f"run_ct:{run_id}")


def record_use(db: Any, v: Verdict, *, run_id: str) -> bool:
    """Record, once per build and authority, that it started on the project's Captain authority:
    the run's lineage, an `agent.captain_authority` lineage event, a product event and a note on
    the squad's board. Returns False when it was already recorded."""
    from . import audit

    current = db.execute(text("SELECT finalize_ctx->'authority' FROM code_task_runs WHERE id = :r FOR UPDATE"),
                         {"r": run_id}).scalar()
    current = _as_dict(current)
    lineage_row = v.lineage()
    if current.get("basis") == "captain_authority" and current.get("grant_id") == v.grant_id \
            and current.get("reviewed_by") == v.reviewed_by:
        return False
    db.execute(text("UPDATE code_task_runs SET finalize_ctx = COALESCE(finalize_ctx, '{}'::jsonb) || "
                    "jsonb_build_object('authority', CAST(:a AS jsonb)), updated_at = now() WHERE id = :r"),
               {"a": json.dumps(lineage_row), "r": run_id})
    audit.record(audit.SecurityEvent(
        org_id=str(v.org_id or v.grant.org_id), event_type="agent.captain_authority",
        outcome=audit.Outcome.SUCCESS, stream=audit.Stream.LINEAGE, actor_principal_id=v.agent_id,
        actor_kind="agent", on_behalf_of=v.sponsor, grant_id=v.grant_id, target_type="run",
        target_id=f"run_ct:{run_id}", permission="code_task:steer", scope=f"project:{v.project_id}",
        details={k: lineage_row[k] for k in ("basis", "work_item_id", "decision", "action", "task_class",
                                             "decided_by", "decision_basis", "reviewed_by",
                                             "workflow_run_id")}), db=db)
    from ..services.core import emit_event

    emit_event(db, v.project_id, "code_task.captain_authority", "boss", "code_task_run", run_id,
               {**lineage_row, "summary": "Started on the project's Captain authority"})
    try:
        from ..services.ambient import collab

        with db.begin_nested():
            collab.post(db, v.work_item_id, kind="finding", sender="boss", body={
                "summary": "Build started on the project's Captain authority"
                           + (f" after {v.reviewed_by} reviewed it" if v.reviewed_by else "")
                           + f" (run {run_id}, decision {v.act_id})"})
    except Exception:  # noqa: BLE001 — the board note is a view; the lineage above is the record
        pass
    return True


__all__ = ["AUTHORITY_KIND", "PERMISSIONS", "FORBIDDEN", "REVIEW_ACTION", "Verdict", "ensure_for_project",
           "backfill", "evaluate", "acting_for", "record_use", "get", "sponsor_for", "require_repos_bound",
           "is_captain_grant", "review_declined", "bound_repos"]
