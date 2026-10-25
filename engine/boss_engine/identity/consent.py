"""Just-in-time consent (IDENTITY.md §7.3 2): starting work that needs more authority than the
person's grants cover answers `409 consent_required` with what the console's consent sheet shows.
Owned by S3.

    raise ConsentRequired  ->  409 {"error": "consent_required", "task", "task_run_id", "agents",
                                    "permissions", "projects", "github", "durations", "max_days"}

The sheet then creates a grant (POST /v1/identity/me/delegations with `kind: "task"` — bound to
`task_run_id`, a `pending:` id the next matching run CLAIMS — or `kind: "standing"`), and the
console retries the start. Only a person in a human session is ever asked; system work, legacy
callers and an install with no Passports are unaffected (`require_authority` returns None).
"""
from __future__ import annotations

import secrets
from typing import Any, Iterable, Optional

from . import catalogue
from .lineage import Acting

try:  # the API renders it as a 409 even before the lead installs the flat-body handler
    from fastapi import HTTPException as _Base
except Exception:  # noqa: BLE001 — the engine stays importable without FastAPI
    _Base = Exception  # type: ignore[assignment,misc]


class ConsentRequired(_Base):  # type: ignore[misc,valid-type]
    def __init__(self, body: dict[str, Any]):
        self.body = body
        if _Base is Exception:
            super().__init__("consent_required")
        else:
            super().__init__(status_code=409, detail=body)


GITHUB_LABELS = {"contents": "code", "pull_requests": "pull requests", "issues": "issues", "workflows": "workflows",
                 "checks": "checks", "statuses": "statuses", "actions": "actions"}
#: The GitHub access every consent sheet asks for on the run's repositories (the sheet's grant
#: body sends it back as `resources.github`, which grants.check_user_repo_access checks).
CONSENT_GITHUB_NEEDS = {"contents": "write", "pull_requests": "write"}


def _github_summary(needs: dict[str, str], pattern: str) -> str:
    parts = [f"{GITHUB_LABELS.get(k, k)} {v}" for k, v in sorted(needs.items()) if k != "metadata"]
    return ", ".join(parts) + (f" · pushes only to {pattern}" if needs.get("contents") == "write" else "")


def carry_retry_authority(db: Any, *, parent_run_id: str, new_run_id: str, project_id: Optional[str],
                          role_keys: Iterable[str], permissions: Iterable[str], repos: Iterable[str] = (),
                          by_boss: bool = False, workflow_run_id: Optional[str] = None
                          ) -> Optional[Acting]:
    """A RETRY KEEPS ITS GRANT (R2): the authority the parent build was started under (its first
    code.task job), carried to the retry as a child run of the parent's (`<parent run>/retry:<new>`),
    so a task-bound grant covers the retry exactly as it covered the parent — and nothing else.

    Only when the caller may continue it (BOSS itself — an auto-retry, the worker — or the parent's
    own person) and the grant STILL covers the retry's builder, permissions and repos; otherwise
    None and the caller asks for authority as for any new build.

    `by_boss`: BOSS ITSELF decided this retry (code_task_service.retry_code_task(automatic=True):
    the one-shot infra retry, the shepherd, an autonomous review-repair) — whoever happens to be
    calling in (the worker's own key, in production `sys_legacy_system`) is not the one asking, so
    the caller check does not apply. crun_da5f6f72 (2026-10-13) was refused `no_grant` because the
    worker's key, not a person, was the request context here. What is carried is never wider:
    * only the parent's OWN person and grant (or, for a build of a workflow run whose job carries a
      chain, that run's recorded live authority — the same person, the same consent); a parent
      stamped as BOSS's own work has no person behind it, so nothing is carried;
    * still live, still covering the builder, permissions and repos, and bound to the same run
      (`<run>/retry:<new>`);
    * never a "this build only" grant (`run_ct:`): it ends with the build it was given for."""
    from . import grants, lineage, passports, runs

    parent = runs.original_build_authority(db, parent_run_id)
    if (by_boss and workflow_run_id and parent is not None and parent.chain
            and not getattr(parent, "system", False)):
        recorded = acting_for_workflow_run(db, workflow_run_id)
        if recorded is not None and parent.principal_id == recorded.principal_id:
            parent = recorded
    if parent is None or not parent.grant_id or getattr(parent, "system", False) or parent.chain:
        return None
    ctx = lineage.current_context()
    ambient = lineage.current()
    if not by_boss:
        if ctx is not None and ctx.run_token is None and ctx.principal.id != parent.principal_id:
            return None                                       # someone else's build: their own grant
        if ctx is None and ambient is not None and not (
                ambient.system or str(ambient.principal_id).startswith("sys_")
                or ambient.principal_id == parent.principal_id):
            return None
    g = grants.load(parent.grant_id, db=db)
    if by_boss and g is not None and str(g.task_run_id or "").startswith("run_ct:"):
        return None                                           # "this build only": not past it
    if g is None or g.user_principal_id != parent.principal_id:
        return None
    agent_ids = []
    for rk in role_keys:
        aid = runs.agent_for_role(db, project_id, rk)
        if not aid or passports.get(aid, db=db) is None:
            return None
        agent_ids.append(aid)
    base = parent.run_id or f"run_ct:{parent_run_id}"
    run_id = f"{base}/retry:{new_run_id}"
    needed = frozenset(k for k in permissions if catalogue.is_known(k) and catalogue.meta(k).delegable)
    if not grants.covers(g, agent_ids=agent_ids, permissions=needed, project_id=project_id,
                         repos=list(repos), run_id=run_id):
        return None
    return Acting(principal_id=parent.principal_id, org_id=parent.org_id, grant_id=g.id, run_id=run_id)


