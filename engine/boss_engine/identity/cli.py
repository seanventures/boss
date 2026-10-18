"""`python -m boss_engine.identity.cli …` — the host-shell identity commands (IDENTITY.md §4.7, §8,
§12). Owned by stream S5.

    recover-owner --email E [--origin URL]   the sole Owner is locked out: make E an active Owner,
                                             remove their MFA and passkeys, end their sessions,
                                             revoke their API keys, print a 15-minute
                                             single-use link. Actor: sys_host_cli. Audited.
    migrate                                  run the M0 mapping now (it also runs by itself)
    legacy status | legacy retire --path P   the legacy bridges, their counts and countdowns
    audit verify | audit export --out F | audit prune --older-than-days N
    shadow-report [--json]                   the per-router shadow -> enforce report
    posture                                  the six posture checks
    claim-status                             one line for `boss doctor`: claimed or not
    self-installer-key [--quiet]             mint the host self-installer's boss_sak_ key
                                             (sys_self_installer; boss self-fix enroll keeps it)
    egress-key [--revoke-others]             mint the egress service's boss_sak_ key (sys_egress),
                                             shown once; it authenticates /v1/identity/introspect
    setup-link                               a new single-use claim link (1 hour), while unclaimed
    host-key [--quiet] [--keep-others]       mint the host tools' key (service account "Host
                                             tools"; boss.sh keeps it in .env as BOSS_HOST_API_KEY)
    mcp-key --project P [--permission X]...  mint one project's MCP key (service account "MCP
             [--quiet]                       (P)"; make mcp-setup keeps it as BOSS_MCP_API_KEY)

HOST SHELL ONLY, by construction: these functions are reached from no HTTP route and no MCP tool
(test_identity_audit_recovery_is_host_only pins it). Run them where the database is:

    docker compose -f scripts/install/community-compose.yml --env-file .env exec -u boss api \
        python -m boss_engine.identity.cli recover-owner --email you@example.com

or, from any directory on the host, `boss identity recover-owner --email you@example.com`.

Nothing here prints a secret except a recovery or setup link, or a key the host shell asked for,
to the terminal that asked for it.
"""
from __future__ import annotations

import argparse
import json
import secrets
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from . import links

HOST_CLI_PRINCIPAL = "sys_host_cli"
RECOVERY_TTL = timedelta(minutes=15)


class RecoveryError(RuntimeError):
    pass


@dataclass(frozen=True)
class Recovery:
    principal_id: str
    link: str
    expires_at: datetime
    reactivated: bool
    owner_bound: bool
    sessions_revoked: int = 0
    api_keys_revoked: int = 0
    passkeys_removed: int = 0


def _sql(statement: str):
    from sqlalchemy import text
    return text(statement)


def _origin(db: Any, org_id: str, explicit: Optional[str]) -> str:
    import os
    from .policy import get_policy
    if explicit:
        return explicit.rstrip("/")
    env = os.environ.get("BOSS_PUBLIC_URL", "").strip()
    if env:
        return env.rstrip("/")
    from .authn_support import local_console_origin
    return (get_policy(org_id, db=db).public_origin or local_console_origin()).rstrip("/")


