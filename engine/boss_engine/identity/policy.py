"""The per-org identity policy (identity_settings.policy). Owned by stream S5 (store, API, posture);
read by every stream. FROZEN IN PHASE 0: the keys and their defaults.

One typed view, so no stream reads raw JSON keys of its own invention. Unknown keys in the stored
JSON are ignored; a missing key takes the default below. `PUT /v1/identity/policy` (S5) validates
against this dataclass and writes `policy.changed` with the diff.
"""
from __future__ import annotations

import json
import threading
from dataclasses import asdict, dataclass, field, fields
from typing import Any, Optional


@dataclass(frozen=True)
class IdentityPolicy:
    # sign-in and sessions (§4.3, §4.6)
    mfa_required_roles: tuple[str, ...] = ("owner", "admin", "auditor")
    session_idle_minutes: int = 12 * 60
    session_absolute_hours: int = 7 * 24
    step_up_minutes: int = 10
    password_min_length: int = 12
    hibp_check: bool = False
    # email sign-in and self sign-up (§4.14, §4.15; D26, D30, D32) — all need SMTP
    email_magic_link: bool = True       # effective only when SMTP is configured
    email_code: bool = True             # effective only when SMTP is configured
    signup_enabled: bool = False
    signup_domains: tuple[str, ...] = ()
    # the public origin bound at claim when BOSS_PUBLIC_URL is unset (§4.12)
    public_origin: Optional[str] = None
    # API keys (§5.3)
    key_default_days: int = 90
    key_max_days: int = 365
    key_allow_no_expiry_for_service_accounts: bool = False
    key_auto_revoke_unused_days: Optional[int] = None
    # delegation (§7.3, §7.4)
    delegation_default_days: int = 30
    delegation_max_days: int = 90
    delegation_allow_until_revoked: bool = False
    org_max_depth: int = 3
    github_jit_credentials: bool = False
    # D28: None = automatic (on exactly when more than one active human exists)
    github_require_user_repo_access: Optional[bool] = None
    # D29: None = automatic (on exactly while one active human exists)
    single_operator_waiver: Optional[bool] = None
    # legacy bridges (D25, D27): days after the first claim
    legacy_cutoff_days: int = 30
    network_bridge_enabled: bool = True
    extra: dict[str, Any] = field(default_factory=dict)


class PolicyError(ValueError):
    """A policy write that does not validate (unknown key, wrong type, out of range)."""


#: Bounds the PUT validates (the console shows them as the input limits).
_BOUNDS: dict[str, tuple[int, int]] = {
    "session_idle_minutes": (5, 7 * 24 * 60),
    "session_absolute_hours": (1, 90 * 24),
    "step_up_minutes": (1, 60),
    "password_min_length": (8, 128),
    "key_default_days": (1, 3650),
    "key_max_days": (1, 3650),
    "key_auto_revoke_unused_days": (1, 3650),
    "delegation_default_days": (1, 365),
    "delegation_max_days": (1, 3650),
    "org_max_depth": (0, 5),
    # M3: the shared credentials end at most 30 days after the claim (an Owner's one extension
    # of at most 30 more is migration.extend_path, audited): a larger number would promise
    # something the bridges never do.
    "legacy_cutoff_days": (0, 30),
}
_ROLE_KEYS = ("owner", "admin", "operator", "viewer", "auditor")

#: (org_id, epoch) -> policy; the epoch moves on every identity mutation, so a cached policy
#: never outlives a change (S2's bump_epoch, and put_policy below).
_cache: dict[tuple[str, int], IdentityPolicy] = {}
_cache_lock = threading.Lock()


def _sql(statement: str):
    from sqlalchemy import text
    return text(statement)


