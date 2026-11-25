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

ALERTS (BOSSAPP-1342). When an account falls to 20, 10 or 5 percent of its week, or reaches its
limit, BOSS says so where people govern, not only in Anthropic's settings: a system finding per
account (`findings_for`; Govern's banner and, at the limit, a card in the Command Center saying
what BOSS will do), the project's usual notification channels (`notify_channels`: a BOSS
self-check alert, so the console and the owner's email), and one `subscription.headroom_changed`
event on each change of state. `observe_hook` raises them
the moment a run's call reports the limit (model_router.accounts.observe), and system_doctor's
tick keeps them true. A reading older than `headroom.STALE_AFTER_S` says how old it is.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from sqlalchemy import text

log = logging.getLogger("boss.subscription_headroom")

#: Builds this week below which no per-build estimate is made (too few to say a pace).
MIN_BUILDS_FOR_ESTIMATE = 3


def _iso(value: Any) -> Optional[str]:
    return value.isoformat() if hasattr(value, "isoformat") else value


def _pct(left: Optional[float]) -> Optional[int]:
    return None if left is None else max(0, min(100, int(round(float(left) * 100))))


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
                 now: Optional[datetime] = None, lean: bool = False) -> dict[str, Any]:
    """Govern's card and the Command Center badge: every subscription source of the project.
    `lean` (the Command Center's header, polled every few seconds) keeps to the router's own
    snapshot and the pool: the login reads from the router's `working` flag instead of Setup's
    model rows, and no builds-left estimate is counted."""
    from . import subscription_pools
    from .model_router import accounts, store
    from .model_router.headroom import STATE_LABELS
    from .model_router.selection import engine_label

    snap = store.snapshot(connection, settings, project_id)
    logins = {} if lean else _logins(connection, settings, project_id)
    pools: dict[str, Any] = {}
    sources: list[dict[str, Any]] = []
    for engine, state in sorted((snap.sources or {}).items()):
        if getattr(state, "kind", "") != "subscription":
            continue
        label = engine_label(engine)
        alts = _alternatives(snap, engine)
        login = logins.get(engine) or (
            {"state": "expired", "words": "Login not working", "tip": "Sign in again in Setup."}
            if lean and not getattr(state, "working", True) else
            {"state": "unknown", "words": "Login not checked yet", "tip": ""})
        # THE POOL A BUILD IS GIVEN (BOSSAPP-1343): the project's own, else its org's — the same
        # read `subscription_pools.assign` makes, and the same choice of member.
        if engine not in pools:
            try:
                with connection.begin_nested():
                    pools[engine] = subscription_pools.routing_pool(connection, project_id, engine)
            except Exception:  # noqa: BLE001 — no pool tables: single logins only
                pools[engine] = None
        pool = pools.get(engine)
        members = [m for m in (pool or {}).get("members") or [] if m.get("status") != "disabled"]
        if members:
            rooms = subscription_pools.headroom_of(connection, members, engine, now=now)
            # The seats that may take work (routable: an adopted install login whose login is
            # refused may not), as `assign` reads them.
            may = {m["id"] for m in subscription_pools.routable(connection, settings, project_id,
                                                                  engine, members)}
            usable = [m for m in members if m["id"] in may
                      and rooms[m["id"]].state != "exhausted"]
            best = subscription_pools.choose_member(usable, rooms)
            for m in members:
                h = rooms[m["id"]]
                left = _pct(h.left)
                others = [o["label"] for o in usable if o["id"] != m["id"]]
                refused = m.get("status") == "needs_reauth" or (
                    m.get("status") == "active" and m["id"] not in may)
                seat_login = ({"state": "expired", "words": "Login expired",
                               "tip": "Sign in to this account again in Setup."}
                              if refused else login)
                resets = _iso(h.resets_at)
                state = "exhausted" if seat_login["state"] == "expired" else h.state or "plenty"
                sources.append({
                    "key": m["account_key"], "engine": engine, "engine_label": label,
                    "label": account_label(label, m["account_key"], m["label"]),
                    "pool": {"id": pool["id"], "name": pool["name"], "position": m["position"],
                             "size": len(members), "scope": pool.get("scope") or "project",
                             "primary": bool(m.get("primary"))},
                    "taking_new_work": best is not None and best["id"] == m["id"],
                    "left_pct": left, "state": state, "state_label": ("Sign-in expired" if seat_login["state"] == "expired"
                                    else STATE_LABELS.get(state, state)),
                    "resets_at": resets, "basis": h.basis if left is not None else "unknown",
                    "login": seat_login,
                    "at_limit": _at_limit(alts, others[0] if others else None, resets, label),
                    "builds_left": None,
                    "builds_basis": "per-account builds are not counted separately yet",
                    "windows": _windows_rows(h, snap.policy.thresholds), "jobs": _jobs(snap, engine),
                    "freshness": h.freshness(now),
                })
            continue
        h = state.headroom
        left = _pct(h.left)
        resets = _iso(h.resets_at)
        n, why = ((None, "not counted here") if lean else
                  _builds_left(connection, engine, h.left, h.resets_at, h.basis))
        try:
            key = accounts.account_key(connection, settings, project_id, engine)
        except Exception:  # noqa: BLE001
            key = engine
        st = "exhausted" if login["state"] == "expired" else h.state
        sources.append({
            "key": key, "engine": engine, "engine_label": label,
            "label": account_label(label, key), "pool": None, "taking_new_work": True,
            "left_pct": left, "state": st, "state_label": ("Sign-in expired" if login["state"] == "expired"
                            else STATE_LABELS.get(st, st)),
            "resets_at": resets, "basis": h.basis, "login": login,
            "at_limit": _at_limit(alts, None, resets, label),
            "builds_left": n, "builds_basis": why,
            "windows": _windows_rows(h, snap.policy.thresholds), "jobs": _jobs(snap, engine),
            "freshness": h.freshness(now),
        })
    order = {"exhausted": 0, "near_limit": 1, "tight": 2, "plenty": 3}
    worst = min(sources, key=lambda s: (order.get(s["state"], 4),
                                        s["left_pct"] if s["left_pct"] is not None else 101),
                default=None)
    return {"project_id": project_id, "sources": sources, "worst": worst,
            "at": (now or datetime.now(timezone.utc)).isoformat()}


