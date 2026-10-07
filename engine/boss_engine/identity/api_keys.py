"""API keys: personal access tokens, service-account keys and SCIM keys (IDENTITY.md §5).

    boss_pat_ / boss_sak_ / boss_scim_ + 43 base62 (32 random bytes) + 6 base62 (CRC32)

Only SHA-256(key) is stored (unique index, looked up by hash; 256 bits needs no slow hash). The key
is returned exactly once, by `create`/`rotate`; after that no part of it is ever shown or logged
(D20). A key's effective set is `owner's LIVE permissions ∩ key.scopes`, computed per request by
the PDP from the CredentialRef this module builds — nothing about the owner is cached on the key.
"""
from __future__ import annotations

import ipaddress
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Iterable, Optional

from sqlalchemy import text

from . import ids
from .audit import Outcome
from .authn import API_KEY_PREFIXES, AuthnError, ParsedApiKey, hash_token, mint_api_key
from .authn_support import emit, get_policy, utcnow
from .catalogue import is_known
from .principal import AuthContext, AuthMethod, CredentialRef, PrincipalKind

GRACE_CHOICES = {"0": 0, "1h": 3600, "24h": 86400, "7d": 7 * 86400}
LAST_USE_WRITE_SECONDS = 60


class KeyRefused(Exception):
    """A key cannot be created/rotated as asked. `code` and `status` are the API's answer."""

    def __init__(self, code: str, status: int = 422, detail: Optional[dict] = None):
        super().__init__(code)
        self.code = code
        self.status = status
        self.detail = detail or {}


@dataclass(frozen=True)
class CreatedKey:
    id: str
    key: str            # shown ONCE
    expires_at: Optional[datetime]

    def __repr__(self) -> str:
        return f"CreatedKey(id={self.id!r})"


@dataclass(frozen=True)
class KeyRow:
    id: str
    principal_id: str
    kind: str
    name: str
    scopes: tuple[str, ...]
    project_ids: Optional[tuple[str, ...]]
    cidrs: Optional[tuple[str, ...]]
    expires_at: Optional[datetime]
    created_by: str
    created_at: datetime
    last_used_at: Optional[datetime]
    last_used_ip: Optional[str]
    rotated_from: Optional[str]
    grace_until: Optional[datetime]
    suspended_at: Optional[datetime]
    revoked_at: Optional[datetime]
    last_used_ua: Optional[str] = None

    @property
    def status(self) -> str:
        """types.ts KeyStatus: active | grace | revoked | expired."""
        now = utcnow()
        if self.revoked_at or self.suspended_at or (self.grace_until and self.grace_until <= now):
            return "revoked"
        if self.expires_at and self.expires_at <= now:
            return "expired"
        return "grace" if self.grace_until else "active"

    def public(self) -> dict[str, Any]:
        """Metadata only. There is no field that holds, or is derived from, the key (A24)."""
        now = utcnow()
        state = "active"
        if self.revoked_at or (self.grace_until and self.grace_until <= now):
            state = "revoked"
        elif self.expires_at and self.expires_at <= now:
            state = "expired"
        elif self.suspended_at:
            state = "suspended"
        elif self.grace_until:
            state = "rotating"
        return {"id": self.id, "principal_id": self.principal_id, "kind": self.kind,
                "name": self.name, "scopes": list(self.scopes),
                "project_ids": list(self.project_ids) if self.project_ids is not None else None,
                "cidrs": list(self.cidrs) if self.cidrs is not None else None,
                "expires_at": self.expires_at, "created_by": self.created_by,
                "created_at": self.created_at, "last_used_at": self.last_used_at,
                "last_used_ip": self.last_used_ip, "rotated_from": self.rotated_from,
                "grace_until": self.grace_until, "state": state}


_COLS = ("id, principal_id, kind, name, scopes, project_ids, cidrs::text[] AS cidrs, expires_at, "
         "created_by, created_at, last_used_at, host(last_used_ip) AS last_used_ip, rotated_from, "
         "grace_until, suspended_at, revoked_at, last_used_ua")


