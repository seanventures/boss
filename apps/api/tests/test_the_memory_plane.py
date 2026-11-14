"""BOSS remembers what it was told, and a long session remembers what it was doing.

Phase A0 of the Kiro Crew gap analysis (docs/analysis/kiro-gap-architecture.md). BOSS had 249
tables and not one stored anything an agent was told: every thread started cold, and the same
correction had to be repeated in each new conversation. Kiro Crew's own account of scaling puts
this first — "Stage 2 to 3 is the one that pays", and stage 3 is memory.

Written against a real database because every rule here is a constraint, a conflict clause or a
scope predicate, and none of those can be checked by reading the source.
"""
from __future__ import annotations

import pytest
from sqlalchemy import text

from apps.api.app.config import get_settings
from boss_engine.services import agent_memory as mem


@pytest.fixture(autouse=True)
def _clean(engine):
    with engine.begin() as c:
        c.execute(text("DELETE FROM agent_memory"))
        c.execute(text("DELETE FROM agent_session_ledger"))
    yield


def _pid():
    return get_settings().default_project_id


class TestALessonIsARuleAndDoesNotBindUntilReviewed:
    """Where BOSS is deliberately stricter than Kiro Crew, which writes lessons straight out."""

    def test_a_lesson_arrives_proposed(self, engine):
        with engine.begin() as c:
            row = mem.remember(c, project_id=_pid(), actor="ceo", kind="lesson",
                               body="always run the frontend checks before calling it done")
        assert row["status"] == "proposed"

    def test_a_proposed_lesson_is_not_injected(self, engine):
        with engine.begin() as c:
            mem.remember(c, project_id=_pid(), actor="ceo", kind="lesson",
                         body="always run the frontend checks before calling it done")
            packed = mem.pack_for_turn(c, project_id=_pid(), actor="ceo")
        assert "frontend checks" not in packed

    def test_reviewing_it_makes_it_bind(self, engine):
        with engine.begin() as c:
            row = mem.remember(c, project_id=_pid(), actor="ceo", kind="lesson",
                               body="always run the frontend checks before calling it done")
            mem.review(c, project_id=_pid(), memory_id=row["id"], actor="ceo",
                       decision="active")
            packed = mem.pack_for_turn(c, project_id=_pid(), actor="ceo")
        assert "frontend checks" in packed
        assert "[RULE]" in packed

    def test_a_rejected_lesson_stays_silent(self, engine):
        with engine.begin() as c:
            row = mem.remember(c, project_id=_pid(), actor="ceo", kind="lesson",
                               body="never write tests")
            mem.review(c, project_id=_pid(), memory_id=row["id"], actor="ceo",
                       decision="rejected")
            packed = mem.pack_for_turn(c, project_id=_pid(), actor="ceo")
        assert "never write tests" not in packed

    def test_context_and_preferences_inform_without_review(self, engine):
        """They shape an answer rather than instructing one, so they are active on arrival."""
        with engine.begin() as c:
            mem.remember(c, project_id=_pid(), actor="ceo", kind="preference",
                         body="prefers short answers with the conclusion first")
            packed = mem.pack_for_turn(c, project_id=_pid(), actor="ceo")
        assert "conclusion first" in packed


class TestSaidTwiceIsStillOneRule:
    def test_the_same_correction_does_not_become_two_entries(self, engine):
        with engine.begin() as c:
            a = mem.remember(c, project_id=_pid(), actor="ceo", kind="preference",
                             body="Prefers Short Answers")
            b = mem.remember(c, project_id=_pid(), actor="ceo", kind="preference",
                             body="  prefers short answers  ")
            rows = mem.recall(c, project_id=_pid(), actor="ceo")
        assert a["id"] == b["id"]
        assert len(rows) == 1


