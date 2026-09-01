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
import re

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


def test_the_scope_note_does_not_pin_the_run():
    """Same rule, new home.

    The console used to build this as a "[app context]" block and prepend it to the operator's
    message — visible to them, and useless for the questions it existed to answer, because a
    summary of a run is not a PRD. Scope now travels as `external_ref` and the server writes it
    into the SYSTEM prompt. The rule it carried has to survive the move: naming a run must not
    stop BOSS looking at the app's other runs when the operator says to.
    """
    from boss_engine.services import intent_router
    src = pathlib.Path(intent_router.__file__).read_text(encoding="utf-8")
    note = src[src.index("def _scope_note"):src.index("def _answer(")]
    assert "STARTING POINT, not a restriction" in note
    assert "correction outranks this block" in note
    # And it says nothing at all on a general thread, rather than inventing a subject.
    assert "if not external_ref:" in note and 'return ""' in note


def test_the_console_no_longer_prepends_context_to_the_operators_message():
    """What the operator sends is what the operator typed."""
    src = pathlib.Path("apps/web/app/agent-ui.tsx").read_text(encoding="utf-8")
    assert 'fd.set("message", typed);' in src
    # Nothing composes the block into an outgoing message any more...
    assert "APP_FACTS_END}" not in src
    assert "${appFacts}" not in src
    # ...but the READERS stay. Threads sent before this change still hold the block in their
    # stored text, and deleting the stripper would un-hide it in every old conversation —
    # fixing today's transcripts by breaking yesterday's.
    assert "stripAppFacts" in src


@pytest.mark.parametrize("reply", [
    # Each of these is verbatim from production, in the order the model invented them as the
    # previous one was blocked. The rule is the ACT of promising a control, not one wording.
    "Here are your options.\n\nPlease choose the appropriate button.",
    "Deploying is not possible yet.\n\n**[Start deployment build]** (click to create a new build)",
    "You can proceed.\n\nClick to start the retry when you are ready.",
])
def test_every_shape_of_fake_button_is_caught(reply):
    out = _no_button_was_promised(reply, [])
    assert "nothing to click" in out, f"still promising a control: {out!r}"
    assert out.splitlines()[0] == reply.splitlines()[0], "the real answer must survive"


# --------------------------------------------------------------------------- #
# A refusal has to be a conversation, not a wall
# --------------------------------------------------------------------------- #
def test_the_cannot_deploy_message_states_what_exists_and_what_is_missing():
    """"I know there's only a staging target — I'm expecting the BOSS agent to communicate that
    to me." Asserted on the OUTPUT, not on where the text lives: the first version of this
    matched a byte range inside _act_on_deployment and broke the moment the wording moved into
    a function, which tells you it was pinning a location rather than a behaviour.

    The exhaustive per-state coverage is in test_deployment_scenarios; this keeps the one case
    that actually reached an operator."""
    from boss_engine.services.shepherd_service import describe_undeployable
    out = describe_undeployable(
        status="completed", repo="paramg/boss-sandbox",
        targets=[{"environment": "staging", "status": "active"}])
    assert "staging" in out                                  # what exists
    assert "no production/live target" in out                # what is missing
    assert "create the missing production target" in out     # what they can do
    assert "TELL THE OPERATOR THIS STATE PLAINLY" in out
    assert "propose_action" in out, "an option in prose is not an option they can take"


def test_it_still_refuses_to_turn_a_deploy_into_a_build():
    """The guardrail that started this must survive the rewrite — in every state."""
    from boss_engine.services.shepherd_service import describe_undeployable
    for targets in ([], [{"environment": "staging", "status": "active"}],
                    [{"environment": "production", "status": "paused"}]):
        out = describe_undeployable(status="cancelled", repo="r/r", targets=targets)
        assert "DO NOT substitute a build for a deploy" in out


# --------------------------------------------------------------------------- #
# The console must not eject a working session over a link typo
# --------------------------------------------------------------------------- #
def test_no_console_link_uses_the_wrong_org_parameter():
    """The page reads ?org=. A link carrying ?org_id= resolves to no org, drops the operator on
    the picker mid-session, and the stored-org effect bounces them back a second later — it
    reads as the console logging you out at random. One shipped link did exactly this."""
    web = pathlib.Path("apps/web/app")
    offenders = []
    for f in sorted(web.glob("*.tsx")) + sorted(web.glob("*.ts")):
        for n, line in enumerate(f.read_text(encoding="utf-8").splitlines(), 1):
            if "org_id=" not in line:
                continue
            if not any(k in line for k in ("href", "url", "URL", "push(", "replace(")):
                continue          # reading a param, not building a link
            if "resolvedSearchParams" in line or "getParam" in line:
                continue
            offenders.append(f"{f.name}:{n}: {line.strip()[:90]}")
    assert not offenders, "links must use ?org=, not ?org_id=:\n" + "\n".join(offenders)


