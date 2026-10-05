"""Meeting consensus completeness (spec §D): an explicit vote phase with quorum/default rules,
per-phase tool budgets, and phase timeouts (stall prevention).

The vote tally is a pure function — given each participant's vote it decides approved / rejected /
no_quorum under quorum + threshold rules, and emits the dissent records the consensus engine
requires. The phase budget/timeout helpers are pure config the LLM phase-machine consults, and
`sweep_stalled_meeting_runs` is the watchdog that fails-out a meeting stuck past its phase timeout
(turning a silent stall into a visible incident decision).
"""
from __future__ import annotations

import math
from datetime import datetime
from typing import Any, Optional

from sqlalchemy import text

from ..config import Settings
from ..db import begin
from .core import create_blocking_decision, emit_event, now_utc

# Per-phase wall-clock timeout (seconds) for the LLM meeting phase-machine. A phase whose row has
# not advanced within this window is treated as stalled by the watchdog.
PHASE_TIMEOUTS_SECONDS = {
    "research": 600,
    "round1": 600,
    "round2": 600,
    "synthesis": 600,
    "vote": 300,
    "draft": 900,
    "finalize": 300,
}
DEFAULT_PHASE_TIMEOUT_SECONDS = 900

# Per-phase model/tool spend cap (USD micros). The phase-machine consults these to avoid one phase
# burning the whole meeting budget.
PHASE_TOOL_BUDGETS_USD_MICROS = {
    "research": 200_000,
    "round1": 300_000,
    "round2": 300_000,
    "synthesis": 200_000,
    "vote": 150_000,
    "draft": 400_000,
    "finalize": 50_000,
}
DEFAULT_PHASE_BUDGET_USD_MICROS = 300_000

_APPROVE = {"approve", "approved", "yes", "aye", "for", "+1"}
_REJECT = {"reject", "rejected", "no", "nay", "against", "-1"}
_ABSTAIN = {"abstain", "abstained", "pass", "present"}


def phase_timeout_seconds(phase: str) -> int:
    return PHASE_TIMEOUTS_SECONDS.get(phase, DEFAULT_PHASE_TIMEOUT_SECONDS)


def phase_timed_out(phase: str, updated_at: datetime, now: datetime) -> bool:
    return (now - updated_at).total_seconds() > phase_timeout_seconds(phase)


def phase_budget_usd_micros(phase: str) -> int:
    return PHASE_TOOL_BUDGETS_USD_MICROS.get(phase, DEFAULT_PHASE_BUDGET_USD_MICROS)


def phase_within_budget(phase: str, phase_cost_usd_micros: int) -> bool:
    return int(phase_cost_usd_micros or 0) <= phase_budget_usd_micros(phase)


def _classify(vote: str) -> str:
    choice = str(vote or "").strip().lower()
    if choice in _APPROVE:
        return "approve"
    if choice in _REJECT:
        return "reject"
    return "abstain"


