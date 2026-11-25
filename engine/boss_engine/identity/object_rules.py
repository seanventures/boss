"""The two object rules a declaration may add to its permit, enforced by the guard and the tool gate.

IDENTITY.md §6.3 ("when a request names an object, the scope comes from the object") and §6.2
(deployment:production). Amendment to the Phase 0 declarations (identity int-b).

  * CONFINE — an id a request names IN ITS BODY, QUERY OR TOOL ARGUMENTS (not the scope's own id)
    must belong to the project the permission was checked at. `Scope.project(body="project_id")`
    proves the caller may act in project A; without this a `task_id`, `thread_id` or
    `host_target_id` from project B rides along and the handler reads or writes B's row with A's
    permission. A confined id that names another project's object (or another org's) is answered
    404, never 403 (no existence oracle). An id that names nothing passes: there is nothing of
    anyone's to reach, and the handler answers its own not-found.

        @permit(P.WORK_ITEM_MANAGE, scope=Scope.project(body="project_id"),
                confine=(Confine.body("task_id", "task"), Confine.body("artifact_id", "artifact")))

  * PRODUCTION — an operation that ships to a target needs deployment:production as well as its own
    permission whenever the TARGET is production. The decision comes from the target's own row
    (its environment column), the deployment run's, or the build's latest deployment — and from a
    caller-supplied environment string only where the operation picks its target by environment.
    Any one source saying production is enough. The check runs at the TARGET's project.

        @permit(P.DEPLOYMENT_CREATE, scope=Scope.project(body="project_id"),
                production=Production(target="body:target_id", environment="body:environment",
                                      environment_default="staging"))

Both are framework-free: `RequestValues` carries the path, query and body (a tool call passes its
arguments as the body, read through the `args:` location).
"""
from __future__ import annotations

import inspect
from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping, Optional

from sqlalchemy import text

from .scope import RequestValues, ResolvedScope, load_object

_WHERE = frozenset({"path", "query", "body", "args"})


def _loc(raw: str) -> tuple[str, str]:
    where, sep, name = str(raw).partition(":")
    if not sep or where not in _WHERE or not name or not name.replace("_", "").isalnum():
        raise ValueError(f"a location is 'path:|query:|body:|args:<name>', got {raw!r}")
    return where, name


def read_values(values: RequestValues, where: str, name: str) -> list[str]:
    """Every non-empty value at a location (a list body field yields each item)."""
    if where == "path":
        raw: Any = values.path_params.get(name)
    elif where == "query":
        raw = values.query.get(name)
    else:   # body | args
        raw = values.body.get(name) if isinstance(values.body, Mapping) else None
    items = raw if isinstance(raw, (list, tuple)) else [raw]
    return [str(v).strip() for v in items if v is not None and str(v).strip()]


# ---- confinement -----------------------------------------------------------------------------------

@dataclass(frozen=True)
class Confine:
    """An id at `where:name` that names an object of `kind` (an object_kinds entry). `kinds` may
    name several (an argument that accepts either a deployment run or a code task, say): the id
    must belong to the scope's project as whichever kind it is."""
    where: str
    name: str
    kinds: tuple[str, ...]

    @staticmethod
    def _make(where: str, name: str, kind: str | Iterable[str]) -> "Confine":
        kinds = (kind,) if isinstance(kind, str) else tuple(kind)
        if not kinds:
            raise ValueError("Confine needs at least one object kind")
        _loc(f"{where}:{name}")
        return Confine(where, name, kinds)

    @classmethod
    def body(cls, name: str, kind: str | Iterable[str]) -> "Confine":
        return cls._make("body", name, kind)

    @classmethod
    def query(cls, name: str, kind: str | Iterable[str]) -> "Confine":
        return cls._make("query", name, kind)

    @classmethod
    def path(cls, name: str, kind: str | Iterable[str]) -> "Confine":
        return cls._make("path", name, kind)

    @classmethod
    def arg(cls, name: str, kind: str | Iterable[str]) -> "Confine":
        return cls._make("args", name, kind)

    def describe(self) -> str:
        return f"{self.where}.{self.name}->{'|'.join(self.kinds)}"