def test_the_page_still_accepts_org_id_from_old_links():
    """Bookmarks and already-sent links carrying org_id must not eject anyone."""
    src = pathlib.Path("apps/web/app/page.tsx").read_text(encoding="utf-8")
    assert "resolvedSearchParams?.org_id" in src, "org_id alias not honoured"


# --------------------------------------------------------------------------- #
# A blip must not tell the operator their workspace is gone
# --------------------------------------------------------------------------- #
def test_an_empty_org_list_retries_instead_of_offering_onboarding():
    """The picker appeared mid-session with the BOSS org MISSING and the "Onboard a new
    organization" form open. fetchOrgs reports apiReachable:true whenever the call returns at
    all, so a degraded read yielding zero orgs is indistinguishable from an empty account —
    and OrgGate opens onboarding when the list is empty. It reads as data loss."""
    src = pathlib.Path("apps/web/app/page.tsx").read_text(encoding="utf-8")
    assert "orgs.items.length === 0" in src, "an empty list is still treated as fact"
    i = src.index("orgs.items.length === 0")
    assert "OrgReconnect" in src[i:i + 900], "empty list does not route to the retrying view"


def test_a_bad_org_link_still_reaches_the_picker():
    """The retry must not become a trap: a list that DID load and simply lacks this org is a
    real mismatch, not a blip, and has to fall through rather than retry forever."""
    src = pathlib.Path("apps/web/app/page.tsx").read_text(encoding="utf-8")
    assert "OrgGate orgs={orgs.items}" in src


def test_the_agent_page_lists_conversations_not_tasks():
    """Tasks have their own Ask BOSS panel on the card; listing them here too split one
    conversation across two entry points and left a single reusable "General chat" slot."""
    import re
    src = pathlib.Path("apps/web/app/agent-ui.tsx").read_text(encoding="utf-8")
    # Assert on CODE, not on the comment explaining what was removed — the first version of
    # this matched the word in its own rationale, which is the same mistake as asserting on a
    # docstring.
    code = re.sub(r"/\*.*?\*/", "", src, flags=re.S)
    code = re.sub(r"^\s*//.*$", "", code, flags=re.M)
    assert "New chat" in code and "generalThreads" in code
    assert "General chat" not in code, "the single shared general slot is still rendered"
    assert "!t.external_ref" in code, "task threads are not excluded from the chat list"


def test_the_chat_does_not_revalidate_the_whole_console():
    """"It showed the answer but why is it still working on it?" — the reply painted and the
    Working bubble kept spinning for another 15s. agentChatAction calls revalidatePath("/"),
    so the transition stays pending while the entire 8 MB console re-renders, and `busy` is
    driven by that transition. Both chat surfaces use the non-revalidating action."""
    import re
    for name in ("agent-ui.tsx", "ask-boss-hero.tsx"):
        src = pathlib.Path(f"apps/web/app/{name}").read_text(encoding="utf-8")
        code = re.sub(r"/\*.*?\*/", "", src, flags=re.S)
        code = re.sub(r"^\s*//.*$", "", code, flags=re.M)
        assert "askBossAction(" in code, f"{name} does not use the non-revalidating action"
        assert "agentChatAction(" not in code, f"{name} still revalidates the world"


def test_ask_boss_uses_the_consoles_standard_header():
    """"This looks a little different from other formats on the page." It was a bespoke card
    with its own radius, gradient and type scale; it now uses the same eyebrow/title/meta
    header and cap-badge vocabulary as the Meeting room."""
    src = pathlib.Path("apps/web/app/ask-boss-hero.tsx").read_text(encoding="utf-8")
    for cls in ("mr-header", "mr-header-main", "mr-header-meta", "mr-title",
                "eyebrow", "cap-badge tone-", "mr-meta-item", "mr-avatar tone-"):
        assert cls in src, f"not using the shared primitive: {cls}"


# --------------------------------------------------------------------------- #
# The third invented shape: the tool call written as markup
# --------------------------------------------------------------------------- #
from boss_engine.services.intent_router import _recover_tagged_actions  # noqa: E402

# Verbatim from production. After "choose the appropriate button", the markdown table, and the
# bracket pseudo-button were each handled, the model started emitting THIS — which renders as
# raw angle brackets in the operator's chat and still produces no button.
_TAGGED = """Only staging targets exist, so "deploy to live" has nowhere to ship the build.

| Action | Button |
|--------|--------|
| Deploy to the existing staging environment | <propose_action tool="redeploy" args='{"run_id":"crun_dad37a91c4b0470480fd","reason":"Deploy to staging as requested"}' label="Deploy to Staging" why="Ships the latest successful run"/> |
"""


def test_a_tool_call_written_as_markup_becomes_a_button():
    out, cleaned = _recover_tagged_actions(_TAGGED, _TOOLS, [])
    assert len(out) == 1, out
    assert out[0]["tool"] == "redeploy"
    assert out[0]["args"]["run_id"] == "crun_dad37a91c4b0470480fd"
    assert out[0]["label"] == "Deploy to Staging"


