"""Links, link passes, test users and the environment a linked app gets. Owner: stream S5.

APP_TEMPLATES_AND_SECURITY_LOOP.md §5. Decisions: D7 same project only, D8 one gateway port per
link, D9 test-user passwords revealable behind `app_secret:write` (step-up), audited.

Words
-----
consumer (B)   the app that calls; it gets BOSS_LINK_<ALIAS>_* at build, preview and deploy.
provider (A)   the app that is called; it gets BOSS_TEST_USERS (and seeds them) when a
               `test_users` link names it. A gains nothing else from being linked (S5.5).
link pass      `boss_lnk_…`: minted for ONE consumer workload (a build run or a preview) and ONE
               link, presented to the gateway as `X-Boss-Link`. Only its hash is stored. It dies
               with its link (revoke), with its workload (the gateway checks the workload is
               live on every request) and at its expiry.
app key        the identity key of the app's name (agent_assignment.app_key): the key every app
               view groups runs by. An API path may also name an app by any of its run ids.

The values of test users and passes are never logged, never returned except by the audited
reveal, and are registered with the redaction filter in the process that holds them.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable, Mapping, Optional

from sqlalchemy import text

from ..identity import link_rules

ALIAS_RE = re.compile(r"^[A-Z][A-Z0-9_]{1,30}$")
ENVS = ("preview", "deploy")
CREDENTIALS = ("none", "test_users", "client")
NOTIFY_CHANNEL = "app_links"
TEST_USERS_PROVIDER = "boss_test_users"
TEST_USER_ROLES = ("user", "user", "admin")
LINK_PORTS_ENV = "BOSS_LINK_PORTS"
LINK_PORTS_DEFAULT = "18100-18199"
PREVIEW_PORTS_ENV = "BOSS_PREVIEW_PORTS"
PREVIEW_PORTS_DEFAULT = "19000-19999"
GATEWAY_HOST_ENV = "BOSS_LINK_GATEWAY_HOST"      # how a consumer names the gateway (default egress)
PREVIEW_HOST_ENV = "BOSS_LINK_PREVIEW_HOST"      # how the gateway names the preview host (default api)
RUN_PASS_TTL = timedelta(hours=6)
LIVE_RUN_STATUSES = ("running", "queued", "repairing")
LIVE_PREVIEW_STATUSES = ("running", "starting")


class LinkError(Exception):
    def __init__(self, code: str, status: int = 400, detail: str = ""):
        super().__init__(code)
        self.code, self.status, self.detail = code, status, detail


# ---- configuration --------------------------------------------------------------------------------

def link_ports() -> link_rules.PortRange:
    return link_rules.PortRange.parse(os.environ.get(LINK_PORTS_ENV), LINK_PORTS_DEFAULT)


def preview_ports() -> link_rules.PortRange:
    return link_rules.PortRange.parse(os.environ.get(PREVIEW_PORTS_ENV), PREVIEW_PORTS_DEFAULT)


def gateway_host() -> str:
    return (os.environ.get(GATEWAY_HOST_ENV) or "egress").strip()


def preview_host() -> str:
    return (os.environ.get(PREVIEW_HOST_ENV) or "api").strip()


def policy() -> link_rules.UpstreamPolicy:
    return link_rules.UpstreamPolicy(preview_host(), preview_ports(), link_ports())


def _hash(raw: str) -> str:
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _notify(db: Any, link_id: str) -> None:
    db.execute(text("SELECT pg_notify(:c, :p)"), {"c": NOTIFY_CHANNEL, "p": link_id})


# ---- apps -----------------------------------------------------------------------------------------

def app_index(db: Any, project_id: str) -> tuple[dict[str, str], dict[str, str]]:
    """({run_id: app_key}, {app_key: app name}) for the project, grouped the way every app view
    groups runs (factory_view.apps)."""
    from ..services.agent_assignment import app_key
    from ..services.code_task_service import list_code_task_runs
    from ..services.factory_view import FLOOR_RUNS, app_name, group_into_apps

    runs = list_code_task_runs(db, project_id, FLOOR_RUNS, exclude_internal=True)
    by_run: dict[str, str] = {}
    names: dict[str, str] = {}
    for group in group_into_apps(runs):
        name = app_name(group[0])
        key = app_key(name)
        if not key:
            continue
        names.setdefault(key, name)
        for run in group:
            by_run[str(run["id"])] = key
    return by_run, names


def resolve_app_key(db: Any, project_id: str, raw: str) -> Optional[str]:
    """An app of THIS project named by its key, any of its run ids, or its name; else None."""
    from ..services.agent_assignment import app_key

    by_run, names = app_index(db, project_id)
    raw = (raw or "").strip()
    if raw in by_run:
        return by_run[raw]
    if raw in names:
        return raw
    key = app_key(raw)
    return key if key in names else None


def project_org(db: Any, project_id: str) -> Optional[str]:
    return db.execute(text("SELECT w.org_id FROM projects p JOIN workspaces w ON w.id = p.workspace_id "
                           "WHERE p.id = :p"), {"p": project_id}).scalar()


# ---- links ----------------------------------------------------------------------------------------

def _row(db: Any, link_id: str) -> Optional[dict[str, Any]]:
    row = db.execute(text("SELECT * FROM app_links WHERE id = :i"), {"i": link_id}).mappings().first()
    return dict(row) if row else None


def _allocate_port(db: Any) -> int:
    """The lowest free port of the pool, under a transaction lock (two creates never share one;
    the partial unique index is the backstop)."""
    db.execute(text("SELECT pg_advisory_xact_lock(hashtext('app_links.gateway_port'))"))
    used = {int(p) for (p,) in db.execute(text(
        "SELECT gateway_port FROM app_links WHERE status = 'active' AND gateway_port IS NOT NULL")).all()}
    pool = link_ports()
    for port in range(pool.low, pool.high + 1):
        if port not in used:
            return port
    raise LinkError("no_free_link_port", 409, f"every port of {LINK_PORTS_ENV} is in use")


def create_link(db: Any, *, project_id: str, consumer: str, provider: str, alias: str, env: str,
                credentials: str, created_by: Optional[str]) -> dict[str, Any]:
    """One active, one-way link consumer -> provider in one project. Raises LinkError."""
    from ..services.core import make_id

    alias = (alias or "").strip()
    if not ALIAS_RE.match(alias):
        raise LinkError("bad_alias", 422, "an alias is A-Z, 0-9 and _, starts with a letter, 2-31 long")
    if env not in ENVS:
        raise LinkError("bad_env", 422, "env is preview or deploy")
    if credentials not in CREDENTIALS:
        raise LinkError("bad_credentials", 422, "credentials is none, test_users or client")
    if credentials == "client":
        raise LinkError("client_credentials_not_available", 422,
                        "client credentials come with BOSS sign-in for apps (F6)")
    consumer_key = resolve_app_key(db, project_id, consumer)
    if consumer_key is None:
        raise LinkError("unknown_app", 404, "no such app in this project")
    # D7: the provider must be an app of the SAME project. An app of another project (or no app
    # at all) resolves to nothing here, so it is refused with the same answer.
    provider_key = resolve_app_key(db, project_id, provider)
    if provider_key is None:
        raise LinkError("provider_not_in_project", 422, "links join two apps of the same project")
    if provider_key == consumer_key:
        raise LinkError("self_link", 422, "an app cannot link to itself")
    exists = db.execute(text(
        "SELECT 1 FROM app_links WHERE project_id = :p AND consumer_app_key = :c AND alias = :a "
        "AND env = :e AND status = 'active'"),
        {"p": project_id, "c": consumer_key, "a": alias, "e": env}).first()
    if exists:
        raise LinkError("alias_taken", 409, "this app already has an active link with that alias")
    port = _allocate_port(db) if env == "preview" else None
    link_id = make_id("lnk")
    db.execute(text(
        "INSERT INTO app_links (id, project_id, consumer_app_key, provider_app_key, alias, env, "
        " credentials, status, gateway_port, created_by) VALUES (:i, :p, :c, :pr, :a, :e, :cr, "
        " 'active', :port, :by)"),
        {"i": link_id, "p": project_id, "c": consumer_key, "pr": provider_key, "a": alias, "e": env,
         "cr": credentials, "port": port, "by": created_by})
    if credentials == "test_users":
        ensure_test_users(db, project_id, provider_key, env)
    _notify(db, link_id)
    return _row(db, link_id) or {}


def revoke_link(db: Any, *, project_id: str, link_id: str, revoked_by: Optional[str]) -> dict[str, Any]:
    """Revoke at once: the row (the gateway reads it per request, cached at most
    link_rules.CACHE_SECONDS, and drops its cache on the NOTIFY), every pass of the link, and the
    port goes back to the pool."""
    row = _row(db, link_id)
    if row is None or row["project_id"] != project_id:
        raise LinkError("not_found", 404)
    if row["status"] != "revoked":
        db.execute(text("UPDATE app_links SET status = 'revoked', revoked_at = now(), revoked_by = :by "
                        "WHERE id = :i"), {"i": link_id, "by": revoked_by})
        db.execute(text("UPDATE app_link_passes SET revoked_at = now() WHERE link_id = :i AND revoked_at IS NULL"),
                   {"i": link_id})
        _notify(db, link_id)
    return _row(db, link_id) or {}


def list_links(db: Any, project_id: str, consumer_key: str) -> list[dict[str, Any]]:
    """The consumer's links, newest first, as the console shows them: never a credential value."""
    _, names = app_index(db, project_id)
    rows = db.execute(text(
        "SELECT * FROM app_links WHERE project_id = :p AND consumer_app_key = :c "
        "ORDER BY (status = 'active') DESC, created_at DESC LIMIT 200"),
        {"p": project_id, "c": consumer_key}).mappings().all()
    users = test_users_presence(db, project_id, [r["provider_app_key"] for r in rows])
    out = []
    for r in rows:
        out.append({
            "id": r["id"], "alias": r["alias"], "provider_app_key": r["provider_app_key"],
            "provider_app": names.get(r["provider_app_key"], r["provider_app_key"]),
            "env": r["env"], "credentials": r["credentials"], "status": r["status"],
            "url": link_url(db, dict(r)), "env_prefix": f"BOSS_LINK_{r['alias']}_",
            "gateway_port": r["gateway_port"],
            "test_users": (users.get((r["provider_app_key"], r["env"]))
                           if r["credentials"] == "test_users" else None),
            "created_by": r["created_by"], "created_at": _iso(r["created_at"]),
            "revoked_at": _iso(r["revoked_at"]), "last_used_at": _iso(r["last_used_at"])})
    return out


