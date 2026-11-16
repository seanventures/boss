"""Named links to an in-progress app (BOSSAPP-1251).

A builder issues a link with a name, a password and an expiry. An admin can
stop that link, or stop new links. Opening an expired link or a stopped link
is refused. The password is hashed before it is stored and is never logged.
This is not the visitor publish snapshot.
"""
from __future__ import annotations

import contextvars
import hashlib
import hmac
import json
import logging
import re
import secrets
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from sqlalchemy import text

_log = logging.getLogger("boss.preview_share")
_SECRETS: contextvars.ContextVar[tuple[str, ...]] = contextvars.ContextVar(
    "preview_share_secrets", default=())
_ADMIN = frozenset({"admin", "owner", "ceo"})
_SLUG = re.compile(r"[^a-z0-9]+")
_MAX_DAYS = 30


class _Scrub(logging.Filter):
    """Drop a password that reached a log line during this call."""

    def filter(self, record: logging.LogRecord) -> bool:
        secrets_now = [item for item in _SECRETS.get() if item]
        if not secrets_now:
            return True
        rendered = record.getMessage()
        clean = rendered
        for secret in secrets_now:
            clean = clean.replace(secret, "[redacted]")
        if clean != rendered:
            record.msg = clean
            record.args = ()
        return True


if not any(isinstance(item, _Scrub) for item in _log.filters):
    _log.addFilter(_Scrub())


class ShareRefused(Exception):
    """The link was not issued or not opened. `code` is safe to show."""

    def __init__(self, code: str, status: int = 403):
        self.code = code
        self.status = status
        super().__init__(code)


def _hold(secret: str):
    return _SECRETS.set((*_SECRETS.get(), secret))


def _as_dt(value: Any) -> datetime:
    if isinstance(value, datetime):
        when = value
    else:
        when = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    return when.astimezone(timezone.utc)


def _iso(value: Any) -> str:
    if isinstance(value, datetime):
        return _as_dt(value).isoformat()
    return str(value)


def _slug(name: str) -> str:
    return _SLUG.sub("-", name.strip().lower()).strip("-")[:48]


def _hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(
        password.encode("utf-8"), salt=salt, n=2**14, r=8, p=1, dklen=32)
    return f"scrypt${salt.hex()}${digest.hex()}"


def _password_ok(password: str, stored: str) -> bool:
    try:
        kind, salt_hex, digest_hex = stored.split("$", 2)
        if kind != "scrypt":
            return False
        digest = hashlib.scrypt(
            password.encode("utf-8"), salt=bytes.fromhex(salt_hex),
            n=2**14, r=8, p=1, dklen=32)
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(digest.hex(), digest_hex)


def _member(connection, project_id: str, actor: str) -> Optional[set[str]]:
    row = connection.execute(text(
        "SELECT role_keys FROM project_members "
        "WHERE project_id = :p AND member_id = :m AND status = 'active'"
    ), {"p": project_id, "m": actor}).mappings().first()
    if row is None:
        return None
    raw = row["role_keys"] or []
    if isinstance(raw, str):
        raw = json.loads(raw)
    return {str(item) for item in raw}


def _require_member(connection, project_id: str, actor: str) -> set[str]:
    roles = _member(connection, project_id, actor)
    if roles is None:
        raise ShareRefused("not_member")
    return roles


def _require_admin(connection, project_id: str, actor: str) -> None:
    roles = _require_member(connection, project_id, actor)
    if not (roles & _ADMIN):
        raise ShareRefused("not_admin")


def _new_stopped(connection, project_id: str) -> bool:
    row = connection.execute(text(
        "SELECT new_links_stopped FROM preview_share_policy WHERE project_id = :p"
    ), {"p": project_id}).first()
    return bool(row and row[0])


def _check_name(name: str) -> str:
    cleaned = str(name or "").strip()
    if not cleaned or len(cleaned) > 80 or not _slug(cleaned):
        raise ShareRefused("bad_name", 422)
    return cleaned


def _check_app(app_key: str) -> str:
    cleaned = str(app_key or "").strip()
    if not cleaned or len(cleaned) > 80:
        raise ShareRefused("bad_app", 422)
    return cleaned


def _check_password(password: str) -> None:
    if (not isinstance(password, str) or not password.strip()
            or len(password) < 8 or len(password) > 128):
        raise ShareRefused("bad_password", 422)


def _check_expiry(value: Any) -> datetime:
    try:
        when = _as_dt(value)
    except (ValueError, TypeError):
        raise ShareRefused("bad_expiry", 422) from None
    now = datetime.now(timezone.utc)
    if when <= now or when > now + timedelta(days=_MAX_DAYS):
        raise ShareRefused("bad_expiry", 422)
    return when


def _unique_slug(connection, name: str) -> str:
    base = _slug(name)
    slug = base
    n = 2
    while connection.execute(
            text("SELECT 1 FROM preview_shares WHERE slug = :s"), {"s": slug}).first():
        slug = f"{base[:44]}-{n}"
        n += 1
    return slug


