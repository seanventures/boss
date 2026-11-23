"""THE ONE ANSWER TO "IS THIS PRODUCTION?" (BOSSAPP-1441, dev autonomy).

Nine places used to answer it nine ways: identity's name list, the platform policy's narrower
one, `environment == "production"` on ChangeSets, three private lists (`prod|production|live`),
the incident squad's inverse rule (anything not on its non-prod list was production), a regex on
a question's words, and deploy policy's "no row means ask, staging too". A target called `prod`
was production to one reader and not to the next, and BOSS could not tell a laptop from a
customer's server at all. Every reader now asks here.

THE RULES, in order (`Verdict.source` says which one answered):

  1. A tier someone SET wins: `deployment_targets.tier` (dev | staging | prod), backfilled once
     from today's name rules and then set in Infra › Deploys.
  2. The environment's NAME, when it is a known word: production / prod / prd / live (and
     `prod-eu`, `production-2`, `live-x`) are production; dev / staging / test / qa / preview /
     sandbox / local (and their `-x` forms) are not.
  3. WHERE IT RUNS, when the name says nothing: a target on this machine (localhost, a loopback
     address, `*.localhost`, the Docker/Colima host alias, a local repository) is development.
  4. Anything else is PRODUCTION, and says it could not tell (`known=False`): an unlabelled
     remote server is treated as production, so BOSS asks (operator decision, 2026-11-23).

For BOSS ITSELF the install's tier is `BOSS_INSTALL_TIER` (dev | prod); an install that does
not say is a development install — a laptop, Colima, a dev VM — unless it says production.

For a MERGE the question is whether the branch SHIPS to production: a branch other than the
repository's default branch never does; the default branch ships wherever the repository's
`default_branch_ships_to` says (a target id → that target's verdict, `none` → development),
and a default branch nobody described ships to production (unknown → prod). A local repository
BOSS keeps on this machine ships nowhere.

Public outputs (a social post, a message to people outside the team) are always production.

`test_one_production_resolver` fails when any other module compares an environment against
"production"/"prod"/"live" or defines its own is_prod helper.
"""
from __future__ import annotations

import ipaddress
import os
import re
from dataclasses import asdict, dataclass
from typing import Any, Mapping, Optional

#: Environment names that mean production. Conservative: a name that might be production is.
PRODUCTION_NAMES = frozenset({"production", "prod", "prd", "live"})
#: Prefixes that keep a production name production (`prod-eu`, `production2`, `live-b`).
PRODUCTION_PREFIXES = ("production", "prod-", "prod_", "live-", "live_", "prd-", "prd_")
#: Names that are staging: shipped to before production, never production itself.
STAGING_NAMES = frozenset({"staging", "stage", "stg", "preview", "preprod", "pre-prod",
                           "pre_prod", "uat", "qa", "test", "testing"})
#: Names that are development.
DEV_NAMES = frozenset({"dev", "development", "local", "localhost", "sandbox", "ci",
                       "review", "feature"})
_NON_PROD_PREFIXES = tuple(f"{n}{sep}" for n in sorted(STAGING_NAMES | DEV_NAMES)
                           for sep in ("-", "_"))

TIERS = ("dev", "staging", "prod")
#: A merge base that ships nowhere: `default_branch_ships_to = 'none'`.
SHIPS_NOWHERE = "none"

#: Install tier words.
_INSTALL_PROD = frozenset({"prod", "production", "live", "prd"})
_INSTALL_DEV = frozenset({"dev", "development", "local", "staging", "test", ""})


@dataclass(frozen=True)
class Verdict:
    """Whether something is production, whether BOSS actually KNOWS, and why."""

    prod: bool
    known: bool
    tier: str           # dev | staging | prod | unknown
    source: str         # target_tier | environment_name | local_host | install_tier | ...
    evidence: str       # one line a person reads: "target tier = staging"

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)

    @property
    def words(self) -> str:
        if self.prod and not self.known:
            return f"BOSS could not tell whether this is production ({self.evidence}), so it is"
        return ("production" if self.prod else "not production") + f": {self.evidence}"


def _dev(tier: str, source: str, evidence: str) -> Verdict:
    return Verdict(False, True, tier, source, evidence)


