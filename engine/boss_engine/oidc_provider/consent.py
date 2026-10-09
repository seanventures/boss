"""Consent per (subject, client, scopes) (§6.6 S6.7).

A client needs the subject's consent for the scopes it asks, unless an admin marked it
`first_party` — and never for `offline_access`, which always needs an explicit consent. The
consent form is CSRF-protected: its token is HMAC(server key, session ‖ interaction), checked
with the session cookie and the Origin. Revoking a consent revokes every grant (and so every
refresh token and access token) of that subject at that client.
"""
from __future__ import annotations

import hmac
from typing import Any, Iterable, Mapping, Optional

from sqlalchemy import text

from .util import audit


def current(db: Any, *, client_id: str, subject_ref: str) -> Optional[set[str]]:
    r = db.execute(text("SELECT scopes FROM oidc_consents WHERE client_id = :c AND subject_ref = :s "
                        "AND revoked_at IS NULL"), {"c": client_id, "s": subject_ref}).first()
    return set(r[0] or ()) if r else None


def needed(db: Any, *, client: Mapping[str, Any], subject_ref: str, scopes: Iterable[str], forced: bool) -> bool:
    scopes = set(scopes)
    if forced:
        return True
    if client.get("first_party") and "offline_access" not in scopes:
        return False
    granted = current(db, client_id=client["id"], subject_ref=subject_ref)
    return granted is None or not scopes <= granted


def covers(db: Any, *, client: Mapping[str, Any], subject_ref: str, scopes: Iterable[str]) -> bool:
    """Do the scopes still stand (refresh time)? first_party covers all but offline_access."""
    scopes = set(scopes)
    granted = current(db, client_id=client["id"], subject_ref=subject_ref) or set()
    if client.get("first_party"):
        return "offline_access" not in scopes or "offline_access" in granted
    return scopes <= granted


def grant(db: Any, *, client: Mapping[str, Any], subject_ref: str, scopes: Iterable[str], actor_kind: str,
          ip: Optional[str] = None) -> None:
    scopes = sorted(set(scopes) | (current(db, client_id=client["id"], subject_ref=subject_ref) or set()))
    db.execute(text(
        "INSERT INTO oidc_consents (subject_ref, client_id, scopes, granted_at, revoked_at) VALUES (:s, :c, :sc, now(), NULL) "
        "ON CONFLICT (subject_ref, client_id) DO UPDATE SET scopes = EXCLUDED.scopes, granted_at = now(), revoked_at = NULL"),
        {"s": subject_ref, "c": client["id"], "sc": scopes})
    audit(db, org_id=client["org_id"], event_type="oidc.consent.granted",
          actor_principal_id=subject_ref if subject_ref.startswith("usr_") else None, actor_kind=actor_kind,
          target_type="oidc_client", target_id=client["id"], ip=ip, details={"scopes": scopes})


def revoke(db: Any, *, org_id: str, client_id: str, subject_ref: str, actor: Optional[str]) -> bool:
    from .tokens import revoke_subject_grants

    changed = db.execute(text("UPDATE oidc_consents SET revoked_at = now() WHERE client_id = :c AND subject_ref = :s "
                              "AND revoked_at IS NULL AND client_id IN (SELECT id FROM oidc_clients WHERE org_id = :o)"),
                         {"c": client_id, "s": subject_ref, "o": org_id}).rowcount
    if not changed:
        return False
    revoke_subject_grants(db, client_id=client_id, subject_ref=subject_ref, reason="consent_revoked")
    audit(db, org_id=org_id, event_type="oidc.consent.revoked", actor_principal_id=actor, actor_kind="user",
          target_type="oidc_client", target_id=client_id, details={"subject_ref": subject_ref})
    return True


def list_consents(db: Any, *, org_id: str, client_id: Optional[str] = None) -> list[dict[str, Any]]:
    sql = ("SELECT c.subject_ref, c.client_id, c.scopes, c.granted_at FROM oidc_consents c JOIN oidc_clients k "
           "ON k.id = c.client_id WHERE k.org_id = :o AND c.revoked_at IS NULL")
    if client_id:
        sql += " AND c.client_id = :c"
    rows = db.execute(text(sql + " ORDER BY c.granted_at DESC LIMIT 500"), {"o": org_id, "c": client_id}).mappings()
    return [{"subject_ref": r["subject_ref"], "client_id": r["client_id"], "scopes": list(r["scopes"] or []),
             "granted_at": r["granted_at"].isoformat()} for r in rows]


def csrf_token(session_id: str, interaction_id: str) -> str:
    from ..identity.authn_support import keyed_hash

    return keyed_hash("oidc-consent-csrf", f"{session_id}\x1f{interaction_id}")


def csrf_ok(session_id: str, interaction_id: str, presented: Optional[str]) -> bool:
    if not presented or len(presented) > 200:
        return False
    return hmac.compare_digest(csrf_token(session_id, interaction_id).encode(), presented.encode())
