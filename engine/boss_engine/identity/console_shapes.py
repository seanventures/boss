"""GET/PUT /v1/identity/policy and GET /v1/identity/posture in the shapes the console reads
(S4's `SecurityPolicy` and `Posture`, apps/web/lib/identity/types.ts). Owned by stream S5.

The console saves the policy by PUTting back the whole object it read. So a PUT applies only the
fields that DIFFER from what GET returns now. That keeps "automatic" settings automatic when the
console round-trips them, and makes a stale read unable to undo a field it did not touch.

Where S4's shape would weaken a rule, the API keeps its own (all listed for S4 in the S5 report):
  - `network_mode`: `active: false` ends the network bridge now ("Require sign-in": the claim
    must have happened; audited). `active: true` is refused: network trust never comes back.
  - `smtp`: read only (it says whether email is configured; it is not a switch).
  - `github_require_user_repo_access`: GET returns the EFFECTIVE value and
    `github_require_user_repo_access_auto` (D28: automatic = on exactly when more than one active
    person exists). A PUT that changes it pins it; `null` returns it to automatic.
  - `public_origin`: https only. Any key the API does not know is refused (no mass assignment).
GET also returns every policy key S4's shape lacks (step_up_minutes, email_magic_link,
email_code, key_default_days, delegation_default_days, legacy_cutoff_days, ...); a PUT accepts
them too.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Optional

from . import audit_view, migration, posture
from .legacy import NETWORK_OPERATOR_PRINCIPAL, LegacyKind, is_bridge_principal
from .policy import PolicyError, get_policy, put_policy, to_mapping

#: S4 field -> IdentityPolicy key (same value)
_RENAMED = {
    "breach_check": "hibp_check",
    "key_no_expiry_for_service_accounts": "key_allow_no_expiry_for_service_accounts",
    "delegation_max_depth": "org_max_depth",
    "delegation_until_revoked": "delegation_allow_until_revoked",
}
_SAME = ("mfa_required_roles", "session_idle_minutes", "session_absolute_hours",
         "password_min_length", "key_max_days", "delegation_max_days", "public_origin")
#: IdentityPolicy keys S4's shape does not carry; served and accepted as-is
_EXTRA = ("step_up_minutes", "email_magic_link", "email_code", "key_default_days",
          "key_auto_revoke_unused_days", "delegation_default_days", "github_jit_credentials",
          "single_operator_waiver", "legacy_cutoff_days", "network_bridge_enabled")
READ_ONLY = ("smtp", "github_require_user_repo_access_auto")
NETWORK_REF = "BOSS_CONSOLE_AUTH=network"


class PolicyRefused(PolicyError):
    """A change the API will not make whatever the caller holds (409, not 422)."""


def _sql(statement: str):
    from sqlalchemy import text
    return text(statement)


def _iso(value: Optional[datetime]) -> Optional[str]:
    return value.isoformat() if value else None


def active_people(org_id: str, *, db: Any) -> int:
    rows = db.execute(_sql("SELECT id FROM identity_principals WHERE org_id = :o AND kind = 'user' "
                           "AND status = 'active' AND NOT network_bound"), {"o": org_id}).all()
    return sum(1 for (pid,) in rows if not is_bridge_principal(pid))


def smtp_configured(db: Any, settings: Any = None) -> bool:
    if settings is None:
        from ..config import get_settings
        settings = get_settings()
    if getattr(settings, "email_smtp_host", None):
        return True
    try:
        from ..services import email_config
        project = getattr(settings, "default_project_id", None) or "boss-dogfood"
        with db.begin_nested():
            return bool(email_config.describe(db, project).get("host"))
    except Exception:  # noqa: BLE001 — no table, no row: no SMTP
        return False


def network_mode(org_id: str, *, db: Any, now: Optional[datetime] = None) -> dict[str, Any]:
    now = now or datetime.now(timezone.utc)
    m = migration.mapping(db, org_id, LegacyKind.NETWORK_MODE, NETWORK_REF)
    if m is None or m["retired_at"] is not None:
        return {"active": False, "ends_at": None}
    status = db.execute(_sql("SELECT status FROM identity_principals WHERE id = :p"),
                        {"p": NETWORK_OPERATOR_PRINCIPAL}).scalar()
    ends = m["retire_after"]
    live = status == "active" and (ends is None or ends > now)
    return {"active": bool(live), "ends_at": _iso(ends) if live else None}


def policy_view(org_id: str, *, db: Any, settings: Any = None,
                now: Optional[datetime] = None) -> dict[str, Any]:
    """S4's SecurityPolicy, plus the keys it lacks."""
    p = to_mapping(get_policy(org_id, db=db))
    out: dict[str, Any] = {k: p[k] for k in _SAME}
    out.update({s4: p[mine] for s4, mine in _RENAMED.items()})
    auto = p["github_require_user_repo_access"] is None
    out["github_require_user_repo_access"] = (active_people(org_id, db=db) > 1) if auto \
        else bool(p["github_require_user_repo_access"])
    out["github_require_user_repo_access_auto"] = auto
    out["network_mode"] = network_mode(org_id, db=db, now=now)
    out["signup"] = {"enabled": p["signup_enabled"], "domains": list(p["signup_domains"])}
    out["smtp"] = smtp_configured(db, settings)
    out.update({k: p[k] for k in _EXTRA})
    return out