def recover_owner(email: str, *, db: Any, origin: Optional[str] = None,
                  now: Optional[datetime] = None) -> Recovery:
    """Host-shell break-glass (§4.7, §6.12). Recovery ASSUMES COMPROMISE (A-2): in ONE
    transaction the person becomes an active Owner, and EVERY credential they hold is ended —
    their TOTP, passkeys and unused recovery codes are removed, all their sessions end and all
    their personal API keys are revoked — then a 15-minute single-use link is minted (only its
    hash is stored), and `owner.recovered_by_host` is written with actor sys_host_cli and the
    counts. Every Owner then sees a banner for 7 days (posture.banners)."""
    from . import audit
    from .audit import Outcome, SecurityEvent
    from .legacy import is_bridge_principal
    from .migration import bind, bump_epoch, ensure_principal, token_hash

    now = now or datetime.now(timezone.utc)
    row = db.execute(_sql(
        "SELECT p.id, p.org_id, p.kind, p.status, p.network_bound FROM identity_users u JOIN "
        "identity_principals p ON p.id = u.principal_id WHERE lower(u.email) = lower(:e)"),
        {"e": (email or "").strip()}).mappings().first()
    if row is None:
        raise RecoveryError("no person has that email on this install")
    if row["kind"] != "user" or row["network_bound"] or is_bridge_principal(row["id"]):
        raise RecoveryError("only a real person can be recovered as an Owner")
    if row["status"] == "revoked":
        raise RecoveryError("that person was deleted; invite them again")
    pid, org_id = row["id"], row["org_id"]
    ensure_principal(db, HOST_CLI_PRINCIPAL, org_id, "system", "active", "BOSS host CLI")
    reactivated = row["status"] != "active"
    if reactivated:
        db.execute(_sql("UPDATE identity_principals SET status = 'active', status_changed_at = "
                        ":now, status_changed_by = :by, status_reason = 'recovered by host', "
                        "updated_at = now() WHERE id = :p"),
                   {"now": now, "by": HOST_CLI_PRINCIPAL, "p": pid})
    had_owner = db.execute(_sql(
        "SELECT 1 FROM identity_role_bindings WHERE subject_type = 'principal' AND subject_id = :p "
        "AND role_id = 'arl_owner' AND scope_type = 'org' AND (expires_at IS NULL OR expires_at > "
        ":now)"), {"p": pid, "now": now}).first() is not None
    bind(db, org_id, pid, "arl_owner", "org", org_id, granted_by=HOST_CLI_PRINCIPAL,
         reason="recovered by host shell (boss identity recover-owner)")
    db.execute(_sql("DELETE FROM identity_totp WHERE principal_id = :p"), {"p": pid})
    # A-2: a passkey an intruder registered would still sign in (a passkey is a full MFA
    # session), and a lost one would dead-end the recovery at an MFA step asking for it.
    passkeys_removed = len(db.execute(_sql(
        "DELETE FROM identity_webauthn_credentials WHERE principal_id = :p RETURNING 1"),
        {"p": pid}).all())
    # F3/H1: the host shell is proof enough: the next sign-in may enrol a FIRST second factor
    # (a later password-only sign-in never can).
    from .signin import grant_first_factor_after_reset

    grant_first_factor_after_reset(db, pid)
    db.execute(_sql("UPDATE identity_recovery_codes SET used_at = :now WHERE principal_id = :p "
                    "AND used_at IS NULL"), {"now": now, "p": pid})
    sessions_revoked = len(db.execute(_sql(
        "UPDATE identity_sessions SET revoked_at = :now, revoked_by = :by, "
        "revoked_reason = 'owner recovered by host' WHERE principal_id = :p AND "
        "revoked_at IS NULL RETURNING id"), {"now": now, "by": HOST_CLI_PRINCIPAL, "p": pid}).all())
    # A-2: every personal API key, through api_keys.revoke (its event and delegation cascade).
    from . import api_keys
    from .principal import AuthContext, AuthMethod, Principal, PrincipalKind, PrincipalStatus

    actor = AuthContext(Principal(HOST_CLI_PRINCIPAL, PrincipalKind.SYSTEM, org_id,
                                  PrincipalStatus.ACTIVE, "BOSS host CLI"), AuthMethod.SYSTEM)
    key_ids = [r[0] for r in db.execute(_sql(
        "SELECT id FROM identity_api_credentials WHERE principal_id = :p AND revoked_at IS NULL"),
        {"p": pid}).all()]
    api_keys_revoked = sum(1 for k in key_ids if api_keys.revoke(db, actor=actor, key_id=k, org_id=org_id))
    token = secrets.token_urlsafe(32)
    expires = now + RECOVERY_TTL
    # The binding column carries the host marker (never a SHA-256, so never a browser binding):
    # only this link is the Owner break-glass under Enforce SSO (email_signin.reset_password).
    from .email_signin import HOST_MINTED_BINDING

    db.execute(_sql("INSERT INTO identity_auth_challenges (id_hash, kind, principal_id, "
                    "binding_hash, expires_at) VALUES (:h, 'password_reset', :p, :b, :exp)"),
               {"h": token_hash(token), "p": pid, "b": HOST_MINTED_BINDING, "exp": expires})
    audit.record(SecurityEvent(
        org_id=org_id, event_type="owner.recovered_by_host", outcome=Outcome.SUCCESS,
        actor_principal_id=HOST_CLI_PRINCIPAL, actor_kind="system", target_type="principal",
        target_id=pid, details={"reactivated": reactivated, "owner_bound": not had_owner,
                                "mfa_reset": True, "assumes_compromise": True,
                                "sessions_revoked": sessions_revoked,
                                "personal_keys_revoked": api_keys_revoked,
                                "passkeys_removed": passkeys_removed,
                                "link_expires_at": expires.isoformat()}),
        db=db)
    bump_epoch(db, org_id, "owner recovered by host")
    # the token travels in the fragment: never in a request line the API or a proxy logs (A88)
    link = links.url("owner_recovery", _origin(db, org_id, origin), token)
    return Recovery(pid, link, expires, reactivated, not had_owner, sessions_revoked,
                    api_keys_revoked, passkeys_removed)


