"""The process the crash test kills with SIGKILL (test_agent_runs_are_durable.py, acceptance (a)).

Usage: python _durable_crash_child.py <database url> <project id> <run id> <ledger path>

Starts a durable kernel run (Postgres checkpointer, `sync`) whose one model turn asks for three
tools; `charge_card` fires and then hangs, so the parent kills this process mid-batch.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from sqlalchemy import create_engine  # noqa: E402

from _durable_support import policies, scripted_batch, toolset  # noqa: E402
from boss_engine import kernel  # noqa: E402
from boss_engine.kernel.durable import PgCheckpointStore  # noqa: E402
from boss_engine.kernel.scripted import ScriptedEngine  # noqa: E402


def main() -> int:
    url, project_id, run_id, ledger = sys.argv[1:5]
    engine = create_engine(url, future=True)
    store = PgCheckpointStore(engine, durability="sync")
    kernel.run(kernel.RunSpec(
        engine=ScriptedEngine(scripted_batch()), tools=toolset(Path(ledger), hang=True),
        policies=policies(), project_id=project_id, system="You are a billing agent.",
        messages=[{"role": "user", "content": "charge the card and tell the customer"}],
        run_id=run_id, persist=True, store=store))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