def require_authority(*, org_id: Optional[str], project_id: Optional[str], role_keys: Iterable[str],
                      permissions: Iterable[str], repos: Iterable[str] = (), task: str, run_id: Optional[str],
                      github_needs: Optional[dict[str, str]] = None, db: Any) -> Optional[Acting]:
    """The Acting (person + covering grant) the work runs under, or ConsentRequired.

    A person in a console session gets the consent sheet. A person using a PAT, an API key or an
    MCP client, and a service account, get the same 409 when no grant of THEIR OWN covers the work
    (#5): they cannot consent mid-request, and the shared credential is never the alternative —
    they create a standing grant first. None only when there is no caller to act for (system
    work, which never acts on anyone's behalf) or the agents have no Passports yet."""
    from . import grants, lineage, runs

    ctx = lineage.current_context()
    if ctx is None or ctx.run_token is not None:
        return None                                       # system work / inside a delegated run
    if not (ctx.principal.is_person or ctx.principal.kind.value == "service_account"):
        return None
    # The same rule as a workflow run's agents (_agents): an agent with no Passport yet gets one
    # now (passports.upgrade_from_roles), and one that still has none is refused under enforce —
    # never a build that starts with no grant and dies at Build (`builder_has_no_passport`).
    role_keys = list(role_keys)
    if lineage._enforced():  # noqa: SLF001
        for rk in role_keys:
            if not runs.agent_for_role(db, project_id, rk):
                raise AgentWithoutPassport(rk)            # no such builder here: say so at start
    agents = _agents(db, project_id, role_keys)
    if not agents:
        return None                                       # identity does not apply (no agent)
    needed = frozenset(k for k in permissions if catalogue.is_known(k) and catalogue.meta(k).delegable)
    repos = list(repos)
    grant = grants.find_covering(ctx.principal.id, agent_ids=[p.agent_id for p in agents], permissions=needed,
                                 project_id=project_id, repos=repos, run_id=run_id, db=db)
    if grant is not None:
        return Acting(principal_id=ctx.principal.id, org_id=ctx.principal.org_id, grant_id=grant.id, run_id=run_id)
    pol = grants.policy_for(ctx.principal.org_id, db=db)
    needs = dict(github_needs or CONSENT_GITHUB_NEEDS)
    if not ctx.is_human_session:
        if not lineage._enforced():
            import logging

            logging.getLogger("boss.identity.consent").warning(
                "[identity] SHADOW: %s started %r with a key and no grant of its own covers it; under "
                "BOSS_AUTHZ_MODE=enforce this is refused (409 consent_required).", ctx.principal.id, task[:80])
            return None
        # No sheet to show a key: say what is missing and where to grant it. Never a task-bound
        # pending grant (that needs a person at a console).
        raise ConsentRequired(offer_link(db, ctx, {
            "error": "consent_required", "task": task[:200], "task_run_id": None,
            "agents": [{"id": p.agent_id, "name": _name(db, p.agent_id), "role_key": p.role_key} for p in agents],
            "permissions": sorted(needed), "projects": [project_id] if project_id else [],
            "github": ({"repo": repos[0], "repos": repos, "permissions": needs} if repos else None),
            "durations": [{"kind": "standing", "label": f"For {int(pol.delegation_default_days)} days",
                           "days": int(pol.delegation_default_days)}],
            "max_days": int(pol.delegation_max_days),
            "hint": "No grant of yours covers this. Open the link to give consent in the console, then "
                    "retry with the same key.",
        }))
    raise ConsentRequired(offer_link(db, ctx, {
        "error": "consent_required",
        "task": task[:200],
        "task_run_id": grants.PENDING_PREFIX + secrets.token_urlsafe(12),
        "agents": [{"id": p.agent_id, "name": _name(db, p.agent_id), "role_key": p.role_key} for p in agents],
        "permissions": sorted(needed),
        "projects": [project_id] if project_id else [],
        "github": ({"repo": repos[0], "repos": repos, "permissions": needs,
                    "summary": _github_summary(needs, "boss/*")} if repos else None),
        "durations": [{"kind": "task", "label": "For this build only", "days": None},
                      {"kind": "standing", "label": f"For {int(pol.delegation_default_days)} days",
                       "days": int(pol.delegation_default_days)}],
        "max_days": int(pol.delegation_max_days),
    }))


# ---- consent asked where no sheet can open ---------------------------------------------------------
#
# An agent turn (console chat, Metatron), an MCP call and a key-authenticated start all meet the
# same 409, and none of them can show the sheet in place. So every consent request carries a LINK
# that opens the very same sheet in the console: `consent_id` names a short-lived record of this
# body (identity_pending_consents), which only the person it was raised for may read. The link holds
# nothing but the id. Kept in its own transaction: the start that raised it is rolled back.

PENDING_CONSENT_TTL_SECONDS = 3600
GRANT_PATH = "/account/agents?consent="