class TestScopeDoesNotLeak:
    def test_one_actors_memory_is_not_anothers(self, engine):
        with engine.begin() as c:
            mem.remember(c, project_id=_pid(), actor="ceo", kind="preference",
                         body="ceo likes terse replies", scope="actor", scope_ref="ceo")
            mine = mem.pack_for_turn(c, project_id=_pid(), actor="ceo")
            theirs = mem.pack_for_turn(c, project_id=_pid(), actor="someone_else")
        assert "terse replies" in mine
        assert "terse replies" not in theirs

    def test_a_role_scoped_memory_reaches_that_role(self, engine):
        with engine.begin() as c:
            mem.remember(c, project_id=_pid(), actor="ceo", kind="project",
                         body="this project publishes to a family channel",
                         scope="role", scope_ref="media_publisher")
            hit = mem.pack_for_turn(c, project_id=_pid(), actor="ceo",
                                    role="media_publisher")
            miss = mem.pack_for_turn(c, project_id=_pid(), actor="ceo", role="qa")
        assert "family channel" in hit
        assert "family channel" not in miss

    def test_actor_and_role_scopes_require_a_reference(self, engine):
        with engine.begin() as c:
            with pytest.raises(mem.MemoryError_) as caught:
                mem.remember(c, project_id=_pid(), actor="ceo", kind="preference",
                             body="x", scope="actor")
        assert "scope_ref" in str(caught.value)


class TestInjectionIsBudgeted:
    def test_it_stops_at_the_budget_rather_than_truncating(self, engine):
        """Half a rule still reads like an instruction, which is worse than no rule."""
        with engine.begin() as c:
            for i in range(40):
                mem.remember(c, project_id=_pid(), actor="ceo", kind="project",
                             body=f"context entry number {i} " + ("padding " * 20))
            packed = mem.pack_for_turn(c, project_id=_pid(), actor="ceo", budget=500)
        body = packed.split("===\n", 1)[-1]
        assert len(body) < 900
        # Every line that made it is whole.
        for line in body.splitlines():
            if line.startswith("- ["):
                assert line.endswith("padding ") or "context entry" in line

    def test_a_binding_rule_outranks_a_preference_under_pressure(self, engine):
        with engine.begin() as c:
            for i in range(30):
                mem.remember(c, project_id=_pid(), actor="ceo", kind="preference",
                             body=f"preference number {i} " + ("pad " * 30))
            rule = mem.remember(c, project_id=_pid(), actor="ceo", kind="lesson",
                                body="never deploy on a Friday")
            mem.review(c, project_id=_pid(), memory_id=rule["id"], actor="ceo",
                       decision="active")
            packed = mem.pack_for_turn(c, project_id=_pid(), actor="ceo", budget=400)
        assert "never deploy on a Friday" in packed

    def test_a_memory_must_be_a_sentence_not_a_transcript(self, engine):
        with engine.begin() as c:
            with pytest.raises(mem.MemoryError_) as caught:
                mem.remember(c, project_id=_pid(), actor="ceo", kind="project",
                             body="x" * 2100)
        assert "sentence" in str(caught.value)


class TestForgettingIsAuditable:
    def test_retired_not_deleted(self, engine):
        with engine.begin() as c:
            row = mem.remember(c, project_id=_pid(), actor="ceo", kind="project",
                               body="a thing that stopped being true")
            mem.forget(c, project_id=_pid(), memory_id=row["id"], actor="ceo")
            packed = mem.pack_for_turn(c, project_id=_pid(), actor="ceo")
            still_there = c.execute(
                text("SELECT status FROM agent_memory WHERE id=:i"),
                {"i": row["id"]}).scalar_one()
        assert "stopped being true" not in packed
        assert still_there == "retired"


