"""A build's money, under the project's caps — reserve, check at each stage, settle, release.

WHY THIS EXISTS
---------------
On Community the code-task pipeline ran on the unmetered budget port (defaults.NoopBudgetPort):
a build's model calls were recorded in `llm_calls` and never reserved or settled a cost receipt,
so no daily or monthly cap could stop a build. Govern said "Builds $284.01 not capped" while the
receipts the caps count held $8.36 (boss-dogfood, October 2026). Chat, meetings and connection
proofs were already governed by the same caps (BudgetGovernor); builds were the one hole.

Every edition with a database now runs builds on the metered port (bootstrap_ports), and every
build path asks this module — so the reservation, the stage-boundary check and the settlement
are one rule, not one per call site:

  * `reservation_estimate` — what a build reserves: the code-task estimate at its MARGINAL cost
    (included_spend.routed_reservation_estimate: $0 on a subscription or local model, a gate
    rather than a hold; the full estimate on an API key).
  * `unsettled_money` — the build's money so far: its `llm_calls` rows that are money
    (included_spend.LLM_MONEY_SQL — a subscription or local call is $0, its list price kept on
    the row as the included estimate), less what an earlier settle of the same run already
    receipted under ``code_task:<run_id>``. One run, one receipt key, so Govern and get_costs
    (included_spend.ledger_spend) never count a call twice.
  * `settle_or_release` — every exit (done, failed, cancelled, stopped by the budget) settles
    that money on the run's reservation, or releases it when there is none.
  * `cap_reached` — the stage-boundary check: the money settled this period plus what running
    builds have spent and not yet settled, against each cap. A build that crosses a cap stops
    at the next stage boundary — never mid-write — with BOSS's usual cap words.
"""
from __future__ import annotations

import logging
from typing import Any, Optional

from .budget import BUILD_CATEGORY
from .included_spend import LLM_MONEY_SQL

logger = logging.getLogger(__name__)

#: The budget category every build reservation and receipt carries.
CATEGORY = BUILD_CATEGORY

#: What a delivery-only step reserves (push + pull request after a build's gates passed, a
#: waiver, a checks-failing PR): no agent runs, so a small envelope for the PR step's own call.
DELIVERY_ESTIMATE_USD_MICROS = 200_000

#: How long a build reservation lives between stage boundaries. Each boundary renews it
#: (`renew`), so a build that runs for hours keeps its hold; one that died lets it lapse.
RESERVATION_MINUTES = 45


def receipt_key(run_id: str) -> str:
    """The `skill_or_model_id` a build's cost receipt is settled under (ledger_spend reads it)."""
    return f"{CATEGORY}:{run_id}"


def reservation_estimate(connection: Any, settings: Any, project_id: str,
                         amount_usd_micros: Optional[int] = None) -> int:
    """What a build reserves before it spends: the code-task estimate (or `amount_usd_micros`)
    at its marginal cost on the project's routed model — $0 on included capacity."""
    from .included_spend import routed_reservation_estimate

    if amount_usd_micros is not None:
        return routed_reservation_estimate(connection, settings, project_id,
                                           int(amount_usd_micros))
    # A WHOLE BUILD reserves what a build really costs (build_money_budget_micros), not $2 — but
    # never more than the tightest cap still allows when it allows something: a build that the
    # cap can partly fund starts, its sessions are held under that headroom (session_ceiling_usd)
    # and it stops at the boundary where the cap is reached. A cap with no room still refuses.
    amount = routed_reservation_estimate(connection, settings, project_id,
                                         build_money_budget_micros(settings))
    if amount > 0:
        try:
            from .budget import BudgetGovernor

            if hasattr(connection, "begin_nested"):
                with connection.begin_nested():   # a failed read never poisons the caller's tx
                    room = money_headroom(BudgetGovernor(settings), connection, project_id)
            else:
                room = money_headroom(BudgetGovernor(settings), connection, project_id)
        except Exception:  # noqa: BLE001 — an unreadable cap reserves the full estimate
            room = None
        if room is not None and room > 0:
            amount = min(amount, room)
    return amount


