"""GUARD: the capability tick stays inside the transaction Postgres will let it hold.

POST /v1/delivery-workflows/run-capabilities used to run `workflow_capability_runner.tick` for
each project inside ONE transaction; since AP-P0 it runs `tick_per_step`, one transaction per
run's step. The API's engine ends any session idle inside a
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


def test_each_step_ticks_in_its_own_transaction():
    """AP-P0: the hazard is removed, not just kept rare. The endpoint hands the ENGINE to
    `tick_per_step`, which opens one transaction per run's step (engine/tests/
    test_ap_p0_commit_per_step.py drives a real idle kill through it)."""
    body = _endpoint_source()
    loop = body.index("for pid in projects:")
    after = body[loop:]
    assert re.search(r"for pid in projects:.*?workflow_capability_runner\.tick_per_step\(\s*"
                     r"engine,", after, re.S), "the tick must commit per step"
    assert "workflow_capability_runner.tick(" not in body
    src = inspect.getsource(workflow_capability_runner.tick_per_step)
    per_run = src.index("for run in runs:")
    assert "with open_tx() as connection:" in src[per_run:]
    assert "_advance_one_run(connection" in src[per_run:]


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
