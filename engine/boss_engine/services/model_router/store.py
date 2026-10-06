"""The router's database side: the policy row, snapshots, route records, headroom observations.

Everything a route reads is loaded here ONCE per project per `SNAPSHOT_TTL_S` (Decision:
"Efficiency"), so `router.route()` itself never touches the database. Every function here is
best-effort where it serves work in flight: a missing table or a failed read degrades to the
legacy answer, never to a failed model call.
"""
from __future__ import annotations

import json
import logging
import threading
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Mapping, Optional

from sqlalchemy import text

from .classifier import jev_state
from .escalation import EscalationLedger
from .headroom import MODEL_WINDOWS, assess
from .policy import PolicyError, RoutingPolicy, from_dict, seed
from .router import ROUTER, RouteDecision, RouteRequest, Snapshot
from .selection import SourceState

log = logging.getLogger("boss.model_router")

SNAPSHOT_TTL_S = 20.0
MODE_KEY = "model_routing_mode"

_SNAPS: dict[str, tuple[float, Snapshot]] = {}
_SNAP_LOCK = threading.Lock()


class PolicyConflict(RuntimeError):
    """Someone saved the policy since this edit was read. Carries the current version."""

    def __init__(self, current: int):
        super().__init__(f"The routing policy changed (now version {current}). Reload and retry.")
        self.current = current


def invalidate(project_id: Optional[str] = None) -> None:
    with _SNAP_LOCK:
        if project_id is None:
            _SNAPS.clear()
            _MODES.clear()
        else:
            for key in [k for k in _SNAPS if k.split("|", 1)[0] == project_id]:
                _SNAPS.pop(key, None)
            _MODES.pop(project_id, None)


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


# ── The policy row ─────────────────────────────────────────────────────────────────────────────

def load_policy_doc(connection: Any, project_id: Optional[str]) -> tuple[Optional[dict], int]:
    if connection is None or not project_id:
        return None, 0
    try:
        with connection.begin_nested():
            row = connection.execute(
                text("SELECT policy, version FROM model_routing_policies WHERE project_id = :p"),
                {"p": project_id}).mappings().first()
    except Exception:  # noqa: BLE001 — no table yet is "no policy saved"
        return None, 0
    if not row:
        return None, 0
    return _json(row["policy"]), int(row["version"] or 0)


def routing_mode(connection: Any, settings: Any, project_id: Optional[str]) -> str:
    """The project's mode (`model_routing_mode`, a Govern-logged knob), else the install's."""
    from ..project_settings import effective

    try:
        value = effective(connection, project_id, settings, MODE_KEY)
    except Exception:  # noqa: BLE001
        value = getattr(settings, "model_routing_mode", "fixed")
    value = str(value or "fixed").strip().lower()
    return value if value in ("fixed", "smart") else "fixed"


_MODES: dict[str, tuple[float, str]] = {}


def mode_of(connection: Any, settings: Any, project_id: Optional[str]) -> str:
    """The mode alone, cached like a snapshot: what a Fixed-mode call needs to know (that it
    is Fixed) without loading anything else."""
    key = project_id or ""
    with _SNAP_LOCK:
        snap = _SNAPS.get(key)
        if snap and time.monotonic() < snap[0]:
            return snap[1].policy.mode
        hit = _MODES.get(key)
        if hit and time.monotonic() < hit[0]:
            return hit[1]
    mode = routing_mode(connection, settings, project_id)
    with _SNAP_LOCK:
        _MODES[key] = (time.monotonic() + SNAPSHOT_TTL_S, mode)
    return mode


