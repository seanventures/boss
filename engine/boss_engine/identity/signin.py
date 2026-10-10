"""The sign-in flows: password, the MFA step, passkeys, step-up, sign-out (IDENTITY.md §4.2–§4.6,
§4.10). Framework-free: each flow takes the request's facts and a database connection and returns
an `Answer` (status, body, cookies) the API renders as it is.

Invariants every flow keeps:
  - one uniform failure body, `{"error": "invalid_credentials"}`, for unknown user, wrong password
    and soft-locked account; an unknown user costs a dummy argon2id verification (A3);
  - a disabled account is revealed only after the correct password (A16);
  - a session is issued only by `finish` — always a NEW token, never the pre-auth cookie (A7);
  - no response body carries a session token, OTP, reset or magic-link token (A15, A81);
  - every sign-in, failure, MFA and step-up is a security event in the same transaction (A73).
"""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Optional

from . import ratelimit, recovery, sessions, totp, users
from . import webauthn as passkeys
from .audit import Outcome
from .authn import (DEVICE_COOKIE, PREAUTH_COOKIE, SESSION_COOKIE, STEP_UP_WINDOW_SECONDS,
                    AuthnError)
from .authn_support import (
    consume_challenge,
    count_attempt,
    create_challenge,
    default_org_id,
    emit,
    identifier_hash,
    latest_bound_challenge,
    random_token,
    secure_origin_ok,
    sha256_hex,
    utcnow,
)
from .passwords import PasswordBusy, needs_rehash, hash_password, verify_password
from .principal import AuthContext, AuthMethod

MFA_CHALLENGE_TTL_SECONDS = 600
PREAUTH_MAX_AGE = 900
DEVICE_MAX_AGE = 400 * 24 * 3600
SIGNIN_IP_PER_MINUTE = 20
SIGNIN_IP_PER_HOUR = 200
ACCOUNT_DELAY_AFTER = 5
ACCOUNT_LOCK_AFTER = 10
ACCOUNT_WINDOW_SECONDS = 15 * 60
MAX_DELAY_SECONDS = 30
INVALID = {"error": "invalid_credentials"}
DISABLED = {"error": "account_disabled", "message": "This account is disabled. Ask an admin."}


@dataclass
class RequestFacts:
    """What a sign-in flow may know about the request. Cookie values are secrets: never logged."""
    ip: Optional[str] = None
    user_agent: Optional[str] = None
    request_id: Optional[str] = None
    preauth: Optional[str] = field(default=None, repr=False)
    session: Optional[str] = field(default=None, repr=False)
    device: Optional[str] = field(default=None, repr=False)
    #: The BOUND public origin (BOSS_PUBLIC_URL, or the one bound at the claim): WebAuthn RP ID,
    #: SSO redirect URIs and every emailed link. Never derived from Host (H2): None when unbound.
    origin: Optional[str] = None
    #: This request's own origin (Host, or X-Forwarded-Host from a trusted proxy). Used ONLY to
    #: decide whether a Secure session cookie may be set before an origin is bound; never a link.
    request_origin: Optional[str] = None


@dataclass
class Answer:
    status: int
    body: dict[str, Any]
    cookies: list[str] = field(default_factory=list)       # Set-Cookie header values
    headers: dict[str, str] = field(default_factory=dict)

    def __repr__(self) -> str:   # bodies and cookies of auth endpoints are never logged (§8.3)
        return f"Answer(status={self.status})"


def too_many(retry_after: Optional[int]) -> Answer:
    return Answer(429, {"error": "rate_limited"},
                  headers={"Retry-After": str(int(retry_after or 60))})


# ---- shared steps ---------------------------------------------------------------------------------

def ip_limited(db: Any, facts: RequestFacts) -> Optional[Answer]:
    """Sign-in per client IP: 20/min and 200/h (§4.10, A1). Also, now and then, sweeps expired
    challenges and rate-limit windows so neither table grows without bound."""
    import random

    if random.random() < 0.01:
        from .authn_support import purge_expired_challenges

        purge_expired_challenges(db)
        ratelimit.purge(db)
    ip = facts.ip or "unknown"
    for ident, limit, window in ((f"m:{ip}", SIGNIN_IP_PER_MINUTE, 60),
                                 (f"h:{ip}", SIGNIN_IP_PER_HOUR, 3600)):
        decision = ratelimit.hit("signin.ip", ident, limit=limit, window_seconds=window, db=db)
        if not decision.allowed:
            return too_many(decision.retry_after_seconds)
    return None


def _ctx_for(user: users.UserRow, facts: RequestFacts, session: Any = None) -> AuthContext:
    return AuthContext(user.principal(), AuthMethod.SESSION, session=session, ip=facts.ip,
                       user_agent=facts.user_agent, request_id=facts.request_id)


def _new_preauth() -> tuple[str, str]:
    secret = random_token("bpa_")
    return secret, sessions.set_cookie_header(PREAUTH_COOKIE, secret, max_age=PREAUTH_MAX_AGE)


def begin_mfa(db: Any, user: users.UserRow, facts: RequestFacts, *, first: str) -> Answer:
    """The pre-auth step: a challenge bound to a NEW pre-auth cookie. No session yet."""
    secret, cookie = _new_preauth()
    create_challenge(db, kind="mfa", binding_secret=secret, ttl_seconds=MFA_CHALLENGE_TTL_SECONDS,
                     principal_id=user.principal_id, payload={"first": first})
    f = users.factors(db, user.principal_id)
    methods = (["passkey"] if f.passkeys else []) + (["totp"] if f.totp else []) + (
        ["recovery"] if f.recovery_codes_remaining else [])
    return Answer(200, {"next": "mfa", "methods": methods, "who": who_of(user)}, cookies=[cookie])


