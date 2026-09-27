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


# --------------------------------------------------------------------------- #
# A PDF, which is what people actually link
# --------------------------------------------------------------------------- #
# The first real brief that reached this skill was a PDF and was refused: "The brief document
# containing the Tamil title and description could not be read because it is not a supported
# text format (it is a PDF)." A PDF is not text — it is instructions for painting a page — and
# reading one has a trap in it that no program can detect afterwards.
#
# THE STRING BELOW IS NOT INVENTED. It is what pypdf returned from a PDF of the two Tamil lines
# in `TYPED`, printed by a browser exactly as a Google Doc export would be. Every glyph is
# correct and the WORD IS WRONG, because Tamil paints ெ ே ை to the left of the consonant they
# belong to while Unicode stores them after it.
AS_PAINTED = "Title: மக்கள்  சிந்தைன  — ஒரு  பார்ைவ"
AS_TYPED = "Title: மக்கள் சிந்தனை — ஒரு பார்வை"


def test_the_glyph_order_of_a_pdf_is_put_back_before_anybody_reads_it():
    """சிந்தைன is not a word. சிந்தனை is, and it is the one that was typed."""
    from boss_engine.services.drive_media import (
        _tidied, in_the_order_a_reader_wrote_it,
    )

    assert in_the_order_a_reader_wrote_it(_tidied(AS_PAINTED)) == AS_TYPED


def test_a_split_vowel_recomposes():
    """வீடிேயா is ய with ே painted in front of it; moving the sign back leaves ய + ே + ா, which
    is ோ once, not two marks. Without the NFC pass the string is still not the one typed."""
    from boss_engine.services.drive_media import in_the_order_a_reader_wrote_it

    assert in_the_order_a_reader_wrote_it("வீடிேயா") == "வீடியோ"


def test_a_consonant_cluster_keeps_the_sign_at_the_end_of_it():
    """A pre-base sign belongs after the WHOLE cluster, not after its first letter."""
    from boss_engine.services.drive_media import in_the_order_a_reader_wrote_it

    assert in_the_order_a_reader_wrote_it("ெக்ர") == "க்ரெ"


def test_text_without_these_signs_is_returned_untouched():
    """The repair must not be something every other document pays for."""
    from boss_engine.services.drive_media import in_the_order_a_reader_wrote_it

    for plain in ("Keep 3:42 to 4:50.", "", "ハロー", "Grüße", "مرحبا"):
        assert in_the_order_a_reader_wrote_it(plain) == plain


def test_the_spacing_of_a_painted_page_is_tidied():
    """Glyphs are positioned, not typed, so a word break arrives as two spaces and a full stop
    arrives detached. Left alone, the string is not the one anybody wrote."""
    from boss_engine.services.drive_media import _tidied

    assert _tidied("ஒரு  பகுதி .") == "ஒரு பகுதி."
    assert _tidied("a   b\nc  d  ") == "a b\nc d"


def test_a_pdf_of_pictures_says_so_instead_of_returning_nothing():
    """A scan has NO text to lift. Returning "" would hand the agent a document it believes it
    read and found blank — which is how an empty title reaches YouTube."""
    import io

    import pytest as _pytest
    from reportlab.pdfgen import canvas

    from boss_engine.services.drive_media import DriveMediaError, _text_out_of_a_pdf

    buffer = io.BytesIO()
    page = canvas.Canvas(buffer)
    page.rect(50, 50, 200, 200, fill=1)
    page.showPage()
    page.save()

    with _pytest.raises(DriveMediaError) as raised:
        _text_out_of_a_pdf(buffer.getvalue(), "scan.pdf")
    assert "no text in any of them" in str(raised.value)
    assert "Google Doc" in str(raised.value), "it has to say what to do instead"


def test_a_damaged_pdf_is_a_message_not_a_traceback():
    from boss_engine.services.drive_media import DriveMediaError, _text_out_of_a_pdf

    with pytest.raises(DriveMediaError) as raised:
        _text_out_of_a_pdf(b"this is not a PDF at all", "brief.pdf")
    assert "brief.pdf" in str(raised.value)


