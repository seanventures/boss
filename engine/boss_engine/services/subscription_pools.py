"""Multi-account pools per provider: several Claude or Codex subscriptions used as one (BOSSAPP-886).

WHY. A team with three Claude seats was capped at one seat's rate window: BOSS spent on exactly one
subscription per provider (model_router.accounts), and pooling was listed as future work.

THE SHAPE. A project's pool for one engine (claude_code | codex) holds MEMBERS. Each member is a
subscription login BOSS already knows how to use, stored the way the single login is:

  * its credential in the secret store, in the SAME subscription namespace, under its own name
    (`secret_name`: CLAUDE_CODE_OAUTH_TOKEN__POOL_<id> / CODEX_AUTH_JSON__POOL_<id>). It is read
    with cli_harness.stored_credential — the one resolver — and a codex member's rotated login is
    written back with cli_harness.sync_rotated_credential on a spec whose `auth_file_secret` is
    the member's name — the one sync seam. There is no second resolver and no second writer
    (BOSSAPP-1033's lesson).
  * its rate windows in subscription_accounts under `account_key` (<engine>:pool:<member id>):
    while a pooled run is in flight the LLM context carries that key, and
    model_router.accounts.account_key answers it, so the CLI's own rate_limit_events and limit
    messages land on the member that spent them.

CHOOSING. `assign` gives a run the member with the most headroom (model_router.headroom.assess
over its windows; nothing known reads as plenty), ties by position then least recently used, and
PINS it for the task (subscription_pool_pins) so one build stays on one account while that
account has room. A member at its limit is skipped; the pool is EXHAUSTED only when every active
member is, and PoolExhausted says when the earliest one resets.

WITHOUT A POOL nothing changes: `assignment_for` answers None and every launcher runs on the
single login exactly as before.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Optional

from sqlalchemy import text
from sqlalchemy.engine import Connection

from .core import emit_event, make_id

_log = logging.getLogger("boss.subscription_pools")

ENGINES = ("claude_code", "codex")
#: The single login's store name per engine; a member's is this plus __POOL_<member id>.
BASE_SECRET = {"claude_code": "CLAUDE_CODE_OAUTH_TOKEN", "codex": "CODEX_AUTH_JSON"}
MEMBER_STATUSES = ("active", "disabled", "needs_reauth")


class PoolError(ValueError):
    """A pool or member that cannot be saved as given."""


class PoolExhausted(RuntimeError):
    """Every active member of the pool is at its limit."""

    def __init__(self, message: str, retry_at: Optional[datetime]) -> None:
        super().__init__(message)
        self.retry_at = retry_at


@dataclass(frozen=True)
class Assignment:
    """Which member a run spends on. Never carries a credential."""

    pool_id: str
    member_id: str
    label: str
    engine: str
    secret_name: str
    account_key: str
    pinned: bool


def secret_name_for(engine: str, member_id: str) -> str:
    return f"{BASE_SECRET[engine]}__POOL_{member_id.upper()}"


def _store(settings: Any):
    from .secret_backends import backend_for, default_secret_backend

    return backend_for(default_secret_backend(settings), settings)


def _store_key(name: str) -> str:
    """The namespaced key BOSS writes a subscription credential under (cli_harness reads both)."""
    from .cli_harness import SUBSCRIPTION_SECRET_SCOPE
    from .secrets_service import ref_key_for

    return ref_key_for(SUBSCRIPTION_SECRET_SCOPE, name)


def _validate_credential(engine: str, credential: str) -> str:
    value = str(credential or "").strip()
    if not value:
        raise PoolError("the account's login is required; it goes to the secret store only")
    if engine == "codex":
        try:
            parsed = json.loads(value)
        except ValueError:
            raise PoolError("a Codex login is the CLI's auth.json; this is not valid JSON") from None
        if not isinstance(parsed, dict):
            raise PoolError("a Codex login is the CLI's auth.json object")
    return value


# ── reading ──────────────────────────────────────────────────────────────────────────────────

def _member_row(row: Any) -> dict[str, Any]:
    iso = lambda v: v.isoformat() if v is not None and hasattr(v, "isoformat") else v  # noqa: E731
    return {"id": row["id"], "pool_id": row["pool_id"], "label": row["label"],
            "secret_name": row["secret_name"], "account_key": row["account_key"],
            "position": int(row["position"]), "status": row["status"],
            "last_assigned_at": iso(row["last_assigned_at"]), "created_by": row["created_by"],
            "created_at": iso(row["created_at"])}


def get_pool(connection: Connection, project_id: str, engine: str) -> Optional[dict[str, Any]]:
    pool = connection.execute(text(
        "SELECT * FROM subscription_pools WHERE project_id = :p AND engine = :e"),
        {"p": project_id, "e": engine}).mappings().first()
    if pool is None:
        return None
    members = connection.execute(text(
        "SELECT * FROM subscription_pool_members WHERE pool_id = :i ORDER BY position, created_at"),
        {"i": pool["id"]}).mappings().all()
    return {"id": pool["id"], "project_id": pool["project_id"], "engine": pool["engine"],
            "name": pool["name"], "status": pool["status"],
            "members": [_member_row(m) for m in members]}


def headroom_of(connection: Connection, members: list[dict[str, Any]], engine: str,
                now: Optional[datetime] = None) -> dict[str, Any]:
    """{member id: Headroom} from each member's own windows. Nothing known reads as plenty."""
    from .model_router import accounts
    from .model_router.headroom import assess

    rows = accounts.load(connection, [m["account_key"] for m in members]) if members else {}
    return {m["id"]: assess(engine, rows.get(m["account_key"]) or {}, thresholds={}, now=now)
            for m in members}