def recovery_summary(done: Recovery) -> str:
    """What `recover-owner` prints: recovery assumes compromise, so it says what it ended."""
    return (f"Recovered {done.principal_id} as an Owner. Recovery assumes the account was "
            f"compromised, so everything that signed in as them was ended: their authenticator app, "
            f"{done.passkeys_removed} passkey(s) and their recovery codes were removed, "
            f"{done.sessions_revoked} session(s) ended and {done.api_keys_revoked} personal API "
            f"key(s) were revoked (mint new keys after signing in).\n"
            f"Open this link within 15 minutes (single use):\n\n  {done.link}\n\n"
            f"Every Owner will see a banner about this for 7 days.")


def claim_status(org_id: str, *, db: Any) -> str:
    """For `boss doctor` (scripts/install/checkup.sh): `unclaimed`, or
    `claimed <when>; shared credentials stop <when>` (never a secret)."""
    from .legacy import LegacyKind
    from .migration import claimed_at, mapping
    when = claimed_at(db, org_id)
    if when is None:
        return "unclaimed"
    cutoffs = [m["retire_after"] for m in (
        mapping(db, org_id, k, r) for k, r in ((LegacyKind.SHARED_PASSWORD, "BOSS_CONSOLE_PASSWORD"),
                                               (LegacyKind.CONSOLE_API_KEY, "BOSS_API_KEY"),
                                               (LegacyKind.NETWORK_MODE,
                                                "BOSS_CONSOLE_AUTH=network")))
        if m and m["retired_at"] is None and m["retire_after"]]
    tail = (f"; shared credentials stop {min(cutoffs).date().isoformat()}" if cutoffs
            else "; shared credentials retired")
    return f"claimed {when.date().isoformat()}{tail}"


# ---- the command line ----------------------------------------------------------------------------

def _print(value: Any, as_json: bool = True) -> None:
    print(json.dumps(value, default=str, indent=2, sort_keys=True) if as_json else value)


def rotate_master_key(new_key_file: str, *, db: Any) -> str:
    """`boss identity rotate-master-key --new-key-file F` (IDENTITY.md §7.8): re-wrap every DEK under
    the key in F in this ONE transaction (locked, audited as vault.master_key_rotated). The caller's
    transaction commits; THEN the configured key file is replaced with F's key (atomically), so a
    crash in between leaves a database under the new key and an old file — which boot refuses —
    and F still holds the key to restore. Prints no key."""
    import base64
    import os

    from . import vault

    try:
        raw = open(new_key_file, encoding="utf-8").read().strip()
        new_key = base64.b64decode(raw, validate=True)
    except (OSError, ValueError) as exc:
        raise ValueError("the new key file is unreadable or not base64") from exc
    if len(new_key) != 32:
        raise ValueError("the new key must be 32 bytes")
    count = vault.rotate_master_key(new_key, db=db, actor_principal_id=HOST_CLI_PRINCIPAL)
    target = (os.environ.get(vault.MASTER_KEY_FILE_ENV) or "").strip()

    def _swap() -> None:
        if target:
            tmp = target + ".rotating"
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(raw + "\n")
            os.replace(tmp, target)
        vault.reset_master_key_cache()

    from sqlalchemy import event

    event.listen(db, "commit", lambda *_: _swap(), once=True)
    where = (f"the key file {target} is replaced once this commits" if target else
             "now set BOSS_MASTER_KEY to the new key and restart every BOSS process")
    return f"re-wrapped {count} vault item(s); {where}. Keep {new_key_file} until BOSS restarts cleanly."