#: The header chip is reused this long per project (the router snapshot's own TTL), and at once
#: rebuilt when the router's state changes (store.generation): the Command Center polls every
#: 10 s and must not pay the headroom reads each time (its query budget, BOSSAPP-1342 review).
CHIP_MEMO_SECONDS = 20.0
_CHIP_MEMO: dict[str, tuple[float, int, Optional[dict[str, Any]]]] = {}


def chip_memo(project_id: str) -> tuple[bool, Optional[dict[str, Any]]]:
    """(True, chip) while the memoised chip is fresh — read with no database work at all, so a
    caller can skip even its savepoint — else (False, None)."""
    import time

    from .model_router import store

    hit = _CHIP_MEMO.get(project_id)
    if hit and time.monotonic() - hit[0] < CHIP_MEMO_SECONDS and hit[1] == store.generation():
        return True, hit[2]
    return False, None


def header_chip(connection: Any, settings: Any, project_id: str) -> Optional[dict[str, Any]]:
    """The Command Center header's headroom chip, memoised (CHIP_MEMO_SECONDS, store generation).
    See `_header_chip`."""
    import time

    from .model_router import store

    fresh, value = chip_memo(project_id)
    if fresh:
        return value
    at, gen = time.monotonic(), store.generation()
    value = _header_chip(connection, settings, project_id)
    _CHIP_MEMO[project_id] = (at, gen, value)
    return value


def _header_chip(connection: Any, settings: Any, project_id: str) -> Optional[dict[str, Any]]:
    """The Command Center header's headroom chip: each account in a few words, worst first —
    project_view's own rows, so the chip, Govern and the router never disagree. None when the
    project has no subscription."""
    view = project_view(connection, settings, project_id, lean=True)
    if not view["sources"]:
        return None
    order = {"exhausted": 0, "near_limit": 1, "tight": 2, "plenty": 3}
    rows = sorted(view["sources"], key=lambda s: (order.get(s["state"], 4),
                                                  s["left_pct"] if s["left_pct"] is not None else 101))
    return {"accounts": [{"key": s["key"], "label": s["label"], "left_pct": s["left_pct"],
                          "state": s["state"], "state_label": s["state_label"],
                          "resets_at": s["resets_at"], "login": s["login"]["state"],
                          "at_limit": s["at_limit"]["words"],
                          "taking_new_work": bool(s.get("taking_new_work")),
                          "pool": (s.get("pool") or {}).get("name")} for s in rows]}


# ── alerts ───────────────────────────────────────────────────────────────────────────────────

