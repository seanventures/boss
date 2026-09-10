"""Starting work from a conversation means starting a WORKFLOW.

The operator asked BOSS to research a stock-recommendation site. BOSS offered — and then ran —
a BARE discovery: `drun_3e542dc4…`, completed, a finished dossier that belonged to no run. The
console has no standing page for one, deliberately ("no one would/should independently kick off
the discovery and meeting panel"), so the only thing that could have linked to it was a workflow
run, and there wasn't one. The operator's conclusion was that nothing had happened.

Research and the PRD panel are STEPS. The unit of work is the workflow and its document is the
outcome.
"""
from boss_engine.tools.builtin import build_discuss_read_registry, build_default_registry
from boss_engine.agent.engine_agent import EngineAgent


def test_the_chat_agent_cannot_start_a_bare_discovery_or_panel():
    names = set(build_discuss_read_registry().names())
    assert "create_discovery" not in names
    assert "request_meeting" not in names


def test_it_can_start_a_workflow_instead():
    names = set(build_discuss_read_registry().names())
    assert "start_workflow" in names


def test_the_workflow_steps_still_have_the_tools_they_run():
    # Withdrawing them from the CHAT registry must not disarm the workflow itself: the research
    # step is `create_discovery` and the meeting step is `request_meeting`.
    names = set(build_default_registry().names())
    assert {"create_discovery", "request_meeting"} <= names


def test_starting_a_workflow_is_offered_not_executed():
    # The most expensive thing BOSS can start from a sentence.
    assert "start_workflow" in EngineAgent.PROPOSE_ONLY


def test_the_button_names_the_workflow_rather_than_the_tool():
    from boss_engine.agent.engine_agent import _propose_label

    label = _propose_label("start_workflow",
                           {"workflow_key": "research_led_discovery", "title": "Day-trading site"})
    assert "Research Led Discovery" in label
    assert "Day-trading site" in label


def test_the_discussion_offers_the_workflow_not_a_research_run():
    from boss_engine.services.intent_router import _discussion_brief

    brief = _discussion_brief()
    assert "Research-Led Discovery WORKFLOW" in brief
    assert "never a thing started on its own" in brief