_COLS_K = ("k.id, k.principal_id, k.kind, k.name, k.scopes, k.project_ids, k.cidrs::text[] AS cidrs, "
           "k.expires_at, k.created_by, k.created_at, k.last_used_at, host(k.last_used_ip) AS "
           "last_used_ip, k.rotated_from, k.grace_until, k.suspended_at, k.revoked_at, k.last_used_ua")


def _row(r: Any) -> KeyRow:
    return KeyRow(r.id, r.principal_id, r.kind, r.name, tuple(r.scopes or ()),
                  tuple(r.project_ids) if r.project_ids is not None else None,
                  tuple(r.cidrs) if r.cidrs is not None else None, r.expires_at, r.created_by,
                  r.created_at, r.last_used_at, r.last_used_ip, r.rotated_from, r.grace_until,
                  r.suspended_at, r.revoked_at, r.last_used_ua)


def view(db: Any, row: KeyRow) -> dict[str, Any]:
    """The console's ApiKey (types.ts). Metadata only: nothing holds, or derives from, the key."""
    owner = db.execute(text("SELECT display_name, kind FROM identity_principals WHERE id = :p"),
                       {"p": row.principal_id}).first()
    iso = lambda d: d.isoformat() if d else None  # noqa: E731
    return {"id": row.id, "name": row.name,
            "owner": {"id": row.principal_id, "name": owner.display_name if owner else row.principal_id,
                      "kind": owner.kind if owner else "user"},
            "scopes": list(row.scopes), "projects": list(row.project_ids) if row.project_ids is not None else None,
            "networks": ", ".join(row.cidrs) if row.cidrs else None, "created_at": iso(row.created_at),
            "last_used_at": iso(row.last_used_at), "last_used_ip": row.last_used_ip,
            "last_used_client": row.last_used_ua, "expires_at": iso(row.expires_at),
            "grace_until": iso(row.grace_until), "rotated_from": row.rotated_from, "status": row.status}


def issued(db: Any, created: "CreatedKey", *, replaced: Optional[str] = None) -> dict[str, Any]:
    """IssuedSecret: the ONE response that carries a key, shown once."""
    row = get(db, created.id)
    assert row is not None
    replaces = None
    if replaced:
        old = get(db, replaced)
        if old is not None:
            stops = old.grace_until or old.revoked_at
            replaces = {"id": old.id, "stops_at": stops.isoformat() if stops else None}
    return {"key": view(db, row), "secret": created.key, "replaces": replaces, "shown_once": True}


def get(db: Any, key_id: str) -> Optional[KeyRow]:
    r = db.execute(text(f"SELECT {_COLS} FROM identity_api_credentials WHERE id = :id"),
                   {"id": key_id}).first()
    return _row(r) if r else None


def list_keys(db: Any, *, principal_id: Optional[str] = None, org_id: Optional[str] = None,
              kinds: Iterable[str] = ("pat", "sak", "scim", "legacy_env")) -> list[KeyRow]:
    sql = (f"SELECT {_COLS_K} FROM identity_api_credentials k "
           "JOIN identity_principals p ON p.id = k.principal_id WHERE k.kind = ANY(:kinds)")
    params: dict[str, Any] = {"kinds": list(kinds)}
    if principal_id:
        sql += " AND k.principal_id = :p"
        params["p"] = principal_id
    if org_id:
        sql += " AND p.org_id = :org"
        params["org"] = org_id
    return [_row(r) for r in db.execute(text(sql + " ORDER BY k.created_at DESC"), params)]


# ---- what a principal holds (for the subset rules of §6.11) -----------------------------------------