def offer_link(db: Any, ctx: Any, body: dict[str, Any]) -> dict[str, Any]:
    """`body` plus `consent_id`, `grant_href` (console path) and, with a public origin, `grant_url`.
    Unchanged when the record cannot be written (the sheet in the console still works)."""
    try:
        consent_id = remember_pending(db, org_id=ctx.principal.org_id, principal_id=ctx.principal.id, body=body)
    except Exception:  # noqa: BLE001 — a missing link must never hide the consent request itself
        import logging

        logging.getLogger("boss.identity.consent").warning("[identity] pending consent not recorded", exc_info=True)
        return body
    href = GRANT_PATH + consent_id
    out = {**body, "consent_id": consent_id, "grant_href": href}
    try:
        from .authn_support import public_origin

        origin = public_origin(db, ctx.principal.org_id)
    except Exception:  # noqa: BLE001
        origin = None
    if origin:
        out["grant_url"] = origin.rstrip("/") + href
    return out


def remember_pending(db: Any, *, org_id: str, principal_id: str, body: dict[str, Any]) -> str:
    import json

    from sqlalchemy import text

    from .ids import new_id

    consent_id = new_id("pcn")
    stored = {k: v for k, v in body.items() if k not in ("consent_id", "grant_href", "grant_url")}
    engine = getattr(db, "engine", None)
    with engine.begin() as own:
        own.execute(text("DELETE FROM identity_pending_consents WHERE expires_at < now()"))
        own.execute(text(
            "INSERT INTO identity_pending_consents (id, org_id, principal_id, body, expires_at) "
            "VALUES (:id, :org, :who, CAST(:body AS JSONB), now() + make_interval(secs => :ttl))"),
            {"id": consent_id, "org": org_id, "who": principal_id, "body": json.dumps(stored, default=str),
             "ttl": PENDING_CONSENT_TTL_SECONDS})
    return consent_id


def pending_consent(db: Any, consent_id: str, *, principal_id: str) -> Optional[dict[str, Any]]:
    """The consent request behind a link, for the person it was raised for; None when it is not
    theirs, never existed or has expired (all three look the same to the caller)."""
    from sqlalchemy import text

    row = db.execute(text(
        "SELECT body FROM identity_pending_consents WHERE id = :id AND principal_id = :who "
        "AND expires_at > now()"), {"id": str(consent_id or ""), "who": principal_id}).scalar()
    if row is None:
        return None
    body = dict(row) if isinstance(row, dict) else __import__("json").loads(row)
    return {**body, "consent_id": consent_id, "grant_href": GRANT_PATH + consent_id}


def _name(db: Any, principal_id: str) -> str:
    from sqlalchemy import text

    return str(db.execute(text("SELECT display_name FROM identity_principals WHERE id = :i"),
                          {"i": principal_id}).scalar() or principal_id)


#: What a code task's builder needs (the same scope the run token is minted with).
CODE_TASK_PERMISSIONS = frozenset({"code_task:read", "code_task:steer", "repository:read", "github:read", "github:sync"})


# ---- workflow runs and schedules ---------------------------------------------------------------------
#
# A workflow run's authority is decided ONCE, at start, by the person starting it (or, for a
# schedule, by the standing grant its creator linked to it). It is recorded server-side on the run
# (`workflow_runs.on_behalf_of / grant_id / authority_org_id / authority_run_id`; rows written before
# those columns carry `policy_snapshot._identity`, read as a fallback only; a caller-sent
# `_identity` is always stripped), and every job,
# build and sub-agent the worker starts for that run carries it — re-verified at every read: the
# grant must be live, belong to the person, and (if task-bound) be bound to this run.

IDENTITY_KEY = "_identity"
WORKFLOW_BASE_PERMISSIONS = frozenset({"workflow_run:read"})
BUILD_CAPABILITIES = frozenset({"build", "retry_build", "code_task", "frontend_engineer", "backend_engineer"})
DEFAULT_BUILDER = "forge"


class AuthorityLost(Exception):
    """A schedule's (or a run's) grant is no longer live: pause, never run on stale authority."""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


def strip_identity(data: Any) -> Any:
    """Callers never set identity: drop `_identity*` keys from a client-supplied mapping."""
    if isinstance(data, dict):
        return {k: v for k, v in data.items() if not str(k).startswith(IDENTITY_KEY)}
    return data


def _multirepo_app(db: Any, project_id: str, input_data: Any) -> Optional[str]:
    """The app key of a run whose app is built from several repositories (multirepo): the
    composer's `repo_layout`, or an `app_key` whose app has several components."""
    data = input_data if isinstance(input_data, dict) else {}
    try:
        from ..multirepo import components

        layout = data.get("repo_layout") if isinstance(data.get("repo_layout"), dict) else {}
        for name in (layout.get("app_name"), data.get("app_key")):
            if name:
                key = components.app_key_of(str(name)) if name == layout.get("app_name") else str(name)
                if components.is_multi(db, project_id, key):
                    return key
    except Exception:  # noqa: BLE001 — no components: the single-repo rules below apply
        return None
    return None