def effective_policy(connection: Any, settings: Any, project_id: Optional[str], *,
                     connected: Optional[Mapping[str, Any]] = None,
                     build_engine: Optional[str] = None) -> RoutingPolicy:
    """The stored policy merged over the one seeded from this install's settings — so a key
    the operator never saved still reads as the install would start with it."""
    from .. import model_routing
    from ..project_settings import applied

    effective_settings = applied(connection, project_id, settings) if project_id else settings
    if connected is None:
        connected = _connected(connection, effective_settings, project_id)
    if build_engine is None:
        try:
            build_engine = model_routing.resolve_job(connection, settings, project_id,
                                                     "build").engine
        except Exception:  # noqa: BLE001
            build_engine = ""
    profile = None
    if connection is not None and project_id:
        try:
            from ..pipeline_profile_service import resolve_for_run

            with connection.begin_nested():
                profile = resolve_for_run(connection, project_id, None, settings)
        except Exception:  # noqa: BLE001
            profile = None
    openrouter_models = None
    if "openrouter" in connected:
        try:
            from ..openrouter_catalog import suggest

            openrouter_models = suggest()
        except Exception:  # noqa: BLE001
            openrouter_models = None
    base = seed(settings=effective_settings, connected=connected,
                build_engine=build_engine or getattr(effective_settings, "code_agent_engine",
                                                     "claude_code"),
                profile=profile, openrouter_models=openrouter_models)
    doc, version = load_policy_doc(connection, project_id)
    policy = from_dict(doc, base=base) if doc else base
    mode = routing_mode(connection, settings, project_id)
    from dataclasses import replace

    return replace(policy, mode=mode, version=version)


def save_policy(connection: Any, project_id: str, patch: Mapping[str, Any], *, actor: str,
                settings: Any, expected_version: Optional[int] = None) -> RoutingPolicy:
    """Merge `patch` into the project's policy and store it, refusing a stale edit. `mode` is
    written through project_settings so Govern's log carries the switch."""
    from ..core import emit_event
    from ..project_settings import set_one

    current = effective_policy(connection, settings, project_id)
    if expected_version is not None and int(expected_version) != current.version:
        raise PolicyConflict(current.version)
    body = {k: v for k, v in dict(patch).items() if k not in ("version",)}
    preset = str(body.get("floor_preset") or "")
    profile = None
    if preset and preset != "custom":
        from ..pipeline_profile_service import resolve_for_run

        profile = resolve_for_run(connection, project_id, preset, settings)
        if profile.get("name") != preset:
            raise PolicyError(f"Unknown floor preset '{preset}'.")
    if profile is not None and "floors" not in body:
        # A PRESET IS A PROFILE (Decision 4): choosing one sets the Smart floors AND the
        # project's default pipeline profile, which Fixed mode routes by — one control, one
        # meaning, in both modes (the old Coding Pipeline page's picker). A custom profile
        # converts the same way.
        from .policy import floors_from_profile

        body["floors"] = [f.to_dict() for f in floors_from_profile(profile)]
    merged = from_dict(body, base=current)          # validates; raises PolicyError
    if profile is not None:
        from ..pipeline_profile_service import set_default_profile

        set_default_profile(connection, project_id, preset, actor)
    if "mode" in body:
        set_one(connection, project_id, MODE_KEY, merged.mode, actor=actor)
    document = merged.to_dict()
    document.pop("mode", None)
    document.pop("version", None)
    new_version = current.version + 1
    connection.execute(
        text("INSERT INTO model_routing_policies (project_id, policy, version, updated_by, "
             "updated_at) VALUES (:p, CAST(:d AS JSONB), :v, :by, CURRENT_TIMESTAMP) "
             "ON CONFLICT (project_id) DO UPDATE SET policy = EXCLUDED.policy, "
             "version = EXCLUDED.version, updated_by = EXCLUDED.updated_by, "
             "updated_at = CURRENT_TIMESTAMP"),
        {"p": project_id, "d": json.dumps(document), "v": new_version, "by": actor})
    emit_event(connection, project_id, "model_routing.policy_changed", actor or "operator",
               "model_routing_policy", project_id,
               {"version": new_version, "keys": sorted(body)})
    invalidate(project_id)
    from dataclasses import replace

    return replace(merged, version=new_version)


# ── The snapshot ───────────────────────────────────────────────────────────────────────────────

def _connected(connection: Any, settings: Any, project_id: Optional[str]) -> dict[str, Any]:
    from .. import setup_service

    try:
        if connection is not None and project_id:
            with connection.begin_nested():
                return setup_service.connected_engines(connection, settings, project_id)
    except Exception:  # noqa: BLE001
        pass
    try:
        return setup_service.environment_engines(settings)
    except Exception:  # noqa: BLE001
        return {}


