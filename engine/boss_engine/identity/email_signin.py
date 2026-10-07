"""Everything that goes through a person's mailbox (IDENTITY.md §4.7, §4.14, §4.15; D26, D30, D32):
magic links, email codes, password reset and self sign-up.

  - SMTP only. Without it the email sign-in and sign-up endpoints answer 404 (A17, A91).
  - EMAILED ONLY. No response body, log line or redirect carries a link, token or code (A81, A85,
    A88). Links put the token in the URL fragment; the console POSTs it.
  - Uniform: every request answers `202 {"sent": true}` with the same bytes and similar timing
    whether or not the address has an account; the mail itself is sent off the request path.
  - Single use, 10 minutes (30 for a reset), hashed at rest: SHA-256 of a link token; argon2id of a
    code, and that hash itself sits only inside the browser-keyed encrypted payload (A84).
  - Bound to the browser that asked (`__Host-boss_preauth`), except password reset (§4.7).
  - Rate limits: per address (HMAC) 5/h and per IP 20/h, shared by magic link and email code;
    5 attempts per code, then it is burned (A86).
  - Never around MFA: an MFA-required person still completes a second factor (A87).
"""
from __future__ import annotations

import secrets
import threading
import time
from typing import Any, Optional

from . import ratelimit, recovery, sessions, users
from .audit import Outcome
from .authn_support import (
    challenge_by_secret,
    consume_challenge,
    count_attempt,
    create_challenge,
    default_org_id,
    emit,
    get_policy,
    identifier_hash,
    latest_bound_challenge,
    pad_to,
    random_token,
    send_mail,
    sha256_hex,
    smtp_configured,
)
from .passwords import PasswordRejected, check_new_password, hash_code, hash_password, verify_code
from .signin import Answer, RequestFacts, finish_first_factor, finish, too_many

EMAIL_TTL_SECONDS = 600
CODE_ATTEMPTS = 5
PER_ADDRESS_PER_HOUR = 5
PER_IP_PER_HOUR = 20
#: Every uniform response takes at least this long, so a known address (which mints a challenge)
#: and an unknown one (dummy work) cannot be told apart by timing (A85).
UNIFORM_FLOOR_SECONDS = 0.35
SENT = {"sent": True}
NOT_FOUND = {"error": "not_found"}
#: H2: no emailed link while BOSS has no bound public origin (BOSS_PUBLIC_URL or the claim's).
#: The same answer for every address, so it is no oracle; posture says why and how to fix it.
NO_PUBLIC_ORIGIN = {"error": "public_origin_unset",
                    "message": "BOSS has no public URL yet, so it sends no links. Set BOSS_PUBLIC_URL."}


def available() -> bool:
    return smtp_configured()


def _send_async(to: str, subject: str, body: str) -> None:
    """Off the request path: delivery time must not become a timing oracle."""
    threading.Thread(target=send_mail, args=(to, subject, body), daemon=True).start()


#: Tests replace this to capture mail synchronously.
DELIVER = _send_async


def _uniform(started: float, cookies: Optional[list[str]] = None) -> Answer:
    pad_to(started, UNIFORM_FLOOR_SECONDS)
    return Answer(202, dict(SENT), cookies=cookies or [])


def _limited(db: Any, bucket: str, email: str, ip: Optional[str]) -> Optional[Answer]:
    for b, ident, limit in ((f"{bucket}.account", email.strip().lower(), PER_ADDRESS_PER_HOUR),
                            (f"{bucket}.ip", ip or "unknown", PER_IP_PER_HOUR)):
        decision = ratelimit.hit(b, ident, limit=limit, window_seconds=3600, db=db)
        if not decision.allowed:
            return too_many(decision.retry_after_seconds)
    return None


def _new_preauth() -> tuple[str, str]:
    from .authn import PREAUTH_COOKIE

    secret = random_token("bpa_")
    return secret, sessions.set_cookie_header(PREAUTH_COOKIE, secret, max_age=EMAIL_TTL_SECONDS + 300)


def _eligible(db: Any, email: str) -> Optional[users.UserRow]:
    """An active person whose address is VERIFIED (M2): mail sign-in and reset go only to an
    address someone proved they read, never to one an admin, SCIM or an IdP merely asserted.
    An unverified address is treated exactly like an unknown one."""
    user = users.by_email(db, email) if email and "@" in email else None
    if user is None or user.status != "active" or user.email_verified_at is None:
        return None
    return user