def apply_policy_put(org_id: str, body: Any, *, db: Any, ctx: Any, settings: Any = None,
                     now: Optional[datetime] = None) -> dict[str, Any]:
    """Apply the fields of `body` that differ from the current view. Raises PolicyError (422)
    for unknown keys or bad values and PolicyRefused (409) for refused changes."""
    if not isinstance(body, dict):
        raise PolicyError("policy must be an object")
    current = policy_view(org_id, db=db, settings=settings, now=now)
    known = set(current) | {"extra"}
    unknown = sorted(set(body) - known)
    if unknown or "extra" in body:
        raise PolicyError(f"unknown policy key {(unknown or ['extra'])[0]!r}")
    changes: dict[str, Any] = {}
    end_network = False
    for key, value in body.items():
        if value == current.get(key) and key != "github_require_user_repo_access":
            continue
        if key in READ_ONLY:
            raise PolicyError(f"{key} is read only")
        if key in _SAME or key in _EXTRA:
            changes[key] = value
        elif key in _RENAMED:
            changes[_RENAMED[key]] = value
        elif key == "signup":
            if not isinstance(value, dict) or set(value) - {"enabled", "domains"}:
                raise PolicyError("signup is {enabled, domains}")
            if value.get("enabled", current["signup"]["enabled"]) != current["signup"]["enabled"]:
                changes["signup_enabled"] = value.get("enabled")
            if "domains" in value and list(value["domains"] or []) != current["signup"]["domains"]:
                changes["signup_domains"] = value["domains"]
        elif key == "github_require_user_repo_access":
            pinned = to_mapping(get_policy(org_id, db=db))["github_require_user_repo_access"]
            if value is None and pinned is not None:
                changes[key] = None                       # back to automatic (D28)
            elif value is not None and value != current[key]:
                changes[key] = value
        elif key == "network_mode":
            if not isinstance(value, dict) or "active" not in value:
                raise PolicyError("network_mode is {active, ends_at}")
            if value["active"] and not current["network_mode"]["active"]:
                raise PolicyRefused("network trust cannot be turned back on; sign in instead")
            if not value["active"] and current["network_mode"]["active"]:
                end_network = True
    if changes:
        put_policy(org_id, changes, db=db, actor=ctx)
    if end_network:
        if migration.claimed_at(db, org_id) is None:
            raise PolicyRefused("claim the first Owner before requiring sign-in")
        migration.retire_path(org_id, LegacyKind.NETWORK_MODE, db=db, now=now,
                              actor_principal_id=ctx.principal.id, actor_ctx=ctx,
                              reason="ended by an Owner (Require sign-in)")
    return policy_view(org_id, db=db, settings=settings, now=now)


# ---- posture ------------------------------------------------------------------------------------