def _repos_for(db: Any, project_id: str, input_data: Any) -> list[str]:
    from sqlalchemy import text

    data = input_data if isinstance(input_data, dict) else {}
    multi = _multirepo_app(db, project_id, input_data)
    if multi:
        # EXACTLY THE APP'S REPOSITORIES (multirepo/grants.py): never the project's or the org's.
        from ..multirepo import grants as repo_grants

        return sorted(r["full_name"] for r in repo_grants.required_repos(db, project_id, multi))
    for key in ("repository", "repo", "github_repo"):
        value = str(data.get(key) or "").strip()
        if value.count("/") == 1:
            return [value]
    owner, repo = str(data.get("owner") or "").strip(), str(data.get("repo") or "").strip()
    if owner and repo:
        return [f"{owner}/{repo}"]
    extends = str(data.get("extends_run_id") or "").strip()
    if extends:
        # A feature for an app that exists (the composer's "Add to an existing app", a template
        # applied to an app: templates.apply.plan) is that app's next build, in ITS repository.
        row = db.execute(text("SELECT spec->>'owner', spec->>'repo' FROM code_task_runs WHERE id = :r "
                              "AND project_id = :p"), {"r": extends, "p": project_id}).first()
        if row and row[0] and row[1]:
            return [f"{row[0]}/{row[1]}"]
    template = data.get("template") if isinstance(data.get("template"), dict) else {}
    app_key = str(data.get("app_key") or template.get("app_key") or "").strip()
    if app_key:
        # Work on an existing app (a security review's fix, a template's build) builds in THAT
        # app's repository: the grant names it, not every repository of the project.
        row = db.execute(text("SELECT spec->>'owner', spec->>'repo' FROM code_task_runs WHERE project_id = :p "
                              "AND finalize_ctx->>'app_id' = :a ORDER BY created_at DESC LIMIT 1"),
                         {"p": project_id, "a": app_key}).first()
        if row and row[0] and row[1]:
            return [f"{row[0]}/{row[1]}"]
    if db.execute(text("SELECT to_regclass('github_repository_bindings') IS NOT NULL")).scalar():
        rows = db.execute(text("SELECT full_name FROM github_repository_bindings WHERE project_id = :p "
                               "AND COALESCE(status, 'active') = 'active' ORDER BY full_name"), {"p": project_id}).scalars().all()
        return sorted({r for r in rows if r and r.count("/") == 1})
    return []


def workflow_needs(db: Any, project_id: str, spec: Any, input_data: Any) -> tuple[list[str], frozenset[str], list[str]]:
    """(agent role keys, permissions, repos) a workflow's steps will use: every named captain,
    every agent bound to a step, every reviewer seat, and the builder for build steps (which also
    brings the code-task permissions and the GitHub repo scope)."""
    roles: list[str] = []
    perms = set(WORKFLOW_BASE_PERMISSIONS)
    builds = False
    named = (input_data or {}).get("step_agents") if isinstance(input_data, dict) else None
    for step in (spec or {}).get("steps") or []:
        binding = step.get("binding") if isinstance(step.get("binding"), dict) else {}
        candidates = [step.get("captain"), binding.get("role_key"), binding.get("agent")]
        if isinstance(named, dict):
            candidates.append(named.get(step.get("id")))
        for seat_key in ("reviewers", "seats", "review_seats", "panel"):
            seats = step.get(seat_key) or binding.get(seat_key)
            if isinstance(seats, list):
                candidates.extend(s.get("role_key") if isinstance(s, dict) else s for s in seats)
        cap_key = str(binding.get("capability") or "")
        tool_perms, cap_captain = _capability_needs(cap_key)
        # What the step's own tool call AND every tool it calls in-process will be checked against.
        perms.update(tool_perms)
        if cap_captain and not any(candidates) and db is not None:
            # The step runs as its capability's captain (workflow runner "ran_as"), so that agent
            # must be in the grant — when this project has it.
            from . import runs
            if runs.agent_for_role(db, project_id, cap_captain):
                candidates.append(cap_captain)
        is_build = cap_key in BUILD_CAPABILITIES or step.get("kind") == "code_task" or _capability_builds(cap_key)
        if is_build:
            builds = True
            # The agent this step's code task will RUN AS — not its captain, not a seat: the
            # same resolution the build's run token uses (runs.resolved_builder).
            candidates.extend(build_step_builders(db, project_id, step, input_data))
        for c in candidates:
            key = str(c or "").strip().lower()
            if key and key != "human" and key not in roles:
                roles.append(key)
    if builds:
        perms |= CODE_TASK_PERMISSIONS
        # Repository work a step does OUTSIDE a delegated chain (a preview's or a deploy's clone,
        # a review's checkout run as the person: runs.repo_access) runs as the default builder.
        default = _resolved_builder()
        if default not in roles:
            roles.append(default)
    return roles, frozenset(perms), (_repos_for(db, project_id, input_data) if builds else [])


def _resolved_builder(named: Optional[str] = None) -> str:
    from . import runs

    return runs.resolved_builder(None, {"agent": {"role_key": named}} if named else None)


def build_step_builders(db: Any, project_id: Optional[str], step: Any, input_data: Any = None) -> list[str]:
    """The agent(s) a BUILD step's code task runs as, by the runtime's own rules: the step runs as
    the agent it names (step_agents.resolve, after the start card's pick: with_run_agent), the
    build tool hands the task to that agent (finalize_ctx.agent), and the run token is minted for
    runs.resolved_builder of that — the default builder when the step names nobody, and also when
    the name is only the catalogue's soft default (an off-duty default falls back to Auto, which
    runs as the person, whose build is the default builder's). The consent sheet asks for exactly
    these, so a build is never refused `grant_does_not_name_agent` after the person consented."""
    from ..services import step_agents

    as_run = step if not isinstance(step, dict) else dict(step)
    if db is not None and project_id and isinstance(step, dict):
        try:
            as_run = step_agents.with_run_agent(db, project_id, step, input_data)
        except Exception:  # noqa: BLE001 — cannot read the card's pick: the step as published
            as_run = step
    named = step_agents.named_agent(as_run) if isinstance(as_run, dict) else ""
    named = "" if named == "human" else named
    out = [_resolved_builder(named or None)]
    if not named or step_agents.is_default_captain(as_run):
        default = _resolved_builder()
        if default not in out:
            out.append(default)
    return out