def _prod(source: str, evidence: str, *, known: bool = True) -> Verdict:
    return Verdict(True, known, "prod" if known else "unknown", source, evidence)


# ---- names ------------------------------------------------------------------------------------

def _norm(value: Any) -> str:
    return str(value or "").strip().lower().replace(" ", "_")


def environment_tier(environment: Any) -> Optional[str]:
    """dev | staging | prod for a name BOSS recognises, else None (it cannot tell)."""
    env = _norm(environment)
    if not env:
        return None
    if env in PRODUCTION_NAMES or env.startswith(PRODUCTION_PREFIXES):
        return "prod"
    if env in STAGING_NAMES:
        return "staging"
    if env in DEV_NAMES:
        return "dev"
    if env.startswith(_NON_PROD_PREFIXES):
        head = re.split(r"[-_]", env, maxsplit=1)[0]
        return "staging" if head in STAGING_NAMES else "dev"
    return None


def is_production_environment(environment: Any) -> bool:
    """Whether the NAME alone says production. A name that says nothing is NOT production here:
    this is the narrow name test used where the name is all there is and an unknown name must
    not escalate (identity's deployment:production permission, display labels). Decisions about
    asking a person use `resolve_environment`, where unknown means production."""
    return environment_tier(environment) == "prod"


def is_named_non_production(environment: Any) -> bool:
    """The name says staging or development."""
    return environment_tier(environment) in ("dev", "staging")


# ---- where it runs ----------------------------------------------------------------------------

_LOCAL_HOSTNAMES = frozenset({"localhost", "host.docker.internal", "host.lima.internal",
                              "host.colima.internal", "gateway.docker.internal",
                              "docker.for.mac.localhost", "colima"})


def _host_of(address: Any) -> str:
    raw = str(address or "").strip().lower()
    if not raw:
        return ""
    raw = re.sub(r"^[a-z][a-z0-9+.-]*://", "", raw)       # scheme
    raw = raw.split("/", 1)[0]
    raw = raw.rsplit("@", 1)[-1]                           # user@
    if raw.startswith("["):                                # [::1]:22
        return raw[1:].split("]", 1)[0]
    if raw.count(":") == 1:
        raw = raw.split(":", 1)[0]
    return raw


def is_local_address(address: Any) -> bool:
    """A host on THIS machine: loopback, localhost, `*.localhost`, the Docker/Colima host."""
    host = _host_of(address)
    if not host:
        return False
    if host in _LOCAL_HOSTNAMES or host.endswith(".localhost"):
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


#: Target kinds that run on this machine by construction.
LOCAL_TARGET_KINDS = frozenset({"local", "local_docker", "local_compose", "localhost",
                                "preview", "sandbox"})


def resolve_environment(environment: Any, *, host: Any = None, local: Optional[bool] = None,
                        tier: Any = None) -> Verdict:
    """The verdict from what is known about a place: a set tier, its name, where it runs.
    Unknown is production (`known=False`)."""
    set_tier = _norm(tier)
    if set_tier in TIERS:
        if set_tier == "prod":
            return _prod("target_tier", "target tier = prod")
        return _dev(set_tier, "target_tier", f"target tier = {set_tier}")
    named = environment_tier(environment)
    if named == "prod":
        return _prod("environment_name", f"environment '{_norm(environment)}'")
    if named in ("dev", "staging"):
        return _dev(named, "environment_name", f"environment '{_norm(environment)}'")
    if local is True or (local is None and is_local_address(host)):
        return _dev("dev", "local_host", f"runs on this machine ({_host_of(host) or 'local'})")
    what = _norm(environment) or "no environment"
    where = _host_of(host)
    return _prod("unknown_remote",
                 f"{what}" + (f" on {where}" if where else "") + " is not labelled",
                 known=False)


# ---- targets, runs, change sets ---------------------------------------------------------------

def _row(connection: Any, sql: str, params: Mapping[str, Any]) -> Optional[dict]:
    from sqlalchemy import text

    point = connection.begin_nested()
    try:
        row = connection.execute(text(sql), dict(params)).mappings().first()
    except Exception:  # noqa: BLE001 — a missing column/table reads as "not known"
        point.rollback()
        return None
    point.commit()
    return dict(row) if row else None


