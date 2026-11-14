"""An operator asking why their work is stuck must not be handed a project plan.

Observed twice: a question containing SQL or a list came back as "I drafted N sequenced phases".

The cause was `_looks_multi_phase`, which returned True on
`len(re.findall(r"\\band\\b", message)) >= 2` — two occurrences of the commonest conjunction in
English. So a diagnostic sentence, or any pasted WHERE clause, was read as a multi-phase command.

The asymmetry decides the default: being wrong toward "answer" costs one follow-up message;
being wrong toward "act" spends money, creates junk work, and buries the answer that was asked
for.
"""

import pytest

from boss_engine.services.intent_router import IntentRouter

from ._source import code_only


def _classify(message: str):
    return IntentRouter.__dict__["_deterministic_intent"](
        IntentRouter.__new__(IntentRouter), message
    )


# --------------------------------------------------------------------------- #
# The reported misroutes
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("message", [
    "the meeting sat queued for two hours and the jobs table shows deadlettered rows "
    "and I need to know why",
    "the run is blocked, the console shows nothing, why?",
    "why is this stuck? the phase says run_tests and nothing has moved",
    "SELECT id, status FROM jobs WHERE kind='meeting.step' and status='queued' "
    "and project_id='boss-dogfood'",
    "the build failed and the deploy failed, what happened",
])
def test_a_diagnostic_message_is_answered_not_planned(message):
    name, _args = _classify(message)
    assert name is None, f"routed to {name!r} instead of answering"


def test_two_ands_are_not_a_plan():
    """The exact rule that caused this: 'and' is a conjunction, not a phase separator."""
    name, _ = _classify("the api is slow and the console is slow and previews fail")
    assert name is None


def test_pasted_sql_is_always_a_question():
    """SQL arrives when someone is investigating. Its ANDs and verbs are precisely what tripped
    the phase detector."""
    name, _ = _classify(
        "SELECT count(*) FROM code_task_runs WHERE status='running' and phase='done'")
    assert name is None


# --------------------------------------------------------------------------- #
# …without breaking what SHOULD spawn work
# --------------------------------------------------------------------------- #
def test_an_explicit_sequence_is_still_a_multi_phase_command():
    name, _ = _classify("First research auth providers and then implement the login page")
    assert name == "propose_command"


@pytest.mark.parametrize("message", [
    "Phase 1 build the API, phase 2 ship the UI",
    "design the schema, then implement it, then deploy end-to-end",
])
def test_messages_that_say_they_have_phases_are_taken_at_their_word(message):
    name, _ = _classify(message)
    assert name == "propose_command"


def test_a_plain_build_order_still_creates_a_code_task():
    name, args = _classify("build a settings page")
    assert name == "create_code_task"
    assert args["instructions"] == "build a settings page"


def test_a_polite_imperative_is_still_an_order():
    """'can you add a dark mode toggle' is a request to act, not a question about acting."""
    name, _ = _classify("can you add a dark mode toggle")
    assert name == "create_code_task"


def test_a_question_containing_a_build_verb_is_still_a_question():
    name, _ = _classify("what would it take to build a settings page?")
    assert name is None


def test_the_phase_detector_no_longer_counts_conjunctions():
    """Asserted BEHAVIOURALLY. The first version of this searched the function's source for the
    old regex — and matched the docstring that explains the bug, which is the same
    reading-prose-as-behaviour mistake recorded as I26."""
    many_ands = "alpha and beta and gamma and delta"
    assert IntentRouter._looks_multi_phase(many_ands) is False
    assert IntentRouter._looks_multi_phase("first alpha then beta") is True


def test_an_imperative_keeps_its_meaning_even_when_it_mentions_a_question_word():
    """'build a page that shows what users did' is an order, not a question."""
    name, _ = _classify("build a page that shows what users did")
    assert name == "create_code_task"


# --------------------------------------------------------------------------- #
# Running out of steps is not the same as having nothing to say
# --------------------------------------------------------------------------- #
def test_the_step_limit_produces_findings_not_an_apology():
    """Observed: "reached the step limit while working" — an abort message where an answer
    should be. The loop already made a final tool-free call, but with no instruction, so a
    model that had gathered real evidence over a dozen calls could still return nothing.

    An investigation that stops early has still LEARNED something, and the half it finished is
    usually the half that matters.
    """
    import inspect
    from boss_engine.services import agent_loop

    from boss_engine.kernel import presets

    # BOTH LOOPS (K4): the old loop's literal, and the kernel preset an agent run drives on.
    for src in (inspect.getsource(agent_loop._drive_run_legacy), presets.AGENT_LOOP_ANSWER):
        assert "You have used your tool budget" in src
        assert "Do not apologise and do not ask" in src
        assert "What you could NOT determine" in src


def test_the_fallback_string_tells_the_operator_what_to_do_next():
    import inspect
    from boss_engine.services import agent_loop

    from boss_engine.kernel import presets

    src = code_only(agent_loop._drive_run_legacy)
    assert "narrowing the question" in src
    assert "narrowing the question" in presets.AGENT_LOOP_FALLBACK  # the kernel's (K4)
    # Read through code_only: the comment above the fix QUOTES the old string, and matching
    # that instead of the code is the mistake this helper exists to prevent.
    assert "(agent reached its step limit before finishing)" not in src


def test_the_new_diagnostic_tools_are_named_in_the_prompt():
    """A tool the prompt never mentions is a tool the model never reaches for — the file's own
    comment records exactly this: 'the previous prompt listed four example tools, so anything
    outside those four got a plausible guess.' inspect_self was added and would have stayed
    invisible."""
    import inspect
    from boss_engine.services.intent_router import IntentRouter

    src = inspect.getsource(IntentRouter)
    assert "inspect_self" in src
    assert "OOM-KILL COUNT" in src, "the trigger matters more than the tool name"
    assert "consistency_report" in src


# --------------------------------------------------------------------------- #
# "Fix the deployment" is not a request to write code
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("message", [
    "The deployment for run crun_abc failed with health check 000. Fix it and retry now.",
    "fix the deployment",
    "the preview is dead, fix it",
    "this job is stuck, fix the queue",
    "the gate is blocking it, fix that",
    "redeploy it",
])
def test_an_operational_fix_is_handed_to_the_agent(message):
    """Observed live: this spawned a governed CODE TASK — a new run and real money — when the
    correct action was one redeploy call. The operator asked for a fix and got the most
    expensive reading of the word.

    None routes to the agent, which holds every tool INCLUDING create_code_task, so it can
    still choose to write code when that is genuinely the answer — with the run in front of it,
    which a regex does not have.
    """
    name, _ = _classify(message)
    assert name is None, f"routed to {name!r} instead of letting the agent choose the tool"


@pytest.mark.parametrize("message", [
    "build a settings page",
    "add a currency picker",
    "implement dark mode",
])
def test_a_genuine_code_request_still_becomes_a_code_task(message):
    """The narrowing must not cost the ordinary path."""
    name, _ = _classify(message)
    assert name == "create_code_task"


def test_fixing_app_code_is_still_a_code_task():
    """No operational noun, so it is what it looks like: a change to their app."""
    name, _ = _classify("fix the slug validation bug in the URL shortener")
    assert name == "create_code_task"
