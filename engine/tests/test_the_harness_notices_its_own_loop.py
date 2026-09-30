"""A model cannot see that it is in a loop. The harness can.

Reported, watching a live thread:

    "it is going into a rabbit hole and never coming out of the loop.. it is erroring out but
     there's no option or solution given to the user, it simply drops the ball"

The thread: eighteen tool calls, seven of them `query_db` failures guessing at identifiers —
`workflows`, `work_class`, `workflow_key`, `graph`, `run_id`, `priority_band`, none of which
exist — and the operator's actual question ("what is the status?") never answered.

Nothing here is a model failing to reason. Each failure arrives ALONE, looks like a small
mistake, and correcting it is locally reasonable every single time. No message in the turn ever
said "this is the fourth attempt", so there was no moment at which stopping was the obvious
move. A loop is a property of the sequence, and only the thing holding the sequence can see it.

Three fixes, one per cause:
  * the failure now carries the real schema, so a wrong identifier costs one correction;
  * a workflow run has a read tool at last, so status needs no SQL;
  * and repeated failure of one tool triggers an explicit instruction to stop, prefer the
    purpose-built tool, and — if there genuinely is not one — treat that as a gap in BOSS and
    raise the fix.

That last one is the answer to "why doesn't BOSS fix BOSS": it always had `search_code` and
`create_code_task`. It was never given the trigger.
"""
from __future__ import annotations

import inspect

from boss_engine.agent.engine_agent import EngineAgent
from boss_engine.tools.builtin import _schema_hint_for, build_discuss_read_registry

#: The handlers are wrapped by the registry decorator, so `inspect.getsource(spec.handler)`
#: returns the wrapper. The text that matters is in the file.
_BUILTIN = inspect.getsource(__import__(
    "boss_engine.tools.builtin", fromlist=["x"]))


def _tool_source(name: str) -> str:
    """The body of one @reg.tool, from the file rather than through the decorator."""
    start = _BUILTIN.index(f'"{name}",')
    nxt = _BUILTIN.find("    @reg.tool(", start)
    return _BUILTIN[start:nxt if nxt > 0 else len(_BUILTIN)]


def _loop_source() -> str:
    return inspect.getsource(EngineAgent)


class TestTheTurnCountsItsOwnFailures:
    def test_a_repeated_failure_is_counted_per_tool(self):
        body = _loop_source()
        assert "tool_failures" in body
        # Per TOOL, not per turn: two different tools failing once each is not a loop.
        assert 'tool_failures[name] = tool_failures.get(name, 0) + 1' in body

    def test_the_third_failure_interrupts_with_an_instruction(self):
        body = _loop_source()
        assert "tool_failures[name] == 3" in body
        # Delivered as a message the MODEL reads. A log line would change nothing.
        assert "you are in a loop and cannot" in body

    def test_it_is_told_to_prefer_the_purpose_built_tool(self):
        body = _loop_source()
        assert "describe_workflow_run" in body
        assert "diagnose_run" in body

    def test_it_is_told_that_a_missing_tool_is_a_bug_it_may_fix(self):
        """The whole point. BOSS had the tools and no reason to reach for them."""
        body = _loop_source()
        assert "search_code" in body
        assert "create_code_task" in body
        assert "GAP IN " in body

    def test_it_must_answer_the_operator_rather_than_apologise(self):
        """"it simply drops the ball" — the turn ended on the tooling, not the question."""
        body = _loop_source()
        assert "ANSWER THE OPERATOR NOW" in body
        assert "apology about your own tooling" in body


class TestAFailedQueryCarriesTheSchema:
    def test_the_failure_path_returns_the_hint(self):
        body = _tool_source("query_db")
        assert "_schema_hint_for" in body
        # The raw Postgres error survives — it is true, just insufficient on its own.
        assert 'f"{type(error).__name__}' in body

    def test_tables_are_taken_from_the_statement(self):
        from boss_engine.tools.builtin import _SQL_TABLES

        found = {t.lower() for t in _SQL_TABLES.findall(
            "SELECT * FROM workflow_runs r JOIN workflow_step_runs s ON s.id = r.id")}
        assert found == {"workflow_runs", "workflow_step_runs"}

    def test_sql_keywords_are_not_mistaken_for_tables(self):
        from boss_engine.tools.builtin import _NOT_TABLES, _SQL_TABLES

        found = {t.lower() for t in _SQL_TABLES.findall("SELECT * FROM (SELECT 1) x")}
        assert found - _NOT_TABLES == set()

    def test_a_hint_that_fails_never_replaces_the_real_error(self):
        """Introspection is a nicety; the Postgres message is the fact."""
        body = inspect.getsource(_schema_hint_for)
        assert "return {}" in body
        assert "must not replace the real error" in body


class TestAFailedRunSaysWhatToDoAboutIt:
    def test_describe_workflow_run_exists_and_reads(self):
        registry = build_discuss_read_registry()
        assert "describe_workflow_run" in registry._tools
        assert registry._tools["describe_workflow_run"].kind == "read"

    def test_it_computes_a_remedy_rather_than_reporting_a_status(self):
        body = _tool_source("describe_workflow_run")
        assert "remedy" in body
        assert "do_this" in body
        # The specific case that was hit: started without an input the spec requires.
        assert "missing" in body
        assert "needs_from_operator" in body

    def test_it_refuses_a_code_task_run_by_name(self):
        """`crun_` and `wfr_` are different objects; being handed the wrong one silently is
        how an agent concludes a run does not exist."""
        body = _tool_source("describe_workflow_run")
        assert 'run_id.startswith("crun_")' in body

    def test_it_names_the_thing_not_to_do(self):
        """Offered no remedy, an agent reached for write_db to edit a dead run."""
        body = _tool_source("describe_workflow_run")
        assert "Do not edit workflow_runs directly" in body