def test_the_raw_markup_never_reaches_the_operator():
    """Leaving the tag in shows angle brackets where a button belongs — worse than nothing."""
    _, cleaned = _recover_tagged_actions(_TAGGED, _TOOLS, [])
    assert "propose_action" not in cleaned
    assert "<" not in cleaned.split("| Action | Button |")[-1]
    assert "Deploy to the existing staging environment" in cleaned, "the row's text must survive"


def test_a_tag_naming_an_unknown_tool_is_stripped_but_not_offered():
    bad = _TAGGED.replace('tool="redeploy"', 'tool="ship_it_now"')
    out, cleaned = _recover_tagged_actions(bad, _TOOLS, [])
    assert out == []
    assert "propose_action" not in cleaned, "unparseable markup must still be removed"


def test_a_tag_with_an_invented_argument_is_refused():
    bad = _TAGGED.replace("crun_dad37a91c4b0470480fd", "crun_placeholder")
    out, _ = _recover_tagged_actions(bad, _TOOLS, [])
    assert out == []


def test_a_reply_with_no_tag_is_returned_untouched():
    plain = "The deployment failed its health check after 120s."
    out, cleaned = _recover_tagged_actions(plain, _TOOLS, [])
    assert out == [] and cleaned == plain


# --------------------------------------------------------------------------- #
# Starter briefs: draft for editing, never spend on a click
# --------------------------------------------------------------------------- #
def _hero_src() -> str:
    return pathlib.Path("apps/web/app/ask-boss-hero.tsx").read_text(encoding="utf-8")


def test_clicking_an_idea_drafts_it_instead_of_building():
    """A one-line prompt used to fire a governed build the instant it was clicked — money spent
    on a spec nobody had read. It now loads the brief for editing."""
    import re
    code = re.sub(r"/\*.*?\*/", "", _hero_src(), flags=re.S)
    code = re.sub(r"^\s*//.*$", "", code, flags=re.M)
    assert "draftIdea" in code
    i = code.index("const draftIdea")
    body = code[i:i + 700]
    assert "setInput(idea.brief)" in body
    assert "assign(" not in body, "clicking a suggestion still starts a build"


def test_every_brief_is_a_real_spec_not_a_sentence():
    """"It should not be one sentence — feed some good info about the idea." Each brief has to
    say what it is, what it must do, and how you know it worked, or Build has nothing to go on."""
    src = _hero_src()
    briefs = re.findall(r"brief: `(.*?)`", src, re.S)
    assert len(briefs) >= 8, f"only {len(briefs)} briefs"
    for b in briefs:
        assert b.count("\n") >= 6, f"too thin to build from:\n{b[:120]}"
        assert "Who it is for:" in b
        assert "What it must do:" in b
        assert "Done when:" in b, "no acceptance criteria"


def test_the_ideas_scroll_rather_than_flipping_on_a_timer():
    """A nine-second auto-flip means the card you are reading can leave mid-sentence, and seeing
    the whole set requires waiting. All of them sit in one scroll strip now."""
    import re
    src = _hero_src()
    code = re.sub(r"/\*.*?\*/", "", src, flags=re.S)
    code = re.sub(r"^\s*//.*$", "", code, flags=re.M)
    assert "askboss-idea-strip" in code
    # Scoped to the idea state, not the file: an unqualified "no setInterval" also catches the
    # elapsed-seconds ticker, which is a different thing and should stay.
    assert "setIdeaPage" not in code and "ideasPaused" not in code, (
        "the paging timer is still there")
    assert "IDEAS.map(" in code, "the strip must hold every idea, not a page of them"


def test_the_arrows_do_not_fight_css_scroll_snap():
    """The arrows did nothing: scrollBy with behavior:"smooth" against a strip carrying CSS
    scroll-snap, where snap pulls the position back mid-animation."""
    css = pathlib.Path("apps/web/app/globals.css").read_text(encoding="utf-8")
    assert "scroll-snap" not in css, "snap is back and will cancel programmatic scrolling"
    src = _hero_src()
    assert "const nudge = useCallback" in src
    i = src.index("const nudge = useCallback")
    body = src[i:i + 600]
    assert "offsetWidth" in body, "still stepping by a magic pixel count instead of a card"


def test_auto_scroll_is_not_silently_disabled_by_an_os_setting():
    """The drift never started: it was gated on prefers-reduced-motion, so on a machine with
    Reduce Motion enabled an explicitly requested feature did nothing and looked broken. The
    preference now sets the DEFAULT of a visible toggle instead of hiding the feature."""
    src = _hero_src()
    assert "setDrift" in src, "no way to turn the drift on"
    # Strip comments first: the phrase also appears in this file's header, and matching that
    # occurrence tests the prose rather than the behaviour — the fifth time in this suite.
    code = re.sub(r"^\s*\*.*$", "", re.sub(r"//.*$", "", src, flags=re.M), flags=re.M)
    i = code.index("prefers-reduced-motion")
    around = code[max(0, i - 500):i + 200]
    assert "setDrift" in around, "the preference still gates the effect rather than the default"


