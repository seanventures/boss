"""Subscription accounts — limit state that is LIVE and ACCOUNT-WIDE (Decision 14).

A Claude subscription's weekly window belongs to the account, not to a BOSS project. Two
projects on one Claude login share one week, so one project hitting the limit is a fact for
every project on that login. Windows and the learned weekly allowance therefore live in
`subscription_accounts`, keyed by the credential that spends:

  * a key stored in Setup               → `<engine>:ref:<secret_ref_id>`
  * the install's CLAUDE_CODE_OAUTH_TOKEN → `<engine>:tok:<sha256 prefix>` (never the token)
  * a CLI logged in on this host         → `<engine>:install`

Writes are rare by construction: a `rate_limit_event` arrives on every Claude call, and is
written only when the window's state changes (status, a 5-point move in use, a new reset) or
at most every THROTTLE_S; snapshots are dropped only when a state actually changed. A later
successful call clears the rejected windows it proves wrong (the source recovered).
"""
from __future__ import annotations

import hashlib
import json
import logging
import threading
import time
from datetime import datetime, timezone
from typing import Any, Mapping, Optional

from sqlalchemy import text

from .headroom import (
    MODEL_WINDOWS, WEEKLY, state_for, windows_from_limit_message, windows_from_rate_limit_event,
)

log = logging.getLogger("boss.model_router.accounts")

THROTTLE_S = 300.0
USED_STEP = 0.05
CACHE_TTL_S = 20.0

_LOCK = threading.Lock()
_KEYS: dict[tuple[str, str], tuple[float, str]] = {}
_ROWS: dict[str, tuple[float, dict[str, Any]]] = {}
_WRITTEN: dict[str, float] = {}
#: The last headroom state seen per account, for hysteresis (headroom.state_for).
PREVIOUS: dict[str, str] = {}


def reset_caches() -> None:
    with _LOCK:
        _KEYS.clear()
        _ROWS.clear()
        _WRITTEN.clear()
        PREVIOUS.clear()


def _json(value: Any) -> dict:
    if isinstance(value, dict):
        return value
    if isinstance(value, str) and value:
        try:
            parsed = json.loads(value)
            return parsed if isinstance(parsed, dict) else {}
        except ValueError:
            return {}
    return {}


def account_key(connection: Any, settings: Any, project_id: Optional[str], engine: str) -> str:
    """Which subscription account `engine` spends on for this project (cached 5 minutes)."""
    cache_key = (project_id or "", engine)
    with _LOCK:
        hit = _KEYS.get(cache_key)
        if hit and time.monotonic() < hit[0]:
            return hit[1]
    key = f"{engine}:install"
    try:
        from ..provider_limits import source_ids_for_engine

        ids = set(source_ids_for_engine(settings, engine))
        rows = []
        if connection is not None and project_id:
            with connection.begin_nested():
                rows = connection.execute(text(
                    "SELECT source_id, credential_secret_ref_id, connection_status "
                    "FROM entitlement_sources WHERE project_id = :p"),
                    {"p": project_id}).mappings().all()
        mine = [r for r in rows if r["source_id"] in ids or any(
            str(r["source_id"] or "").startswith(i + "-") for i in ids if i.startswith("setup-"))]
        with_ref = [r for r in mine if r["credential_secret_ref_id"]]
        token = str(getattr(settings, "claude_code_oauth_token", "") or "") \
            if engine == "claude_code" else ""
        if with_ref:
            key = f"{engine}:ref:{with_ref[0]['credential_secret_ref_id']}"
        elif token:
            key = f"{engine}:tok:{hashlib.sha256(token.encode()).hexdigest()[:16]}"
    except Exception:  # noqa: BLE001 — an unknown account is the install's
        key = f"{engine}:install"
    with _LOCK:
        _KEYS[cache_key] = (time.monotonic() + 300.0, key)
    return key


def load(connection: Any, keys: list[str]) -> dict[str, dict[str, Any]]:
    """Account rows by key — one query for the ones not cached. Missing → {}."""
    out: dict[str, dict[str, Any]] = {}
    wanted: list[str] = []
    now = time.monotonic()
    with _LOCK:
        for k in keys:
            hit = _ROWS.get(k)
            if hit and now < hit[0]:
                out[k] = hit[1]
            else:
                wanted.append(k)
    if wanted and connection is not None:
        try:
            with connection.begin_nested():
                rows = connection.execute(text(
                    "SELECT account_key, windows, learned_weekly_allowance_usd "
                    "FROM subscription_accounts WHERE account_key IN :keys")
                    .bindparams(_expanding("keys")), {"keys": wanted}).mappings().all()
        except Exception:  # noqa: BLE001 — no table yet: nothing known
            rows = []
        found = {r["account_key"]: {"windows": _json(r["windows"]),
                                    "learned_weekly_allowance_usd":
                                        float(r["learned_weekly_allowance_usd"])
                                        if r["learned_weekly_allowance_usd"] is not None else None}
                 for r in rows}
        with _LOCK:
            for k in wanted:
                value = found.get(k, {"windows": {}, "learned_weekly_allowance_usd": None})
                _ROWS[k] = (now + CACHE_TTL_S, value)
                out[k] = value
    return out


