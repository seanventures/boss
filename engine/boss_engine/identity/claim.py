"""Claiming the first Owner (IDENTITY.md §4.11, §4.12; D25, D27). Three paths, one outcome:

  setup token       fresh install: 32 random bytes, SHA-256 at rest, 1 hour; printed to the logs and
                    by `boss identity setup-link`; the claim URL carries it in the FRAGMENT
  legacy password   an install with BOSS_CONSOLE_PASSWORD: the shared password proves "operator"
  network bridge    a BOSS_CONSOLE_AUTH=network install (shared-server): the request is bridged to
                    usr_network_operator by S5's legacy.bridge; the claim binds the browser's
                    origin (Origin == Host, https) as the public origin — no env, no logs

Every path is single use, rate-limited (10/h per IP) and answers 404 for good once a real Owner
exists (A14). The claimant becomes the first Owner; `bootstrap.claimed` arms the last-Owner trigger.
"""
from __future__ import annotations

import hmac
from dataclasses import dataclass
from typing import Any, Optional

from sqlalchemy import text

from . import ratelimit, users
from .audit import Outcome
from .authn_support import (
    challenge_by_secret,
    consume_challenge,
    create_challenge,
    default_org_id,
    emit,
    origin_of,
    public_origin,
    random_token,
)
from .legacy import NETWORK_OPERATOR_PRINCIPAL
from .passwords import PasswordRejected, check_new_password, hash_password
from .principal import AuthContext, AuthMethod

SETUP_TTL_SECONDS = 3600
CLAIMS_PER_IP_PER_HOUR = 10


class ClaimRefused(Exception):
    def __init__(self, code: str, status: int = 400):
        super().__init__(code)
        self.code = code
        self.status = status


def is_claimed(db: Any, org_id: Optional[str] = None) -> bool:
    """A real Owner exists (or once did: a bootstrap.claimed event is permanent)."""
    org = org_id or default_org_id(db)
    if not org:
        return False
    if users.active_owner_ids(db, org):
        return True
    return db.execute(text("SELECT 1 FROM identity_security_events WHERE org_id = :o AND "
                           "event_type = 'bootstrap.claimed' LIMIT 1"), {"o": org}).first() is not None


def _legacy_password() -> str:
    from ..config import get_settings

    return getattr(get_settings(), "legacy_console_password", "") or ""


def _shared_password_is_claim_credential(db: Any, candidate: str) -> bool:
    """S5's legacy.check_shared_password when it exists (it also seeds the legacy mapping);
    otherwise a constant-time compare with BOSS_CONSOLE_PASSWORD."""
    from . import legacy

    check = getattr(legacy, "check_shared_password", None)
    if check is not None:
        return check(candidate, db=db).status == "claim"
    expected = _legacy_password()
    return bool(expected) and hmac.compare_digest(candidate.encode(), expected.encode())


def _on_claimed(db: Any, org_id: str, owner_id: str, path: str, *, seat: Optional[str] = None) -> None:
    """S5's migration.on_claimed starts the legacy countdown (usr_legacy_shared as Operator for
    30 days, the network bridge as Operator) in the claim's transaction. Absent until S5 lands."""
    import importlib

    try:
        migration = importlib.import_module("boss_engine.identity.migration")
    except ModuleNotFoundError as exc:
        if exc.name != "boss_engine.identity.migration":
            raise
        return
    migration.on_claimed(org_id, owner_id, db=db, via=path,
                         assume_member=seat if seat in legacy_seats(db) else None)


def legacy_password_configured() -> bool:
    return bool(_legacy_password())


#: Today's governance seats (default_safety.yaml): the first Owner may "be" one, so history and
#: approvals map to them (§12 M1).
DEFAULT_SEATS = ("ceo", "cto", "cao", "vp_agents", "release_manager")


def legacy_seats(db: Any) -> list[str]:
    return list(DEFAULT_SEATS)


def setup_expires_at(db: Any) -> Optional[str]:
    row = db.execute(text("SELECT max(expires_at) FROM identity_auth_challenges WHERE kind = 'setup' "
                          "AND consumed_at IS NULL AND expires_at > now()")).scalar()
    return row.isoformat() if row else None


def available_paths(db: Any, ctx: Optional[AuthContext]) -> list[str]:
    paths = ["setup_token"]
    if _legacy_password():
        paths.append("legacy_password")
    if is_network_bridged(ctx):
        paths.append("network")
    return paths