_REGISTRY: list = []


def _registry(build):
    if not _REGISTRY:
        _REGISTRY.append(build())
    return _REGISTRY[0]


#: The tools that start a build: a capability that calls one IS a build step, whatever it is
#: called (security_fix was not in BUILD_CAPABILITIES, so a security review's consent asked for
#: no build authority and its fix build was refused at start: "authority refused: no_grant").
BUILD_TOOLS = frozenset({"create_code_task", "retry_code_task"})


def _capability_builds(cap_key: str) -> bool:
    if not cap_key:
        return False
    try:
        from ..services import workflow_capabilities

        cap = workflow_capabilities.CAPABILITIES.get(cap_key)
    except Exception:  # noqa: BLE001 — unknown here: the hardcoded set still decides
        return False
    if cap is None:
        return False
    return (getattr(cap, "tool", None) in BUILD_TOOLS
            or "code_task_run_id" in tuple(getattr(cap, "produces", ()) or ()))


def _capability_needs(cap_key: str) -> tuple[tuple[str, ...], Optional[str]]:
    """(the delegable permissions of every tool the capability calls, its default captain): its
    `tool` and the tools it calls in-process (`Capability.calls`: More tests' and PLRR's
    extend_code_task). The consent must ask for exactly what those calls are checked against
    (identity_gate), or the run starts and then fails at that step under enforce — More tests
    was refused `code_task:retry` on every run because its tool is None (wfr_9575c86a)."""
    if not cap_key:
        return (), None
    try:
        from ..services import workflow_capabilities
        from ..tools.builtin import build_default_registry

        cap = workflow_capabilities.CAPABILITIES.get(cap_key)
        if cap is None:
            return (), None
        specs = {t.name: t for t in _registry(build_default_registry).specs()}
        perms: list[str] = []
        for tool in capability_tools(cap):
            key = str(getattr(specs.get(tool), "permission", "") or "")
            if key and catalogue.is_known(key) and catalogue.meta(key).delegable and key not in perms:
                perms.append(key)
        return tuple(perms), (getattr(cap, "captain", None) or None)
    except Exception:  # noqa: BLE001 — unknown here; the step's own check still decides
        return (), None


def capability_tools(cap: Any) -> tuple[str, ...]:
    """Every tool a capability's step calls: its `tool` and its declared in-process `calls`."""
    tools = [str(cap.tool)] if getattr(cap, "tool", None) else []
    tools += [str(t) for t in (getattr(cap, "calls", ()) or ()) if t and str(t) not in tools]
    return tuple(tools)


class AgentWithoutPassport(Exception):
    """A person started work whose agent has no Passport and none could be made: under enforce the
    start is refused (409 `agent_without_passport`) rather than run without a delegation."""

    def __init__(self, role_key: str):
        super().__init__(role_key)
        self.role_key = role_key


def _agents(db: Any, project_id: str, role_keys: list[str]):
    """Passports for the roles. A role with an agent but no Passport yet (an agent made after the
    claim, an install that never ran the upgrade) gets one now, sponsored by the role's own sponsor
    or the org's Owner (passports.upgrade_from_roles). None only when identity does not apply
    (no agent at all for a role); under enforce an agent that still has no Passport is refused —
    work never silently runs outside delegation."""
    from . import lineage, passports, runs

    out = []
    upgraded = False
    for rk in role_keys:
        aid = runs.agent_for_role(db, project_id, rk)
        p = passports.get(aid, db=db) if aid else None
        if p is None and aid and not upgraded:
            upgraded = True
            org = _org_of_project(db, project_id)
            if org:
                # In its OWN transaction: the caller's is about to roll back on 409
                # consent_required, and the Passports must outlive it for the grant to name them.
                _upgrade_committed(db, org)
            p = passports.get(aid, db=db)
        if p is None:
            if aid and lineage._enforced():  # noqa: SLF001
                raise AgentWithoutPassport(rk)
            return None
        out.append(p)
    return out


def _upgrade_committed(db: Any, org: str) -> None:
    from . import passports

    try:
        from ..db import get_engine

        engine = get_engine()
    except Exception:  # noqa: BLE001 — no process engine (tests): the caller's transaction
        engine = None
    if engine is None or getattr(db, "engine", None) is None or db.engine.url != engine.url:
        passports.upgrade_from_roles(org_id=org, db=db, default_sponsor=_first_owner(db, org))
        return
    with engine.begin() as own:
        passports.upgrade_from_roles(org_id=org, db=own, default_sponsor=_first_owner(own, org))


def _org_of_project(db: Any, project_id: str) -> Optional[str]:
    from sqlalchemy import text

    return db.execute(text("SELECT w.org_id FROM projects p JOIN workspaces w ON w.id = p.workspace_id "
                           "WHERE p.id = :p"), {"p": project_id}).scalar()


def _first_owner(db: Any, org_id: str) -> Optional[str]:
    from . import users

    owners = users.active_owner_ids(db, org_id)
    return sorted(owners)[0] if owners else None


