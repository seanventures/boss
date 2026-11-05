"""Claiming the first Owner (IDENTITY.md §4.11, §4.12; D25, D27). Three paths, one outcome:

  setup token       fresh install: 32 random bytes, SHA-256 at rest, 1 hour; printed to the logs and
                    by `boss identity setup-link`, or answered to the install's admin key by
                    POST /v1/identity/setup-link (no shell); the claim URL carries it in the FRAGMENT
  legacy password   an install with BOSS_CONSOLE_PASSWORD: the shared password proves "operator"
  network bridge    a BOSS_CONSOLE_AUTH=network install: the request is bridged to
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
    from .migration import claimed_at   # one definition of "claimed"

    return claimed_at(db, org) is not None


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
    # §6.13: the first Owner holds every approval seat nobody holds yet (a fresh install has no
    # one else to approve a tier-2 or tier-3 decision); an Owner reassigns them later.
    from .approvals import seat_the_first_owner

    seat_the_first_owner(db, org_id, owner_id)


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
    from .authn_support import local_console_origin

    origin = public_origin(db) or local_console_origin()
    from .links import url

    return url("setup", origin, token)


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
          headers: dict[str, str], peer_ip: Optional[str] = None) -> users.UserRow:
    """Create the first Owner. Raises ClaimRefused (404 once claimed; 401 for a wrong credential).
    Serialised by an advisory lock so two claims cannot both win, and by a DB constraint (L4: one
    `bootstrap.claimed` per org, identity_one_claim_per_org) should the lock ever be bypassed.

    EVERY path binds the public origin (H2) unless BOSS_PUBLIC_URL is set: the origin the
    claimant's browser addressed (its Origin header, already checked by the pre-session CSRF
    guard; https, or http on loopback). From then on emailed links, WebAuthn and SSO use only it."""
    db.execute(text("SELECT pg_advisory_xact_lock(hashtext('boss.identity.claim'))"))
    org_id = default_org_id(db)
    if not org_id or is_claimed(db, org_id):
        raise ClaimRefused("not_found", 404)
    path = _authorise(db, req, bridged=bridged, headers=headers, peer_ip=peer_ip)
    email = (req.email or "").strip()
    if "@" not in email or len(email) > 320:
        raise ClaimRefused("invalid_email", 422)
    # PASSWORD FIRST (BOSSAPP-812): the first Owner always chooses a password. A passkey-only
    # claim left the Owner with one way in that lives in one browser profile; in any other
    # browser there was nothing to sign in with. The second factor (passkey or authenticator
    # app) is enrolled next and stays required for Owners.
    if not req.password:
        raise ClaimRefused("password_required", 422)
    try:
        check_new_password(req.password, email=email, policy=users.get_policy(org_id, db=db))
    except PasswordRejected as exc:
        raise ClaimRefused(exc.code, 422) from None
    password_hash = hash_password(req.password)
    if users.by_email(db, email):
        raise ClaimRefused("email_in_use", 409)
    pid = users.create_user(db, org_id=org_id, email=email, display_name=req.name or email,
                            password_hash=password_hash, status="active", created_by="bootstrap")
    users.add_binding(db, org_id=org_id, subject_id=pid, role_key="owner", granted_by=pid,
                      reason=f"first Owner (claim: {path})")
    _bind_public_origin(db, org_id, claim_origin(path, req, headers), by=pid)
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


def proof_method(*, setup_token: Optional[str], legacy_password: Optional[str]) -> str:
    """The sign-in method a successful claim's session records: the proof `_authorise` accepted,
    in its precedence (a setup link, then the shared password, then the network bridge)."""
    if setup_token:
        return "setup_link"
    if legacy_password:
        return "legacy_password"
    return "network"


def claim_origin(path: str, req: ClaimRequest, headers: dict[str, str]) -> str:
    """The origin a claim binds (H2). The network path's was confirmed on screen and checked
    against the addressed host in `_authorise`; the others' is the browser's Origin header, which
    the pre-session CSRF check already required to be this request's own origin. Only a secure
    origin (https, or http on loopback) is ever bound: a plain-http LAN address stays unbound and
    posture says so."""
    from .authn_support import secure_origin_ok

    if path == "network":
        origin = origin_of(req.confirm_origin or "") or ""
    else:
        origin = origin_of(headers.get("origin", "")) or ""
    return origin if secure_origin_ok(origin) else ""


def _authorise(db: Any, req: ClaimRequest, *, bridged: Optional[AuthContext],
               headers: dict[str, str], peer_ip: Optional[str] = None) -> str:
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
        from .authn_support import is_trusted_proxy

        origin = origin_of(headers.get("origin", ""))
        # L1: X-Forwarded-Host only from a trusted proxy (BOSS_TRUSTED_PROXIES), like every other
        # forwarded header; from anyone else the addressed host is the Host header.
        forwarded = headers.get("x-forwarded-host") if is_trusted_proxy(peer_ip) else None
        host = (forwarded or headers.get("host") or "").split(",")[0].strip()
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
    from . import policy as policy_module

    with policy_module._cache_lock:   # this transaction reads it next (the claim's own session)
        for stale in [k for k in policy_module._cache if k[0] == org_id]:
            policy_module._cache.pop(stale, None)


# ---- `boss identity setup-link` (host shell) -----------------------------------------------------------

def setup_link_command(db: Any) -> tuple[int, str]:
    """Mint a NEW setup link (voiding any earlier one) while the install has no Owner. Host shell
    only: the one HTTP twin is `setup_link_over_api`, for the install's admin key alone. Returns
    (exit code, text to print)."""
    if is_claimed(db):
        return 1, "This BOSS already has an Owner: there is nothing to claim. (Locked out? Use recover-owner.)"
    token = issue_setup_token(db, force=True)
    if not token:
        return 1, "No setup link could be made."
    return 0, (f"Claim this BOSS within 1 hour (single use):\n\n  {setup_url(db, token)}\n\n"
               "Open it in the browser you will sign in with.")


# ---- POST /v1/identity/setup-link (the install's admin key) -------------------------------------------

#: Links the admin key may issue an hour. Each one voids the last, so this only bounds churn.
LINKS_PER_HOUR = 10


class SetupLinkRefused(Exception):
    def __init__(self, code: str, status: int, retry_after: Optional[int] = None):
        super().__init__(code)
        self.code = code
        self.status = status
        self.retry_after = retry_after


def is_install_admin_key(ctx: Optional[AuthContext]) -> bool:
    """The install's ADMIN credential: BOSS_API_KEY (the key the compose sets), bridged to
    sys_legacy_console. The same trust as a shell on the box: it already controls the whole API.
    Never a person, a service account's key, a BOSS_API_KEYS entry or the network bridge."""
    from .legacy import LEGACY_CONSOLE_PRINCIPAL

    return bool(ctx and ctx.method == AuthMethod.LEGACY_API_KEY
                and ctx.principal.id == LEGACY_CONSOLE_PRINCIPAL)