#: A build's model calls, each with its money and whether it was made while the build was
#: metered: at or after `finalize_ctx.metered_since`, stamped by the build's first reservation
#: (`bind`). A build from before builds were metered has no stamp — all its calls are its own —
#: but once such a build is resumed or redelivered, only the calls from then on settle: money
#: spent while no cap counted builds stays "not under cap" (ledger_spend) instead of landing on
#: today's cap at once. Filtered by `{where}` (over llm_calls `l`).
CALLS_SQL = (
    f"SELECT l.code_task_run_id AS r, {LLM_MONEY_SQL} AS money, "
    "       l.created_at >= COALESCE(CAST(cr.finalize_ctx->>'metered_since' AS timestamptz), "
    "                                CAST('-infinity' AS timestamptz)) AS metered "
    "FROM llm_calls l LEFT JOIN code_task_runs cr ON cr.id = l.code_task_run_id "
    "WHERE {where}")

#: What each build has settled, all time, under its receipt key.
SETTLED_SQL = (
    "SELECT substr(skill_or_model_id, 11) AS r, "
    "       SUM(COALESCE(actual_usd_micros, estimated_usd_micros, 0)) AS s "
    "FROM cost_receipts WHERE project_id = :p AND skill_or_model_id LIKE 'code_task:%' "
    "GROUP BY 1")


def bind(connection: Any, run_id: str, reservation_id: Optional[str]) -> None:
    """Record the build's reservation on its run, and when it was first metered (kept)."""
    if not run_id or not hasattr(connection, "execute") or hasattr(connection, "save_run"):
        return
    from sqlalchemy import text

    connection.execute(text(
        "UPDATE code_task_runs SET reservation_id = COALESCE(:res, reservation_id), "
        "finalize_ctx = COALESCE(finalize_ctx, '{}'::jsonb) || jsonb_build_object("
        "'metered_since', COALESCE(finalize_ctx->'metered_since', to_jsonb(now()))) "
        "WHERE id = :r"), {"r": run_id, "res": reservation_id})


def unsettled_money(connection: Any, project_id: str, run_id: Optional[str]) -> int:
    """The build's money not yet on a receipt: the money of its metered calls (CALLS_SQL) less
    what was already settled under its receipt key. Never negative; 0 without a SQL ledger."""
    if not run_id or not hasattr(connection, "execute") or hasattr(connection, "save_run"):
        return 0  # an in-memory store (no SQL ledger): nothing recorded to settle
    from sqlalchemy import text

    gross = int(connection.execute(
        text("SELECT COALESCE(SUM(money) FILTER (WHERE metered), 0) FROM ("
             + CALLS_SQL.format(where="l.code_task_run_id = :r") + ") calls"),
        {"r": run_id}).scalar() or 0)
    settled = int(connection.execute(
        text("SELECT COALESCE(SUM(COALESCE(actual_usd_micros, estimated_usd_micros, 0)), 0) "
             "FROM cost_receipts WHERE project_id = :p AND skill_or_model_id = :k"),
        {"p": project_id, "k": receipt_key(run_id)}).scalar() or 0)
    return max(0, gross - settled)


# ── ONE SESSION'S CEILING ─────────────────────────────────────────────────────────────────────
#
# The Claude CLI's `--max-budget-usd` counts LIST price, whatever the billing. It was handed the
# per-task MONEY budget ($2) less the run's money — and a subscription's money is $0, so every
# session of a build got $2 at list price: a medium repo's implement, acceptance check and
# re-review were killed mid-answer (Northgate, crun_24f39976 / crun_9a9a535c). A session's ceiling
# is now sized to a session (scaled by risk tier and repo size) and bounded by what is left of
# the build's LIST-PRICE allowance — the same units the CLI counts.

#: Repo size (tracked files) → ceiling multiplier. Below the first bound is a small/medium repo.
REPO_SIZE_SCALE = ((10_000, 2.0), (2_000, 1.5))
#: Risk tier → ceiling multiplier (tiers 0-1 are 1.0).
RISK_TIER_SCALE = {2: 1.5, 3: 2.0}


def session_scale(risk_tier: Optional[int] = None, repo_files: Optional[int] = None) -> float:
    """How much larger than a medium, ordinary-risk session's this build's sessions may be."""
    scale = 1.0
    try:
        scale *= RISK_TIER_SCALE.get(int(risk_tier), 1.0) if risk_tier is not None else 1.0
    except (TypeError, ValueError):
        pass
    if repo_files:
        for bound, factor in REPO_SIZE_SCALE:
            if int(repo_files) >= bound:
                scale *= factor
                break
    return scale