def link_origin(facts: RequestFacts) -> Optional[str]:
    """The origin an emailed link points at: the BOUND public origin only, never the request's
    Host (H2, A11)."""
    return facts.origin or None


# ---- magic link and email code --------------------------------------------------------------------

def request_sign_in(db: Any, facts: RequestFacts, *, kind: str, email: str) -> Answer:
    """`kind` is magic_link or email_code. 404 without SMTP or when the policy turned it off."""
    started = time.monotonic()
    if kind not in ("magic_link", "email_code") or not available():
        return Answer(404, dict(NOT_FOUND))
    org_id = default_org_id(db)
    pol = get_policy(org_id, db=db) if org_id else None
    if pol is not None and not (pol.email_magic_link if kind == "magic_link" else pol.email_code):
        return Answer(404, dict(NOT_FOUND))
    if kind == "magic_link" and not link_origin(facts):
        return Answer(409, dict(NO_PUBLIC_ORIGIN))
    email = (email or "").strip()
    limited = _limited(db, "email_signin", email, facts.ip)
    if limited:
        return limited
    secret, cookie = _new_preauth()
    user = _eligible(db, email)
    if org_id:
        emit(db, org_id=org_id, event_type=f"auth.{kind}.requested", outcome=Outcome.INFO,
             target_type="user" if user else None, target_id=user.principal_id if user else None,
             ip=facts.ip, user_agent=facts.user_agent, request_id=facts.request_id,
             details={"identifier": identifier_hash(email), "known": bool(user)})
    if kind == "magic_link":
        token = random_token()
        if user:
            create_challenge(db, kind="magic_link", binding_secret=secret, ttl_seconds=EMAIL_TTL_SECONDS,
                             principal_id=user.principal_id, secret=token)
            link = f"{link_origin(facts)}/signin/link#t={token}"
            DELIVER(user.email, "Your BOSS sign-in link",
                    f"Sign in to BOSS with this link. It works once, for 10 minutes, in the browser "
                    f"where you asked for it:\n\n{link}\n\nIf you did not ask, ignore this email.")
        else:
            sha256_hex(token)  # the same cheap work as a known address
    else:
        code = f"{secrets.randbelow(10 ** 6):06d}"
        code_hash = hash_code(code)          # dummy work for an unknown address, too
        if user:
            create_challenge(db, kind="email_code", binding_secret=secret, ttl_seconds=EMAIL_TTL_SECONDS,
                             principal_id=user.principal_id, payload={"h": code_hash})
            DELIVER(user.email, "Your BOSS sign-in code",
                    f"Your BOSS sign-in code is {code}. It works once, for 10 minutes, in the browser "
                    f"where you asked for it. If you did not ask, ignore this email.")
    return _uniform(started, [cookie])


#: The one answer to a magic link that signs nobody in: unknown, used, expired, or opened in a
#: browser other than the one that asked. Indistinguishable on purpose.
LINK_REFUSED = {"error": "link_invalid",
                "message": "This link no longer works here. Links work once, for 10 minutes, in the "
                           "browser where you asked for them. Ask for a new one."}


def verify_magic_link(db: Any, facts: RequestFacts, *, token: str) -> Answer:
    if not available():
        return Answer(404, dict(NOT_FOUND))
    org_id = default_org_id(db)
    ch = challenge_by_secret(db, kind="magic_link", secret=token, binding_secret=facts.preauth,
                             require_binding=True)
    if ch is None:
        unbound = challenge_by_secret(db, kind="magic_link", secret=token, lock=False)
        if org_id:
            emit(db, org_id=org_id, event_type="auth.magic_link.rejected", outcome=Outcome.FAILURE,
                 ip=facts.ip, user_agent=facts.user_agent, request_id=facts.request_id,
                 details={"reason": "other_browser" if unbound else "invalid_or_used"})
        # A83 refuses the other browser — and says so in ONE answer for every failure. A
        # distinct "wrong browser" answer told whoever holds a token that it is real and live (an
        # oracle for a guessed or leaked link); the audit event above keeps the difference.
        return Answer(401, dict(LINK_REFUSED))
    if not consume_challenge(db, ch.id_hash):
        return Answer(401, dict(LINK_REFUSED))
    user = users.by_id(db, ch.principal_id or "")
    if user is None or user.status != "active":
        return Answer(401, dict(LINK_REFUSED))
    emit(db, org_id=user.org_id, event_type="auth.magic_link.used", outcome=Outcome.SUCCESS,
         target_type="user", target_id=user.principal_id, ip=facts.ip, user_agent=facts.user_agent,
         request_id=facts.request_id)
    return finish_first_factor(db, user, facts, method="magic_link", email_method=True)