class TestTheSessionLedger:
    def test_a_new_ledger_needs_a_goal(self, engine):
        with engine.begin() as c:
            with pytest.raises(mem.MemoryError_):
                mem.write_ledger(c, project_id=_pid(), thread_id="athr_x", actor="ceo",
                                 phase="thinking")

    def test_it_carries_goal_phase_and_next_step(self, engine):
        with engine.begin() as c:
            mem.write_ledger(c, project_id=_pid(), thread_id="athr_x", actor="ceo",
                             goal="ship the video workflow",
                             phase="wiring inputs",
                             next_step="add the Windows branch, the test already fails")
            packed = mem.pack_ledger(c, thread_id="athr_x")
        assert "ship the video workflow" in packed
        assert "add the Windows branch" in packed

    def test_rejected_approaches_append_and_are_warned_against(self, engine):
        """The costliest thing a resumed session does is re-run a failed approach."""
        with engine.begin() as c:
            mem.write_ledger(c, project_id=_pid(), thread_id="athr_x", actor="ceo",
                             goal="fix the panel", rejected="retrying the dead meeting id")
            mem.write_ledger(c, project_id=_pid(), thread_id="athr_x", actor="ceo",
                             rejected="setting OPENAI_API_KEY")
            packed = mem.pack_ledger(c, thread_id="athr_x")
        assert "do not repeat" in packed.lower()
        assert "retrying the dead meeting id" in packed
        assert "setting OPENAI_API_KEY" in packed

    def test_the_rejected_list_is_bounded(self, engine):
        with engine.begin() as c:
            mem.write_ledger(c, project_id=_pid(), thread_id="athr_x", actor="ceo",
                             goal="g")
            for i in range(30):
                mem.write_ledger(c, project_id=_pid(), thread_id="athr_x", actor="ceo",
                                 rejected=f"approach {i}")
            led = mem.read_ledger(c, thread_id="athr_x")
        assert len(led["rejected"]) == mem.MAX_REJECTED_ENTRIES
        assert "approach 29" in led["rejected"]

    def test_a_finished_ledger_stops_steering(self, engine):
        """"A stale ledger quietly steering a session that has moved on"."""
        with engine.begin() as c:
            mem.write_ledger(c, project_id=_pid(), thread_id="athr_x", actor="ceo",
                             goal="ship the video workflow")
            mem.finish_ledger(c, thread_id="athr_x", actor="ceo")
            assert mem.read_ledger(c, thread_id="athr_x") is None
            assert mem.pack_ledger(c, thread_id="athr_x") == ""

    def test_writing_again_reopens_it(self, engine):
        with engine.begin() as c:
            mem.write_ledger(c, project_id=_pid(), thread_id="athr_x", actor="ceo", goal="g")
            mem.finish_ledger(c, thread_id="athr_x", actor="ceo")
            mem.write_ledger(c, project_id=_pid(), thread_id="athr_x", actor="ceo",
                             next_step="the work came back")
            led = mem.read_ledger(c, thread_id="athr_x")
        assert led is not None and led["next_step"] == "the work came back"


class TestDecay:
    def test_an_unused_memory_is_retired(self, engine):
        with engine.begin() as c:
            row = mem.remember(c, project_id=_pid(), actor="ceo", kind="project",
                               body="never once relevant")
            c.execute(text("UPDATE agent_memory SET created_at = now() - interval '200 days' "
                           " WHERE id=:i"), {"i": row["id"]})
            result = mem.prune(c, project_id=_pid(), unused_days=90)
        assert result["retired_unused"] == 1

    def test_a_used_memory_survives_but_mere_injection_is_not_use(self, engine):
        """BOSSAPP-960: pack_for_turn used to count as use, so anything ever injected could
        never decay — the loop measured its own output. Use is a value signal (the same thing
        taught again); a memory that was only ever packed ages out like one never packed."""
        with engine.begin() as c:
            row = mem.remember(c, project_id=_pid(), actor="ceo", kind="project",
                               body="still matters")
            mem.remember(c, project_id=_pid(), actor="ceo", kind="project",
                         body="still matters")                        # re-taught: the use signal
            only_packed = mem.remember(c, project_id=_pid(), actor="ceo", kind="project",
                                       body="injected every turn, never once mattered")
            mem.pack_for_turn(c, project_id=_pid(), actor="ceo")      # injection, not use
            c.execute(text("UPDATE agent_memory SET created_at = now() - interval '200 days' "
                           " WHERE id IN (:i, :j)"), {"i": row["id"], "j": only_packed["id"]})
            mem.prune(c, project_id=_pid(), unused_days=90)
            packed = mem.pack_for_turn(c, project_id=_pid(), actor="ceo")
        assert "still matters" in packed
        assert "never once mattered" not in packed

    def test_a_reviewed_rule_does_not_decay_from_disuse(self, engine):
        """Review was a deliberate act; ageing it out silently would undo a human decision."""
        with engine.begin() as c:
            row = mem.remember(c, project_id=_pid(), actor="ceo", kind="lesson",
                               body="never deploy on a Friday")
            mem.review(c, project_id=_pid(), memory_id=row["id"], actor="ceo",
                       decision="active")
            c.execute(text("UPDATE agent_memory SET created_at = now() - interval '400 days' "
                           " WHERE id=:i"), {"i": row["id"]})
            mem.prune(c, project_id=_pid(), unused_days=90)
            packed = mem.pack_for_turn(c, project_id=_pid(), actor="ceo")
        assert "never deploy on a Friday" in packed