def session_budget_usd(settings: Any, *, risk_tier: Optional[int] = None,
                       repo_files: Optional[int] = None) -> Optional[float]:
    """One session's list-price ceiling in dollars, or None when the install sets none."""
    base = float(getattr(settings, "code_agent_session_budget_usd", 0) or 0)
    if base <= 0:
        return None
    return round(base * session_scale(risk_tier, repo_files), 2)


def run_list_budget_usd(settings: Any, *, risk_tier: Optional[int] = None,
                        repo_files: Optional[int] = None) -> Optional[float]:
    """A whole build's list-price allowance in dollars, or None when the install sets none."""
    base = float(getattr(settings, "code_agent_run_list_budget_usd", 0) or 0)
    if base <= 0:
        return None
    return round(base * session_scale(risk_tier, repo_files), 2)


def list_price_spend(connection: Any, run_id: Optional[str]) -> int:
    """The build's spend at LIST price in micros: every `llm_calls` row of the run, money AND
    included (a subscription row keeps its list price as `cost_usd_micros`). 0 without a ledger."""
    if not run_id or not hasattr(connection, "execute") or hasattr(connection, "save_run"):
        return 0
    from sqlalchemy import text

    return int(connection.execute(
        text("SELECT COALESCE(SUM(COALESCE(cost_usd_micros, 0)), 0) FROM llm_calls "
             "WHERE code_task_run_id = :r"), {"r": run_id}).scalar() or 0)


#: An escalation may lift a session's ceiling at most this many times its session budget.
MAX_LIFT = 4.0


def build_money_budget_micros(settings: Any) -> int:
    """ONE BUILD'S MONEY BUDGET on a money-billed lane, in micros: what a build reserves and the
    most its sessions may spend in money. `code_task_build_money_budget_usd` (the real expected
    cost of a medium build, $9-13 measured) — the $2 per-task figure it replaces was a fraction of
    one session. Falls back to `code_task_budget_usd_micros` when unset (0)."""
    usd = float(getattr(settings, "code_task_build_money_budget_usd", 0) or 0)
    if usd > 0:
        return int(round(usd * 1e6))
    return int(getattr(settings, "code_task_budget_usd_micros", 0) or 0)


def next_session_is_money(agent: Any, routed_included: Any) -> bool:
    """Whether the NEXT session of `agent` spends MONEY (an API key, a metered gateway).

    Resolved now, by the engine's own credential resolution (`billing_for_next_session`, the
    order its child env uses) — never the `_billing` stamp, which is written while the child env
    is built, AFTER the session's ceiling was computed: reading it classed each session by the
    PREVIOUS session's credential, so the first session after a subscription -> API key switch
    (credential order, spend-cap failover, pool change) ran under subscription-only limits.
    An engine without the resolver: `routed_included()` (the project's routed source).
    Unknown, or a resolution that fails, is money — the safe side."""
    from .included_spend import INCLUDED_BILLING

    resolve = getattr(agent, "billing_for_next_session", None) if agent is not None else None
    if callable(resolve):
        try:
            billing = str(resolve() or "").strip().lower()
        except Exception:  # noqa: BLE001 — no credential resolves: money
            return True
        if billing:
            return billing not in INCLUDED_BILLING
    try:
        return not bool(routed_included())
    except Exception:  # noqa: BLE001
        return True


def _run_bounds(settings: Any, *, list_spent_micros: int, money: bool,
                build_money_spent_micros: int, period_headroom_micros: Optional[int],
                risk_tier: Optional[int], repo_files: Optional[int]) -> list[float]:
    """What is left of every run-level limit, in dollars: the build's list-price allowance on
    every lane; on a MONEY-billed lane also the build's money budget and the tightest period
    cap's headroom (cap - settled - in flight), so no single session can overshoot a cap."""
    bounds: list[float] = []
    allowance = run_list_budget_usd(settings, risk_tier=risk_tier, repo_files=repo_files)
    if allowance is not None:
        bounds.append(max(allowance - max(int(list_spent_micros or 0), 0) / 1e6, 0.0))
    if money:
        budget = build_money_budget_micros(settings)
        if budget > 0:
            bounds.append(max(budget - max(int(build_money_spent_micros or 0), 0), 0) / 1e6)
        if period_headroom_micros is not None:
            bounds.append(max(int(period_headroom_micros), 0) / 1e6)
    return bounds


