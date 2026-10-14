"""GUARD: the capability tick stays inside the transaction Postgres will let it hold.

POST /v1/delivery-workflows/run-capabilities runs `workflow_capability_runner.tick` for each
project inside ONE transaction; the tick claims and executes up to MAX_STEPS_PER_TICK steps
inline (savepoints), and commits at the end. The API's engine ends any session idle inside a
transaction for longer than BOSS_API_DB_IDLE_IN_TRANSACTION_SECONDS (300 s). A step that waits
longer than that between two statements rolls back every step the tick ran for that project,
and the next tick runs them again — a duplicate wherever the step's effect was not keyed. The
hazard and the fix that removes it (commit per claimed step) are written at the endpoint in
apps/api/app/main.py; this pins what keeps it rare until then:

  * each project gets its OWN transaction (one stuck project never rolls back another's work);
  * every per-call wall the tick runs inline is well under the idle limit.

It fails the moment either moves the wrong way, so the hazard cannot grow unnoticed.
"""
from __future__ import annotations

import inspect
import re
from pathlib import Path

from boss_engine.config import Settings
from boss_engine.services import typed_decision_spend, workflow_capability_runner

MAIN = (Path(__file__).resolve().parents[1] / "app" / "main.py").read_text(encoding="utf-8")


def _endpoint_source() -> str:
    start = MAIN.index("def run_delivery_capabilities(")
    end = MAIN.index("\n@app.", start)
    return MAIN[start:end]


def test_each_project_ticks_in_its_own_transaction():
    body = _endpoint_source()
    loop = body.index("for pid in projects:")
    after = body[loop:]
    # The transaction opens INSIDE the per-project loop, around the tick.
    assert re.search(r"for pid in projects:.*?with begin\(engine\) as connection:\s*\n\s*result = "
                     r"workflow_capability_runner\.tick\(", after, re.S), \
        "the tick must run in one transaction PER PROJECT, opened inside the loop"
    # And the hazard is written where the next person will look.
    assert "KNOWN HAZARD — ONE TRANSACTION PER PROJECT, NOT PER STEP" in body


def test_every_inline_call_wall_is_well_under_the_idle_limit():
    idle = Settings.model_fields["api_db_idle_in_transaction_seconds"].default
    assert idle == 300
    walls = {
        "workflow_capability_runner.LEASE_SECONDS": workflow_capability_runner.LEASE_SECONDS,
        "typed_decision_spend.LEASE_TTL_SECONDS": typed_decision_spend.LEASE_TTL_SECONDS,
    }
    for name, seconds in walls.items():
        # Half the limit: a wall that reaches it turns one slow step into a rolled-back tick.
        assert seconds <= idle // 2, f"{name}={seconds}s is too close to the {idle}s idle limit"
    probe = inspect.signature(workflow_capability_runner._preview_answers).parameters["timeout"].default
    assert probe <= 30


def test_the_tick_is_bounded():
    assert 1 <= workflow_capability_runner.MAX_STEPS_PER_TICK <= 10
