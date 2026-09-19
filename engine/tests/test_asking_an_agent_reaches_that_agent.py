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
        assert "Never imply experience you cannot point at." in block

    def test_but_a_missing_record_is_not_the_opening_subject(self):
        """The honesty rule was right; "say so plainly" as an OPENER was not.

        It read "if nothing is recorded for this role yet, say so plainly", and followed
        literally — the only way to follow it — every reply began with the role's own empty
        history. Asked for a single row count, the Program Manager opened "I have no recorded
        history on this project."
        """
        block = _role_identity("QA / Test")
        assert "say so plainly" not in block
        assert "do NOT open with" in block
        assert "only if the operator" in block

    def test_the_question_is_answered_first_and_the_role_is_the_lens(self):
        block = _role_identity("QA / Test")
        assert "ANSWER THE OPERATOR'S QUESTION, FULLY AND FIRST" in block
        assert "LENS" in block
        assert "If they asked for data, give the data" in block

    def test_the_two_role_reads_are_not_mandatory_on_every_turn(self):
        """"FIRST call describe_role… Then call recall_role_experience" ran two lookups before
        every answer, including "how many rows are in this table"."""
        block = _role_identity("QA / Test")
        assert "FIRST call describe_role" not in block
        assert "Neither is required" in block

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
        assert "no past contributions recorded for this role" in body

    def test_describe_role_reads_the_agent_definition_when_there_is_one(self):
        """Rules and skills live in agent_versions, apart from the panel lens, so one can be
        extended without editing the other."""
        import inspect

        body = inspect.getsource(_tools()["describe_role"].handler)
        assert "agent_versions" in body
        assert "requested_skills" in body

    def test_a_role_is_defined_by_its_spec_not_by_an_optional_extension(self):
        """The second half of "there is no Program Manager role defined on this project".

        Naming the role fixed the first half; the agent then said it had "no agent definition
        and no lens" and "nothing to argue from" — while sitting on a spec with a mission,
        two boundary lists, decision rights and a comms contract. `roles.spec` was selected
        as `NULL::text AS lens` and discarded, and `has_agent_definition` reported on an
        `agent_versions` row that no role on any project has.

        An agent_version EXTENDS a role. The spec BRINGS IT INTO BEING.
        """
        import inspect

        body = inspect.getsource(_tools()["describe_role"].handler)
        # The spec is read, not nulled out.
        assert "NULL::text AS lens" not in body
        assert "spec" in body
        # And the substance of it reaches the model, so "answer as this role" has a source.
        for field in ("mission", "primary_outcomes", "decision_rights", "comms_contract"):
            assert field in body, f"describe_role drops {field}"
        # The mission doubles as the lens for a workforce role, which has no panel seat.
        assert 'spec.get("lens") or spec.get("mission")' in body

    def test_describe_role_does_not_invite_the_agent_to_plead_undefined(self):
        """`has_agent_definition: false` was read out to the OPERATOR as a defect.

        A model handed a false-looking flag explains it. The flag was about an optional
        extension, but the sentence it produced — "I have no panel positions here and nothing
        in the record to agree or disagree with" — reads to a customer as a broken product.
        """
        import inspect

        body = inspect.getsource(_tools()["describe_role"].handler)
        assert '"has_agent_definition": True' in body
        # Which of the two sources answered is still reported — truthfully, but as provenance
        # rather than as an absence.
        assert '"definition_source": "spec"' in body
        assert '"definition_source": "agent_version"' in body
        assert "never say it is not" in body


class TestTheEndpointAndTheServiceAgree:
    """The regression this suite did not catch, and the reason it did not.

    `role` was added to the API request and forwarded as `chat(role=...)`, and the parameter
    landed on `start_or_load_thread` — a different method with the same tail of arguments
    (`surface`, `external_ref`). `AgentService.chat` never took it, so EVERY chat raised
    TypeError and the console's agent returned 500. Not just role-addressed chats: all of them.

    2769 tests passed, because every one of them exercised the router or the service directly
    and none crossed the boundary where the endpoint calls the service. A keyword argument is
    a contract between two files, and it was checked by neither.
    """

    #: What the endpoint actually passes. Kept here so adding an argument on one side without
    #: the other fails a test rather than a production request.
    ENDPOINT_KWARGS = {
        "project_id", "actor", "message", "thread_id",
        "confirm_plan_id", "surface", "external_ref", "role",
    }

    def test_the_service_accepts_everything_the_endpoint_sends(self):
        import inspect

        from boss_engine.services.agent_service import AgentService

        accepted = set(inspect.signature(AgentService.chat).parameters)
        missing = self.ENDPOINT_KWARGS - accepted
        assert not missing, f"the endpoint passes {sorted(missing)} and chat() does not take it"

    def test_the_endpoint_really_does_pass_them(self):
        """Otherwise the set above drifts into fiction and guards nothing."""
        import pathlib
        import re

        main = pathlib.Path(__file__).resolve().parents[2] / "apps/api/app/main.py"
        source = main.read_text()
        call = source[source.index("result = orchestration_agent.chat("):][:900]
        passed = set(re.findall(r"^\s+(\w+)=", call, re.M))
        assert self.ENDPOINT_KWARGS <= passed | {"project_id"}, (
            f"endpoint passes {sorted(passed)}")

    def test_the_router_accepts_what_the_service_forwards(self):
        """The same contract, one layer down."""
        import inspect

        from boss_engine.services.intent_router import IntentRouter

        accepted = set(inspect.signature(IntentRouter.route_turn).parameters)
        assert "role" in accepted


class TestTheRoleIsALensNotTheSubject:
    """The over-correction, and what it cost.

    Fixing "there is no Program Manager role defined on this project" meant telling the model
    to answer AS the role, from its mission and boundaries. That instruction said nothing about
    answering the QUESTION — so the pendulum swung all the way through. Asked to run two SQL
    queries and paste the rows, the Program Manager replied with several hundred words about
    never having been seated on a panel on this project. True, and not what was asked.

    Reported as: "I dont see any recommendations or identifying the right issue, the Agent
    quality is very bad and no one can work with it."

    A specialist brings their lens TO your question. They do not answer a question about a
    failing step with their own job description.
    """

    def _describe(self):
        import inspect

        return inspect.getsource(_tools()["describe_role"].handler)

    def test_the_question_comes_first(self):
        body = self._describe()
        assert "ANSWER THE OPERATOR'S ACTUAL QUESTION FIRST" in body
        assert "LENS" in body

    def test_it_must_not_open_with_its_own_biography(self):
        body = self._describe()
        assert "Do not open with who you are" in body
        # The specific failure: narrating gaps in its own configuration at the operator.
        assert "missing from your" in body

    def test_asked_for_data_it_gives_data(self):
        assert "If asked for data, give the data" in self._describe()

    def test_an_empty_memory_is_not_a_topic(self):
        """`recall_role_experience` told the model to SAY its answer was not from experience
        here, so every reply disclosed an absence nobody had asked about."""
        import inspect

        body = inspect.getsource(_tools()["recall_role_experience"].handler)
        # Fragments that live inside ONE string literal: an assertion spanning a
        # concatenation boundary fails on formatting rather than on meaning.
        assert "Do NOT make this absence the" in body
        assert "mention it only if the operator asked" in body

    def test_it_still_never_claims_to_be_undefined(self):
        """The original bug must not come back while fixing its cure."""
        assert "never say it is not" in self._describe()
