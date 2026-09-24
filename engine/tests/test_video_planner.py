"""Turning prose into an edit plan.

The model is faked here, deliberately. What is worth testing is not whether a model can write
JSON — it is what BOSS does with what comes back, and every interesting case is a bad answer:
an invented operation, a timecode past the end of the video, a fenced block, a sentence of
preamble. Those are cheap and deterministic to test, and they are where this breaks.
"""
import pytest

from boss_engine.services import video_planner as vp
from boss_engine.services.video_edit import VideoEditError


class FakeModel:
    """A model that returns whatever it was told to, and records what it was asked."""

    def __init__(self, *answers: str):
        self.answers = list(answers)
        self.asked: list[tuple[str, str]] = []

    def create_text_response(self, user_prompt, system_prompt=""):
        self.asked.append((system_prompt, user_prompt))
        answer = self.answers.pop(0) if self.answers else "{}"
        return "ref", answer, {"model": "fake"}


GOOD = """{"plan": [{"op": "trim", "start": "5", "end": "20", "why": "make the short"},
                    {"op": "reframe", "aspect": "9:16", "why": "vertical"}],
           "assumptions": ["took 'the start' to mean the first five seconds"],
           "refused": []}"""


def _plan(model, **kw):
    return vp.plan_from_instructions(
        model, instructions=kw.pop("instructions", "make a short from 5 to 20, vertical"),
        house_style=kw.pop("house_style", ""), duration_seconds=kw.pop("duration", 60.0),
        width=kw.pop("width", 1920), height=kw.pop("height", 1080), **kw)


# --- what the model is told --------------------------------------------------------------

def test_the_model_is_told_the_video_s_duration():
    """A model asked to "cut the bit near the end" without the duration invents a timecode.

    An invented timecode is a VALID plan that edits the wrong part — the one failure mode
    validation cannot catch, because there is nothing malformed about it.
    """
    model = FakeModel(GOOD)
    _plan(model, duration=743.5, width=1280, height=720)
    system, _user = model.asked[0]
    assert "743.50 seconds" in system
    assert "1280x720" in system


def test_the_model_is_told_the_ordering_trap():
    """Each operation applies to the result of the last, so a caption after a cut shifts.

    If the prompt does not say so, the model produces plans that are correct as written and
    wrong as intended — and the person who wrote the instruction reads their timecodes off the
    original video every time.
    """
    model = FakeModel(GOOD)
    _plan(model)
    system, _ = model.asked[0]
    assert "applies to the RESULT of the one before" in system
    assert "BEFORE any cut or trim" in system


def test_house_style_is_a_default_and_this_video_s_instructions_override_it():
    model = FakeModel(GOOD)
    _plan(model, house_style="Always use 1:1.", instructions="make it 9:16")
    _system, user = model.asked[0]
    assert "Always use 1:1." in user
    assert "override" in user.lower(), "the prompt must say which one wins"
    assert user.index("STANDING HOUSE RULES") < user.index("INSTRUCTIONS FOR THIS VIDEO")


def test_the_model_may_only_name_images_it_was_given():
    """A plan naming an artifact BOSS was never told about is a plan naming an arbitrary file."""
    model = FakeModel(GOOD)
    _plan(model, assets={"logo": "art_logo_1"})
    system, _ = model.asked[0]
    assert "art_logo_1" in system

    bare = FakeModel(GOOD)
    _plan(bare)
    system, _ = bare.asked[0]
    assert "do not use overlay_image or watermark" in system


# --- what comes back ----------------------------------------------------------------------

def test_a_good_answer_becomes_a_validated_plan():
    result = _plan(FakeModel(GOOD))
    assert [s["op"] for s in result["plan"]] == ["trim", "reframe"]
    assert result["assumptions"] == ["took 'the start' to mean the first five seconds"]
    assert result["attempts"] == 1


@pytest.mark.parametrize("wrapped", [
    "```json\n" + GOOD + "\n```",
    "```\n" + GOOD + "\n```",
    "Here is the plan you asked for:\n" + GOOD,
    GOOD + "\n\nLet me know if you want changes!",
])
def test_the_json_is_found_however_the_model_wrapped_it(wrapped):
    """Models are told to emit bare JSON and sometimes do not.

    Failing on a code fence would make this step's reliability a function of the model's mood.
    What is NOT relaxed is the content — whatever is extracted still gets validated.
    """
    assert len(_plan(FakeModel(wrapped))["plan"]) == 2


