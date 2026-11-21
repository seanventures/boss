"""AGENTS FIX FAILURES WITH THE STARTER'S AUTHORITY — the org default (operator, 2026-11-21).

THE OPERATOR'S WORDS. "the main issue was Agents were not acting autonomously to fix the issue -
and you are saying permission problem - for fixing the issue, let's grant by default for all
logged in users to fix these failures, so Agents can automatically get the power of the user ..
we cannot hide behind the security stuff of not doing the job - please fix the Agent being
autonomously fixing the failure or if it's blocked worst case it should provide the guidance for
humans to fix the issue".

THE CASES (laptop, 2026-11-21). A merge conflict on northgate-gateway #9 was not handed to the
build agent because the build ran under its workflow's "this run only" grant, which had ended
with the run (consent.carry_retry_authority: "grant … is expired"), so the card went back to the
person. Two builds (invoice-desk, recipe-box) sat on "no person's grant covers another attempt",
and two step-recovery agents (Helm, Pilot) asked a person to "re-authorize" work they had
diagnosed. Every one of those was the SAME person's own work, failing, with an agent ready to fix
it.

THE AUTHORITY. When a signed-in person starts work (or owns the project a lineage with no person
belongs to), BOSS's recovery and fixing agents may continue THAT work with that person's own
DELEGABLE authority, without asking again. It is an ordinary grant row in
identity_delegation_grants with `authority_kind = 'fixer'` — one per (person, project) — created
on first need and widened only to what a recovery of that person's work needs:

  * never wider than the person's live permissions at the project ∩ the named agents' ceilings
    (checked here, and re-intersected live at every token mint, like any grant);
  * never a non-delegable key, and never the keys below (FORBIDDEN): approving a decision,
    production deploy, merge, secrets, host:exec, admin — those are a person's act or an org
    rule, and IDENTITY.md §7.13 keeps them out of every agent's reach;
  * only inside the person's own org (the project's org must be the person's);
  * never re-created after a person REVOKED it (My account › Agents acting for me): a revoked
    fixer grant for a project is that person's "no" for that project; an expired one is renewed;
  * recorded: `delegation.created` (authority "fixer", org default) when made and
    `delegation.org_default_applied` every time it is widened or a run is moved onto it.

THE SWITCH. Default ON for every org and project, and ON for existing installs on upgrade (a
missing policy key reads as the default). Off per org: Identity › Policy
`agents_fix_failures = false`; off per project: the project setting `agents_fix_failures = 0`,
drawn in the console at Govern › More › Advanced limits › Each failed step.
Off means the old behaviour: the work stops and a person's card asks for a grant.

HARD LIMITS (things an agent literally cannot or must not do, kept whatever this says): typing
a password, a token or a one-time code; a production deploy's approval when the org requires
one; spending above the org's budget (the budget gate is unchanged and still refuses); acting
in another org.
"""
from __future__ import annotations

import json
import logging
from typing import Any, Iterable, Optional

from sqlalchemy import text

from . import catalogue
from .captain_authority import FORBIDDEN as _CAPTAIN_FORBIDDEN

log = logging.getLogger("boss.identity.fix_authority")

AUTHORITY_KIND = "fixer"
POLICY_KEY = "agents_fix_failures"
KNOB = "agents_fix_failures"
PURPOSE = "Agents fix failures with the starter's authority (org default)"
ORG_DEFAULT_EVENT = "delegation.org_default_applied"
#: Never on this authority, whatever a caller asks for.
FORBIDDEN = frozenset(_CAPTAIN_FORBIDDEN) | frozenset({"deployment:production"})
_FORBIDDEN_WORDS = ("merge", "secret", "admin", "approve", "override", "production")
#: The GitHub access a fix needs: push its `boss/*` branch, update its PR. Never admin.
GITHUB_ACCESS = {"contents": "write", "pull_requests": "write", "checks": "read", "statuses": "read"}
#: The hard limits, said once (docs, the inventory, the guard test).
HARD_LIMITS = (
    "typing a password, token or one-time code",
    "approving a production deploy when the org requires a person's approval",
    "spending above the org's budget",
    "acting in another org",
)


def allowed(key: str) -> bool:
    """May this authority ever carry `key`?"""
    return (key not in FORBIDDEN and not any(w in key for w in _FORBIDDEN_WORDS)
            and catalogue.is_known(key) and catalogue.meta(key).delegable)