def _used_usd(connection: Any, project_id: Optional[str]) -> dict[str, float]:
    """API-equivalent value of included calls per engine over the trailing week — the
    estimate behind headroom when the CLI has not reported a number."""
    if connection is None or not project_id:
        return {}
    since = datetime.now(timezone.utc) - timedelta(days=7)
    try:
        with connection.begin_nested():
            rows = connection.execute(
                text("SELECT engine, COALESCE(SUM(cost_usd_micros), 0) AS micros FROM llm_calls "
                     "WHERE project_id = :p AND included_in_subscription AND created_at >= :s "
                     "GROUP BY engine"), {"p": project_id, "s": since}).mappings().all()
    except Exception:  # noqa: BLE001
        return {}
    return {str(r["engine"] or ""): float(r["micros"] or 0) / 1e6 for r in rows}


def _budget_state(connection: Any, settings: Any, project_id: Optional[str]) -> str:
    if connection is None or not project_id:
        return "green"
    try:
        from ..budget import BudgetGovernor

        with connection.begin_nested():
            return str(BudgetGovernor(settings).status_for_project(connection, project_id)
                       .get("budget_state") or "green")
    except Exception:  # noqa: BLE001
        return "green"


def _job_pins(connection: Any, project_id: Optional[str]) -> dict[str, tuple[str, Optional[str]]]:
    from ..model_routing import JOB_KNOBS, chat_model_knob

    if connection is None or not project_id:
        return {}
    try:
        from ..project_settings import get_all

        with connection.begin_nested():
            chosen = get_all(connection, project_id)
    except Exception:  # noqa: BLE001
        return {}
    pins: dict[str, tuple[str, Optional[str]]] = {}
    for job in ("build", "review", "chat"):
        engine_knob, model_knob = JOB_KNOBS[job]
        engine = str(chosen.get(engine_knob) or "").strip().lower()
        if not engine:
            continue
        model = chosen.get(model_knob or chat_model_knob(engine))
        pins[job] = (engine, str(model) if model else None)
    return pins


def _subscription_windows(connection: Any, settings: Any, project_id: Optional[str],
                          engines: list[str]) -> dict[str, tuple[str, dict[str, Any]]]:
    """engine → (account key, account row) for each subscription engine: windows pooled per
    ACCOUNT (model_router.accounts), so one project's limit is every project's limit."""
    from . import accounts

    keys = {e: accounts.account_key(connection, settings, project_id, e) for e in engines}
    rows = accounts.load(connection, list(set(keys.values()))) if keys else {}
    return {e: (k, rows.get(k) or {}) for e, k in keys.items()}


def sources_from(connected: Mapping[str, Mapping[str, Any]], policy: RoutingPolicy,
                 used: Mapping[str, float], now: Optional[datetime] = None,
                 accounts_by_engine: Optional[Mapping[str, tuple[str, dict]]] = None,
                 with_headroom: bool = True) -> dict[str, SourceState]:
    from . import accounts

    out: dict[str, SourceState] = {}
    for engine, info in connected.items():
        capacity = info.get("capacity") or {}
        observed = capacity.get("observed") if isinstance(capacity.get("observed"), dict) else {}
        kind = str(info.get("kind") or "metered")
        if kind == "env":
            kind = "local" if engine in ("ollama", "vllm") else (
                "subscription" if info.get("env_attr") == "claude_code_oauth_token" else "env")
        account_key, account = (accounts_by_engine or {}).get(engine, (None, {}))
        if kind == "subscription" and with_headroom:
            observed = {**{k: v for k, v in observed.items() if k != "windows"},
                        "windows": dict(account.get("windows") or {})}
            if account.get("learned_weekly_allowance_usd"):
                observed["learned_weekly_allowance_usd"] = account["learned_weekly_allowance_usd"]
            previous = accounts.PREVIOUS.get(account_key or engine)
            headroom = assess(engine, observed, thresholds=policy.thresholds,
                              used_usd=used.get(engine),
                              allowance_usd=policy.weekly_allowance_usd.get(engine), now=now,
                              previous=previous)
            accounts.PREVIOUS[account_key or engine] = headroom.state
        else:
            headroom = assess(engine, {k: v for k, v in observed.items() if k != "windows"},
                              thresholds=policy.thresholds, now=now)
        out[engine] = SourceState(
            engine=engine, kind=kind, connected=True,
            working=info.get("working") is not False,
            available=bool(capacity.get("available", True)),
            source_id=info.get("source_id"), headroom=headroom)
    return out