def test_the_hover_detail_does_not_resize_the_card():
    """Three attempts at this. Inline, it tripled the card's height and shoved the row around.
    Floated below, it was clipped by the strip's overflow-x. It now covers the card's own area,
    so containment is structural — inset:0 inside a card that hides overflow cannot change the
    layout or be sliced, and needs no text-length trick to stay put."""
    css = pathlib.Path("apps/web/app/globals.css").read_text(encoding="utf-8")
    i = css.index(".askboss-idea-peek {")
    block = css[i:css.index("}", i)]
    assert "position: absolute" in block, "the detail still occupies layout space"
    assert "inset: 0" in block, "the overlay is not bounded to the card"
    card = css.index(".askboss-idea { position: relative;")
    assert "overflow: hidden" in css[card:css.index("}", card)], (
        "the card does not clip its own overlay, so long text can still escape")


def test_there_are_enough_ideas_to_be_worth_cycling():
    """Eight was thin enough that the operator saw the whole set at once."""
    briefs = re.findall(r"brief: `(.*?)`", _hero_src(), re.S)
    assert len(briefs) >= 16, f"only {len(briefs)} briefs"


def test_the_ideas_cover_more_than_one_kind_of_product():
    """A pool that is all developer tooling is a narrower prompt than an empty box."""
    tags = {t.lower() for t in re.findall(r'tag: "([^"]+)"', _hero_src())}
    assert len(tags) >= 5, f"only {len(tags)} categories: {tags}"


def test_the_composer_can_show_a_multi_line_brief():
    """A brief is a paragraph; a fixed one-row box hid all but the first line."""
    assert "scrollHeight" in _hero_src()


# --------------------------------------------------------------------------- #
# A dashboard number must be the number it claims to be
# --------------------------------------------------------------------------- #
def test_a_metric_is_never_named_for_something_it_cannot_know():
    """"Are these real or placeholders?" One tile read exactly 100 — the console's own page
    size, not a count. Writing these, the same trap appeared again: it is tempting to call
    "completed run that has a PR" a merged PR. Merge state is NOT in this database (checked:
    code_task_runs has pull_request_url/number, finalize_ctx has no merge key), so the field is
    named for what it is and prs_merged is explicitly null with a reason."""
    import inspect
    from boss_engine.services import self_observability
    src = inspect.getsource(self_observability.delivery_metrics)
    assert '"runs_completed_with_pr"' in src
    assert '"prs_merged": None' in src, "a number is being passed off as a merge count"
    assert "prs_merged_note" in src


def test_the_metrics_are_aggregates_not_page_counts():
    """The bug being fixed: fetch 100 rows, count them, display the page size as a total."""
    import inspect
    from boss_engine.services import self_observability
    src = inspect.getsource(self_observability.delivery_metrics)
    assert "count(*)" in src and "COALESCE(sum(" in src
    assert "LIMIT" not in src.upper(), "a metric must not be computed from a limited page"


# --------------------------------------------------------------------------- #
# Build has to actually start the governed sequence
# --------------------------------------------------------------------------- #
def test_build_starts_a_prd_panel_not_a_plan_artifact():
    """An operator clicked an idea, saw "Working", and nothing ever happened. The task WAS
    created and the job DID succeed — it was an openai.plan job, which writes a plan artifact
    and stops, because nothing downstream consumes a plan. Build must use the documented entry
    point for "build X": POST /v1/meetings/prd -> panel -> PRD -> code task."""
    import re
    actions = pathlib.Path("apps/web/app/actions.ts").read_text(encoding="utf-8")
    assert "startBuildAction" in actions
    i = actions.index("export async function startBuildAction")
    body = actions[i:i + 900]
    assert "/v1/meetings/prd" in body, "Build does not convene a panel"
    assert "directive" in body

    hero = pathlib.Path("apps/web/app/ask-boss-hero.tsx").read_text(encoding="utf-8")
    code = re.sub(r"/\*.*?\*/", "", hero, flags=re.S)
    code = re.sub(r"^\s*//.*$", "", code, flags=re.M)
    assert "submitCommandAction" not in code, "Build still uses the plan-artifact pipeline"


def test_build_reports_success_and_failure():
    """The old action returned void, so a failure and a success looked identical: "Working",
    then nothing, forever."""
    import re
    hero = pathlib.Path("apps/web/app/ask-boss-hero.tsx").read_text(encoding="utf-8")
    i = hero.index("const assign = useCallback")
    body = hero[i:i + 1400]
    assert "setError(" in body, "a failed build is still silent"
    assert "setReply(" in body, "a started build gives no confirmation"
    # The press starts a WORKFLOW RUN now, not a panel, so the id worth naming is the run's —
    # it is the thing the operator goes and watches.
    assert "runId" in body, "the operator is not told what was created"