def from_mapping(raw: Optional[dict[str, Any]]) -> IdentityPolicy:
    """Defaults, overlaid with the stored keys this dataclass knows; unknown keys ignored."""
    raw = dict(raw or {})
    kwargs: dict[str, Any] = {}
    for f in fields(IdentityPolicy):
        if f.name not in raw:
            continue
        value = raw[f.name]
        if f.name == "extra":
            kwargs["extra"] = dict(value or {})
        elif isinstance(getattr(IdentityPolicy(), f.name), tuple):
            kwargs[f.name] = tuple(value or ())
        else:
            kwargs[f.name] = value
    try:
        return IdentityPolicy(**kwargs)
    except TypeError:
        return IdentityPolicy()


def to_mapping(policy: IdentityPolicy) -> dict[str, Any]:
    out = asdict(policy)
    for k, v in list(out.items()):
        if isinstance(v, tuple):
            out[k] = list(v)
    return out


def _read(org_id: str, db: Any) -> tuple[dict[str, Any], int]:
    row = db.execute(_sql("SELECT policy, epoch FROM identity_settings WHERE org_id = :o"),
                     {"o": org_id}).first()
    if not row:
        return {}, 0
    stored = row[0] if isinstance(row[0], dict) else json.loads(row[0] or "{}")
    return stored, int(row[1] or 0)


def get_policy(org_id: str, *, db: Any) -> IdentityPolicy:
    """The org's policy with defaults filled in. Cached per identity epoch."""
    if db is None:
        from ..db import begin
        with begin() as conn:
            return get_policy(org_id, db=conn)
    epoch_row = db.execute(_sql("SELECT epoch FROM identity_settings WHERE org_id = :o"),
                           {"o": org_id}).first()
    epoch = int(epoch_row[0]) if epoch_row else -1
    key = (org_id, epoch)
    with _cache_lock:
        cached = _cache.get(key)
    if cached is not None and epoch >= 0:
        return cached
    stored, _ = _read(org_id, db)
    policy = from_mapping(stored)
    if epoch >= 0:
        with _cache_lock:
            for stale in [k for k in _cache if k[0] == org_id]:
                _cache.pop(stale, None)
            _cache[key] = policy
    return policy


def validate_changes(changes: dict[str, Any]) -> dict[str, Any]:
    """Check a PUT body against the dataclass: known keys, right types, sane ranges."""
    if not isinstance(changes, dict):
        raise PolicyError("policy changes must be an object")
    defaults = IdentityPolicy()
    known = {f.name: f for f in fields(IdentityPolicy)}
    clean: dict[str, Any] = {}
    for key, value in changes.items():
        if key not in known:
            raise PolicyError(f"unknown policy key {key!r}")
        default = getattr(defaults, key)
        if key == "extra":
            if not isinstance(value, dict):
                raise PolicyError("extra must be an object")
            clean[key] = dict(value)
            continue
        if key == "public_origin":
            if value is not None and not (isinstance(value, str)
                                          and value.startswith("https://") and len(value) < 300):
                raise PolicyError("public_origin must be an https:// origin")
            clean[key] = value.rstrip("/") if isinstance(value, str) else None
            continue
        if isinstance(default, bool) or key in ("github_require_user_repo_access",
                                                "single_operator_waiver"):
            if not (isinstance(value, bool) or (value is None and default is None)):
                raise PolicyError(f"{key} must be true or false")
            clean[key] = value
            continue
        if isinstance(default, tuple):
            if not isinstance(value, (list, tuple)) or not all(isinstance(v, str) for v in value):
                raise PolicyError(f"{key} must be a list of strings")
            if key == "mfa_required_roles" and any(v not in _ROLE_KEYS for v in value):
                raise PolicyError(f"mfa_required_roles holds role keys only: {_ROLE_KEYS}")
            clean[key] = tuple(value)
            continue
        if isinstance(default, int) or key == "key_auto_revoke_unused_days":
            if value is None and key == "key_auto_revoke_unused_days":
                clean[key] = None
                continue
            if isinstance(value, bool) or not isinstance(value, int):
                raise PolicyError(f"{key} must be a whole number")
            low, high = _BOUNDS.get(key, (0, 10**6))
            if not low <= value <= high:
                raise PolicyError(f"{key} must be between {low} and {high}")
            clean[key] = value
            continue
        raise PolicyError(f"{key} cannot be set here")
    return clean


