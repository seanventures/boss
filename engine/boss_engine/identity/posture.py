"""Identity posture (IDENTITY.md §9.1) and the console-wide banners the API owns (MeBanner).
Owned by stream S5. Read-only: nothing here changes state.

Seven checks, each {key, status: ok|warn|critical, summary, count, fix}:
  mfa_coverage, stale_api_keys, risky_delegations, legacy_credentials, orphans, audit_chain,
  public_origin (F3/H2).
`legacy_credentials` carries one row per legacy path with its principal, use count, last use and
the countdown to its cutoff, so "every legacy path shows in posture with counts and a countdown".
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from .legacy import (
    LEGACY_SHARED_PRINCIPAL,
    NETWORK_OPERATOR_PRINCIPAL,
    LegacyKind,
    is_bridge_principal,
)

COUNTDOWN_BANNER_DAYS = 14
OWNER_RECOVERED_BANNER_DAYS = 7
STALE_KEY_DAYS = 30
#: §7.3: a grant unused for this long shows as idle (never used counts from its creation).
IDLE_GRANT_DAYS = 14
NETWORK_BANNER = "Anyone who can reach this BOSS acts as the operator. Claim it to finish setup."


def _sql(statement: str):
    from sqlalchemy import text
    return text(statement)


def _iso(value: Optional[datetime]) -> Optional[str]:
    return value.isoformat() if value else None


def legacy_paths(org_id: str, *, db: Any, now: Optional[datetime] = None) -> list[dict[str, Any]]:
    now = now or datetime.now(timezone.utc)
    rows = db.execute(_sql(
        "SELECT m.legacy_kind, m.legacy_ref, m.principal_id, m.access_role_key, m.retire_after, "
        "m.first_used_at, m.last_used_at, m.use_count, m.retired_at, p.display_name, p.status "
        "FROM identity_legacy_mappings m JOIN identity_principals p ON p.id = m.principal_id "
        "WHERE m.org_id = :o AND m.legacy_kind <> 'member_id' ORDER BY m.legacy_kind, "
        "m.legacy_ref"), {"o": org_id}).mappings().all()
    out = []
    for r in rows:
        days_left = None
        if r["retire_after"] is not None and r["retired_at"] is None:
            days_left = max(0, (r["retire_after"] - now).days)
        if r["retired_at"] is not None:
            status = "ok"
        elif r["legacy_kind"] == "network_mode":
            status = "critical"     # §4.12: anyone who can reach it acts as someone; always red
        elif r["legacy_kind"] == "console_api_key" and r["retire_after"] is None:
            status = "critical"
        elif days_left is not None and days_left <= COUNTDOWN_BANNER_DAYS:
            status = "critical"
        else:
            status = "warn"
        out.append({
            "kind": r["legacy_kind"],
            "ref": r["legacy_ref"],      # a name, a member id or a key's public id; never a secret
            "principal_id": r["principal_id"], "principal": r["display_name"],
            "principal_status": r["status"], "role": r["access_role_key"],
            "use_count": int(r["use_count"] or 0), "first_used_at": _iso(r["first_used_at"]),
            "last_used_at": _iso(r["last_used_at"]), "retire_after": _iso(r["retire_after"]),
            "days_left": days_left, "retired_at": _iso(r["retired_at"]), "status": status,
        })
    return out


def _check(key: str, status: str, summary: str, count: int, fix: str) -> dict[str, Any]:
    return {"key": key, "status": status, "summary": summary, "count": count, "fix": fix}


def _scalar(db: Any, statement: str, params: dict[str, Any]) -> int:
    try:
        with db.begin_nested():
            return int(db.execute(_sql(statement), params).scalar() or 0)
    except Exception:  # noqa: BLE001 — a missing optional table is "nothing to report"
        return 0


def enforcement_check(settings: Any = None) -> dict[str, Any]:
    """Is every route enforced? Critical when the global mode or any per-router override is not
    `enforce` (shadow evaluates and records but REFUSES NOTHING; off does not even evaluate)."""
    from .enforcement import parse_router_modes

    if settings is None:
        from ..config import get_settings
        settings = get_settings()
    mode = (getattr(settings, "authz_mode", "") or "enforce").strip().lower()
    overrides = {k: v.value for k, v in parse_router_modes(
        getattr(settings, "authz_router_modes", "") or "").items() if v.value != "enforce"}
    weak = mode != "enforce" or bool(overrides)
    summary = (f"Access control is {mode.upper()}" if mode != "enforce" else "Access control is enforced")
    if overrides:
        summary += "; not enforced for " + ", ".join(f"{k} ({v})" for k, v in sorted(overrides.items()))
    return {**_check("enforcement", "critical" if weak else "ok", summary, len(overrides) + (mode != "enforce"),
                     "Set BOSS_AUTHZ_MODE=enforce and clear BOSS_AUTHZ_ROUTER_MODES once the shadow "
                     "report is empty (Identity › Posture › Shadow report)."),
            "mode": mode, "router_modes": overrides}


def posture(org_id: str, *, db: Any, now: Optional[datetime] = None) -> dict[str, Any]:
    from . import audit, migration

    now = now or datetime.now(timezone.utc)
    checks = []

    # 1. MFA coverage for the roles that require it
    rows = db.execute(_sql(
        "SELECT DISTINCT p.id, ar.key AS role FROM identity_principals p JOIN "
        "identity_role_bindings b ON b.subject_type = 'principal' AND b.subject_id = p.id JOIN "
        "identity_access_roles ar ON ar.id = b.role_id WHERE p.org_id = :o AND p.kind = 'user' "
        "AND p.status = 'active' AND NOT p.network_bound AND ar.key IN ('owner','admin','auditor') "
        "AND (b.expires_at IS NULL OR b.expires_at > :now)"), {"o": org_id, "now": now}).all()
    people = {pid for pid, _ in rows if not is_bridge_principal(pid)}
    enrolled = set()
    if people:
        enrolled = {r[0] for r in db.execute(_sql(
            "SELECT principal_id FROM identity_totp WHERE principal_id = ANY(:ids) UNION "
            "SELECT principal_id FROM identity_webauthn_credentials WHERE principal_id = "
            "ANY(:ids)"), {"ids": sorted(people)}).all()}
    missing = len(people - enrolled)
    owners_with_passkey = _scalar(db, (
        "SELECT count(DISTINCT w.principal_id) FROM identity_webauthn_credentials w JOIN "
        "identity_role_bindings b ON b.subject_id = w.principal_id AND b.role_id = 'arl_owner'"),
        {})
    checks.append({**_check(
        "mfa_coverage", "critical" if missing else "ok",
        f"{len(people) - missing} of {len(people)} Owners, Admins and Auditors have MFA"
        + ("" if owners_with_passkey or not people else "; no passkey on any Owner"),
        missing, "Ask them to enrol a passkey or TOTP (My account › Security)."),
        "total": len(people)})

    # 2. stale API keys
    stale = _scalar(db, (
        "SELECT count(*) FROM identity_api_credentials c JOIN identity_principals p ON "
        "p.id = c.principal_id WHERE p.org_id = :o AND c.revoked_at IS NULL AND c.kind <> "
        "'legacy_env' AND ((c.last_used_at IS NULL AND c.created_at < :old) OR c.last_used_at < "
        ":old OR (c.expires_at IS NOT NULL AND c.expires_at < :soon))"),
        {"o": org_id, "old": now - timedelta(days=STALE_KEY_DAYS),
         "soon": now + timedelta(days=14)})
    checks.append(_check("stale_api_keys", "warn" if stale else "ok",
                         f"{stale} API keys unused for {STALE_KEY_DAYS} days or expiring soon",
                         stale, "Revoke the ones nobody uses; rotate the rest."))

    # 3. risky delegations
    risky = _scalar(db, (
        "SELECT count(*) FROM identity_delegation_grants WHERE org_id = :o AND status = 'active' "
        "AND (expires_at IS NULL OR last_used_at < :idle OR (last_used_at IS NULL AND created_at < :idle))"),
        {"o": org_id, "idle": now - timedelta(days=IDLE_GRANT_DAYS)})
    checks.append(_check("risky_delegations", "warn" if risky else "ok",
                         f"{risky} delegations with no expiry or idle for {IDLE_GRANT_DAYS} days", risky,
                         "Review them in Identity › Agents."))

    # 4. legacy credentials still in use
    paths = legacy_paths(org_id, db=db, now=now)
    live = [p for p in paths if p["retired_at"] is None and (p["use_count"] or p["kind"] in (
        "network_mode", "console_api_key", "shared_password"))]
    pats = _scalar(db, "SELECT count(*) FROM github_connections WHERE status <> 'revoked' AND "
                       "auth_method = 'personal_access_token'", {})
    worst = "critical" if any(p["status"] == "critical" for p in live) else (
        "warn" if live or pats else "ok")
    summary = ", ".join(f"{p['kind']} ({p['use_count']} uses"
                        + (f", {p['days_left']} days left" if p["days_left"] is not None else "")
                        + ")" for p in live) or "none"
    if pats:
        summary += f"; {pats} classic GitHub PATs"
    checks.append({**_check("legacy_credentials", worst, f"Legacy credentials in use: {summary}",
                            len(live) + pats, "Invite people, move keys to service accounts, "
                            "then retire the shared paths."), "paths": paths})

    # 5. orphaned service accounts (no active owner) and agents without a passport
    orphans = _scalar(db, (
        "SELECT count(*) FROM identity_service_accounts s JOIN identity_principals sp ON "
        "sp.id = s.principal_id WHERE sp.org_id = :o AND sp.status = 'active' AND NOT EXISTS ("
        "SELECT 1 FROM identity_principals op WHERE op.id = ANY(s.owner_principal_ids) AND "
        "op.status = 'active')"), {"o": org_id})
    no_sponsor = _scalar(db, (
        "SELECT count(*) FROM identity_principals p WHERE p.org_id = :o AND p.kind = 'agent' AND "
        "p.status = 'active' AND NOT EXISTS (SELECT 1 FROM identity_agent_passports a WHERE "
        "a.agent_id = p.id)"), {"o": org_id})
    checks.append(_check("orphans", "warn" if orphans or no_sponsor else "ok",
                         f"{orphans} service accounts without an active owner; {no_sponsor} "
                         "agents without a sponsor", orphans + no_sponsor,
                         "Give each an owner or sponsor, or disable it."))

    # 6. the audit chain
    verified = audit.last_verified(org_id, db=db)
    if verified is None:
        status, text = "warn", "The audit chain has not been verified yet"
    elif not verified["ok"]:
        status, text = "critical", f"The audit chain FAILED verification at {_iso(verified['at'])}"
    elif verified["at"] < now - timedelta(days=2):
        status, text = "warn", f"The audit chain was last verified at {_iso(verified['at'])}"
    else:
        status, text = "ok", f"The audit chain verified at {_iso(verified['at'])}"
    checks.append({**_check("audit_chain", status, text, 0, "Verify it in Identity › Audit."),
                   "verified_at": _iso(verified["at"]) if verified else None})

    # 6b. the optional breach check (§4.2): on but HIBP unreachable means passwords were accepted
    #     unchecked (the bundled blocklist is still the floor)
    checks.append(breach_check(org_id, db=db))

    # 7. enforcement (H2): anything but enforce, globally or for any router, is red — shadow
    #    refuses nothing and is an upgrade-time setting only.
    checks.append(enforcement_check())
    # 8. a bound public origin (F3/H2): emailed links, passkeys and SSO need one
    checks.append(public_origin_check(org_id, db=db))
    # 9. how builds reach GitHub (Setup › Repo): the App is ok, the org token is a warning, a
    #    person-only setup or nothing at all is critical (builds cannot clone or push)
    checks.append(github_check(org_id, db=db))

    claimed = migration.claimed_at(db, org_id)
    return {"org_id": org_id, "generated_at": now.isoformat(), "claimed_at": _iso(claimed),
            "status": "critical" if any(c["status"] == "critical" for c in checks) else (
                "warn" if any(c["status"] == "warn" for c in checks) else "ok"),
            "checks": checks}


def breach_check(org_id: str, *, db: Any) -> dict[str, Any]:
    from .passwords import hibp_unreachable_since
    from .policy import get_policy

    if not get_policy(org_id, db=db).hibp_check:
        return _check("breach_check", "ok", "New passwords are checked against the bundled list of "
                      "common passwords", 0, "Turn on the breach check (Have I Been Pwned) in the "
                      "security policy to check them against known breaches too.")
    if hibp_unreachable_since():
        return _check("breach_check", "warn", "The breach check could not reach Have I Been Pwned in "
                      "the last day, so some new passwords were accepted unchecked", 1,
                      "Allow outbound HTTPS to api.pwnedpasswords.com, or turn the check off.")
    return _check("breach_check", "ok", "New passwords are checked against known breaches", 0,
                  "Nothing to do.")


def public_origin_check(org_id: str, *, db: Any) -> dict[str, Any]:
    """H2: the origin every emailed link, passkey and SSO redirect is bound to. Without one BOSS
    sends no links at all (it never builds one from a request's Host), so say so plainly."""
    from ..config import get_settings
    from .authn_support import origin_of, public_origin

    origin = public_origin(db, org_id)
    source = "env" if origin_of(get_settings().public_url or "") else ("claim" if origin else None)
    if origin is None:
        return {**_check(
            "public_origin", "critical",
            "BOSS has no public URL, so it sends no emailed links (sign-in links, password "
            "resets, invitations) and offers no passkeys or SSO", 1,
            "Set BOSS_PUBLIC_URL to the address people open BOSS at (for example "
            "https://boss.example.com) and restart, or set the public origin in Identity › Policy."),
            "origin": None, "source": None}
    return {**_check("public_origin", "ok", f"Links, passkeys and SSO use {origin}", 0,
                     "Change BOSS_PUBLIC_URL if people reach BOSS at another address."),
            "origin": origin, "source": source}


def github_check(org_id: str, *, db: Any) -> dict[str, Any]:
    from . import github_setup

    try:
        with db.begin_nested():
            m = github_setup.mode(db, org_id)
    except Exception:  # noqa: BLE001 — a missing table on an old schema: nothing connected yet
        m = {"mode": "none", "env_token": {"present": False, "read_by_builds": False}}
    fix = "Connect a GitHub App in Setup › Repo (recommended)."
    if m["mode"] == "app":
        return {**_check("github", "ok", "Builds reach GitHub through the BOSS GitHub App", 0,
                         "Nothing to do."), "github_mode": "app"}
    if m["mode"] == "token":
        return {**_check("github", "warn", "Builds reach GitHub with the org's fine-grained token (long-lived; "
                         "less safe than the App)", 1, fix), "github_mode": "token"}
    env = m.get("env_token") or {}
    if env.get("read_by_builds"):
        return {**_check("github", "warn", "Builds use BOSS_GITHUB_TOKEN from the environment (not enforced): "
                         "move it into the vault", 1, fix), "github_mode": "env"}
    if m["mode"] == "as_me":
        summary = "Only people's own GitHub connections: a build whose grant does not say \"act as me\" has no GitHub"
    elif env.get("present"):
        summary = ("BOSS_GITHUB_TOKEN is set but builds do not use it under enforce: move it into the vault "
                   "or connect a GitHub App")
    else:
        summary = "Builds have no way to reach GitHub"
    return {**_check("github", "critical", summary, 1, fix), "github_mode": m["mode"]}


def banners(ctx: Any, *, db: Any, now: Optional[datetime] = None) -> list[dict[str, Any]]:
    """MeBanner dicts for this request's principal (S1 puts them in GET /v1/identity/me)."""
    from . import migration

    now = now or datetime.now(timezone.utc)
    org_id = ctx.principal.org_id
    out: list[dict[str, Any]] = []
    claimed = migration.claimed_at(db, org_id)
    if ctx.principal.id == NETWORK_OPERATOR_PRINCIPAL or ctx.legacy == LegacyKind.NETWORK_MODE:
        m = migration.mapping(db, org_id, LegacyKind.NETWORK_MODE, "BOSS_CONSOLE_AUTH=network")
        if claimed is None:
            out.append({"kind": "network_bridge", "message": NETWORK_BANNER, "until": None})
        else:
            until = m["retire_after"] if m else None
            out.append({"kind": "network_bridge", "until": until,
                        "message": "This network sign-in ends "
                                   + (f"on {until.date().isoformat()}" if until else "soon")
                                   + ". Sign in with your own account."})
    if ctx.principal.id == LEGACY_SHARED_PRINCIPAL or ctx.legacy == LegacyKind.SHARED_PASSWORD:
        m = migration.mapping(db, org_id, LegacyKind.SHARED_PASSWORD, "BOSS_CONSOLE_PASSWORD")
        until = m["retire_after"] if m else None
        if until and until - now <= timedelta(days=COUNTDOWN_BANNER_DAYS):
            out.append({"kind": "legacy_password", "until": until,
                        "message": f"The shared password stops working in "
                                   f"{max(0, (until - now).days)} days. "
                                   "Ask an Owner to invite you."})
    if ctx.legacy in (LegacyKind.CONSOLE_API_KEY, LegacyKind.ENV_API_KEY):
        m = migration.mapping(db, org_id, LegacyKind.CONSOLE_API_KEY, "BOSS_API_KEY")
        until = m["retire_after"] if m else None
        if until and until - now <= timedelta(days=COUNTDOWN_BANNER_DAYS):
            out.append({"kind": "legacy_api_key", "until": until,
                        "message": "The shared API key stops working soon. Switch to a personal "
                                   "or service-account key."})
    recovered = db.execute(_sql(
        "SELECT ts FROM identity_security_events WHERE org_id = :o AND event_type = "
        "'owner.recovered_by_host' AND ts > :since ORDER BY seq DESC LIMIT 1"),
        {"o": org_id, "since": now - timedelta(days=OWNER_RECOVERED_BANNER_DAYS)}).first()
    if recovered and _holds_owner(db, ctx.principal.id, now):
        out.append({"kind": "owner_recovered", "until": recovered[0] + timedelta(
            days=OWNER_RECOVERED_BANNER_DAYS), "message": "An Owner account was recovered from "
                    "the host shell. Check Identity › Audit."})
    return out


def _holds_owner(db: Any, principal_id: str, now: datetime) -> bool:
    return db.execute(_sql(
        "SELECT 1 FROM identity_role_bindings WHERE subject_type = 'principal' AND subject_id = :p "
        "AND role_id = 'arl_owner' AND (expires_at IS NULL OR expires_at > :now)"),
        {"p": principal_id, "now": now}).first() is not None