# --------------------------------------------------------------------------- #
# The Command Center shows work, not machine noise
# --------------------------------------------------------------------------- #
def test_the_landing_panel_shows_started_work_not_the_event_log():
    """"These don't make real sense to me." The panel was the raw event stream — worker
    heartbeats, leased jobs, budget reservations released. All true, none of it actionable, and
    it crowded out the only question an operator has after pressing Build: what is it doing and
    where do I look?"""
    src = pathlib.Path("apps/web/app/page.tsx").read_text(encoding="utf-8")
    assert 'aria-label="In flight"' in src
    assert "cc-flight-stage" in src, "no stage is shown"
    i = src.index('aria-label="In flight"')
    panel = src[i:i + 2200]
    assert "AreaLink" in panel and "item.area" in panel, "rows do not route to the stage"
    assert "heartbeat" not in panel


def test_in_flight_only_uses_columns_that_exist():
    """The first version joined meetings on task_id — a column meetings does not have — and the
    endpoint returned "Required database relation is not applied". Verified against
    information_schema, not assumed."""
    import inspect
    from boss_engine.services import self_observability
    src = inspect.getsource(self_observability.in_flight)
    # Strip SQL comments before asserting: the first version of this test matched the comment
    # EXPLAINING why action_task_ids is unused, which is the third time this suite has asserted
    # on prose instead of code.
    code = re.sub(r"--.*$", "", src, flags=re.M)
    assert "m.task_id" not in code
    assert "action_task_ids" not in code, "that array is empty on this path — checked live"


def test_every_in_flight_row_names_an_area_to_open():
    """A status with nowhere to go is the same dead end as no status."""
    import inspect
    from boss_engine.services import self_observability
    src = inspect.getsource(self_observability)
    assert "_STAGE_AREA" in src
    for stage in ("discovery", "meeting", "coding", "review", "deployment"):
        assert f'"{stage}"' in src, f"{stage} has no console area"


def test_the_host_list_is_fetched_once_per_page_not_once_per_card():
    """Every app card sat at "Checking for a host…", which reads as "BOSS cannot reach the VM".
    The API was fine — ~3s, one verified host, SSH allowed. SetupDeployment renders inside every
    card and each copy fired its own identical lookup; past ~6 apps they queue behind a browser's
    per-origin connection limit while a multi-megabyte page loads, so none of them resolves."""
    import re
    src = pathlib.Path("apps/web/app/code-tasks-ui.tsx").read_text(encoding="utf-8")
    code = re.sub(r"/\*.*?\*/", "", src, flags=re.S)
    code = re.sub(r"^\s*//.*$", "", code, flags=re.M)
    assert "loadHostsOnce" in code and "hostsCache" in code
    assert code.count("/api/deployment-hosts?project_id=") == 1, (
        "the host list is still fetched from more than one place")
    # And it must NOT be a Server Action: that re-renders the whole route to reply, which on a
    # 20-27s page is the entire reason this looked like a hang.
    # Comments still explain WHY the Server Action was removed, so strip them first — the
    # fourth time this suite has nearly asserted on prose.
    assert "listDeploymentHostsAction(" not in code, (
        "a Server Action reply re-renders the route — that was the bug")


def test_the_host_lookup_always_settles():
    """A fetch with no timeout can hang forever, and this one is the only thing between the
    operator and the Deploy button — so "still loading" was indistinguishable from "broken"."""
    src = pathlib.Path("apps/web/app/code-tasks-ui.tsx").read_text(encoding="utf-8")
    i = src.index("function loadHostsOnce")
    # Slice to the end of the function rather than a fixed byte count — the first version cut
    # at 900 chars and started failing when a comment grew, which tests nothing about behaviour.
    body = src[i:src.index("\n}", i)]
    assert "Promise.race" in body and "timed out" in body
    assert ".catch(" in body, "a rejected lookup would leave the card loading forever"


def test_the_host_lookup_is_a_route_handler_not_a_server_action():
    """The lookup answered in ~2s and still hit a 20s timeout. A Server Action does not just
    return its value — the App Router re-renders the current route and ships a fresh RSC
    payload with the reply, and this console takes 20-27s to render. Three earlier measurements
    (API, API-under-render, web-under-render) all came back fast because they measured the
    REQUEST path; the cost was in the reply."""
    route = pathlib.Path("apps/web/app/api/deployment-hosts/route.ts")
    assert route.exists(), "no route handler for the host list"
    body = route.read_text(encoding="utf-8")
    assert "export async function GET" in body
    assert "/v1/deployment-hosts" in body and "authHeaders" in body


