"""User pools, subjects, claims and app roles (§6.2, §6.3).

A WORKFORCE pool's subjects are BOSS people (usr_ principals) signed in to the console; who may
sign in to a client is a grant: `oidc_app:sign_in` at the client's project. A CUSTOMER pool's
subjects are oidc_pool_users, a separate principal kind with no identity_principals row (phase 2).

`sub` is never a BOSS id. Pairwise (the default): HMAC(pool key, subject ‖ client) — two apps
cannot correlate a user. Public: HMAC(pool key, subject) — stable across the pool's apps.
"""
from __future__ import annotations

import hmac
import hashlib
import re
from typing import Any, Mapping, Optional

from sqlalchemy import text

from .util import audit, b64u, new_id

POOL_COLUMNS = ("id, org_id, name, kind, signup, signup_domains, mfa, passkeys, password_policy, "
                "subject_key_vault_item_id, created_at")
VAULT_PROVIDER = "boss-oidc-subject"
_SUBJECT_KEYS: dict[str, bytes] = {}


def _pool(r: Any) -> dict[str, Any]:
    d = dict(r)
    d["signup_domains"] = list(d.get("signup_domains") or [])
    return d


def public_view(p: Mapping[str, Any]) -> dict[str, Any]:
    out = {k: p.get(k) for k in ("id", "name", "kind", "signup", "signup_domains", "mfa", "passkeys",
                                 "password_policy")}
    out["created_at"] = p["created_at"].isoformat() if p.get("created_at") else None
    return out


def load(db: Any, *, org_id: str, pool_id: Any) -> Optional[dict[str, Any]]:
    if not isinstance(pool_id, str):
        return None
    r = db.execute(text(f"SELECT {POOL_COLUMNS} FROM oidc_user_pools WHERE id = :id AND org_id = :o"),
                   {"id": pool_id, "o": org_id}).mappings().first()
    return _pool(r) if r else None


def list_pools(db: Any, *, org_id: str) -> list[dict[str, Any]]:
    return [_pool(r) for r in db.execute(text(f"SELECT {POOL_COLUMNS} FROM oidc_user_pools WHERE org_id = :o "
                                              "ORDER BY created_at"), {"o": org_id}).mappings()]


def _validate(c: dict[str, Any]) -> dict[str, Any]:
    name = str(c.get("name") or "").strip()
    if not name or len(name) > 120:
        raise ValueError("name is required (120 characters at most)")
    kind = c.get("kind") or "workforce"
    if kind not in ("workforce", "customer"):
        raise ValueError("kind must be workforce or customer")
    signup = c.get("signup") or "closed"
    if signup not in ("closed", "invite", "open", "domain"):
        raise ValueError("signup must be closed, invite, open or domain")
    if kind == "workforce" and signup != "closed":
        raise ValueError("a workforce pool's members are BOSS people: its signup is closed")
    domains = [str(d).strip().lower() for d in (c.get("signup_domains") or [])]
    if any(not re.fullmatch(r"[a-z0-9]([a-z0-9-]*[a-z0-9])?(\.[a-z0-9]([a-z0-9-]*[a-z0-9])?)+", d) for d in domains):
        raise ValueError("signup_domains must be domain names")
    if signup == "domain" and not domains:
        raise ValueError("signup 'domain' needs signup_domains")
    mfa = c.get("mfa") or "optional"
    if mfa not in ("off", "optional", "required"):
        raise ValueError("mfa must be off, optional or required")
    policy = c.get("password_policy") or {}
    if not isinstance(policy, Mapping):
        raise ValueError("password_policy must be an object")
    return {"name": name, "kind": kind, "signup": signup, "signup_domains": domains, "mfa": mfa,
            "passkeys": bool(c.get("passkeys", True)), "password_policy": dict(policy)}


