"""THE BARE LEVEL (BOSSAPP-977): the plain-coding-agent baseline, in dollars.

Every other bench level runs BOSS's governed pipeline, so "BOSS vs a plain coding agent on the
same tasks" could not be shown with numbers. `--level bare` hands the task brief to the ENGINE'S
OWN LOOP — one session of claude_code, codex, or any registered harness, through the same
engine abstraction the pipeline uses (`run_role`), in an isolated clone of the task's fixture.
No pipeline, no stages, no review panel, no gates, no approvals, no repair rounds, no PR.

The BENCH still does its part, exactly as on the governed levels:
  * it seeds the same pinned fixture into the local GitHub;
  * it runs the SAME acceptance checks afterwards (the task's own tests and the held tests the
    agent never sees), on a clean checkout of the branch the bench made from the session's work;
  * it records wall time, tokens and dollars read from the engine's own telemetry — the
    {usage, cost_usd_micros, turns} every engine's `run_role` already returns (Claude Code: its
    session/cost output; codex: its event stream, priced at list).

HONESTY. An engine that does not report a number gets UNKNOWN (null in the report, named in the
result's `telemetry_unknown`), never a fabricated zero. The stored numeric columns coerce null
to 0 — the report's `detail` JSON and the `telemetry_unknown` list are the record of what was
actually known.

This module holds the parts that need no world (brief, metrics, components), so they can be
tested without a database; scripts/harness_bench.py owns the orchestration (seed, clone, session,
branch, acceptance, store).
"""
from __future__ import annotations

from typing import Any, Optional

#: The one session's turn budget. A plain coding agent runs until it is done; the pipeline's
#: per-stage default (code_agent_max_turns, 40) is a budget for ONE stage of many, so the bare
#: session — which is every stage at once — gets a larger, fixed cap. Fixed, so two bare runs
#: are configured alike and the config fingerprint says so.
BARE_MAX_TURNS = 80

#: The usage keys an engine's telemetry reports token counts in (llm_calls' split).
_TOKEN_KEYS = ("prompt_tokens", "completion_tokens", "cache_read_tokens", "cache_write_tokens")


def bare_brief(task: dict[str, Any]) -> str:
    """The task handed to the engine's own loop: the SAME brief fields the governed pipeline
    receives (title, instructions, acceptance criteria, test commands), as a person would paste
    them into a plain coding agent. No BOSS repo-context pack, no pipeline stage framing —
    whatever rules an engine's own adapter appends to every invocation apply to every lane
    alike and are part of that engine, not of the pipeline being compared away."""
    criteria = "\n".join(f"- {c}" for c in task.get("acceptance_criteria") or []) or "- (none)"
    tests = "\n".join(f"- {c}" for c in task.get("test_commands") or []) or "- (none)"
    return (
        "You are working in a checked-out git repository (your current working directory). "
        "Implement the task below by editing files directly with your tools, then run the "
        "tests to verify.\n\n"
        f"TASK: {task['title']}\n\n"
        f"{str(task['instructions']).strip()}\n\n"
        f"ACCEPTANCE CRITERIA:\n{criteria}\n\n"
        f"TEST/BUILD COMMANDS (run these and make them pass):\n{tests}\n\n"
        "Edit files in place; do not create a branch, commit, or push. When done, end with a "
        "concise summary of what you changed.\n"
    )


def bare_metrics(session: Optional[dict[str, Any]], *,
                 wall_s: float) -> tuple[dict[str, Any], list[str]]:
    """One bare session's metrics, read from the engine's own telemetry (`run_role`'s result),
    in the exact shape metrics.measure() gives a pipeline task, so the same results store,
    compare table and CI guard read them unchanged.

    Returns (metrics, unknown): `unknown` names every metric the engine did not report, which
    is then null in the report — recorded as unknown, never fabricated. There is no pipeline,
    so repair_rounds and interventions are exactly 0 by construction, not unknown."""
    s = dict(session or {})
    usage = dict(s.get("usage") or {})
    unknown: list[str] = []
    metrics: dict[str, Any] = {"wall_s": round(float(wall_s), 2)}
    if any(usage.get(k) is not None for k in _TOKEN_KEYS):
        crt = int(usage.get("cache_read_tokens") or 0)
        cwt = int(usage.get("cache_write_tokens") or 0)
        tokens_in = int(usage.get("prompt_tokens") or 0) + crt + cwt
        metrics.update(tokens_in=tokens_in, tokens_out=int(usage.get("completion_tokens") or 0),
                       cache_read_tokens=crt, cache_write_tokens=cwt,
                       cache_share=(round(crt / tokens_in, 6) if tokens_in else 0.0))
    else:
        metrics.update(tokens_in=None, tokens_out=None, cache_read_tokens=None,
                       cache_write_tokens=None, cache_share=None)
        unknown += ["tokens_in", "tokens_out", "cache_share"]
    cost_micros = s.get("cost_usd_micros")
    if s.get("cost_unreported") or cost_micros is None:
        metrics["cost_usd"] = None
        unknown.append("cost_usd")
    else:
        metrics["cost_usd"] = int(cost_micros) / 1_000_000
    metrics.update(
        model_calls=1,                           # one session: the engine's own loop, once
        turns=int(s.get("turns") or 0),          # the engine's own turn count
        tool_calls=int(s.get("tool_calls") or 0),
        repair_rounds=0,                         # no pipeline: nothing ran a fix round
        continuations=0,
        lineage_runs=1,
        interventions=0,                         # no pipeline: nothing asked a person anything
    )
    return metrics, unknown


def bare_components(*, session_ok: bool, changed: bool, acceptance_passed: bool,
                    timed_out: bool = False) -> dict[str, bool]:
    """What success means with no pipeline: the session finished, it changed the repository,
    and the benchmark's own acceptance checks passed on a clean checkout. The pipeline's
    components (pr_opened, verify_passed, review_passed, gates_passed) do not exist here and
    are not pretended."""
    components = {
        "completed": bool(session_ok),
        "changed": bool(changed),
        "acceptance_passed": bool(acceptance_passed),
    }
    if timed_out:
        components["within_time_limit"] = False
    return components