def describe(connection: Connection, project_id: str) -> list[dict[str, Any]]:
    """Every pool of the project with each member's headroom — metadata only, never a login."""
    out = []
    for engine in ENGINES:
        pool = get_pool(connection, project_id, engine)
        if pool is None:
            continue
        rooms = headroom_of(connection, pool["members"], engine)
        for m in pool["members"]:
            h = rooms.get(m["id"])
            m["headroom"] = {"state": h.state, "left": h.left,
                             "resets_at": h.resets_at.isoformat() if h.resets_at else None,
                             "detail": h.detail} if h else None
            m.pop("secret_name", None)
        out.append(pool)
    return out


# ── writing ──────────────────────────────────────────────────────────────────────────────────

def create_pool(connection: Connection, project_id: str, *, engine: str, name: str,
                actor: str) -> dict[str, Any]:
    if engine not in ENGINES:
        raise PoolError(f"engine must be one of: {', '.join(ENGINES)}")
    label = " ".join(str(name or "").split())[:80] or f"{engine} pool"
    if get_pool(connection, project_id, engine) is not None:
        raise PoolError(f"this project already has a {engine} pool; add accounts to it")
    pid = make_id("spool")
    connection.execute(text(
        "INSERT INTO subscription_pools (id, project_id, engine, name, created_by) "
        "VALUES (:i, :p, :e, :n, :a)"), {"i": pid, "p": project_id, "e": engine, "n": label,
                                         "a": actor})
    emit_event(connection, project_id, "subscription_pool.created", actor, "subscription_pool",
               pid, {"engine": engine, "name": label})
    return get_pool(connection, project_id, engine)


def _pool_by_id(connection: Connection, project_id: str, pool_id: str) -> dict[str, Any]:
    row = connection.execute(text(
        "SELECT engine FROM subscription_pools WHERE id = :i AND project_id = :p"),
        {"i": pool_id, "p": project_id}).first()
    if row is None:
        raise LookupError(f"no pool {pool_id!r} in this project")
    return get_pool(connection, project_id, row[0])


def add_member(connection: Connection, settings: Any, project_id: str, pool_id: str, *,
               label: str, credential: str, actor: str) -> dict[str, Any]:
    """Store one account's login (the store only — never this table, never a response) and add it."""
    pool = _pool_by_id(connection, project_id, pool_id)
    engine = pool["engine"]
    value = _validate_credential(engine, credential)
    name = " ".join(str(label or "").split())[:80]
    if not name:
        raise PoolError("name the account, e.g. the seat's owner")
    if any(m["label"].lower() == name.lower() for m in pool["members"]):
        raise PoolError(f"the pool already has an account named {name!r}")
    mid = make_id("spm")
    secret = secret_name_for(engine, mid)
    _store(settings).put(_store_key(secret), value)
    position = max([m["position"] for m in pool["members"]] + [-1]) + 1
    try:
        connection.execute(text(
            "INSERT INTO subscription_pool_members (id, pool_id, label, secret_name, account_key, "
            "position, created_by) VALUES (:i, :pool, :l, :s, :k, :pos, :a)"),
            {"i": mid, "pool": pool_id, "l": name, "s": secret,
             "k": f"{engine}:pool:{mid}", "pos": position, "a": actor})
    except Exception:
        try:
            _store(settings).delete(_store_key(secret))
        except Exception:  # noqa: BLE001
            pass
        raise
    emit_event(connection, project_id, "subscription_pool.member_added", actor,
               "subscription_pool", pool_id, {"member_id": mid, "label": name, "engine": engine})
    member = next(m for m in get_pool(connection, project_id, engine)["members"] if m["id"] == mid)
    member.pop("secret_name", None)
    return member


