"""Cost truth, shadow counterfactuals, escalation recovery, and a quotable per-project savings
report (BOSSAPP-779, BOSSAPP-780, BOSSAPP-781, BOSSAPP-782).

Every call is recomputed at list price from its actual token split (`prompt_tokens`,
`completion_tokens`, `cache_read_tokens`, `cache_write_tokens`) using `openai_adapter.price_for_model`
and compared against an all-frontier baseline priced on the SAME tokens with the SAME cache
multipliers. Metered cash dollars saved are strictly separated from subscription capacity saved.
"""
from __future__ import annotations

import html
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from sqlalchemy import text

from ..openai_adapter import CACHE_READ_MULT, CACHE_WRITE_MULT, list_price_cost_usd_micros, price_for_model
from .outcomes import outcome_table_report
from .policy import model_tier
from .tiers import BALANCED, FAST, FRONTIER, TIER_LABELS, TIERS

#: All-Frontier reference price (USD per 1M input / output tokens — current Opus class).
FRONTIER_BASELINE_MODEL = "claude-opus-4-6"
FRONTIER_BASELINE_PRICE = (5.00, 25.00)

#: Reference prices per tier when a counterfactual shadow decision names no specific priced model.
TIER_REFERENCE_PRICE: dict[str, tuple[float, float]] = {
    FAST: (1.00, 5.00),
    BALANCED: (3.00, 15.00),
    FRONTIER: FRONTIER_BASELINE_PRICE,
}


def _tokens_cost_usd_micros(
    *,
    prompt_tokens: int,
    completion_tokens: int,
    cache_read_tokens: int,
    cache_write_tokens: int,
    price: tuple[float, float],
) -> int:
    inp, out = price
    return int(round(
        prompt_tokens * inp
        + completion_tokens * out
        + cache_read_tokens * inp * CACHE_READ_MULT
        + cache_write_tokens * inp * CACHE_WRITE_MULT
    ))


def _price_for_target(model: Optional[str], tier: str) -> tuple[float, float]:
    if model:
        inp, out, known = price_for_model(str(model))
        if known:
            return (inp, out)
    return TIER_REFERENCE_PRICE.get(tier, FRONTIER_BASELINE_PRICE)


def _call_tier(route_tier: Optional[str], engine: Optional[str], model: Optional[str]) -> str:
    if route_tier in TIERS:
        # Correct historical mislabelled Codex/gpt-6 frontier reviews too (BOSSAPP-782)
        inferred = model_tier(engine or "", model)
        if inferred == FRONTIER and route_tier != FRONTIER:
            return FRONTIER
        return str(route_tier)
    inferred = model_tier(engine or "", model)
    return inferred if inferred in TIERS else BALANCED