def _iso(v: Optional[datetime]) -> Optional[str]:
    return v.isoformat() if v else None


def link_url(db: Any, link: Mapping[str, Any]) -> Optional[str]:
    """Where the consumer reaches the provider from where it runs (§5.4)."""
    if link.get("status") != "active":
        return None
    if link["env"] == "preview":
        return f"http://{gateway_host()}:{int(link['gateway_port'])}"
    return deployed_url(db, link["project_id"], link["provider_app_key"])


def deployed_url(db: Any, project_id: str, app_key: str) -> Optional[str]:
    """The provider's public URL from its newest healthy deploy, if one recorded it."""
    from ..services.agent_assignment import app_key as key_of

    for row in db.execute(text(
            "SELECT app_name, release_packet FROM deployment_runs WHERE project_id = :p AND status IN "
            "('healthy','succeeded','completed','passed') ORDER BY created_at DESC LIMIT 100"),
            {"p": project_id}).mappings().all():
        if key_of(str(row["app_name"] or "")) != app_key:
            continue
        packet = row["release_packet"] if isinstance(row["release_packet"], dict) else \
            json.loads(row["release_packet"] or "{}")
        for name in ("public_url", "app_url", "url"):
            url = str(packet.get(name) or "")
            if url.startswith(("https://", "http://")):
                return url
    return None


