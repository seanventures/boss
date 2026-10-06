"""Shadow mode's evidence, and the shadow -> enforce rollout per router (IDENTITY.md §6.6,
§12 M2/M4, D21, amendment A5). Owned by stream S5.

  - `flush` moves the in-process ShadowLedger (enforcement.LEDGER) into identity_authz_shadow,
    aggregated per (day, router, method, path, permission, reason, principal kind), and writes
    `authz.shadow_deny` once per NEW row (so once per key per day, never per request);
  - `report` is the per-router summary the console reads (GET /v1/identity/shadow-report): what
    would be denied, by whom, how often, which routes are still undeclared, which routers are
    ready to enforce, and the BOSS_AUTHZ_ROUTER_MODES value that flips exactly those;
  - a router is READY when it has no undeclared routes, has been observed for 7 days, and every
    would-deny of the last 7 days is explained (an Owner wrote down why it is right to refuse it);
  - `start_flusher` runs flush + legacy retirement + the daily audit anchor in the background.
"""
from __future__ import annotations

import json
import threading
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable, Optional

from .enforcement import LEDGER, AuthzMode, LedgerKey, RouteEntry, ShadowLedger, mode_for

OBSERVATION_DAYS = 7
_flushed: dict[LedgerKey, int] = {}
_flush_lock = threading.Lock()


def _sql(statement: str):
    from sqlalchemy import text
    return text(statement)


def _settings(settings: Any = None) -> Any:
    if settings is None:
        from ..config import get_settings
        return get_settings()
    return settings


def _org(db: Any, settings: Any = None) -> Optional[str]:
    from .migration import default_org
    return default_org(db, _settings(settings))


def flush(db: Any, *, ledger: ShadowLedger = LEDGER, now: Optional[datetime] = None,
          settings: Any = None) -> int:
    """Write the ledger's new counts to identity_authz_shadow. Returns rows touched."""
    from . import audit, legacy
    from .policy import get_policy, set_extra

    now = now or datetime.now(timezone.utc)
    snapshot = ledger.snapshot()
    with _flush_lock:
        deltas = {k: n - _flushed.get(k, 0) for k, n in snapshot.items()
                  if n - _flushed.get(k, 0) > 0}
        _flushed.update({k: snapshot[k] for k in deltas})
    org_id = _org(db, settings)
    touched = 0
    for key, delta in deltas.items():
        row = db.execute(_sql(
            "INSERT INTO identity_authz_shadow (day, router_key, method, path_template, "
            "permission, reason, principal_kind, count, first_seen, last_seen) VALUES (:d, :r, "
            ":m, :p, :perm, :reason, :pk, :n, :now, :now) ON CONFLICT (day, router_key, method, "
            "path_template, permission, reason, principal_kind) DO UPDATE SET count = "
            "identity_authz_shadow.count + EXCLUDED.count, last_seen = EXCLUDED.last_seen "
            "RETURNING (xmax = 0) AS inserted"),
            {"d": key.day, "r": key.router_key, "m": key.method, "p": key.path_template,
             "perm": key.permission, "reason": key.reason, "pk": key.principal_kind, "n": delta,
             "now": now}).first()
        touched += 1
        if row and row[0] and org_id:
            audit.record(audit.SecurityEvent(
                org_id=org_id, event_type="authz.shadow_deny", outcome=audit.Outcome.DENIED,
                actor_kind=key.principal_kind or None, permission=key.permission or None,
                target_type="route", target_id=f"{key.method} {key.path_template}",
                details={"router": key.router_key, "reason": key.reason, "day": key.day.isoformat(),
                         "first_count": delta}), db=db)
    legacy.flush_usage(db)
    if org_id:
        extra = get_policy(org_id, db=db).extra or {}
        if "shadow_started_at" not in extra:
            set_extra(org_id, "shadow_started_at", now.isoformat(), db=db)
        note_mode(org_id, db=db, settings=settings)
    return touched


def forget_flushed() -> None:
    with _flush_lock:
        _flushed.clear()


def note_mode(org_id: str, *, db: Any, settings: Any = None) -> bool:
    """Write `authz.mode_changed` when BOSS_AUTHZ_MODE / BOSS_AUTHZ_ROUTER_MODES differ from what
    this install ran with last time (the rollout's own audit trail). Returns True if changed."""
    from . import audit
    from .policy import get_policy, set_extra

    s = _settings(settings)
    current = {"mode": str(s.authz_mode or "shadow").lower(),
               "router_modes": str(s.authz_router_modes or "")}
    previous = (get_policy(org_id, db=db).extra or {}).get("authz_mode")
    if previous == current:
        return False
    set_extra(org_id, "authz_mode", current, db=db)
    audit.record(audit.SecurityEvent(
        org_id=org_id, event_type="authz.mode_changed", outcome=audit.Outcome.INFO,
        actor_principal_id="sys_host_cli", actor_kind="system", target_type="authz_mode",
        details={"from": previous, "to": current}), db=db)
    return True