def _public(row) -> dict[str, Any]:
    return {
        "id": row["id"],
        "project_id": row["project_id"],
        "app_key": row["app_key"],
        "name": row["name"],
        "slug": row["slug"],
        "href": f"/preview-share/{row['slug']}",
        "expires_at": _iso(row["expires_at"]),
        "stopped": row["stopped_at"] is not None,
        "expired": bool(row["expired"]),
        "created_by": row["created_by"],
    }


_PUBLIC_SQL = """
SELECT id, project_id, app_key, name, slug, expires_at, stopped_at, created_by,
       (expires_at <= now()) AS expired
FROM preview_shares
"""


def _load(connection, share_id: str):
    return connection.execute(
        text(_PUBLIC_SQL + " WHERE id = :id"), {"id": share_id}).mappings().first()


def create_share(engine, *, project_id: str, app_key: str, name: str, password: str,
                 expires_at: Any, actor: str) -> dict[str, Any]:
    token = _hold(password)
    try:
        with engine.begin() as connection:
            _require_member(connection, project_id, actor)
            if _new_stopped(connection, project_id):
                raise ShareRefused("new_links_stopped")
            cleaned = _check_name(name)
            app = _check_app(app_key)
            _check_password(password)
            when = _check_expiry(expires_at)
            slug = _unique_slug(connection, cleaned)
            share_id = "psh_" + uuid.uuid4().hex[:16]
            connection.execute(text(
                "INSERT INTO preview_shares ("
                "id, project_id, app_key, name, slug, password_hash, expires_at, created_by"
                ") VALUES (:id, :p, :app, :name, :slug, :ph, :exp, :actor)"
            ), {
                "id": share_id, "p": project_id, "app": app, "name": cleaned,
                "slug": slug, "ph": _hash_password(password), "exp": when, "actor": actor,
            })
            _log.info("preview share issued slug=%s project=%s", slug, project_id)
            return _public(_load(connection, share_id))
    finally:
        _SECRETS.reset(token)


def open_share(engine, *, slug: str, password: str, project_id: str) -> dict[str, Any]:
    token = _hold(password)
    try:
        with engine.begin() as connection:
            row = connection.execute(text(
                "SELECT id, project_id, app_key, name, slug, password_hash, expires_at, "
                "stopped_at, created_by, (expires_at <= now()) AS expired "
                "FROM preview_shares WHERE slug = :s"
            ), {"s": slug}).mappings().first()
            if row is None or row["project_id"] != project_id:
                _log.info("preview share refused code=unknown slug=%s", slug)
                raise ShareRefused("unknown", 404)
            if row["stopped_at"] is not None:
                _log.info("preview share refused code=stopped slug=%s", slug)
                raise ShareRefused("stopped")
            if row["expired"]:
                _log.info("preview share refused code=expired slug=%s", slug)
                raise ShareRefused("expired")
            if not _password_ok(password, row["password_hash"]):
                _log.info("preview share refused code=password slug=%s", slug)
                raise ShareRefused("password")
            _log.info("preview share opened slug=%s", slug)
            return _public(row)
    finally:
        _SECRETS.reset(token)


def stop_share(engine, *, project_id: str, share_id: str, actor: str) -> dict[str, Any]:
    with engine.begin() as connection:
        _require_admin(connection, project_id, actor)
        found = connection.execute(text(
            "SELECT id FROM preview_shares WHERE id = :id AND project_id = :p"
        ), {"id": share_id, "p": project_id}).first()
        if found is None:
            raise ShareRefused("unknown", 404)
        connection.execute(text(
            "UPDATE preview_shares SET stopped_at = COALESCE(stopped_at, now()), "
            "stopped_by = COALESCE(stopped_by, :a) WHERE id = :id"
        ), {"id": share_id, "a": actor})
        _log.info("preview share stopped id=%s", share_id)
        return _public(_load(connection, share_id))


def stop_new_links(engine, *, project_id: str, actor: str) -> dict[str, Any]:
    with engine.begin() as connection:
        _require_admin(connection, project_id, actor)
        connection.execute(text(
            "INSERT INTO preview_share_policy "
            "(project_id, new_links_stopped, stopped_by, stopped_at) "
            "VALUES (:p, true, :a, now()) "
            "ON CONFLICT (project_id) DO UPDATE SET "
            "new_links_stopped = true, stopped_by = :a, stopped_at = now()"
        ), {"p": project_id, "a": actor})
        _log.info("preview share new links stopped project=%s", project_id)
        return {"project_id": project_id, "new_links_stopped": True, "stopped_by": actor}


def list_shares(engine, *, project_id: str) -> list[dict[str, Any]]:
    with engine.begin() as connection:
        rows = connection.execute(text(
            _PUBLIC_SQL + " WHERE project_id = :p ORDER BY created_at DESC"
        ), {"p": project_id}).mappings().all()
    return [_public(row) for row in rows]