# ---- test users (BOSS_TEST_USERS) -----------------------------------------------------------------

def _generate_users(app_key: str) -> list[dict[str, str]]:
    slug = re.sub(r"[^a-z0-9]+", "-", app_key.lower()).strip("-")[:24] or "app"
    out = []
    for i, role in enumerate(TEST_USER_ROLES, start=1):
        out.append({"username": f"boss-test-{i}@{slug}.test", "password": secrets.token_urlsafe(18),
                    "role": role})
    return out


def _register(values: Iterable[str]) -> None:
    from ..identity import redaction

    redaction.register_secrets(values)


def ensure_test_users(db: Any, project_id: str, app_key: str, env: str, *,
                      rotate: bool = False) -> list[dict[str, str]]:
    """The provider's test users for env: generated on first need, regenerated when `rotate`
    (each new preview of the provider), vaulted. The previous vault item is deleted on rotation."""
    from ..identity import vault

    row = db.execute(text("SELECT * FROM app_test_users WHERE project_id = :p AND app_key = :a AND env = :e "
                          "FOR UPDATE"), {"p": project_id, "a": app_key, "e": env}).mappings().first()
    if row is not None and not rotate:
        return read_test_users(db, project_id, app_key, env)
    org = project_org(db, project_id)
    if not org:
        raise LinkError("unknown_project", 404)
    users = _generate_users(app_key)
    _register(u["password"] for u in users)
    item = vault.store(org_id=org, owner_principal_id=None, kind=vault.VaultItemKind.PROVIDER_CREDENTIAL,
                       provider=TEST_USERS_PROVIDER, secret=json.dumps(users), db=db,
                       connection_name=f"{project_id}/{app_key}/{env}"[:200])
    if row is None:
        db.execute(text("INSERT INTO app_test_users (project_id, app_key, env, vault_item_id, user_count) "
                        "VALUES (:p, :a, :e, :v, :n)"),
                   {"p": project_id, "a": app_key, "e": env, "v": item, "n": len(users)})
    else:
        db.execute(text("UPDATE app_test_users SET vault_item_id = :v, user_count = :n, "
                        "generation = generation + 1, rotated_at = now() "
                        "WHERE project_id = :p AND app_key = :a AND env = :e"),
                   {"p": project_id, "a": app_key, "e": env, "v": item, "n": len(users)})
        db.execute(text("DELETE FROM identity_vault_items WHERE id = :v AND provider = :pr"),
                   {"v": row["vault_item_id"], "pr": TEST_USERS_PROVIDER})
    return users