def held_permissions(db: Any, principal: Any) -> frozenset[str]:
    """Everything the principal holds at org scope or at any project it is bound in (S2's
    effective_permissions, evaluated live)."""
    from . import authz
    from .scope import ResolvedScope

    held: set[str] = set(authz.effective_permissions(principal, ResolvedScope.org_level(principal.org_id),
                                                     db=db).permissions)
    projects = db.execute(text(
        "SELECT DISTINCT scope_id FROM identity_role_bindings WHERE scope_type = 'project' AND ("
        "(subject_type = 'principal' AND subject_id = :p) OR (subject_type = 'group' AND subject_id IN "
        "(SELECT group_id FROM identity_group_members WHERE principal_id = :p)))"), {"p": principal.id})
    for (project_id,) in projects:
        held |= set(authz.effective_permissions(
            principal, ResolvedScope.in_project(principal.org_id, project_id), db=db).permissions)
    return frozenset(held)


def _validate_scopes(scopes: Iterable[str]) -> list[str]:
    out = sorted({str(s) for s in scopes})
    unknown = [s for s in out if not is_known(s)]
    if unknown:
        raise KeyRefused("unknown_permission", 422, {"permissions": unknown})
    if not out:
        raise KeyRefused("empty_scopes", 422)
    return out


def _validate_cidrs(cidrs: Optional[Iterable[str]]) -> Optional[list[str]]:
    if cidrs is None:
        return None
    out = []
    for c in cidrs:
        try:
            out.append(str(ipaddress.ip_network(str(c).strip(), strict=False)))
        except ValueError:
            raise KeyRefused("invalid_cidr", 422) from None
    return out or None


def _expiry(db: Any, org_id: str, *, kind: str, days: Optional[int]) -> Optional[datetime]:
    pol = get_policy(org_id, db=db)
    if days is None:
        days = pol.key_default_days
    if days == 0:
        if kind == "sak" and pol.key_allow_no_expiry_for_service_accounts:
            return None
        raise KeyRefused("expiry_required", 422)
    if days < 0 or days > pol.key_max_days:
        raise KeyRefused("expiry_too_long", 422, {"max_days": pol.key_max_days})
    return utcnow() + timedelta(days=days)


def create(db: Any, *, creator: AuthContext, owner: Any, kind: str, name: str,
           scopes: Iterable[str], project_ids: Optional[Iterable[str]] = None,
           cidrs: Optional[Iterable[str]] = None, expires_in_days: Optional[int] = None,
           rotated_from: Optional[str] = None, check_subset: bool = True) -> CreatedKey:
    """Mint a key for `owner`. Rules (§5.2, §5.4, §6.11): scopes are catalogue keys and ⊆ the
    owner's permissions; when the owner is not the creator, the creator must hold ⊇ everything the
    owner holds (no back door to a stronger service account, A25)."""
    if kind not in API_KEY_PREFIXES:
        raise KeyRefused("unknown_kind", 422)
    if kind == "pat" and owner.kind != PrincipalKind.USER:
        raise KeyRefused("pat_for_people_only", 422)
    if kind == "sak" and owner.kind != PrincipalKind.SERVICE_ACCOUNT:
        raise KeyRefused("sak_for_service_accounts_only", 422)
    name = (name or "").strip()[:120]
    if not name:
        raise KeyRefused("name_required", 422)
    wanted = _validate_scopes(scopes)
    if check_subset:
        owner_held = held_permissions(db, owner)
        beyond_owner = sorted(set(wanted) - owner_held)
        if beyond_owner:
            raise KeyRefused("scopes_exceed_owner", 403, {"permissions": beyond_owner})
        if owner.id != creator.principal.id:
            creator_held = held_permissions(db, creator.principal)
            missing = sorted(owner_held - creator_held)
            if missing:
                raise KeyRefused("owner_exceeds_creator", 403, {"permissions": missing})
    key = mint_api_key(kind)
    kid = ids.new_id("key")
    expires_at = _expiry(db, owner.org_id, kind=kind, days=expires_in_days)
    db.execute(text(
        "INSERT INTO identity_api_credentials (id, principal_id, kind, name, token_hash, scopes, "
        "project_ids, cidrs, expires_at, created_by, rotated_from) VALUES (:id, :p, :k, :n, :th, :s, "
        ":proj, CAST(:cidrs AS cidr[]), :exp, :by, :rf)"),
        {"id": kid, "p": owner.id, "k": kind, "n": name, "th": hash_token(key), "s": wanted,
         "proj": sorted(set(project_ids)) if project_ids is not None else None,
         "cidrs": _validate_cidrs(cidrs), "exp": expires_at, "by": creator.principal.id,
         "rf": rotated_from})
    emit(db, org_id=owner.org_id, event_type="api_key.created" if not rotated_from else "api_key.rotated",
         outcome=Outcome.SUCCESS, ctx=creator, target_type="api_key", target_id=kid,
         details={"kind": kind, "owner": owner.id, "scopes": wanted,
                  "expires_at": expires_at.isoformat() if expires_at else None,
                  **({"rotated_from": rotated_from} if rotated_from else {})})
    return CreatedKey(kid, key, expires_at)


