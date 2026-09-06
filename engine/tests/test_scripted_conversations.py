"""Real operator messages, replayed against the router with a scripted model.

The suite the PRD calls T2. Every other test in this area checks a helper in isolation; this one
runs the actual answer path — the write gate, the resolution loop, the reconcilers, the
de-duplicator — and asserts what the operator would have received.

It costs nothing to run: the model is a script, so each case states exactly what the model tries
to do and the test asserts what the SYSTEM did with it. That is the point. Every incident so far
has been the system's handling of a plausible model output, not an exotic model failure:

  * the model wanted to cancel a job → the system let it, on a question
  * the model answered after three lookups → the system accepted it
  * the model said "I could not determine" → the system sent it

The messages are the operator's own words where they exist.
"""
from __future__ import annotations

import pytest

from boss_engine.services import intent_router as ir


class _Budget:
    status = "reserved"

    def reserve(self, *a, **kw):
        return {"status": "reserved", "reservation_id": "res_1"}

    def release(self, *a, **kw):
        return None


class _Turn:
    def __init__(self, reply, trace):
        self.reply = reply
        self.trace = trace


class ScriptedModel:
    """A model that does exactly what a case says, in order, one scripted turn per chat() call.

    `steps` is what the model tries: a list of (tool, args, result) per turn. `replies` is what
    it says. When the router asks for another turn — the resolution loop, the act-on-claim
    retry — the next script entry is used, so a case can assert what a continuation produced.
    """

    def __init__(self, script):
        self.script = list(script)
        self.calls: list[dict] = []

    def chat(self, **kwargs):
        self.calls.append(kwargs)
        if not self.script:
            return _Turn("", [])
        reply, steps = self.script.pop(0)
        trace = [{"tool": t, "args": a, "result": r} for t, a, r in steps]
        return _Turn(reply, trace)


def run(monkeypatch, message, script, *, history=None):
    """One turn through the real router. Returns the RouterTurn the operator would see."""
    router = ir.IntentRouter.__new__(ir.IntentRouter)
    router.budget = _Budget()
    router.settings = type("S", (), {"agent_router_budget_usd_micros": 1000})()
    model = ScriptedModel(script)
    monkeypatch.setattr(router, "_answer_agent", lambda: model, raising=False)
    monkeypatch.setattr(ir, "emit_event", lambda *a, **kw: None)
    messages = list(history or []) + [{"role": "user", "content": message}]
    turn = router._answer(None, "boss-dogfood", messages)
    return turn, model


#: What a blocked destructive tool looks like coming back from the agent loop. This is the shape
#: engine_agent produces, so a case can script "the model tried to cancel" honestly.
BLOCKED_CANCEL = {
    "ok": True, "proposed": True, "tool": "cancel_job",
    "args": {"job_id": "job_cc61"}, "label": "cancel job",
    "why": "throws away work in flight, or removes a control — the operator decides",
}
RAN_RETRY = {"ok": True, "status": "retried"}
SIX_READS = [(f"read_{i}", {}, {"ok": True}) for i in range(6)]