def _consent_body(db: Any, *, ctx_person: str, org_id: str, agents, needed, project_id, repos, task: str,
                  scope: str, standing_only: bool) -> dict[str, Any]:
    from . import grants

    pol = grants.policy_for(org_id, db=db)
    needs = dict(CONSENT_GITHUB_NEEDS)
    durations = [] if standing_only else [{"kind": "task", "label": "For this run only", "days": None, "scope": scope}]
    durations.append({"kind": "standing", "label": f"For {int(pol.delegation_default_days)} days",
                      "days": int(pol.delegation_default_days)})
    del ctx_person
    return {
        "error": "consent_required", "scope": scope, "task": task[:200],
        "task_run_id": None if standing_only else grants.PENDING_PREFIX + secrets.token_urlsafe(12),
        "agents": [{"id": p.agent_id, "name": _name(db, p.agent_id), "role_key": p.role_key} for p in agents],
        "permissions": sorted(needed), "projects": [project_id] if project_id else [],
        "github": ({"repo": repos[0], "repos": repos, "permissions": needs, "summary": _github_summary(needs, "boss/*")}
                   if repos else None),
        "durations": durations, "max_days": int(pol.delegation_max_days),
    }


def own_github_refusal(db: Any, *, org_id: str, person_id: str, project_id: str,
                       role_keys: Iterable[str], permissions: Iterable[str], repos: Iterable[str],
                       probe: bool = True, use_cache: bool = True) -> dict[str, Any]:
    """WOULD THIS PERSON'S CONSENT TO A RUN BE REFUSED FOR THEIR OWN GITHUB? The workflow
    pre-check's question (github.own_link), answered with the start door's own rules so the two
    cannot disagree: require_for_workflow asks for consent, the sheet posts a grant with
    CONSENT_GITHUB_NEEDS, and grants.check_user_repo_access refuses it github_access_lacking.

      {"state": "not_asked"}    no repository; a role with no agent (identity does not apply); or
                                a live grant of the person's already covers the run (no sheet)
      {"state": "not_checked"}  this install does not check people's own GitHub
                                (grants.repo_guard_active: one person, or the policy)
      {"state": "ok", "needs"}
      {"state": "refused", "repo", "needs", "answer", "linked"}  answer False (no link or no
                                access) or None (GitHub did not answer)
      {"state": "unprobed", "needs"}  `probe=False` and the person has a link: GitHub not asked

    Read-only: one GET per repository with the person's own token (github_app.user_repo_access);
    none with `probe=False` (a person with no link is refused without asking GitHub anyway)."""
    from sqlalchemy import text

    from . import grants, runs

    repos, role_keys = list(repos), list(role_keys)
    if not repos:
        return {"state": "not_asked"}
    agent_ids = [runs.agent_for_role(db, project_id, rk) for rk in role_keys]
    if not all(agent_ids):
        return {"state": "not_asked"}
    needed = frozenset(permissions)
    if any(grants.covers(g, agent_ids=agent_ids, permissions=needed, project_id=project_id,
                         repos=repos, run_id=None) for g in grants.for_person(person_id, db=db)):
        return {"state": "not_asked", "covered": True}
    if not grants.repo_guard_active(org_id, db=db):
        return {"state": "not_checked"}
    gh = {"repos": repos, **CONSENT_GITHUB_NEEDS}
    linked = bool(db.execute(text(
        "SELECT 1 FROM identity_vault_items WHERE owner_principal_id = :p AND kind = 'oauth_user' "
        "AND provider = 'github' AND status = 'active' LIMIT 1"), {"p": person_id}).first())
    if linked and not probe:
        return {"state": "unprobed", "needs": grants.repo_access_needed(gh)}
    refused = grants.user_repo_access_refusal(org_id, person_id, gh, db=db, use_cache=use_cache)
    if refused is None:
        return {"state": "ok", "needs": grants.repo_access_needed(gh)}
    return {"state": "refused", **refused, "linked": linked}


def require_for_workflow(db: Any, *, project_id: str, spec: Any, input_data: Any, run_id: str,
                         parent_run_id: Optional[str] = None, title: str = "") -> Optional[Acting]:
    """At workflow-run start. Returns the Acting to record on the run, None when identity does not
    apply (system start, no Passports), or raises ConsentRequired (a person) / AuthorityLost (a
    schedule's or job's grant that no longer covers)."""
    from . import grants, lineage

    wf_run = f"run_wf:{run_id}"
    ctx = lineage.current_context()
    ambient = lineage.current()
    if parent_run_id:
        # A CHILD RUN INHERITS ITS PARENT'S AUTHORITY ONLY FOR THAT AUTHORITY'S OWN PERSON (#2):
        # naming someone else's run as the parent is not a way to run under their grant. Allowed:
        # the parent's person themself, or BOSS continuing the parent (a system caller, or the
        # parent's own delegated run). Anyone else is refused before anything is written.
        recorded = recorded_authority(db, parent_run_id)
        if recorded is not None and not _may_continue(ctx, ambient, recorded, parent_run_id):
            raise ParentNotYours("parent_run_not_yours")
        parent = acting_for_workflow_run(db, parent_run_id)
        if parent is not None:
            return Acting(principal_id=parent.principal_id, org_id=parent.org_id, grant_id=parent.grant_id,
                          run_id=f"{parent.run_id}/wf:{run_id}")
    human = ctx is not None and ctx.is_human_session
    if not human and (ambient is None or not ambient.grant_id or ambient.chain):
        return None
    role_keys, needed, repos = workflow_needs(db, project_id, spec, input_data)
    agents = _agents(db, project_id, role_keys)
    if not agents:
        return None
    needed = frozenset(k for k in needed if catalogue.is_known(k) and catalogue.meta(k).delegable)
    agent_ids = [p.agent_id for p in agents]
    if human:
        grant = grants.find_covering(ctx.principal.id, agent_ids=agent_ids, permissions=needed, project_id=project_id,
                                     repos=repos, run_id=wf_run, db=db)
        if grant is None:
            body = _consent_body(db, ctx_person=ctx.principal.id, org_id=ctx.principal.org_id,
                                 agents=agents, needed=needed, project_id=project_id, repos=repos,
                                 task=title or "this workflow run", scope="workflow_run",
                                 standing_only=False)
            multi = _multirepo_app(db, project_id, input_data)
            if multi:
                # The consent sheet's multi-repo layout: which repositories, with which access,
                # which it cannot reach, and how long the access lasts.
                from ..multirepo import grants as repo_grants

                body["multirepo"] = repo_grants.consent_details(db, project_id, multi, run_label=title or None)
            raise ConsentRequired(body)
        return Acting(principal_id=ctx.principal.id, org_id=ctx.principal.org_id, grant_id=grant.id, run_id=wf_run)
    # A schedule firing (or a job) under a stored standing grant: it must still cover, standing only.
    g = grants.load(ambient.grant_id, db=db)
    if g is None or g.user_principal_id != ambient.principal_id or g.task_run_id or not grants.covers(
            g, agent_ids=agent_ids, permissions=needed, project_id=project_id, repos=repos, run_id=wf_run):
        raise AuthorityLost("grant_no_longer_covers")
    return Acting(principal_id=ambient.principal_id, org_id=ambient.org_id, grant_id=g.id, run_id=wf_run)