def shadow_summary(
    connection: Any,
    project_id: str,
    *,
    days: int = 14,
    now: Optional[datetime] = None,
) -> dict[str, Any]:
    """Counterfactual Smart vs Fixed comparison over shadow-logged routes (BOSSAPP-779)."""
    now = now or datetime.now(timezone.utc)
    since = now - timedelta(days=int(days))
    empty = {
        "days": int(days),
        "routes": 0,
        "agreed": 0,
        "agreement_rate": None,
        "actual_tiers": {t: 0 for t in TIERS},
        "shadow_tiers": {t: 0 for t in TIERS},
        "actual_list_usd": 0.0,
        "shadow_projected_usd": 0.0,
        "projected_saving_usd": 0.0,
        "projected_saving_pct": None,
        "by_stage": [],
    }
    if connection is None or not project_id:
        return empty
    try:
        with connection.begin_nested():
            rows = connection.execute(text("""
                SELECT r.id, r.step, r.tier AS actual_tier, r.engine AS actual_engine,
                       r.model AS actual_model, r.shadow_tier, r.shadow_engine, r.shadow_model,
                       COALESCE(SUM(c.prompt_tokens), 0) AS pt,
                       COALESCE(SUM(c.completion_tokens), 0) AS ct,
                       COALESCE(SUM(c.cache_read_tokens), 0) AS cr,
                       COALESCE(SUM(c.cache_write_tokens), 0) AS cw
                FROM model_routes r
                LEFT JOIN llm_calls c ON c.route_id = r.id
                WHERE r.project_id = :p AND r.created_at >= :s
                  AND r.shadow_tier IS NOT NULL
                  AND r.step <> 'connection_proof'
                GROUP BY r.id, r.step, r.tier, r.engine, r.model,
                         r.shadow_tier, r.shadow_engine, r.shadow_model
            """), {"p": project_id, "s": since}).mappings().all()
    except Exception:  # noqa: BLE001
        return empty

    if not rows:
        return empty

    actual_tiers = {t: 0 for t in TIERS}
    shadow_tiers = {t: 0 for t in TIERS}
    agreed = 0
    actual_micros = 0
    shadow_micros = 0
    stage_map: dict[str, dict[str, Any]] = {}

    for r in rows:
        at = _call_tier(r["actual_tier"], r["actual_engine"], r["actual_model"])
        st = str(r["shadow_tier"]) if r["shadow_tier"] in TIERS else BALANCED
        actual_tiers[at] += 1
        shadow_tiers[st] += 1
        if at == st:
            agreed += 1
        pt, ct = int(r["pt"] or 0), int(r["ct"] or 0)
        cr, cw = int(r["cr"] or 0), int(r["cw"] or 0)
        a_cost = _tokens_cost_usd_micros(
            prompt_tokens=pt, completion_tokens=ct, cache_read_tokens=cr, cache_write_tokens=cw,
            price=_price_for_target(r["actual_model"], at))
        s_cost = _tokens_cost_usd_micros(
            prompt_tokens=pt, completion_tokens=ct, cache_read_tokens=cr, cache_write_tokens=cw,
            price=_price_for_target(r["shadow_model"], st))
        actual_micros += a_cost
        shadow_micros += s_cost
        step = str(r["step"] or "other")
        entry = stage_map.setdefault(step, {
            "stage": step, "routes": 0, "actual_tiers": {t: 0 for t in TIERS},
            "shadow_tiers": {t: 0 for t in TIERS}, "actual_micros": 0, "shadow_micros": 0,
        })
        entry["routes"] += 1
        entry["actual_tiers"][at] += 1
        entry["shadow_tiers"][st] += 1
        entry["actual_micros"] += a_cost
        entry["shadow_micros"] += s_cost

    n = len(rows)
    actual_usd = actual_micros / 1e6
    shadow_usd = shadow_micros / 1e6
    saved_usd = max(0.0, actual_usd - shadow_usd)
    return {
        "days": int(days),
        "routes": n,
        "agreed": agreed,
        "agreement_rate": round(agreed / n, 4) if n else None,
        "actual_tiers": actual_tiers,
        "shadow_tiers": shadow_tiers,
        "actual_list_usd": round(actual_usd, 4),
        "shadow_projected_usd": round(shadow_usd, 4),
        "projected_saving_usd": round(saved_usd, 2),
        "projected_saving_pct": round((actual_usd - shadow_usd) / actual_usd, 4)
        if actual_usd > 0 else None,
        "by_stage": [
            {
                "stage": v["stage"], "routes": v["routes"],
                "actual_tiers": v["actual_tiers"], "shadow_tiers": v["shadow_tiers"],
                "actual_list_usd": round(v["actual_micros"] / 1e6, 4),
                "shadow_projected_usd": round(v["shadow_micros"] / 1e6, 4),
                "projected_saving_usd": round(
                    max(0.0, v["actual_micros"] - v["shadow_micros"]) / 1e6, 4),
            }
            for v in sorted(stage_map.values(), key=lambda x: (-x["routes"], x["stage"]))
        ],
    }