class Confined(LookupError):
    """A confined id names an object outside the request's project: answered 404."""

    def __init__(self, rule: Confine, object_id: str):
        super().__init__(f"{rule.describe()} {object_id!r} is not in this project")
        self.rule = rule
        self.object_id = object_id


def check_confined(rules: Iterable[Confine], values: RequestValues, *, org_id: str,
                   project_id: Optional[str], db: Any = None) -> None:
    """Raise Confined when any confined id names an object in another project or org.

    `project_id` None (an org-level check) confines to the org only. ONE RULE with the central
    check (`_confine_one`), org-shared kinds included: this copy of it confined a shared
    deployment host to the project that added it, so every tool declaring `host_id` (deploy_app,
    create_config_target, create_deployment_target, retarget_app …) was refused "not_found" for
    the org's own verified host from any other project (laptop 2026-11-20)."""
    for rule in rules:
        for object_id in read_values(values, rule.where, rule.name):
            _confine_one(rule, object_id, org_id=org_id, project_id=project_id, db=db)


# ---- central confinement (F2 / M9, M5): every id a request names, by its name ------------------------

#: How deep the central rule looks into a JSON body for id keys (nested body ids are confined too).
BODY_DEPTH = 8


def _id_values(raw: Any) -> list[str]:
    items = raw if isinstance(raw, (list, tuple)) else [raw]
    return [str(v).strip() for v in items
            if isinstance(v, (str, int)) and not isinstance(v, bool) and str(v).strip()]


def _walk(node: Any, depth: int = 0):
    """(depth, key, value) for every key of every mapping in a JSON body, at any nesting."""
    if depth > BODY_DEPTH:
        return
    if isinstance(node, Mapping):
        for key, value in node.items():
            yield depth, str(key), value
            if isinstance(value, (Mapping, list, tuple)):
                yield from _walk(value, depth + 1)
    elif isinstance(node, (list, tuple)):
        for item in node:
            if isinstance(item, (Mapping, list, tuple)):
                yield from _walk(item, depth + 1)


def named_ids(values: RequestValues, *, skip: Iterable[tuple[str, str]] = ()
              ) -> list[tuple[Confine, str]]:
    """Every (rule, id) the central rule confines: each path / query parameter and each body key
    (at any depth) whose NAME is an object id (object_kinds.ID_PARAM_KINDS), except the locations
    in `skip` (the scope's own id, already resolved). A rule here is built, not declared."""
    from .object_kinds import kinds_for_id

    skipped = set(skip)
    out: list[tuple[Confine, str]] = []
    for where, source in (("path", values.path_params), ("query", values.query)):
        for name, raw in (source or {}).items():
            kinds = kinds_for_id(str(name))
            if not kinds or (where, str(name)) in skipped:
                continue
            out += [(Confine(where, str(name), kinds), v) for v in _id_values(raw)]
    for depth, name, raw in _walk(values.body):
        kinds = kinds_for_id(name)
        if not kinds or (depth == 0 and ("body", name) in skipped):
            continue
        out += [(Confine("body", name, kinds), v) for v in _id_values(raw)]
    return out


def check_org_ids(values: RequestValues, *, org_id: str,
                  locations: Iterable[str] = ("path", "query", "body")) -> None:
    """M5: an `org_id` the operation READS (its declared path / query / body parameter) must be
    the caller's org. One org per install today; this holds before a second org can exist. An
    org_id the operation does not take is the handler's to refuse (A36: 422 field_not_allowed)."""
    rule = Confine("query", "org_id", ("org",))
    wanted = set(locations)
    found = [v for where, src in (("path", values.path_params), ("query", values.query))
             if where in wanted
             for k, raw in (src or {}).items() if k == "org_id" for v in _id_values(raw)]
    if "body" in wanted and isinstance(values.body, Mapping):
        found += _id_values(values.body.get("org_id"))
    for value in found:
        if value != org_id:
            raise Confined(rule, value)