def snapshot(connection: Any, settings: Any, project_id: Optional[str], *,
             fresh: bool = False, lite: bool = False) -> Snapshot:
    """Everything a route reads. Cached per project; never raises (a failure is Fixed mode
    with no sources, which routes exactly as the legacy code would).

    `lite`: what a Fixed-mode route needs — the connected sources and nothing else (no policy
    seeding, no headroom, no budget, no pins), so Fixed mode pays a fraction of a full load."""
    key = f"{project_id or ''}|{'lite' if lite else 'full'}"
    if not fresh:
        with _SNAP_LOCK:
            hit = _SNAPS.get(key)
            if hit and time.monotonic() < hit[0]:
                return hit[1]
    try:
        from ..project_settings import applied

        effective = applied(connection, project_id, settings) if project_id else settings
        connected = _connected(connection, effective, project_id)
        if lite:
            snap = Snapshot(project_id=project_id, policy=RoutingPolicy(mode="fixed"),
                            sources=sources_from(connected, RoutingPolicy(), {},
                                                 with_headroom=False),
                            settings=effective)
        else:
            policy = effective_policy(connection, settings, project_id, connected=connected)
            subs = [e for e, i in connected.items()
                    if str(i.get("kind")) == "subscription"
                    or i.get("env_attr") == "claude_code_oauth_token"]
            snap = Snapshot(
                project_id=project_id, policy=policy,
                sources=sources_from(
                    connected, policy, _used_usd(connection, project_id),
                    accounts_by_engine=_subscription_windows(connection, effective, project_id,
                                                             subs)),
                budget_state=_budget_state(connection, settings, project_id),
                job_pins=_job_pins(connection, project_id), jev_state=jev_state(effective),
                settings=effective)
            _wire_deciders(snap, connection, effective, project_id)
            if "openrouter" in connected:
                _openrouter_catalogue(connection, effective, project_id)
    except Exception:  # noqa: BLE001 — routing must never fail the work it serves
        log.warning("model router snapshot failed; routing as Fixed", exc_info=True)
        snap = Snapshot(project_id=project_id, policy=RoutingPolicy(), sources={},
                        settings=settings)
    with _SNAP_LOCK:
        _SNAPS[key] = (time.monotonic() + SNAPSHOT_TTL_S, snap)
    return snap


_CATALOGUE_REFRESHING = threading.Lock()


def _openrouter_catalogue(connection: Any, settings: Any, project_id: Optional[str]) -> None:
    """OpenRouter's priced catalogue: loaded from the database on first use, refreshed in the
    background when older than its TTL — never a network call on the routing path."""
    from .. import openrouter_catalog as cat

    if not cat.cached():
        cat.load(connection)
    if cat.fresh() or not project_id or not _CATALOGUE_REFRESHING.acquire(blocking=False):
        return
    try:
        db_engine = connection.engine
    except Exception:  # noqa: BLE001
        _CATALOGUE_REFRESHING.release()
        return

    def run() -> None:
        try:
            with db_engine.begin() as c:
                cat.refresh(c, settings, project_id)
        except Exception:  # noqa: BLE001
            log.warning("OpenRouter catalogue refresh failed", exc_info=True)
        finally:
            _CATALOGUE_REFRESHING.release()

    threading.Thread(target=run, name="openrouter-catalogue", daemon=True).start()


def _wire_deciders(snap: Snapshot, connection: Any, settings: Any,
                   project_id: Optional[str]) -> None:
    """Jev and the cheap-model classifier, built only when a route actually asks for them."""
    if snap.jev_state == "ready":
        def jev():
            from ..typed_decisions import TypeSafeDecider

            return TypeSafeDecider(settings)
        snap.jev_factory = jev
    if not project_id:
        return
    try:
        db_engine = connection.engine
    except Exception:  # noqa: BLE001
        return

    def model():
        return GovernedModelDecider(db_engine, settings, project_id, snap)
    snap.model_factory = model