def who_of(user: users.UserRow) -> dict[str, Any]:
    """The console's greeting (SignInStep.who). Only ever after the first factor succeeded."""
    import hashlib

    hue = int(hashlib.sha256(user.principal_id.encode()).hexdigest()[:4], 16) % 360
    return {"display_name": user.display_name, "email": user.email, "hue": hue}


def finish(db: Any, user: users.UserRow, facts: RequestFacts, *, methods: list[str],
           mfa: bool, replaces: Optional[str] = None, event: str = "auth.login.succeeded",
           extra: Optional[dict[str, Any]] = None, sso_method: Optional[str] = None) -> Answer:
    """Issue a session (the ONLY place one is issued): a new token, the device marker, the events,
    and the body the console needs — never the token itself.

    Enforce SSO is checked HERE, at the one choke point every sign-in path reaches (A-1): the
    first factor is `methods[0]` (or `sso_method`, a break-glass label the caller derived from
    a host-only proof), so no path — a reset link, an invitation, a sign-up, a future one — can
    hand a session to a person whose domain the IdP alone admits."""
    if not secure_origin_ok(facts.origin or facts.request_origin):
        # A20: never a session cookie for a plain-http, non-loopback origin.
        return Answer(400, {"error": "insecure_origin"})
    refused = sso_refusal(db, user, facts, method=sso_method or (methods[0] if methods else ""))
    if refused is not None:
        return refused
    familiar = sessions.device_is_familiar(db, user.principal_id, facts.device)
    device = facts.device if (facts.device and len(facts.device) <= 100) else random_token("bdv_", 24)
    now = utcnow()
    grant = first_factor_grant(db, user, mfa=mfa, now=now)
    issued = sessions.issue(db, principal_id=user.principal_id, org_id=user.org_id, methods=methods,
                            mfa_at=now if mfa else None, ip=facts.ip, user_agent=facts.user_agent,
                            device_id_hash=sha256_hex(device), replaces=replaces,
                            enrol_grant_until=grant)
    users.mark_login(db, user.principal_id)
    ratelimit.reset("signin.account", user.email, db=db)
    for bucket, ident in _source_keys(user.email, facts):
        ratelimit.reset(bucket, ident, db=db)
    ctx = _ctx_for(user, facts)
    emit(db, org_id=user.org_id, event_type="session.created", outcome=Outcome.SUCCESS, ctx=ctx,
         session_ref=issued.session_id, details={"methods": sorted(set(methods))})
    emit(db, org_id=user.org_id, event_type=event, outcome=Outcome.SUCCESS, ctx=ctx,
         session_ref=issued.session_id, details={"methods": sorted(set(methods)), "mfa": mfa})
    # A FIRST-EVER SIGN-IN IS NOT NEWS: not an email, and not a "Not you?" banner either. The
    # claim's own sessions (the claim, then the authenticator step) greeted a brand-new Owner with
    # "New sign-in from a new device … Not you?" on the very first page.
    if not familiar and user.last_login_at is not None:
        emit(db, org_id=user.org_id, event_type="auth.new_device", outcome=Outcome.INFO, ctx=ctx,
             session_ref=issued.session_id, details={"methods": sorted(set(methods))})
        notify_new_device(user, facts, methods)
    max_age = int((issued.absolute_expires_at - now).total_seconds())
    cookies = [sessions.set_cookie_header(SESSION_COOKIE, issued.token, max_age=max_age),
               sessions.set_cookie_header(DEVICE_COOKIE, device, max_age=DEVICE_MAX_AGE),
               sessions.clear_cookie_header(PREAUTH_COOKIE)]
    enrol = users.mfa_required(db, user) and not users.factors(db, user.principal_id).any_second_factor
    # SignInStep (the console's contract): "enrol" when the person must add a second factor now.
    # A passkey is offered only where a browser can make one (webauthn.passkey_blocker): never on
    # an IP-address or plain-http origin, where the authenticator app is the way in.
    passkey_reason = passkeys.passkey_blocker(facts.origin or facts.request_origin)
    body: dict[str, Any] = {"next": "enrol" if enrol else "done",
                            "methods": ((["totp"] if passkey_reason else ["passkey", "totp"]) if enrol else []),
                            "who": who_of(user),
                            "session_id": issued.session_id,
                            "expires_at": issued.absolute_expires_at.isoformat()}
    if enrol:
        body["mfa_enrolment_required"] = True
        body["passkey_reason"] = f"passkey_{passkey_reason}" if passkey_reason else None
        # H1: the session is ENROLMENT-ONLY. Say whether it can enrol the first factor itself, or
        # the person needs an admin's MFA reset (a later password-only sign-in never can).
        body["enrolment_grant"] = grant is not None
    if extra:
        body.update(extra)
    return Answer(200, body, cookies=cookies)