def read_test_users(db: Any, project_id: str, app_key: str, env: str) -> list[dict[str, str]]:
    from ..identity import vault

    row = db.execute(text("SELECT vault_item_id FROM app_test_users WHERE project_id = :p AND app_key = :a "
                          "AND env = :e"), {"p": project_id, "a": app_key, "e": env}).first()
    if row is None:
        return []
    users = json.loads(vault.read_secret(row[0], db=db, kind=vault.VaultItemKind.PROVIDER_CREDENTIAL))
    _register(u.get("password", "") for u in users)
    return users


def test_users_presence(db: Any, project_id: str, app_keys: Iterable[str]) -> dict[tuple[str, str], dict]:
    keys = sorted(set(app_keys))
    if not keys:
        return {}
    out = {}
    for r in db.execute(text("SELECT app_key, env, user_count, generation, rotated_at FROM app_test_users "
                             "WHERE project_id = :p AND app_key = ANY(:k)"), {"p": project_id, "k": keys}).mappings():
        out[(r["app_key"], r["env"])] = {"count": r["user_count"], "generation": r["generation"],
                                         "rotated_at": _iso(r["rotated_at"])}
    return out


def provides_test_users(db: Any, project_id: str, app_key: str, env: str) -> bool:
    return db.execute(text(
        "SELECT 1 FROM app_links WHERE project_id = :p AND provider_app_key = :a AND env = :e "
        "AND credentials = 'test_users' AND status = 'active' LIMIT 1"),
        {"p": project_id, "a": app_key, "e": env}).first() is not None