def is_network_bridged(ctx: Optional[AuthContext]) -> bool:
    return bool(ctx and ctx.method == AuthMethod.NETWORK
                and ctx.principal.id == NETWORK_OPERATOR_PRINCIPAL and ctx.principal.network_bound)


# ---- the setup token --------------------------------------------------------------------------------

def issue_setup_token(db: Any, *, force: bool = False) -> Optional[str]:
    """A new setup token when the install has no Owner. Reuses nothing: an earlier live token is
    voided when `force` (the CLI), and none is minted while one is live otherwise (boot)."""
    if is_claimed(db):
        return None
    live = db.execute(text("SELECT 1 FROM identity_auth_challenges WHERE kind = 'setup' AND "
                           "consumed_at IS NULL AND expires_at > now() LIMIT 1")).first()
    if live and not force:
        return None
    db.execute(text("UPDATE identity_auth_challenges SET consumed_at = now() WHERE kind = 'setup' "
                    "AND consumed_at IS NULL"))
    token = random_token("bst_")
    create_challenge(db, kind="setup", binding_secret=None, ttl_seconds=SETUP_TTL_SECONDS, secret=token)
    return token


def setup_url(db: Any, token: str) -> str:
    """The claim link. The token is in the fragment, so it never reaches an access log."""
    origin = public_origin(db) or "http://localhost:3000"
    return f"{origin}/claim#t={token}"


def announce_setup_link(db: Any) -> Optional[str]:
    """Called at boot (and by the setup-status route): mint and PRINT the claim URL when the
    install has no Owner and no live setup token. Returns the URL printed, if any."""
    token = issue_setup_token(db)
    if not token:
        return None
    url = setup_url(db, token)
    print(f"[identity] this BOSS has no Owner yet. Claim it within 1 hour: {url}", flush=True)
    return url


# ---- the claim ------------------------------------------------------------------------------------------

@dataclass(frozen=True)
class ClaimRequest:
    name: str
    email: str
    password: Optional[str]
    setup_token: Optional[str] = None
    legacy_password: Optional[str] = None
    confirm_origin: Optional[str] = None
    seat: Optional[str] = None


def count_attempt(db: Any, ip: Optional[str]) -> Optional[int]:
    """Count one claim attempt (10/h per IP). Call it in its OWN transaction before `claim`, so a
    refused claim (which rolls back) still counts. Returns Retry-After seconds when limited."""
    rate = ratelimit.hit("setup_claim.ip", ip or "unknown", limit=CLAIMS_PER_IP_PER_HOUR,
                         window_seconds=3600, db=db)
    return None if rate.allowed else (rate.retry_after_seconds or 3600)


def claim(db: Any, req: ClaimRequest, *, ip: Optional[str], bridged: Optional[AuthContext],
          headers: dict[str, str]) -> users.UserRow:
    """Create the first Owner. Raises ClaimRefused (404 once claimed; 401 for a wrong credential).
    Serialised by an advisory lock so two claims cannot both win."""
    db.execute(text("SELECT pg_advisory_xact_lock(hashtext('boss.identity.claim'))"))
    org_id = default_org_id(db)
    if not org_id or is_claimed(db, org_id):
        raise ClaimRefused("not_found", 404)
    path = _authorise(db, req, bridged=bridged, headers=headers)
    email = (req.email or "").strip()
    if "@" not in email or len(email) > 320:
        raise ClaimRefused("invalid_email", 422)
    password_hash = None
    if req.password:
        try:
            check_new_password(req.password, email=email)
        except PasswordRejected as exc:
            raise ClaimRefused(exc.code, 422) from None
        password_hash = hash_password(req.password)
    elif path != "network":
        raise ClaimRefused("password_required", 422)
    if users.by_email(db, email):
        raise ClaimRefused("email_in_use", 409)
    pid = users.create_user(db, org_id=org_id, email=email, display_name=req.name or email,
                            password_hash=password_hash, status="active", created_by="bootstrap")
    users.add_binding(db, org_id=org_id, subject_id=pid, role_key="owner", granted_by=pid,
                      reason=f"first Owner (claim: {path})")
    if path == "network":
        _bind_public_origin(db, org_id, origin_of(req.confirm_origin or "") or "", by=pid)
    db.execute(text("UPDATE identity_auth_challenges SET consumed_at = now() WHERE kind = 'setup' "
                    "AND consumed_at IS NULL"))
    user = users.by_id(db, pid)
    assert user is not None
    ctx = AuthContext(user.principal(), AuthMethod.SESSION, ip=ip)
    emit(db, org_id=org_id, event_type="bootstrap.claimed", outcome=Outcome.SUCCESS, ctx=ctx,
         target_type="user", target_id=pid, details={"path": path})
    emit(db, org_id=org_id, event_type="owner.added", outcome=Outcome.SUCCESS, ctx=ctx,
         target_type="user", target_id=pid, details={"via": "claim"})
    _on_claimed(db, org_id, pid, path, seat=req.seat)
    from .authn_support import bump_epoch

    try:
        bump_epoch(org_id, db=db, reason="bootstrap.claimed")
    except NotImplementedError:
        pass   # S2's epoch; nothing is cached before the first Owner exists
    return user