def notify_new_device(user: users.UserRow, facts: RequestFacts, methods: list[str]) -> None:
    """Tell a person about a sign-in from a device BOSS has not seen for them (every sign-in method
    passes through `finish`). With SMTP: an email, off the request path. Without: the
    `auth.new_device` event, which /v1/identity/me shows as a notice."""
    from .authn_support import smtp_configured

    if not smtp_configured():
        return
    from . import email_signin

    how = ", ".join(sorted(set(methods))) or "sign-in"
    from .where import public_address

    where = f", from {public_address(facts.ip)}" if public_address(facts.ip) else ""
    browser = (facts.user_agent or "an unknown browser")[:160]
    email_signin.DELIVER(
        user.email, "New sign-in to BOSS",
        f"Someone signed in to your BOSS account from a new device: {how}{where}, "
        f"using {browser}, at {utcnow().strftime('%Y-%m-%d %H:%M UTC')}.\n\nIf this was you, there is "
        "nothing to do. If not, sign out everywhere (My account > Sessions) and change your "
        "password and second factor now.")


# ---- H1: enrolment-only sessions and the first-factor grant -------------------------------------------

#: How long after the granting sign-in the FIRST second factor may be enrolled.
FIRST_FACTOR_WINDOW_SECONDS = 15 * 60
#: How long an admin's MFA reset leaves the person to sign in and enrol.
RESET_GRANT_SECONDS = 72 * 3600

#: What an ENROLMENT-ONLY session may reach: who am I, enrol a factor, sign out. Everything else is
#: refused 403 `mfa_enrolment_required` by authn (and by the guard in every mode but `off`).
ENROLMENT_ONLY_ROUTES = frozenset({
    ("GET", "/v1/identity/me"),
    ("GET", "/v1/identity/me/permissions"),
    ("GET", "/v1/identity/me/factors"),
    ("POST", "/v1/identity/me/mfa/totp"),
    ("POST", "/v1/identity/me/mfa/totp/confirm"),
    ("GET", "/v1/identity/me/passkeys"),
    ("POST", "/v1/identity/me/passkeys/options"),
    ("POST", "/v1/identity/me/passkeys"),
    ("GET", "/v1/auth/session"),
    ("POST", "/v1/auth/logout"),
})


def enrolment_only(db: Any, principal_id: str, mfa_at: Optional[datetime]) -> bool:
    """A session without MFA of a person whose roles require MFA (H1). Such a person has no second
    factor (sign-in would have asked for it), so the session can only enrol one."""
    if mfa_at is not None:
        return False
    user = users.by_id(db, principal_id)
    if user is None or user.network_bound:
        return False
    return users.mfa_required(db, user)


def first_factor_grant(db: Any, user: users.UserRow, *, mfa: bool,
                       now: Optional[datetime] = None) -> Optional[datetime]:
    """The window in which a NEW session may enrol the person's FIRST second factor, or None.

    Granted only to a session that proves more than a password: the person's first sign-in ever
    (the claim and an invitation acceptance are first sign-ins: their proof is the setup link,
    the shared password or the invitation token), or the first sign-in after an admin reset their
    MFA (identity_users.first_factor_grant_until, consumed here). Any later password-only sign-in
    gets none: whoever holds only the password cannot bind their own factor to the account."""
    if mfa:
        return None
    if users.factors(db, user.principal_id).any_second_factor:
        return None
    now = now or utcnow()
    from sqlalchemy import text

    pending = db.execute(text(
        "UPDATE identity_users u SET first_factor_grant_until = NULL FROM identity_users old "
        "WHERE u.principal_id = :p AND old.principal_id = u.principal_id AND "
        "old.first_factor_grant_until IS NOT NULL RETURNING old.first_factor_grant_until"),
        {"p": user.principal_id}).scalar()
    if user.last_login_at is None or (pending is not None and pending > now):
        return now + timedelta(seconds=FIRST_FACTOR_WINDOW_SECONDS)
    return None


def grant_first_factor_after_reset(db: Any, principal_id: str) -> None:
    """An admin's MFA reset: the person's next sign-in may enrol a first factor (H1)."""
    from sqlalchemy import text

    db.execute(text("UPDATE identity_users SET first_factor_grant_until = now() + "
                    "make_interval(secs => :s) WHERE principal_id = :p"),
               {"s": RESET_GRANT_SECONDS, "p": principal_id})


PROMOTION_GRANT_SECONDS = 72 * 3600


def grant_first_factor_on_promotion(db: Any, principal_ids: Any, *, was_required: dict) -> list[str]:
    """Integration round 2 (b): a person PROMOTED into an MFA-required role (a binding, or joining a
    group that holds one) who has no second factor would otherwise be stranded — every later
    password-only sign-in is enrolment-less (H1). Like an admin's MFA reset, their next sign-in may
    enrol a FIRST factor within PROMOTION_GRANT_SECONDS. `was_required` is principal -> bool taken
    before the change; only people for whom the change made MFA required get the window, and an
    existing window is never extended. Returns who got one."""
    from sqlalchemy import text

    granted = []
    for pid in principal_ids:
        if was_required.get(pid, True):
            continue
        user = users.by_id(db, pid)
        if user is None or user.network_bound or not users.mfa_required(db, user):
            continue
        if users.factors(db, pid).any_second_factor:
            continue
        db.execute(text("UPDATE identity_users SET first_factor_grant_until = now() + "
                        "make_interval(secs => :s) WHERE principal_id = :p AND "
                        "(first_factor_grant_until IS NULL OR first_factor_grant_until < now())"),
                   {"s": PROMOTION_GRANT_SECONDS, "p": pid})
        granted.append(pid)
    return granted


def mfa_required_now(db: Any, principal_ids: Any) -> dict:
    """principal -> is MFA required for them right now (people only; others count as required, so
    the promotion grant never applies to them)."""
    out = {}
    for pid in principal_ids:
        user = users.by_id(db, pid)
        out[pid] = True if user is None else bool(users.mfa_required(db, user))
    return out


