"""`seed-test-accounts`: one signed-in-able person per access role, a custom role, and a service
account — for walking the console as each role (the per-role Playwright walk, a tester after a
deploy). Host shell only (identity/cli.py); never reachable from HTTP or MCP.

    python -m boss_engine.identity.cli seed-test-accounts --org org_boss \
        --owner-email you@example.com --out /root/seed-accounts.json [--domain example.test] [--i-know]

OWNER-RUN: every change is made, and audited, as the active Owner named by --owner-email.
NOT IN PRODUCTION: refused when this install looks like production (below) unless --i-know.
IDEMPOTENT: a second run finds the same people, role and service account by their seed emails and
names; it gives each person a NEW password (and a new TOTP secret where their role needs MFA),
re-asserts their one binding, and replaces the service account's seed key. The file always holds
working credentials, and no second copy of anyone is made.
SECRETS GO TO --out ONLY, created 0600 before anything is written to it. Nothing secret is printed
or logged; the command prints the file's path and the account count.

"Looks like production": BOSS_ENVIRONMENT is not one of dev/development/test/e2e/ci/staging, AND
the public origin (BOSS_PUBLIC_URL, or the one bound at claim) is not loopback or a .test/.local/
.localhost/.internal name.
"""
from __future__ import annotations

import json
import os
import secrets
from dataclasses import dataclass
from typing import Any, Optional
from urllib.parse import urlsplit

#: Built-in human roles, each gets one person. MFA roles get a TOTP secret (the policy requires it).
SEED_ROLES = ("owner", "admin", "operator", "viewer", "auditor")
#: The custom role (there is no built-in Developer): builds and repositories, no approvals, no deploys.
CUSTOM_ROLE_KEY = "developer"
CUSTOM_ROLE_NAME = "Developer"
CUSTOM_ROLE_PERMISSIONS = (
    "project:read", "code_task:read", "code_task:create", "code_task:steer", "code_task:retry",
    "repository:read", "github:read", "workflow:read", "workflow_run:read", "preview:read",
    "decision:read", "activity:read", "agent:read", "meeting:read", "skill:read", "setup:read",
)
SERVICE_ACCOUNT_NAME = "seed-ci"
SERVICE_ACCOUNT_ROLE = "viewer"
SERVICE_ACCOUNT_SCOPES = ("project:read", "code_task:read")
SEED_KEY_NAME = "seed"
TEST_ENVIRONMENTS = frozenset({"dev", "development", "test", "e2e", "ci", "staging"})
TEST_HOST_SUFFIXES = (".test", ".local", ".localhost", ".internal", ".example")


class SeedRefused(RuntimeError):
    pass


@dataclass(frozen=True)
class SeedResult:
    out: str
    people: int
    service_accounts: int


def _sql(statement: str):
    from sqlalchemy import text

    return text(statement)


def looks_like_production(db: Any, org_id: str) -> bool:
    from ..config import get_settings
    from .authn_support import public_origin

    env = (os.environ.get("BOSS_ENVIRONMENT") or "").strip().lower()
    if env in TEST_ENVIRONMENTS:
        return False
    origin = public_origin(db, org_id) or (get_settings().public_url or "")
    host = (urlsplit(origin).hostname or "").lower()
    if host in ("localhost", "127.0.0.1", "::1") or host.endswith(TEST_HOST_SUFFIXES):
        return False
    return True


def _open_out(path: str):
    """The file is 0600 BEFORE a byte is written (umask cannot widen it); an existing file is
    truncated and re-chmodded."""
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    os.fchmod(fd, 0o600)
    return os.fdopen(fd, "w", encoding="utf-8")


def _password() -> str:
    return secrets.token_urlsafe(18)          # 24 chars, ~144 bits