# ---- explanations (an Owner records why a would-deny is right) -----------------------------------

def explained(org_id: str, *, db: Any) -> list[dict[str, Any]]:
    from .policy import get_policy
    return list((get_policy(org_id, db=db).extra or {}).get("shadow_explained") or [])


def explain_denial(org_id: str, item: dict[str, Any], *, db: Any, ctx: Any) -> list[dict[str, Any]]:
    """Mark one (router, method, path, reason) would-deny as expected: it no longer blocks the
    router's flip to enforce. Audited as policy.changed."""
    from . import audit
    from .policy import set_extra

    entry = {k: str(item.get(k) or "") for k in ("router_key", "method", "path_template",
                                                 "reason")}
    if not entry["router_key"] or not entry["path_template"] or not entry["reason"]:
        raise ValueError("router_key, path_template and reason are required")
    entry["note"] = str(item.get("note") or "")[:500]
    entry["by"] = ctx.principal.id
    entry["at"] = datetime.now(timezone.utc).isoformat()
    items = [e for e in explained(org_id, db=db)
             if (e.get("router_key"), e.get("method"), e.get("path_template"), e.get("reason"))
             != (entry["router_key"], entry["method"], entry["path_template"], entry["reason"])]
    items.append(entry)
    set_extra(org_id, "shadow_explained", items, db=db)
    audit.record(audit.SecurityEvent.from_context(
        ctx, "policy.changed", audit.Outcome.SUCCESS, target_type="shadow_report",
        target_id=entry["router_key"], details={"shadow_explained": entry}), db=db)
    return items


def _is_explained(row: dict[str, Any], items: list[dict[str, Any]]) -> bool:
    for e in items:
        if e.get("router_key") != row["router_key"] or e.get("reason") != row["reason"]:
            continue
        if e.get("path_template") not in (row["path_template"], "*"):
            continue
        if e.get("method") and e.get("method") not in (row["method"], "*"):
            continue
        return True
    return False


# ---- the report ----------------------------------------------------------------------------------

@dataclass
class _Router:
    key: str
    operations: int = 0
    undeclared: int = 0


def report(db: Any, *, routes: Optional[Iterable[RouteEntry]] = None, days: int = OBSERVATION_DAYS,
           now: Optional[datetime] = None, settings: Any = None, top: int = 10) -> dict[str, Any]:
    """The per-router shadow report (console-readable; no secrets, no request bodies)."""
    from .policy import get_policy

    s = _settings(settings)
    now = now or datetime.now(timezone.utc)
    org_id = _org(db, s)
    since = (now - timedelta(days=days)).date()
    rows = [dict(r) for r in db.execute(_sql(
        "SELECT day, router_key, method, path_template, permission, reason, principal_kind, "
        "count, first_seen, last_seen FROM identity_authz_shadow WHERE day >= :since"),
        {"since": since}).mappings().all()]
    extra = (get_policy(org_id, db=db).extra or {}) if org_id else {}
    items = list(extra.get("shadow_explained") or [])
    started_raw = extra.get("shadow_started_at")
    started = datetime.fromisoformat(started_raw) if started_raw else None
    observed_days = (now - started).days if started else 0

    routers: dict[str, _Router] = {}
    undeclared_paths: dict[str, list[str]] = defaultdict(list)
    for entry in routes or ():
        if entry.kind == "mount":
            continue
        r = routers.setdefault(entry.router_key, _Router(entry.router_key))
        n = len(entry.labels())
        r.operations += n
        if entry.access is None:
            r.undeclared += n
            undeclared_paths[entry.router_key].extend(entry.labels())
    for row in rows:
        routers.setdefault(row["router_key"], _Router(row["router_key"]))

    out = []
    ready_keys = []
    for key in sorted(routers):
        r = routers[key]
        mine = [row for row in rows if row["router_key"] == key]
        unexplained = [row for row in mine if not _is_explained(row, items)]
        by_reason: Counter[str] = Counter()
        by_kind: Counter[str] = Counter()
        grouped: dict[tuple[str, str, str, str, str], dict[str, Any]] = {}
        for row in mine:
            by_reason[row["reason"]] += int(row["count"])
            by_kind[row["principal_kind"] or "anonymous"] += int(row["count"])
            g = grouped.setdefault(
                (row["method"], row["path_template"], row["permission"], row["reason"],
                 row["principal_kind"]),
                {"method": row["method"], "path": row["path_template"],
                 "permission": row["permission"] or None, "reason": row["reason"],
                 "principal_kind": row["principal_kind"] or "anonymous", "count": 0,
                 "last_seen": None, "explained": _is_explained(row, items)})
            g["count"] += int(row["count"])
            seen = row["last_seen"]
            if g["last_seen"] is None or (seen and seen > g["last_seen"]):
                g["last_seen"] = seen
        mode = mode_for(key, global_mode=s.authz_mode, router_modes=s.authz_router_modes).value
        blockers = []
        if r.undeclared:
            blockers.append(f"{r.undeclared} undeclared operations")
        if unexplained:
            blockers.append(f"{sum(int(x['count']) for x in unexplained)} unexplained would-denies "
                            f"in the last {days} days")
        if observed_days < days:
            blockers.append(f"observed {observed_days} of {days} days")
        if routes is not None and r.operations == 0:
            blockers.append("no routes")
        ready = not blockers and mode != AuthzMode.ENFORCE.value
        if ready:
            ready_keys.append(key)
        top_rows = sorted(grouped.values(), key=lambda g: -g["count"])[:top]
        for g in top_rows:
            g["last_seen"] = g["last_seen"].isoformat() if g["last_seen"] else None
        last = max((row["last_seen"] for row in unexplained), default=None)
        out.append({
            "router_key": key, "mode": mode, "operations": r.operations,
            "undeclared": r.undeclared, "undeclared_sample": undeclared_paths[key][:5],
            "would_deny": sum(by_reason.values()), "unexplained": sum(int(x["count"])
                                                                      for x in unexplained),
            "by_reason": dict(by_reason), "by_principal_kind": dict(by_kind), "top": top_rows,
            "last_unexplained_at": last.isoformat() if last else None,
            "ready_to_enforce": ready, "blockers": blockers,
        })
    current = {k for k, v in _router_modes(s).items() if v == AuthzMode.ENFORCE.value}
    suggested = ",".join(f"{k}=enforce" for k in sorted(current | set(ready_keys)))
    global_mode = str(s.authz_mode or "shadow").lower()
    all_ready = bool(out) and all(r["mode"] == "enforce" or r["ready_to_enforce"] for r in out)
    return {
        "org_id": org_id,
        "generated_at": now.isoformat(),
        "mode": global_mode,
        "router_modes": s.authz_router_modes or "",
        "window_days": days,
        "observed_since": started.isoformat() if started else None,
        "observed_days": observed_days,
        "totals": {"routers": len(out), "would_deny": sum(r["would_deny"] for r in out),
                   "unexplained": sum(r["unexplained"] for r in out),
                   "undeclared": sum(r["undeclared"] for r in out),
                   "ready": len(ready_keys),
                   "enforced": sum(1 for r in out if r["mode"] == "enforce")},
        "routers": out,
        "suggested_router_modes": suggested,
        "global_ready": all_ready and global_mode != "enforce",
        "next_step": _next_step(global_mode, all_ready, ready_keys, suggested),
    }