def rotate(db: Any, *, actor: AuthContext, key_id: str, owner: Any, grace: str = "1h") -> CreatedKey:
    """New key, same scopes; the old one keeps working until `grace` (0, 1h, 24h, 7d), then is
    refused. Both are listed during the overlap (§5.3)."""
    if grace not in GRACE_CHOICES:
        raise KeyRefused("invalid_grace", 422, {"choices": sorted(GRACE_CHOICES)})
    # Locked: two concurrent rotations of one key cannot both pass the checks below.
    db.execute(text("SELECT 1 FROM identity_api_credentials WHERE id = :id FOR UPDATE"), {"id": key_id})
    old = get(db, key_id)
    if old is None or old.principal_id != owner.id or old.revoked_at is not None:
        raise KeyRefused("not_found", 404)
    now = utcnow()
    if old.grace_until is not None:
        # M4: a key already rotated (in its grace window, or past it) is never rotated again, so
        # the grace window can neither be extended nor restarted; rotate its successor instead.
        raise KeyRefused("already_rotated", 409)
    if old.suspended_at is not None or (old.expires_at is not None and old.expires_at <= now):
        raise KeyRefused("key_not_active", 409)
    created = create(db, creator=actor, owner=owner, kind=old.kind, name=old.name, scopes=old.scopes,
                     project_ids=old.project_ids, cidrs=old.cidrs, rotated_from=old.id,
                     expires_in_days=None if old.expires_at else 0)
    seconds = GRACE_CHOICES[grace]
    if seconds == 0:
        db.execute(text("UPDATE identity_api_credentials SET revoked_at = now(), revoked_by = :by "
                        "WHERE id = :id"), {"by": actor.principal.id, "id": old.id})
    else:
        db.execute(text("UPDATE identity_api_credentials SET grace_until = now() + make_interval(secs => :s) "
                        "WHERE id = :id"), {"s": seconds, "id": old.id})
    return created


def revoke(db: Any, *, actor: AuthContext, key_id: str, org_id: str,
           owner_id: Optional[str] = None) -> bool:
    """Revoke now; the next request with the key fails (A29). `owner_id` restricts to one owner's
    keys (self-service)."""
    sql = ("UPDATE identity_api_credentials k SET revoked_at = now(), revoked_by = :by FROM "
           "identity_principals p WHERE k.id = :id AND p.id = k.principal_id AND p.org_id = :org "
           "AND k.revoked_at IS NULL")
    params: dict[str, Any] = {"by": actor.principal.id, "id": key_id, "org": org_id}
    if owner_id:
        sql += " AND k.principal_id = :owner"
        params["owner"] = owner_id
    done = db.execute(text(sql + " RETURNING k.id"), params).first() is not None
    if done:
        emit(db, org_id=org_id, event_type="api_key.revoked", outcome=Outcome.SUCCESS, ctx=actor,
             target_type="api_key", target_id=key_id)
        _cascade(db, actor, key_id)
    return done