_STATUS = {"ok": "ok", "warn": "warn", "critical": "bad"}
_CHECK = {  # my check key -> (S4 id, label, fix label, href)
    "mfa_coverage": ("mfa", "MFA coverage", "Find who has none", "/identity/people?mfa=none"),
    "stale_api_keys": ("keys", "Stale keys", "Review keys", "/identity/service-accounts"),
    "risky_delegations": ("delegations", "Risky delegations", "Review grants",
                          "/identity/agents/delegations"),
    "legacy_credentials": ("legacy", "Legacy paths", "Retire them", "/identity/sign-in#legacy"),
    "orphans": ("owners", "Owners & sponsors", "Assign owners", "/identity/people"),
    "audit_chain": ("audit", "Audit chain", "Verify now", "/identity/audit?verify=1"),
    # F3/H2: without a bound public origin BOSS sends no emailed links and offers no passkeys/SSO
    "public_origin": ("origin", "Public URL", "Set BOSS_PUBLIC_URL", "/identity/sign-in#public-url"),
}
_LEGACY_LABEL = {
    "shared_password": ("legacy_password", "Shared console password"),
    "console_api_key": ("legacy_api_key", "BOSS_API_KEY"),
    "network_mode": ("network_bridge", "Network-trusted access"),
    "env_api_key": ("legacy_env_key", "Env API key"),
    "oidc_bearer": ("legacy_oidc_bearer", "OIDC bearer callers"),
    "scim_bearer": ("legacy_scim_token", "SCIM bearer token"),
}


def _count(db: Any, statement: str, params: dict[str, Any]) -> int:
    try:
        with db.begin_nested():
            return int(db.execute(_sql(statement), params).scalar() or 0)
    except Exception:  # noqa: BLE001
        return 0


def _value(check: dict[str, Any], key: str) -> tuple[str, Optional[str]]:
    if key == "audit_chain":
        return ("✓" if check["status"] == "ok" else "!" if check["status"] == "critical"
                else "?"), None
    if key == "mfa_coverage":
        total = check.get("total", 0)
        return str(total - check["count"]), f"of {total}"
    if key == "orphans":
        return str(check["count"]), "issues"
    return str(check["count"]), None


def posture_view(org_id: str, *, db: Any, now: Optional[datetime] = None) -> dict[str, Any]:
    """S4's Posture: score, headline, summary, six checks, counts, who is acting now, the audit
    chain and the legacy paths with their dates."""
    now = now or datetime.now(timezone.utc)
    base = posture.posture(org_id, db=db, now=now)
    checks = []
    for c in base["checks"]:
        sid, label, fix_label, href = _CHECK[c["key"]]
        value, unit = _value(c, c["key"])
        status = _STATUS[c["status"]]
        checks.append({"id": sid, "label": label, "status": status, "value": value, "unit": unit,
                       "detail": c["summary"],
                       "fix": None if status == "ok" and sid != "audit" else
                       {"label": fix_label, "href": href}})
    bad = sum(1 for c in checks if c["status"] == "bad")
    warn = sum(1 for c in checks if c["status"] == "warn")
    score = max(0, 100 - 15 * bad - 5 * warn)
    word = "good" if not bad else "needs attention" if bad == 1 else "at risk"
    to_fix = bad + warn
    chain = audit_view.chain_status(org_id, db=db)
    paths = [p for p in posture.legacy_paths(org_id, db=db, now=now) if p["retired_at"] is None]
    shown = [p for p in paths if p["kind"] not in ("env_api_key", "oidc_bearer")
             or p["use_count"]]
    legacy = []
    seen = set()
    for p in shown:
        kind, label = _LEGACY_LABEL.get(p["kind"], (p["kind"], p["kind"]))
        if (kind, p["retire_after"]) in seen:
            continue
        seen.add((kind, p["retire_after"]))
        legacy.append({"kind": kind, "label": label, "until": p["retire_after"]})
    mfa = next(c for c in base["checks"] if c["key"] == "mfa_coverage")
    summary = [mfa["summary"],
               f"{len(legacy)} legacy path{'s' if len(legacy) != 1 else ''} still in use",
               next(c for c in base["checks"] if c["key"] == "audit_chain")["summary"]]
    return {
        "score": score,
        "headline": f"Posture: {word}" + (f", {to_fix} thing{'s' if to_fix != 1 else ''} to fix"
                                          if to_fix else ""),
        "summary": summary,
        "checks": checks,
        "counts": counts(org_id, db=db, now=now),
        "acting_now": acting_now(org_id, db=db, now=now),
        "audit": {"verified_at": chain["verified_at"], "seq": chain["seq"], "ok": chain["ok"]},
        "legacy": legacy,
        "status": base["status"],
        "claimed_at": base["claimed_at"],
        "generated_at": base["generated_at"],
    }