def may_enrol(db: Any, row: sessions.SessionRow, user: users.UserRow) -> Optional[tuple[int, str]]:
    """May this session add a second factor? None when yes, else (status, error).

      - fresh MFA on this session (10 minutes) always may;
      - a person who already has a factor: only with that fresh MFA (step up first);
      - the FIRST factor of a person whose roles require MFA: this session's first-factor grant
        (claim, invitation, first sign-in, or after an admin reset), never a later password-only
        sign-in — 403 `enrolment_grant_required` (ask an admin to reset your second factor);
      - the first factor of anyone else: a sign-in within the last 10 minutes."""
    now = utcnow()
    if row.mfa_at is not None and (now - row.mfa_at).total_seconds() <= STEP_UP_WINDOW_SECONDS:
        return None
    if users.factors(db, user.principal_id).any_second_factor:
        return 401, "step_up_required"
    if users.mfa_required(db, user):
        if row.enrol_grant_until is None or now >= row.enrol_grant_until:
            return 403, "enrolment_grant_required"
        return None
    if not recently_authenticated(row):
        return 401, "step_up_required"
    return None


#: The first-factor label of a reset link minted by the host shell (`boss identity recover-owner`).
#: Never recorded on a session; only `sso_refusal` reads it (the Owner break-glass under Enforce SSO).
HOST_RECOVERY = "host_recovery"


def sso_refusal(db: Any, user: users.UserRow, facts: RequestFacts, *,
                method: str) -> Optional[Answer]:
    """Enforce SSO (IDENTITY.md §4.8), for EVERY first factor in one place: when an active
    provider enforces SSO for the person's email domain, only that provider signs them in — not a
    password, an email link or code, or another provider. A passkey still works for an Owner (the
    break-glass), for nobody else. None: this method may proceed."""
    from sqlalchemy import text

    domain = user.email.rsplit("@", 1)[-1].lower() if user.email and "@" in user.email else ""
    if not domain:
        return None
    enforcing = {r[0] for r in db.execute(text(
        "SELECT id FROM identity_providers WHERE status = 'active' AND enforce_sso AND "
        ":d = ANY(domains)"), {"d": domain})}
    if not enforcing:
        return None
    if method.startswith("sso:") and method[4:] in enforcing:
        return None
    if method in ("passkey", HOST_RECOVERY) and users.is_owner(db, user.principal_id, user.org_id):
        # The Owner break-glass: their passkey, or a reset link minted by `boss identity
        # recover-owner` on the host shell (proof of the machine, stronger than any IdP).
        return None
    emit(db, org_id=user.org_id, event_type="auth.login.failed", outcome=Outcome.DENIED,
         target_type="user", target_id=user.principal_id, ip=facts.ip,
         user_agent=facts.user_agent, request_id=facts.request_id,
         details={"reason": "sso_required", "method": method.split(":", 1)[0]})
    return Answer(403, {"error": "sso_required"})


def finish_first_factor(db: Any, user: users.UserRow, facts: RequestFacts, *, method: str,
                        email_method: bool = False, idp_mfa: bool = False) -> Answer:
    """After a first factor: MFA if the person has a second factor (or the IdP vouched for MFA),
    otherwise a session. An email method never satisfies an MFA-required role (A87). Enforce SSO
    is checked here, so every first factor (password, email link or code, any provider) obeys it."""
    refused = sso_refusal(db, user, facts, method=method)
    if refused is not None:
        return refused
    f = users.factors(db, user.principal_id)
    if idp_mfa:
        return finish(db, user, facts, methods=[method], mfa=True)
    if f.any_second_factor:
        return begin_mfa(db, user, facts, first=method)
    if email_method and users.mfa_required(db, user):
        return Answer(401, {"error": "second_factor_required"})
    return finish(db, user, facts, methods=[method], mfa=False)


# ---- password -------------------------------------------------------------------------------------

_DELAY_SLOTS = threading.BoundedSemaphore(8)


def _progressive_delay(failures: int) -> None:
    """1, 2, 4 … s (cap 30) after the 5th consecutive failure. Bounded concurrency, so the delay
    itself cannot exhaust the worker pool; the per-IP limits and the soft lock still apply."""
    if failures < ACCOUNT_DELAY_AFTER:
        return
    seconds = min(2 ** (failures - ACCOUNT_DELAY_AFTER), MAX_DELAY_SECONDS)
    if _DELAY_SLOTS.acquire(blocking=False):
        try:
            time.sleep(seconds)
        finally:
            _DELAY_SLOTS.release()


#: Tests set this to 0 to keep the suite fast; the default is the spec's schedule.
DELAY_ENABLED = True


def _source_keys(email: str, facts: RequestFacts) -> list[tuple[str, str]]:
    """The buckets one sign-in source fills for an account (L2): this client IP, and this device
    when it sends its marker cookie (no cookie is no device: never one bucket all cookieless
    browsers share). Keyed through the server key like every bucket."""
    keys = [("signin.account_ip", f"{email.lower()}|{facts.ip or 'unknown'}")]
    if facts.device and len(facts.device) <= 100:
        keys.append(("signin.account_device", f"{email.lower()}|{sha256_hex(facts.device)}"))
    return keys


def _source_failures(db: Any, email: str, facts: RequestFacts) -> int:
    return max(ratelimit.peek(bucket, ident, window_seconds=ACCOUNT_WINDOW_SECONDS, db=db)
               for bucket, ident in _source_keys(email, facts))