def require_for_retry(db: Any, *, project_id: str, run_id: str, spec: Any, input_data: Any,
                      title: Optional[str] = None) -> Optional[Acting]:
    """A person's Try again on a run whose RECORDED grant no longer covers what the run needs
    (a grant consented before the consent asked for every agent that acts — D1, 2026-10-10:
    the builder was missing and the build was refused `grant_does_not_name_agent` — or one the
    person narrowed since): ask again, exactly as at start, and record the covering grant on the
    run. Otherwise the retry would re-run the step under the same grant and be refused again.

    None (nothing to do) when the run has no recorded authority, the caller is not that run's own
    person in a console session, or the recorded grant still covers. Raises ConsentRequired with
    the same body as a start; a task grant made from it is claimed for this run (`run_wf:<id>`)."""
    from sqlalchemy import text

    from . import grants, lineage

    recorded = recorded_authority(db, run_id)
    if recorded is None or not recorded.grant_id or recorded.chain:
        return None
    ctx = lineage.current_context()
    if ctx is None or ctx.run_token is not None or not ctx.is_human_session \
            or ctx.principal.id != recorded.principal_id:
        return None
    wf_run = recorded.run_id or f"run_wf:{run_id}"
    role_keys, needed, repos = workflow_needs(db, project_id, spec, input_data)
    agents = _agents(db, project_id, role_keys)
    if not agents:
        return None
    needed = frozenset(k for k in needed if catalogue.is_known(k) and catalogue.meta(k).delegable)
    agent_ids = [p.agent_id for p in agents]
    current = grants.load(recorded.grant_id, db=db)
    if current is not None and current.user_principal_id == recorded.principal_id and grants.covers(
            current, agent_ids=agent_ids, permissions=needed, project_id=project_id, repos=repos, run_id=wf_run):
        return None
    grant = grants.find_covering(ctx.principal.id, agent_ids=agent_ids, permissions=needed, project_id=project_id,
                                 repos=repos, run_id=wf_run, db=db)
    if grant is None:
        missing = [p for p in agents if current is None or not grants.names_agent(current, p.agent_id)]
        body = _consent_body(db, ctx_person=ctx.principal.id, org_id=ctx.principal.org_id, agents=agents,
                             needed=needed, project_id=project_id, repos=repos,
                             task=title or "this workflow run", scope="workflow_run", standing_only=False)
        if missing:
            body["hint"] = ("The grant this run was started under does not let "
                            + ", ".join(_name(db, p.agent_id) for p in missing)
                            + " act, and this run needs it. Consent again to retry.")
        raise ConsentRequired(body)
    db.execute(text("UPDATE workflow_runs SET grant_id = :g, updated_at = now() WHERE id = :r"),
               {"g": grant.id, "r": run_id})
    return Acting(principal_id=recorded.principal_id, org_id=recorded.org_id, grant_id=grant.id, run_id=wf_run)


class ParentNotYours(_Base):  # type: ignore[misc,valid-type]
    """A child workflow run named a parent whose authority belongs to someone else: 403."""

    def __init__(self, reason: str):
        self.reason = reason
        if _Base is Exception:
            super().__init__(reason)
        else:
            super().__init__(status_code=403, detail={"error": "forbidden", "reason": reason})


def _may_continue(ctx: Any, ambient: Optional[Acting], recorded: Acting, parent_run_id: str) -> bool:
    """May this caller start a child of a run recorded under `recorded`?"""
    if ctx is not None and ctx.run_token is not None:
        rt = ctx.run_token
        # The parent's own delegated run (or one nested in it) continuing itself.
        return (rt.sub == recorded.principal_id and bool(rt.task.run_id)
                and str(rt.task.run_id).startswith(f"run_wf:{parent_run_id}"))
    if ctx is not None:
        return ctx.principal.id == recorded.principal_id
    if ambient is None:
        return True                                    # in-process BOSS continuation (no request)
    if ambient.system or str(ambient.principal_id).startswith("sys_"):
        return True                                    # the worker continuing the run
    return ambient.principal_id == recorded.principal_id