def may_rerun_job(kind: Any) -> tuple[bool, str]:
    """May BOSS re-run a dead job of `kind` FOR ITS PERSON? Only when the permission that kind
    needs is one this authority may ever carry, and a duplicate of it is not IRREVERSIBLE: a
    merge (github:merge, change_set.merge), a sent message, a deleted server — never re-run by
    an agent. An unknown kind is refused (fail closed)."""
    from ..services.durable_operations import IRREVERSIBLE, side_effect_class
    from .job_permissions import permission_for

    kind = str(kind or "")
    perm = permission_for(kind)
    if perm is None:
        return False, f"BOSS does not know what a '{kind}' job may do, so it is not re-run for a person"
    if side_effect_class(kind) == IRREVERSIBLE:
        return False, f"a '{kind}' job cannot be undone, so only its person runs it again"
    if not allowed(perm):
        return False, f"a '{kind}' job needs {perm}, which an agent never holds"
    return True, ""


def _has_column(db: Any) -> bool:
    return bool(db.execute(text(
        "SELECT 1 FROM information_schema.columns WHERE table_name = 'identity_delegation_grants' "
        "AND column_name = 'authority_kind'")).scalar())


def _org_of_project(db: Any, project_id: str) -> Optional[str]:
    return db.execute(text("SELECT w.org_id FROM projects p JOIN workspaces w ON w.id = p.workspace_id "
                           "WHERE p.id = :p"), {"p": project_id}).scalar()


def org_allows(db: Any, project_id: str) -> bool:
    """The org half of `enabled`: False when the project's org turned the switch off in its
    Identity policy. Govern draws the project row Off (and not editable) when this is False, so
    the board never says On while enabled() refuses. Raises when the policy cannot be read."""
    org = _org_of_project(db, project_id)
    if not org:
        return True
    from .policy import get_policy

    return bool(getattr(get_policy(org, db=db), POLICY_KEY, True))


def enabled(db: Any, project_id: Optional[str], *, org_id: Optional[str] = None,
            settings: Any = None) -> bool:
    """The org's policy AND the project's setting. ON when the key is simply absent (the
    default, and every existing install on upgrade); an UNREADABLE or erroring read is logged
    and treated as OFF — authority is never assumed from a read that failed. Read at every use
    of a fixer grant (grants.usable), so turning it off stops work already standing on one."""
    if not project_id or db is None:
        return False
    try:
        with db.begin_nested():
            org = org_id or _org_of_project(db, project_id)
            if not org:
                log.warning("fix authority: project %s has no org; treated as off", project_id)
                return False
            from .policy import get_policy

            if not bool(getattr(get_policy(org, db=db), POLICY_KEY, True)):
                return False
            row = db.execute(text("SELECT value FROM project_settings WHERE project_id = :p "
                                  "AND key = :k"), {"p": project_id, "k": KNOB}).first()
    except Exception:  # noqa: BLE001 — unreadable: OFF, said in the log
        log.warning("fix authority: the agents_fix_failures switch could not be read for %s; "
                    "treated as off", project_id, exc_info=True)
        return False
    if row is None:
        if settings is None:
            from ..config import get_settings

            settings = get_settings()
        value = getattr(settings, KNOB, 1)
    else:
        value = row[0]
    return str(1 if value in (None, "") else value).strip().lower() not in ("0", "false", "off")


def get(db: Any, person_id: str, project_id: str):
    """The person's fixer authority for the project (newest, any status), or None."""
    from . import grants

    if not _has_column(db):
        return None
    gid = db.execute(text(
        "SELECT id FROM identity_delegation_grants WHERE authority_kind = :k AND user_principal_id = :u "
        "AND project_ids[1] = :p ORDER BY created_at DESC LIMIT 1"),
        {"k": AUTHORITY_KIND, "u": person_id, "p": project_id}).scalar()
    return grants.load(gid, db=db) if gid else None


def _person(db: Any, person_id: str):
    from . import grants

    p = grants.principal(db, person_id)
    if p is None or not p.active or not p.is_person:
        return None
    if str(person_id).startswith("sys_") or person_id in ("usr_legacy_shared", "usr_network_operator"):
        return None
    return p


def _resources(db: Any, project_id: str, repos: Iterable[str], base: Any = None) -> dict[str, Any]:
    from . import grants

    names = {str(r) for r in repos if r}
    names |= set(grants.grant_repos(base)) if base is not None else set()
    github, local = grants.split_repos(sorted(names))
    out: dict[str, Any] = {}
    if github:
        out["github"] = {"repos": sorted(set(github)), **GITHUB_ACCESS}
    local = [r for r in local if grants.local_repo_registered(db, project_id, r)]
    if local:
        out["local"] = {"repos": sorted(set(local))}
    return out


