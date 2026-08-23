"""What BOSS DOES for the requests an operator actually makes.

Every agent defect this week was found by the operator, not by a test:

  * "fix and retry the deployment"      -> an essay ending in "let me know if you would like me to"
  * "list the tools that CHANGE things" -> spawned a governed code task
  * "fix the incorrect URL"             -> wrote an invented domain to the database
  * "deploy living colors to live"      -> silently started a REBUILD and called it a deploy
  * "what are my options"               -> proposed a button with run_id="crun_placeholder"

They share a shape: the reply READS fine and the wrong tool ran. No assertion about text can
catch that, which is why none of them was caught before shipping.

So this suite asserts on the DECISION — which tools executed, which were merely offered — using
the same registry and the same PROPOSE_ONLY rules the live agent uses. It is deliberately
model-free: it does not need a model to answer, because the properties being pinned are
structural (what is executable, what is offered, what an offer must contain). A model-driven
end-to-end pass belongs in a separate, slower suite; this one runs in CI on every commit and
would have caught four of the five above.
"""

import pytest

from boss_engine.agent.engine_agent import EngineAgent, _propose_label
from boss_engine.tools.builtin import (
    _DISCUSS_OPERATIONS_TOOLS,
    _placeholder_argument,
    build_default_registry,
    build_discuss_read_registry,
)


# --------------------------------------------------------------------------- #
# Starting expensive work is always an OFFER
# --------------------------------------------------------------------------- #
def test_the_expensive_tools_are_named_here_not_just_counted():
    """Guarding a set by iterating it is self-defeating: empty the set and the parametrized
    tests below do not fail, they DISAPPEAR (an empty parametrize is a skip). Mutation-testing
    this file found exactly that. So name them, and make removing one a failure."""
    assert {"create_code_task", "create_discovery", "request_meeting",
            "retry_code_task", "extend_code_task"} <= EngineAgent.PROPOSE_ONLY


@pytest.mark.parametrize("tool", sorted(EngineAgent.PROPOSE_ONLY))
def test_expensive_work_is_never_taken_unasked(tool):
    """"Deploy it" must not become "build it". The agent tried exactly that, twice, after being
    told not to in the system prompt AND in redeploy's own failure message."""
    reg = build_discuss_read_registry()
    agent = EngineAgent.__new__(EngineAgent)
    agent.registry = reg
    pending: list[dict] = []
    out = agent._run_tool(tool, {"title": "x", "run_id": "crun_abc"},
                          project_id="p", actor="ceo", allow_writes=True, pending=pending)
    assert out.get("proposed") is True, f"{tool} executed instead of being offered"
    assert pending and pending[0]["tool"] == tool


@pytest.mark.parametrize("tool", ["redeploy", "start_preview", "stop_preview", "cancel_job",
                                  "set_job_priority", "close_orphaned_runs", "decide",
                                  "waive_gates", "write_db"])
def test_cheap_or_reversible_actions_still_run_directly(tool):
    """The offer mechanism must not become a new way to stall. These are what "fix it" means."""
    assert tool not in EngineAgent.PROPOSE_ONLY, (
        f"{tool} behind a button would make BOSS ask permission to do its job")


def test_propose_only_is_a_subset_of_what_boss_may_run():
    """Offering something BOSS cannot execute produces a dead button."""
    assert EngineAgent.PROPOSE_ONLY <= set(_DISCUSS_OPERATIONS_TOOLS)


def test_a_proposal_says_plainly_that_nothing_ran():
    """It described starting a build as "I've retried the failed task, which will redeploy" —
    the operator could not tell a build had begun."""
    agent = EngineAgent.__new__(EngineAgent)
    agent.registry = build_discuss_read_registry()
    out = agent._run_tool("create_code_task", {"title": "Build Living Colors"},
                          project_id="p", actor="ceo", allow_writes=True, pending=[])
    assert "NOT run" in out["note"]
    assert "do not describe it as something you have already done" in out["note"].lower()


# --------------------------------------------------------------------------- #
# A button has to be pressable
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("label_args,expect", [
    (("create_code_task", {"title": "Build Living Colors"}), "Build: Building"),
    (("retry_code_task", {"run_id": "crun_a57b42adc0884dc4bfda"}), "Retry crun_"),
    (("request_meeting", {}), "Convene"),
])
def test_a_button_reads_as_an_action(label_args, expect):
    tool, args = label_args
    label = _propose_label(tool, args)
    assert label and label != tool, "a raw tool name is not a button label"


@pytest.mark.parametrize("args", [
    {"run_id": "crun_placeholder"},
    {"repository": "owner/name"},          # seen live in a proposal
    {"domain": "living-colors.example.com"},
])
def test_an_offer_with_invented_arguments_is_refused(args):
    assert _placeholder_argument(args), f"{args} would produce a button that fails on click"


# --------------------------------------------------------------------------- #
# The capability surface an operator relies on
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("ask,tool", [
    ("retry the deployment",        "redeploy"),
    ("start the preview",           "start_preview"),
    ("cancel that stuck job",       "cancel_job"),
    ("approve that gate",           "decide"),
    ("this run is orphaned",        "close_orphaned_runs"),
    ("why did the process die",     "inspect_self"),
    ("read the worker logs",        "read_logs"),
    ("is anything starving",        "find_starved_work"),
])
def test_boss_owns_a_tool_for_what_operators_ask(ask, tool):
    """Each of these was, at some point this week, a request BOSS could not act on because the
    tool was absent or unreachable — and it answered with advice instead."""
    assert tool in build_discuss_read_registry().names(), f"{ask!r} has no tool: {tool}"


def test_no_write_is_registered_but_unreachable():
    """Owning eighteen tools and being allowed five is how "it only recommends" happened."""
    reg_writes = {s.name for s in build_default_registry().specs(kind="write")}
    assert reg_writes == set(_DISCUSS_OPERATIONS_TOOLS), (
        f"unreachable: {sorted(reg_writes - set(_DISCUSS_OPERATIONS_TOOLS))}")


def test_the_ability_to_act_is_not_narrowed_again():
    """The regression that started all of this was a five-tool allowlist. Re-narrowing it must
    fail HERE with a readable message — mutation-testing showed it otherwise surfaced only as a
    collection error in an unrelated test, which reads like a broken suite, not a broken rule."""
    allowed = set(_DISCUSS_OPERATIONS_TOOLS)
    for tool in ("redeploy", "write_db", "start_preview", "cancel_job", "decide"):
        assert tool in allowed, f"BOSS can no longer {tool} — it will go back to giving advice"
    assert len(allowed) >= 15, f"write surface shrank to {len(allowed)} tools"
