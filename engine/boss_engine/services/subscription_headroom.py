"""Subscription headroom where people govern (BOSSAPP-1342).

"Claude Code: near limit, 3% left, resets Fri 11:00" and "Codex: login expired" were visible only
deep in Setup's routing editor; Govern showed nothing, so people opened Anthropic's settings in
the middle of their work. This is the one read Govern (a card) and the Command Center (a badge in
its header) show: each subscription source, separately when an engine has several accounts (a
pool), with

  * how much of this week's window is left, and when it resets;
  * whether its login works;
  * what BOSS does at the limit — the next account of the pool, another connected source, or a
    pause until the reset;
  * how many builds the rest covers at this week's pace, when that can be estimated.

THE SAME DATA AS THE ROUTER. The project's sources and their headroom come from
model_router.store.snapshot (the windows the CLI reported on its own calls, the limit messages,
the estimate) and a pool's members from subscription_pools.describe — nothing here asks a
provider anything, and no new polling exists.

WARNINGS (`warnings_due`): at 20%, 10% and 5% left, once per threshold per reset window, as a
card in the Command Center and a message on the project's chat channels (system_doctor raises
them on its tick, from `install_accounts`).
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from sqlalchemy import text

log = logging.getLogger("boss.subscription_headroom")

#: Percent left at which people are told, most urgent last.
THRESHOLDS = (20, 10, 5)
#: Builds this week below which no per-build estimate is made (too few to say a pace).
MIN_BUILDS_FOR_ESTIMATE = 3


def _iso(value: Any) -> Optional[str]:
    return value.isoformat() if hasattr(value, "isoformat") else value


def _pct(left: Optional[float]) -> Optional[int]:
    return None if left is None else max(0, min(100, int(round(float(left) * 100))))


def threshold_for(left_pct: Optional[int]) -> Optional[int]:
    """The deepest warning threshold `left_pct` is at or under (5, 10 or 20), else None."""
    if left_pct is None:
        return None
    hit = [t for t in THRESHOLDS if left_pct <= t]
    return min(hit) if hit else None


def account_label(engine_label: str, account_key: str, member_label: str = "") -> str:
    """Which account it is, in words: a pool seat's own name, this machine's login, a key."""
    if member_label:
        return f"{engine_label} · {member_label}"
    tail = account_key.split(":", 1)[1] if ":" in account_key else ""
    if tail == "install":
        return f"{engine_label} (this machine's login)"
    if tail.startswith("ref:"):
        return f"{engine_label} (login stored in Setup)"
    if tail.startswith("tok:"):
        return f"{engine_label} (the install's token)"
    return engine_label


def _builds_left(connection: Any, engine: str, left: Optional[float],
                 resets_at: Optional[datetime], basis: str) -> tuple[Optional[int], str]:
    """How many builds the rest of the window covers at this week's pace, or (None, why not).
    Builds on `engine` since the window began (account-wide, every project), and the share of
    the window they used: an estimate, and said as one."""
    if left is None or resets_at is None or basis not in ("cli", "limit"):
        return None, "BOSS has no reported usage for this week yet"
    used = 1.0 - float(left)
    if used < 0.05:
        return None, "too little used this week to estimate"
    since = resets_at - timedelta(days=7)
    try:
        with connection.begin_nested():
            n = int(connection.execute(text(
                "SELECT count(*) FROM code_task_runs WHERE created_at >= :s AND "
                "state->>'build_engine' = :e"), {"s": since, "e": engine}).scalar() or 0)
    except Exception:  # noqa: BLE001
        return None, "builds this week could not be counted"
    if n < MIN_BUILDS_FOR_ESTIMATE:
        return None, f"only {n} build(s) this week, too few to estimate"
    per_build = used / n
    return int(float(left) // per_build), f"at this week's pace ({n} builds used {round(used * 100)}%)"


def _logins(connection: Any, settings: Any, project_id: str) -> dict[str, dict[str, str]]:
    """{engine: {"state": "expired", "words": …}} for each subscription whose login failed —
    Setup's own model rows (setup_view._models), read from the database only."""
    out: dict[str, dict[str, str]] = {}
    try:
        from .cli_harness import catalogs_from_cache_only
        from .setup_view import _models, engine_for_provider, now_utc

        with catalogs_from_cache_only(), connection.begin_nested():
            models = _models(connection, settings, project_id, now_utc(), False)
    except Exception:  # noqa: BLE001 — the meters still show; the login reads unknown
        log.debug("subscription headroom: the login states were not read", exc_info=True)
        return out
    for m in models:
        if m.get("kind") != "subscription":
            continue
        pill = m.get("pill") or {}
        engine = engine_for_provider(str(m.get("provider") or ""))
        if pill.get("tone") == "red":
            out[engine] = {"state": "expired", "words": str(pill.get("label") or "Login expired"),
                           "tip": str(pill.get("tip") or "Sign in again in Setup.")}
        else:
            out.setdefault(engine, {"state": "ok", "words": "Signed in", "tip": ""})
    return out