def record_on_run(snapshot: Any, acting: Optional[Acting]) -> dict[str, Any]:
    """The policy snapshot to INSERT: the caller's, minus any identity. The authority itself goes
    in the run's columns (`authority_columns`), never in this caller-shaped JSON."""
    del acting
    return strip_identity(dict(snapshot or {}))


def authority_columns(acting: Optional[Acting]) -> dict[str, Any]:
    """The workflow_runs identity columns for `acting` (all NULL: no authority recorded)."""
    if acting is None:
        return {"on_behalf_of": None, "grant_id": None, "authority_org_id": None, "authority_run_id": None}
    return {"on_behalf_of": acting.principal_id, "grant_id": acting.grant_id,
            "authority_org_id": acting.org_id or None, "authority_run_id": acting.run_id}


def recorded_authority(db: Any, workflow_run_id: str) -> Optional[Acting]:
    """The run's columns; `policy_snapshot._identity` only for a run recorded before them."""
    from sqlalchemy import text

    row = db.execute(text("SELECT on_behalf_of, grant_id, authority_org_id, authority_run_id, policy_snapshot "
                          "FROM workflow_runs WHERE id = :i"), {"i": workflow_run_id}).mappings().first()
    if row is None:
        return None
    if row["on_behalf_of"]:
        return Acting(principal_id=row["on_behalf_of"], org_id=row["authority_org_id"] or "",
                      grant_id=row["grant_id"], run_id=row["authority_run_id"])
    snap = row["policy_snapshot"]
    data = snap.get(IDENTITY_KEY) if isinstance(snap, dict) else None
    return Acting.from_json(data) if data else None


def acting_for_workflow_run(db: Any, workflow_run_id: Optional[str]) -> Optional[Acting]:
    """The verified principal + grant a workflow run acts under (None: none recorded, or no longer
    valid — the caller then cannot act on anyone's behalf)."""
    from sqlalchemy import text

    from . import grants

    if not workflow_run_id:
        return None
    if not db.execute(text("SELECT to_regclass('workflow_runs') IS NOT NULL")).scalar():
        return None
    acting = recorded_authority(db, workflow_run_id)
    if acting is None or not acting.grant_id:
        return None
    g = grants.load(acting.grant_id, db=db)
    if g is None or g.user_principal_id != acting.principal_id or not grants.is_live(g):
        return None
    if not (acting.run_id or "").startswith("run_wf:") or not grants.run_matches(g, acting.run_id):
        return None
    return acting


def require_for_schedule(db: Any, *, project_id: str, spec: Any, input_data: Any, name: str) -> Optional[dict[str, Any]]:
    """A schedule runs unattended, so it needs a STANDING grant from the person creating it that
    covers the workflow. Returns the link to store on the schedule (its `on_behalf_of` and
    `grant_id` columns), None when identity does not apply, or raises ConsentRequired (scope
    "schedule", standing durations only)."""
    from . import grants, lineage

    ctx = lineage.current_context()
    if ctx is None or not ctx.is_human_session:
        return None
    role_keys, needed, repos = workflow_needs(db, project_id, spec, input_data)
    agents = _agents(db, project_id, role_keys)
    if not agents:
        return None
    needed = frozenset(k for k in needed if catalogue.is_known(k) and catalogue.meta(k).delegable)
    standing = [g for g in grants.for_person(ctx.principal.id, db=db)
                if not g.task_run_id and grants.covers(g, agent_ids=[p.agent_id for p in agents], permissions=needed,
                                                       project_id=project_id, repos=repos, run_id=None)]
    if not standing:
        raise ConsentRequired(_consent_body(db, ctx_person=ctx.principal.id, org_id=ctx.principal.org_id, agents=agents,
                                            needed=needed, project_id=project_id, repos=repos,
                                            task=name or "this schedule", scope="schedule", standing_only=True))
    g = sorted(standing, key=lambda x: (x.expires_at is None, x.expires_at))[-1]     # the longest-lived
    return {"on_behalf_of": ctx.principal.id, "org_id": ctx.principal.org_id, "grant_id": g.id}


def schedule_link(schedule_row: Any) -> Optional[dict[str, Any]]:
    """The schedule's grant link: its `on_behalf_of` / `grant_id` columns, or `input._identity`
    for a schedule saved before those columns existed."""
    row = dict(schedule_row)
    if row.get("on_behalf_of"):
        return {"on_behalf_of": row["on_behalf_of"], "grant_id": row.get("grant_id")}
    data = row.get("input") if isinstance(row.get("input"), dict) else {}
    link = data.get(IDENTITY_KEY)
    return dict(link) if isinstance(link, dict) and link else None


def schedule_acting(db: Any, schedule_row: Any) -> Optional[Acting]:
    """At every firing: the schedule's linked grant must still be live, standing and its creator's.
    Raises AuthorityLost (the caller pauses the schedule: "Renew authority"). None: an unlinked
    schedule from before identity (refused under BOSS_AUTHZ_MODE=enforce)."""
    from . import grants
    from .lineage import _enforced

    link = schedule_link(schedule_row)
    if not link:
        if _enforced():
            raise AuthorityLost("schedule_has_no_grant")
        return None
    g = grants.load(link.get("grant_id"), db=db)
    if g is None or g.user_principal_id != link.get("on_behalf_of") or g.task_run_id:
        raise AuthorityLost("grant_unknown")
    if not grants.is_live(g):
        raise AuthorityLost(f"grant_{g.status.value}")
    return Acting(principal_id=g.user_principal_id, org_id=g.org_id, grant_id=g.id)
