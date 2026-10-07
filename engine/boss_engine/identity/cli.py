"""`python -m boss_engine.identity.cli …` — the host-shell identity commands (IDENTITY.md §4.7, §8,
§12). Owned by stream S5.

    recover-owner --email E [--origin URL]   the sole Owner is locked out: make E an active Owner,
                                             reset their MFA, end their sessions, print a 15-minute
                                             single-use link. Actor: sys_host_cli. Audited.
    migrate                                  run the M0 mapping now (it also runs by itself)
    legacy status | legacy retire --path P   the legacy bridges, their counts and countdowns
    audit verify | audit export --out F | audit prune --older-than-days N
    shadow-report [--json]                   the per-router shadow -> enforce report
    posture                                  the six posture checks
    claim-status                             one line for `boss doctor`: claimed or not
    egress-key [--revoke-others]             mint the egress service's boss_sak_ key (sys_egress),
                                             shown once; it authenticates /v1/identity/introspect

HOST SHELL ONLY, by construction: these functions are reached from no HTTP route and no MCP tool
(test_identity_audit_recovery_is_host_only pins it). Run them where the database is:

    docker compose exec api python -m boss_engine.identity.cli recover-owner --email you@example.com

Nothing here prints a secret except the one recovery link, to the terminal that asked for it.
"""
from __future__ import annotations

import argparse
import json
import secrets
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

HOST_CLI_PRINCIPAL = "sys_host_cli"
RECOVERY_TTL = timedelta(minutes=15)
RECOVERY_PATH = "/auth/recover"


class RecoveryError(RuntimeError):
    pass


@dataclass(frozen=True)
class Recovery:
    principal_id: str
    link: str
    expires_at: datetime
    reactivated: bool
    owner_bound: bool


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
    return (get_policy(org_id, db=db).public_origin or "http://localhost:3000").rstrip("/")


def recover_owner(email: str, *, db: Any, origin: Optional[str] = None,
                  now: Optional[datetime] = None) -> Recovery:
    """Host-shell break-glass (§4.7, §6.12). In ONE transaction: the person becomes an active
    Owner, their TOTP and unused recovery codes are removed, their sessions end, a 15-minute
    single-use link is minted (only its hash is stored), and `owner.recovered_by_host` is
    written with actor sys_host_cli. Every Owner then sees a banner for 7 days (posture.banners)."""
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
    db.execute(_sql("UPDATE identity_recovery_codes SET used_at = :now WHERE principal_id = :p "
                    "AND used_at IS NULL"), {"now": now, "p": pid})
    db.execute(_sql("UPDATE identity_sessions SET revoked_at = :now, revoked_by = :by, "
                    "revoked_reason = 'owner recovered by host' WHERE principal_id = :p AND "
                    "revoked_at IS NULL"), {"now": now, "by": HOST_CLI_PRINCIPAL, "p": pid})
    token = secrets.token_urlsafe(32)
    expires = now + RECOVERY_TTL
    db.execute(_sql("INSERT INTO identity_auth_challenges (id_hash, kind, principal_id, "
                    "expires_at) VALUES (:h, 'password_reset', :p, :exp)"),
               {"h": token_hash(token), "p": pid, "exp": expires})
    audit.record(SecurityEvent(
        org_id=org_id, event_type="owner.recovered_by_host", outcome=Outcome.SUCCESS,
        actor_principal_id=HOST_CLI_PRINCIPAL, actor_kind="system", target_type="principal",
        target_id=pid, details={"reactivated": reactivated, "owner_bound": not had_owner,
                                "mfa_reset": True, "link_expires_at": expires.isoformat()}),
        db=db)
    bump_epoch(db, org_id, "owner recovered by host")
    # the token travels in the fragment: never in a request line the API or a proxy logs (A88)
    link = f"{_origin(db, org_id, origin)}{RECOVERY_PATH}#token={token}"
    return Recovery(pid, link, expires, reactivated, not had_owner)


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
                print(f"Recovered {done.principal_id} as an Owner. MFA was reset and their "
                      f"sessions ended.\nOpen this link within 15 minutes (single use):\n\n"
                      f"  {done.link}\n\nEvery Owner will see a banner about this for 7 days.")
            elif args.cmd == "rotate-master-key":
                print(rotate_master_key(args.new_key_file, db=db))
            elif args.cmd == "egress-key":
                from . import tokens

                created = tokens.issue_egress_service_key(db, org_id=org_id, created_by=HOST_CLI_PRINCIPAL,
                                                          revoke_others=args.revoke_others)
                print("The egress service's key (sys_egress). It is shown once; give it to boss-egress as "
                      f"its bearer for /v1/identity/introspect.\n\n  {created.key}\n")
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