def _cascade(db: Any, actor: AuthContext, key_id: str) -> None:
    from . import delegation

    try:
        delegation.revoke_cascade(delegation.CascadeTrigger.API_KEY_REVOKED, key_id, actor=actor,
                                  reason="api key revoked", db=db)
    except NotImplementedError:
        pass  # S3's cascade; until it lands the key itself is already refused


# ---- authentication -------------------------------------------------------------------------------

def authenticate_key(db: Any, key: str, parsed: ParsedApiKey, *, ip: Optional[str],
                     user_agent: Optional[str], request_id: Optional[str]) -> AuthContext:
    """Look the key up by hash (constant-time by construction: an indexed equality on a SHA-256,
    never a prefix scan or a string compare of the secret, A27) and check it live."""
    from . import ratelimit, users

    r = db.execute(text(f"SELECT {_COLS} FROM identity_api_credentials WHERE token_hash = :th"),
                   {"th": hash_token(key)}).first()
    row = _row(r) if r else None
    now = utcnow()
    if row is None or row.kind != parsed.kind:
        if ip:
            ratelimit.hit("api_key_failure.ip", ip, limit=50, window_seconds=60, db=db)
        raise AuthnError("invalid_credentials")
    if (row.revoked_at is not None or row.suspended_at is not None
            or (row.expires_at is not None and row.expires_at <= now)
            or (row.grace_until is not None and row.grace_until <= now)):
        raise AuthnError("invalid_credentials")
    if row.cidrs:
        try:
            addr = ipaddress.ip_address((ip or "").split("%", 1)[0])
        except ValueError:
            raise AuthnError("invalid_credentials") from None
        if not any(addr in ipaddress.ip_network(c, strict=False) for c in row.cidrs):
            raise AuthnError("invalid_credentials")
    principal = users.load_principal(db, row.principal_id)
    if principal is None or not principal.active:
        raise AuthnError("invalid_credentials")
    if row.kind == "scim" and not principal.id.startswith("sys_scim_"):
        raise AuthnError("invalid_credentials")
    ctx = AuthContext(
        principal=principal, method=AuthMethod.API_KEY,
        credential=CredentialRef(row.id, row.kind, row.principal_id, frozenset(row.scopes),
                                 frozenset(row.project_ids) if row.project_ids is not None else None,
                                 row.expires_at),
        ip=ip, user_agent=user_agent, request_id=request_id)
    _record_use(db, row, ctx, ip=ip, user_agent=user_agent)
    return ctx


def _record_use(db: Any, row: KeyRow, ctx: AuthContext, *, ip: Optional[str],
                user_agent: Optional[str]) -> None:
    now = utcnow()
    new_ip = bool(ip and row.last_used_ip and row.last_used_ip != ip)
    first = row.last_used_at is None
    stale = first or (now - row.last_used_at).total_seconds() >= LAST_USE_WRITE_SECONDS
    if not (stale or new_ip):
        return
    from .sessions import _inet

    db.execute(text("UPDATE identity_api_credentials SET last_used_at = now(), "
                    "last_used_ip = CAST(:ip AS inet), last_used_ua = :ua WHERE id = :id"),
               {"ip": _inet(ip), "ua": (user_agent or "")[:400] or None, "id": row.id})
    if first:
        emit(db, org_id=ctx.principal.org_id, event_type="api_key.first_used", outcome=Outcome.INFO,
             ctx=ctx, target_type="api_key", target_id=row.id)
    elif new_ip:
        emit(db, org_id=ctx.principal.org_id, event_type="api_key.used_from_new_ip",
             outcome=Outcome.INFO, ctx=ctx, target_type="api_key", target_id=row.id,
             details={"previous_ip": row.last_used_ip})