def session_ceiling_usd(settings: Any, *, list_spent_micros: int, money: bool = False,
                        build_money_spent_micros: int = 0,
                        period_headroom_micros: Optional[int] = None,
                        risk_tier: Optional[int] = None,
                        repo_files: Optional[int] = None) -> Optional[float]:
    """The next session's ceiling: the session budget, never more than what is left of any
    run-level limit (_run_bounds; floored at a cent by engine_contract._default_ceiling).

    On included (subscription/local) billing that is the list-price allowance only. On a
    MONEY-billed lane it is also the build's money budget left and the tightest period cap's
    headroom: a $20 session on an API key could otherwise overshoot a $10 daily cap that is only
    checked between stages. None when nothing is set."""
    session = session_budget_usd(settings, risk_tier=risk_tier, repo_files=repo_files)
    bounds = _run_bounds(settings, list_spent_micros=list_spent_micros, money=money,
                         build_money_spent_micros=build_money_spent_micros,
                         period_headroom_micros=period_headroom_micros,
                         risk_tier=risk_tier, repo_files=repo_files)
    limits = ([session] if session is not None else []) + bounds
    return round(min(limits), 2) if limits else None


def lifted_ceiling_usd(settings: Any, requested_usd: float, *, list_spent_micros: int,
                       money: bool = False, build_money_spent_micros: int = 0,
                       period_headroom_micros: Optional[int] = None,
                       risk_tier: Optional[int] = None,
                       repo_files: Optional[int] = None) -> float:
    """An ESCALATED ceiling (a re-ask lifting the limit that stopped a pass), clamped: never
    above MAX_LIFT x the session budget, and never above what is left of any run-level limit —
    an explicit ceiling bypasses the session default, so doubling it per round compounded
    ($20 -> $40 -> $80) past every allowance."""
    limits = [float(requested_usd)]
    session = session_budget_usd(settings, risk_tier=risk_tier, repo_files=repo_files)
    if session is not None:
        limits.append(session * MAX_LIFT)
    limits += _run_bounds(settings, list_spent_micros=list_spent_micros, money=money,
                          build_money_spent_micros=build_money_spent_micros,
                          period_headroom_micros=period_headroom_micros,
                          risk_tier=risk_tier, repo_files=repo_files)
    return round(min(limits), 2)


def allowance_reached(settings: Any, *, list_spent_micros: int, money: bool = False,
                      build_money_spent_micros: int = 0, override_micros: int = 0,
                      risk_tier: Optional[int] = None,
                      repo_files: Optional[int] = None) -> Optional[str]:
    """The build's own allowance, checked at a stage boundary: its words when the build has used
    its list-price allowance (or, on a money-billed lane, its money budget), else None. Without
    this, a spent allowance only gave every later session a one-cent ceiling — partial attempts
    until the counters ran out. An approved budget.override (BOSSAPP-884) adds its amount."""
    extra = max(int(override_micros or 0), 0)
    allowance = run_list_budget_usd(settings, risk_tier=risk_tier, repo_files=repo_files)
    if allowance is not None and int(list_spent_micros or 0) >= allowance * 1e6 + extra:
        return (f"This build used its ${allowance:,.2f} allowance "
                f"(${int(list_spent_micros) / 1e6:,.2f} at list price).")
    budget = build_money_budget_micros(settings)
    if money and budget > 0 and int(build_money_spent_micros or 0) >= budget + extra:
        return (f"This build used its ${budget / 1e6:,.2f} money budget "
                f"(${int(build_money_spent_micros) / 1e6:,.2f} spent).")
    return None