# --------------------------------------------------------------------------- #
# Drive is asked to convert first, because Google knows what the glyphs MEAN
# --------------------------------------------------------------------------- #
class _FakeDrive:
    """Drive, enough of it: copy converts, export returns text, delete records the tidy-up."""

    def __init__(self, *, copy_status: int = 200, exported: str = "ஒரு தலைப்பு"):
        self.copy_status = copy_status
        self.exported = exported
        self.deleted: list[str] = []
        self.copy_params: dict[str, str] = {}

    class _Reply:
        def __init__(self, status: int, payload=None, content: bytes = b""):
            self.status_code = status
            self._payload = payload or {}
            self.content = content
            self.text = content.decode("utf-8", errors="replace")

        def json(self):
            return self._payload

    def post(self, url, params=None, json=None, headers=None, timeout=None):
        self.copy_params = dict(params or {})
        if self.copy_status != 200:
            return self._Reply(self.copy_status, content=b"nope")
        assert json["mimeType"] == "application/vnd.google-apps.document", json
        return self._Reply(200, {"id": "copy_1"})

    def get(self, url, params=None, headers=None, timeout=None):
        return self._Reply(200, content=self.exported.encode("utf-8"))

    def delete(self, url, headers=None, timeout=None):
        self.deleted.append(url)
        return self._Reply(204)


def test_drive_converts_the_pdf_and_the_copy_is_deleted_again():
    """Converting is better than extracting — a Doc holds real text, in logical order, and OCR
    handles a scan on the way through. The copy is a workspace and must not survive the call."""
    from boss_engine.services.drive_media import _text_by_asking_drive_to_convert

    drive = _FakeDrive(exported="மக்கள் சிந்தனை — ஒரு பார்வை")
    got = _text_by_asking_drive_to_convert(
        drive, {"Authorization": "Bearer t"}, file_id="f1", name="brief.pdf", language="ta")

    assert got == "மக்கள் சிந்தனை — ஒரு பார்வை"
    assert drive.copy_params.get("ocrLanguage") == "ta", "OCR in the wrong language is nonsense"
    assert drive.deleted and drive.deleted[0].endswith("/copy_1"), (
        "the converted copy was left in the operator's Drive")


def test_a_refused_conversion_falls_back_to_extraction():
    """An account that cannot write to Drive must still be able to READ a PDF."""
    import io

    from boss_engine.services import drive_media

    class _NoWriteDrive(_FakeDrive):
        def get(self, url, params=None, headers=None, timeout=None):
            if url.endswith("/files/f1"):
                if (params or {}).get("alt") == "media":
                    return self._Reply(200, content=_a_pdf_saying("Hello from the page"))
                return self._Reply(200, {"id": "f1", "name": "brief.pdf",
                                         "mimeType": "application/pdf", "size": "2000"})
            raise AssertionError(f"unexpected GET {url}")

    def _a_pdf_saying(words: str) -> bytes:
        from reportlab.pdfgen import canvas
        buffer = io.BytesIO()
        page = canvas.Canvas(buffer)
        page.drawString(70, 700, words)
        page.showPage()
        page.save()
        return buffer.getvalue()

    drive = _NoWriteDrive(copy_status=403)
    result = drive_media.read_document.__wrapped__ if hasattr(
        drive_media.read_document, "__wrapped__") else drive_media.read_document

    import unittest.mock as mock
    with mock.patch.object(drive_media, "_http", return_value=drive), \
            mock.patch.object(drive_media, "_token", return_value="tok"):
        out = result(None, None, project_id="p", file_id="f1")

    assert out["read_via"] == "extracted_and_reordered"
    assert "Hello from the page" in out["text"]
    assert not drive.deleted, "nothing was created, so nothing should be deleted"


def test_the_skill_still_refuses_an_image():
    """Widening to PDFs is not widening to everything — a picture needs media.fetch_image."""
    import inspect

    from boss_engine.services import drive_media

    source = inspect.getsource(drive_media.read_document)
    assert "different skill" in source
    assert "application/pdf" not in drive_media.READABLE_TEXT_PREFIXES, (
        "a PDF is handled on its own branch, not by pretending it is text")