def update_member(connection: Connection, settings: Any, project_id: str, member_id: str, *,
                  actor: str, status: Optional[str] = None, position: Optional[int] = None,
                  credential: Optional[str] = None) -> dict[str, Any]:
    row = connection.execute(text(
        "SELECT m.*, s.engine FROM subscription_pool_members m JOIN subscription_pools s "
        "ON s.id = m.pool_id WHERE m.id = :i AND s.project_id = :p"),
        {"i": member_id, "p": project_id}).mappings().first()
    if row is None:
        raise LookupError(f"no pool account {member_id!r} in this project")
    if status is not None and status not in MEMBER_STATUSES:
        raise PoolError(f"status must be one of: {', '.join(MEMBER_STATUSES)}")
    if credential is not None:
        # A fresh login for this seat: through the same store key the runs read and sync.
        _store(settings).put(_store_key(row["secret_name"]),
                             _validate_credential(row["engine"], credential))
        status = status or "active"
    connection.execute(text(
        "UPDATE subscription_pool_members SET status = COALESCE(:s, status), "
        "position = COALESCE(:pos, position) WHERE id = :i"),
        {"s": status, "pos": position, "i": member_id})
    emit_event(connection, project_id, "subscription_pool.member_changed", actor,
               "subscription_pool", row["pool_id"],
               {"member_id": member_id, "status": status, "position": position,
                "login_replaced": credential is not None})
    out = _member_row(connection.execute(text(
        "SELECT * FROM subscription_pool_members WHERE id = :i"), {"i": member_id}
    ).mappings().one())
    out.pop("secret_name", None)
    return out


def remove_member(connection: Connection, settings: Any, project_id: str, member_id: str, *,
                  actor: str) -> dict[str, Any]:
    row = connection.execute(text(
        "SELECT m.id, m.pool_id, m.secret_name FROM subscription_pool_members m JOIN "
        "subscription_pools s ON s.id = m.pool_id WHERE m.id = :i AND s.project_id = :p"),
        {"i": member_id, "p": project_id}).mappings().first()
    if row is None:
        raise LookupError(f"no pool account {member_id!r} in this project")
    connection.execute(text("DELETE FROM subscription_pool_members WHERE id = :i"),
                       {"i": member_id})
    try:
        _store(settings).delete(_store_key(row["secret_name"]))
    except Exception:  # noqa: BLE001 — the account is gone either way; say so in the log
        _log.warning("could not delete the stored login of pool account %s", member_id)
    emit_event(connection, project_id, "subscription_pool.member_removed", actor,
               "subscription_pool", row["pool_id"], {"member_id": member_id})
    return {"removed": member_id}


def set_pool_status(connection: Connection, project_id: str, pool_id: str, *, status: str,
                    actor: str) -> dict[str, Any]:
    if status not in ("active", "disabled"):
        raise PoolError("status must be active or disabled")
    pool = _pool_by_id(connection, project_id, pool_id)
    connection.execute(text(
        "UPDATE subscription_pools SET status = :s, updated_at = now() WHERE id = :i"),
        {"s": status, "i": pool_id})
    emit_event(connection, project_id, "subscription_pool.status", actor, "subscription_pool",
               pool_id, {"status": status})
    return get_pool(connection, project_id, pool["engine"])


def delete_pool(connection: Connection, project_id: str, pool_id: str, *,
                actor: str) -> dict[str, Any]:
    """Delete an EMPTY pool (BOSSAPP-1042). A pool that still has accounts is refused, never
    cascaded: each account's login lives in the secret store, and only remove_member deletes it
    there — a cascading row delete would leave those logins behind with nothing pointing at them.
    """
    pool = _pool_by_id(connection, project_id, pool_id)
    n = len(pool["members"])
    if n:
        raise PoolError(
            f"remove its {n} account{'s' if n != 1 else ''} first — each Remove also deletes "
            "that account's stored login; then the empty pool can go")
    connection.execute(text("DELETE FROM subscription_pools WHERE id = :i AND project_id = :p"),
                       {"i": pool_id, "p": project_id})
    emit_event(connection, project_id, "subscription_pool.deleted", actor, "subscription_pool",
               pool_id, {"engine": pool["engine"]})
    return {"deleted": pool_id}