# --------------------------------------------------------------------------- #
# Research is a stage, and a stale tab is not a broken host
# --------------------------------------------------------------------------- #
def test_the_composer_can_research_not_only_ask_and_build():
    """The flow started at "convene a meeting", so the only way in was to already know what you
    wanted made. Discovery is the stage for when you do not."""
    src = _hero_src()
    # Asserts the CAPABILITY, not the mechanism: this originally required
    # startDiscoveryAction, and broke when that Server Action was replaced by a route handler
    # to stop it waiting on a full console re-render. Research still has to exist either way.
    assert "/api/discoveries" in src, "no way to start a research run"
    assert "askboss-research" in src, "no Research action on the composer"
    assert "RESEARCH" in src and src.count("seed:") >= 4, "no research starters"


def test_the_research_row_stays_one_compact_line():
    """Four full-width questions is a lot of height for a secondary path, and it pushed the
    composer — the thing that should read first — down the panel. The chip carries a short
    label; the full question goes into the composer on click."""
    src = _hero_src()
    assert "askboss-seed" in src
    import re
    labels = re.findall(r'label: "([^"]+)"', src)
    assert len(labels) >= 4, "no short labels"
    for lbl in labels:
        assert len(lbl) <= 26, f"not a chip: {lbl!r}"
        assert "?" not in lbl, f"the full question belongs in the composer, not the chip: {lbl!r}"


def test_research_seeds_are_questions_not_products():
    """A seed that names a product is a build directive wearing a research label."""
    import re
    seeds = re.findall(r'seed: "([^"]+)"', _hero_src())
    assert len(seeds) >= 4
    for s in seeds:
        assert s.rstrip().endswith("?"), f"not a research question: {s}"
        assert not s.lower().startswith("build "), f"that is a build, not research: {s}"


def test_a_stale_tab_is_not_reported_as_a_broken_host():
    """After a redeploy an open tab calls server-action ids that no longer exist. It surfaced as
    "Could not read the host list", sending the operator to check a VM that was never down."""
    src = pathlib.Path("apps/web/app/code-tasks-ui.tsx").read_text(encoding="utf-8")
    assert "isStaleClient" in src
    assert "UnrecognizedActionError" in src
    i = src.index("isStaleClient(loadError)")
    assert "older version of the console" in src[i:i + 500]
    assert "reload" in src[i:i + 700].lower(), "no way out is offered"


def test_the_debug_grid_is_gone_and_failures_are_not():
    """Eleven tiles answering questions nobody asks — is Postgres reachable, which auth method —
    three of them already duplicated by the KPI row. Deadletters was the one that mattered, so
    it survives as an alert that appears only when there is something to act on."""
    src = pathlib.Path("apps/web/app/page.tsx").read_text(encoding="utf-8")
    assert "<h2>Auth Session</h2>" not in src
    assert "<h2>Blocking State</h2>" not in src
    assert "<h2>Database</h2>" not in src
    # The deadletter banner went too, on the operator's call: "25 items failed and are waiting"
    # floating above the fold names a number and nothing else — not what failed, not when, not
    # whether it still matters. A count with no context is an anxiety generator; failures
    # belong beside the work they came from, in Monitoring.
    assert "cc-alert" not in src, "the contextless failure banner is back"


# --------------------------------------------------------------------------- #
# The console must not poll itself faster than it can render
# --------------------------------------------------------------------------- #
def test_the_auto_refresh_backs_off_to_what_a_render_costs():
    """A fixed 15s interval against a 20-27s render means every open tab asks for the next
    refresh before the last finished. The per-tab guard stops ONE tab stacking; enough tabs
    still saturate a single-threaded Node process — unrelated requests intermittently took 56s,
    the api healthcheck went unhealthy, and the console 502'd. The code comment shows this
    already happened once at 7s, so 15s only moved the threshold."""
    src = pathlib.Path("apps/web/app/ConsoleShell.tsx").read_text(encoding="utf-8")
    assert "lastRefreshMs" in src, "the refresh cost is not measured"
    i = src.index("const schedule = ()")
    body = src[i:i + 400]
    assert "lastRefreshMs.current * 3" in body, "the interval does not scale with the cost"
    assert "Math.max(20_000" in body, "no floor on the interval"
    assert "Math.min(180_000" in body, "no ceiling — a slow render could stop refresh entirely"
    assert "setInterval" not in src[i - 600:i + 900], "still a fixed-rate timer"


def test_the_refresh_still_skips_a_hidden_tab_and_a_typing_operator():
    """The existing guards are load-bearing and must survive the rewrite."""
    src = pathlib.Path("apps/web/app/ConsoleShell.tsx").read_text(encoding="utf-8")
    i = src.index("const tick = ()")
    body = src[i:i + 700]
    for guard in ("document.hidden", "refreshPending", "paletteOpen", '"TEXTAREA"'):
        assert guard in body, f"lost guard: {guard}"