def counts(org_id: str, *, db: Any, now: datetime) -> dict[str, int]:
    people = db.execute(_sql(
        "SELECT p.id, p.status, (u.principal_id IS NOT NULL) AS has_user FROM identity_principals "
        "p LEFT JOIN identity_users u ON u.principal_id = p.id WHERE p.org_id = :o AND "
        "p.kind = 'user' AND NOT p.network_bound"), {"o": org_id}).all()
    real = [r for r in people if not is_bridge_principal(r[0])]
    # placeholders for legacy members are pending with no account: not people yet
    accounts = [r for r in real if r[2]]
    params = {"o": org_id, "now": now}
    return {
        "people": sum(1 for r in accounts if r[1] != "pending"),
        "active": sum(1 for r in accounts if r[1] == "active"),
        "invited": _count(db, "SELECT count(*) FROM identity_invitations WHERE org_id = :o AND "
                              "accepted_at IS NULL AND revoked_at IS NULL AND expires_at > :now",
                          params),
        "disabled": sum(1 for r in accounts if r[1] == "disabled"),
        "agents": _count(db, "SELECT count(*) FROM identity_principals WHERE org_id = :o AND "
                             "kind = 'agent' AND status <> 'revoked'", params),
        "suspended_agents": _count(db, "SELECT count(*) FROM identity_principals WHERE org_id = :o "
                                       "AND kind = 'agent' AND status = 'suspended'", params),
        "groups": _count(db, "SELECT count(*) FROM identity_groups WHERE org_id = :o", params),
        "roles_builtin": 5,
        "roles_custom": _count(db, "SELECT count(*) FROM identity_access_roles WHERE org_id = :o "
                                   "AND NOT builtin AND NOT system", params),
        "service_accounts": _count(db, "SELECT count(*) FROM identity_principals WHERE org_id = :o "
                                       "AND kind = 'service_account' AND status = 'active'",
                                   params),
        "active_grants": _count(db, "SELECT count(*) FROM identity_delegation_grants WHERE "
                                    "org_id = :o AND status = 'active' AND (expires_at IS NULL OR "
                                    "expires_at > :now)", params),
        "runs_acting": _count(db, "SELECT count(DISTINCT run_id) FROM identity_run_credentials "
                                  "WHERE org_id = :o AND revoked_at IS NULL AND expires_at > :now",
                              params),
        "tokens_out": _count(db, "SELECT count(*) FROM identity_run_credentials WHERE org_id = :o "
                                 "AND revoked_at IS NULL AND expires_at > :now", params),
    }


def acting_now(org_id: str, *, db: Any, now: datetime, limit: int = 4) -> list[dict[str, Any]]:
    """Grants with a live run token right now: who, which agents, where."""
    try:
        with db.begin_nested():
            rows = db.execute(_sql(
                "SELECT g.id, g.user_principal_id, g.agent_ids, g.project_ids, g.task_run_id, "
                "g.expires_at FROM identity_delegation_grants g WHERE g.org_id = :o AND "
                "g.status = 'active' AND EXISTS (SELECT 1 FROM identity_run_credentials r WHERE "
                "r.grant_id = g.id AND r.revoked_at IS NULL AND r.expires_at > :now) ORDER BY "
                "g.last_used_at DESC NULLS LAST LIMIT :n"),
                {"o": org_id, "now": now, "n": limit}).mappings().all()
    except Exception:  # noqa: BLE001
        return []
    ids = set()
    for r in rows:
        ids.add(r["user_principal_id"])
        ids.update(r["agent_ids"] or [])
    names = audit_view._names(db, ids)
    out = []
    for r in rows:
        user = r["user_principal_id"]
        if r["task_run_id"]:
            label = "this build only"
        elif r["expires_at"]:
            label = f"{max(1, round((r['expires_at'] - now).total_seconds() / 86400))}d"
        else:
            label = "until revoked"
        out.append({"user": {"id": user, "name": names.get(user, (user, None))[0],
                             "hue": audit_view.hue(user)},
                    "agents": [{"id": a, "name": names.get(a, (a, None))[0]}
                               for a in (r["agent_ids"] or [])],
                    "project": (r["project_ids"] or [""])[0], "label": label})
    return out
