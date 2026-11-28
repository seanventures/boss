"""Setup › Model routing, as data — the console renders this and decides nothing itself.

No network: OpenRouter's catalogue and the CLIs' model lists come from their caches.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from sqlalchemy import text

from . import store
from .classifier import jev_state
from .headroom import STATE_LABELS
from .policy import CLASSIFIER_BACKENDS, ENGINE_TIER_MODELS, RoutingPolicy
from .selection import engine_label
from .tiers import STEP_LABELS, TIER_LABELS, TIERS

#: List prices for "saved compared with all-Frontier" (USD per million tokens, input/output).
#: An estimate, and labelled one: a subscription's real price is flat.
REFERENCE_PRICE = {"fast": (1.0, 5.0), "balanced": (3.0, 15.0), "frontier": (5.0, 25.0)}

#: The profiles' names as presets. Not "Balanced" (a tier on the same row) nor "VAG" (jargon).
PRESETS = (("lean", "Lean"), ("balanced", "Standard"), ("strict", "Strict"),
           ("vag", "Verify-heavy"))

#: Steps an operator can put a floor on, in pipeline order.
FLOOR_STEPS = ("spec", "plan", "write_tests", "implement", "review_code", "security_check",
               "consistency_check", "check_tests", "panel_facilitator", "panel_member",
               "research_synthesis", "chat", "agent", "docs", "writing")

BACKEND_LABELS = {"auto": "Auto", "jev": "Jev", "rules": "Rules only", "model": "Cheap model"}


def _options(engine: str, info: dict[str, Any], policy: RoutingPolicy) -> list[Optional[str]]:
    """The models offered for `engine` in a tier picker: what its CLI reports, the tier
    defaults, OpenRouter's cached catalogue, and whatever the policy already names."""
    models: list[Optional[str]] = []
    try:
        from ..setup_view import _offered_models

        models.extend(_offered_models(engine, info.get("model")))
    except Exception:  # noqa: BLE001
        pass
    models.extend(m for m in (ENGINE_TIER_MODELS.get(engine) or {}).values() if m)
    if engine == "openrouter":
        # The tier defaults stay offered whatever the catalogue holds, so replacing one never
        # makes it unchoosable again.
        from .policy import OPENROUTER_FALLBACK_MODELS

        models.extend(OPENROUTER_FALLBACK_MODELS.values())
        try:
            from ..openrouter_catalog import cached

            listed = sorted(cached().values(), key=lambda m: (m.blended, m.id))
            models.extend(m.id for m in listed if m.tools)
        except Exception:  # noqa: BLE001
            pass
    for tier in TIERS:
        models.extend(t.model for t in policy.targets(tier) if t.engine == engine and t.model)
    seen: list[Optional[str]] = []
    for m in models or [None]:
        if m not in seen:
            seen.append(m)
    return seen[:80]


def _week(connection: Any, project_id: str) -> dict[str, Any]:
    from ..openai_adapter import CACHE_READ_MULT, CACHE_WRITE_MULT, list_price_cost_usd_micros

    since = datetime.now(timezone.utc) - timedelta(days=7)
    out: dict[str, Any] = {"routes": 0, "tiers": {t: 0 for t in TIERS}, "escalations": 0,
                           "moved": 0, "saved_usd": 0.0, "smart_routes": 0, "shadow_routes": 0}
    try:
        with connection.begin_nested():
            rows = connection.execute(text(
                "SELECT tier, mode, COUNT(*) AS n, "
                "SUM(CASE WHEN escalation IS NOT NULL THEN 1 ELSE 0 END) AS esc, "
                "SUM(CASE WHEN moved_by_headroom THEN 1 ELSE 0 END) AS moved "
                "FROM model_routes WHERE project_id = :p AND created_at >= :s "
                "AND step <> 'connection_proof' "      # Setup's own checks are not work
                "GROUP BY tier, mode"), {"p": project_id, "s": since}).mappings().all()
            calls = connection.execute(text(
                "SELECT r.tier, c.model, "
                "COALESCE(c.prompt_tokens, 0) AS pin, "
                "COALESCE(c.completion_tokens, 0) AS pout, "
                "COALESCE(c.cache_read_tokens, 0) AS cr, "
                "COALESCE(c.cache_write_tokens, 0) AS cw, "
                "COALESCE(c.cost_usd_micros, 0) AS micros "
                "FROM llm_calls c JOIN model_routes r ON r.id = c.route_id "
                "WHERE c.project_id = :p AND c.created_at >= :s "
                "AND r.mode = 'smart'"),
                {"p": project_id, "s": since}).mappings().all()
    except Exception:  # noqa: BLE001
        return out
    for r in rows:
        n = int(r["n"] or 0)
        out["routes"] += n
        if r["tier"] in out["tiers"]:
            out["tiers"][r["tier"]] += n
        out["escalations"] += int(r["esc"] or 0)
        out["moved"] += int(r["moved"] or 0)
        if r["mode"] == "smart":
            out["smart_routes"] += n
        elif r["mode"] == "shadow":
            out["shadow_routes"] += n
    fin, fout = REFERENCE_PRICE["frontier"]
    for c in calls:
        if c["tier"] == "frontier":
            continue
        pin, pout = int(c["pin"] or 0), int(c["pout"] or 0)
        cr, cw = int(c["cr"] or 0), int(c["cw"] or 0)
        at_frontier = (pin * fin + pout * fout
                       + cr * fin * CACHE_READ_MULT + cw * fin * CACHE_WRITE_MULT) / 1e6
        list_micros, known = list_price_cost_usd_micros({
            "model": c["model"] or "", "prompt_tokens": pin, "completion_tokens": pout,
            "cache_read_tokens": cr, "cache_write_tokens": cw,
        })
        tin, tout = REFERENCE_PRICE.get(str(c["tier"]), (fin, fout))
        fallback_tier = (pin * tin + pout * tout
                         + cr * tin * CACHE_READ_MULT + cw * tin * CACHE_WRITE_MULT) / 1e6
        actual = (list_micros / 1e6 if (known and (pin + pout + cr + cw) > 0)
                  else (float(c["micros"] or 0) / 1e6 or fallback_tier))
        out["saved_usd"] += max(0.0, at_frontier - actual)
    out["saved_usd"] = round(out["saved_usd"], 2)
    return out