_TARGET_SQL = (
    "SELECT t.id, t.environment, t.tier, t.target_kind, t.target_ref, h.address AS host_address "
    "FROM deployment_targets t LEFT JOIN deployment_hosts h ON h.id = t.host_id "
    "WHERE t.id = :i")


def target_verdict(target: Mapping[str, Any]) -> Verdict:
    """The verdict for a deployment target row (any of its usual columns may be missing)."""
    kind = _norm(target.get("target_kind"))
    host = target.get("host_address") or target.get("address") or target.get("target_ref")
    local = True if kind in LOCAL_TARGET_KINDS else None
    return resolve_environment(target.get("environment"), host=host, local=local,
                               tier=target.get("tier"))


def resolve_target(connection: Any, target_id: Any) -> Verdict:
    if not str(target_id or "").strip():
        return _prod("no_target", "no deployment target named", known=False)
    row = _row(connection, _TARGET_SQL, {"i": str(target_id)})
    if row is None:
        row = _row(connection, "SELECT id, environment, target_kind, target_ref "
                               "FROM deployment_targets WHERE id = :i", {"i": str(target_id)})
    if row is None:
        return _prod("no_target", f"target {target_id} not found", known=False)
    return target_verdict(row)


def resolve_deployment_run(connection: Any, run_id: Any) -> Verdict:
    row = _row(connection, "SELECT environment, deployment_target_id FROM deployment_runs "
                           "WHERE id = :i", {"i": str(run_id or "")})
    if row is None:
        return _prod("no_target", f"deployment run {run_id} not found", known=False)
    if row.get("deployment_target_id"):
        return resolve_target(connection, row["deployment_target_id"])
    return resolve_environment(row.get("environment"))


def resolve_changeset(connection: Any, changeset_id: Any) -> Verdict:
    rows = _row(connection, "SELECT bool_or(production_affecting) AS prod, count(*) AS n "
                            "FROM changeset_operations WHERE changeset_id = :i",
                {"i": str(changeset_id or "")})
    if not rows or not rows.get("n"):
        return _prod("no_target", f"change set {changeset_id} has no operations", known=False)
    if rows.get("prod"):
        return _prod("changeset", "an operation is production-affecting")
    return _dev("dev", "changeset", "no operation is production-affecting")


def resolve_project_environment(connection: Any, project_id: str, environment: Any) -> Verdict:
    """An environment NAME inside a project: the project's active target for that environment
    answers when there is one (its tier and host), else the name alone."""
    row = _row(connection,
               "SELECT t.id, t.environment, t.tier, t.target_kind, t.target_ref, "
               "h.address AS host_address FROM deployment_targets t "
               "LEFT JOIN deployment_hosts h ON h.id = t.host_id "
               "WHERE t.project_id = :p AND lower(t.environment) = :e "
               "AND t.status NOT IN ('deleted', 'retired') "
               "ORDER BY t.updated_at DESC NULLS LAST LIMIT 1",
               {"p": project_id, "e": _norm(environment)})
    if row is not None:
        return target_verdict(row)
    return resolve_environment(environment)


def production_affecting(connection: Any = None, *, environment: Any = None,
                         target: Optional[Mapping[str, Any]] = None) -> bool:
    """The `production_affecting` flag of a ChangeSet operation, from the verdict — never from
    `environment == "production"`, which missed `prod` and `live` targets."""
    if target is not None:
        return target_verdict(target).prod
    return resolve_environment(environment).prod


def risk_tier_for(environment: Any = None, *, target: Optional[Mapping[str, Any]] = None,
                  prod_tier: int = 3, other_tier: int = 2) -> int:
    """The risk tier of an act on a place: higher on production."""
    return prod_tier if production_affecting(environment=environment, target=target) \
        else other_tier


# ---- merges -----------------------------------------------------------------------------------

