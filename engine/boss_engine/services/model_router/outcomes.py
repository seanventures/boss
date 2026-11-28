"""(e) Outcome learning — join routes with real stage outcomes and feed priors to the router
(BOSSAPP-781: learn from outcomes).

BOSS has richer outcome signals than any external router: authoritative test passes, adversarial
review verdicts, and PR merges. Instead of routing on raw per-call HTTP/CLI `ok` flags (a call
that returned 200 with broken code is not a success), every route is joined with its stage
outcome (`tests_passed`, `review_verdict`, `merged`, `stage_passed`) and list-price cost per
`(task_type, stage, tier)` (and optionally `repo`).

The router reads those empirical pass rates as priors:
  * Route DOWN when a cheaper tier holds pass rate (≥ 85%, within 5 pts of the higher tier,
    with enough samples) — never below a policy floor and never on sensitive work.
  * Route UP when the current tier's pass rate drops below 65% with enough samples.
  * Pass the empirical table summary into the classifier context so Jev sees the priors too.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Mapping, Optional

from sqlalchemy import text

from .tiers import BALANCED, FAST, FAST_SHADOW_STAGES, TIERS, down, rank, step_label, up

log = logging.getLogger("boss.model_router.outcomes")

MIN_PRIOR_SAMPLES = 5
DOWN_PASS_RATE = 0.85
UP_PASS_RATE = 0.65
MAX_PASS_DROP = 0.05


@dataclass(frozen=True)
class OutcomePrior:
    task_type: str
    stage: str
    tier: str
    samples: int
    passed: int
    tests_passed_count: int
    tests_total_count: int
    review_approved_count: int
    review_total_count: int
    merged_count: int
    merged_total_count: int
    avg_list_cost_usd: float
    avg_tokens: float
    repo: Optional[str] = None

    @property
    def pass_rate(self) -> float:
        return round(self.passed / self.samples, 4) if self.samples > 0 else 0.0

    @property
    def tests_pass_rate(self) -> Optional[float]:
        return round(self.tests_passed_count / self.tests_total_count, 4) \
            if self.tests_total_count > 0 else None

    @property
    def review_approval_rate(self) -> Optional[float]:
        return round(self.review_approved_count / self.review_total_count, 4) \
            if self.review_total_count > 0 else None

    @property
    def merge_rate(self) -> Optional[float]:
        return round(self.merged_count / self.merged_total_count, 4) \
            if self.merged_total_count > 0 else None

    def to_dict(self) -> dict[str, Any]:
        return {
            "task_type": self.task_type,
            "stage": self.stage,
            "stage_label": step_label(self.stage),
            "tier": self.tier,
            "repo": self.repo,
            "samples": self.samples,
            "passed": self.passed,
            "pass_rate": self.pass_rate,
            "tests_pass_rate": self.tests_pass_rate,
            "review_approval_rate": self.review_approval_rate,
            "merge_rate": self.merge_rate,
            "avg_list_cost_usd": round(self.avg_list_cost_usd, 4),
            "avg_tokens": round(self.avg_tokens, 1),
        }


@dataclass(frozen=True)
class PriorRecommendation:
    direction: str          # "down" | "up"
    from_tier: str
    to_tier: str
    reason: str
    prior: OutcomePrior


def _stage_passed(stage: str, *, tests_passed: Optional[bool], review_verdict: Optional[str],
                  merged: Optional[bool], terminal_state: Optional[str]) -> bool:
    """Derive a stage-outcome label rather than trusting the per-call `ok` flag."""
    st = (stage or "").strip().lower()
    if st in ("review_code", "security_check", "consistency_check", "check_tests"):
        if review_verdict is not None:
            return review_verdict in ("approved", "survived", "passed", "clean")
    if st in ("implement", "solo", "repair", "write_tests"):
        if tests_passed is not None:
            return bool(tests_passed)
    if merged is True:
        return True
    if tests_passed is not None:
        return bool(tests_passed)
    if review_verdict is not None:
        return review_verdict in ("approved", "survived", "passed", "clean")
    return (terminal_state or "").lower() == "completed"


def record_route_outcome(
    connection: Any,
    *,
    route_id: str,
    project_id: str,
    stage: str,
    tier: str,
    stage_passed: bool,
    unit_id: Optional[str] = None,
    repo: Optional[str] = None,
    task_type: str = "unknown",
    engine: Optional[str] = None,
    model: Optional[str] = None,
    mode: str = "fixed",
    tests_passed: Optional[bool] = None,
    review_verdict: Optional[str] = None,
    merged: Optional[bool] = None,
    escalated: bool = False,
    escalation_signal: Optional[str] = None,
    escalation_strategy: Optional[str] = None,
    escalation_rescued: Optional[bool] = None,
    cost_usd_micros: int = 0,
    list_cost_usd_micros: int = 0,
    tokens_in: int = 0,
    tokens_out: int = 0,
) -> bool:
    """Upsert one stage-outcome record for `route_id`. Best-effort inside a savepoint."""
    if connection is None or not route_id or not project_id:
        return False
    try:
        with connection.begin_nested():
            connection.execute(text("""
                INSERT INTO model_route_outcomes (
                    route_id, project_id, unit_id, repo, task_type, stage, tier, engine, model,
                    mode, tests_passed, review_verdict, merged, stage_passed, escalated,
                    escalation_signal, escalation_strategy, escalation_rescued,
                    cost_usd_micros, list_cost_usd_micros, tokens_in, tokens_out, recorded_at
                )
                VALUES (
                    :rid, :pid, :uid, :repo, :tt, :stage, :tier, :eng, :mdl,
                    :mode, :tp, :rv, :merged, :sp, :esc,
                    :esig, :estrat, :eresc,
                    :cost, :lcost, :tin, :tout, now()
                )
                ON CONFLICT (route_id) DO UPDATE SET
                    repo = COALESCE(EXCLUDED.repo, model_route_outcomes.repo),
                    task_type = CASE WHEN EXCLUDED.task_type <> 'unknown' THEN EXCLUDED.task_type
                                     ELSE model_route_outcomes.task_type END,
                    tests_passed = COALESCE(EXCLUDED.tests_passed, model_route_outcomes.tests_passed),
                    review_verdict = COALESCE(EXCLUDED.review_verdict, model_route_outcomes.review_verdict),
                    merged = COALESCE(EXCLUDED.merged, model_route_outcomes.merged),
                    stage_passed = EXCLUDED.stage_passed,
                    escalation_rescued = COALESCE(EXCLUDED.escalation_rescued,
                                                  model_route_outcomes.escalation_rescued),
                    cost_usd_micros = GREATEST(EXCLUDED.cost_usd_micros,
                                               model_route_outcomes.cost_usd_micros),
                    list_cost_usd_micros = GREATEST(EXCLUDED.list_cost_usd_micros,
                                                    model_route_outcomes.list_cost_usd_micros),
                    tokens_in = GREATEST(EXCLUDED.tokens_in, model_route_outcomes.tokens_in),
                    tokens_out = GREATEST(EXCLUDED.tokens_out, model_route_outcomes.tokens_out),
                    recorded_at = now()
            """), {
                "rid": route_id, "pid": project_id, "uid": unit_id, "repo": repo,
                "tt": (task_type or "unknown")[:60], "stage": stage, "tier": tier or BALANCED,
                "eng": engine, "mdl": model, "mode": mode or "fixed",
                "tp": tests_passed, "rv": review_verdict, "merged": merged,
                "sp": bool(stage_passed), "esc": bool(escalated),
                "esig": escalation_signal, "estrat": escalation_strategy,
                "eresc": escalation_rescued,
                "cost": int(cost_usd_micros or 0), "lcost": int(list_cost_usd_micros or 0),
                "tin": int(tokens_in or 0), "tout": int(tokens_out or 0),
            })
        return True
    except Exception:  # noqa: BLE001 — outcome recording never fails work
        log.warning("model_route_outcomes upsert failed (non-fatal)", exc_info=True)
        return False


def sync_unit_outcomes(
    connection: Any,
    *,
    project_id: str,
    unit_id: str,
    task_type: Optional[str] = None,
    repo: Optional[str] = None,
    tests_passed: Optional[bool] = None,
    review_verdict: Optional[str] = None,
    merged: Optional[bool] = None,
    terminal_state: Optional[str] = None,
) -> int:
    """Join all `model_routes` of `unit_id` with `llm_calls` and stage outcomes into
    `model_route_outcomes`. Returns the number of route outcomes synced."""
    if connection is None or not project_id or not unit_id:
        return 0
    from ..openai_adapter import list_price_cost_usd_micros

    try:
        with connection.begin_nested():
            routes = connection.execute(text("""
                SELECT id, step, tier, engine, model, mode, escalation, escalation_signal,
                       escalation_strategy
                FROM model_routes
                WHERE unit_id = :u AND (project_id = :p OR project_id IS NULL)
                  AND step <> 'connection_proof'
                ORDER BY created_at
            """), {"u": unit_id, "p": project_id}).mappings().all()
            if not routes:
                return 0
            # Pull scorecard facts when caller omitted them.
            if task_type is None or tests_passed is None or review_verdict is None or merged is None:
                sc = connection.execute(text("""
                    SELECT task_kind, tests_passed, tests_total, review_approved, pr_merged,
                           terminal_state
                    FROM run_scorecards
                    WHERE unit_id = :u AND project_id = :p
                    ORDER BY computed_at DESC LIMIT 1
                """), {"u": unit_id, "p": project_id}).mappings().first()
                if sc:
                    if task_type is None and sc.get("task_kind"):
                        task_type = str(sc["task_kind"])
                    if tests_passed is None and sc.get("tests_total"):
                        tests_passed = int(sc.get("tests_passed") or 0) >= int(sc["tests_total"])
                    if review_verdict is None and sc.get("review_approved") is not None:
                        review_verdict = "approved" if sc["review_approved"] else "changes_requested"
                    if merged is None and sc.get("pr_merged") is not None:
                        merged = bool(sc["pr_merged"])
                    if terminal_state is None and sc.get("terminal_state"):
                        terminal_state = str(sc["terminal_state"])
            if repo is None:
                try:
                    spec_repo = connection.execute(text(
                        "SELECT spec->>'repo' FROM code_task_runs WHERE id = :u"),
                        {"u": unit_id}).scalar()
                    if spec_repo:
                        repo = str(spec_repo)
                except Exception:  # noqa: BLE001
                    pass
            calls = connection.execute(text("""
                SELECT route_id, model, prompt_tokens, completion_tokens,
                       cache_read_tokens, cache_write_tokens, cost_usd_micros,
                       COALESCE(included_in_subscription, false) AS included
                FROM llm_calls
                WHERE route_id = ANY(:rids)
            """), {"rids": [r["id"] for r in routes]}).mappings().all()
    except Exception:  # noqa: BLE001
        return 0

    by_route: dict[str, dict[str, int]] = {}
    for c in calls:
        rid = str(c["route_id"])
        agg = by_route.setdefault(rid, {"cost": 0, "list_cost": 0, "tin": 0, "tout": 0})
        pt = int(c["prompt_tokens"] or 0)
        ct = int(c["completion_tokens"] or 0)
        cr = int(c["cache_read_tokens"] or 0)
        cw = int(c["cache_write_tokens"] or 0)
        list_micros, known = list_price_cost_usd_micros({
            "model": c["model"] or "",
            "prompt_tokens": pt, "completion_tokens": ct,
            "cache_read_tokens": cr, "cache_write_tokens": cw,
        })
        recorded = int(c["cost_usd_micros"] or 0)
        agg["list_cost"] += list_micros if (known and (pt + ct + cr + cw) > 0) else recorded
        agg["cost"] += 0 if c["included"] else recorded
        agg["tin"] += pt + cr + cw
        agg["tout"] += ct

    count = 0
    for r in routes:
        rid = str(r["id"])
        stage = str(r["step"] or "")
        passed = _stage_passed(stage, tests_passed=tests_passed, review_verdict=review_verdict,
                               merged=merged, terminal_state=terminal_state)
        esc = bool(r.get("escalation") or r.get("escalation_signal"))
        agg = by_route.get(rid, {"cost": 0, "list_cost": 0, "tin": 0, "tout": 0})
        if record_route_outcome(
            connection,
            route_id=rid, project_id=project_id, unit_id=unit_id, repo=repo,
            task_type=task_type or "unknown", stage=stage,
            tier=str(r.get("tier") or BALANCED), engine=r.get("engine"), model=r.get("model"),
            mode=str(r.get("mode") or "fixed"),
            tests_passed=tests_passed, review_verdict=review_verdict, merged=merged,
            stage_passed=passed, escalated=esc,
            escalation_signal=r.get("escalation_signal"),
            escalation_strategy=r.get("escalation_strategy")
            or ("tier_escalation" if esc else None),
            escalation_rescued=(passed if esc else None),
            cost_usd_micros=agg["cost"], list_cost_usd_micros=agg["list_cost"],
            tokens_in=agg["tin"], tokens_out=agg["tout"],
        ):
            count += 1
    return count


def load_outcome_priors(
    connection: Any,
    project_id: Optional[str],
    *,
    days: int = 90,
    repo: Optional[str] = None,
) -> dict[tuple[str, str, str], OutcomePrior]:
    """Load empirical stage-outcome rates keyed by `(task_type, stage, tier)` (plus a wildcard
    `('*', stage, tier)` aggregate across task types as fallback)."""
    if connection is None or not project_id:
        return {}
    since = datetime.now(timezone.utc) - timedelta(days=int(days))
    where_repo = " AND repo = :repo" if repo else ""
    params: dict[str, Any] = {"p": project_id, "s": since}
    if repo:
        params["repo"] = repo
    try:
        with connection.begin_nested():
            rows = connection.execute(text(f"""
                SELECT task_type, stage, tier,
                       COUNT(*) AS samples,
                       SUM(CASE WHEN stage_passed THEN 1 ELSE 0 END) AS passed,
                       SUM(CASE WHEN tests_passed IS TRUE THEN 1 ELSE 0 END) AS tp_ok,
                       COUNT(tests_passed) AS tp_tot,
                       SUM(CASE WHEN review_verdict IN ('approved', 'survived', 'passed', 'clean')
                                THEN 1 ELSE 0 END) AS rv_ok,
                       COUNT(review_verdict) AS rv_tot,
                       SUM(CASE WHEN merged IS TRUE THEN 1 ELSE 0 END) AS m_ok,
                       COUNT(merged) AS m_tot,
                       COALESCE(AVG(list_cost_usd_micros), 0) AS avg_lcost,
                       COALESCE(AVG(tokens_in + tokens_out), 0) AS avg_tok
                FROM model_route_outcomes
                WHERE project_id = :p AND recorded_at >= :s{where_repo}
                GROUP BY task_type, stage, tier
            """), params).mappings().all()
    except Exception:  # noqa: BLE001
        return {}

    priors: dict[tuple[str, str, str], OutcomePrior] = {}
    by_stage: dict[tuple[str, str], dict[str, float]] = {}
    for r in rows:
        tt = str(r["task_type"] or "unknown")
        st = str(r["stage"] or "")
        tr = str(r["tier"] or BALANCED)
        n = int(r["samples"] or 0)
        if n <= 0 or tr not in TIERS:
            continue
        p = OutcomePrior(
            task_type=tt, stage=st, tier=tr, repo=repo,
            samples=n, passed=int(r["passed"] or 0),
            tests_passed_count=int(r["tp_ok"] or 0), tests_total_count=int(r["tp_tot"] or 0),
            review_approved_count=int(r["rv_ok"] or 0), review_total_count=int(r["rv_tot"] or 0),
            merged_count=int(r["m_ok"] or 0), merged_total_count=int(r["m_tot"] or 0),
            avg_list_cost_usd=float(r["avg_lcost"] or 0) / 1e6,
            avg_tokens=float(r["avg_tok"] or 0),
        )
        priors[(tt, st, tr)] = p
        agg = by_stage.setdefault((st, tr), {
            "n": 0, "passed": 0, "tp_ok": 0, "tp_tot": 0,
            "rv_ok": 0, "rv_tot": 0, "m_ok": 0, "m_tot": 0,
            "cost_sum": 0.0, "tok_sum": 0.0,
        })
        agg["n"] += n
        agg["passed"] += p.passed
        agg["tp_ok"] += p.tests_passed_count
        agg["tp_tot"] += p.tests_total_count
        agg["rv_ok"] += p.review_approved_count
        agg["rv_tot"] += p.review_total_count
        agg["m_ok"] += p.merged_count
        agg["m_tot"] += p.merged_total_count
        agg["cost_sum"] += p.avg_list_cost_usd * n
        agg["tok_sum"] += p.avg_tokens * n

    for (st, tr), a in by_stage.items():
        n = int(a["n"])
        if n > 0:
            priors[("*", st, tr)] = OutcomePrior(
                task_type="*", stage=st, tier=tr, repo=repo,
                samples=n, passed=int(a["passed"]),
                tests_passed_count=int(a["tp_ok"]), tests_total_count=int(a["tp_tot"]),
                review_approved_count=int(a["rv_ok"]), review_total_count=int(a["rv_tot"]),
                merged_count=int(a["m_ok"]), merged_total_count=int(a["m_tot"]),
                avg_list_cost_usd=a["cost_sum"] / n,
                avg_tokens=a["tok_sum"] / n,
            )
    return priors


def _lookup(priors: Mapping[tuple[str, str, str], OutcomePrior],
            task_type: Optional[str], stage: str, tier: str) -> Optional[OutcomePrior]:
    if task_type:
        exact = priors.get((task_type, stage, tier))
        if exact is not None and exact.samples >= MIN_PRIOR_SAMPLES:
            return exact
    return priors.get(("*", stage, tier))


def recommend_from_priors(
    priors: Optional[Mapping[tuple[str, str, str], OutcomePrior]],
    *,
    step: str,
    task_type: Optional[str],
    current_tier: str,
    sensitive: bool = False,
    floor: Optional[str] = None,
    min_samples: int = MIN_PRIOR_SAMPLES,
    down_pass_rate: float = DOWN_PASS_RATE,
    up_pass_rate: float = UP_PASS_RATE,
    max_drop: float = MAX_PASS_DROP,
) -> Optional[PriorRecommendation]:
    """Recommend routing down or up based on empirical stage-outcome pass rates (BOSSAPP-781).

    Invariants:
      * Sensitive work is NEVER routed down.
      * Never routes below `floor`.
      * Route DOWN when a cheaper tier has ≥ `min_samples` and `pass_rate >= down_pass_rate`
        (and within `max_drop` of `current_tier`'s pass rate if `current_tier` has ≥ `min_samples`).
      * Route UP when `current_tier` has ≥ `min_samples` and `pass_rate < up_pass_rate` and a
        higher tier exists (and has not performed worse).
    """
    if not priors or current_tier not in TIERS:
        return None
    cur_prior = _lookup(priors, task_type, step, current_tier)

    # 1. Check route DOWN first (when allowed by sensitivity & floor)
    cheaper = down(current_tier)
    if not sensitive and cheaper != current_tier and (not floor or rank(cheaper) >= rank(floor)):
        low_prior = _lookup(priors, task_type, step, cheaper)
        if low_prior is not None and low_prior.samples >= min_samples \
                and low_prior.pass_rate >= down_pass_rate:
            holds = (cur_prior is None or cur_prior.samples < min_samples
                     or low_prior.pass_rate >= cur_prior.pass_rate - max_drop)
            if holds:
                pct = round(low_prior.pass_rate * 100)
                return PriorRecommendation(
                    direction="down", from_tier=current_tier, to_tier=cheaper,
                    prior=low_prior,
                    reason=(f"Outcome prior: {cheaper} passed {pct}% "
                            f"({low_prior.passed}/{low_prior.samples}) on "
                            f"{low_prior.task_type}/{step} → {cheaper.capitalize()}"),
                )

    # 2. Check route UP when current tier underperforms
    higher = up(current_tier)
    if higher != current_tier and cur_prior is not None and cur_prior.samples >= min_samples \
            and cur_prior.pass_rate < up_pass_rate:
        hi_prior = _lookup(priors, task_type, step, higher)
        if hi_prior is None or hi_prior.samples < min_samples \
                or hi_prior.pass_rate > cur_prior.pass_rate:
            pct = round(cur_prior.pass_rate * 100)
            return PriorRecommendation(
                direction="up", from_tier=current_tier, to_tier=higher,
                prior=cur_prior,
                reason=(f"Outcome prior: {current_tier} passed only {pct}% "
                        f"({cur_prior.passed}/{cur_prior.samples}) on "
                        f"{cur_prior.task_type}/{step} → {higher.capitalize()}"),
            )
    return None


def format_priors_for_classifier(
    priors: Optional[Mapping[tuple[str, str, str], OutcomePrior]],
    *,
    step: str,
    task_type: Optional[str],
) -> str:
    """Short empirical summary for Jev / model classifier context."""
    if not priors:
        return ""
    bits = []
    for tier in TIERS:
        p = _lookup(priors, task_type, step, tier)
        if p is not None and p.samples >= 3:
            bits.append(f"{tier}: {round(p.pass_rate * 100)}% pass ({p.passed}/{p.samples}, "
                        f"avg ${p.avg_list_cost_usd:.3f})")
    if not bits:
        return ""
    return f"Historical stage outcomes for {task_type or 'all'}/{step}: " + "; ".join(bits)


def outcome_table_report(
    connection: Any,
    project_id: str,
    *,
    days: int = 30,
    repo: Optional[str] = None,
) -> dict[str, Any]:
    """Per-stage, per-task-type outcome table + projected savings at equal pass rate (BOSSAPP-781)."""
    priors = load_outcome_priors(connection, project_id, days=days, repo=repo)
    grouped: dict[tuple[str, str], dict[str, OutcomePrior]] = {}
    for (tt, st, tr), p in priors.items():
        if tt == "*":
            continue
        grouped.setdefault((tt, st), {})[tr] = p

    rows: list[dict[str, Any]] = []
    projected_savings_usd = 0.0
    fast_shadow_candidates: list[dict[str, Any]] = []

    for (tt, st), by_tier in sorted(grouped.items()):
        # Determine highest-sample active tier as current reference
        tiers_dict = {t: by_tier[t].to_dict() for t in TIERS if t in by_tier}
        dominant = max(by_tier.values(), key=lambda x: x.samples)
        rec = recommend_from_priors(priors, step=st, task_type=tt, current_tier=dominant.tier)
        saving_usd = 0.0
        if rec and rec.direction == "down" and dominant.tier in by_tier and rec.to_tier in by_tier:
            delta = max(0.0, by_tier[dominant.tier].avg_list_cost_usd
                        - by_tier[rec.to_tier].avg_list_cost_usd)
            saving_usd = round(delta * by_tier[dominant.tier].samples, 4)
            projected_savings_usd += saving_usd
        row = {
            "task_type": tt,
            "stage": st,
            "stage_label": step_label(st),
            "dominant_tier": dominant.tier,
            "tiers": tiers_dict,
            "recommendation": {
                "direction": rec.direction, "from_tier": rec.from_tier, "to_tier": rec.to_tier,
                "reason": rec.reason,
            } if rec else None,
            "projected_saving_usd": round(saving_usd, 2),
        }
        rows.append(row)
        if st in FAST_SHADOW_STAGES and FAST in by_tier:
            fast_p = by_tier[FAST]
            bal_p = by_tier.get(BALANCED)
            eq_pass = (bal_p is None or fast_p.pass_rate >= bal_p.pass_rate - MAX_PASS_DROP)
            fast_shadow_candidates.append({
                "task_type": tt, "stage": st, "stage_label": step_label(st),
                "fast_samples": fast_p.samples, "fast_pass_rate": fast_p.pass_rate,
                "balanced_pass_rate": bal_p.pass_rate if bal_p else None,
                "equal_pass_rate": eq_pass and fast_p.pass_rate >= DOWN_PASS_RATE,
                "projected_saving_usd": round(
                    max(0.0, (bal_p.avg_list_cost_usd if bal_p else fast_p.avg_list_cost_usd * 3)
                        - fast_p.avg_list_cost_usd) * (bal_p.samples if bal_p else fast_p.samples),
                    2),
            })

    return {
        "project_id": project_id,
        "days": int(days),
        "repo": repo,
        "rows": rows,
        "projected_saving_usd": round(projected_savings_usd, 2),
        "fast_shadow_candidates": fast_shadow_candidates,
    }
