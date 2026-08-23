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

import pathlib

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


# --------------------------------------------------------------------------- #
# A promise of a control the operator cannot see
# --------------------------------------------------------------------------- #
from boss_engine.services.intent_router import _no_button_was_promised  # noqa: E402

# Verbatim from production, 2026-08-24, "deploy living colors to live". BOSS declined to
# substitute a rebuild (correct) and then told the operator to click buttons that did not exist.
_LIVE_REPLY = """The **redeploy** tool could not run because the "Living Colors" app has never \
been deployed before.

| Action | What it does |
|--------|--------------|
| **Create a deployment** | Ships the existing successful build. |
| **Cancel** | Do nothing right now. |

Please choose the appropriate button."""


def test_the_reply_that_shipped_promising_absent_buttons():
    out = _no_button_was_promised(_LIVE_REPLY, [])
    assert "choose the appropriate button" not in out.lower()
    assert "nothing to click" in out
    assert "Create a deployment" in out, "the useful explanation must survive"


def test_the_promise_stands_when_buttons_are_real():
    proposed = [{"id": "act_1", "tool": "redeploy", "args": {"run_id": "crun_x"}}]
    assert _no_button_was_promised(_LIVE_REPLY, proposed) == _LIVE_REPLY


@pytest.mark.parametrize("reply", [
    "I redeployed it; the health check passed.",
    "Living Colors has no deployment yet.",
    "",
])
def test_ordinary_replies_are_untouched(reply):
    assert _no_button_was_promised(reply, []) == reply


# --------------------------------------------------------------------------- #
# A promise to come back, from something that cannot come back
# --------------------------------------------------------------------------- #
from boss_engine.services.intent_router import _boss_cannot_call_you_back  # noqa: E402


@pytest.mark.parametrize("reply", [
    "I've started the redeploy.\nI'll monitor the deployment and update you when it completes.",
    "Retrying now.\nI will keep an eye on it and let you know how it goes.",
    "Kicked off.\nI'm going to watch the health check and report back.",
])
def test_boss_does_not_promise_to_message_you_later(reply):
    """BOSS is turn-scoped: nothing wakes it after it replies. Promising to "update you" makes
    the operator WAIT instead of asking again — the most expensive possible false statement."""
    out = _boss_cannot_call_you_back(reply)
    assert "update you" not in out.lower() or "cannot watch this" in out
    assert "ask me again" in out.lower()
    assert out.splitlines()[0] == reply.splitlines()[0], "the real action must survive"


@pytest.mark.parametrize("reply", [
    "The deployment failed the health check after 120s.",
    "Ask me again and I'll check the run page for the current status.",
    "",
])
def test_replies_that_promise_nothing_are_untouched(reply):
    assert _boss_cannot_call_you_back(reply) == reply


def test_write_db_tells_the_model_it_is_the_no_dedicated_tool_escape_hatch():
    """Asked to deploy to production, BOSS found the app had only a staging target and said
    creating one was a "custom admin action (requires a human to add a row)". It was holding
    write_db, which does exactly that -- verified: the INSERT passes _is_single_write. The model
    only sees tool DESCRIPTIONS, so "there is no create-target tool" has to be answered where it
    is reading: in the description of the tool that is the general case."""
    spec = {s.name: s for s in build_discuss_read_registry().specs()}["write_db"]
    d = spec.description.lower()
    assert "requires a human" in d and "no dedicated tool" in d
    assert "deployment_targets" in d, "name the case that actually came up"


# --------------------------------------------------------------------------- #
# A tool result must never destroy the turn on the way to the model
# --------------------------------------------------------------------------- #
import datetime as _dt  # noqa: E402
import decimal as _dec  # noqa: E402
import json as _json  # noqa: E402
import uuid as _uuid  # noqa: E402

from boss_engine.agent.engine_agent import _jsonable  # noqa: E402


@pytest.mark.parametrize("value,expect", [
    (_dec.Decimal("12.34"), 12.34),
    (_dec.Decimal("0"), 0.0),
])
def test_money_survives_the_trip_to_the_model(value, expect):
    """"What is my spend so far this month?" -> "TypeError: Object of type Decimal is not JSON
    serializable". The tool worked and read the real numbers; encoding them killed the answer.
    Decimal becomes float because the model has to compare and add it."""
    assert _jsonable(value) == expect


def test_a_realistic_cost_row_encodes():
    row = {
        "project_id": "boss-dogfood",
        "spend_usd": _dec.Decimal("41.9900"),
        "budget_usd": _dec.Decimal("250"),
        "as_of": _dt.datetime(2026, 8, 24, 19, 52, tzinfo=_dt.timezone.utc),
        "run_id": _uuid.UUID("1b4e28ba-2fa1-11d2-883f-0016d3cca427"),
        "tags": {"api", "worker"},
    }
    out = _json.loads(_json.dumps(row, default=_jsonable))
    assert out["spend_usd"] == 41.99 and out["budget_usd"] == 250.0
    assert out["as_of"].startswith("2026-08-24T19:52")
    assert out["tags"] == ["api", "worker"]


def test_an_unknown_object_degrades_instead_of_raising():
    """A tool returning something exotic must cost fidelity, never the whole answer."""
    class Odd:
        def __str__(self): return "odd-value"
    assert _json.loads(_json.dumps({"x": Odd()}, default=_jsonable))["x"] == "odd-value"