#: Warn when an account's week falls to 20, 10 and 5 percent left (the operator's levels,
#: BOSSAPP-1342), and at the limit: each crossing is said once, with its own severity.
ALERT_LEVELS = (20, 10, 5)
LIMIT = "limit"
ALERT_SEVERITY = {20: "info", 10: "warn", 5: "critical", LIMIT: "critical"}
KIND_PREFIX = "subscription_limit:"


def alert_level(account: dict[str, Any]) -> Any:
    """The lowest level the account has fallen to: LIMIT, 5, 10, 20 — or None (above 20%)."""
    if account.get("state") == "exhausted":
        return LIMIT
    pct = account.get("left_pct")
    if pct is None:
        return None
    return next((lvl for lvl in sorted(ALERT_LEVELS) if pct <= lvl), None)


def _windows_rows(h: Any, thresholds: Any) -> list[dict[str, Any]]:
    """The 5-hour and weekly windows, each with its own % left, state and reset — the numbers
    the headroom strip is made of, one row per window the CLI or a limit message reported."""
    from .model_router.headroom import (SESSION, STATE_LABELS, WEEKLY, EXHAUSTED, state_for)

    names = {SESSION: "5-hour", WEEKLY: "Weekly", "seven_day_opus": "Weekly (Opus)",
             "seven_day_sonnet": "Weekly (Sonnet)"}
    rows = []
    for kind in (SESSION, WEEKLY, "seven_day_opus", "seven_day_sonnet"):
        w = (h.windows or {}).get(kind)
        if w is None:
            continue
        left = None if w.spent else 1.0 - w.used
        st = EXHAUSTED if w.spent else state_for(left, thresholds or {})
        rows.append({"kind": kind, "label": names[kind], "left_pct": _pct(0.0 if w.spent else left),
                     "state": st, "state_label": STATE_LABELS[st],
                     "resets_at": _iso(w.resets_at),
                     "observed_at": _iso(w.observed_at)})
    return rows


def _jobs(snap: Any, engine: str) -> list[str]:
    """Which jobs route to this source: the ones pinned to it in Setup, and — under Smart
    routing — the tiers whose targets name it."""
    from .model_router.tiers import TIER_LABELS, TIERS

    out = []
    for job, (pinned, _model) in sorted((getattr(snap, "job_pins", None) or {}).items()):
        if pinned == engine:
            out.append(job.capitalize())
    try:
        if snap.policy.mode == "smart":
            for tier in TIERS:
                if any(t.engine == engine for t in snap.policy.targets(tier)):
                    out.append(f"{TIER_LABELS[tier]} tier")
    except Exception:  # noqa: BLE001
        pass
    return out


def install_accounts(connection: Any, *, now: Optional[datetime] = None) -> list[dict[str, Any]]:
    """Every subscription account BOSS has seen a window for, install-wide (an account's week is
    the account's, not a project's), with its headroom — the router's own rows
    (subscription_accounts) and assessment, no provider call."""
    from .model_router.headroom import STATE_LABELS, assess
    from .model_router.selection import engine_label

    try:
        with connection.begin_nested():
            rows = connection.execute(text(
                # ONE ROW PER ACCOUNT: an install login adopted into several orgs' pools is one
                # account with several seat names; only a pool seat's own key carries its name
                # (never another org's label for the install login, BOSSAPP-1342 review).
                "SELECT DISTINCT ON (a.account_key) a.account_key, a.engine, a.windows, "
                "a.learned_weekly_allowance_usd, CASE WHEN a.account_key LIKE '%:pool:%' "
                "THEN m.label END AS member_label FROM subscription_accounts a LEFT JOIN "
                "subscription_pool_members m ON m.account_key = a.account_key "
                "ORDER BY a.account_key, m.created_at")).mappings().all()
    except Exception:  # noqa: BLE001 — an older schema: nothing to alert about
        return []
    out = []
    for r in rows:
        windows = r["windows"] if isinstance(r["windows"], dict) else json.loads(r["windows"] or "{}")
        h = assess(str(r["engine"]), {"windows": windows}, thresholds={}, now=now)
        out.append({"account_key": str(r["account_key"]), "engine": str(r["engine"]),
                    "label": account_label(engine_label(str(r["engine"])), str(r["account_key"]),
                                           str(r["member_label"] or "")),
                    "left_pct": _pct(h.left), "state": h.state,
                    "state_label": STATE_LABELS.get(h.state, h.state),
                    "resets_at": _iso(h.resets_at), "detail": h.detail})
    return out