def create(db: Any, *, org_id: str, actor: str, body: Mapping[str, Any]) -> dict[str, Any]:
    import json
    import secrets

    from ..identity import vault
    from . import keys

    p = _validate(dict(body))
    pool_id = new_id("opl")
    item = vault.store(org_id=org_id, owner_principal_id=None, kind=vault.VaultItemKind.SIGNING_KEY,
                       provider=VAULT_PROVIDER, secret=b64u(secrets.token_bytes(32)), db=db,
                       connection_name=pool_id)
    db.execute(text(
        "INSERT INTO oidc_user_pools (id, org_id, name, kind, signup, signup_domains, mfa, passkeys, "
        "password_policy, subject_key_vault_item_id) VALUES (:id, :o, :n, :k, :s, :d, :m, :pk, "
        "CAST(:pp AS JSONB), :item)"),
        {"id": pool_id, "o": org_id, "n": p["name"], "k": p["kind"], "s": p["signup"], "d": p["signup_domains"],
         "m": p["mfa"], "pk": p["passkeys"], "pp": json.dumps(p["password_policy"]), "item": item})
    keys.provision(db, org_id=org_id, actor=actor)
    audit(db, org_id=org_id, event_type="oidc.pool.created", actor_principal_id=actor, actor_kind="user",
          target_type="oidc_pool", target_id=pool_id, details={"kind": p["kind"], "signup": p["signup"]})
    return load(db, org_id=org_id, pool_id=pool_id)


def update(db: Any, *, org_id: str, actor: str, pool_id: str, body: Mapping[str, Any]) -> dict[str, Any]:
    import json

    current = load(db, org_id=org_id, pool_id=pool_id)
    if current is None:
        raise LookupError("not_found")
    if "kind" in body and body["kind"] not in (None, current["kind"]):
        raise ValueError("a pool's kind cannot change")
    merged = {**current, **{k: v for k, v in body.items() if v is not None}}
    p = _validate(merged)
    db.execute(text(
        "UPDATE oidc_user_pools SET name = :n, signup = :s, signup_domains = :d, mfa = :m, passkeys = :pk, "
        "password_policy = CAST(:pp AS JSONB) WHERE id = :id"),
        {"id": pool_id, "n": p["name"], "s": p["signup"], "d": p["signup_domains"], "m": p["mfa"],
         "pk": p["passkeys"], "pp": json.dumps(p["password_policy"])})
    audit(db, org_id=org_id, event_type="oidc.pool.updated", actor_principal_id=actor, actor_kind="user",
          target_type="oidc_pool", target_id=pool_id, details={"fields": sorted(body)})
    return load(db, org_id=org_id, pool_id=pool_id)


def delete(db: Any, *, org_id: str, actor: str, pool_id: str) -> None:
    if load(db, org_id=org_id, pool_id=pool_id) is None:
        raise LookupError("not_found")
    in_use = db.execute(text("SELECT 1 FROM oidc_clients WHERE pool_id = :p AND status <> 'deleted' LIMIT 1"),
                        {"p": pool_id}).first()
    if in_use:
        raise ValueError("pool_in_use")
    users = db.execute(text("SELECT 1 FROM oidc_pool_users WHERE pool_id = :p LIMIT 1"), {"p": pool_id}).first()
    if users:
        raise ValueError("pool_has_users")
    db.execute(text("DELETE FROM oidc_role_assignments WHERE pool_id = :p"), {"p": pool_id})
    db.execute(text("DELETE FROM oidc_app_roles WHERE pool_id = :p AND client_id IN "
                    "(SELECT id FROM oidc_clients WHERE status = 'deleted')"), {"p": pool_id})
    if db.execute(text("SELECT 1 FROM oidc_clients WHERE pool_id = :p LIMIT 1"), {"p": pool_id}).first():
        # deleted clients still reference it (their rows are kept for audit): the pool stays, emptied
        raise ValueError("pool_in_use")
    db.execute(text("DELETE FROM oidc_user_pools WHERE id = :p"), {"p": pool_id})
    audit(db, org_id=org_id, event_type="oidc.pool.deleted", actor_principal_id=actor, actor_kind="user",
          target_type="oidc_pool", target_id=pool_id)


# ---- subjects ----------------------------------------------------------------------------------

def _subject_key(db: Any, pool: Mapping[str, Any]) -> bytes:
    from ..identity import vault

    cached = _SUBJECT_KEYS.get(pool["id"])
    if cached is not None:
        return cached
    secret = vault.read_secret(pool["subject_key_vault_item_id"], db=db, kind=vault.VaultItemKind.SIGNING_KEY)
    key = secret.encode("ascii")
    _SUBJECT_KEYS[pool["id"]] = key
    return key