class GovernedModelDecider:
    """The cheap-model classifier: a Fast-tier source answering the typed tier question
    through the governed adapter — reserved, receipted and settled (Decision 9)."""

    backend = "model"

    def __init__(self, db_engine: Any, settings: Any, project_id: str, snap: Snapshot):
        self._db, self._settings, self._project_id, self._snap = (
            db_engine, settings, project_id, snap)

    def decide(self, context: str, questions: list):
        from ..model_routing import ModelChoice, adapter_for, governed
        from ..typed_decisions import DecisionBatch, _parse, _prompt
        from .selection import select

        chosen = select("fast", self._snap.policy, self._snap.sources)
        if chosen.target is None:
            raise RuntimeError("no Fast-tier source for the classifier")
        started = time.monotonic()
        with self._db.begin() as connection:
            choice = ModelChoice(chosen.target.engine, "router classifier",
                                 chosen.source.source_id if chosen.source else None)
            adapter = governed(
                adapter_for(choice, connection, self._settings, self._project_id,
                            model=chosen.target.model),
                connection, self._settings, self._project_id,
                skill_id="model.route_classify", ref="router")
            _, answer, usage = adapter.create_text_response(_prompt(context, questions))
        return DecisionBatch(decisions=_parse(answer, questions), backend="model",
                             model=str(usage.get("model") or chosen.target.model or ""),
                             latency_ms=int((time.monotonic() - started) * 1000),
                             cost_usd_micros=int(usage.get("cost_usd_micros") or 0),
                             usage=usage)


# ── Routing + recording, for callers ───────────────────────────────────────────────────────────

def route(connection: Any, settings: Any, request: RouteRequest, *,
          ledger: Optional[EscalationLedger] = None, record_it: bool = True) -> RouteDecision:
    """The one call sites use: snapshot (cached), route (pure), record (one insert)."""
    mode = (request.mode_override or mode_of(connection, settings, request.project_id))
    agent_mode = str((request.agent_policy or {}).get("selection_mode") or "router").lower()
    # A Fixed route needs the connected sources only; Smart (or an agent's Local/Pinned policy,
    # or an experiment's Smart arm on a Fixed project) needs the whole picture, headroom too.
    lite = mode != "smart" and agent_mode not in ("local", "pinned")
    snap = snapshot(connection, settings, request.project_id, lite=lite)
    if request.sticky and request.unit_id and request.session_pin is None \
            and mode == "smart":
        prior = last_route(connection, request.unit_id, request.step, request.ref)
        if prior and prior.get("engine"):
            from dataclasses import replace

            from .policy import Target

            request = replace(request, session_pin=Target(prior["engine"], prior.get("model")))
    decision = ROUTER.route(request, snap, ledger=ledger)
    if record_it and connection is not None:
        record(connection, decision)
    return decision


_RECORDED: dict[str, float] = {}


def record(connection: Any, decision: RouteDecision) -> None:
    """One `model_routes` row per distinct decision (a reused id is not written twice). Best
    effort, in a savepoint: the record of a route is not the route."""
    if decision.id in _RECORDED:
        return
    c = decision.classification
    try:
        with connection.begin_nested():
            connection.execute(text("""
                INSERT INTO model_routes (id, project_id, unit_kind, unit_id, step, ref, role, mode,
                    tier, classified_tier, classifier, confidence, engine, model, source_id,
                    source_kind, rules, why, reason, escalation, escalation_signal, fallback,
                    moved_by_headroom, budget_lite, latency_ms)
                VALUES (:id, :p, :k, :u, :s, :ref, :r, :m, :t, :ct, :cl, :cf, :e, :mo, :si, :sk,
                    :rules, :why, :reason, :esc, :sig, :fb, :mv, :bl, :lat)
                ON CONFLICT (id) DO NOTHING"""),
                {"id": decision.id, "p": decision.project_id, "k": decision.kind,
                 "u": decision.unit_id, "s": decision.step, "ref": decision.ref,
                 "r": decision.role,
                 "m": decision.mode, "t": decision.tier, "ct": decision.classified_tier,
                 "cl": c.classifier if c else None,
                 "cf": round(c.confidence, 3) if c else None,
                 "e": decision.engine or None, "mo": decision.model,
                 "si": decision.source_id, "sk": decision.source_kind or None,
                 "rules": json.dumps(list(decision.rules)), "why": json.dumps(list(decision.why)),
                 "reason": decision.reason[:500], "esc": decision.escalation,
                 "sig": decision.escalation_signal, "fb": decision.fallback,
                 "mv": decision.moved_by_headroom, "bl": decision.budget_lite,
                 "lat": round(decision.latency_ms, 3)})
        _RECORDED[decision.id] = time.monotonic()
        if len(_RECORDED) > 50_000:
            _RECORDED.pop(next(iter(_RECORDED)))
    except Exception:  # noqa: BLE001
        log.warning("model route record failed (non-fatal)", exc_info=True)