def ensure(db: Any, *, person_id: Optional[str], project_id: Optional[str],
           agent_ids: Iterable[str], permissions: Iterable[str], repos: Iterable[str] = (),
           why: str = "", ref: Optional[str] = None, settings: Any = None,
           gap: Optional[list[str]] = None):
    """The person's fixer authority covering this recovery — made or widened now — or None, with
    the reason appended to `gap`. Nothing here is wider than the person ∩ the agents' ceilings."""
    from . import grants, lineage, passports
    from .ids import new_id

    def no(reason: str):
        if gap is not None:
            gap.append(reason)
        return None

    if not person_id or not project_id:
        return no("no person started this work and the project has no Owner to act for")
    if not _has_column(db):
        return no("this install has no fixer authority yet")
    org = _org_of_project(db, project_id)
    if not enabled(db, project_id, org_id=org, settings=settings):
        return no("agents fixing failures with the starter's authority is turned off for this "
                  "project (Govern › More › Advanced limits › Each failed step › Agents fix "
                  "failures with your authority, or the org's Identity policy)")
    person = _person(db, person_id)
    if person is None:
        return no(f"{person_id} is not an active person, so there is no one's authority to use")
    if not org or person.org_id != org:
        return no("the work is in another org than its person's; BOSS never acts across orgs")
    existing = get(db, person_id, project_id)
    if existing is not None and getattr(existing.status, "value", existing.status) in ("revoked", "suspended"):
        return no(f"{person_id} revoked the fixer authority for this project (grant {existing.id}); "
                  "BOSS does not re-create it")
    agents: list[str] = []
    ceilings = []
    for aid in agent_ids:
        p = passports.get(aid, db=db) if aid else None
        if p is None or not p.active or p.org_id != org:
            return no(f"agent {aid} has no active Passport in this org")
        if aid not in agents:
            agents.append(aid)
            ceilings.append(p.ceiling)
    if not agents:
        return no("no agent with a Passport would do the fix")
    # A KEY THE PERSON REMOVED from their fixer authority (narrow_grant) is never re-granted.
    withheld = frozenset(((getattr(existing, "resources", None) or {}).get("fixer") or {})
                         .get("withheld") or ()) if existing is not None else frozenset()
    live = grants.person_live(db, person, project_id)
    union = frozenset().union(*ceilings)
    wanted = frozenset(k for k in permissions if catalogue.is_known(k) and catalogue.meta(k).delegable)
    usable = frozenset(k for k in wanted if allowed(k) and k in live and k in union)
    removed = sorted(wanted & withheld)
    if removed:
        return no(f"{person_id} removed {', '.join(removed)} from their fixer authority; BOSS "
                  "does not give it back")
    missing = sorted(wanted - usable)
    if missing:
        return no(f"the fix needs {', '.join(missing)}, which {person_id} cannot delegate here "
                  "(not theirs, outside the agents' ceilings, or never an agent's)")
    repos = [str(r) for r in repos if r]
    base = existing if existing is not None and grants.is_live(existing) else None
    if base is not None and grants.covers(base, agent_ids=agents, permissions=usable,
                                          project_id=project_id, repos=repos, run_id=None):
        return base
    resources = _resources(db, project_id, repos, base)
    if withheld:
        resources["fixer"] = {"withheld": sorted(withheld)}
    gh = resources.get("github") or {}
    if gh and grants.repo_guard_active(org, db=db):
        try:
            grants.check_user_repo_access(org, person_id, gh, db=db)
        except grants.GrantRefused as refused:
            return no(f"{person_id} cannot reach the repository on GitHub ({refused})")
    from .policy import get_policy

    pol = get_policy(org, db=db)
    days = max(1, min(int(pol.delegation_default_days or 30), int(pol.delegation_max_days or 90), 90))
    if base is not None:
        all_agents = sorted(set(base.agent_ids) | set(agents))
        add = sorted(usable - base.permissions)
        db.execute(text(
            "UPDATE identity_delegation_grants SET agent_ids = :a, resources = CAST(:r AS JSONB), "
            "expires_at = GREATEST(expires_at, now() + make_interval(days => :d)) WHERE id = :g"),
            {"a": all_agents, "r": json.dumps(resources), "d": days, "g": base.id})
        for key in add:
            db.execute(text("INSERT INTO identity_delegation_grant_permissions (grant_id, permission_key) "
                            "VALUES (:g, :k) ON CONFLICT DO NOTHING"), {"g": base.id, "k": key})
        grant = grants.load(base.id, db=db)
        lineage.grant_event(db, grant, ORG_DEFAULT_EVENT, details={
            "authority": AUTHORITY_KIND, "org_default": POLICY_KEY, "change": "widened",
            "added_permissions": add, "agents": all_agents, "why": why[:300], "for": ref,
            "reason": PURPOSE})
        return grant
    gid = new_id("dlg")
    db.execute(text(
        "INSERT INTO identity_delegation_grants (id, org_id, user_principal_id, agent_ids, project_ids, "
        " resources, allow_subdelegation, allowed_children, max_depth, purpose, status, expires_at, "
        " authority_kind) VALUES (:id, :org, :u, :agents, :projects, CAST(:res AS JSONB), false, '{}', 1, "
        " :purpose, 'active', now() + make_interval(days => :d), :kind)"),
        {"id": gid, "org": org, "u": person_id, "agents": agents, "projects": [project_id],
         "res": json.dumps(resources), "purpose": PURPOSE, "d": days, "kind": AUTHORITY_KIND})
    for key in sorted(usable):
        db.execute(text("INSERT INTO identity_delegation_grant_permissions (grant_id, permission_key) "
                        "VALUES (:g, :k)"), {"g": gid, "k": key})
    grant = grants.load(gid, db=db)
    lineage.grant_event(db, grant, "delegation.created", details={
        "authority": AUTHORITY_KIND, "org_default": POLICY_KEY, "agents": agents,
        "projects": [project_id], "permissions": sorted(usable), "why": why[:300], "for": ref,
        "reason": PURPOSE})
    return grant


