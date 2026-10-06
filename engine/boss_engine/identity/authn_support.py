"""Shared plumbing for stream S1 (authentication): the server key, request facts (client IP,
public origin), the pre-auth challenge store, identity mail, and thin calls into the other
streams' frozen interfaces. Nothing here decides access.

Every function that touches the database takes `db` (a SQLAlchemy Connection in the caller's
transaction), so a failed security-event write rolls back the change it describes (D22).
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import ipaddress
import json
import os
import secrets
import socket
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Mapping, Optional
from urllib.parse import urlsplit

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from sqlalchemy import text

from ..config import get_settings
from . import audit, policy as policy_module
from .audit import Outcome, SecurityEvent, Stream


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def sha256_hex(value: str | bytes) -> str:
    data = value.encode("utf-8") if isinstance(value, str) else value
    return hashlib.sha256(data).hexdigest()


def b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")


def b64url_decode(value: str) -> bytes:
    pad = "=" * (-len(value) % 4)
    return base64.urlsafe_b64decode(value + pad)


def random_token(prefix: str = "", nbytes: int = 32) -> str:
    """`prefix` + base64url of `nbytes` CSPRNG bytes (256 bits by default)."""
    return prefix + b64url(secrets.token_bytes(nbytes))


# ---- the server key ---------------------------------------------------------------------------

_SERVER_KEY: Optional[bytes] = None
_SERVER_KEY_LOCK = threading.Lock()


def _master_key_bytes() -> Optional[bytes]:
    from .vault import MASTER_KEY_ENV, MASTER_KEY_FILE_ENV

    path = os.environ.get(MASTER_KEY_FILE_ENV, "").strip()
    raw = ""
    if path:
        try:
            with open(path, "r", encoding="utf-8") as handle:
                raw = handle.read().strip()
        except OSError:
            raw = ""
    raw = raw or os.environ.get(MASTER_KEY_ENV, "").strip()
    if not raw:
        return None
    try:
        key = base64.b64decode(raw, validate=True)
    except ValueError:
        return None
    return key if len(key) >= 32 else None


def server_key() -> bytes:
    """The HMAC key for CSRF tokens and hashed identifiers. BOSS_IDENTITY_SERVER_KEY, else
    HKDF(master key), else a per-process random key (announced once: CSRF tokens then change on
    restart, and rate-limit counters do not survive one)."""
    global _SERVER_KEY
    with _SERVER_KEY_LOCK:
        if _SERVER_KEY is not None:
            return _SERVER_KEY
        configured = (get_settings().identity_server_key or "").strip()
        key: Optional[bytes] = None
        if configured:
            try:
                decoded = base64.b64decode(configured, validate=True)
                key = decoded if len(decoded) >= 32 else None
            except ValueError:
                key = None
            if key is None:
                raise RuntimeError("BOSS_IDENTITY_SERVER_KEY must be at least 32 bytes, base64")
        if key is None:
            master = _master_key_bytes()
            if master:
                key = HKDF(algorithm=hashes.SHA256(), length=32, salt=None,
                           info=b"boss identity server key v1").derive(master)
        if key is None:
            key = secrets.token_bytes(32)
            print("[identity] no BOSS_IDENTITY_SERVER_KEY or vault master key: using a per-process "
                  "server key (CSRF tokens change on restart)", flush=True)
        _SERVER_KEY = key
        return key


def reset_server_key_for_tests() -> None:
    global _SERVER_KEY
    with _SERVER_KEY_LOCK:
        _SERVER_KEY = None


def keyed_hash(label: str, value: str) -> str:
    """HMAC-SHA256(server key, label ‖ value) hex. Identifiers (typed emails) are lowercased by
    the caller where that is the rule; nothing here is reversible."""
    return hmac.new(server_key(), f"{label}\x1f{value}".encode("utf-8"), hashlib.sha256).hexdigest()


def identifier_hash(identifier: str) -> str:
    return keyed_hash("identifier", (identifier or "").strip().lower())


# ---- request facts ------------------------------------------------------------------------------

def _trusted_networks(trusted: str) -> list[Any]:
    nets: list[Any] = []
    for entry in (trusted or "").split(","):
        entry = entry.strip()
        if not entry:
            continue
        try:
            nets.append(ipaddress.ip_network(entry, strict=False))
        except ValueError:
            try:
                for info in socket.getaddrinfo(entry, None):
                    nets.append(ipaddress.ip_network(info[4][0]))
            except OSError:
                continue
    return nets


def resolve_client_ip(peer: Optional[str], forwarded: Optional[str],
                      trusted: Optional[str] = None) -> str:
    """The socket peer, unless the peer is a trusted proxy: then the right-most X-Forwarded-For
    hop that is not itself trusted (§4.10). A client-sent XFF is never trusted otherwise."""
    trusted = get_settings().trusted_proxies if trusted is None else trusted
    nets = _trusted_networks(trusted or "")

    def is_trusted(addr: str) -> bool:
        try:
            ip = ipaddress.ip_address(addr)
        except ValueError:
            return False
        return any(ip in n for n in nets)

    def valid(addr: str) -> bool:
        try:
            ipaddress.ip_address(addr)
            return True
        except ValueError:
            return False

    peer = peer or ""
    if not is_trusted(peer):
        return peer
    hops = [h.strip() for h in (forwarded or "").split(",") if valid(h.strip())]
    for hop in reversed(hops):
        if not is_trusted(hop):
            return hop
    return hops[0] if hops else peer


def is_trusted_proxy(peer: Optional[str]) -> bool:
    """The peer is one of BOSS_TRUSTED_PROXIES (so its X-Forwarded-* headers are believed)."""
    if not peer:
        return False
    try:
        ip = ipaddress.ip_address(peer.split("%", 1)[0])
    except ValueError:
        return False
    return any(ip in n for n in _trusted_networks(get_settings().trusted_proxies or ""))


def origin_of(url: str) -> Optional[str]:
    """scheme://host[:port] of an absolute http(s) URL, lowercased; None otherwise."""
    try:
        parts = urlsplit((url or "").strip())
    except ValueError:
        return None
    if parts.scheme not in ("http", "https") or not parts.hostname:
        return None
    host = parts.hostname.lower()
    if ":" in host:
        host = f"[{host}]"
    default = 443 if parts.scheme == "https" else 80
    port = f":{parts.port}" if parts.port and parts.port != default else ""
    return f"{parts.scheme}://{host}{port}"