def escalation_recovery_summary(
    connection: Any,
    project_id: str,
    *,
    days: int = 30,
    now: Optional[datetime] = None,
) -> dict[str, Any]:
    """Escalation recovery rate and cost per rescued build per escalation route (BOSSAPP-780)."""
    now = now or datetime.now(timezone.utc)
    since = now - timedelta(days=int(days))
    empty = {
        "total": 0,
        "rescued": 0,
        "recovery_rate": None,
        "total_cost_usd": 0.0,
        "cost_per_rescued_build_usd": None,
        "by_route": [],
    }
    if connection is None or not project_id:
        return empty
    try:
        with connection.begin_nested():
            rows = connection.execute(text("""
                SELECT COALESCE(escalation_signal, 'unknown') AS signal,
                       COALESCE(escalation_strategy, 'tier_escalation') AS strategy,
                       tier, COALESCE(model, engine, 'default') AS target,
                       COUNT(*) AS attempts,
                       SUM(CASE WHEN escalation_rescued IS TRUE THEN 1 ELSE 0 END) AS rescued,
                       COALESCE(SUM(list_cost_usd_micros), 0) AS list_micros
                FROM model_route_outcomes
                WHERE project_id = :p AND recorded_at >= :s AND escalated IS TRUE
                GROUP BY 1, 2, 3, 4
                ORDER BY attempts DESC, signal
            """), {"p": project_id, "s": since}).mappings().all()
    except Exception:  # noqa: BLE001
        return empty

    total = 0
    rescued = 0
    total_micros = 0
    by_route: list[dict[str, Any]] = []
    for r in rows:
        att = int(r["attempts"] or 0)
        res = int(r["rescued"] or 0)
        mic = int(r["list_micros"] or 0)
        total += att
        rescued += res
        total_micros += mic
        cost_usd = mic / 1e6
        by_route.append({
            "signal": str(r["signal"]),
            "strategy": str(r["strategy"]),
            "tier": str(r["tier"]),
            "target": str(r["target"]),
            "attempts": att,
            "rescued": res,
            "recovery_rate": round(res / att, 4) if att > 0 else None,
            "total_cost_usd": round(cost_usd, 4),
            "cost_per_rescued_build_usd": round(cost_usd / res, 4) if res > 0 else None,
        })
    total_usd = total_micros / 1e6
    return {
        "total": total,
        "rescued": rescued,
        "recovery_rate": round(rescued / total, 4) if total > 0 else None,
        "total_cost_usd": round(total_usd, 4),
        "cost_per_rescued_build_usd": round(total_usd / rescued, 4) if rescued > 0 else None,
        "by_route": by_route,
    }