def _count_failure(db: Any, email: str, facts: RequestFacts) -> None:
    for bucket, ident in [("signin.account", email), *_source_keys(email, facts)]:
        ratelimit.hit(bucket, ident, limit=10 ** 6, window_seconds=ACCOUNT_WINDOW_SECONDS, db=db)


def _backoff_failures(db: Any, email: str, facts: RequestFacts) -> int:
    """The CAPTCHA-free backoff (L2): this source's own failures drive the full 1, 2, 4 … 30 s
    delay; the account-wide count (many sources at once) only a gentle one, capped at 4 s, so a
    distributed guesser is slowed and the real person is never made to wait long."""
    own = _source_failures(db, email, facts)
    account = ratelimit.peek("signin.account", email, window_seconds=ACCOUNT_WINDOW_SECONDS, db=db)
    return max(own, min(account, ACCOUNT_DELAY_AFTER + 2) if account >= 2 * ACCOUNT_LOCK_AFTER else 0)


def password_login(db: Any, facts: RequestFacts, *, email: str, password: str) -> Answer:
    if not (email or "").strip():
        # no email: the shared console password (Operator for 30 days after the claim)
        return shared_password_login(db, facts, password=password)
    limited = ip_limited(db, facts)
    if limited:
        return limited
    email = (email or "").strip()
    user = users.by_email(db, email) if email else None
    org_id = user.org_id if user else default_org_id(db)
    familiar = bool(user) and sessions.device_is_familiar(db, user.principal_id, facts.device)
    # L2: the soft lock is per SOURCE (this device, this IP), never per account: someone failing
    # against a victim's address locks their own device and address, not the victim's sign-in.
    failures = _source_failures(db, email, facts)
    if not familiar and failures >= ACCOUNT_LOCK_AFTER:
        # Soft lock of the PASSWORD factor from unfamiliar devices. Same body, same work (A2, A3).
        verify_password(password, None)
        if org_id:
            emit(db, org_id=org_id, event_type="auth.locked_soft", outcome=Outcome.DENIED,
                 target_type="user" if user else None, target_id=user.principal_id if user else None,
                 ip=facts.ip, user_agent=facts.user_agent, request_id=facts.request_id,
                 details={"identifier": identifier_hash(email)})
        return Answer(401, dict(INVALID))
    ok = verify_password(password, user.password_hash if user else None)
    if not ok:
        _count_failure(db, email, facts)
        if org_id:
            emit(db, org_id=org_id, event_type="auth.login.failed", outcome=Outcome.FAILURE,
                 target_type="user" if user else None, target_id=user.principal_id if user else None,
                 ip=facts.ip, user_agent=facts.user_agent, request_id=facts.request_id,
                 details={"identifier": identifier_hash(email),
                          "reason": "bad_password" if user else "unknown_identifier"})
        if DELAY_ENABLED and not familiar:
            _progressive_delay(_backoff_failures(db, email, facts))
        return Answer(401, dict(INVALID))
    assert user is not None
    if user.status == "disabled":
        emit(db, org_id=user.org_id, event_type="auth.login.failed", outcome=Outcome.DENIED,
             target_type="user", target_id=user.principal_id, ip=facts.ip,
             user_agent=facts.user_agent, request_id=facts.request_id, details={"reason": "disabled"})
        return Answer(403, dict(DISABLED))
    if user.status != "active":
        return Answer(401, dict(INVALID))
    if sso_enforced_for(db, user.email):
        return Answer(403, {"error": "sso_required"})
    if needs_rehash(user.password_hash):
        try:
            users.rehash_quietly(db, user.principal_id, hash_password(password))
        except PasswordBusy:
            pass   # upgrade next time
    return finish_first_factor(db, user, facts, method="pwd")


#: The rate-limit identity of the shared console password (it has no email of its own).
SHARED_IDENTIFIER = "(shared console password)"
SHARED_RETIRED = {"error": "shared_password_retired",
                  "message": "The shared password no longer signs in. Ask an Owner to invite you."}