def _router_modes(s: Any) -> dict[str, str]:
    from .enforcement import parse_router_modes
    try:
        return {k: v.value for k, v in parse_router_modes(s.authz_router_modes or "").items()}
    except ValueError:
        return {}


def _next_step(global_mode: str, all_ready: bool, ready: list[str], suggested: str) -> str:
    if global_mode == "enforce":
        return "Enforcing everywhere. Keep the shadow report at zero."
    if all_ready:
        return "Every router is ready: set BOSS_AUTHZ_MODE=enforce."
    if ready:
        return f"Flip the ready routers: BOSS_AUTHZ_ROUTER_MODES={suggested}"
    return "Keep shadow mode: declare the remaining routes and explain or fix each would-deny."


# ---- the background loop -------------------------------------------------------------------------

_flusher: Optional[threading.Thread] = None
_flusher_stop = threading.Event()


def run_once(*, settings: Any = None) -> None:
    """One tick: flush the ledger and legacy counters, retire due bridges, anchor the chain."""
    from ..db import begin
    from . import audit, migration

    with begin() as db:
        flush(db, settings=settings)
    with begin() as db:
        org_id = migration.ensure_seeded(db, settings=settings)
        if org_id:
            migration.retire_due(org_id, db=db)
            audit.daily_anchor(org_id, db=db)


def start_flusher(*, interval: Optional[float] = None) -> bool:
    """Start the background loop once per process (the identity admin router starts it)."""
    global _flusher
    if _flusher is not None and _flusher.is_alive():
        return False
    period = float(interval or getattr(_settings(), "identity_shadow_flush_seconds", 60) or 60)

    def loop() -> None:
        while not _flusher_stop.wait(period):
            try:
                run_once()
            except Exception as exc:  # noqa: BLE001 — never kill the API over the report
                print(f"[identity] shadow flush failed: {type(exc).__name__}", flush=True)

    _flusher_stop.clear()
    _flusher = threading.Thread(target=loop, name="identity-shadow-flush", daemon=True)
    _flusher.start()
    return True


def stop_flusher() -> None:
    _flusher_stop.set()


def as_json(value: Any) -> str:
    return json.dumps(value, default=str, sort_keys=True)


__all__ = ["flush", "report", "explain_denial", "explained", "note_mode", "start_flusher",
           "stop_flusher", "run_once", "OBSERVATION_DAYS"]