def resolve_merge(connection: Any, *, project_id: Optional[str], repo: Any,
                  base_branch: Any) -> Verdict:
    """Whether merging into `base_branch` of `repo` releases to production."""
    full = str(repo or "").strip().strip("/")
    base = str(base_branch or "").strip()
    binding = None
    if full:
        binding = _row(connection,
                       "SELECT full_name, default_branch, host, default_branch_ships_to "
                       "FROM github_repository_bindings WHERE lower(full_name) = lower(:r) "
                       + ("AND project_id = :p " if project_id else "")
                       + "ORDER BY (status = 'active') DESC LIMIT 1",
                       {"r": full, **({"p": project_id} if project_id else {})})
    if binding is not None and _norm(binding.get("host")) == "local":
        return _dev("dev", "local_repository", f"{full} is a local repository on this machine")
    default = str((binding or {}).get("default_branch") or "").strip()
    if not base:
        return _prod("merge_base", "no base branch named", known=False)
    if default and base != default:
        return _dev("dev", "merge_base", f"{base} is not {full or 'the repository'}'s default "
                                         f"branch ({default})")
    if not default and base not in ("main", "master", "trunk", "production", "release"):
        return _dev("dev", "merge_base", f"{base} is not a default branch")
    ships = str((binding or {}).get("default_branch_ships_to") or "").strip()
    if ships == SHIPS_NOWHERE:
        return _dev("dev", "default_branch_ships_to", f"{base} ships nowhere (set on the repo)")
    if ships:
        verdict = resolve_target(connection, ships)
        return Verdict(verdict.prod, verdict.known, verdict.tier, "default_branch_ships_to",
                       f"{base} ships to target {ships}: {verdict.evidence}")
    return _prod("default_branch_ships_to",
                 f"where {full or 'the repository'}'s {base} ships is not set", known=False)


# ---- BOSS itself ------------------------------------------------------------------------------

def install_tier() -> str:
    """dev | prod for THIS BOSS install (BOSS_INSTALL_TIER; unset = dev)."""
    raw = _norm(os.environ.get("BOSS_INSTALL_TIER"))
    if raw in _INSTALL_PROD:
        return "prod"
    return "dev"


def resolve_install() -> Verdict:
    raw = _norm(os.environ.get("BOSS_INSTALL_TIER"))
    if raw in _INSTALL_PROD:
        return _prod("install_tier", f"BOSS_INSTALL_TIER={raw}")
    if raw in _INSTALL_DEV:
        return _dev("dev", "install_tier",
                    f"BOSS_INSTALL_TIER={raw}" if raw else "BOSS install with no tier set "
                    "(a development install unless BOSS_INSTALL_TIER=production)")
    # An install tier word BOSS does not know: fail closed.
    return _prod("install_tier", f"BOSS_INSTALL_TIER={raw} is not dev or prod", known=False)


# ---- one entry point --------------------------------------------------------------------------

PUBLIC = _prod("public", "a public audience is production")


def resolve(connection: Any, scope: Mapping[str, Any], *,
            project_id: Optional[str] = None) -> Verdict:
    """The verdict for whatever `scope` names, in order: public, BOSS's install, a deployment
    target, a deployment run, a change set, a merge, a named environment. Nothing named: unknown,
    so production."""
    scope = scope or {}
    if scope.get("public"):
        return PUBLIC
    if scope.get("install") or scope.get("boss_install"):
        return resolve_install()
    for key in ("target_id", "deployment_target_id"):
        if scope.get(key):
            return resolve_target(connection, scope[key])
    if scope.get("deployment_run_id"):
        return resolve_deployment_run(connection, scope["deployment_run_id"])
    if scope.get("changeset_id"):
        return resolve_changeset(connection, scope["changeset_id"])
    base = scope.get("pr_base") or scope.get("base_branch") or scope.get("base")
    if base:
        return resolve_merge(connection, project_id=project_id or scope.get("project_id"),
                             repo=scope.get("repo") or scope.get("repository")
                             or scope.get("full_name"), base_branch=base)
    if scope.get("environment") or scope.get("host"):
        if project_id and connection is not None and not scope.get("host"):
            return resolve_project_environment(connection, project_id, scope.get("environment"))
        return resolve_environment(scope.get("environment"), host=scope.get("host"))
    return _prod("nothing_named", "nothing says where this acts", known=False)
