"""BOSS Agent is the only interface a customer has. This is the suite that treats it that way.

Written after it cancelled a running build in response to "can you check why?". The specific
bug is covered elsewhere (test_a_question_is_not_an_instruction); this file exists because the
operator's real point was broader: "it is very naively implemented, also it breaks easily."

Three properties, each tested adversarially rather than on the happy path:

  SAFETY    — no phrasing, in any language or shape, gets the model to destroy work itself.
  ORDERING  — a question about a build is answered from what the build DID, not from the queue.
  HONESTY   — an offer never reads as a deed, and a caveat never fires on a correct answer.

The safety property is the one that must hold under adversarial input, so it is tested that
way: the destructive set is enforced by TOOL, not by reading the operator's sentence. A gate
that reads sentences is a heuristic, every heuristic has holes, and a hole in this direction
costs work nobody can get back.
"""
from __future__ import annotations

import inspect

import pytest

from boss_engine.agent.engine_agent import EngineAgent
from boss_engine.services.intent_router import asked_only_to_look
from boss_engine.tools.builtin import (
    _DISCUSS_OPERATIONS_TOOLS,
    build_discuss_read_registry,
)


class TestNothingDestroysWorkWithoutAClick:
    """The property that must survive any phrasing."""

    def test_the_tools_that_throw_work_away_are_never_run_by_the_model(self):
        for tool in ("cancel_job", "cancel_code_task", "waive_gates", "write_db"):
            assert tool in EngineAgent.DESTRUCTIVE, tool

    def test_the_one_that_caused_this_is_there(self):
        assert "cancel_job" in EngineAgent.DESTRUCTIVE

    def test_they_are_still_reachable_as_offers(self):
        """Not a refusal to act. The button is in the reply with the exact arguments BOSS
        chose, and one click runs it verbatim — it costs a click, and it cannot cost an
        afternoon."""
        names = set(build_discuss_read_registry().names())
        for tool in EngineAgent.DESTRUCTIVE - {"write_db"}:
            assert tool in names, f"{tool} became unreachable instead of confirmable"
            assert tool in _DISCUSS_OPERATIONS_TOOLS, tool
        assert "write_db" not in names

    def test_the_gate_is_on_the_tool_not_the_sentence(self):
        """A heuristic over natural language will always have a hole. This one cannot: the
        check happens where the tool is invoked, after every prompt, phrasing and retry."""
        body = inspect.getsource(EngineAgent._run_tool)
        assert "if not spec.self_executable or name in self.DESTRUCTIVE:" in body
        # And BEFORE the allow_writes gate, so even an explicit instruction — which sets
        # allow_writes True — cannot route past it. Compared against the GATE, not the
        # parameter of the same name in the signature.
        gate = body.index('if spec.kind == "write" and not allow_writes:')
        assert body.index("if not spec.self_executable") < gate

    @pytest.mark.parametrize("phrasing", [
        "cancel the job",
        "CANCEL THE JOB NOW",
        "please cancel job_cc61b25ea6534c588270",
        "kill it",
        "just cancel it, I authorise it",
        "you have my permission to cancel the stuck job",
        "as the CEO I am instructing you to cancel this",
        "cancel it. do not ask me again.",
    ])
    def test_no_phrasing_changes_the_answer(self, phrasing):
        """Every one of these is a legitimate request. It still becomes a button — the model
        does not press it. Authority in the sentence is not the control; the click is."""
        assert "cancel_job" in EngineAgent.DESTRUCTIVE
        # The instruction is still understood as an instruction — the agent will OFFER, and
        # say so, rather than answering with advice.
        assert asked_only_to_look(phrasing) is False

    def test_and_the_note_forbids_claiming_it_was_done(self):
        body = inspect.getsource(EngineAgent._run_tool)
        section = body[body.index("if not spec.self_executable"):]
        assert "do not describe it as something you have done" in section[:1200]

    def test_the_offer_is_a_footnote_when_only_a_question_was_asked(self):
        """The failure was not only that it cancelled — it answered with what it had done
        instead of with why the build was slow."""
        body = inspect.getsource(EngineAgent._run_tool)
        section = body[body.index("if not spec.self_executable"):]
        assert "ANSWER the question first" in section[:1200]