def test_an_invented_operation_is_rejected_and_the_model_is_told_why():
    """The retry carries the REASON. Asking again with the same prompt spends money on the same
    mistake; a model told exactly what was rejected usually fixes it."""
    model = FakeModel('{"plan":[{"op":"speed_up","factor":2}]}', GOOD)
    result = _plan(model)
    assert result["attempts"] == 2
    _system, second = model.asked[1]
    assert "rejected" in second and "speed_up" in second


def test_two_bad_answers_is_a_real_failure_and_the_error_says_what_was_wrong():
    model = FakeModel('{"plan":[{"op":"speed_up"}]}', '{"plan":[{"op":"nonsense"}]}')
    with pytest.raises(vp.PlanningError, match="nonsense"):
        _plan(model)


def test_a_timecode_that_is_not_a_timecode_never_reaches_the_editor():
    """"90s" is the mistake a model actually makes, and ffmpeg would take it as 90 seconds
    or refuse depending on position — so it is caught here, not discovered mid-render."""
    model = FakeModel('{"plan":[{"op":"trim","start":"90s","end":"120s"}]}',
                      '{"plan":[{"op":"trim","start":"90","end":"120"}]}')
    assert _plan(model)["plan"][0]["start"] == "90"


def test_an_answer_that_is_not_a_plan_at_all_fails_with_what_the_model_said():
    """"I'd be happy to help!" is not a planning failure anybody can debug without the text."""
    model = FakeModel("I'd be happy to help with that!", "Still no JSON here.")
    with pytest.raises(vp.PlanningError, match="happy to help|did not return a plan"):
        _plan(model)


def test_no_instructions_is_refused_before_a_model_is_paid_to_read_nothing():
    model = FakeModel(GOOD)
    with pytest.raises(vp.PlanningError, match="nothing to plan"):
        _plan(model, instructions="   ")
    assert model.asked == [], "the model must not be called at all"


def test_an_empty_plan_is_a_valid_answer():
    """"Have a look at this" asks for no edits. Inventing some would be worse than none."""
    result = _plan(FakeModel('{"plan":[],"assumptions":["nothing was actually asked for"]}'))
    assert result["plan"] == []
    assert "nothing to do" in vp.summarise(result)


# --- what the reviewer sees ------------------------------------------------------------------

def test_the_summary_shows_the_guesses_and_the_refusals_not_just_the_plan():
    """The two things most likely to make the answer wrong are what it guessed and what it could
    not do. A reviewer deciding whether to publish will not read JSON carefully."""
    result = _plan(FakeModel("""{"plan":[{"op":"reframe","aspect":"9:16","why":"for a short"}],
        "assumptions":["assumed the whole video"],
        "refused":["speed up the middle section — BOSS has no speed operation"]}"""))
    text = vp.summarise(result)
    assert "reframe" in text and "for a short" in text
    assert "Assumed:" in text and "assumed the whole video" in text
    assert "Could NOT do:" in text and "speed up" in text


def test_the_reviewer_is_warned_when_the_timecodes_will_not_mean_what_they_look_like():
    """Cut first, caption at 5:00, and 5:00 is no longer where the author looked.

    Raised at PLANNING time because the reviewer is the only person who can say whether those
    timecodes were read off the original — by render time nobody is looking.
    """
    result = _plan(FakeModel("""{"plan":[
        {"op":"cut","remove":[{"start":"60","end":"120"}],"why":"dead air"},
        {"op":"overlay_image","image_artifact_id":"art_d","start":"300","end":"304",
         "why":"the diagram"}]}"""), assets={"diagram": "art_d"})
    assert result["timeline_note"] and "SHORTENED" in result["timeline_note"]
    assert "Note:" in vp.summarise(result)


def test_the_house_style_falls_back_rather_than_failing_the_step():
    """An edit plan with no house style is a worse plan, not a broken one.

    A missing knowledge row must not stop somebody publishing a video.
    """
    class Boom:
        def execute(self, *a, **k):
            raise RuntimeError("no such table")

    assert vp.house_style_for(Boom(), "p") == vp.DEFAULT_HOUSE_STYLE
    assert "HOW THIS CHANNEL EDITS" in vp.DEFAULT_HOUSE_STYLE