class TestTheIncidentItself:
    """The operator's exact message, and the model doing exactly what it did that day."""

    MESSAGE = ("This project Interview Scorecard App stuck for 27m for ● Engineer implementing "
               "can you check why ?")

    def test_the_cancel_comes_back_as_a_button_not_a_deed(self, monkeypatch):
        turn, _ = run(monkeypatch, self.MESSAGE, [
            ("I cancelled the holding job to free the worker slot.",
             [("get_code_task", {}, {"ok": True}),
              ("find_starved_work", {}, {"ok": True}),
              ("inspect_self", {}, {"ok": True}),
              ("cancel_job", {"job_id": "job_cc61"}, BLOCKED_CANCEL)]),
            ("The build hung on a command that never returned.", SIX_READS),
        ])
        assert turn.actions == [], "a destructive tool was executed"
        assert any(p["tool"] == "cancel_job" for p in turn.proposed_actions)

    def test_and_the_reply_does_not_claim_it_was_done(self, monkeypatch):
        turn, _ = run(monkeypatch, self.MESSAGE, [
            ("I cancelled the holding job to free the worker slot.",
             [("cancel_job", {"job_id": "job_cc61"}, BLOCKED_CANCEL)]),
            ("The build hung on a command that never returned.", SIX_READS),
        ])
        # Either the continuation replaced the false claim, or the reconciler corrected it.
        assert ("cancelled the holding job" not in turn.reply
                or "nothing above was actually changed" in turn.reply)

    def test_three_lookups_is_not_enough_to_answer_with(self, monkeypatch):
        _turn, model = run(monkeypatch, self.MESSAGE, [
            ("The implement phase is stalled.",
             [("get_code_task", {}, {"ok": True}),
              ("find_starved_work", {}, {"ok": True}),
              ("inspect_self", {}, {"ok": True})]),
            ("It hung on `npm run verify`, which never returned.", SIX_READS),
        ])
        assert len(model.calls) >= 2, "the router accepted a three-lookup answer"
        assert "lookups" in model.calls[1]["message"]

    def test_a_thorough_answer_is_left_alone(self, monkeypatch):
        """The floor must not punish an answer that did the work."""
        _turn, model = run(monkeypatch, self.MESSAGE, [
            ("It hung on `npm run verify`.", SIX_READS + [("read_logs", {}, {"ok": True})]),
        ])
        assert len(model.calls) == 1


class TestGivingUpIsNotAnEnding:
    @pytest.mark.parametrize("ending", [
        "I could not determine why this happened.",
        "It is unclear why the build stopped.",
        "Would you like me to investigate further?",
        "Let me know if you'd like me to look at the logs.",
    ])
    def test_the_turn_continues_instead_of_sending_it(self, monkeypatch, ending):
        _turn, model = run(monkeypatch, "why is crun_33a0 stuck?", [
            (ending, SIX_READS),
            ("It hung on `npm run verify`; the retry is offered below.", SIX_READS),
        ])
        assert len(model.calls) >= 2, f"sent without a direction: {ending!r}"
        assert "handing the problem back" in model.calls[1]["message"]

    def test_but_an_offer_counts_as_a_direction(self, monkeypatch):
        """"I could not determine the cause" WITH a button is a real answer — the operator has
        somewhere to go."""
        _turn, model = run(monkeypatch, "why is crun_33a0 stuck?", [
            ("I could not determine the cause from the logs.",
             SIX_READS + [("cancel_job", {"job_id": "j"}, BLOCKED_CANCEL)]),
        ])
        assert len(model.calls) == 1

    def test_a_direct_tool_instruction_is_not_hijacked(self, monkeypatch):
        """Observed live. Asked to run `SELECT statements, failed FROM schema_apply_runs`, the
        word "failed" alone made this an operational question, the floor fired, and BOSS
        answered a plain SQL request with "please provide the run ID and I'll run the full
        diagnosis". A guard that hijacks a direct instruction is worse than no guard."""
        _turn, model = run(monkeypatch,
                           "Use query_db once, raw row only: SELECT statements, failed "
                           "FROM schema_apply_runs ORDER BY id DESC LIMIT 1",
                           [("statements: 667, failed: 0", [("query_db", {}, {"ok": True})])])
        assert len(model.calls) == 1

    def test_a_symptom_alone_is_not_enough(self, monkeypatch):
        """One keyword is too loose; it needs a subject AND a symptom."""
        from boss_engine.services.intent_router import is_operational_question

        assert is_operational_question("how many runs failed last week?") is False
        assert is_operational_question("why is the build failing?") is True

    def test_a_non_operational_question_is_not_forced_to_investigate(self, monkeypatch):
        """"What can you do?" does not need six lookups. The floor is for questions about work
        that is running or broken."""
        _turn, model = run(monkeypatch, "what workflows are available?", [
            ("Eleven, including Standard Product Delivery.", [("list_workflows", {}, {})]),
        ])
        assert len(model.calls) == 1


