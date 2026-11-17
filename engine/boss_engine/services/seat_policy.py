"""Which review seats a profile may leave unconvened, and how a skipped seat is read back.

ONE DECISION, TWO READERS (AR-32, BOSSAPP-1346). The quality gates recorded `security_clean` and
`consistent` as PASSED for a run whose profile never convened those seats, while the run's
retrospective called the same seats a blocker ("reviewer(s) never produced a verdict"): two
places each deciding for themselves what "the seat did not run" means. `skip_reason` decides,
once, when the pipeline builds the panel; the decision is recorded on the run (`<seat>_skipped`,
`gates_skipped`) and everything after — the gate record, the PR, the console, the retrospective —
reads it with `skipped_seats`.
"""
from __future__ import annotations

from typing import Any, Iterable, Mapping, Optional

#: seat -> the gate its verdict decides.
SEAT_GATES: dict[str, str] = {"review_code": "code_review_passed", "security_check": "security_clean",
                              "consistency_check": "consistent"}
#: seat -> the evidence key holding its verdict.
SEAT_VERDICTS: dict[str, str] = {"review_code": "adversarial_verdict",
                                 "security_check": "security_verdict",
                                 "consistency_check": "consistency_verdict"}
#: Seats no profile may skip: the code reviewer is the floor (review_panel_seats), and the
#: acceptance check (BOSSAPP-1211) sits on every profile — a lean run must never turn it into a
#: pass by not asking.
NEVER_SKIPPED = frozenset({"review_code", "acceptance_met"})


def skip_reason(seat: str, enabled_seats: Iterable[str], profile: Optional[Mapping[str, Any]]
                ) -> Optional[str]:
    """Why `seat` may be left unconvened (its words), or None when it must have run."""
    if seat in NEVER_SKIPPED or seat in set(enabled_seats):
        return None
    return f"not convened: profile '{(profile or {}).get('name')}' does not include this seat"


def skipped_seats(state: Optional[Mapping[str, Any]]) -> dict[str, str]:
    """seat -> why, for the seats a run's record says were not convened."""
    state = state or {}
    return {seat: str(state[f"{seat}_skipped"]) for seat in SEAT_GATES
            if state.get(f"{seat}_skipped") and seat not in NEVER_SKIPPED}


def skipped_gates(state: Optional[Mapping[str, Any]]) -> dict[str, str]:
    """gate -> why, for the gates whose check did not run (what the gate record keeps)."""
    recorded = (state or {}).get("gates_skipped")
    if isinstance(recorded, Mapping):
        return {str(g): str(why) for g, why in recorded.items()}
    return {SEAT_GATES[seat]: why for seat, why in skipped_seats(state).items()}
