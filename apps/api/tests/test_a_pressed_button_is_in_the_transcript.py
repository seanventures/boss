"""Pressing a button must change the conversation, not just the browser.

Reported, AFTER the previous round of loop fixes shipped:

    "I just did all the operations mentioned, and then nothing happened, no workflow got
    created, then I refreshed the page went back to the Chat agent and then again it displays
    all 4 operations again — the agent just loops like this."

Both symptoms are one absence. `/v1/engine/agent/action` ran the tool, emitted an audit event,
returned the result, and never wrote to the thread:

  * The tick and the spent offer were React state. `runAgentActionAction` calls
    `revalidatePath("/")`, which re-renders the page and discards exactly that state — so the
    press left no mark even BEFORE a refresh. That is "nothing happened".
  * The stored assistant message still carried all of its `proposed_actions`, so reopening the
    thread re-rendered every button. That is "it displays all 4 operations again".
  * And the next turn reads that same stored history. The model saw an offer with no outcome,
    concluded the operator had not chosen, and offered it again — the loop itself.

The previous fix fed FAILURES back to the model from the client. This is the other half, and
the half the operator actually hit: a SUCCESS was invisible too.

Written against a real database, because the bug lived precisely in what was and was not
persisted — a source-grep suite would have passed the whole way through.
"""
from __future__ import annotations

from apps.api.app.config import get_settings
from boss_engine.services.agent_service import AgentService

OFFERS = [
    {"id": "act_publish_v3", "tool": "draft_workflow", "label": "Publish v3", "args": {}},
    {"id": "act_retire_a", "tool": "retire_workflow", "label": "Retire intro key", "args": {}},
    {"id": "act_retire_b", "tool": "retire_workflow", "label": "Retire trim key", "args": {}},
]


def _thread_with_offers(engine):
    """A reply that put three buttons above itself, as a real turn does."""
    settings = get_settings()
    service = AgentService(settings)
    with engine.begin() as connection:
        thread_id = service.start_or_load_thread(
            connection, project_id=settings.default_project_id, actor="ceo")["id"]
        service.append_message(
            connection, thread_id=thread_id, role="assistant",
            content="BOSS can do this for you:", proposed_actions=OFFERS)
    return service, thread_id


def _rendered(service, engine, thread_id):
    """The offers the console would draw: the NEWEST assistant reply's, and only those."""
    with engine.begin() as connection:
        history = service.list_messages(connection, thread_id)
    for message in reversed(history):
        if message.get("role") == "assistant":
            return {o["id"] for o in (message.get("proposed_actions") or [])}
    return set()


def _press(service, engine, thread_id, *, action_id="act_publish_v3", ok=True,
           result=None, error=""):
    with engine.begin() as connection:
        service.record_action_outcome(
            connection, thread_id=thread_id, action_id=action_id, tool="draft_workflow",
            args={"workflow_key": "family_channel_video"}, label="Publish v3", ok=ok,
            result=result if result is not None else {"ok": ok}, error=error)
    with engine.begin() as connection:
        return service.list_messages(connection, thread_id)


def test_a_successful_press_survives_the_refresh(engine):
    service, thread_id = _thread_with_offers(engine)
    history = _press(service, engine, thread_id,
                     result={"ok": True, "status": "published", "version": 3})

    # THE PRESS IS IN THE TRANSCRIPT. Not an event row — the model does not read events.
    assert any(m.get("role") == "assistant" and "Publish v3" in (m.get("content") or "")
               for m in history), "a pressed button left no trace in the conversation"
    outcome = history[-1]["content"]
    # Said in words the next turn will read, because that turn is where the loop happened.
    assert "SUCCEEDED" in outcome
    assert "Do not offer it again" in outcome
    # WHAT it did, not merely that it did something.
    assert "published" in outcome


def test_the_pressed_offer_does_not_come_back(engine):
    """"I refreshed and it displays all 4 operations again"."""
    service, thread_id = _thread_with_offers(engine)
    _press(service, engine, thread_id)
    assert "act_publish_v3" not in _rendered(service, engine, thread_id)


def test_the_buttons_you_did_not_press_do_come_back(engine):
    """The console renders only the NEWEST reply's offers.

    So appending an outcome carrying none would retire the two unrelated cleanup buttons as
    well — a second way for one press to look like it broke something.
    """
    service, thread_id = _thread_with_offers(engine)
    _press(service, engine, thread_id)
    assert _rendered(service, engine, thread_id) == {"act_retire_a", "act_retire_b"}


def test_a_failed_press_asks_for_a_corrected_action_not_the_same_one(engine):
    service, thread_id = _thread_with_offers(engine)
    reason = "step 'edit' needs 'media_artifact_id', which is not set"
    history = _press(service, engine, thread_id, ok=False,
                     result={"ok": False, "error": reason}, error=reason)

    outcome = history[-1]["content"]
    assert "FAILED" in outcome
    # In a code fence: markdown renders the identifier as "mediaartifactid" outside one, and an
    # identifier you cannot copy is an error you cannot act on. Reported verbatim once already.
    assert "```" in outcome
    assert "media_artifact_id" in outcome
    assert "do not re-offer the same call unchanged" in outcome.lower()
    # A failed offer is spent too: it needs a DIFFERENT approach, not the same button again.
    assert "act_publish_v3" not in _rendered(service, engine, thread_id)


def test_an_older_console_that_sends_no_action_id_still_stops_replaying(engine):
    """Falling back to the tool name is the safe direction to be wrong in."""
    service, thread_id = _thread_with_offers(engine)
    _press(service, engine, thread_id, action_id="")
    assert "act_publish_v3" not in _rendered(service, engine, thread_id)