def shared_password_login(db: Any, facts: RequestFacts, *, password: str) -> Answer:
    """The shared console password, signed in WITHOUT an email (docs/community/IDENTITY_UPGRADE.md).

    Before the first Owner is claimed it only claims: 409 `claim_first`. From the claim it signs in
    as `usr_legacy_shared` with the Operator role until the legacy cutoff (claim + 30 days, plus at
    most one audited Owner extension of at most 30 days); the session never outlives that cutoff,
    and every page shows the countdown banner. After the cutoff: 403 `shared_password_retired`.
    A wrong password is the same uniform failure, with the same per-source backoff, as any other."""
    from . import legacy

    limited = ip_limited(db, facts)
    if limited:
        return limited
    org_id = default_org_id(db)
    if _source_failures(db, SHARED_IDENTIFIER, facts) >= ACCOUNT_LOCK_AFTER:
        if org_id:
            emit(db, org_id=org_id, event_type="auth.locked_soft", outcome=Outcome.DENIED,
                 ip=facts.ip, user_agent=facts.user_agent, request_id=facts.request_id,
                 details={"identifier": identifier_hash(SHARED_IDENTIFIER)})
        return Answer(401, dict(INVALID))
    result = legacy.check_shared_password(password or "", db=db)
    if result.status in ("invalid", "not_configured"):
        _count_failure(db, SHARED_IDENTIFIER, facts)
        if org_id:
            emit(db, org_id=org_id, event_type="auth.login.failed", outcome=Outcome.FAILURE,
                 ip=facts.ip, user_agent=facts.user_agent, request_id=facts.request_id,
                 details={"identifier": identifier_hash(SHARED_IDENTIFIER),
                          "reason": "bad_shared_password"})
        if DELAY_ENABLED:
            _progressive_delay(_source_failures(db, SHARED_IDENTIFIER, facts))
        return Answer(401, dict(INVALID))
    if result.status == "claim":
        return Answer(409, {"error": "claim_first",
                            "message": "This BOSS has no Owner yet. Claim it first."})
    principal = users.load_principal(db, legacy.LEGACY_SHARED_PRINCIPAL)
    if result.status != "ok" or principal is None or not principal.active:
        return Answer(403, dict(SHARED_RETIRED))
    if not secure_origin_ok(facts.origin or facts.request_origin):
        return Answer(400, {"error": "insecure_origin"})
    now = utcnow()
    _idle, absolute = sessions._lifetimes(db, principal.org_id)  # noqa: SLF001
    until = result.retire_after
    ends = min(now + absolute, until) if until else now + absolute
    device = facts.device if (facts.device and len(facts.device) <= 100) else random_token("bdv_", 24)
    issued = sessions.issue(db, principal_id=principal.id, org_id=principal.org_id,
                            methods=["legacy_password"], mfa_at=None, ip=facts.ip,
                            user_agent=facts.user_agent, device_id_hash=sha256_hex(device),
                            absolute_expires_at=ends)
    for bucket, ident in [("signin.account", SHARED_IDENTIFIER),
                          *_source_keys(SHARED_IDENTIFIER, facts)]:
        ratelimit.reset(bucket, ident, db=db)
    ctx = AuthContext(principal, AuthMethod.LEGACY_PASSWORD, legacy="shared_password", ip=facts.ip,
                      user_agent=facts.user_agent, request_id=facts.request_id)
    emit(db, org_id=principal.org_id, event_type="session.created", outcome=Outcome.SUCCESS, ctx=ctx,
         session_ref=issued.session_id, details={"methods": ["legacy_password"]})
    emit(db, org_id=principal.org_id, event_type="auth.login.succeeded", outcome=Outcome.SUCCESS,
         ctx=ctx, session_ref=issued.session_id,
         details={"methods": ["legacy_password"], "mfa": False})
    emit(db, org_id=principal.org_id, event_type="legacy.password_used", outcome=Outcome.INFO,
         ctx=ctx, target_type="legacy_path", target_id="shared_password",
         details={"path": "shared_password", "until": until.isoformat() if until else None})
    legacy._count_use(principal.org_id, "shared_password", "BOSS_CONSOLE_PASSWORD",  # noqa: SLF001
                      principal.id, now)
    legacy.note_sunset(principal.org_id, "shared_password", principal.id, until)
    max_age = max(1, int((issued.absolute_expires_at - now).total_seconds()))
    cookies = [sessions.set_cookie_header(SESSION_COOKIE, issued.token, max_age=max_age),
               sessions.set_cookie_header(DEVICE_COOKIE, device, max_age=DEVICE_MAX_AGE),
               sessions.clear_cookie_header(PREAUTH_COOKIE)]
    return Answer(200, {"next": "done", "methods": [],
                        "who": {"display_name": principal.display_name, "email": "", "hue": 0},
                        "session_id": issued.session_id,
                        "expires_at": issued.absolute_expires_at.isoformat(),
                        "shared_password_until": until.isoformat() if until else None},
                  cookies=cookies)


def sso_enforced_for(db: Any, email: str) -> bool:
    from sqlalchemy import text

    domain = email.rsplit("@", 1)[-1].lower() if "@" in email else ""
    if not domain:
        return False
    return db.execute(text("SELECT 1 FROM identity_providers WHERE status = 'active' AND enforce_sso "
                           "AND :d = ANY(domains) LIMIT 1"), {"d": domain}).first() is not None


# ---- the MFA step ---------------------------------------------------------------------------------

def _mfa_challenge(db: Any, facts: RequestFacts) -> Optional[Any]:
    ch = latest_bound_challenge(db, kind="mfa", binding_secret=facts.preauth)
    if ch is None or "first" not in ch.payload or not ch.principal_id:
        return None
    return ch


def _mfa_user(db: Any, ch: Any) -> Optional[users.UserRow]:
    user = users.by_id(db, ch.principal_id)
    return user if user and user.status == "active" else None


def mfa_totp(db: Any, facts: RequestFacts, *, code: str) -> Answer:
    ch = _mfa_challenge(db, facts)
    user = _mfa_user(db, ch) if ch else None
    if ch is None or user is None:
        return Answer(401, {"error": "mfa_challenge_invalid"})
    limited = ratelimit.hit("mfa.user", user.principal_id, limit=totp.PER_USER_PER_HOUR,
                            window_seconds=3600, db=db)
    if not limited.allowed:
        return too_many(limited.retry_after_seconds)
    if not totp.verify(db, user.principal(), code):
        attempts = count_attempt(db, ch.id_hash, max_attempts=totp.MAX_ATTEMPTS_PER_CHALLENGE)
        ratelimit.hit("signin.account", user.email, limit=10 ** 6,
                      window_seconds=ACCOUNT_WINDOW_SECONDS, db=db)
        emit(db, org_id=user.org_id, event_type="auth.mfa.failed", outcome=Outcome.FAILURE,
             ctx=_ctx_for(user, facts), details={"method": "totp", "attempt": attempts})
        return Answer(401, {"error": "invalid_code"})
    if not consume_challenge(db, ch.id_hash):
        return Answer(401, {"error": "mfa_challenge_invalid"})
    emit(db, org_id=user.org_id, event_type="auth.mfa.succeeded", outcome=Outcome.SUCCESS,
         ctx=_ctx_for(user, facts), details={"method": "totp"})
    return finish(db, user, facts, methods=[ch.payload["first"], "totp"], mfa=True)