def savings_report(
    connection: Any,
    project_id: str,
    *,
    days: int = 30,
    now: Optional[datetime] = None,
) -> dict[str, Any]:
    """Per-project routing cost-truth & savings report against an all-frontier baseline
    (BOSSAPP-782). Recomputes every call at list price from tokens so drift vs list-price
    recomputation is explicitly checked (≤ 5% target)."""
    now = now or datetime.now(timezone.utc)
    since = now - timedelta(days=int(days))

    by_tier: dict[str, dict[str, Any]] = {
        t: {"tier": t, "label": TIER_LABELS[t], "calls": 0, "tokens": 0,
            "actual_list_micros": 0, "baseline_micros": 0, "saved_micros": 0}
        for t in TIERS
    }
    total_calls = 0
    routed_down_calls = 0
    total_tokens = 0
    routed_down_tokens = 0
    metered_actual_micros = 0
    metered_baseline_micros = 0
    sub_actual_micros = 0
    sub_baseline_micros = 0
    recorded_micros_sum = 0
    recomputed_micros_sum = 0

    try:
        with connection.begin_nested():
            call_rows = connection.execute(text("""
                SELECT c.engine, c.model, c.stage,
                       COALESCE(c.prompt_tokens, 0) AS pt,
                       COALESCE(c.completion_tokens, 0) AS ct,
                       COALESCE(c.cache_read_tokens, 0) AS cr,
                       COALESCE(c.cache_write_tokens, 0) AS cw,
                       COALESCE(c.cost_usd_micros, 0) AS recorded_micros,
                       COALESCE(c.included_in_subscription, false) AS included,
                       r.tier AS route_tier, r.mode AS route_mode
                FROM llm_calls c
                LEFT JOIN model_routes r ON r.id = c.route_id
                WHERE c.project_id = :p AND c.created_at >= :s
                  AND COALESCE(c.stage, '') <> 'connection_proof'
            """), {"p": project_id, "s": since}).mappings().all()
    except Exception:  # noqa: BLE001
        call_rows = []

    for c in call_rows:
        pt, ct = int(c["pt"] or 0), int(c["ct"] or 0)
        cr, cw = int(c["cr"] or 0), int(c["cw"] or 0)
        toks = pt + ct + cr + cw
        tier = _call_tier(c["route_tier"], c["engine"], c["model"])
        list_micros, known = list_price_cost_usd_micros({
            "model": c["model"] or "",
            "prompt_tokens": pt, "completion_tokens": ct,
            "cache_read_tokens": cr, "cache_write_tokens": cw,
        })
        recorded = int(c["recorded_micros"] or 0)
        # Canonical list-price cost: recomputed from tokens when tokens > 0 & model is priced,
        # else falls back to recorded cost (e.g. a synthetic or unpriced gateway receipt).
        actual_list = list_micros if (toks > 0 and known) else (recorded or list_micros)
        baseline = (
            _tokens_cost_usd_micros(
                prompt_tokens=pt, completion_tokens=ct,
                cache_read_tokens=cr, cache_write_tokens=cw,
                price=FRONTIER_BASELINE_PRICE,
            ) if toks > 0 else actual_list
        )
        # Ensure Frontier-tier calls priced at higher than baseline (e.g. Opus 4.1 $15/$75) do not
        # show negative baseline comparison; baseline is max(frontier_ref, actual) for frontier tier.
        if tier == FRONTIER and baseline < actual_list:
            baseline = actual_list

        total_calls += 1
        total_tokens += toks
        if tier in (FAST, BALANCED):
            routed_down_calls += 1
            routed_down_tokens += toks
        recorded_micros_sum += recorded
        recomputed_micros_sum += actual_list

        if c["included"]:
            sub_actual_micros += actual_list
            sub_baseline_micros += baseline
        else:
            metered_actual_micros += actual_list
            metered_baseline_micros += baseline

        bt = by_tier[tier]
        bt["calls"] += 1
        bt["tokens"] += toks
        bt["actual_list_micros"] += actual_list
        bt["baseline_micros"] += baseline
        bt["saved_micros"] += max(0, baseline - actual_list)

    # Merged changes & review verdicts by tier
    merged_changes = 0
    review_by_tier: dict[str, dict[str, Any]] = {
        t: {"tier": t, "label": TIER_LABELS[t], "approved": 0, "changes_requested": 0,
            "total": 0, "approval_rate": None}
        for t in TIERS
    }
    try:
        with connection.begin_nested():
            merged_changes = int(connection.execute(text("""
                SELECT COUNT(*) FROM run_scorecards
                WHERE project_id = :p AND ended_at >= :s
                  AND unit_kind IN ('build', 'quick_change')
                  AND pr_merged IS TRUE
            """), {"p": project_id, "s": since}).scalar() or 0)
            rev_rows = connection.execute(text("""
                SELECT COALESCE(r.tier, s.route_tier, 'balanced') AS tier,
                       r.engine, r.model, s.review_approved
                FROM run_scorecards s
                LEFT JOIN LATERAL (
                    SELECT tier, engine, model FROM model_routes
                    WHERE unit_id = s.unit_id AND step IN ('review_code', 'security_check',
                                                           'consistency_check')
                    ORDER BY created_at DESC LIMIT 1
                ) r ON TRUE
                WHERE s.project_id = :p AND s.ended_at >= :s
                  AND s.review_approved IS NOT NULL
            """), {"p": project_id, "s": since}).mappings().all()
            for rv in rev_rows:
                t = _call_tier(rv["tier"], rv["engine"], rv["model"])
                bucket = review_by_tier[t]
                bucket["total"] += 1
                if rv["review_approved"]:
                    bucket["approved"] += 1
                else:
                    bucket["changes_requested"] += 1
    except Exception:  # noqa: BLE001
        pass

    for bucket in review_by_tier.values():
        if bucket["total"] > 0:
            bucket["approval_rate"] = round(bucket["approved"] / bucket["total"], 4)

    total_actual_usd = (metered_actual_micros + sub_actual_micros) / 1e6
    total_baseline_usd = (metered_baseline_micros + sub_baseline_micros) / 1e6
    total_saved_usd = max(0.0, total_baseline_usd - total_actual_usd)
    metered_saved_usd = max(0.0, (metered_baseline_micros - metered_actual_micros) / 1e6)
    sub_saved_usd = max(0.0, (sub_baseline_micros - sub_actual_micros) / 1e6)

    recorded_usd = recorded_micros_sum / 1e6
    recomputed_usd = recomputed_micros_sum / 1e6
    drift_pct = (
        round(abs(recorded_usd - recomputed_usd) / recomputed_usd, 4)
        if recomputed_usd > 0 else 0.0
    )

    esc = escalation_recovery_summary(connection, project_id, days=days, now=now)
    esc_rate = round(esc["total"] / total_calls, 4) if total_calls > 0 else 0.0
    esc["rate"] = esc_rate

    return {
        "project_id": project_id,
        "days": int(days),
        "generated_at": now.isoformat(),
        "baseline_model": FRONTIER_BASELINE_MODEL,
        "baseline_all_frontier_usd": round(total_baseline_usd, 2),
        "actual_list_value_usd": round(total_actual_usd, 2),
        "total_saved_usd": round(total_saved_usd, 2),
        "total_savings_pct": round(total_saved_usd / total_baseline_usd, 4)
        if total_baseline_usd > 0 else 0.0,
        # Strictly separated: real metered cash vs flat-rate subscription capacity
        "metered": {
            "actual_usd": round(metered_actual_micros / 1e6, 2),
            "baseline_usd": round(metered_baseline_micros / 1e6, 2),
            "dollars_saved_usd": round(metered_saved_usd, 2),
        },
        "subscription": {
            "actual_list_usd": round(sub_actual_micros / 1e6, 2),
            "baseline_list_usd": round(sub_baseline_micros / 1e6, 2),
            "capacity_saved_usd": round(sub_saved_usd, 2),
        },
        "total_calls": total_calls,
        "routed_down_calls": routed_down_calls,
        "pct_calls_routed_down": round(routed_down_calls / total_calls, 4)
        if total_calls > 0 else 0.0,
        "total_tokens": total_tokens,
        "routed_down_tokens": routed_down_tokens,
        "pct_tokens_routed_down": round(routed_down_tokens / total_tokens, 4)
        if total_tokens > 0 else 0.0,
        "merged_changes": merged_changes,
        "cost_per_merged_change_usd": round(total_actual_usd / merged_changes, 2)
        if merged_changes > 0 else None,
        "metered_cost_per_merged_change_usd": round(
            (metered_actual_micros / 1e6) / merged_changes, 2)
        if merged_changes > 0 else None,
        "baseline_cost_per_merged_change_usd": round(total_baseline_usd / merged_changes, 2)
        if merged_changes > 0 else None,
        "escalations": esc,
        "review_verdicts_by_tier": [review_by_tier[t] for t in TIERS],
        "by_tier": [
            {
                "tier": t, "label": by_tier[t]["label"],
                "calls": by_tier[t]["calls"], "tokens": by_tier[t]["tokens"],
                "actual_list_usd": round(by_tier[t]["actual_list_micros"] / 1e6, 2),
                "baseline_usd": round(by_tier[t]["baseline_micros"] / 1e6, 2),
                "saved_usd": round(by_tier[t]["saved_micros"] / 1e6, 2),
            }
            for t in TIERS
        ],
        "recomputation_check": {
            "recorded_usd": round(recorded_usd, 4),
            "recomputed_list_usd": round(recomputed_usd, 4),
            "drift_pct": drift_pct,
            "within_5pct": drift_pct <= 0.05,
        },
        "shadow": shadow_summary(connection, project_id, days=days, now=now),
        "outcomes": outcome_table_report(connection, project_id, days=days),
    }


