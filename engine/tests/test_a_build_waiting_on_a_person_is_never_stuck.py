"""WHO HOLDS THE NEXT MOVE (independent re-check R1, R4): a build at its plan, its merge, or any
open decision waits on a person. Clean up never stops it, Shepherd never asks about it, and a
finding about it never says BOSS couldn't name the reason. Pure, plus the one-door guard."""
from __future__ import annotations

import pathlib
import re

from boss_engine.services.build_waits import waits_on_person, why_waiting
from boss_engine.services.system_doctor import escalation_words

ROOT = pathlib.Path(__file__).resolve().parents[2]


def test_who_holds_the_next_move():
    assert waits_on_person("blocked", "awaiting_plan_approval", None)
    assert waits_on_person("blocked", "awaiting_gate_override", "expired")
    assert waits_on_person("completed", "done", "pending")          # a merge to approve
    assert not waits_on_person("running", "coding", None)            # BOSS's move
    assert not waits_on_person("cancelled", "awaiting_plan_approval", "pending")
    assert why_waiting("awaiting_plan_approval", None) == "approve its plan"
    assert why_waiting("done", "pending") == "approve its merge"


def test_a_waiting_build_is_never_called_unexplained():
    build = {"id": "crun_x1y2z3a4b5", "title": "Build an interview scorecard", "status": "blocked",
             "phase": "awaiting_plan_approval", "decision_status": "expired", "spec": {},
             "finalize_ctx": {}}
    title, _ = escalation_words("unknown — how should this proceed?", "crun_x1y2z3a4b5", build)
    assert "couldn't name" not in title and "unknown" not in title.lower()
    assert title == "“Interview Scorecard” waits on you to approve its plan"


def test_shepherd_asks_through_one_door():
    """Every question Shepherd writes goes through shepherd_service.open_question, which checks
    who holds the build's next move and dedupes; a second INSERT anywhere would bypass both."""
    hits = []
    for path in (ROOT / "engine" / "boss_engine").rglob("*.py"):
        for m in re.finditer(r"INSERT INTO shepherd_escalations", path.read_text(encoding="utf-8")):
            hits.append(path.name)
    assert hits == ["shepherd_service.py"], hits
    src = (ROOT / "engine/boss_engine/services/shepherd_service.py").read_text(encoding="utf-8")
    door = src[src.index("def open_question"):]
    assert "build_waits_on_person" in door[:1500]
