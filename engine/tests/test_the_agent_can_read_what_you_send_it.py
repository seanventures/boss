"""Screenshots and text files reach BOSS.

"I want BOSS agent ability to read images when we upload the image or screenshot of the errors"
and "support uploading the text files too, so BOSS can understand if there's lots of information
to read."

Describing a broken screen to something that cannot see it is the slowest possible way to report
a UI fault, and pasting six thousand lines of log into the chat box ruins the transcript for
every turn after it.
"""
import pytest

from boss_engine.schemas import AgentAttachment, AgentChatRequest
from boss_engine.services.intent_router import _ATTACHED_TEXT_CHARS, _with_attachments


def _request(**kw):
    base = {"project_id": "p", "actor": "ceo", "message": "hi"}
    return AgentChatRequest(**{**base, **kw})


def test_an_image_becomes_content_blocks_the_model_can_see():
    body, blocks = _with_attachments(
        "why does this look wrong?",
        [{"name": "err.png", "kind": "image", "media_type": "image/png", "data": "QUJD"}])
    assert blocks is not None
    assert [b["type"] for b in blocks] == ["text", "image_url"]
    assert blocks[1]["image_url"]["url"].startswith("data:image/png;base64,")
    # And the words still say a file is there, for a reader of the transcript.
    assert "[attached image: err.png]" in body


def test_text_stays_a_plain_string_so_nothing_downstream_changes():
    body, blocks = _with_attachments(
        "why?", [{"name": "app.log", "kind": "text", "data": "boom"}])
    assert blocks is None
    assert isinstance(body, str)


def test_a_text_file_is_delimited_and_named():
    """Pasted flat, a file is indistinguishable from the question — and a log full of imperative
    sentences reads as instructions."""
    body, _ = _with_attachments("why?", [{"name": "app.log", "kind": "text", "data": "boom"}])
    assert "--- attached file: app.log ---" in body
    assert "--- end of app.log ---" in body


def test_a_huge_file_is_truncated_and_says_so():
    """Silently answering about the first half of the evidence is worse than being cut off."""
    body, _ = _with_attachments(
        "why?", [{"name": "huge.log", "kind": "text", "data": "x" * (_ATTACHED_TEXT_CHARS + 500)}])
    assert "truncated" in body
    assert str(_ATTACHED_TEXT_CHARS + 500) in body


def test_the_words_are_always_a_string_even_with_an_image():
    """`asked_only_to_look`, `under_investigated` and the deploy-subject match all run regexes
    over the latest message. Handing them content blocks turns a screenshot into a TypeError."""
    body, _ = _with_attachments("look", [
        {"name": "a.png", "kind": "image", "media_type": "image/png", "data": "QUJD"}])
    assert isinstance(body, str)


def test_a_screenshot_with_no_words_is_still_a_message():
    """"Look at this" is how a bug gets reported; demanding words alongside it is ceremony."""
    request = _request(message="", attachments=[
        {"name": "a.png", "media_type": "image/png", "data": "QUJD"}])
    assert request.attachments[0].kind == "image"


def test_a_type_boss_cannot_read_is_refused_by_name():
    with pytest.raises(ValueError) as caught:
        _request(attachments=[{"name": "x.zip", "media_type": "application/zip", "data": "AA"}])
    assert "x.zip" in str(caught.value)
    assert "application/zip" in str(caught.value)


def test_a_log_served_as_octet_stream_is_still_text():
    """Browsers give a .log no media type at all. Refusing it would refuse the common case."""
    request = _request(attachments=[
        {"name": "app.log", "media_type": "application/octet-stream", "data": "boom"}])
    assert request.attachments[0].kind == "text"


@pytest.mark.parametrize("bad,expected", [
    ({"count": 9}, "at most"),
    ({"size": True}, "the limit is"),
])
def test_the_bounds_are_refused_with_a_reason(bad, expected):
    files = ([{"name": f"{i}.txt", "media_type": "text/plain", "data": "x"} for i in range(9)]
             if bad.get("count") else
             [{"name": "big.txt", "media_type": "text/plain", "data": "x" * (13 * 1024 * 1024)}])
    with pytest.raises(ValueError) as caught:
        _request(attachments=files)
    assert expected in str(caught.value)