class TestSayingItOnce:
    def test_a_repeated_paragraph_is_dropped(self, monkeypatch):
        said = ("The build hung on npm run verify because the output pipe stayed open after "
                "Playwright started the webServer, so the timeout could not end it.")
        turn, _ = run(monkeypatch, "and why did the timeout not fire?",
                      [(said + " The fix is to kill the process group.", SIX_READS)],
                      history=[{"role": "assistant", "content": said}])
        # Removed, not shortened: repeating it is the defect, and a reference is cheaper for
        # the reader than a second telling.
        assert "output pipe stayed open" not in turn.reply
        assert "kill the process group" in turn.reply
        assert "already covered above" in turn.reply

    def test_a_fresh_answer_is_untouched(self, monkeypatch):
        turn, _ = run(monkeypatch, "why is it stuck?",
                      [("It hung on a command that never returned.", SIX_READS)],
                      history=[{"role": "assistant", "content": "Something else entirely."}])
        assert turn.reply == "It hung on a command that never returned."

    def test_the_whole_reply_is_never_eaten(self, monkeypatch):
        """A de-duplicator that removes everything is worse than one that misses a
        repetition — the operator would get silence."""
        said = ("The build hung on npm run verify because the output pipe stayed open after "
                "Playwright started the webServer, so the timeout could not end it.")
        turn, _ = run(monkeypatch, "say that again", [(said, SIX_READS)],
                      history=[{"role": "assistant", "content": said}])
        assert turn.reply.strip()


class TestAPrintedOfferBecomesAButton:
    """Observed live on the very question this work exists for. BOSS diagnosed the hung build,
    decided the right fix, and printed it as a fenced JSON block — the shape of a perfect offer,
    which is not one. The operator got a code block where a button should be."""

    PRINTED = ('The run stopped because the final Bash command never returned.\n\n'
               '**Fix:** retry the same run.\n\n'
               '```json\n{\n "tool": "retry_code_task",\n'
               ' "args": {"run_id": "crun_33a0a77e6ccf4d17b36e"},\n'
               ' "label": "Retry Interview Scorecard App",\n'
               ' "why": "re-run the cancelled implement phase"\n}\n```')

    def test_the_offer_is_recovered(self, monkeypatch):
        turn, _ = run(monkeypatch, "why is crun_33a0a77e6ccf4d17b36e stuck?",
                      [(self.PRINTED, SIX_READS)])
        assert [p["tool"] for p in turn.proposed_actions] == ["retry_code_task"]
        assert turn.proposed_actions[0]["args"] == {"run_id": "crun_33a0a77e6ccf4d17b36e"}
        assert turn.proposed_actions[0]["label"] == "Retry Interview Scorecard App"

    def test_and_the_json_is_taken_out_of_the_text(self, monkeypatch):
        """Leaving it shows the operator raw markup beside the button that now does the same
        thing — worse than the missing button was."""
        turn, _ = run(monkeypatch, "why is crun_33a0a77e6ccf4d17b36e stuck?",
                      [(self.PRINTED, SIX_READS)])
        assert "```json" not in turn.reply
        assert "The run stopped because" in turn.reply

    def test_a_proposal_of_a_proposal_is_unwrapped(self, monkeypatch):
        """Observed live. `propose_action` is itself a tool, so a model writing out its own
        call describes {"tool": "propose_action", "args": {"tool": "retry_code_task", …}}.
        Recovered literally that is a button whose tool is propose_action — on click it would
        propose something rather than doing it. The offer read "Retry Interview Scorecard App"
        and would have run nothing."""
        printed = ('```json\n{"tool": "propose_action", "args": '
                   '{"tool": "retry_code_task", "args": {"run_id": "crun_33a0"}}, '
                   '"label": "Retry Interview Scorecard App"}\n```')
        turn, _ = run(monkeypatch, "why is crun_33a0 stuck?", [(printed, SIX_READS)])
        assert [p["tool"] for p in turn.proposed_actions] == ["retry_code_task"]
        assert turn.proposed_actions[0]["args"] == {"run_id": "crun_33a0"}

    def test_a_json_sample_the_operator_asked_for_survives(self, monkeypatch):
        """Not every fenced block is a proposal. One that does not name a real tool with an
        args object is left exactly as written."""
        sample = 'Here is the shape:\n\n```json\n{"status": "running", "attempts": 3}\n```'
        turn, _ = run(monkeypatch, "what does a code task row look like?", [(sample, [])])
        assert "```json" in turn.reply
        assert turn.proposed_actions == []

    def test_an_invented_argument_is_not_offered(self, monkeypatch):
        """A button that fails on click is worse than no button."""
        printed = ('```json\n{"tool": "retry_code_task", '
                   '"args": {"run_id": "crun_placeholder"}, "label": "Retry"}\n```')
        turn, _ = run(monkeypatch, "why is crun_33a0 stuck?", [(printed, SIX_READS)])
        assert turn.proposed_actions == []