def _authorise(db: Any, req: ClaimRequest, *, bridged: Optional[AuthContext],
               headers: dict[str, str]) -> str:
    if req.setup_token:
        ch = challenge_by_secret(db, kind="setup", secret=req.setup_token)
        if ch is None or not consume_challenge(db, ch.id_hash):
            raise ClaimRefused("invalid_setup_token", 401)
        return "setup_token"
    if req.legacy_password:
        if not _shared_password_is_claim_credential(db, req.legacy_password):
            raise ClaimRefused("invalid_credentials", 401)
        return "legacy_password"
    if is_network_bridged(bridged):
        origin = origin_of(headers.get("origin", ""))
        host = (headers.get("x-forwarded-host") or headers.get("host") or "").split(",")[0].strip()
        expected = origin_of(f"https://{host}") if host else None
        confirmed = origin_of(req.confirm_origin or "")
        # The origin bound is the one the browser addressed: Origin == Host, https, and confirmed
        # by the claimant on screen (§4.12 step 2).
        if not origin or not origin.startswith("https://") or origin != expected or confirmed != origin:
            raise ClaimRefused("origin_mismatch", 400)
        return "network"
    raise ClaimRefused("credential_required", 401)


def _bind_public_origin(db: Any, org_id: str, origin: str, *, by: str) -> None:
    from ..config import get_settings

    if origin_of(get_settings().public_url or "") or not origin:
        return   # BOSS_PUBLIC_URL, when set, always wins
    db.execute(text(
        "INSERT INTO identity_settings (org_id, policy, updated_by) VALUES (:o, "
        "jsonb_build_object('public_origin', CAST(:origin AS text)), :by) ON CONFLICT (org_id) DO UPDATE "
        "SET policy = identity_settings.policy || jsonb_build_object('public_origin', CAST(:origin AS text)), "
        "updated_by = :by, updated_at = now()"), {"o": org_id, "origin": origin, "by": by})


# ---- `boss identity setup-link` (host shell) -----------------------------------------------------------

def setup_link_command(db: Any) -> tuple[int, str]:
    """Mint a NEW setup link (voiding any earlier one) while the install has no Owner. Host shell
    only: no HTTP route or MCP tool reaches this. Returns (exit code, text to print)."""
    if is_claimed(db):
        return 1, "This BOSS already has an Owner: there is nothing to claim. (Locked out? Use recover-owner.)"
    token = issue_setup_token(db, force=True)
    if not token:
        return 1, "No setup link could be made."
    return 0, (f"Claim this BOSS within 1 hour (single use):\n\n  {setup_url(db, token)}\n\n"
               "Open it in the browser you will sign in with.")


def main(argv: Optional[list[str]] = None) -> int:
    """`python -m boss_engine.identity.claim setup-link` — S5's `boss identity` dispatcher calls
    `setup_link_command` for its `setup-link` subcommand."""
    import sys

    args = list(sys.argv[1:] if argv is None else argv)
    if args != ["setup-link"]:
        print("usage: python -m boss_engine.identity.claim setup-link", file=sys.stderr)
        return 2
    from ..db import begin

    with begin() as db:
        code, message = setup_link_command(db)
    print(message, file=sys.stderr if code else sys.stdout)
    return code


if __name__ == "__main__":
    raise SystemExit(main())