def verify_email_code(db: Any, facts: RequestFacts, *, code: str) -> Answer:
    if not available():
        return Answer(404, dict(NOT_FOUND))
    ch = latest_bound_challenge(db, kind="email_code", binding_secret=facts.preauth)
    user = users.by_id(db, ch.principal_id) if ch and ch.principal_id else None
    if ch is None or user is None or "h" not in ch.payload:
        verify_code("000000", hash_code("1"))   # same work as a wrong code
        return Answer(401, {"error": "code_invalid"})
    if not verify_code((code or "").strip(), ch.payload["h"]):
        attempts = count_attempt(db, ch.id_hash, max_attempts=CODE_ATTEMPTS)
        emit(db, org_id=user.org_id, event_type="auth.email_code.failed", outcome=Outcome.FAILURE,
             target_type="user", target_id=user.principal_id, ip=facts.ip,
             user_agent=facts.user_agent, request_id=facts.request_id, details={"attempt": attempts})
        return Answer(401, {"error": "code_invalid"})
    if not consume_challenge(db, ch.id_hash) or user.status != "active":
        return Answer(401, {"error": "code_invalid"})
    emit(db, org_id=user.org_id, event_type="auth.email_code.succeeded", outcome=Outcome.SUCCESS,
         target_type="user", target_id=user.principal_id, ip=facts.ip, user_agent=facts.user_agent,
         request_id=facts.request_id)
    return finish_first_factor(db, user, facts, method="email_code", email_method=True)


# ---- password reset --------------------------------------------------------------------------------

def forgot_password(db: Any, facts: RequestFacts, *, email: str) -> Answer:
    """Always 202 (anti-enumeration). With SMTP, an existing active person gets a 30-minute,
    single-use link on the bound public origin — never the request's Host (A11)."""
    started = time.monotonic()
    if available() and not link_origin(facts):
        return Answer(409, dict(NO_PUBLIC_ORIGIN))
    email = (email or "").strip()
    for bucket, ident, limit in (("reset.account", email.lower(), 5), ("reset.ip", facts.ip or "unknown", 20)):
        decision = ratelimit.hit(bucket, ident, limit=limit, window_seconds=3600, db=db)
        if not decision.allowed:
            return too_many(decision.retry_after_seconds)
    user = _eligible(db, email)
    token = random_token()
    if user and available():
        token = recovery.issue_reset_token(db, user.principal_id)
        emit(db, org_id=user.org_id, event_type="password.reset_requested", outcome=Outcome.INFO,
             target_type="user", target_id=user.principal_id, ip=facts.ip,
             user_agent=facts.user_agent, request_id=facts.request_id)
        DELIVER(user.email, "Reset your BOSS password",
                f"Reset your BOSS password with this link (30 minutes, once):\n\n"
                f"{link_origin(facts)}/reset#t={token}\n\nIf you did not ask, ignore this email.")
    else:
        sha256_hex(token)
    return _uniform(started)


def reset_password(db: Any, facts: RequestFacts, *, token: str, new_password: str) -> Answer:
    ch = challenge_by_secret(db, kind="password_reset", secret=token)
    user = users.by_id(db, ch.principal_id) if ch and ch.principal_id else None
    if ch is None or user is None or user.status != "active":
        return Answer(400, {"error": "reset_invalid"})
    try:
        check_new_password(new_password, email=user.email, policy=get_policy(user.org_id, db=db))
    except PasswordRejected as exc:
        return Answer(422, {"error": exc.code})
    if not consume_challenge(db, ch.id_hash):
        return Answer(400, {"error": "reset_invalid"})
    users.set_password_hash(db, user.principal_id, hash_password(new_password))
    recovery.void_reset_tokens(db, user.principal_id)
    revoked = sessions.revoke_all(db, user.principal_id, by=user.principal_id, reason="password reset")
    emit(db, org_id=user.org_id, event_type="password.reset_completed", outcome=Outcome.SUCCESS,
         target_type="user", target_id=user.principal_id, ip=facts.ip, user_agent=facts.user_agent,
         request_id=facts.request_id, details={"sessions_revoked": revoked})
    from .signin import notify

    notify(user.email, "Your BOSS password was reset",
           "Your BOSS password was just reset and every session was signed out. Review your API "
           "keys under My account. If this was not you, contact an administrator now.")
    # No session: a reset never bypasses MFA — the person signs in (and completes MFA) next.
    return Answer(200, {"next": "signin"})


