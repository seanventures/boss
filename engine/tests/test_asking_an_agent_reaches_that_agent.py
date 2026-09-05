"""Asking the QA Lead a question got BOSS, answering as itself.

The Agents page listed a workforce and its "Ask" link did this:

    ?area=agent&ask=@QA Lead:

The name was PROSE IN A PROMPT. The reply came from BOSS-in-general — no lens, no remit, no
memory of anything that role had argued — which is worse than having no expert, because a
generic answer under a name badge reads as authority.

The material for a real answer was already there and nobody read it:

  * `meeting_roles.lens`        — the perspective that role argues from
  * `agent_versions`            — its rules, skills and prompts, versioned
  * `meeting_runs.state`        — every position every role has taken on this project's
                                  panels, keyed by ROLE TITLE, in full prose

So this is retrieval over what the factory already records, not a new persona system and not a
new capture mechanism nobody would remember to write to.
"""
from __future__ import annotations

from boss_engine.services.intent_router import _role_identity
from boss_engine.tools.builtin import build_default_registry


def _tools():
    return {spec.name: spec for spec in build_default_registry().specs()}


class TestTheQuestionIsAddressed:
    def test_no_role_changes_nothing(self):
        """The general agent must stay exactly as it was."""
        assert _role_identity(None) == ""
        assert _role_identity("") == ""

    def test_the_role_is_named_to_the_model(self):
        assert "QA / Test" in _role_identity("QA / Test")

    def test_it_is_told_to_read_who_it_is_before_answering(self):
        block = _role_identity("QA / Test")
        assert "describe_role" in block
        assert "recall_role_experience" in block

    def test_it_may_not_imply_experience_it_cannot_point_at(self):
        """The failure mode of a persona: sounding experienced without having been there."""
        block = _role_identity("QA / Test")
        assert "Do not imply experience you cannot point at." in block
        assert "say so plainly" in block

    def test_it_is_told_to_stay_in_role_and_dissent(self):
        """A panel member who agrees with everything adds nothing."""
        block = _role_identity("Security")
        assert "Stay in role" in block
        assert "disagree" in block


class TestTheToolsThatMakeItReal:
    def test_a_role_can_be_described(self):
        assert "describe_role" in _tools()

    def test_a_role_can_read_its_own_past_arguments(self):
        assert "recall_role_experience" in _tools()

    def test_both_are_reads(self):
        """Asking an expert a question must not change anything."""
        tools = _tools()
        assert tools["describe_role"].kind == "read"
        assert tools["recall_role_experience"].kind == "read"

    def test_recall_resolves_a_key_to_the_title_the_rounds_are_keyed_by(self):
        """`meeting_runs.state` is keyed by TITLE ("QA / Test"), so a key like "qa" matches
        nothing unless it is resolved first."""
        import inspect

        body = inspect.getsource(_tools()["recall_role_experience"].handler)
        assert "FROM meeting_roles" in body
        assert "lower(role_key) = lower(:w)" in body

    def test_recall_reads_every_round_a_role_speaks_in(self):
        import inspect

        body = inspect.getsource(_tools()["recall_role_experience"].handler)
        for round_name in ("round1", "round2", "synthesis"):
            assert round_name in body

    def test_recall_says_when_there_is_no_experience_rather_than_returning_empty(self):
        """An empty list invites the model to fill the silence."""
        import inspect

        body = inspect.getsource(_tools()["recall_role_experience"].handler)
        assert "nothing recorded for this role yet" in body

    def test_describe_role_reads_the_agent_definition_when_there_is_one(self):
        """Rules and skills live in agent_versions, apart from the panel lens, so one can be
        extended without editing the other."""
        import inspect

        body = inspect.getsource(_tools()["describe_role"].handler)
        assert "agent_versions" in body
        assert "requested_skills" in body