def tally_votes(
    votes: list[dict[str, Any]],
    *,
    eligible_count: int,
    quorum: Optional[int] = None,
    threshold: float = 0.5,
    default_action: str = "hold",
) -> dict[str, Any]:
    """Tally a panel vote under quorum + threshold rules. Returns the outcome (approved | rejected |
    no_quorum), the counts, whether consensus was reached, and the dissent records to persist."""
    normalized = [
        {"voter": v.get("voter"), "vote": _classify(v.get("vote")), "reason": v.get("reason")}
        for v in votes
    ]
    approve = sum(1 for v in normalized if v["vote"] == "approve")
    reject = sum(1 for v in normalized if v["vote"] == "reject")
    abstain = sum(1 for v in normalized if v["vote"] == "abstain")
    votes_cast = len(normalized)
    eligible = max(int(eligible_count or 0), votes_cast)
    if quorum is None:
        quorum = math.ceil(eligible / 2) if eligible else 0
    quorum_met = votes_cast >= quorum and votes_cast > 0
    decisive = approve + reject
    approval_ratio = (approve / decisive) if decisive else 0.0

    if not quorum_met:
        outcome = "no_quorum"
        consensus = False
    elif approval_ratio >= threshold:
        outcome = "approved"
        consensus = True
    else:
        outcome = "rejected"
        consensus = False

    # WHO DISSENTED, NAMED ONLY WHEN THEY HAVE A NAME. A count recorded from the console has no
    # voters, and inventing ids for it put "r0: voted to reject" on the meeting's record. The
    # unnamed are one record in words: "1 seat voted to reject".
    named = [v for v in normalized if v["vote"] == "reject" and str(v["voter"] or "").strip()]
    unnamed = sum(1 for v in normalized if v["vote"] == "reject" and not str(v["voter"] or "").strip())
    dissent = [
        {"role": v["voter"], "position": "reject", "reason": v["reason"] or "voted to reject"}
        for v in named
    ]
    if unnamed:
        dissent.append({"role": "panel", "position": "reject",
                        "reason": f"{unnamed} seat{'' if unnamed == 1 else 's'} voted to reject"})
    if outcome == "no_quorum":
        dissent.append(
            {
                "role": "quorum",
                "position": "no_quorum",
                "reason": (
                    f"Only {votes_cast} of the {quorum} votes needed were cast, so the default "
                    f"— {default_action} — applies."
                ),
            }
        )
    return {
        "outcome": outcome,
        "approve": approve,
        "reject": reject,
        "abstain": abstain,
        "votes_cast": votes_cast,
        "eligible": eligible,
        "quorum": quorum,
        "quorum_met": quorum_met,
        "approval_ratio": round(approval_ratio, 4),
        "threshold": threshold,
        "default_action": default_action,
        "consensus_reached": consensus,
        "dissent_records": dissent,
    }


def sweep_stalled_meeting_runs(
    engine, settings: Settings, *, actor: str = "meeting_watchdog"
) -> dict[str, Any]:
    """Fail out any running meeting phase that has stalled past its phase timeout, opening an
    incident Decision so a silent stall becomes a visible, actionable item (spec §D timeouts)."""
    swept: list[dict[str, Any]] = []
    now = now_utc()
    with begin(engine) as connection:
        rows = connection.execute(
            text(
                """
                SELECT id, project_id, task_id, phase, updated_at
                FROM meeting_runs
                WHERE status = 'running'
                """
            )
        ).mappings().all()
        for row in rows:
            if not phase_timed_out(row["phase"], row["updated_at"], now):
                continue
            age = int((now - row["updated_at"]).total_seconds())
            timeout = phase_timeout_seconds(row["phase"])
            connection.execute(
                text(
                    "UPDATE meeting_runs SET status = 'failed', error = :error, updated_at = now() "
                    "WHERE id = :id"
                ),
                {"error": f"phase '{row['phase']}' stalled ({age}s > {timeout}s)", "id": row["id"]},
            )
            decision_id = create_blocking_decision(
                connection,
                row["project_id"],
                row["task_id"],
                f"Meeting run stalled in phase '{row['phase']}'",
                {"action": "review_stalled_meeting", "run_id": row["id"], "phase": row["phase"]},
                {"age_seconds": age, "timeout_seconds": timeout},
                "meeting_run_stalled",
                risk_tier=2,
            )
            emit_event(
                connection,
                row["project_id"],
                "meeting.stalled",
                actor,
                "meeting_run",
                row["id"],
                {"phase": row["phase"], "age_seconds": age, "decision_id": decision_id},
            )
            swept.append(
                {
                    "run_id": row["id"],
                    "phase": row["phase"],
                    "age_seconds": age,
                    "timeout_seconds": timeout,
                    "decision_id": decision_id,
                }
            )
    return {"swept": len(swept), "runs": swept}