def render_one_page_report(
    report: dict[str, Any],
    *,
    project_name: str = "",
    format: str = "html",
) -> str:
    """One-page customer/sales export of the per-project routing savings report (BOSSAPP-782).
    Self-contained HTML (zero remote scripts or fonts, print-ready) or Markdown."""
    title = project_name or str(report.get("project_id") or "BOSS Project")
    days = int(report.get("days") or 30)
    pct_saved = round(float(report.get("total_savings_pct") or 0) * 100, 1)
    pct_calls = round(float(report.get("pct_calls_routed_down") or 0) * 100, 1)
    pct_tokens = round(float(report.get("pct_tokens_routed_down") or 0) * 100, 1)
    metered = report.get("metered") or {}
    sub = report.get("subscription") or {}
    esc = report.get("escalations") or {}
    recomp = report.get("recomputation_check") or {}
    cost_merged = (f"${report['cost_per_merged_change_usd']:.2f}"
                   if report.get("cost_per_merged_change_usd") is not None else "—")
    base_merged = (f"${report['baseline_cost_per_merged_change_usd']:.2f}"
                   if report.get("baseline_cost_per_merged_change_usd") is not None else "—")
    esc_rec = (f"{round(float(esc['recovery_rate']) * 100, 1)}%"
               if esc.get("recovery_rate") is not None else "—")

    if format.lower() in ("md", "markdown"):
        lines = [
            f"# BOSS Model Routing Savings Report — {title} ({days}-Day Window)",
            "",
            f"- **Total savings vs all-Frontier baseline ({report.get('baseline_model')}):** "
            f"**${report.get('total_saved_usd', 0):.2f}** ({pct_saved}%)",
            f"- **Metered dollars saved:** ${metered.get('dollars_saved_usd', 0):.2f} "
            f"(actual ${metered.get('actual_usd', 0):.2f} vs baseline ${metered.get('baseline_usd', 0):.2f})",
            f"- **Subscription capacity saved (list value):** ${sub.get('capacity_saved_usd', 0):.2f} "
            f"(actual ${sub.get('actual_list_usd', 0):.2f} vs baseline ${sub.get('baseline_list_usd', 0):.2f})",
            f"- **Routed below Frontier:** {pct_calls}% of calls ({report.get('routed_down_calls', 0)}/{report.get('total_calls', 0)}), "
            f"{pct_tokens}% of tokens",
            f"- **Cost per merged change:** {cost_merged} (all-Frontier baseline: {base_merged}, "
            f"{report.get('merged_changes', 0)} merged PRs)",
            f"- **Escalations:** {esc.get('total', 0)} ({round(float(esc.get('rate') or 0) * 100, 1)}% of calls), "
            f"recovery rate: {esc_rec}",
            f"- **List-price audit check:** drift {round(float(recomp.get('drift_pct') or 0) * 100, 2)}% "
            f"({'within 5%' if recomp.get('within_5pct', True) else 'above 5%'})",
            "",
            "## Breakdown by Tier",
            "| Tier | Calls | Tokens | Actual (List $) | All-Frontier Baseline | Saved |",
            "|---|---:|---:|---:|---:|---:|",
        ]
        for row in report.get("by_tier") or []:
            lines.append(
                f"| {row['label']} | {row['calls']:,} | {row['tokens']:,} | "
                f"${row['actual_list_usd']:.2f} | ${row['baseline_usd']:.2f} | "
                f"${row['saved_usd']:.2f} |"
            )
        lines.extend([
            "",
            "## Review Verdicts by Tier",
            "| Tier | Approved | Changes Requested | Approval Rate |",
            "|---|---:|---:|---:|",
        ])
        for rv in report.get("review_verdicts_by_tier") or []:
            rate = f"{round(rv['approval_rate'] * 100, 1)}%" if rv.get("approval_rate") is not None else "—"
            lines.append(
                f"| {rv['label']} | {rv['approved']} | {rv['changes_requested']} | {rate} |"
            )
        return "\n".join(lines) + "\n"

    e = html.escape
    tier_rows = "".join(
        f"<tr><td>{e(str(r['label']))}</td><td>{r['calls']:,}</td><td>{r['tokens']:,}</td>"
        f"<td>${r['actual_list_usd']:.2f}</td><td>${r['baseline_usd']:.2f}</td>"
        f"<td><strong>${r['saved_usd']:.2f}</strong></td></tr>"
        for r in (report.get("by_tier") or [])
    )
    def _fmt_rate(val: Any) -> str:
        return f"{round(float(val) * 100, 1)}%" if val is not None else "—"

    review_rows = "".join(
        f"<tr><td>{e(str(rv['label']))}</td><td>{rv['approved']}</td>"
        f"<td>{rv['changes_requested']}</td>"
        f"<td>{_fmt_rate(rv.get('approval_rate'))}</td></tr>"
        for rv in (report.get("review_verdicts_by_tier") or [])
    )
    esc_cost_cell = (
        f"${esc['cost_per_rescued_build_usd']:.2f}"
        if esc.get("cost_per_rescued_build_usd") is not None else "—"
    )
    drift_pct = round(float(recomp.get("drift_pct") or 0) * 100, 2)
    drift_badge = "PASS (≤ 5%)" if recomp.get("within_5pct", True) else "REVIEW (> 5%)"
    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8" />