def setup_link_over_api(db: Any, ctx: AuthContext) -> dict[str, str]:
    """`boss identity setup-link` for an install with no shell (a managed host reached only by an
    ops API): the same semantics — a NEW link that voids any earlier one — answered to the admin
    key only. Returns {url, expires_at}. Audited as identity.setup_link_issued (never the token).
    Raises SetupLinkRefused: 404 once claimed (for good, like the claim), 403 for any other
    credential, 429 past LINKS_PER_HOUR."""
    org_id = default_org_id(db)
    if not org_id or is_claimed(db, org_id):
        raise SetupLinkRefused("not_found", 404)
    if not is_install_admin_key(ctx):
        raise SetupLinkRefused("admin_key_only", 403)
    rate = ratelimit.hit("setup_link.key", ctx.principal.id, limit=LINKS_PER_HOUR,
                         window_seconds=3600, db=db)
    if not rate.allowed:
        raise SetupLinkRefused("rate_limited", 429, rate.retry_after_seconds or 3600)
    token = issue_setup_token(db, force=True)
    if not token:
        raise SetupLinkRefused("not_found", 404)
    emit(db, org_id=org_id, event_type="identity.setup_link_issued", outcome=Outcome.SUCCESS,
         ctx=ctx, details={"ttl_seconds": SETUP_TTL_SECONDS})
    return {"url": setup_url(db, token), "expires_at": setup_expires_at(db) or ""}


def links_over_api() -> bool:
    """Whether this install has an admin key that can issue a claim link over the API."""
    from ..config import get_settings

    return bool(str(getattr(get_settings(), "legacy_console_api_key", "") or "").strip())


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