# ---- link passes ----------------------------------------------------------------------------------

def mint_pass(db: Any, link: Mapping[str, Any], *, consumer_kind: str, consumer_id: str,
              expires_at: datetime) -> str:
    raw = link_rules.PASS_PREFIX + secrets.token_urlsafe(32)
    db.execute(text("INSERT INTO app_link_passes (token_hash, link_id, project_id, consumer_kind, consumer_id, "
                    "expires_at) VALUES (:h, :l, :p, :k, :c, :x)"),
               {"h": _hash(raw), "l": link["id"], "p": link["project_id"], "k": consumer_kind,
                "c": consumer_id, "x": expires_at})
    _register([raw])
    return raw


def resolve_pass(db: Any, raw: Optional[str]) -> Optional[dict[str, Any]]:
    """The live pass row for a presented pass, else None (unknown, expired, revoked: one answer)."""
    raw = (raw or "").strip()
    if not raw.startswith(link_rules.PASS_PREFIX) or len(raw) > 128:
        return None
    row = db.execute(text("SELECT * FROM app_link_passes WHERE token_hash = :h AND revoked_at IS NULL "
                          "AND expires_at > now()"), {"h": _hash(raw)}).mappings().first()
    return dict(row) if row else None


def revoke_passes_of(db: Any, consumer_kind: str, consumer_id: str) -> None:
    """A workload ended (a preview stopped, a run finished): its passes die with it."""
    db.execute(text("UPDATE app_link_passes SET revoked_at = now() WHERE consumer_kind = :k AND consumer_id = :c "
                    "AND revoked_at IS NULL"), {"k": consumer_kind, "c": consumer_id})


def consumer_live(db: Any, link_pass: Mapping[str, Any]) -> bool:
    """Is the workload the pass was minted for still running? A run: its row is live and, when
    egress is in use, its run token's egress credential is not revoked or expired. A preview:
    running or starting, and not expired."""
    kind, cid = link_pass["consumer_kind"], link_pass["consumer_id"]
    if kind == "preview":
        return db.execute(text(
            "SELECT 1 FROM app_previews WHERE id = :i AND project_id = :p AND status = ANY(:s) "
            "AND (expires_at IS NULL OR expires_at > now())"),
            {"i": cid, "p": link_pass["project_id"], "s": list(LIVE_PREVIEW_STATUSES)}).first() is not None
    if kind == "run":
        live = db.execute(text("SELECT 1 FROM code_task_runs WHERE id = :i AND project_id = :p AND status = ANY(:s)"),
                          {"i": cid, "p": link_pass["project_id"], "s": list(LIVE_RUN_STATUSES)}).first()
        if live is None:
            return False
        creds = db.execute(text(
            "SELECT count(*) AS n, count(*) FILTER (WHERE revoked_at IS NULL AND expires_at > now()) AS ok "
            "FROM identity_egress_credentials WHERE run_id = :i"), {"i": cid}).mappings().first()
        return not creds or creds["n"] == 0 or creds["ok"] > 0
    return False


# ---- what the gateway looks up --------------------------------------------------------------------

def link_on_port(db: Any, port: int) -> Optional[dict[str, Any]]:
    row = db.execute(text("SELECT * FROM app_links WHERE gateway_port = :p AND status = 'active'"),
                     {"p": port}).mappings().first()
    return dict(row) if row else None


def active_ports(db: Any) -> dict[int, str]:
    return {int(p): str(i) for (p, i) in db.execute(text(
        "SELECT gateway_port, id FROM app_links WHERE status = 'active' AND env = 'preview' "
        "AND gateway_port IS NOT NULL")).all()}