# --------------------------------------------------------------------------- #
# A money figure has to be reproducible
# --------------------------------------------------------------------------- #


def test_the_spend_total_is_not_the_sum_of_one_page():
    """Asked "what is my spend this month" five times, BOSS answered $258.44, $19.56, 0.9141 and
    40.2165. total_cost_usd was sum(runs) and `runs` is LIMIT-ed to the 25 most recent, so the
    "total" was the total of a PAGE. The aggregate must not depend on the page size."""
    import inspect
    from boss_engine.tools import builtin
    src = inspect.getsource(builtin)
    i = src.index('"get_costs"')
    body = src[i:i + 4200]
    assert 'sum(r["cost_usd"] for r in runs)' not in body, (
        "the total is being summed from the truncated page again")
    assert "COALESCE(sum(cost_usd_micros), 0)" in body, "no window-wide aggregate"
    assert "runs_truncated" in body, "a truncated list must say so"


def test_the_window_is_stated_so_it_cannot_be_called_a_month():
    """It reported a `days`-windowed figure as "this month (last 30 days)". Those differ, and the
    operator is reading it as a bill."""
    import inspect
    from boss_engine.tools import builtin
    src = inspect.getsource(builtin)
    i = src.index('"get_costs"')
    body = src[i:i + 4200]
    assert "window_description" in body
    assert "not a calendar month" in body


# --------------------------------------------------------------------------- #
# A table is not a button — recover the one the model described
# --------------------------------------------------------------------------- #
from boss_engine.services.intent_router import _recover_described_actions  # noqa: E402

_TOOLS = set(build_discuss_read_registry().names())

# Verbatim shape from production, 2026-08-24. BOSS wrote this and called nothing, then said
# "Please click the button above."
_DESCRIBED = """I can't deploy: the run was cancelled and never produced a build.

**Proposed action**

| Button label | Tool | Arguments | What it does (cost) |
|---|---|---|---|
| **Retry Build for Living Colors** | `retry_code_task` | { "run_id": "crun_a796fafaddd74427a0b8", "reason": "Retry after cancellation" } | Re-runs the cancelled task. This will spend compute (charged). |

Please click the button above to start the retry."""


def test_the_described_button_becomes_a_real_one():
    out = _recover_described_actions(_DESCRIBED, _TOOLS, [])
    assert len(out) == 1, out
    a = out[0]
    assert a["tool"] == "retry_code_task"
    assert a["args"] == {"run_id": "crun_a796fafaddd74427a0b8",
                         "reason": "Retry after cancellation"}
    assert a["label"] == "Retry Build for Living Colors"


def test_recovery_never_overrides_a_real_offer():
    existing = [{"id": "act_1", "tool": "redeploy", "args": {}}]
    assert _recover_described_actions(_DESCRIBED, _TOOLS, existing) == []


@pytest.mark.parametrize("bad", [
    # invented argument — the exact failure the placeholder guard exists for
    _DESCRIBED.replace("crun_a796fafaddd74427a0b8", "crun_placeholder"),
    # a tool that does not exist
    _DESCRIBED.replace("retry_code_task", "deploy_to_production_now"),
    # arguments that are not parseable
    _DESCRIBED.replace('{ "run_id": "crun_a796fafaddd74427a0b8", "reason": "Retry after cancellation" }',
                       "the run id of the cancelled task"),
])
def test_an_uncertain_row_produces_no_button_rather_than_a_wrong_one(bad):
    """A button is an authorisation. Wrong is worse than absent."""
    assert _recover_described_actions(bad, _TOOLS, []) == []


def test_ordinary_tables_are_not_mistaken_for_offers():
    """BOSS answers with evidence tables constantly; none of them are proposals."""
    evidence = """| run | status | cost |\n|---|---|---|\n| crun_a | failed | $1.20 |"""
    assert _recover_described_actions(evidence, _TOOLS, []) == []


def test_the_prompt_forbids_the_table_that_pretends_to_be_a_button():
    """The recovery above is a safety net, not the plan: the model should call the tool."""
    from engine.tests._source import prompt_text
    from boss_engine.services import intent_router
    text = prompt_text(intent_router).lower()
    assert "that table is not a button" in text
    assert "most answers are one to three sentences" in text


def test_the_operator_correction_outranks_the_pinned_run():
    """"No, use the latest working one — the preview works fine" was ignored twice: the app
    context block ended with "use your tools with run_id X", which reads as a restriction."""
    from engine.tests._source import prompt_text
    from boss_engine.services import intent_router
    text = prompt_text(intent_router).lower()
    assert "the correction wins" in text
    assert "never return the same conclusion twice" in text
    assert "a preview is serving, a build exists" in text


def test_the_console_context_block_does_not_pin_the_run():
    """The block is built in the web app, so the rule has to hold there too."""
    src = pathlib.Path("apps/web/app/code-tasks-ui.tsx").read_text(encoding="utf-8")
    i = src.index("[app context]")
    block = src[i:i + 3000]
    assert "STARTING POINT, not a restriction" in block
    assert "correction outranks this block" in block
    assert "latest_successful_run" in src
