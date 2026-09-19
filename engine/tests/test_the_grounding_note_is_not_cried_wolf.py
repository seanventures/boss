"""A caveat attached to a correct answer costs more than it saves.

The grounding guard exists for a real failure: asked what worried it about shipping HookWatch,
the Operations agent called describe_role, skipped recall_role_experience, and wrote that "the
deployment target record shows the container publishing on port 8080" — a number it invented,
attributed to records it never opened. So a role answer that never read that role's record gets
sent back to read it, and is labelled unverified if it still will not.

The first version demanded the tool on EVERY turn. Live, on the second message of a
conversation:

    You: What did you just tell me was your biggest worry? Quote yourself.
    Security: > "My single biggest worry is that a malicious actor could exploit the BOSS
               preview proxy to ..."
               (Note, added by BOSS: this answer was NOT checked against Security's record on
                this project — treat any specific figure in it as unverified.)

The quote was exact and came from two messages up. There was nothing to check it against and
nothing wrong with it. A note that fires on correct answers is not caution: it teaches the
operator that the note means nothing, and spends the credibility it needs for the turn that IS
invented.

The record is read once and stays in the thread's context. The question the guard should ask is
whether this CONVERSATION is grounded, not whether this particular turn re-read the file.
"""
from __future__ import annotations

import inspect

from boss_engine.services import intent_router


def _guard_source() -> str:
    """The guard, from the read-before check to the note it may append."""
    src = inspect.getsource(intent_router)
    start = src.index("read_before = any(")
    return src[start:src.index("IT SAID IT DID IT")]


class TestTheGuardStillFires:
    """The failure it was written for must still be caught."""

    def test_a_role_answer_with_no_reading_at_all_is_challenged(self):
        body = _guard_source()
        assert 'c.get("tool") == "recall_role_experience"' in body
        # BOSS-417: this assertion used to read `self.agent.chat(`, and it PASSED for months
        # while the guard did nothing at all — `IntentRouter` has no `agent` attribute, so the
        # re-ask raised AttributeError on every turn inside `except Exception: reread = None`.
        # A source grep proves the line is present, never that it runs. It is pinned to the
        # real collaborator now, and test_the_grounding_reask_actually_runs.py checks the
        # wiring (agent exists, actor is threaded, failures are logged) rather than the text.
        assert "self._answer_agent().chat(" in body

    def test_and_is_labelled_if_it_still_will_not_read(self):
        assert "was NOT checked against" in _guard_source()

    def test_the_re_read_cannot_change_anything(self):
        """It is a re-read, not a second chance to act: a turn that already answered must not
        get write access on the way back through."""
        assert "allow_writes=False" in _guard_source()


class TestItDoesNotFireOnAGroundedConversation:
    def test_prior_turns_in_this_thread_count(self):
        body = _guard_source()
        assert "for message in (messages or [])" in body
        assert 'for call in (message.get("tool_calls") or [])' in body

    def test_and_short_circuit_the_guard(self):
        assert "not read_before" in _guard_source()

    def test_the_check_survives_a_message_with_no_tool_calls(self):
        """Every user message has none, and a null column reads as None rather than []."""
        messages = [{"role": "user", "content": "hi"},
                    {"role": "assistant", "content": "hello", "tool_calls": None}]
        read_before = any(
            isinstance(call, dict) and call.get("tool") == "recall_role_experience"
            for message in (messages or [])
            for call in (message.get("tool_calls") or [])
        )
        assert read_before is False

    def test_it_sees_a_read_recorded_on_an_earlier_turn(self):
        messages = [
            {"role": "user", "content": "what worries you?"},
            {"role": "assistant", "content": "the proxy",
             "tool_calls": [{"tool": "describe_role"}, {"tool": "recall_role_experience"}]},
            {"role": "user", "content": "quote yourself"},
        ]
        read_before = any(
            isinstance(call, dict) and call.get("tool") == "recall_role_experience"
            for message in (messages or [])
            for call in (message.get("tool_calls") or [])
        )
        assert read_before is True

    def test_a_string_in_the_tool_calls_column_does_not_crash_it(self):
        """Old rows stored looser shapes; a guard that raises on one turns a good answer into
        'I could not answer that'."""
        messages = [{"role": "assistant", "tool_calls": ["recall_role_experience"]}]
        read_before = any(
            isinstance(call, dict) and call.get("tool") == "recall_role_experience"
            for message in (messages or [])
            for call in (message.get("tool_calls") or [])
        )
        assert read_before is False


class TestTheGuardDoesNotFireOnAnEvidencedAnswer:
    """The guard is right about invention and was wrong about everything else.

    It exists because Operations called only describe_role and then invented a port number
    while claiming to have inspected records. Real, and worth catching. But the trigger was
    "did recall_role_experience run", which fires on EVERY role turn.

    Live, after the role prompt was already fixed: asked "how many rows are in the artifacts
    table? One number, nothing else", the Program Manager called query_db, got the number —
    and this block discarded that answer and re-asked with an instruction that MANDATES the
    opener. The operator got a biography instead of a number, and paid for two model turns.

    An answer built on query_db or factory_briefing is not invented. It is evidenced, just not
    from the panel record — an objective property of the trace, not a judgement about prose.
    """

    def _source(self):
        import inspect

        from boss_engine.services import intent_router

        return inspect.getsource(intent_router)

    def test_another_real_tool_call_counts_as_grounding(self):
        body = self._source()
        assert "grounded_otherwise" in body
        assert "not grounded_otherwise" in body

    def test_the_two_role_reads_do_not_count_as_grounding_themselves(self):
        """describe_role returns the role's own spec — reading it grounds nothing about the
        project, which is exactly the hole the original failure went through."""
        body = self._source()
        assert '_ROLE_READS = {"describe_role", "recall_role_experience"}' in body

    def test_a_failed_tool_call_does_not_count(self):
        """A query that errored produced no evidence; treating it as grounding would reopen
        the invention hole."""
        body = self._source()
        assert 'c.get("ok", True)' in body

    def test_the_reask_no_longer_mandates_the_opener(self):
        body = self._source()
        # The sentence that produced the biography, gone.
        assert "say plainly that you have no history here" not in body
        assert "that is not the answer and not" in body

    def test_the_reask_still_drops_unread_specifics(self):
        """The actual point of the guard survives: an invented number must not be repeated."""
        body = self._source()
        assert "DROP any" in body
        assert "you did not read" in body