def main(argv: Optional[list[str]] = None) -> int:
    from ..db import begin
    from . import audit, migration, posture, shadow_report
    from .legacy import LegacyKind

    parser = argparse.ArgumentParser(prog="boss identity", description=__doc__.split("\n\n")[0])
    sub = parser.add_subparsers(dest="cmd", required=True)
    rec = sub.add_parser("recover-owner", help="make a locked-out person an Owner again")
    rec.add_argument("--email", required=True)
    rec.add_argument("--origin", default=None, help="the console origin for the link")
    sub.add_parser("migrate", help="run the M0 legacy mapping now")
    leg = sub.add_parser("legacy", help="legacy bridges")
    leg.add_argument("action", choices=("status", "retire"))
    leg.add_argument("--path", choices=[k.value for k in (LegacyKind.SHARED_PASSWORD,
                                                          LegacyKind.CONSOLE_API_KEY,
                                                          LegacyKind.NETWORK_MODE)])
    aud = sub.add_parser("audit", help="the security event log")
    aud.add_argument("action", choices=("verify", "export", "prune"))
    aud.add_argument("--out", default=None)
    aud.add_argument("--older-than-days", type=int, default=None)
    rep = sub.add_parser("shadow-report", help="per-router shadow report")
    rep.add_argument("--json", action="store_true")
    sub.add_parser("posture", help="identity posture checks")
    sub.add_parser("claim-status", help="one line: unclaimed, or claimed and the legacy cutoff")
    rmk = sub.add_parser("rotate-master-key", help="re-wrap every vault key under a new master key")
    rmk.add_argument("--new-key-file", required=True,
                     help="a file holding the new 32-byte key, base64 (never printed)")
    sub.add_parser("setup-link", help="a new single-use claim link, while the install has no Owner")
    hk = sub.add_parser("host-key", help="mint the host tools' key (shown once)")
    hk.add_argument("--quiet", action="store_true", help="print the key alone (for boss.sh)")
    hk.add_argument("--keep-others", action="store_true", help="do not end the earlier host keys")
    mk = sub.add_parser("mcp-key", help="mint one project's MCP key (shown once)")
    mk.add_argument("--project", required=True)
    mk.add_argument("--permission", action="append", dest="permissions", default=None,
                    help="a capability (agent_chat, …) or a permission (workflow_run:start); repeat")
    mk.add_argument("--quiet", action="store_true", help="print the key alone (for make mcp-setup)")
    sik = sub.add_parser("self-installer-key",
                         help="mint the host self-installer's boss_sak_ key (boss self-fix enroll)")
    sik.add_argument("--quiet", action="store_true", help="print the key alone (for boss self-fix enroll)")
    egk = sub.add_parser("egress-key", help="mint the egress service's own boss_sak_ key (shown once)")
    egk.add_argument("--revoke-others", action="store_true", help="end every earlier egress key now")
    args = parser.parse_args(argv)

    try:
        with begin() as db:
            org_id = migration.ensure_seeded(db)
            if org_id is None:
                print("no org in this database yet: start BOSS once first", file=sys.stderr)
                return 2
            if args.cmd == "recover-owner":
                done = recover_owner(args.email, db=db, origin=args.origin)
                print(recovery_summary(done))
            elif args.cmd == "rotate-master-key":
                print(rotate_master_key(args.new_key_file, db=db))
            elif args.cmd == "self-installer-key":
                from . import tokens

                created = tokens.issue_self_installer_key(db, org_id=org_id, created_by=HOST_CLI_PRINCIPAL)
                if args.quiet:
                    print(created.key)
                else:
                    print("The host self-installer's key (sys_self_installer). It is shown once; "
                          f"`boss self-fix enroll` keeps it for you.\n\n  {created.key}\n")
            elif args.cmd == "egress-key":
                from . import tokens

                created = tokens.issue_egress_service_key(db, org_id=org_id, created_by=HOST_CLI_PRINCIPAL,
                                                          revoke_others=args.revoke_others)
                print("The egress service's key (sys_egress). It is shown once; give it to boss-egress as "
                      f"its bearer for /v1/identity/introspect.\n\n  {created.key}\n")
            elif args.cmd == "setup-link":
                from .claim import setup_link_command

                code, message = setup_link_command(db)
                print(message, file=sys.stderr if code else sys.stdout)
                return code
            elif args.cmd in ("host-key", "mcp-key"):
                from . import host_tools

                if args.cmd == "host-key":
                    created = host_tools.issue_host_key(db, org_id=org_id,
                                                        revoke_others=not args.keep_others)
                    what = ("The host tools' key (service account \"Host tools\"). It is shown once; "
                            "keep it in .env as BOSS_HOST_API_KEY (boss.sh does this for you).")
                else:
                    created = host_tools.issue_mcp_key(
                        db, org_id=org_id, project_id=args.project,
                        wanted=args.permissions or host_tools.MCP_DEFAULT_CAPABILITIES)
                    what = (f"The MCP key for project {args.project}. It is shown once; keep it in .env "
                            "as BOSS_MCP_API_KEY (make mcp-setup does this for you).")
                print(created.key if args.quiet else f"{what}\n\n  {created.key}\n")
            elif args.cmd == "migrate":
                migration.forget_seeded()
                _print(migration.seed(db, org_id=org_id).as_dict())
            elif args.cmd == "legacy" and args.action == "status":
                _print(posture.legacy_paths(org_id, db=db))
            elif args.cmd == "legacy":
                if not args.path:
                    parser.error("legacy retire needs --path")
                _print(migration.retire_path(org_id, LegacyKind(args.path), db=db,
                                             actor_principal_id=HOST_CLI_PRINCIPAL,
                                             reason="retired from the host shell").__dict__)
            elif args.cmd == "audit" and args.action == "verify":
                result = audit.verify_chain(org_id, db=db)
                audit.record(audit.SecurityEvent(
                    org_id=org_id, event_type="audit.verified",
                    outcome=audit.Outcome.SUCCESS if result.ok else audit.Outcome.FAILURE,
                    actor_principal_id=HOST_CLI_PRINCIPAL, actor_kind="system",
                    details={"checked": result.checked, "last_seq": result.last_seq,
                             "first_break_seq": result.first_break_seq}), db=db)
                _print(result.__dict__)
            elif args.cmd == "audit" and args.action == "export":
                bundle = audit.export(org_id, db=db, actor_principal_id=HOST_CLI_PRINCIPAL,
                                      actor_kind="system")
                text = json.dumps(bundle, default=str, indent=1)
                if args.out:
                    with open(args.out, "w", encoding="utf-8") as fh:
                        fh.write(text)
                    print(f"wrote {len(bundle['events'])} events to {args.out}")
                else:
                    print(text)
            elif args.cmd == "audit":
                from ..config import get_settings
                days = args.older_than_days or get_settings().identity_audit_retention_days
                before = datetime.now(timezone.utc) - timedelta(days=days)
                _print(audit.prune(org_id, before=before, db=db,
                                   actor_principal_id=HOST_CLI_PRINCIPAL,
                                   actor_kind="system").__dict__)
            elif args.cmd == "shadow-report":
                shadow_report.flush(db)
                result = shadow_report.report(db)
                if args.json:
                    _print(result)
                else:
                    print(result["next_step"])
                    for r in result["routers"]:
                        flag = "READY" if r["ready_to_enforce"] else r["mode"]
                        print(f"  {r['router_key']:<28} {flag:<8} would-deny {r['would_deny']:>6}"
                              f"  unexplained {r['unexplained']:>6}  {'; '.join(r['blockers'])}")
            elif args.cmd == "posture":
                _print(posture.posture(org_id, db=db))
            elif args.cmd == "claim-status":
                print(claim_status(org_id, db=db))
    except (RecoveryError, ValueError) as exc:
        print(f"refused: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