def mfa_recovery(db: Any, facts: RequestFacts, *, code: str) -> Answer:
    ch = _mfa_challenge(db, facts)
    user = _mfa_user(db, ch) if ch else None
    if ch is None or user is None:
        return Answer(401, {"error": "mfa_challenge_invalid"})
    limited = ratelimit.hit("mfa.user", user.principal_id, limit=totp.PER_USER_PER_HOUR,
                            window_seconds=3600, db=db)
    if not limited.allowed:
        return too_many(limited.retry_after_seconds)
    if not recovery.use(db, user.principal_id, code):
        count_attempt(db, ch.id_hash, max_attempts=totp.MAX_ATTEMPTS_PER_CHALLENGE)
        ratelimit.hit("signin.account", user.email, limit=10 ** 6,
                      window_seconds=ACCOUNT_WINDOW_SECONDS, db=db)
        emit(db, org_id=user.org_id, event_type="auth.mfa.failed", outcome=Outcome.FAILURE,
             ctx=_ctx_for(user, facts), details={"method": "recovery_code"})
        return Answer(401, {"error": "invalid_code"})
    consume_challenge(db, ch.id_hash)
    remaining = recovery.remaining(db, user.principal_id)
    emit(db, org_id=user.org_id, event_type="recovery_code.used", outcome=Outcome.SUCCESS,
         ctx=_ctx_for(user, facts), details={"remaining": remaining})
    notify(user.email, "A BOSS recovery code was used",
           "One of your BOSS recovery codes was just used to sign in. If this was not you, "
           "contact an administrator now.")
    return finish(db, user, facts, methods=[ch.payload["first"], "recovery_code"], mfa=True,
                  extra={"recovery_codes_remaining": remaining,
                         "must_regenerate_recovery_codes": remaining < recovery.FORCE_REGENERATE_BELOW})


def notify(email: str, subject: str, body: str) -> None:
    from .authn_support import send_mail, smtp_configured

    if smtp_configured() and email:
        send_mail(email, subject, body)


# ---- passkeys ---------------------------------------------------------------------------------------

def passkey_options(db: Any, facts: RequestFacts, *, purpose: str) -> Answer:
    """`login`: a discoverable-credential challenge bound to a NEW pre-auth cookie. `mfa`: bound to
    the pending MFA challenge's browser and person. `step_up`: bound to the session."""
    try:
        if purpose == "login":
            limited = ip_limited(db, facts)      # a passkey sign-in counts toward the IP limit
            if limited:
                return limited
            secret, cookie = _new_preauth()
            options = passkeys.authentication_options(db, purpose="login", origin=facts.origin,
                                                      binding_secret=secret)
            return Answer(200, {"publicKey": options}, cookies=[cookie])
        if purpose == "mfa":
            ch = _mfa_challenge(db, facts)
            if ch is None:
                return Answer(401, {"error": "mfa_challenge_invalid"})
            options = passkeys.authentication_options(db, purpose="mfa", origin=facts.origin,
                                                      binding_secret=facts.preauth or "",
                                                      principal_id=ch.principal_id)
            return Answer(200, {"publicKey": options})
        if purpose == "step_up":
            row = sessions.lookup(db, facts.session)
            if row is None:
                return Answer(401, {"error": "unauthenticated"})
            options = passkeys.authentication_options(db, purpose="step_up", origin=facts.origin,
                                                      binding_secret=facts.session or "",
                                                      principal_id=row.principal_id)
            return Answer(200, {"publicKey": options})
    except passkeys.PasskeyError as exc:
        return Answer(409 if exc.code in ("unavailable", "needs_domain", "needs_https") else 400, {"error": f"passkey_{exc.code}"})
    return Answer(422, {"error": "invalid_purpose"})


def passkey_verify(db: Any, facts: RequestFacts, *, credential: Any, purpose: str) -> Answer:
    binding = facts.session if purpose == "step_up" else facts.preauth
    try:
        verified = passkeys.verify_authentication(db, purpose=purpose, origin=facts.origin,
                                                  binding_secret=binding or "", credential=credential)
    except passkeys.PasskeyError as exc:
        org = default_org_id(db)
        if org and exc.regressed is not None:
            emit(db, org_id=org, event_type="passkey.counter_regressed", outcome=Outcome.DENIED,
                 ip=facts.ip, user_agent=facts.user_agent, request_id=facts.request_id,
                 target_type="passkey", target_id=exc.regressed.passkey_id,
                 details={"blocked": True, "principal": exc.regressed.principal_id})
        if org:
            emit(db, org_id=org, event_type="auth.passkey.failed", outcome=Outcome.FAILURE,
                 ip=facts.ip, user_agent=facts.user_agent, request_id=facts.request_id,
                 details={"reason": exc.code, "purpose": purpose})
        if exc.code == "unavailable":
            return Answer(409, {"error": "passkey_unavailable"})
        return Answer(401, {"error": "passkey_failed"})
    user = users.by_id(db, verified.principal_id)
    if user is None:
        return Answer(401, {"error": "passkey_failed"})
    ctx = _ctx_for(user, facts)
    if verified.counter_regressed:
        emit(db, org_id=user.org_id, event_type="passkey.counter_regressed", outcome=Outcome.INFO,
             ctx=ctx, target_type="passkey", target_id=verified.passkey_id)
    if user.status == "disabled":
        return Answer(403, dict(DISABLED))     # the passkey proved possession: safe to say
    if user.status != "active":
        return Answer(401, {"error": "passkey_failed"})
    emit(db, org_id=user.org_id, event_type="auth.passkey.succeeded", outcome=Outcome.SUCCESS,
         ctx=ctx, target_type="passkey", target_id=verified.passkey_id, details={"purpose": purpose})
    if purpose == "login":
        refused = sso_refusal(db, user, facts, method="passkey")
        if refused is not None:
            return refused
        return finish(db, user, facts, methods=["passkey"], mfa=True)
    if purpose == "mfa":
        ch = _mfa_challenge(db, facts)
        if ch is None or ch.principal_id != user.principal_id or not consume_challenge(db, ch.id_hash):
            return Answer(401, {"error": "mfa_challenge_invalid"})
        emit(db, org_id=user.org_id, event_type="auth.mfa.succeeded", outcome=Outcome.SUCCESS,
             ctx=ctx, details={"method": "passkey"})
        return finish(db, user, facts, methods=[ch.payload["first"], "passkey"], mfa=True)
    return step_up_finish(db, user, facts, method="passkey")


