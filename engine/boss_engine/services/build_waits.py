"""WHO HOLDS A BUILD'S NEXT MOVE. One rule for every surface that decides whether a build is
"stuck" (BOSS owns the next move and nothing moved) or waiting on a person (a plan to approve,
a merge to approve, any open decision, any `awaiting_*` phase). Clean up must never stop the
second kind, and Shepherd must never ask about it (independent re-check R1, R4)."""
from __future__ import annotations

from typing import Any, Optional

from sqlalchemy import text

#: A decision in one of these is still waiting on its person.
OPEN_DECISION = frozenset({"pending", "delegated", "revision_requested", "expired"})
_GONE = frozenset({"cancelled", "failed", "simulated"})


def waits_on_person(status: Any, phase: Any, decision_status: Any) -> bool:
    """True when the build's next move is a person's. Pure."""
    if str(status or "") in _GONE:
        return False
    if str(phase or "").startswith("awaiting_"):
        return True
    return str(decision_status or "") in OPEN_DECISION


def build_waits_on_person(connection: Any, run_id: str) -> bool:
    """`waits_on_person` for one build, read with its decision. False for a build not found."""
    row = connection.execute(text(
        "SELECT r.status, r.phase, d.status AS decision_status FROM code_task_runs r "
        "LEFT JOIN decisions d ON d.id = r.decision_id WHERE r.id = :id"),
        {"id": run_id}).mappings().first()
    return bool(row) and waits_on_person(row["status"], row["phase"], row["decision_status"])


def why_waiting(phase: Any, decision_status: Any) -> Optional[str]:
    """What the person is asked for, in words: `approve its plan`, `answer a check`, …"""
    p = str(phase or "")
    if p == "awaiting_plan_approval":
        return "approve its plan"
    if p == "awaiting_gate_override":
        return "decide on its failed checks"
    if p.startswith("awaiting_"):
        return "answer it"
    if str(decision_status or "") in OPEN_DECISION:
        return "approve its merge"
    return None