def last_route(connection: Any, unit_id: str, step: str, ref: Optional[str] = None
               ) -> Optional[dict[str, Any]]:
    """The unit's last route for `step` (and `ref`) — the one lookup a resumed session makes."""
    try:
        with connection.begin_nested():
            row = connection.execute(
                text("SELECT engine, model, tier FROM model_routes WHERE unit_id = :u "
                     "AND step = :s AND COALESCE(ref, '') = :r AND mode = 'smart' "
                     "ORDER BY created_at DESC LIMIT 1"),
                {"u": unit_id, "s": step, "r": ref or ""}).mappings().first()
    except Exception:  # noqa: BLE001
        return None
    return dict(row) if row else None


def agent_model_policy(connection: Any, project_id: Optional[str], role_key: Optional[str]
                       ) -> Optional[dict[str, Any]]:
    """The Agent Studio model policy of the agent holding `role_key`: its published version's,
    else its newest draft's. None when the agent was never built in Agent Studio."""
    if connection is None or not project_id or not role_key:
        return None
    try:
        with connection.begin_nested():
            row = connection.execute(
                text("SELECT model_policy FROM agent_versions WHERE project_id = :p "
                     "AND role_key = :r AND status <> 'retired' "
                     "ORDER BY (status = 'published') DESC, version DESC LIMIT 1"),
                {"p": project_id, "r": role_key}).mappings().first()
    except Exception:  # noqa: BLE001
        return None
    policy = _json(row["model_policy"]) if row else {}
    return policy or None


def routes_for(connection: Any, unit_id: str) -> list[dict[str, Any]]:
    rows = connection.execute(
        text("SELECT * FROM model_routes WHERE unit_id = :u ORDER BY created_at, id"),
        {"u": unit_id}).mappings().all()
    out = []
    for r in rows:
        item = dict(r)
        for key in ("rules", "why"):
            value = item.get(key)
            item[key] = json.loads(value) if isinstance(value, str) else (value or [])
        if item.get("confidence") is not None:
            item["confidence"] = float(item["confidence"])
        if item.get("latency_ms") is not None:
            item["latency_ms"] = float(item["latency_ms"])
        if hasattr(item.get("created_at"), "isoformat"):
            item["created_at"] = item["created_at"].isoformat()
        # Words for the console ("Why this model"): it renders, it does not look things up.
        from .selection import engine_label
        from .tiers import TIER_LABELS, step_label

        name = engine_label(item.get("engine") or "") if item.get("engine") else "no model"
        item["label"] = f"{name} · {item['model']}" if item.get("model") else name
        item["step_label"] = step_label(str(item.get("step") or ""))
        item["tier_label"] = TIER_LABELS.get(str(item.get("tier") or ""), item.get("tier"))
        out.append(item)
    return out


# ── Headroom observations (fed by telemetry) ──────────────────────────────────────────────────

def observe(connection: Any, settings: Any, project_id: Optional[str], engine: str, *,
            event: Optional[Mapping[str, Any]] = None, error: Optional[str] = None,
            ok_model: Optional[str] = None, now: Optional[datetime] = None
            ) -> dict[str, dict[str, Any]]:
    """Fold a call's evidence into its subscription ACCOUNT (model_router.accounts)."""
    from . import accounts

    return accounts.observe(connection, settings, project_id, engine, event=event,
                            error=error, ok_model=ok_model, now=now)


__all__ = ["PolicyConflict", "PolicyError", "MODEL_WINDOWS", "effective_policy", "invalidate",
           "observe", "record", "route", "routes_for", "save_policy", "snapshot"]