def check_named(values: RequestValues, *, org_id: str, project_id: Optional[str],
                skip: Iterable[tuple[str, str]] = (), db: Any = None,
                org_locations: Iterable[str] = ("path", "query", "body")) -> None:
    """The central rule: every named id (named_ids) belongs to `project_id` (the org, when None),
    and every org_id the operation reads is the caller's org. Raises Confined (answered 404)."""
    check_org_ids(values, org_id=org_id, locations=org_locations)
    for rule, object_id in named_ids(values, skip=skip):
        _confine_one(rule, object_id, org_id=org_id, project_id=project_id, db=db)


#: Kinds that belong to the ORGANISATION, not to the project that recorded them. A verified
#: deployment host is usable by every project in its org (deployment_fleet.usable_hosts_sql), so
#: confining its id to the project that added it refused the host's own page in every other
#: project ("Can't read this machine", helpdesk-ai on boss-dogfood's host, 2026-11-05).
#: The org-owned kinds (object_kinds.OrgKind with no project: a fleet host, a registry
#: repository, a remote-agent profile, a workspace, the org-wide row of a mixed table) belong
#: here too: any project of the org may name them, another org's is not found.
ORG_SHARED_KINDS = frozenset({
    "deployment_host",
    "agent_host", "agent_host_join_token", "director_node", "factory_launch_attachment",
    "github_repo", "org_execution_environment", "org_llm_gateway", "org_skill_policy_rule", "remote_agent_profile",
    "workspace",
    # A provider sign-in (object_kinds.InProcessOrgKind) belongs to the ORG it was started for:
    # its token lands in that org's model home, and a start from any project of that org is
    # handed the live one, so the org's projects all poll it. Another org's is not found, and
    # start refuses another org's live session (409) rather than hand it over.
    "subscription_sign_in",
})


def _confine_one(rule: Confine, object_id: str, *, org_id: str, project_id: Optional[str],
                 db: Any) -> None:
    found_any = False
    accepts_org = False
    try:
        sig = inspect.signature(load_object)
        accepts_org = "org_id" in sig.parameters or any(
            p.kind == inspect.Parameter.VAR_KEYWORD for p in sig.parameters.values()
        )
    except (TypeError, ValueError):
        accepts_org = False
    for kind in rule.kinds:
        found = load_object(kind, db, object_id, org_id=org_id) if accepts_org else load_object(kind, db, object_id)
        if found is None:
            continue
        found_any = True
        shared = kind in ORG_SHARED_KINDS
        if found[0] == org_id and (project_id is None or shared or found[1] == project_id):
            return
    if found_any:
        raise Confined(rule, object_id)


# ---- production ------------------------------------------------------------------------------------

#: Environment names that mean production: the ONE resolver's (services/production.py).
from ..services.production import PRODUCTION_NAMES  # noqa: E402,F401 — re-exported
from ..services.production import is_production_environment  # noqa: E402,F401 — re-exported