def test_no_composer_action_waits_on_a_route_re_render():
    """Pressing Research spun for 163 seconds for a call that enqueues in about one. It was a
    Server Action that ALSO called revalidatePath, so it waited on a full 20-27s console render
    twice over. Ask, Research and Build all go to route handlers now — JSON only, no re-render."""
    import re
    src = _hero_src()
    code = re.sub(r"/\*.*?\*/", "", src, flags=re.S)
    code = re.sub(r"^\s*//.*$", "", code, flags=re.M)
    for gone in ("startDiscoveryAction", "startBuildAction", "submitCommandAction"):
        assert gone not in code, f"{gone} re-renders the route to reply"
    for route in ("/api/discoveries", "/api/builds"):
        assert route in code, f"missing route handler call: {route}"


def test_every_composer_action_is_bounded():
    """Research and Build had NO timeout, so nothing ever ended the spin — the operator watched
    163s tick past a bound that only wrapped Ask."""
    import re
    src = _hero_src()
    assert "postBounded" in src
    i = src.index("async function postBounded")
    body = src[i:src.index("\n}", i)]
    assert "STALL_SECONDS" in body and "Promise.race" in body
    assert "nothing was `" in body or "nothing was cancelled" in body, (
        "a stalled action must not claim the work was stopped")
    # and it is actually used by both paths
    code = re.sub(r"^\s*//.*$", "", re.sub(r"/\*.*?\*/", "", src, flags=re.S), flags=re.M)
    assert code.count("postBounded(") >= 3, "some action still has an unbounded wait"


# --------------------------------------------------------------------------- #
# Discovery: the reply was truncated, not the research missing
# --------------------------------------------------------------------------- #
def test_the_sweep_asks_for_evidence_before_the_summary():
    """Three runs failed with "web research returned no findings" at ~$0.50 each, having
    actually done the research: the reply was cut off after the brief and its sources and
    BEFORE the findings array existed. The schema asked for the disposable part first."""
    import inspect
    from boss_engine.services import discovery_service
    src = inspect.getsource(discovery_service._sweep_prompt)
    findings_at = src.index('"findings"')
    brief_at = src.index('"brief"')
    assert findings_at < brief_at, "the evidence is still requested last, so truncation loses it"
    # The instruction spans adjacent string literals, so join them before matching — the same
    # trap as asserting on a comment.
    # Concatenate adjacent literals the way Python does — with NOTHING between them — then
    # normalise whitespace. Joining with a space puts a double space at every literal boundary,
    # which is exactly where the phrase being matched happened to split.
    joined = " ".join("".join(re.findall(r'"([^"]*)"', src)).split())
    assert "cut the brief, never the findings" in joined


def test_a_truncated_sweep_is_not_reported_as_a_broken_search_tool():
    """It told the operator to check WebSearch/WebFetch while the reply contained six real
    citations. Sending someone to debug a working tool is worse than saying nothing."""
    import inspect
    from boss_engine.services import discovery_service
    src = inspect.getsource(discovery_service.run_discovery) if hasattr(
        discovery_service, "run_discovery") else inspect.getsource(discovery_service)
    assert "cut off before it produced any findings" in src
    assert "Web search itself WORKED" in src
    assert "sources_consulted" in src, "the two cases are not told apart by evidence"


@pytest.mark.parametrize("label,text,expect_findings", [
    ("mid-key",
     '```json\n{"findings": [{"angle": "demand", "finding": "x"}], "brief": {"hyp', 1),
    ("mid-array",
     '{"findings": [{"angle": "market", "finding": "a"}, {"angle": "timing", "fin', 1),
    ("mid-element",
     '{"findings": [{"angle": "a", "finding": "1"}, {"angle": "b", "finding": "2"}, {"ang', 2),
    ("brief first then cut",
     '{"brief": {"hypothesis": "h"}, "findings": [{"angle": "demand", "finding": "z"}, {"an', 1),
])
def test_a_truncated_sweep_still_yields_its_findings(label, text, expect_findings):
    """Bracket-balancing alone cannot fix a reply cut mid-key or mid-element: it produced
    invalid JSON, so the whole object was thrown away INCLUDING findings that had parsed. That
    is how a sweep carrying eleven good findings reported "no findings"."""
    from boss_engine.services.discovery_service import _extract_json
    out = _extract_json(text)
    assert len(out.get("findings") or []) >= expect_findings, f"{label}: evidence lost"


def test_salvage_does_not_invent_a_dossier_from_noise():
    """Salvaging must not become "always return something" — a refusal has no findings."""
    from boss_engine.services.discovery_service import _extract_json
    assert _extract_json("sorry, I could not complete that") == {}
    assert _extract_json("") == {}


