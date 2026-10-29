"""Shared contract for the L2a run supervisor (see run_supervisor.py).

Kept in its own module so the pipeline integration points and the test suite can import the
action constants / dataclass without pulling in the supervisor's judgement-call machinery.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

STEER = "steer"
CHANGE_APPROACH = "change_approach"
PLATFORM_FIX = "platform_fix"
STOP_REPORT = "stop_report"
REACH_OUT = "reach_out"

ACTIONS: tuple[str, ...] = (STEER, CHANGE_APPROACH, PLATFORM_FIX, STOP_REPORT, REACH_OUT)

#: Fail-closed-only state. Never a valid judgement output — only `decide()` may return it, and
#: only when a decision was already partially recorded this cycle before the failure happened.
DEGRADED = "degraded_no_action"

#: The PERSON allow-list: a refusal (credential/authority/permission/budget/quota) can only ever
#: be routed to one of these two actions, regardless of what judgement answers.
REFUSAL_ALLOWED: tuple[str, ...] = (REACH_OUT, STOP_REPORT)

DECIDED_EVENT = "supervisor.decided"

STALL_MINUTES_SETTING = "run_supervisor_stall_minutes"
DEFAULT_STALL_MINUTES = 15
MAX_ROUNDS_WITHOUT_JUDGEMENT = 3

#: The three independently toggle-able feature flags (project_settings.KNOBS / Settings).
SIGNAL_SETTING = "run_supervisor_signal"
JUDGEMENT_SETTING = "run_supervisor_judgement"
ACTION_SETTING = "run_supervisor_action"


@dataclass(frozen=True)
class ProgressSignal:
    tripped: bool
    reason: str
    forced: bool
    caused: tuple[str, ...]
    environment: tuple[str, ...]
    refusals: tuple[str, ...]
    repeated_signature: Optional[str]
    rounds_used: int
    spend_usd_micros: int
    elapsed_seconds: int

    @property
    def allowed_actions(self) -> tuple[str, ...]:
        if self.refusals and not (self.caused or self.environment):
            return REFUSAL_ALLOWED
        return ACTIONS