def _reset_words(resets: Optional[str]) -> str:
    try:
        return f" It resets {datetime.fromisoformat(str(resets)):%a %d %b %H:%M} UTC." if resets else ""
    except ValueError:
        return ""


def findings_for(accounts: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """system_doctor findings for the accounts at 20, 10 or 5 percent of the week left (a banner)
    and at the limit (a card with the choices). The level is the finding's evidence `state`, so
    each crossing counts as a change (raise_findings) and is said once."""
    out = []
    for a in accounts:
        level = alert_level(a)
        if level is None:
            continue
        label = a["label"]
        when = _reset_words(a.get("resets_at"))
        if level == LIMIT:
            title = f"{label}: limit reached"
            detail = (f"{label} has no subscription window left.{when} BOSS keeps work moving by "
                      "the routing rules: it moves new work to the next account or another "
                      "connected source when there is one, and otherwise pauses new work on it "
                      "until the reset. Nothing running was stopped.")
            options = [
                {"key": "limit_see_govern", "label": "See subscriptions",
                 "tip": "Opens Govern: every account, its windows and what BOSS does at the limit."},
                {"key": "limit_open_setup", "label": "Choose another model",
                 "tip": "Opens Setup, where each job picks its model."},
                {"key": "keep_going", "label": "Remind me in an hour", "tip": "BOSS asks again."}]
        else:
            title = f"{label}: {a.get('left_pct')}% of the week left"
            detail = (f"{label} is at {a.get('left_pct')}% of its weekly window (under "
                      f"{level}%).{when} BOSS keeps routing by the project's rules and moves work "
                      "away before it runs out.")
            options = []
        out.append({"kind": KIND_PREFIX + a["account_key"], "severity": ALERT_SEVERITY[level],
                    "title": title, "detail": detail, "options": options,
                    "needs_choice": level == LIMIT,
                    "evidence": {"account_key": a["account_key"], "engine": a["engine"],
                                 "state": f"level_{level}", "router_state": a.get("state"),
                                 "left_pct": a.get("left_pct"),
                                 "resets_at": a.get("resets_at")}, "auto": []})
    return out


# ── the usual notification channels ─────────────────────────────────────────────────────────

def alert_names(account_key: str) -> dict[Any, str]:
    """One alert name per level, so crossing 20 -> 10 -> 5 -> the limit is a new message each
    time and the level left behind resolves. Keyed on the ACCOUNT, never its label: a renamed
    seat still resolves its own alerts (the label is in the alert's summary)."""
    names: dict[Any, str] = {lvl: f"Subscription {account_key}: {lvl}% or less of the week left"
                             for lvl in ALERT_LEVELS}
    names[LIMIT] = f"Subscription {account_key}: limit reached"
    return names


def projects_using(connection: Any, account: dict[str, Any]) -> list[str]:
    """The projects told about an account: a pool seat's pool project, else the projects that
    built on its engine in the last week (an install login serves every project)."""
    try:
        with connection.begin_nested():
            pool_project = connection.execute(text(
                "SELECT p.project_id FROM subscription_pool_members m JOIN subscription_pools p "
                "ON p.id = m.pool_id WHERE m.account_key = :k LIMIT 1"),
                {"k": account["account_key"]}).scalar()
            if pool_project:
                return [str(pool_project)]
            return [str(r) for r in connection.execute(text(
                "SELECT DISTINCT project_id FROM code_task_runs WHERE created_at >= now() - "
                "interval '7 days' AND state->>'build_engine' = :e"),
                {"e": account["engine"]}).scalars()]
    except Exception:  # noqa: BLE001 — an older schema: nobody to tell beyond the console
        return []


def notify_channels(connection: Any, accounts: list[dict[str, Any]], *,
                    project_ids: Optional[list[str]] = None,
                    now: Optional[datetime] = None) -> int:
    """Say each level through the project's usual notification channels: a BOSS self-check
    alert (monitor_scheduler.meta_alert -> notify's meta route: the console and the owner's
    email, with the outbox's retries and receipts). One alert per account and level; the level
    the account left resolves. `project_ids` adds projects to tell (the run that observed it).
    Returns how many alerts fired. Never raises."""
    from .monitor_scheduler import meta_alert

    now = now or datetime.now(timezone.utc)
    fired = 0
    for a in accounts:
        try:
            with connection.begin_nested():
                level = alert_level(a)
                names = alert_names(a["account_key"])
                found = findings_for([a])
                summary = (found[0]["title"] + ". " + found[0]["detail"]) if found else ""
                # WHEREVER THESE ALERTS FIRE: a level raised in a project that is not (or no
                # longer) a user of the account — the run that observed it — resolves there too.
                firing: dict[str, set[str]] = {}
                for pid, name in connection.execute(text(
                        "SELECT project_id, alertname FROM app_alerts WHERE state = 'firing' "
                        "AND alertname = ANY(:n)"), {"n": list(names.values())}).all():
                    firing.setdefault(str(pid), set()).add(str(name))
                for pid, names_on in firing.items():
                    for lvl, name in names.items():
                        if lvl != level and name in names_on:
                            meta_alert(connection, pid, name, "", False, now)
                if level is not None:
                    pids = dict.fromkeys([*(project_ids or []), *projects_using(connection, a)])
                    for pid in pids:
                        if names[level] not in firing.get(pid, set()):
                            meta_alert(connection, pid, names[level], summary[:900], True, now)
                            fired += 1
        except Exception:  # noqa: BLE001 — telling people never breaks the tick or the call
            log.warning("subscription headroom notification failed (non-fatal)", exc_info=True)
    return fired


def observe_hook(connection: Any, settings: Any, project_id: Optional[str], account_key: str,
                 engine: str) -> None:
    """Called when an account's state moved (a run's call reported a window, a limit, or a
    success cleared one): the alert is raised or lifted at once and, when the state changed,
    ONE event says so. Never raises."""
    try:
        with connection.begin_nested():
            _raise(connection, settings, project_id, account_key, engine)
    except Exception:  # noqa: BLE001 — bookkeeping never fails the call it observes
        log.warning("subscription headroom alert failed (non-fatal)", exc_info=True)


def _raise(connection: Any, settings: Any, project_id: Optional[str], account_key: str,
           engine: str) -> None:
    if True:
        from .core import emit_event
        from .system_doctor import raise_findings, resolve_finding

        mine = [a for a in install_accounts(connection) if a["account_key"] == account_key]
        if not mine:
            return
        kind = KIND_PREFIX + account_key
        notify_channels(connection, mine, project_ids=[project_id] if project_id else None)
        found = findings_for(mine)
        if not found:
            resolve_finding(connection, kind)
            return
        changed = raise_findings(connection, found)
        if changed and project_id:
            a = mine[0]
            did = {}
            try:
                view = project_view(connection, settings, project_id)
                did = next((s["at_limit"] for s in view["sources"] if s["key"] == account_key), {})
            except Exception:  # noqa: BLE001 — the event still says the state
                did = {}
            emit_event(connection, project_id, "subscription.headroom_changed", "boss",
                       "subscription_account", account_key,
                       {"engine": engine, "label": a["label"], "state": a["state"],
                        "state_label": a["state_label"], "left_pct": a["left_pct"],
                        "resets_at": a["resets_at"], "boss_will": did.get("words"),
                        "does": did.get("does")})


def govern_payload(connection: Any, settings: Any, project_id: str, *,
                   cacheable: bool = False) -> dict[str, Any]:
    """Govern's Subscriptions: project_view plus the open alerts raised for these accounts.
    `cacheable` drops what changes every second (`at`, the reading's age) so `/v1/govern`'s ETag
    still matches while nothing real moved; the console computes ages from `as_of`."""
    from . import system_doctor

    view = project_view(connection, settings, project_id)
    keys = {s["key"] for s in view["sources"]}
    view["alerts"] = [f for f in system_doctor.open_findings(connection)
                      if str(f["kind"]).startswith(KIND_PREFIX)
                      and str(f["kind"])[len(KIND_PREFIX):] in keys]
    if cacheable:
        view.pop("at", None)
        for src in view["sources"]:
            src["freshness"] = {"as_of": src["freshness"]["as_of"], "stale": src["freshness"]["stale"]}
        for alert in view["alerts"]:
            for k in ("first_seen", "last_seen", "snoozed_until"):
                alert.pop(k, None)
    return view


__all__ = ["ALERT_LEVELS", "alert_level", "alert_names", "govern_payload", "account_label", "chip_memo", "header_chip",
           "findings_for", "install_accounts", "notify_channels", "observe_hook", "project_view",
           "projects_using"]