def test_the_drift_banks_sub_pixel_movement():
    """The drift did nothing on a real browser. 14px/s is ~0.23px per frame at 60fps, and
    scrollLeft is rounded to whole pixels — so `scrollLeft = scrollLeft + 0.23` rounded back to
    the same integer every frame, forever. Nothing about the CSS or the ref was wrong; the
    arrows worked because they moved a whole card at a time.

    The motion must be accumulated until it is worth at least one pixel."""
    import re
    src = _hero_src()
    i = src.index("const PX_PER_SECOND")
    body = src[max(0, i - 900):i + 900]
    assert "carry" in body, "sub-pixel movement is still assigned straight to scrollLeft"
    assert "Math.floor(carry)" in body, "the remainder is not banked"
    # and the naive form must not come back
    code = re.sub(r"//.*$", "", body, flags=re.M)
    assert "scrollLeft + move" not in code, "fractional pixels assigned to scrollLeft again"


def test_the_discovery_LIST_is_restyled_not_only_the_expanded_run():
    """"I asked for a change on the discovery page but it looks the same." It did. The colour
    work went into RunDetail, which is one click away, so the page the operator opens was
    untouched — and my own check showed dx-flow absent from that page's HTML, which I explained
    away instead of following."""
    src = pathlib.Path("apps/web/app/discoveries-ui.tsx").read_text(encoding="utf-8")
    i = src.index("setSelected(d.id)")           # the list row, not the detail
    row = src[max(0, i - 400):i + 2600]
    assert "dx-row" in row, "the list row is still the old ct-card"
    assert "dx-stage" in row, "no stage chip on the row"
    assert "dx-mini-flow" in row, "the row does not show where the run got to"
    assert "dx-pill" in row, "the numbers are still plain grey text"


def test_the_hover_overlay_cannot_be_clipped_by_the_scroll_container():
    """Floated below the card it was sliced off: the strip needs overflow-x:auto to scroll, and
    that clips the y axis too. The overlay covers the card's own area instead."""
    css = pathlib.Path("apps/web/app/globals.css").read_text(encoding="utf-8")
    i = css.index(".askboss-idea-peek {")
    block = css[i:css.index("}", i)]
    assert "inset: 0" in block, "the overlay still hangs outside the card"
    assert "top: calc(100%" not in block, "still positioned below the card, where it is clipped"


# --------------------------------------------------------------------------- #
# The console must ask for things concurrently
# --------------------------------------------------------------------------- #
def test_the_page_does_not_fetch_its_feeds_one_at_a_time():
    """Load time was 20-30s and none of it was work. Measured: the API serves 82 concurrent
    requests in 1.6s, Node issues 82 in 0.8s, and the same page renders in 1.9s when every feed
    comes from memory. The page was simply asking for them one at a time — seven sequential
    awaits at the top, each a full round-trip before the next began."""
    src = pathlib.Path("apps/web/app/page.tsx").read_text(encoding="utf-8")
    body = src[src.index("export default async function HomePage"):]
    # Count barriers: an await that is not part of a Promise.all forces everything after it to
    # wait for one round-trip.
    barriers = [
        ln.strip() for ln in body.splitlines()
        if "await " in ln and "Promise.all" not in ln and not ln.strip().startswith(("//", "*"))
    ]
    assert len(barriers) <= 4, (
        f"{len(barriers)} sequential round-trips before the page can render:\n  "
        + "\n  ".join(barriers[:10])
    )


def test_a_failing_feed_still_cannot_take_the_console_down():
    """The batch must not turn one dead panel into a blank page: Promise.all rejects if any
    member throws, so every feed keeps its own .catch."""
    src = pathlib.Path("apps/web/app/page.tsx").read_text(encoding="utf-8")
    i = src.index("] = await Promise.all([")
    batch = src[i:i + 700]
    feeds = [ln for ln in batch.splitlines() if "Action(" in ln]
    assert feeds, "no feeds in the batch"
    for ln in feeds:
        assert ".catch(" in ln, f"unguarded feed in a Promise.all batch: {ln.strip()}"


def test_the_audit_export_does_not_run_on_every_page():
    """Measured: /v1/audit/export took 13.2s — more than half the page load, and more than the
    other 78 feeds combined. It serialises the ENTIRE audit trail (every task, job, decision,
    artifact, receipt, reservation, routing log and event) so the ops screen can show eight
    counts, and it is rendered only in data-area="ops". Every other area waited 13 seconds for
    something it never displayed."""
    src = pathlib.Path("apps/web/app/page.tsx").read_text(encoding="utf-8")
    i = src.index("fetchAuditSummary(activeProjectId)")
    guard = src[max(0, i - 900):i]
    assert 'requestedArea === "ops"' in guard, (
        "the audit export is unconditional again — 13s on every page for eight numbers"
    )
    assert "emptyAuditSummary" in src, "no stand-in for the areas that skip it"


def test_a_skipped_feed_says_why_rather_than_showing_zero():
    """A count of 0 and a count that was never fetched must not look the same — one is a fact
    about the system, the other is a fact about the page."""
    src = pathlib.Path("apps/web/app/page.tsx").read_text(encoding="utf-8")
    i = src.index("emptyAuditSummary(")
    assert "too expensive to fetch on every page" in src[i:i + 400], (
        "the skipped summary gives no reason"
    )