# ---- step-up --------------------------------------------------------------------------------------

def step_up(db: Any, facts: RequestFacts, *, method: str, code: Optional[str] = None,
            password: Optional[str] = None) -> Answer:
    """Fresh MFA for 10 minutes (§4.6). TOTP, or the password ONLY for a person with no second
    factor whose roles do not require MFA. Passkeys go through passkey_verify(purpose=step_up).
    Bearer credentials never reach here: there is no session to rotate."""
    row = sessions.lookup(db, facts.session)
    user = users.by_id(db, row.principal_id) if row else None
    if row is None or user is None or user.status != "active":
        return Answer(401, {"error": "unauthenticated"})
    limited = ratelimit.hit("mfa.user", user.principal_id, limit=totp.PER_USER_PER_HOUR,
                            window_seconds=3600, db=db)
    if not limited.allowed:
        return too_many(limited.retry_after_seconds)
    ok = False
    if method == "totp":
        ok = totp.verify(db, user.principal(), code or "")
    elif method == "password":
        if users.factors(db, user.principal_id).any_second_factor or users.mfa_required(db, user):
            return Answer(422, {"error": "second_factor_required"})
        ok = verify_password(password or "", user.password_hash)
    else:
        return Answer(422, {"error": "invalid_method"})
    if not ok:
        emit(db, org_id=user.org_id, event_type="auth.mfa.failed", outcome=Outcome.FAILURE,
             ctx=_ctx_for(user, facts, row.ref()), details={"method": method, "purpose": "step_up"})
        return Answer(401, {"error": "invalid_code" if method == "totp" else "invalid_credentials"})
    return step_up_finish(db, user, facts, method=method)


def step_up_finish(db: Any, user: users.UserRow, facts: RequestFacts, *, method: str) -> Answer:
    row = sessions.lookup(db, facts.session)
    if row is None or row.principal_id != user.principal_id:
        return Answer(401, {"error": "unauthenticated"})
    issued = sessions.issue(db, principal_id=user.principal_id, org_id=user.org_id,
                            methods=[*row.auth_methods, method], mfa_at=utcnow(), ip=facts.ip,
                            user_agent=facts.user_agent, device_id_hash=row.device_id_hash,
                            replaces=row.id, absolute_expires_at=row.absolute_expires_at)
    ctx = _ctx_for(user, facts, row.ref())
    emit(db, org_id=user.org_id, event_type="auth.step_up", outcome=Outcome.SUCCESS, ctx=ctx,
         details={"method": method})
    emit(db, org_id=user.org_id, event_type="session.rotated", outcome=Outcome.INFO, ctx=ctx,
         details={"replaced_by": issued.session_id, "reason": "step_up"})
    max_age = int((issued.absolute_expires_at - utcnow()).total_seconds())
    fresh_until = utcnow() + timedelta(seconds=STEP_UP_WINDOW_SECONDS)
    return Answer(200, {"next": "done", "session_id": issued.session_id,
                        "step_up_fresh_until": fresh_until.isoformat()},
                  cookies=[sessions.set_cookie_header(SESSION_COOKIE, issued.token, max_age=max_age)])


def logout(db: Any, facts: RequestFacts) -> Answer:
    row = sessions.lookup(db, facts.session)
    if row is not None:
        sessions.revoke(db, row.id, by=row.principal_id, reason="logout")
        user = users.by_id(db, row.principal_id)
        if user:
            emit(db, org_id=user.org_id, event_type="auth.logout", outcome=Outcome.SUCCESS,
                 ctx=_ctx_for(user, facts, row.ref()))
    return Answer(200, {"signed_out": True}, cookies=[sessions.clear_cookie_header(SESSION_COOKIE)])


def recently_authenticated(row: sessions.SessionRow, *, seconds: int = 600) -> bool:
    """Signed in, or stepped up, within `seconds` (TOTP enrolment, passkey registration)."""
    newest: datetime = max(row.created_at, row.mfa_at or row.created_at)
    return (utcnow() - newest).total_seconds() <= seconds


def require_fresh(row: Optional[sessions.SessionRow], *, seconds: int = 600) -> None:
    if row is None:
        raise AuthnError("unauthenticated")
    if row.mfa_at is None or (utcnow() - row.mfa_at).total_seconds() > seconds:
        raise AuthnError("step_up_required", 401)