# ── choosing ─────────────────────────────────────────────────────────────────────────────────

def assign(connection: Connection, project_id: str, engine: str, task_ref: Optional[str], *,
           now: Optional[datetime] = None) -> Optional[Assignment]:
    """The member this run spends on, or None when the project has no active pool for `engine`.
    Raises PoolExhausted when every active member is at its limit."""
    from .model_router.headroom import EXHAUSTED

    pool = get_pool(connection, project_id, engine)
    if pool is None or pool["status"] != "active":
        return None
    live = [m for m in pool["members"] if m["status"] == "active"]
    if not live:
        return None
    rooms = headroom_of(connection, live, engine, now=now)
    usable = [m for m in live if rooms[m["id"]].state != EXHAUSTED]
    if not usable:
        resets = [rooms[m["id"]].resets_at for m in live if rooms[m["id"]].resets_at]
        earliest = min(resets) if resets else None
        raise PoolExhausted(
            f"every account in the {pool['name']} pool is at its limit"
            + (f"; the first resets at {earliest.isoformat()}" if earliest else "")
            + ". Add an account to the pool, or wait.", earliest)
    ref = str(task_ref or "").strip()
    pinned = None
    if ref:
        pin = connection.execute(text(
            "SELECT member_id FROM subscription_pool_pins WHERE pool_id = :p AND task_ref = :t"),
            {"p": pool["id"], "t": ref}).scalar()
        pinned = next((m for m in usable if m["id"] == pin), None)
    chosen = pinned or sorted(
        usable, key=lambda m: (-(rooms[m["id"]].left if rooms[m["id"]].left is not None else 1.0),
                               m["position"], m["last_assigned_at"] or ""))[0]
    if ref and pinned is None:
        connection.execute(text(
            "INSERT INTO subscription_pool_pins (pool_id, task_ref, member_id) VALUES (:p, :t, :m) "
            "ON CONFLICT (pool_id, task_ref) DO UPDATE SET member_id = EXCLUDED.member_id, "
            "pinned_at = now()"), {"p": pool["id"], "t": ref, "m": chosen["id"]})
    connection.execute(text(
        "UPDATE subscription_pool_members SET last_assigned_at = clock_timestamp() WHERE id = :i"),
        {"i": chosen["id"]})
    return Assignment(pool_id=pool["id"], member_id=chosen["id"], label=chosen["label"],
                      engine=engine, secret_name=chosen["secret_name"],
                      account_key=chosen["account_key"], pinned=pinned is not None)


def assignment_for(engine: str, settings: Any = None) -> Optional[Assignment]:
    """The launchers' seam: the member this run spends on, from the run's LLM context (its
    project, and its run as the task pin). Also puts the member's account on the context so
    the run's rate-limit observations land on it. None — no project, no pool, no database — means
    the run uses the single login exactly as before. PoolExhausted propagates."""
    if engine not in ENGINES:
        return None
    try:
        from .llm_telemetry import get_llm_context, set_llm_context

        ctx = get_llm_context()
    except Exception:  # noqa: BLE001
        return None
    project_id = ctx.get("project_id")
    if not project_id:
        return None
    task_ref = ctx.get("workflow_run_id") or ctx.get("run_id")
    try:
        from ..db import begin, get_engine

        engine_db = ctx.get("db_engine") or get_engine(settings)
        with begin(engine_db) as c:
            with c.begin_nested():
                exists = c.execute(text(
                    "SELECT 1 FROM subscription_pools WHERE project_id = :p AND engine = :e "
                    "AND status = 'active'"), {"p": project_id, "e": engine}).first()
            if not exists:
                return None
            chosen = assign(c, project_id, engine, task_ref)
    except PoolExhausted:
        raise
    except Exception:  # noqa: BLE001 — no table, no database: the single login, as before
        _log.debug("pool lookup failed; using the single login", exc_info=True)
        return None
    if chosen is not None:
        set_llm_context(pool_account=chosen.account_key, pool_engine=engine)
    return chosen


def mark_needs_reauth(settings: Any, member_id: str) -> None:
    """A member whose login the provider refused is taken out of rotation until a person replaces
    its login (update_member with a credential). Never raises."""
    try:
        from ..db import begin, get_engine

        with begin(get_engine(settings)) as c:
            c.execute(text("UPDATE subscription_pool_members SET status = 'needs_reauth' "
                           "WHERE id = :i AND status = 'active'"), {"i": member_id})
    except Exception:  # noqa: BLE001
        _log.warning("could not mark pool account %s as needing a new login", member_id)