def is_loopback_origin(origin: str) -> bool:
    host = urlsplit(origin).hostname or ""
    if host == "localhost" or host.endswith(".localhost"):
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def public_origin(db: Any, org_id: Optional[str] = None) -> Optional[str]:
    """BOSS_PUBLIC_URL when set (it always wins), else the origin bound at the network claim
    (policy.public_origin, §4.12). None when neither exists."""
    configured = origin_of(get_settings().public_url or "")
    if configured:
        return configured
    if db is None:
        return None
    org = org_id or default_org_id(db)
    if not org:
        return None
    return origin_of(get_policy(org, db=db).public_origin or "")


def secure_origin_ok(origin: Optional[str]) -> bool:
    """A20: a session cookie is only ever set for an https origin, or plain http on loopback."""
    if not origin:
        return False
    return origin.startswith("https://") or is_loopback_origin(origin)


# ---- org, policy, events ------------------------------------------------------------------------

def default_org_id(db: Any) -> Optional[str]:
    """One org per install in v1 (D24): the first one created."""
    row = db.execute(text("SELECT id FROM orgs ORDER BY created_at, id LIMIT 1")).first()
    return row[0] if row else None


def get_policy(org_id: str, *, db: Any) -> policy_module.IdentityPolicy:
    return policy_module.get_policy(org_id, db=db)