class TestItReachesTheTurn:
    """The plane is inert unless what it stores arrives in the prompt.

    A0.1's acceptance is "a correction in one thread changes behaviour in the next", and the
    only way to check that without paying for a model call is to assert on the system prompt
    the router assembles.
    """

    def test_an_active_memory_is_injected(self, engine):
        from boss_engine.services.intent_router import _remembered_context

        with engine.begin() as c:
            mem.remember(c, project_id=_pid(), actor="ceo", kind="project",
                         body="the family channel is Makkal Sinthanai")
            block = _remembered_context(c, _pid(), "ceo", None, None)
        assert "Makkal Sinthanai" in block
        assert "WHAT YOU ALREADY KNOW" in block

    def test_a_proposed_lesson_is_not(self, engine):
        from boss_engine.services.intent_router import _remembered_context

        with engine.begin() as c:
            mem.remember(c, project_id=_pid(), actor="ceo", kind="lesson",
                         body="deploy only on Tuesdays")
            block = _remembered_context(c, _pid(), "ceo", None, None)
        assert "Tuesdays" not in block

    def test_the_ledger_rides_along_when_there_is_a_thread(self, engine):
        from boss_engine.services.intent_router import _remembered_context

        with engine.begin() as c:
            mem.write_ledger(c, project_id=_pid(), thread_id="athr_inj", actor="ceo",
                             goal="finish the Kiro gap analysis",
                             next_step="wire the memory plane into the turn")
            block = _remembered_context(c, _pid(), "ceo", None, "athr_inj")
        assert "finish the Kiro gap analysis" in block
        assert "wire the memory plane" in block

    def test_an_empty_project_pays_nothing(self, engine):
        from boss_engine.services.intent_router import _remembered_context

        with engine.begin() as c:
            assert _remembered_context(c, _pid(), "ceo", None, None) == ""

    def test_a_failure_never_costs_the_answer(self, engine):
        """An agent that cannot answer because its memory lookup broke is strictly worse than
        one that answers without memory."""
        from boss_engine.services.intent_router import _remembered_context

        class Broken:
            def execute(self, *a, **k):
                raise RuntimeError("database is on fire")

        assert _remembered_context(Broken(), _pid(), "ceo", None, None) == ""