def settle_or_release(budget: Any, connection: Any, project_id: str,
                      reservation_id: Optional[str], reason: str,
                      run_id: Optional[str] = None) -> Optional[str]:
    """Settle the build's unsettled money on `reservation_id`, or release it when there is none.

    Money already burned is REAL even when the build failed or was stopped (BOSS-280), so every
    exit settles it. A build whose calls all ran on a subscription has $0 of money: its hold is
    released, and its calls' list price stays on `llm_calls` as the included estimate. Returns
    the cost receipt id when one was written. Fail-soft: an unsettled spend is recorded, never
    silent, and the hold is released either way."""
    if not reservation_id:
        return None
    if _already_closed(connection, project_id, reservation_id):
        return None  # settled or released before (a resumed build's earlier hold): nothing owed
    try:
        money = unsettled_money(connection, project_id, run_id)
        if money > 0:
            # settle() records a cost receipt against a governance receipt (every settled
            # effect gets one).
            from .receipts import ReceiptService
            from sqlalchemy import text

            scope = connection.execute(
                text("SELECT p.workspace_id, w.org_id FROM projects p "
                     "JOIN workspaces w ON w.id = p.workspace_id WHERE p.id = :p"),
                {"p": project_id}).mappings().first()
            receipt, _created = ReceiptService().create_or_reuse(
                connection, scope["org_id"], scope["workspace_id"], project_id,
                dedupe_key=f"budget:settle:{run_id}:{reservation_id}",
                effect_kind="budget.llm_spend.settle",
                effect_scope={"run_id": run_id, "reservation_id": reservation_id},
                payload={"actual_usd_micros": money, "reason": reason},
            )
            # allow_expired: a hold that lapsed while its build ran is still the build's — the
            # money was spent, so it is recorded rather than dropped with the hold.
            return budget.settle(
                connection, project_id, reservation_id,
                receipt_id=receipt["id"], category=CATEGORY,
                skill_or_model_id=receipt_key(run_id),
                estimated_usd_micros=money, actual_usd_micros=money, allow_expired=True)
        budget.release(connection, project_id, reservation_id, reason)
    except Exception:  # noqa: BLE001 — always fail-soft, but an unsettled spend is never silent
        from .crash_frames import record_swallowed
        record_swallowed(logger, "release_budget", project_id=project_id, run_id=run_id,
                         connection=connection if hasattr(connection, "begin_nested") else None)
        try:
            budget.release(connection, project_id, reservation_id, reason)
        except Exception:  # noqa: BLE001
            pass
    return None


def _already_closed(connection: Any, project_id: str, reservation_id: str) -> bool:
    if not hasattr(connection, "execute") or hasattr(connection, "save_run"):
        return False
    from sqlalchemy import text

    try:
        with connection.begin_nested():
            status = connection.execute(text(
                "SELECT status FROM budget_reservations WHERE id = :i AND project_id = :p"),
                {"i": reservation_id, "p": project_id}).scalar()
    except Exception:  # noqa: BLE001 — unknown: settle or release as usual
        return False
    return status in ("consumed", "released")


def settle_on_pull_request(budget: Any, connection: Any, project_id: str, reservation_id: str,
                           receipt_id: str, reserved_usd_micros: int, run_id: str) -> str:
    """The pull request opened: the build's reservation is settled with its unsettled money,
    under the build's receipt key — the same figure every other exit settles."""
    money = unsettled_money(connection, project_id, run_id)
    return budget.settle(
        connection, project_id, reservation_id, receipt_id=receipt_id, category=CATEGORY,
        skill_or_model_id=receipt_key(run_id), estimated_usd_micros=int(reserved_usd_micros),
        actual_usd_micros=money, allow_expired=True)


def inflight_money(connection: Any, project_id: str) -> int:
    """Money running builds have spent and not yet settled: builds whose reservation is still
    active. It lands in the current period when they settle, so the caps count it now."""
    from sqlalchemy import text

    return int(connection.execute(text(
        "WITH live AS (SELECT r.id FROM code_task_runs r "
        "              JOIN budget_reservations b ON b.id = r.reservation_id "
        "              WHERE r.project_id = :p AND b.project_id = :p AND b.status = 'active'), "
        "     spent AS (SELECT r, SUM(money) FILTER (WHERE metered) AS m FROM ("
        + CALLS_SQL.format(where="l.code_task_run_id IN (SELECT id FROM live)")
        + ") calls GROUP BY 1), "
        f"     settled AS ({SETTLED_SQL}) "
        "SELECT COALESCE(SUM(GREATEST(spent.m - COALESCE(settled.s, 0), 0)), 0) "
        "FROM spent LEFT JOIN settled ON settled.r = spent.r"), {"p": project_id}).scalar() or 0)