def owner_of(db: Any, project_id: str) -> Optional[str]:
    """The project's Owner person (a lineage with no person behind it acts for its project)."""
    from .captain_authority import sponsor_for

    try:
        return sponsor_for(db, project_id)
    except Exception:  # noqa: BLE001
        return None


def renew_run(db: Any, *, project_id: str, run_id: str, why: str = "",
              gap: Optional[list[str]] = None):
    """A workflow run whose recorded grant ended (or no longer covers it): move it onto its
    person's fixer authority and return the run's Acting, or None (with the reason in `gap`).
    A run a person cancelled, or whose person revoked the authority, is never renewed."""
    from . import consent, grants
    from .lineage import Acting

    recorded = consent.recorded_authority(db, run_id)
    if recorded is None or not recorded.grant_id or recorded.chain:
        return None
    live = consent.acting_for_workflow_run(db, run_id)
    if live is not None:
        return live
    current = grants.load(recorded.grant_id, db=db)
    origin_id = db.execute(text("SELECT policy_snapshot->>'fixer_from' FROM workflow_runs "
                                "WHERE id = :r"), {"r": run_id}).scalar()
    origin = grants.load(origin_id, db=db) if origin_id else current
    for g in (current, origin):
        if g is not None and getattr(g.status, "value", g.status) in ("revoked", "suspended"):
            if gap is not None:
                gap.append(f"grant {g.id} was revoked by a person; BOSS does not act on that run")
            return None
    row = db.execute(text("SELECT status, input, workflow_version_id FROM workflow_runs WHERE id = :r"),
                     {"r": run_id}).mappings().first()
    if row is None or str(row["status"]) in ("cancelled", "completed"):
        return None
    spec = db.execute(text("SELECT spec FROM workflow_versions WHERE id = :v"),
                      {"v": row["workflow_version_id"]}).scalar()
    spec = json.loads(spec) if isinstance(spec, str) else (spec or {})
    inp = row["input"] if isinstance(row["input"], dict) else json.loads(row["input"] or "{}")
    role_keys, needed, repos = consent.workflow_needs(db, project_id, spec, inp)
    agents = consent._agents(db, project_id, role_keys)  # noqa: SLF001 — the one resolver
    grant = ensure(db, person_id=recorded.principal_id, project_id=project_id,
                   agent_ids=[p.agent_id for p in agents or []], permissions=needed, repos=repos,
                   why=why or "a recovery agent continues this workflow run", ref=f"run_wf:{run_id}",
                   gap=gap)
    if grant is None:
        return None
    # The run remembers the grant it was STARTED under (`fixer_from`): revoking that one ends
    # this run's fixer authority too (consent.acting_for_workflow_run).
    db.execute(text(
        "UPDATE workflow_runs SET grant_id = :g, updated_at = now(), policy_snapshot = "
        "COALESCE(policy_snapshot, '{}'::jsonb) || jsonb_build_object('fixer_from', "
        "COALESCE(policy_snapshot->>'fixer_from', CAST(:o AS text))) WHERE id = :r"),
        {"g": grant.id, "r": run_id, "o": origin_id or recorded.grant_id})
    from . import lineage

    lineage.grant_event(db, grant, ORG_DEFAULT_EVENT, details={
        "authority": AUTHORITY_KIND, "org_default": POLICY_KEY, "change": "run_moved",
        "run": f"run_wf:{run_id}", "from_grant": recorded.grant_id, "why": (why or "")[:300],
        "reason": PURPOSE})
    return Acting(principal_id=recorded.principal_id, org_id=recorded.org_id, grant_id=grant.id,
                  run_id=recorded.run_id or f"run_wf:{run_id}")


def is_fixer(grant: Any) -> bool:
    return grant is not None and getattr(grant, "authority_kind", None) == AUTHORITY_KIND


__all__ = ["AUTHORITY_KIND", "FORBIDDEN", "HARD_LIMITS", "KNOB", "ORG_DEFAULT_EVENT", "POLICY_KEY",
           "PURPOSE", "allowed", "enabled", "ensure", "get", "is_fixer", "owner_of", "renew_run"]