def subject_for(db: Any, *, pool: Mapping[str, Any], client: Mapping[str, Any], subject_ref: str) -> str:
    """The `sub` an app sees. Never a BOSS id; pairwise by default (S6.9)."""
    key = _subject_key(db, pool)
    if client["subject_type"] == "public":
        msg = f"public\x1f{subject_ref}"
    else:
        msg = f"pairwise\x1f{subject_ref}\x1f{client['id']}"
    return b64u(hmac.new(key, msg.encode("utf-8"), hashlib.sha256).digest())


# ---- workforce subjects --------------------------------------------------------------------------

def workforce_principal(db: Any, principal_id: str) -> Optional[Any]:
    """The BOSS person behind a workforce subject, if they may still be one: an active USER, not
    the legacy shared-password principal (it is nobody in particular)."""
    from ..identity import users
    from ..identity.legacy import LEGACY_SHARED_PRINCIPAL
    from ..identity.principal import PrincipalKind

    principal = users.load_principal(db, principal_id)
    if principal is None or not principal.active or principal.kind != PrincipalKind.USER:
        return None
    if principal.id == LEGACY_SHARED_PRINCIPAL:
        return None
    return principal


def workforce_may_sign_in(db: Any, *, principal: Any, client: Mapping[str, Any], session: Any = None) -> bool:
    """The grant: `oidc_app:sign_in` at the client's project, decided by BOSS's PDP."""
    from ..identity import authz
    from ..identity.permissions_gen import P
    from ..identity.principal import AuthContext, AuthMethod
    from ..identity.scope import ResolvedScope

    ctx = AuthContext(principal, AuthMethod.SESSION, session=session)
    scope = ResolvedScope.in_project(principal.org_id, client["project_id"], object_kind="oidc_client",
                                     object_id=client["id"])
    return authz.check(ctx, P.OIDC_APP_SIGN_IN, scope, db=db).allowed


def workforce_profile(db: Any, principal_id: str) -> dict[str, Any]:
    r = db.execute(text("SELECT p.display_name, u.email, u.email_verified_at FROM identity_principals p "
                        "LEFT JOIN identity_users u ON u.principal_id = p.id WHERE p.id = :id"),
                   {"id": principal_id}).first()
    if r is None:
        return {}
    return {"name": r[0], "email": r[1], "email_verified": r[2] is not None}


def subject_alive(db: Any, *, pool: Mapping[str, Any], client: Mapping[str, Any], subject_ref: str) -> bool:
    """May this subject still hold tokens for this client (refresh, userinfo)?"""
    if pool["kind"] == "workforce":
        principal = workforce_principal(db, subject_ref)
        return principal is not None and principal.org_id == client["org_id"] and \
            workforce_may_sign_in(db, principal=principal, client=client)
    r = db.execute(text("SELECT status FROM oidc_pool_users WHERE id = :id AND pool_id = :p"),
                   {"id": subject_ref, "p": pool["id"]}).first()
    return r is not None and r[0] == "active"


def claims_for(db: Any, *, pool: Mapping[str, Any], client: Mapping[str, Any], subject_ref: str,
               scopes: list[str]) -> dict[str, Any]:
    """The claims the granted scopes allow (S6.9), `sub` always."""
    out: dict[str, Any] = {"sub": subject_for(db, pool=pool, client=client, subject_ref=subject_ref)}
    if pool["kind"] == "workforce":
        profile = workforce_profile(db, subject_ref)
    else:
        r = db.execute(text("SELECT name, email, email_verified FROM oidc_pool_users WHERE id = :id"),
                       {"id": subject_ref}).first()
        profile = {"name": r[0], "email": r[1], "email_verified": bool(r[2])} if r else {}
    if "profile" in scopes and profile.get("name"):
        out["name"] = profile["name"]
    if "email" in scopes and profile.get("email"):
        out["email"] = profile["email"]
        out["email_verified"] = bool(profile.get("email_verified"))
    if "roles" in scopes:
        out["roles"] = roles_for(db, pool_id=pool["id"], client_id=client["id"], subject_ref=subject_ref)
    return out