class TestTheToolsThemselves:
    """Invoked through the REGISTRY, which is the path that actually broke.

    The service tests above all passed while `remember` raised `AttributeError:
    'EngineContext' object has no attribute 'ambient'` on every real call. The tools read
    `ctx.ambient`, but `engine_agent` merges ambient values into the ARGUMENT dict
    (`args={**self.ambient, **args, ...}`) and never puts them on the context. Nothing between
    the service and the model exercised that seam.

    Found in production by BOSS itself, which traced it to the line and refused to invent a
    workaround.
    """

    def _invoke(self, engine, name, args):
        from apps.api.app.config import get_settings as _gs
        from boss_engine.context import EngineContext
        from boss_engine.tools import build_default_registry

        ctx = EngineContext(settings=_gs(), engine=engine)
        return build_default_registry().invoke(
            name, ctx, project_id=_pid(), actor="ceo", args=args)

    def test_remember_works_through_the_registry(self, engine):
        result = self._invoke(engine, "remember", {
            "kind": "project", "body": "the family channel is Makkal Sinthanai"})
        assert result["ok"] is True, result
        assert result["status"] == "active"

    def test_it_keeps_the_thread_that_taught_it(self, engine):
        """Provenance: a surprising rule has to be traceable to the conversation that set it.
        The thread id arrives as an ambient `_thread_id` in args."""
        self._invoke(engine, "remember", {
            "kind": "project", "body": "drive is the handover point",
            "_thread_id": "athr_source"})
        with engine.begin() as c:
            got = c.execute(text(
                "SELECT source_thread_id FROM agent_memory WHERE body LIKE 'drive is%'"
            )).scalar_one()
        assert got == "athr_source"

    def test_a_lesson_tool_call_says_it_is_not_yet_binding(self, engine):
        """The model must not tell the operator a rule is in force when it is awaiting review."""
        result = self._invoke(engine, "remember", {
            "kind": "lesson", "body": "always run the frontend checks first"})
        assert result["status"] == "proposed"
        assert "does not bind" in result["note"]

    def test_update_ledger_works_without_a_thread(self, engine):
        """AP-P0: over MCP and in agent steps nothing injects `_thread_id`, and the tool used to
        refuse every call there. It now keeps one ledger per actor (or per `session_id`)."""
        result = self._invoke(engine, "update_ledger", {"goal": "x"})
        assert result["ok"] is True, result
        assert result["thread_id"].startswith("session:")

    def test_update_ledger_writes_and_recall_reads(self, engine):
        wrote = self._invoke(engine, "update_ledger", {
            "_thread_id": "athr_tool", "goal": "ship phase A0",
            "next_step": "wire injection", "rejected": "reading ctx.ambient"})
        assert wrote["ok"] is True, wrote
        with engine.begin() as c:
            packed = mem.pack_ledger(c, thread_id="athr_tool")
        assert "ship phase A0" in packed
        assert "reading ctx.ambient" in packed

    def test_recall_returns_what_was_remembered(self, engine):
        self._invoke(engine, "remember", {"kind": "project", "body": "a recorded fact"})
        result = self._invoke(engine, "recall", {})
        assert result["count"] == 1
        assert "a recorded fact" in result["memories"][0]["body"]

    def test_forget_retires_it(self, engine):
        made = self._invoke(engine, "remember", {"kind": "project", "body": "no longer true"})
        gone = self._invoke(engine, "forget", {"memory_id": made["id"]})
        assert gone["ok"] is True
        assert self._invoke(engine, "recall", {})["count"] == 0

    def test_no_memory_tool_reads_ctx_ambient(self):
        """The specific mistake, pinned. Ambient values are arguments, not context."""
        import inspect

        from boss_engine.tools import builtin

        for name in ("remember", "recall", "forget", "update_ledger"):
            start = builtin.__file__  # noqa: F841 - readability
        source = inspect.getsource(builtin)
        for line in source.splitlines():
            if "ctx.ambient" in line:
                assert line.strip().startswith("#"), (
                    f"a tool reads ctx.ambient again: {line.strip()}")


class TestTeachingItAgainAfterRetiring:
    """Reported by an independent review: a silent no-op that reported success.

    `ON CONFLICT … DO UPDATE` only touched `updated_at`, so re-recording something the operator
    had retired left the row `retired` — invisible to `recall`, in no review queue, unable to
    ever bind — while the tool returned ok and said it was waiting for review.
    """

    def test_a_retired_memory_comes_back(self, engine):
        with engine.begin() as c:
            first = mem.remember(c, project_id=_pid(), actor="ceo", kind="project",
                                 body="the handover is Google Drive")
            mem.forget(c, project_id=_pid(), memory_id=first["id"], actor="ceo")
            again = mem.remember(c, project_id=_pid(), actor="ceo", kind="project",
                                 body="the handover is Google Drive")
            packed = mem.pack_for_turn(c, project_id=_pid(), actor="ceo")
        assert again["id"] == first["id"]
        assert again["status"] == "active"
        assert "Google Drive" in packed

    def test_a_rejected_rule_comes_back_as_proposed_not_active(self, engine):
        """A human refused it once, so a human decides again."""
        with engine.begin() as c:
            first = mem.remember(c, project_id=_pid(), actor="ceo", kind="lesson",
                                 body="skip the tests when in a hurry")
            mem.review(c, project_id=_pid(), memory_id=first["id"], actor="ceo",
                       decision="rejected")
            again = mem.remember(c, project_id=_pid(), actor="ceo", kind="lesson",
                                 body="skip the tests when in a hurry")
            packed = mem.pack_for_turn(c, project_id=_pid(), actor="ceo")
        assert again["status"] == "proposed"
        assert "skip the tests" not in packed

    def test_an_active_memory_is_not_disturbed_by_being_repeated(self, engine):
        with engine.begin() as c:
            first = mem.remember(c, project_id=_pid(), actor="ceo", kind="lesson",
                                 body="always run the frontend checks first")
            mem.review(c, project_id=_pid(), memory_id=first["id"], actor="ceo",
                       decision="active")
            again = mem.remember(c, project_id=_pid(), actor="ceo", kind="lesson",
                                 body="always run the frontend checks first")
        assert again["status"] == "active", "repeating a live rule must not un-approve it"