def provider_upstream(db: Any, link: Mapping[str, Any]) -> Optional[tuple[str, int]]:
    """(preview host, port) of the provider's newest RUNNING process preview, or None."""
    by_run, _ = app_index(db, link["project_id"])
    runs = [rid for rid, key in by_run.items() if key == link["provider_app_key"]]
    if not runs:
        return None
    row = db.execute(text(
        "SELECT port FROM app_previews WHERE project_id = :p AND run_id = ANY(:r) AND status = 'running' "
        "AND mode = 'process' AND port IS NOT NULL AND (expires_at IS NULL OR expires_at > now()) "
        "ORDER BY created_at DESC LIMIT 1"), {"p": link["project_id"], "r": runs}).first()
    return (preview_host(), int(row[0])) if row else None


def touch_used(db: Any, link_id: str) -> None:
    db.execute(text("UPDATE app_links SET last_used_at = now() WHERE id = :i AND "
                    "(last_used_at IS NULL OR last_used_at < now() - interval '30 seconds')"), {"i": link_id})


# ---- the environment a linked app gets (§5.3) -----------------------------------------------------

def consumer_environment(db: Any, *, project_id: str, app_key: str, env: str, consumer_kind: str,
                         consumer_id: str, expires_at: Optional[datetime] = None) -> dict[str, str]:
    """BOSS_LINK_<ALIAS>_URL / _TOKEN / _USERS for every active link of the consumer in env.
    A preview link also mints this workload's pass (`_TOKEN`, sent as `X-Boss-Link`)."""
    out: dict[str, str] = {}
    rows = db.execute(text("SELECT * FROM app_links WHERE project_id = :p AND consumer_app_key = :c AND env = :e "
                           "AND status = 'active' ORDER BY alias"),
                      {"p": project_id, "c": app_key, "e": env}).mappings().all()
    for r in rows:
        link = dict(r)
        prefix = f"BOSS_LINK_{link['alias']}_"
        url = link_url(db, link)
        if url:
            out[prefix + "URL"] = url
        if env == "preview":
            out[prefix + "TOKEN"] = mint_pass(db, link, consumer_kind=consumer_kind, consumer_id=consumer_id,
                                              expires_at=expires_at or (_now() + RUN_PASS_TTL))
        if link["credentials"] == "test_users":
            users = ensure_test_users(db, project_id, link["provider_app_key"], env)
            out[prefix + "USERS"] = json.dumps(users, separators=(",", ":"))
    return out


def provider_environment(db: Any, *, project_id: str, app_key: str, env: str,
                         rotate: bool = False) -> dict[str, str]:
    """BOSS_TEST_USERS for an app some active `test_users` link names as provider in env.
    `rotate=True` on each NEW preview of the provider (§5.3)."""
    if not provides_test_users(db, project_id, app_key, env):
        return {}
    users = ensure_test_users(db, project_id, app_key, env, rotate=rotate)
    return {"BOSS_TEST_USERS": json.dumps(users, separators=(",", ":"))}


def workload_environment(db: Any, *, project_id: str, run_id: Optional[str], env: str, consumer_kind: str,
                         consumer_id: str, expires_at: Optional[datetime] = None,
                         rotate: bool = False) -> dict[str, str]:
    """Everything links add to one workload of the app `run_id` belongs to: as a consumer and as
    a provider. Empty when the run is not an app of the project or nothing links it."""
    if not run_id:
        return {}
    if db.execute(text("SELECT 1 FROM app_links WHERE project_id = :p AND status = 'active' LIMIT 1"),
                  {"p": project_id}).first() is None:
        return {}                     # the common case: nothing linked in this project, no lookups
    by_run, _ = app_index(db, project_id)
    key = by_run.get(run_id)
    if not key:
        return {}
    out = provider_environment(db, project_id=project_id, app_key=key, env=env, rotate=rotate)
    out.update(consumer_environment(db, project_id=project_id, app_key=key, env=env,
                                    consumer_kind=consumer_kind, consumer_id=consumer_id,
                                    expires_at=expires_at))
    return out