def put_policy(org_id: str, changes: dict[str, Any], *, db: Any, actor: Any) -> IdentityPolicy:
    """Validate, store, bump the identity epoch and write `policy.changed` with the diff, all in
    the caller's transaction (the event and the change commit together, A76). `actor` is the
    request's AuthContext (or, from the host CLI, a SecurityEvent-ready principal id)."""
    from . import audit

    clean = validate_changes(changes)
    stored, _ = _read(org_id, db)
    before = from_mapping(stored)
    after_map = to_mapping(before)
    after_map.update({k: (list(v) if isinstance(v, tuple) else v) for k, v in clean.items()})
    after = from_mapping(after_map)
    diff = {k: {"from": to_mapping(before)[k], "to": to_mapping(after)[k]}
            for k in clean if to_mapping(before)[k] != to_mapping(after)[k]}
    if not diff:
        return before
    merged = dict(stored)
    merged.update({k: to_mapping(after)[k] for k in diff})
    db.execute(_sql(
        "INSERT INTO identity_settings (org_id, policy, epoch, updated_by, updated_at) "
        "VALUES (:o, CAST(:p AS JSONB), 1, :by, now()) ON CONFLICT (org_id) DO UPDATE SET "
        "policy = EXCLUDED.policy, epoch = identity_settings.epoch + 1, "
        "updated_by = EXCLUDED.updated_by, updated_at = now()"),
        {"o": org_id, "p": json.dumps(merged, default=str), "by": _actor_id(actor)})
    audit.record(_event(org_id, actor, "policy.changed", {"diff": _safe_diff(diff)}), db=db)
    if "legacy_cutoff_days" in diff:
        from . import migration
        migration.reschedule_cutoffs(org_id, db=db)
    return after


def _safe_diff(diff: dict[str, Any]) -> dict[str, Any]:
    return json.loads(json.dumps(diff, default=str))


def _actor_id(actor: Any) -> Optional[str]:
    principal = getattr(actor, "principal", None)
    if principal is not None:
        return principal.id
    return str(actor) if actor else None


def _event(org_id: str, actor: Any, event_type: str, details: dict[str, Any]):
    from .audit import Outcome, SecurityEvent
    from .principal import AuthContext

    if isinstance(actor, AuthContext):
        return SecurityEvent.from_context(actor, event_type, Outcome.SUCCESS,
                                          target_type="policy", target_id=org_id, details=details)
    return SecurityEvent(org_id=org_id, event_type=event_type, outcome=Outcome.SUCCESS,
                         actor_principal_id=_actor_id(actor),
                         actor_kind="system" if str(actor or "").startswith("sys_") else None,
                         target_type="policy", target_id=org_id, details=details)


def set_extra(org_id: str, key: str, value: Any, *, db: Any) -> None:
    """Internal bookkeeping in policy.extra (rollout state, shadow start). Not an operator
    setting: no event, no epoch bump; never a secret."""
    db.execute(_sql(
        "INSERT INTO identity_settings (org_id, policy) VALUES (:o, jsonb_build_object('extra', "
        "jsonb_build_object(CAST(:k AS TEXT), CAST(:v AS JSONB)))) ON CONFLICT (org_id) DO UPDATE "
        "SET policy = jsonb_set(CASE WHEN jsonb_typeof(identity_settings.policy->'extra') = "
        "'object' THEN identity_settings.policy ELSE identity_settings.policy || "
        "'{\"extra\": {}}'::jsonb END, ARRAY['extra', CAST(:k AS TEXT)], CAST(:v AS JSONB))"),
        {"o": org_id, "k": key, "v": json.dumps(value, default=str)})
    with _cache_lock:     # no epoch bump for bookkeeping, so drop this process's copy
        for stale in [k for k in _cache if k[0] == org_id]:
            _cache.pop(stale, None)
