"""Autonomous ("ambient") agents: the shared foundation (BOSSAPP-902; spec:
docs/product/AUTONOMOUS_AGENTS_SPEC.md; design: boss-autonomous-agents-design.md).

BOSS already had event ledgers, schedules, wakeups, focused agents, a shepherd, a doctor,
self-fix, a ranked work-item table, budgets, grants and receipts. What it lacked was the layer
that joins them: one place every signal lands, one board the agents share, one ledger of what
they decided and why, one policy for how much they may do alone, one envelope and one stop.
This package is that layer. It adds no executor and no second decision engine: agents still
decide through the one agent path, and work still runs as workflows and builds.

    signals.py      one normalised, deduplicated signal inbox + the pull-adapter sweep
    adapters.py     the first five adapters (app alerts, failed steps/builds, the doctor,
                    shepherd escalations, stale PRs)
    board.py        ambient work items on work_items: lifecycle, owner leases, takeover, links
    collab.py       typed board messages and consensus by risk
    ledger.py       decisions as agent_acts of kind 'decision' (append-only, override chain)
    policy.py       autonomy per task class (earned), the gate, the envelope, the stop
    guards.py       loop/duplicate guards: depth, cooldown, caps, concurrency, circuit breaker
    task_classes.py the task-class and action catalogue (code, not config)
    read_model.py   Agent Operations: per-item flow graph, filtered lists, the scorecard
"""
from . import (adapters, board, collab, guards, ledger, policy, signals,  # noqa: F401
               task_classes)

__all__ = ["adapters", "board", "collab", "guards", "ledger", "policy", "signals",
           "task_classes"]
