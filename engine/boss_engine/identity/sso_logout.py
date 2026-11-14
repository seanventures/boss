"""SSO sessions and logout (BOSSAPP-1048).

A session an OIDC provider signed in remembers that provider, the subject, the IdP's session id
(`sid`) and the id_token. The id_token is the RP-initiated logout hint; it is SEALED under a key
derived from the session token (the same scheme as challenge payloads), so a database dump alone
cannot read it, and it is unreadable once the cookie is gone.

  record        at session issue (signin.finish), from RequestFacts.federation
  carry         a rotated session (step-up) keeps its provider and hint
  logout_url    sign-out of an SSO session: the IdP's end_session_endpoint with the hint and
                where to come back to (`post_logout_redirect_uri`), or None
  backchannel   an IdP's verified logout token ends every live session it names (by `sid`, else
                by `sub`) at that provider; its `jti` is spent once
"""
from __future__ import annotations

from typing import Any, Optional

from sqlalchemy import text

from .authn_support import _open, _seal, consume_challenge, create_challenge

_KIND = "sso_session"


def federation_of(provider_id: str, ident: Any) -> Optional[dict[str, Any]]:
    """What a session remembers of the IdP that signed it in (None: nothing worth keeping)."""
    if not getattr(ident, "subject", None):
        return None
    return {"provider_id": provider_id, "subject": str(ident.subject)[:500],
            "sid": getattr(ident, "sid", None), "id_token": getattr(ident, "id_token", None)}


def record(db: Any, *, session_id: str, token: str, federation: Optional[dict[str, Any]]) -> None:
    if not federation or not federation.get("provider_id"):
        return
    hint = federation.get("id_token")
    sealed = _seal(token, _KIND, session_id, {"id_token": hint}) if hint else None
    db.execute(text("UPDATE identity_sessions SET sso_provider_id = :p, sso_subject = :s, sso_sid = :sid, "
                    "sso_id_token_sealed = :h WHERE id = :id"),
               {"p": federation["provider_id"], "s": federation.get("subject"), "sid": federation.get("sid"),
                "h": sealed, "id": session_id})


def _hint(row: Any, token: Optional[str]) -> Optional[str]:
    if not (token and row.sso_id_token_sealed):
        return None
    try:
        return _open(token, _KIND, row.id, row.sso_id_token_sealed).get("id_token")
    except Exception:  # noqa: BLE001 — a hint that cannot be opened is no hint
        return None


def _row(db: Any, session_id: str) -> Any:
    return db.execute(text("SELECT id, sso_provider_id, sso_subject, sso_sid, sso_id_token_sealed "
                           "FROM identity_sessions WHERE id = :id"), {"id": session_id}).first()


def carry(db: Any, *, old_session_id: str, old_token: Optional[str], new_session_id: str, new_token: str) -> None:
    row = _row(db, old_session_id)
    if row is None or not row.sso_provider_id:
        return
    record(db, session_id=new_session_id, token=new_token,
           federation={"provider_id": row.sso_provider_id, "subject": row.sso_subject, "sid": row.sso_sid,
                       "id_token": _hint(row, old_token)})


def logout_url(db: Any, *, session_id: str, token: Optional[str], origin: Optional[str]) -> Optional[str]:
    """Where the browser goes after BOSS ends an SSO session: the IdP's sign-out, then back to
    `{origin}/login`. None for a non-SSO session, a provider without end_session_endpoint, or no
    origin to come back to."""
    from . import sso_oidc

    if not origin:
        return None
    row = _row(db, session_id)
    if row is None or not row.sso_provider_id:
        return None
    provider = sso_oidc.load_provider(db, row.sso_provider_id)
    if provider is None or provider.protocol != "oidc":
        return None
    return sso_oidc.end_session_url(provider, id_token=_hint(row, token),
                                    post_logout_redirect_uri=post_logout_redirect_uri(origin))


def post_logout_redirect_uri(origin: str) -> str:
    return f"{origin.rstrip('/')}/login"


def backchannel_logout_uri(origin: str, provider_id: str) -> str:
    return f"{origin.rstrip('/')}/v1/auth/sso/{provider_id}/backchannel-logout"


def spend_jti(db: Any, provider_id: str, jti: str, ttl_seconds: int = 900) -> bool:
    """A logout token's `jti` is used once (replay). False when it was already spent."""
    from sqlalchemy.exc import IntegrityError

    nested = db.begin_nested()
    try:
        id_hash = create_challenge(db, kind="oidc_state", binding_secret=None, ttl_seconds=ttl_seconds,
                                   secret=f"logout-jti:{provider_id}:{jti}")
        consume_challenge(db, id_hash)
        nested.commit()
        return True
    except IntegrityError:
        nested.rollback()
        return False


def backchannel(db: Any, *, provider_id: str, claims: dict[str, Any]) -> list[tuple[str, str]]:
    """End the live sessions a verified logout token names; answers [(session_id, principal_id)].
    `sid` names one IdP session; without it, `sub` names every session of that person signed in
    through this provider."""
    from . import sessions

    sid, sub = claims.get("sid"), claims.get("sub")
    sql = ("SELECT id, principal_id FROM identity_sessions WHERE sso_provider_id = :p AND revoked_at IS NULL")
    params: dict[str, Any] = {"p": provider_id}
    if isinstance(sid, str) and sid:
        sql += " AND sso_sid = :sid"
        params["sid"] = sid
        if isinstance(sub, str) and sub:
            sql += " AND sso_subject = :sub"
            params["sub"] = sub
    else:
        sql += " AND sso_subject = :sub"
        params["sub"] = sub
    ended = [(r.id, r.principal_id) for r in db.execute(text(sql), params)]
    for session_id, _principal in ended:
        sessions.revoke(db, session_id, by=f"sso:{provider_id}", reason="idp back-channel logout")
    return ended

