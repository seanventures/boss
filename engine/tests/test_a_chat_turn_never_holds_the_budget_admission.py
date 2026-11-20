"""A CHAT TURN NEVER HOLDS THE PROJECT'S BUDGET ADMISSION (laptop 2026-11-20).

The admission lock is transaction-scoped (budget._try_admit). The orchestration turn reserved
its model budget on the turn's own connection and then ran minutes of model calls and tools
with that transaction open, so every tool in the turn that reserved on its own connection —
deploy_app, deploy_config, a build — was refused "Budget admission is busy with another
transaction", four times in a row, with nothing else running on the project (pg_locks showed
the turn's backend `idle in transaction` holding the project's lock for 48 s+).
"""
from __future__ import annotations

from pathlib import Path

from sqlalchemy import text

from boss_engine.config import get_settings
from boss_engine.services import budget

from ._ambient_support import eng, project, purge  # noqa: F401

ROOT = Path(__file__).resolve().parents[2]


def _admits(engine, pid) -> bool:
    with engine.begin() as other:
        return bool(other.execute(text(
            "SELECT pg_try_advisory_xact_lock(hashtextextended('boss-budget:' || :p, 0))"),
            {"p": pid}).scalar_one())


def test_a_reservation_held_apart_leaves_admission_free_for_the_turn_s_tools(eng, project, monkeypatch):  # noqa: F811
    monkeypatch.setattr(budget, "ADMISSION_RETRY_PAUSES", ())
    gov = budget.BudgetGovernor(get_settings())
    with eng.begin() as turn:                     # the chat turn's transaction, still open
        gov.reserve(turn, project, "model", 1000, "the old way", 10)
        assert not _admits(eng, project), "the old way: the turn holds admission"
    with eng.begin() as turn:
        budget.reserve_apart(gov, turn, project, "model", 1000, "held apart", 10)
        assert _admits(eng, project), "a tool in the same turn can reserve"


def test_the_orchestration_turn_reserves_its_model_calls_apart():
    src = (ROOT / "engine/boss_engine/services/intent_router.py").read_text(encoding="utf-8")
    for reason in ("Orchestration first answer from a subject's record.",
                   "Orchestration conversational answer.",
                   "Orchestration intent classification."):
        i = src.index(reason)
        call = src[src.rindex("reservation = ", 0, i):i]
        assert "reserve_apart(" in call, \
            f"{reason!r} must reserve apart: its transaction stays open across the model call"