def _person(db: Any, *, org_id: str, role: str, domain: str, owner_ctx: Any) -> dict[str, Any]:
    from . import passwords, totp, users, vault
    from .authn_support import emit
    from .audit import Outcome
    from .users import add_binding

    email = f"seed-{role}@{domain}"
    name = f"Seed {role.replace('_', ' ').title()}"
    password = _password()
    phc = passwords.hash_password(password)
    existing = users.by_email(db, email)
    if existing is not None and existing.org_id != org_id:
        raise SeedRefused(f"{email} belongs to another org")
    if existing is None:
        pid = users.create_user(db, org_id=org_id, email=email, display_name=name, password_hash=phc,
                                email_verified=True, created_by=owner_ctx.principal.id)
        emit(db, org_id=org_id, event_type="user.invited", outcome=Outcome.SUCCESS, ctx=owner_ctx,
             target_type="principal", target_id=pid, details={"via": "seed_test_accounts", "role": role})
    else:
        pid = existing.principal_id
        users.set_password_hash(db, pid, phc)
        db.execute(_sql("UPDATE identity_principals SET status = 'active' WHERE id = :p"), {"p": pid})
        emit(db, org_id=org_id, event_type="password.changed", outcome=Outcome.SUCCESS, ctx=owner_ctx,
             target_type="principal", target_id=pid, details={"via": "seed_test_accounts"})
    add_binding(db, org_id=org_id, subject_id=pid, role_key=role, granted_by=owner_ctx.principal.id,
                reason="seed-test-accounts")
    emit(db, org_id=org_id, event_type="owner.added" if role == "owner" else "role_binding.added",
         outcome=Outcome.SUCCESS, ctx=owner_ctx, target_type="principal", target_id=pid,
         details={"role": role, "via": "seed_test_accounts"})
    record: dict[str, Any] = {"role": role, "email": email, "password": password, "principal_id": pid}
    user = users.by_id(db, pid)
    if user is not None and users.mfa_required(db, user):
        secret = totp.generate_secret()
        totp.remove(db, pid)
        item = vault.store(org_id=org_id, owner_principal_id=pid, kind=vault.VaultItemKind.TOTP_SECRET,
                           provider="totp", secret=secret, db=db)
        db.execute(_sql("INSERT INTO identity_totp (principal_id, vault_item_id, last_step) VALUES (:p, :v, 0)"),
                   {"p": pid, "v": item})
        emit(db, org_id=org_id, event_type="mfa.enrolled", outcome=Outcome.SUCCESS, ctx=owner_ctx,
             target_type="principal", target_id=pid, details={"factor": "totp", "via": "seed_test_accounts"})
        record["totp_secret"] = secret
    return record


def _custom_role(db: Any, owner_ctx: Any) -> str:
    from . import roles

    org = owner_ctx.principal.org_id
    row = db.execute(_sql("SELECT id FROM identity_access_roles WHERE org_id = :o AND key = :k"),
                     {"o": org, "k": CUSTOM_ROLE_KEY}).scalar()
    if row is None:
        made = roles.create_role(db, owner_ctx, key=CUSTOM_ROLE_KEY, name=CUSTOM_ROLE_NAME,
                                 description="Seeded for testing: builds and repositories; no approvals, no deploys.",
                                 permissions=CUSTOM_ROLE_PERMISSIONS, confirm_critical=CUSTOM_ROLE_PERMISSIONS)
        return str(made["id"])
    held = roles.permissions_of(db, str(row))
    if held != frozenset(CUSTOM_ROLE_PERMISSIONS):
        roles.update_role(db, owner_ctx, str(row), permissions=CUSTOM_ROLE_PERMISSIONS,
                          confirm_critical=CUSTOM_ROLE_PERMISSIONS)
    return str(row)


