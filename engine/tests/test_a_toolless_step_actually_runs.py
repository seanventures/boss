"""A step with no tool must still do its job, or say plainly that it has none.

PRODUCTION LAUNCH READINESS NEVER RAN. `plrr` declares `tool=None` — it has no single tool,
because it reads the run's own spec and then starts the repair that closes what the app is
missing. Its dispatch sat AFTER the `tool is None` branch, which returns unconditionally, so it
was unreachable from the day it was written. Every run recorded:

    "'Production launch readiness' is recorded from the work it accompanies."

started and completed in the same second, green, having done nothing. The operator spotted it
from the timing alone:

    "it took 0s which means it did not run at all.. is it doing anything in the workflow step
     now ?"

It was not. The step that exists to ask whether anyone can operate the thing at 3am — metrics
per feature, /metrics, health and readiness, failure behaviour, rollback — was the one step
that never ran, while reporting success and declaring three outputs it never produced.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from boss_engine.services.workflow_capabilities import CAPABILITIES
from boss_engine.services.workflow_capability_runner import _OBSERVED_GATES

SOURCE = (
    Path(__file__).resolve().parents[1]
    / "boss_engine/services/workflow_capability_runner.py"
).read_text(encoding="utf-8")


def _toolless_branch() -> str:
    """The body of `if capability.tool is None:` — everything a tool-less step can reach."""
    start = SOURCE.index("    if capability.tool is None:")
    end = SOURCE.index("    # ADOPT, DO NOT DUPLICATE.", start)
    return SOURCE[start:end]


def _handled_in_branch() -> set[str]:
    return set(re.findall(r'key == "([a-z_]+)"', _toolless_branch()))


#: Tool-less capabilities that genuinely have nothing to do themselves. `monitor` and `notify`
#: are executed elsewhere; the rest report what the work beside them recorded. Listed by name
#: so that adding a capability with real work to do and forgetting to wire it FAILS here.
DELIBERATE_NO_OPS = {"video_edit", "social_publish", "monitor", "notify"}


def test_production_launch_readiness_is_reachable():
    """The regression itself. `plrr` must be dispatched from inside the branch that runs for
    tool-less capabilities, not after it."""
    assert 'key == "plrr"' in _toolless_branch(), (
        "plrr is declared tool=None, so a dispatch placed after the tool-is-None branch can "
        "never run: that branch returns for every path through it"
    )


def test_the_dead_dispatch_is_gone_rather_than_duplicated():
    """Two dispatches for one key is how the next person 'fixes' the reachable one and leaves
    the dead one behind to confuse the one after that."""
    assert SOURCE.count('if key == "plrr":') == 1


def test_every_tool_less_capability_is_either_run_observed_gated_or_named():
    """The invariant that would have caught this.

    A capability with no tool has exactly four honest fates: it is dispatched inside the
    branch, it reports a gate the build already recorded, it waits for a person, or it is
    named here as having nothing to do. Anything else reports success for work that did not
    happen — which is what plrr did, silently, on every run.
    """
    unexplained = []
    handled = _handled_in_branch()
    for key, capability in CAPABILITIES.items():
        if capability.tool is not None:
            continue
        if getattr(capability, "is_gate", False):
            continue
        if key in handled or key in _OBSERVED_GATES or key in DELIBERATE_NO_OPS:
            continue
        unexplained.append(key)
    assert not unexplained, (
        f"tool-less capabilities that run nothing and are not declared no-ops: {unexplained}. "
        "Each will report success having done nothing."
    )


def test_a_capability_that_promises_outputs_does_not_silently_produce_none():
    """plrr declares findings, gaps_closed and code_task_run_id. A step declaring outputs and
    reaching the no-op return produces none of them while reporting success — and the steps
    bound to those outputs then read empty."""
    promising = {
        key for key, c in CAPABILITIES.items()
        if c.tool is None and c.produces and not getattr(c, "is_gate", False)
        and key not in DELIBERATE_NO_OPS
    }
    handled = _handled_in_branch() | set(_OBSERVED_GATES)
    assert promising <= handled, (
        f"these promise outputs but reach the no-op: {sorted(promising - handled)}"
    )


def test_the_no_op_still_says_so_rather_than_claiming_work():
    """The fallback is not the bug — a step with genuinely nothing to do should say that. The
    bug was reaching it by accident."""
    branch = _toolless_branch()
    assert "is recorded from the work it accompanies" in branch


@pytest.mark.parametrize("promise", ["metrics", "health", "rollback"])
def test_the_readiness_brief_still_asks_for_what_it_was_written_to_ask(promise):
    """Now that it runs, what it asks for matters. These are the operator's own words for what
    production readiness means: signals per feature, an endpoint that says whether it can
    serve, and a release that can be undone."""
    brief_start = SOURCE.index("def _plrr_brief(")
    brief = SOURCE[brief_start:SOURCE.index("\ndef ", brief_start + 1)]
    assert promise in brief.lower()