def _alternatives(snap: Any, engine: str) -> list[str]:
    """Other sources the router may move work to: connected, working, not exhausted, and named
    in the routing policy's tiers (or connected at all when the policy names none)."""
    from .model_router.selection import engine_label

    named: set[str] = set()
    try:
        from .model_router.tiers import TIERS

        for tier in TIERS:
            named |= {t.engine for t in snap.policy.targets(tier)}
    except Exception:  # noqa: BLE001
        named = set()
    out = []
    for other, state in (snap.sources or {}).items():
        if other == engine or not getattr(state, "working", True):
            continue
        if named and other not in named:
            continue
        h = getattr(state, "headroom", None)
        if h is not None and getattr(h, "state", "") == "exhausted":
            continue
        out.append(engine_label(other) + (" (metered: counts against the budget)"
                                          if getattr(state, "kind", "") == "metered" else ""))
    return sorted(out)


def _at_limit(alternatives: list[str], next_seat: Optional[str], resets: Optional[str],
              engine_label: str) -> dict[str, str]:
    if next_seat:
        return {"does": "next_account", "words": f"Moves to the next {engine_label} account: {next_seat}."}
    if alternatives:
        return {"does": "fails_over", "words": f"Moves work to {', '.join(alternatives[:3])}."}
    return {"does": "pauses",
            "words": "Pauses new work on it until it resets" + (" (see the time)." if resets else ".")}


def project_view(connection: Any, settings: Any, project_id: str, *,
                 now: Optional[datetime] = None) -> dict[str, Any]:
    """Govern's card and the Command Center badge: every subscription source of the project."""
    from . import subscription_pools
    from .model_router import accounts, store
    from .model_router.headroom import STATE_LABELS
    from .model_router.selection import engine_label

    snap = store.snapshot(connection, settings, project_id)
    logins = _logins(connection, settings, project_id)
    try:
        with connection.begin_nested():
            pools = {p["engine"]: p for p in subscription_pools.describe(connection, project_id)}
    except Exception:  # noqa: BLE001 — no pool tables: single logins only
        pools = {}
    sources: list[dict[str, Any]] = []
    for engine, state in sorted((snap.sources or {}).items()):
        if getattr(state, "kind", "") != "subscription":
            continue
        label = engine_label(engine)
        alts = _alternatives(snap, engine)
        login = logins.get(engine) or {"state": "unknown", "words": "Login not checked yet", "tip": ""}
        pool = pools.get(engine)
        members = [m for m in (pool or {}).get("members") or [] if m.get("status") != "disabled"]
        if members:
            usable = [m for m in members if m.get("status") == "active"
                      and ((m.get("headroom") or {}).get("state") != "exhausted")]
            # The member a new build is given (subscription_pools.assign): the most room.
            best = max(usable, key=lambda m: ((m.get("headroom") or {}).get("left")
                                              if (m.get("headroom") or {}).get("left") is not None
                                              else 1.0), default=None)
            for m in members:
                h = m.get("headroom") or {}
                left = _pct(h.get("left"))
                others = [o["label"] for o in usable if o["id"] != m["id"]]
                seat_login = ({"state": "expired", "words": "Login expired",
                               "tip": "Paste a fresh login for this account in Setup."}
                              if m.get("status") == "needs_reauth" else login)
                resets = h.get("resets_at")
                state = "exhausted" if seat_login["state"] == "expired" else h.get("state") or "plenty"
                sources.append({
                    "key": m["account_key"], "engine": engine, "engine_label": label,
                    "label": account_label(label, m["account_key"], m["label"]),
                    "pool": {"id": pool["id"], "name": pool["name"], "position": m["position"],
                             "size": len(members)},
                    "taking_new_work": best is not None and best["id"] == m["id"],
                    "left_pct": left, "state": state, "state_label": STATE_LABELS.get(state, state),
                    "resets_at": resets, "basis": "cli" if left is not None else "unknown",
                    "login": seat_login,
                    "at_limit": _at_limit(alts, others[0] if others else None, resets, label),
                    "builds_left": None,
                    "builds_basis": "per-account builds are not counted separately yet",
                    "threshold": threshold_for(left),
                })
            continue
        h = state.headroom
        left = _pct(h.left)
        resets = _iso(h.resets_at)
        n, why = _builds_left(connection, engine, h.left, h.resets_at, h.basis)
        try:
            key = accounts.account_key(connection, settings, project_id, engine)
        except Exception:  # noqa: BLE001
            key = engine
        st = "exhausted" if login["state"] == "expired" else h.state
        sources.append({
            "key": key, "engine": engine, "engine_label": label,
            "label": account_label(label, key), "pool": None, "taking_new_work": True,
            "left_pct": left, "state": st, "state_label": STATE_LABELS.get(st, st),
            "resets_at": resets, "basis": h.basis, "login": login,
            "at_limit": _at_limit(alts, None, resets, label),
            "builds_left": n, "builds_basis": why, "threshold": threshold_for(left),
        })
    order = {"exhausted": 0, "near_limit": 1, "tight": 2, "plenty": 3}
    worst = min(sources, key=lambda s: (order.get(s["state"], 4),
                                        s["left_pct"] if s["left_pct"] is not None else 101),
                default=None)
    return {"project_id": project_id, "sources": sources, "worst": worst,
            "thresholds": list(THRESHOLDS),
            "at": (now or datetime.now(timezone.utc)).isoformat()}