def _service_account(db: Any, owner_ctx: Any) -> dict[str, Any]:
    from . import api_keys, service_accounts, users
    from .principal import PrincipalKind

    org = owner_ctx.principal.org_id
    pid = db.execute(_sql("SELECT id FROM identity_principals WHERE org_id = :o AND kind = 'service_account' "
                          "AND display_name = :n AND status = 'active' ORDER BY created_at LIMIT 1"),
                     {"o": org, "n": SERVICE_ACCOUNT_NAME}).scalar()
    if pid is None:
        pid = service_accounts.create(db, actor=owner_ctx, name=SERVICE_ACCOUNT_NAME,
                                      description="Seeded for testing (seed-test-accounts).",
                                      owners=[owner_ctx.principal.id]).principal_id
    users.add_binding(db, org_id=org, subject_id=pid, role_key=SERVICE_ACCOUNT_ROLE,
                      granted_by=owner_ctx.principal.id, reason="seed-test-accounts")
    for (key_id,) in db.execute(_sql("SELECT id FROM identity_api_credentials WHERE principal_id = :p AND "
                                     "name = :n AND revoked_at IS NULL"), {"p": pid, "n": SEED_KEY_NAME}):
        api_keys.revoke(db, actor=owner_ctx, key_id=key_id, org_id=org)
    from .principal import Principal, PrincipalStatus

    owner = Principal(pid, PrincipalKind.SERVICE_ACCOUNT, org, PrincipalStatus.ACTIVE, SERVICE_ACCOUNT_NAME)
    created = api_keys.create(db, creator=owner_ctx, owner=owner, kind="sak", name=SEED_KEY_NAME,
                              scopes=SERVICE_ACCOUNT_SCOPES, expires_in_days=30)
    return {"name": SERVICE_ACCOUNT_NAME, "principal_id": pid, "role": SERVICE_ACCOUNT_ROLE,
            "scopes": list(SERVICE_ACCOUNT_SCOPES), "key": created.key,
            "expires_at": created.expires_at.isoformat() if created.expires_at else None}


def seed_test_accounts(db: Any, *, org_id: str, owner_email: str, out: str, domain: str = "example.test",
                       i_know: bool = False) -> SeedResult:
    from . import users
    from .principal import AuthContext, AuthMethod

    if not out:
        raise SeedRefused("--out is required: the credentials are written only there")
    if not db.execute(_sql("SELECT 1 FROM orgs WHERE id = :o"), {"o": org_id}).first():
        raise SeedRefused(f"no org {org_id!r}")
    owner = users.by_email(db, owner_email or "")
    if owner is None or owner.org_id != org_id or owner.status != "active" or not users.is_owner(db, owner.principal_id, org_id):
        raise SeedRefused("--owner-email must name an active Owner of this org: the seed runs as them")
    if looks_like_production(db, org_id) and not i_know:
        raise SeedRefused("this install looks like production (BOSS_ENVIRONMENT unset and a public origin); "
                          "pass --i-know to seed test accounts anyway")
    domain = (domain or "example.test").strip().lower()
    if not domain or "@" in domain or "/" in domain:
        raise SeedRefused("--domain is a bare domain name")
    ctx = AuthContext(owner.principal(), AuthMethod.SESSION)
    fh = _open_out(out)                    # fail before any change when the file cannot be written
    try:
        _custom_role(db, ctx)
        people = [_person(db, org_id=org_id, role=role, domain=domain, owner_ctx=ctx)
                  for role in (*SEED_ROLES, CUSTOM_ROLE_KEY)]
        sa = _service_account(db, ctx)
        from .authz import bump_epoch

        bump_epoch(org_id, db=db, reason="seed_test_accounts")
        document = {"org_id": org_id, "seeded_by": owner.principal_id, "domain": domain,
                    "note": "Test accounts from seed-test-accounts. Secrets: keep this file 0600; delete it when done.",
                    "people": people,
                    "custom_role": {"key": CUSTOM_ROLE_KEY, "permissions": list(CUSTOM_ROLE_PERMISSIONS)},
                    "service_account": sa}
        json.dump(document, fh, indent=1)
        fh.write("\n")
    except BaseException:
        fh.close()
        os.unlink(out)                     # no half-written file of credentials that never existed
        raise
    fh.close()
    return SeedResult(out=out, people=len(people), service_accounts=1)