# ---- app roles -----------------------------------------------------------------------------------

def roles_for(db: Any, *, pool_id: str, client_id: str, subject_ref: str) -> list[str]:
    return [r[0] for r in db.execute(text(
        "SELECT a.role_key FROM oidc_role_assignments a JOIN oidc_app_roles r ON r.pool_id = a.pool_id "
        "AND r.client_id = a.client_id AND r.role_key = a.role_key WHERE a.pool_id = :p AND a.client_id = :c "
        "AND a.subject_ref = :s ORDER BY a.role_key"), {"p": pool_id, "c": client_id, "s": subject_ref})]


def list_roles(db: Any, *, client: Mapping[str, Any]) -> list[dict[str, Any]]:
    return [dict(r) for r in db.execute(text("SELECT role_key, name FROM oidc_app_roles WHERE pool_id = :p "
                                             "AND client_id = :c ORDER BY role_key"),
                                        {"p": client["pool_id"], "c": client["id"]}).mappings()]


def create_role(db: Any, *, actor: str, client: Mapping[str, Any], role_key: str, name: str) -> dict[str, Any]:
    if not re.fullmatch(r"[a-z][a-z0-9_.:-]{0,63}", role_key or ""):
        raise ValueError("role_key must be a lowercase word")
    name = (name or role_key).strip()[:120]
    inserted = db.execute(text("INSERT INTO oidc_app_roles (pool_id, client_id, role_key, name) VALUES "
                               "(:p, :c, :k, :n) ON CONFLICT DO NOTHING"),
                          {"p": client["pool_id"], "c": client["id"], "k": role_key, "n": name}).rowcount
    if not inserted:
        raise ValueError("role_exists")
    audit(db, org_id=client["org_id"], event_type="oidc.role.created", actor_principal_id=actor, actor_kind="user",
          target_type="oidc_client", target_id=client["id"], details={"role_key": role_key})
    return {"role_key": role_key, "name": name}


def list_assignments(db: Any, *, client: Mapping[str, Any]) -> list[dict[str, Any]]:
    return [dict(r) for r in db.execute(text("SELECT subject_ref, role_key FROM oidc_role_assignments WHERE "
                                             "pool_id = :p AND client_id = :c ORDER BY subject_ref, role_key"),
                                        {"p": client["pool_id"], "c": client["id"]}).mappings()]


def assign_role(db: Any, *, actor: str, client: Mapping[str, Any], subject_ref: str, role_key: str,
                remove: bool = False) -> None:
    pool = load(db, org_id=client["org_id"], pool_id=client["pool_id"])
    exists = db.execute(text("SELECT 1 FROM oidc_app_roles WHERE pool_id = :p AND client_id = :c AND role_key = :k"),
                        {"p": client["pool_id"], "c": client["id"], "k": role_key}).first()
    if exists is None:
        raise LookupError("role_not_found")
    if pool["kind"] == "workforce":
        principal = workforce_principal(db, subject_ref)
        if principal is None or principal.org_id != client["org_id"]:
            raise LookupError("subject_not_found")
    elif db.execute(text("SELECT 1 FROM oidc_pool_users WHERE id = :s AND pool_id = :p"),
                    {"s": subject_ref, "p": pool["id"]}).first() is None:
        raise LookupError("subject_not_found")
    if remove:
        db.execute(text("DELETE FROM oidc_role_assignments WHERE pool_id = :p AND subject_ref = :s AND client_id = :c "
                        "AND role_key = :k"), {"p": pool["id"], "s": subject_ref, "c": client["id"], "k": role_key})
    else:
        db.execute(text("INSERT INTO oidc_role_assignments (pool_id, subject_ref, client_id, role_key) VALUES "
                        "(:p, :s, :c, :k) ON CONFLICT DO NOTHING"),
                   {"p": pool["id"], "s": subject_ref, "c": client["id"], "k": role_key})
    audit(db, org_id=client["org_id"], event_type="oidc.role.unassigned" if remove else "oidc.role.assigned",
          actor_principal_id=actor, actor_kind="user", target_type="oidc_client", target_id=client["id"],
          details={"role_key": role_key, "subject_ref": subject_ref})