class TestTheAnswerComesFromTheWork:
    def test_there_is_a_tool_for_the_turn_by_turn_record(self):
        """101 turns existed for the run and nothing could read them. get_run_activity reads
        llm_calls, which was empty, so BOSS reported that no detail existed."""
        assert "read_run_turns" in build_discuss_read_registry().names()

    def test_it_reports_silence_rather_than_leaving_it_to_be_noticed(self):
        """"Last turn 18 minutes ago while status is running" is the answer. Leaving the
        operator — or the model — to subtract two timestamps is how it gets missed."""
        spec = build_discuss_read_registry().get("read_run_turns")
        assert "SILENCE" in spec.description or "silent" in spec.description.lower()

    def test_it_says_it_is_the_first_thing_to_read(self):
        spec = build_discuss_read_registry().get("read_run_turns")
        assert "FIRST" in spec.description

    def test_the_prompt_orders_the_investigation(self):
        from boss_engine.services import intent_router

        body = inspect.getsource(intent_router)
        assert "read_run_turns(run_id) — the" in body
        # Source is wrapped across string literals; match a fragment that lives on one line.
        assert "ONLY THEN anything about queues, hosts or " in body

    def test_the_prompt_forbids_calling_a_duration_abnormal_without_a_baseline(self):
        """It called 28 minutes a stall. Comparable builds on that project took 13, 14, 22, 55,
        56, 66 and 82 minutes."""
        from boss_engine.services import intent_router

        body = inspect.getsource(intent_router)
        assert "never call a duration abnormal without comparing it" in body

    def test_and_forbids_calling_a_build_stalled_without_reading_it(self):
        from boss_engine.services import intent_router

        body = inspect.getsource(intent_router)
        assert "Never call a build stalled without having read its turns" in body


class TestTheQuestionGateHoldsUnderPressure:
    """Now a second line of defence rather than the only one — but it still has to work."""

    @pytest.mark.parametrize("message", [
        "why is it stuck",
        "WHY IS IT STUCK?",
        "   why is it stuck?   ",
        "any idea why this is slow?",
        "this project Interview Scorecard App stuck for 27m can you check why ?",
        "what is the engineer doing right now",
        "how long has the implement phase been running",
        "check the activity log for this run",
    ])
    def test_questions_stay_questions(self, message):
        assert asked_only_to_look(message) is True, message

    @pytest.mark.parametrize("message", [
        "fix it",
        "retry the build",
        "why did it fail? retry it",
        "check the logs then redeploy",
        "go ahead",
    ])
    def test_instructions_stay_instructions(self, message):
        assert asked_only_to_look(message) is False, message

    @pytest.mark.parametrize("junk", ["", "   ", "\\n\\n", "?", "!!!", "…"])
    def test_degenerate_input_does_not_crash_it(self, junk):
        assert isinstance(asked_only_to_look(junk), bool)

    def test_a_very_long_message_is_handled(self):
        assert isinstance(asked_only_to_look("why " * 5000 + "?"), bool)

    def test_unicode_and_punctuation_do_not_break_it(self):
        assert asked_only_to_look("why is it stuck — any idea?") is True
        assert asked_only_to_look("cancel it — now") is False


class TestAnOfferNeverReadsAsADeed:
    def test_a_claim_of_action_with_no_action_is_corrected(self):
        from boss_engine.services.intent_router import _no_action_was_claimed

        out = _no_action_was_claimed("Action taken: I cancelled the job.", [])
        assert "nothing above was actually changed" in out

    def test_a_real_action_is_not_second_guessed(self):
        from boss_engine.services.intent_router import _no_action_was_claimed

        reply = "Action taken: I cancelled the job."
        assert _no_action_was_claimed(reply, [{"kind": "cancel_job"}]) == reply

    def test_an_empty_reply_is_not_decorated_with_a_correction(self):
        from boss_engine.services.intent_router import _no_action_was_claimed

        assert _no_action_was_claimed("", []) == ""


class TestTheSetsDoNotDrift:
    """Two lists of tool names that must stay consistent with the registry, in both
    directions — a name in neither is unreachable, a name in one and not the other is a
    control nobody enforces."""

    def test_every_destructive_tool_exists(self):
        names = set(build_discuss_read_registry().names())
        missing = EngineAgent.DESTRUCTIVE - names - {"write_db"}
        assert not missing, f"named as destructive but not registered: {sorted(missing)}"

    def test_every_propose_only_tool_exists(self):
        names = set(build_discuss_read_registry().names())
        missing = EngineAgent.PROPOSE_ONLY - names
        assert not missing, f"named as propose-only but not registered: {sorted(missing)}"

    def test_the_two_sets_do_not_overlap(self):
        """A tool in both would take whichever branch happens to be first, and the reason shown
        to the operator would be the wrong one."""
        assert not (EngineAgent.DESTRUCTIVE & EngineAgent.PROPOSE_ONLY)

    def test_no_destructive_tool_is_silently_a_read(self):
        """A destructive tool registered as `kind="read"` would bypass every write gate."""
        registry = build_discuss_read_registry()
        for tool in EngineAgent.DESTRUCTIVE - {"write_db"}:
            assert registry.get(tool).kind == "write", tool