@dataclass(frozen=True)
class Production:
    """Where an operation's TARGET comes from. Each is a location string `where:name`:

    target            a deployment_targets id
    deployment_run    a deployment_runs id (its environment and its target's)
    stack_deployment  a stack_deployments id (its target's environment)
    code_task         a code task run (crun_): the target of its latest deployment run
    environment       an environment name the operation picks its target by
    environment_default  the environment the operation uses when the request names none
    always            True when every call of this operation counts as production (an
                      operation that reaches a host with no environment of its own)
    resolved          a `target` counts when the one production resolver says production or
                      cannot tell (relabelling a target's tier: BOSSAPP-1441)
    """
    target: Optional[str] = None
    deployment_run: Optional[str] = None
    stack_deployment: Optional[str] = None
    code_task: Optional[str] = None
    environment: Optional[str] = None
    environment_default: Optional[str] = None
    always: bool = False
    #: True: a `target` source counts as production whenever services/production.py says so,
    #: unknown included (an unlabelled remote server), not only by its name or set tier.
    resolved: bool = False
    _sources: tuple[tuple[str, str, str], ...] = field(default=(), compare=False, repr=False)

    def __post_init__(self) -> None:
        sources = []
        for kind in ("target", "deployment_run", "stack_deployment", "code_task", "environment"):
            raw = getattr(self, kind)
            if raw:
                where, name = _loc(raw)
                sources.append((kind, where, name))
        if not sources and not self.always:
            raise ValueError("Production(...) names at least one source, or always=True")
        object.__setattr__(self, "_sources", tuple(sources))

    def describe(self) -> str:
        if self.always:
            return "production:always"
        if self.resolved:
            return "production:resolved(" + ",".join(f"{k}={w}.{n}" for k, w, n in self._sources) + ")"
        return "production(" + ",".join(f"{k}={w}.{n}" for k, w, n in self._sources) + ")"


_TARGET_SQL = text("SELECT project_id, environment, tier FROM deployment_targets WHERE id = :i")
_RUN_SQL = text("SELECT r.project_id, r.environment, t.environment, t.tier FROM deployment_runs r "
                "LEFT JOIN deployment_targets t ON t.id = r.deployment_target_id WHERE r.id = :i")
_STACK_SQL = text("SELECT s.project_id, t.environment, t.tier FROM stack_deployments s "
                  "JOIN deployment_targets t ON t.id = s.target_id WHERE s.id = :i")
#: the deployment `redeploy` re-ships: the build's latest (shepherd_service.redeploy_latest_deployment)
_CODE_TASK_SQL = text("SELECT r.project_id, r.environment, t.environment, t.tier FROM deployment_runs r "
                      "LEFT JOIN deployment_targets t ON t.id = r.deployment_target_id "
                      "WHERE r.release_packet->>'code_task_run_id' = :i "
                      "ORDER BY r.created_at DESC LIMIT 1")


def production_projects(spec: Production, values: RequestValues, *, scope: ResolvedScope,
                        db: Any = None) -> list[Optional[str]]:
    """The projects at which deployment:production must be held for this request: one per
    production source (the target's own project; None = the org, for an org-level operation),
    empty when nothing here is production."""
    if spec.always:
        return [scope.project_id]
    out: list[Optional[str]] = []
    lookups: list[tuple[Any, str]] = []
    for kind, where, name in spec._sources:
        found = read_values(values, where, name)
        if kind == "environment":
            envs = found or ([spec.environment_default] if spec.environment_default else [])
            if any(is_production_environment(e) for e in envs):
                out.append(scope.project_id)
            continue
        sql = {"target": _TARGET_SQL, "deployment_run": _RUN_SQL,
               "stack_deployment": _STACK_SQL, "code_task": _CODE_TASK_SQL}[kind]
        lookups += [(sql, object_id) for object_id in found]
    if lookups:
        from .epoch import connection

        with connection(db) as conn:
            for sql, object_id in lookups:
                row = conn.execute(sql, {"i": object_id}).first()
                if row is not None and spec.resolved and sql is _TARGET_SQL:
                    from ..services.production import resolve_target

                    if resolve_target(conn, object_id).prod:
                        out.append(str(row[0]))
                    continue
                # A target's set tier counts too (deployment_targets.tier, BOSSAPP-1441): a
                # person's "prod" holds deployment:production whatever the name says.
                if row is not None and (any(is_production_environment(e) for e in row[1:])
                                        or str(row[-1] or "").strip().lower() == "prod"):
                    out.append(str(row[0]))
    return list(dict.fromkeys(out))