def emit(db: Any, *, org_id: str, event_type: str, outcome: Outcome, ctx: Any = None,
         stream: Stream = Stream.SECURITY, **fields: Any) -> None:
    """Record one security event in the caller's transaction (S5's audit.record). Never best
    effort: if it raises, the caller's change fails with it (A76)."""
    if ctx is not None:
        import dataclasses

        event = dataclasses.replace(SecurityEvent.from_context(ctx, event_type, outcome),
                                    org_id=org_id, stream=stream, **fields)
    else:
        event = SecurityEvent(org_id=org_id, event_type=event_type, outcome=outcome,
                              stream=stream, **fields)
    audit.record(event, db=db)


def bump_epoch(org_id: str, *, db: Any, reason: str) -> None:
    from . import authz

    authz.bump_epoch(org_id, db=db, reason=reason)


# ---- pre-auth challenges ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Challenge:
    id_hash: str
    kind: str
    principal_id: Optional[str]
    payload: dict[str, Any]
    attempts: int
    expires_at: datetime


def _payload_key(binding_secret: str, kind: str) -> bytes:
    """The key for a challenge's payload is derived from the BROWSER's binding secret (the
    pre-auth cookie or session token), so a database dump alone cannot read a PKCE verifier,
    nonce or pending TOTP secret."""
    return HKDF(algorithm=hashes.SHA256(), length=32, salt=server_key(),
                info=f"boss challenge payload v1 {kind}".encode()).derive(binding_secret.encode())


def _seal(binding_secret: str, kind: str, id_hash: str, payload: Mapping[str, Any]) -> bytes:
    nonce = secrets.token_bytes(12)
    data = json.dumps(dict(payload), separators=(",", ":"), sort_keys=True).encode()
    return nonce + AESGCM(_payload_key(binding_secret, kind)).encrypt(nonce, data, id_hash.encode())


def _open(binding_secret: str, kind: str, id_hash: str, blob: Optional[bytes]) -> dict[str, Any]:
    if not blob:
        return {}
    blob = bytes(blob)
    plain = AESGCM(_payload_key(binding_secret, kind)).decrypt(blob[:12], blob[12:], id_hash.encode())
    return json.loads(plain)


def create_challenge(db: Any, *, kind: str, binding_secret: Optional[str], ttl_seconds: int,
                     principal_id: Optional[str] = None, payload: Optional[Mapping[str, Any]] = None,
                     secret: Optional[str] = None, id_hash: Optional[str] = None) -> str:
    """Store a single-use challenge. Its id is SHA-256 of `secret` (a random token when none is
    given), bound to SHA-256 of `binding_secret`. Returns the id hash; the secret is never stored."""
    secret = secret or random_token()
    id_hash = id_hash or sha256_hex(secret)
    blob = _seal(binding_secret, kind, id_hash, payload) if (payload and binding_secret) else None
    db.execute(text(
        "INSERT INTO identity_auth_challenges (id_hash, kind, principal_id, binding_hash, "
        "payload_enc, attempts, expires_at) VALUES (:h, :k, :p, :b, :pl, 0, :exp)"),
        {"h": id_hash, "k": kind, "p": principal_id,
         "b": sha256_hex(binding_secret) if binding_secret else None, "pl": blob,
         "exp": utcnow() + timedelta(seconds=ttl_seconds)})
    return id_hash


def _row_to_challenge(row: Any, binding_secret: Optional[str]) -> Challenge:
    payload: dict[str, Any] = {}
    if row.payload_enc is not None and binding_secret:
        payload = _open(binding_secret, row.kind, row.id_hash, row.payload_enc)
    return Challenge(row.id_hash, row.kind, row.principal_id, payload, row.attempts, row.expires_at)


