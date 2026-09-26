"""Reading a transcript into decisions and actions.

The model is faked. What matters is not whether a model can write JSON — it is what BOSS does
with a bad answer, and the worst answer here is a plausible one: an invented owner, an invented
deadline, or two action items conjured from a conversation that produced none.
"""
import pytest

from boss_engine.services import meeting_actions as ma


class FakeModel:
    def __init__(self, *answers):
        self.answers = list(answers)
        self.asked = []

    def create_text_response(self, user_prompt, system_prompt=""):
        self.asked.append((system_prompt, user_prompt))
        return "ref", (self.answers.pop(0) if self.answers else "{}"), {"model": "fake"}


# One line per string value: a literal newline inside a JSON string is not valid JSON, and a
# fixture that cannot parse tests the parser's error path rather than the thing intended.
GOOD = """{"decisions": [{"what": "Ship the pricing page on Friday",
                          "why": "the campaign starts Monday"}],
           "actions": [{"title": "Draft three pricing options",
                        "detail": "Ravi said he would write three tiers before Wednesday.",
                        "owner": "Ravi", "due": "before Wednesday"},
                       {"title": "Update the campaign copy",
                        "detail": "Nobody volunteered.",
                        "owner": null, "due": null}],
           "unclear": ["a discussion about the logo that reached no conclusion"]}"""


def _extract(model, **kw):
    kw.setdefault("transcript", "Ravi: I'll do the pricing. Sam: ship Friday.")
    return ma.extract(model, **kw)


def test_a_transcript_becomes_decisions_and_actions():
    result = _extract(FakeModel(GOOD))
    assert [d["what"] for d in result["decisions"]] == ["Ship the pricing page on Friday"]
    assert len(result["actions"]) == 2
    assert result["action_count"] if "action_count" in result else True


def test_an_owner_nobody_named_stays_empty_rather_than_guessed():
    """A ticket assigned to the wrong person is worse than one assigned to nobody.

    Every tracker has an unassigned state; none has an "assigned to whoever the model thought"
    state, and the person it lands on will not know why.
    """
    result = _extract(FakeModel(GOOD))
    assert result["actions"][0]["owner"] == "Ravi"
    assert result["actions"][1]["owner"] is None

    # And the instruction that produces that is in the prompt, not left to the model's taste.
    prompt, _user = ma.build_prompt(transcript="x")
    assert "Do not invent an owner" in prompt
    assert "Do not invent a due date" in prompt


def test_a_meeting_that_decided_nothing_produces_nothing():
    """The failure this guards is a model inventing work to look useful.

    An empty result is a real outcome — a stand-up where nothing was agreed — and treating it
    as an error would make "nothing to do" look like a broken workflow.
    """
    result = _extract(FakeModel('{"decisions": [], "actions": [], "unclear": []}'))
    assert result["actions"] == []
    assert "No action items" in result["summary"]


def test_an_action_with_no_title_is_refused_because_it_cannot_be_a_ticket():
    model = FakeModel('{"actions": [{"detail": "something", "owner": "Sam"}]}', GOOD)
    result = _extract(model)
    assert result["attempts"] == 2
    _system, second = model.asked[1]
    assert "rejected" in second and "title" in second


def test_a_transcript_is_required_before_a_model_is_paid_to_read_nothing():
    model = FakeModel(GOOD)
    with pytest.raises(ma.ExtractionError, match="nothing to read"):
        _extract(model, transcript="   ")
    assert model.asked == []


@pytest.mark.parametrize("wrapped", [
    "```json\n" + GOOD + "\n```",
    "Here is what I found:\n" + GOOD,
    GOOD + "\n\nHope that helps!",
])
def test_the_json_is_found_however_it_was_wrapped(wrapped):
    assert len(_extract(FakeModel(wrapped))["actions"]) == 2


def test_the_summary_is_what_a_person_approves_on():
    """They are deciding whether these become real tickets other people act on."""
    text = _extract(FakeModel(GOOD))["summary"]
    assert "Decided:" in text and "Ship the pricing page" in text
    assert "Tickets to create (2)" in text
    assert "[Ravi, before Wednesday]" in text
    assert "[unassigned]" in text          # the one nobody owned, said plainly
    assert "Could not place:" in text      # what it heard and could not resolve


def test_the_ticket_body_carries_what_was_said():
    """The person reading the ticket was not in the meeting."""
    from boss_engine.services.skill_executors import _issue_body

    body = _issue_body({"detail": "Ravi will write three tiers.",
                        "owner": "Ravi", "due": "before Wednesday"})
    assert "three tiers" in body
    assert "Ravi" in body and "before Wednesday" in body
    assert "BOSS" in body, "a ticket nobody can trace back to a meeting is a mystery"