class TestActingWhenActuallyTold:
    """P6: refusing to act is a failure too. The safety work must not have made BOSS passive."""

    def test_a_safe_write_still_runs_when_instructed(self, monkeypatch):
        turn, _ = run(monkeypatch, "retry the build", [
            ("Retried it.", [("retry_code_task", {"run_id": "crun_1"}, RAN_RETRY)]),
        ])
        assert any(a["kind"] == "retry_code_task" for a in turn.actions)

    def test_and_the_reply_is_not_stripped_of_its_claim(self, monkeypatch):
        turn, _ = run(monkeypatch, "retry the build", [
            ("Retried it.", [("retry_code_task", {"run_id": "crun_1"}, RAN_RETRY)]),
        ])
        assert "Retried it." in turn.reply
        assert "nothing above was actually changed" not in turn.reply


class TestTheTurnSurvivesBadInput:
    """The operator's other complaint: "it breaks easily"."""

    @pytest.mark.parametrize("message", [
        "?", "   ", "why", "🙂", "a" * 4000,
        "why is it stuck; DROP TABLE code_task_runs; --",
    ])
    def test_it_answers_or_fails_cleanly(self, monkeypatch, message):
        turn, _ = run(monkeypatch, message, [("Here is what I found.", SIX_READS)])
        assert isinstance(turn.reply, str)
        assert isinstance(turn.actions, list)

    def test_a_model_that_returns_nothing_does_not_produce_a_blank_turn(self, monkeypatch):
        turn, _ = run(monkeypatch, "why is crun_1 stuck?", [("", [])])
        assert turn.reply.strip(), "an empty reply reached the operator"

    def test_a_model_that_raises_is_reported_not_swallowed(self, monkeypatch):
        class Boom:
            def chat(self, **kw):
                raise RuntimeError("provider exploded")

        router = ir.IntentRouter.__new__(ir.IntentRouter)
        router.budget = _Budget()
        router.settings = type("S", (), {"agent_router_budget_usd_micros": 1000})()
        monkeypatch.setattr(router, "_answer_agent", lambda: Boom(), raising=False)
        monkeypatch.setattr(ir, "emit_event", lambda *a, **kw: None)
        turn = router._answer(None, "p", [{"role": "user", "content": "why is crun_1 stuck?"}])
        assert "provider exploded" in turn.reply or "could not answer" in turn.reply.lower()

    def test_a_trace_with_junk_in_it_does_not_crash_the_turn(self, monkeypatch):
        turn, _ = run(monkeypatch, "why is crun_1 stuck?", [
            ("Found it.", [(None, None, None), ("read_logs", {}, {"ok": True})]),
        ])
        assert isinstance(turn.reply, str)
