"""An agent could fetch a video and an image and could not read a document.

So an operator who wrote the title and description for their video in a Drive doc — and linked
it — was asked to retype both into a form. "I did not say it is for Public and all the title,
description are in the prompt file."

TWO THINGS HAD TO EXIST FOR THAT TO WORK, and the second is the less obvious one:

  * a skill that turns a document link into text, which is `media.read_document`;
  * a way for an agent step to hand a VALUE to the next step. Its output is otherwise whatever
    its skills returned, and a title is not a skill's output — it is a judgement the agent makes
    after reading the brief.
"""
import pytest

from boss_engine.services.agent_steps import _declared_values


# --------------------------------------------------------------------------- #
# Reading what the agent decided
# --------------------------------------------------------------------------- #
def test_a_declared_value_is_read_out_of_the_reply():
    out = _declared_values('Done.\n\n{"title": "A short", "description": "About it"}',
                           ["title", "description"])
    assert out == {"title": "A short", "description": "About it"}


def test_tamil_survives_the_round_trip():
    """THE POINT OF THE ENCODING WORK. A title in Tamil has to arrive as a title in Tamil, not
    as mojibake that still reads as a successful run."""
    reply = ('```json\n{"title": "மக்கள் சிந்தனை — ஒரு பார்வை", '
             '"description": "மதுராந்தகம் பற்றிய விளக்கம்"}\n```')
    out = _declared_values(reply, ["title", "description"])
    assert out["title"] == "மக்கள் சிந்தனை — ஒரு பார்வை"
    assert out["description"] == "மதுராந்தகம் பற்றிய விளக்கம்"


def test_a_fenced_block_after_prose_is_still_read():
    """Lenient on purpose: failing a step over punctuation would be absurd."""
    reply = "I trimmed and reframed it.\n\n```json\n{\"title\": \"Kept\"}\n```\nAnything else?"
    assert _declared_values(reply, ["title"]) == {"title": "Kept"}


def test_only_the_keys_that_were_asked_for_come_back():
    """Not a channel for the agent to set fields nobody asked about."""
    out = _declared_values('{"title": "A", "visibility": "public", "cost": 0}', ["title"])
    assert out == {"title": "A"}


def test_nothing_stated_is_nothing_returned():
    assert _declared_values("I could not work out a title.", ["title"]) == {}
    assert _declared_values("", ["title"]) == {}
    assert _declared_values('{"title": "A"}', []) == {}


def test_an_empty_value_is_not_passed_on_as_an_answer():
    """The agent is told to use an empty string where a value is genuinely unavailable; an
    empty title must not reach YouTube as if it were one."""
    assert _declared_values('{"title": "", "description": "real"}',
                            ["title", "description"]) == {"description": "real"}


# --------------------------------------------------------------------------- #
# The step asks for them, and a skill's own `title` does not masquerade as one
# --------------------------------------------------------------------------- #
def test_a_declared_value_beats_a_skill_field_of_the_same_name():
    """`media.read_document` returns `name`, and other skills return a `title` meaning the name
    of a Drive file. Publishing a video called "short.mp4" is what conflating them looks like."""
    import inspect

    from boss_engine.services import agent_steps

    source = inspect.getsource(agent_steps.run)
    merge = source.index("_what_the_skills_returned")
    declared = source.index("_declared_values(")
    assert merge < declared, "the declared values must be applied last, or the skill field wins"


def test_the_agent_is_told_to_read_rather_than_invent():
    import inspect

    from boss_engine.services import agent_steps

    source = inspect.getsource(agent_steps.run)
    assert "read it and take the" in source
    assert "do not guess" in source


# --------------------------------------------------------------------------- #
# The reader itself
# --------------------------------------------------------------------------- #
def test_a_google_doc_is_exported_rather_than_downloaded():
    """A Google-native file has no bytes to download; export is the only way to read one."""
    import inspect

    from boss_engine.services import drive_media

    source = inspect.getsource(drive_media.read_document)
    assert "/export" in source
    assert 'GOOGLE_NATIVE_EXPORT' in source
    assert drive_media.GOOGLE_NATIVE_EXPORT[
        "application/vnd.google-apps.document"] == "text/plain"


def test_the_text_is_decoded_as_utf8_deliberately():
    """Letting the HTTP client guess the encoding is how Tamil becomes mojibake that still
    looks like success."""
    import inspect

    from boss_engine.services import drive_media

    source = inspect.getsource(drive_media.read_document)
    assert 'response.content.decode("utf-8", errors="replace")' in source
    # `response.text[:200]` in an ERROR message is fine — that is an HTTP failure body, not the
    # document. What must never happen is the DOCUMENT being read that way, because httpx would
    # guess the encoding from the headers.
    assert "= response.text" not in source
    assert "text_value = response.content.decode" in source


def test_a_pdf_is_refused_rather_than_half_read():
    """Extracting text from a PDF is a different problem; returning something that looks like
    prose and is not would be worse than refusing."""
    import inspect

    from boss_engine.services import drive_media

    source = inspect.getsource(drive_media.read_document)
    assert "needs a" in source and "different skill" in source
    assert "application/pdf" not in drive_media.READABLE_TEXT_PREFIXES