def _presets(connection: Any, project_id: str) -> list[dict[str, str]]:
    """The four built-in profiles, then this project's custom ones (Enterprise)."""
    out = [{"name": n, "label": label} for n, label in PRESETS]
    try:
        from ..pipeline_profile_service import list_profiles

        with connection.begin_nested():
            for p in list_profiles(connection, project_id):
                if p.get("custom"):
                    out.append({"name": p["name"], "label": str(p.get("label") or p["name"])[:40]})
    except Exception:  # noqa: BLE001
        pass
    return out


def routing_view(connection: Any, settings: Any, project_id: str) -> dict[str, Any]:
    snap = store.snapshot(connection, settings, project_id)
    policy = snap.policy
    connected = store._connected(connection, snap.settings or settings, project_id)
    sources = []
    for engine, info in connected.items():
        state = snap.sources.get(engine)
        sources.append({
            "engine": engine, "label": engine_label(engine),
            "kind": state.kind if state else str(info.get("kind") or ""),
            "working": bool(state.working) if state else True,
            "models": _options(engine, info, policy)})
    sources.sort(key=lambda s: ({"subscription": 0, "local": 1}.get(s["kind"], 2), s["label"]))
    headroom = []
    for engine, state in snap.sources.items():
        if state.kind != "subscription":
            continue
        h = state.headroom
        headroom.append({**h.to_dict(), "engine": engine, "source_label": engine_label(engine),
                         "describe": h.describe()})

    def target(t) -> dict[str, Any]:
        return {"engine": t.engine, "model": t.model,
                "label": engine_label(t.engine) + (f" · {t.model}" if t.model else ""),
                "connected": t.engine in connected}

    from .outcomes import outcome_table_report
    from .savings import shadow_summary

    return {
        "mode": policy.mode,
        "version": policy.version,
        "tiers": [{"tier": t, "label": TIER_LABELS[t],
                   "criteria": policy.classifier.criteria.get(t, ""),
                   "targets": [target(x) for x in policy.targets(t)]} for t in TIERS],
        "floors": [{**f.to_dict(), "describe": f.describe(), "tier_label": TIER_LABELS[f.tier]}
                   for f in policy.floors],
        "floor_preset": policy.floor_preset,
        "presets": _presets(connection, project_id),
        "floor_steps": [{"step": s, "label": STEP_LABELS.get(s, s)} for s in FLOOR_STEPS],
        "headroom": headroom,
        "headroom_rules": [r.to_dict() for r in policy.headroom],
        "headroom_states": [{"state": s, "label": label} for s, label in STATE_LABELS.items()
                            if s != "plenty"],
        "thresholds": dict(policy.thresholds),
        "budget": dict(policy.budget),
        "budget_state": snap.budget_state,
        "classifier": {**policy.classifier.to_dict(), "jev": jev_state(snap.settings or settings),
                       "backends": [{"value": b, "label": BACKEND_LABELS[b]}
                                    for b in CLASSIFIER_BACKENDS]},
        "escalation": [e.to_dict() for e in policy.escalation],
        "sources": sources,
        "pins": {job: {"engine": e, "model": m, "label": engine_label(e) + (f" · {m}" if m else "")}
                 for job, (e, m) in snap.job_pins.items()},
        "week": _week(connection, project_id),
        "shadow": shadow_summary(connection, project_id, days=14),
        "outcomes": outcome_table_report(connection, project_id, days=30),
        "hard_pins": list(policy.hard_pins),
        "frontier_allows_local": policy.frontier_allows_local,
        "use_outcome_priors": policy.use_outcome_priors,
        "fast_stage_shadow": policy.fast_stage_shadow,
    }