<title>BOSS Model Routing Savings Report — {e(title)}</title>
<style>
  * {{ box-sizing: border-box; }}
  body {{ font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
         color: #111827; margin: 0; padding: 28px; background: #fff; line-height: 1.4; }}
  .sheet {{ max-width: 820px; margin: 0 auto; }}
  header {{ display: flex; justify-content: space-between; align-items: baseline;
            border-bottom: 2px solid #111827; padding-bottom: 10px; margin-bottom: 18px; }}
  h1 {{ font-size: 20px; margin: 0; }}
  .meta {{ font-size: 12px; color: #4b5563; }}
  .kpis {{ display: grid; grid-template-columns: repeat(4, 1fr); gap: 12px; margin-bottom: 18px; }}
  .kpi {{ border: 1px solid #e5e7eb; border-radius: 6px; padding: 10px 12px; }}
  .kpi .label {{ font-size: 11px; text-transform: uppercase; color: #6b7280; letter-spacing: .04em; }}
  .kpi .val {{ font-size: 20px; font-weight: 700; margin-top: 2px; }}
  .kpi .sub {{ font-size: 11px; color: #4b5563; margin-top: 2px; }}
  h2 {{ font-size: 13px; text-transform: uppercase; letter-spacing: .05em; color: #374151;
        margin: 18px 0 8px; }}
  table {{ width: 100%; border-collapse: collapse; font-size: 12.5px; }}
  th, td {{ text-align: left; padding: 6px 8px; border-bottom: 1px solid #e5e7eb; }}
  th {{ font-weight: 600; color: #374151; background: #f9fafb; }}
  td:not(:first-child), th:not(:first-child) {{ text-align: right; }}
  .split {{ display: grid; grid-template-columns: 1fr 1fr; gap: 16px; }}
  footer {{ margin-top: 18px; padding-top: 8px; border-top: 1px solid #e5e7eb;
            font-size: 11px; color: #6b7280; display: flex; justify-content: space-between; }}
  @media print {{ body {{ padding: 0; }} .sheet {{ max-width: 100%; }} }}
</style>
</head>
<body>
<div class="sheet">
  <header>
    <h1>BOSS Model Routing Savings — {e(title)}</h1>
    <div class="meta">Last {days} days · Baseline: {e(str(report.get('baseline_model') or FRONTIER_BASELINE_MODEL))}</div>
  </header>
  <div class="kpis">
    <div class="kpi">
      <div class="label">Total Savings</div>
      <div class="val">${report.get('total_saved_usd', 0):.2f} ({pct_saved}%)</div>
      <div class="sub">vs ${report.get('baseline_all_frontier_usd', 0):.2f} all-Frontier</div>
    </div>
    <div class="kpi">
      <div class="label">Metered vs Subscription</div>
      <div class="val">${metered.get('dollars_saved_usd', 0):.2f} cash</div>
      <div class="sub">${sub.get('capacity_saved_usd', 0):.2f} subscription capacity</div>
    </div>
    <div class="kpi">
      <div class="label">Routed Below Frontier</div>
      <div class="val">{pct_calls}% calls</div>
      <div class="sub">{pct_tokens}% of {int(report.get('total_tokens') or 0):,} tokens</div>
    </div>
    <div class="kpi">
      <div class="label">Cost / Merged Change</div>
      <div class="val">{e(cost_merged)}</div>
      <div class="sub">Baseline {e(base_merged)} · {int(report.get('merged_changes') or 0)} merged</div>
    </div>
  </div>
  <h2>Cost &amp; Volume by Tier (List-Price Recomputation)</h2>
  <table>
    <thead><tr><th>Tier</th><th>Calls</th><th>Tokens</th><th>Actual (List)</th><th>All-Frontier</th><th>Saved</th></tr></thead>
    <tbody>{tier_rows}</tbody>
  </table>
  <div class="split">
    <div>
      <h2>Review Verdicts by Tier</h2>
      <table>
        <thead><tr><th>Tier</th><th>Approved</th><th>Changes Req.</th><th>Rate</th></tr></thead>
        <tbody>{review_rows}</tbody>
      </table>
    </div>
    <div>
      <h2>Escalation Recovery</h2>
      <table>
        <thead><tr><th>Metric</th><th>Value</th></tr></thead>
        <tbody>
          <tr><td>Escalated Calls</td><td>{int(esc.get('total') or 0)} ({round(float(esc.get('rate') or 0) * 100, 1)}%)</td></tr>
          <tr><td>Rescued Builds / Stages</td><td>{int(esc.get('rescued') or 0)} ({e(esc_rec)})</td></tr>
          <tr><td>Cost / Rescued Build</td><td>{e(esc_cost_cell)}</td></tr>
          <tr><td>List-Price Audit Drift</td><td>{drift_pct}% · {e(drift_badge)}</td></tr>
        </tbody>
      </table>
    </div>
  </div>
  <footer>
    <span>Generated by BOSS Model Router ({e(str(report.get('generated_at') or '')[:19])}Z)</span>
    <span>Metered cash spend and included subscription list value reported separately.</span>
  </footer>
</div>
</body>
</html>
"""