# ── warnings ─────────────────────────────────────────────────────────────────────────────────

def install_accounts(connection: Any, *, now: Optional[datetime] = None) -> list[dict[str, Any]]:
    """Every subscription account BOSS has seen a window for, install-wide (an account's week is
    the account's, not a project's), with its headroom — the router's own rows
    (subscription_accounts) and assessment, no provider call."""
    from .model_router.headroom import assess
    from .model_router.selection import engine_label

    try:
        with connection.begin_nested():
            rows = connection.execute(text(
                "SELECT a.account_key, a.engine, a.windows, a.learned_weekly_allowance_usd, "
                "m.label AS member_label FROM subscription_accounts a LEFT JOIN "
                "subscription_pool_members m ON m.account_key = a.account_key")).mappings().all()
    except Exception:  # noqa: BLE001 — an older schema: nothing to warn about
        return []
    out = []
    for r in rows:
        windows = r["windows"] if isinstance(r["windows"], dict) else json.loads(r["windows"] or "{}")
        h = assess(str(r["engine"]), {"windows": windows}, thresholds={}, now=now)
        left = _pct(h.left)
        out.append({"account_key": str(r["account_key"]), "engine": str(r["engine"]),
                    "label": account_label(engine_label(str(r["engine"])), str(r["account_key"]),
                                           str(r["member_label"] or "")),
                    "left_pct": left, "state": h.state, "resets_at": _iso(h.resets_at),
                    "threshold": threshold_for(left)})
    return out


def warning_key(account: dict[str, Any]) -> Optional[str]:
    """One key per account, threshold and reset window: a warning is said once for each."""
    if account.get("threshold") is None:
        return None
    window = str(account.get("resets_at") or "")[:13] or "unknown"
    return f"{account['account_key']}|{account['threshold']}|{window}"


def warning_words(account: dict[str, Any]) -> tuple[str, str]:
    """(title, detail) for one warning."""
    left = account.get("left_pct")
    resets = account.get("resets_at")
    when = ""
    if resets:
        try:
            at = datetime.fromisoformat(str(resets))
            when = f" It resets {at:%a %d %b %H:%M} UTC."
        except ValueError:
            when = ""
    title = f"{account['label']}: {left}% of this week's subscription left"
    detail = (f"{account['label']} has {left}% of its weekly window left.{when} BOSS keeps "
              "routing work by the project's rules: at the limit it moves work to the next "
              "account or source, or pauses it until the reset. See Govern for each source.")
    return title, detail


__all__ = ["THRESHOLDS", "account_label", "install_accounts", "project_view", "threshold_for",
           "warning_key", "warning_words"]
