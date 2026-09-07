"""One agent, given the whole factory — and the Shepherd's judgement without its autopilot.

BOSS had three agent surfaces and the operator used one of them:

  * the chat area — a thread rail and a composer that knew nothing about the system;
  * the Metatron Captain — an always-on captain whose `metatron_sessions` table does not exist
    in this database, so the page was a shell over state that was never created;
  * the Task Shepherd — an autopilot switched off after one afternoon in which it opened repair
    tasks nobody asked about, retried a build the operator was already retrying by hand, and
    held the only worker slot for 33 minutes.

"I never used Metatron and Agent chat is not good... please come up with the new design for
common Agent and it should have full picture of the BOSS software factory."

What makes the agent good on the OTHER screens is not the model. Those screens hand it the facts
before the first word. This is that, for everything at once.

And the Shepherd's taxonomy — what an operational failure MEANS and what usually fixes it,
written from a session where 13 runs cost $66.65 and 58% produced nothing — was always the good
part. Acting on it unattended was the problem. Here it names each failure and offers the remedy
as a button.
"""
from __future__ import annotations

import inspect

import pytest

from boss_engine.services import task_shepherd
from boss_engine.services.factory_briefing import (
    PER_KIND,
    briefing_lines,
    factory_briefing,
)


class _Conn:
    """Answers each of the briefing's queries from a canned table."""

    def __init__(self, **by_table):
        self.by_table = by_table

    def execute(self, statement, params=None):
        text = str(statement)
        rows = []
        for table, value in self.by_table.items():
            if f"FROM {table}" in text:
                rows = value
                break

        class Result:
            def mappings(self_inner):
                return self_inner

            def all(self_inner):
                return rows

        return Result()


DECISION = {"id": "dec_1", "title": "Merge PR #186", "status": "pending", "risk_tier": 2,
            "deadline_at": "2026-09-07T02:33Z", "default_action": "hold",
            "created_at": "2026-09-06T02:31Z"}
STEP = {"id": "wfs_1", "step_id": "deploy", "status": "deadlettered",
        "last_error": "deployment failed: connection refused", "output": {},
        "workflow_run_id": "wfr_1", "updated_at": "2026-09-06T06:04Z",
        "input": {"title": "Build a lead triage inbox.\nWho it is for: a small team"}}


class TestTheBriefingIsTheWholeFactory:
    def test_it_covers_every_domain_that_can_stop(self):
        out = factory_briefing(_Conn(), "p")
        assert set(out["counts"]) == {"decisions", "workflow_steps", "unhealthy", "quiet_builds"}

    def test_a_quiet_factory_says_so_rather_than_inventing_work(self):
        out = factory_briefing(_Conn(), "p")
        assert out["quiet"] is True and out["total"] == 0

    def test_one_broken_section_does_not_take_the_others_with_it(self):
        """A briefing that fails because one table is missing tells the operator nothing about
        the four that are fine."""
        class Half(_Conn):
            def execute(self, statement, params=None):
                if "FROM decisions" in str(statement):
                    raise RuntimeError("relation does not exist")
                return super().execute(statement, params)

        out = factory_briefing(Half(workflow_step_runs=[STEP]), "p")
        assert out["counts"]["decisions"] == 0
        assert out["counts"]["workflow_steps"] == 1

    def test_an_unanswered_hold_says_why_it_waits_for_ever(self):
        """"Overdue" alone is a nag. An expired hold applies NOTHING, which is the reason to
        act — seven of these were why the factory was idle."""
        out = factory_briefing(_Conn(decisions=[DECISION]), "p")
        assert "applies nothing" in out["items"][0]["note"]

    def test_a_quiet_build_is_not_called_stalled(self):
        """The last build called stalled was cancelled, and it was fine."""
        conn = _Conn(code_task_runs=[{"id": "crun_1", "title": "Interview Scorecard App",
                                      "status": "running", "phase": "implement",
                                      "updated_at": "x", "quiet_seconds": 1700}])
        item = factory_briefing(conn, "p")["items"][0]
        assert "stall" not in item["note"].lower()
        assert item["quiet_minutes"] == 28

    def test_each_kind_is_bounded(self):
        """Thirty rows of "needs you" is the same as none: nobody reads to the bottom."""
        conn = _Conn(decisions=[{**DECISION, "id": f"dec_{i}"} for i in range(40)])
        assert factory_briefing(conn, "p")["counts"]["decisions"] <= PER_KIND


class TestTheShepherdsJudgementWithoutItsAutopilot:
    def test_a_failure_is_named_in_words(self):
        out = factory_briefing(_Conn(workflow_step_runs=[STEP]), "p")
        assert out["items"][0]["diagnosis"]["kind"] == "deployment failed"

    def test_and_carries_what_usually_fixes_it(self):
        out = factory_briefing(_Conn(workflow_step_runs=[STEP]), "p")
        remedies = out["items"][0]["diagnosis"]["remedies"]
        assert remedies and all("describe" in r for r in remedies)

    def test_the_taxonomy_is_the_shepherd_s_own(self):
        """Not a second table that drifts from it — the same classify() the shepherd used."""
        from boss_engine.services import factory_briefing as module

        body = inspect.getsource(module)
        assert "task_shepherd.classify(" in body
        assert "task_shepherd.REMEDIES" in body

    def test_nothing_is_run(self):
        """The one thing the autopilot got wrong. Remedies are described and offered; the
        briefing never calls one."""
        from boss_engine.services import factory_briefing as module

        body = inspect.getsource(module)
        assert "registry.invoke" not in body
        assert "_act_on_deployment" not in body

    def test_an_unclassifiable_failure_is_still_shown(self):
        out = factory_briefing(
            _Conn(workflow_step_runs=[{**STEP, "last_error": "something nobody has seen"}]), "p")
        assert out["total"] == 1

    @pytest.mark.parametrize("error,expected", [
        ("deployment failed: connection refused", "deployment failed"),
        ("merge conflict in server/index.js", "true merge conflict"),
    ])
    def test_the_classes_it_leans_on_still_exist(self, error, expected):
        assert task_shepherd.CLASS_NAMES[task_shepherd.classify(error)] == expected


class TestWhatTheAgentIsHanded:
    def test_the_lines_name_the_app_not_the_directive(self):
        lines = briefing_lines(factory_briefing(_Conn(workflow_step_runs=[STEP]), "p"))
        assert "Lead Triage Inbox" in lines[0]
        assert "Who it is for" not in lines[0]

    def test_and_the_failure_class_travels_with_the_error(self):
        lines = briefing_lines(factory_briefing(_Conn(workflow_step_runs=[STEP]), "p"))
        assert "(deployment failed)" in lines[0]

    def test_it_is_reachable_as_a_tool_so_the_agent_reads_the_same_picture(self):
        """The list an operator is looking at and the facts BOSS answers from must not be two
        different reads."""
        from boss_engine.tools import build_discuss_read_registry

        registry = build_discuss_read_registry()
        assert "factory_briefing" in registry.names()
        assert registry.get("factory_briefing").kind == "read"

    def test_the_tool_tells_the_model_to_start_there(self):
        from boss_engine.tools import builtin

        body = inspect.getsource(builtin)
        assert "Call this FIRST when asked an open " in body
        assert "instead of assembling the same picture from six separate " in body