def _money_periods(budget: Any, connection: Any, project_id: str,
                   run_id: Optional[str]) -> Optional[tuple[list, int, int]]:
    """The capped periods a build's money counts against, what running builds have spent
    unsettled, and this run's approved override — or None when nothing caps the build (no cap,
    or the operator's at-limit choice `cheaper` lets it go on)."""
    snapshot = budget.status_for_project(connection, project_id)
    periods = (snapshot or {}).get("periods") or {}
    capped = [p for p in periods.values() if int(p.get("cap_usd_micros") or 0)]
    # BOSSAPP-807: the project's monthly limit stops a build like a cap, and the operator's
    # at-limit choice `cheaper` lets a running build go on (red budget: low-cost models).
    from . import spend_limits

    try:
        with connection.begin_nested():
            extra = spend_limits.limit_period(connection, project_id, snapshot or {})
    except Exception:  # noqa: BLE001 — no limit readable is no limit, never a stopped build
        extra = None
    if extra is not None:
        capped.append(extra)
    if capped and spend_limits.at_limit(connection, getattr(budget, "settings", None),
                                        project_id) == "cheaper":
        return None
    if not capped:
        return None  # unmetered, or no cap set: nothing to cross
    building = inflight_money(connection, project_id)
    return capped, building, override_money(connection, project_id, run_id)


def override_money(connection: Any, project_id: str, run_id: Optional[str]) -> int:
    """A PERSON SAID THIS BUILD MAY GO OVER (BOSSAPP-884, agent_requests): an approved
    budget.override's amount, for this run's lineage only. 0 when none or unreadable."""
    if not run_id:
        return 0
    from .agent_requests import override_headroom

    try:
        with connection.begin_nested():
            return int(override_headroom(connection, project_id, run_id) or 0)
    except Exception:  # noqa: BLE001 — an override that cannot be read grants nothing
        return 0


def cap_reached(budget: Any, connection: Any, project_id: str,
                run_id: Optional[str] = None) -> Optional[dict[str, Any]]:
    """The cap a running build has crossed, or None. Money only: the money settled in each cap's
    period plus what running builds have spent unsettled. Other work's holds are a moment, not
    spending (BudgetGovernor.cap_refusal's `headroom`), so they never stop a build.

    Returns the period (as status_for_project gives it) with `committed_usd_micros` and
    `utilization_ratio` set to the money, and `message` — BOSS's cap words."""
    found = _money_periods(budget, connection, project_id, run_id)
    if found is None:
        return None
    capped, building, headroom = found
    for period in capped:  # status_for_project orders the day first
        cap = int(period["cap_usd_micros"])
        spent = int(period.get("actual_usd_micros") or 0) + building
        if spent < cap + headroom:
            continue
        from .model_failures import cap_used_sentence, resets_sentence

        said = {**period, "committed_usd_micros": spent, "utilization_ratio": spent / cap}
        return {**said, "message": (f"Budget cap reached: {cap_used_sentence(said)}. "
                                    f"{resets_sentence(said)}")}
    return None


def money_headroom(budget: Any, connection: Any, project_id: str,
                   run_id: Optional[str] = None) -> Optional[int]:
    """What the tightest money cap still allows, in micros: min over capped periods of
    cap + override - settled - in flight (cap_reached's arithmetic). None when nothing caps it."""
    found = _money_periods(budget, connection, project_id, run_id)
    if found is None:
        return None
    capped, building, headroom = found
    return max(0, min(int(p["cap_usd_micros"]) + headroom
                      - int(p.get("actual_usd_micros") or 0) - building for p in capped))


def renew(budget: Any, connection: Any, project_id: str, reservation_id: Optional[str],
          minutes: int = RESERVATION_MINUTES) -> None:
    """Keep a running build's hold alive across a stage boundary. Never raises."""
    if not reservation_id:
        return
    try:
        budget.extend(connection, project_id, reservation_id, minutes)
    except Exception:  # noqa: BLE001 — a hold that lapses is settled with allow_expired
        logger.info("build reservation %s not renewed", reservation_id)