# ---- self sign-up (off by default) ------------------------------------------------------------------

def _domain_allowed(email: str, domains: tuple[str, ...]) -> bool:
    if "@" not in email:
        return False
    domain = email.rsplit("@", 1)[1].lower()
    return not domains or domain in {d.lower().lstrip("@") for d in domains}


def signup_enabled(db: Any) -> bool:
    org_id = default_org_id(db)
    return bool(org_id and available() and get_policy(org_id, db=db).signup_enabled)


def request_signup(db: Any, facts: RequestFacts, *, name: str, email: str) -> Answer:
    started = time.monotonic()
    if not signup_enabled(db):
        return Answer(404, dict(NOT_FOUND))
    if not link_origin(facts):
        return Answer(409, dict(NO_PUBLIC_ORIGIN))
    org_id = default_org_id(db)
    assert org_id
    email = (email or "").strip()
    limited = _limited(db, "signup", email, facts.ip)
    if limited:
        return limited
    secret, cookie = _new_preauth()
    pol = get_policy(org_id, db=db)
    token = random_token()
    allowed = "@" in email and len(email) <= 320 and _domain_allowed(email, tuple(pol.signup_domains))
    exists = users.by_email(db, email) is not None if allowed else False
    if allowed and not exists:
        create_challenge(db, kind="self_signup_verify", binding_secret=secret,
                         ttl_seconds=EMAIL_TTL_SECONDS, secret=token,
                         payload={"email": email, "name": (name or "").strip()[:200]})
        emit(db, org_id=org_id, event_type="auth.signup.requested", outcome=Outcome.INFO,
             ip=facts.ip, user_agent=facts.user_agent, request_id=facts.request_id,
             details={"identifier": identifier_hash(email)})
        DELIVER(email, "Finish creating your BOSS account",
                f"Finish creating your BOSS account with this link (10 minutes, once, in the "
                f"browser where you asked):\n\n{link_origin(facts)}/signup/verify#t={token}\n\n"
                f"If you did not ask, ignore this email.")
    else:
        sha256_hex(token)
        emit(db, org_id=org_id, event_type="auth.signup.refused", outcome=Outcome.DENIED,
             ip=facts.ip, user_agent=facts.user_agent, request_id=facts.request_id,
             details={"identifier": identifier_hash(email),
                      "reason": "exists" if exists else "domain_not_allowed"})
    return _uniform(started, [cookie])


def complete_signup(db: Any, facts: RequestFacts, *, token: str, password: Optional[str]) -> Answer:
    if not signup_enabled(db):
        return Answer(404, dict(NOT_FOUND))
    ch = challenge_by_secret(db, kind="self_signup_verify", secret=token, binding_secret=facts.preauth,
                             require_binding=True)
    if ch is None or "email" not in ch.payload:
        return Answer(401, {"error": "link_invalid"})
    email = ch.payload["email"]
    org_id = default_org_id(db)
    assert org_id
    pol = get_policy(org_id, db=db)
    if not _domain_allowed(email, tuple(pol.signup_domains)) or users.by_email(db, email):
        consume_challenge(db, ch.id_hash)
        return Answer(401, {"error": "link_invalid"})
    password_hash = None
    if password:
        try:
            check_new_password(password, email=email, policy=pol)
        except PasswordRejected as exc:
            return Answer(422, {"error": exc.code})
        password_hash = hash_password(password)
    if not consume_challenge(db, ch.id_hash):
        return Answer(401, {"error": "link_invalid"})
    # A self-signed-up person holds NO access role until an admin binds one (A91).
    pid = users.create_user(db, org_id=org_id, email=email, display_name=ch.payload.get("name") or email,
                            password_hash=password_hash, email_verified=True, created_by="self_signup",
                            self_signed_up=True)
    user = users.by_id(db, pid)
    assert user is not None
    emit(db, org_id=org_id, event_type="auth.signup.completed", outcome=Outcome.SUCCESS,
         target_type="user", target_id=pid, ip=facts.ip, user_agent=facts.user_agent,
         request_id=facts.request_id)
    return finish(db, user, facts, methods=["magic_link" if not password else "pwd"], mfa=False,
                  event="auth.login.succeeded")