def latest_bound_challenge(db: Any, *, kind: str, binding_secret: Optional[str],
                           lock: bool = True) -> Optional[Challenge]:
    """The newest live, unconsumed challenge of `kind` bound to this browser."""
    if not binding_secret:
        return None
    row = db.execute(text(
        "SELECT id_hash, kind, principal_id, payload_enc, attempts, expires_at "
        "FROM identity_auth_challenges WHERE kind = :k AND binding_hash = :b "
        "AND consumed_at IS NULL AND expires_at > now() ORDER BY created_at DESC LIMIT 1"
        + (" FOR UPDATE" if lock else "")),
        {"k": kind, "b": sha256_hex(binding_secret)}).first()
    return _row_to_challenge(row, binding_secret) if row else None


def challenge_by_secret(db: Any, *, kind: str, secret: str, binding_secret: Optional[str] = None,
                        require_binding: bool = False, lock: bool = True) -> Optional[Challenge]:
    """A live, unconsumed challenge whose id is SHA-256(secret). With `require_binding`, it must
    also be bound to `binding_secret` (the requesting browser)."""
    if not secret:
        return None
    row = db.execute(text(
        "SELECT id_hash, kind, principal_id, binding_hash, payload_enc, attempts, expires_at "
        "FROM identity_auth_challenges WHERE id_hash = :h AND kind = :k "
        "AND consumed_at IS NULL AND expires_at > now()" + (" FOR UPDATE" if lock else "")),
        {"h": sha256_hex(secret), "k": kind}).first()
    if row is None:
        return None
    if require_binding:
        if not binding_secret or not row.binding_hash or not hmac.compare_digest(
                row.binding_hash, sha256_hex(binding_secret)):
            return None
    return _row_to_challenge(row, binding_secret)


def consume_challenge(db: Any, id_hash: str) -> bool:
    """Single use: True exactly once."""
    res = db.execute(text("UPDATE identity_auth_challenges SET consumed_at = now() "
                          "WHERE id_hash = :h AND consumed_at IS NULL RETURNING id_hash"),
                     {"h": id_hash}).first()
    return res is not None


def count_attempt(db: Any, id_hash: str, *, max_attempts: int) -> int:
    """Count one failed attempt; burn the challenge at `max_attempts`. Returns the new count."""
    row = db.execute(text(
        "UPDATE identity_auth_challenges SET attempts = attempts + 1, "
        "consumed_at = CASE WHEN attempts + 1 >= :m THEN now() ELSE consumed_at END "
        "WHERE id_hash = :h RETURNING attempts"), {"h": id_hash, "m": max_attempts}).first()
    return int(row[0]) if row else max_attempts


def purge_expired_challenges(db: Any) -> None:
    db.execute(text("DELETE FROM identity_auth_challenges WHERE expires_at < now() - interval '1 day'"))


# ---- identity mail (SMTP only; D26, D30) ----------------------------------------------------------

def smtp_configured() -> bool:
    s = get_settings()
    return bool(s.email_smtp_host and getattr(s, "email_from_address", None)
                and getattr(s, "email_real_send_allowed", False))


def _smtp_send(to: str, subject: str, body: str) -> None:
    from ..services.email_adapter import EmailAdapter

    EmailAdapter(get_settings()).send_message(f"email:{to}", subject, body)


#: The transport. Tests replace it; production sends through the configured SMTP server.
MAIL_TRANSPORT: Callable[[str, str, str], None] = _smtp_send


def send_mail(to: str, subject: str, body: str) -> None:
    """Send an identity email. A delivery failure is swallowed AFTER the response shape is fixed:
    callers answer uniformly whether or not mail could be sent (anti-enumeration)."""
    try:
        MAIL_TRANSPORT(to, subject, body)
    except Exception as exc:  # noqa: BLE001 — never leak delivery state to the caller
        print(f"[identity] identity mail not delivered: {type(exc).__name__}", flush=True)


# ---- timing ----------------------------------------------------------------------------------------

def pad_to(started: float, floor_seconds: float) -> None:
    """Sleep until `floor_seconds` have passed since `started` (uniform-timing responses)."""
    remaining = floor_seconds - (time.monotonic() - started)
    if remaining > 0:
        time.sleep(remaining)