def _expanding(name: str):
    from sqlalchemy import bindparam

    return bindparam(name, expanding=True)


def _changed(old: Mapping[str, Any], new: Mapping[str, Any]) -> bool:
    if not old:
        return True
    if str(old.get("status")) != str(new.get("status")):
        return True
    if str(old.get("resets_at") or "")[:16] != str(new.get("resets_at") or "")[:16]:
        return True
    try:
        return abs(float(old.get("used") or 0) - float(new.get("used") or 0)) >= USED_STEP
    except (TypeError, ValueError):
        return True


def _bucket(window: Optional[Mapping[str, Any]]) -> str:
    if not window:
        return "-"
    try:
        used = float(window.get("used") or 0)
    except (TypeError, ValueError):
        used = 0.0
    return f"{window.get('status')}:{state_for(1.0 - used, {'tight': 0.4, 'near_limit': 0.15})}"


def observe(connection: Any, settings: Any, project_id: Optional[str], engine: str, *,
            event: Optional[Mapping[str, Any]] = None, error: Optional[str] = None,
            ok_model: Optional[str] = None, now: Optional[datetime] = None
            ) -> dict[str, dict[str, Any]]:
    """Fold one call's evidence into its account. `event`: the CLI's rate_limit_event;
    `error`: a failure's text (its limit sentence, if any); `ok_model`: the call SUCCEEDED on
    this model — which clears the rejected windows it proves wrong. Returns the patch applied
    ({} when nothing changed and nothing was written). Never raises."""
    now = now or datetime.now(timezone.utc)
    patch: dict[str, dict[str, Any]] = {}
    if event:
        patch.update(windows_from_rate_limit_event(event))
    if error:
        patch.update(windows_from_limit_message(error, now))
    if connection is None or not project_id or not (patch or ok_model is not None):
        return patch
    try:
        key = account_key(connection, settings, project_id, engine)
        current = load(connection, [key]).get(key) or {}
        windows = dict(current.get("windows") or {})
        cleared: list[str] = []
        if ok_model is not None and not patch:
            family = next((f for f in ("opus", "sonnet") if f in str(ok_model).lower()), None)
            for kind, body in list(windows.items()):
                if str((body or {}).get("status")) != "rejected":
                    continue
                if kind in MODEL_WINDOWS and MODEL_WINDOWS[kind] != family:
                    continue                         # a Sonnet success says nothing of Opus
                windows.pop(kind)
                cleared.append(kind)
            if not cleared:
                return {}
        changed = bool(cleared)
        state_moved = bool(cleared)
        stamp = now.isoformat()
        for kind, body in patch.items():
            old = windows.get(kind) or {}
            if _changed(old, body):
                changed = True
            if _bucket(old) != _bucket(body):
                state_moved = True
            windows[kind] = {**body, "observed_at": stamp}
        last = _WRITTEN.get(key, 0.0)
        if not changed and time.monotonic() - last < THROTTLE_S:
            return {}
        learned = current.get("learned_weekly_allowance_usd")
        if patch.get(WEEKLY, {}).get("status") == "rejected" and \
                str((current.get("windows") or {}).get(WEEKLY, {}).get("status")) != "rejected":
            from .store import _used_usd

            used = _used_usd(connection, project_id).get(engine)
            learned = round(used, 2) if used else learned
        with connection.begin_nested():
            connection.execute(text(
                "INSERT INTO subscription_accounts (account_key, engine, windows, "
                "learned_weekly_allowance_usd, updated_at) VALUES (:k, :e, :w, :l, :t) "
                "ON CONFLICT (account_key) DO UPDATE SET windows = EXCLUDED.windows, "
                "learned_weekly_allowance_usd = EXCLUDED.learned_weekly_allowance_usd, "
                "updated_at = EXCLUDED.updated_at")
                if connection.dialect.name == "sqlite" else text(
                "INSERT INTO subscription_accounts (account_key, engine, windows, "
                "learned_weekly_allowance_usd, updated_at) VALUES (:k, :e, CAST(:w AS JSONB), "
                ":l, :t) ON CONFLICT (account_key) DO UPDATE SET windows = EXCLUDED.windows, "
                "learned_weekly_allowance_usd = EXCLUDED.learned_weekly_allowance_usd, "
                "updated_at = EXCLUDED.updated_at"),
                {"k": key, "e": engine, "w": json.dumps(windows), "l": learned, "t": now})
        with _LOCK:
            _ROWS[key] = (time.monotonic() + CACHE_TTL_S,
                          {"windows": windows, "learned_weekly_allowance_usd": learned})
            _WRITTEN[key] = time.monotonic()
        if state_moved:
            # The account is shared: every project's snapshot is stale, not only this one's.
            from .store import invalidate

            invalidate()
        return {**patch, **{k: {"cleared": True} for k in cleared}}
    except Exception:  # noqa: BLE001 — bookkeeping never fails the call it observes
        log.warning("headroom observation failed (non-fatal)", exc_info=True)
        return patch
