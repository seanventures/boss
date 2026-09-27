"""An agent read Tamil correctly and then destroyed it typing it back.

The brief was read perfectly — `media.read_document` returned every character intact. The agent
then wrote the title into its reply, and what arrived was a different sentence:

    மதுராந்தகத்தில் இடைத்தேர்தலே தேவையில்லை!      what the brief says
    மதுராந்தகத்தில்இட்டைத்தெற்றலே தவெய்ல்ளை!      what the agent typed back

The description came back shortened, with "(full description as extracted from the document)"
standing in for the rest of it. Both would have been published as the operator's own words.

NEITHER IS A BUG THAT CAN BE FIXED WITH A PROMPT. The model is not making a mistake it can be
told about; it is re-generating characters, and non-Latin scripts do not survive that. So a value
the agent COPIED is transferred by reference — it says which part of the document it means and
the original bytes are lifted out — and a value the agent WROTE comes through as written, because
that text has no original to be faithful to.
"""
import pytest

from boss_engine.services.agent_steps import _copied_exactly, _declared, _declared_values

BRIEF = """prompt

Name: (as caption) இரா. ஞானகுரு, அரசியல் விமர்சகர் (display for 15s)

I need a shoft video from this lengthy video - from - start: 1:11- end:2:36

**Title:**
மதுராந்தகத்தில் இடைத்தேர்தலே தேவையில்லை! அதிமுகவிலிருந்து தவெகவுக்கு தாவியது மக்கள் நலனுக்கா? அல்லது சுயலாபத்திற்காகவா?


**Description:**

மதுராந்தகத்தில் இடைத்தேர்தல் வரவேண்டிய அவசியமே இல்லை!

அதிமுகவைச் சேர்ந்த இரு வேட்பாளர்களும் தவெகவுக்கு சென்றது மக்கள் நலனுக்காகவா? அல்லது தனிப்பட்ட அரசியல் அதிகாரம், பதவி மற்றும் செல்வாக்குக்காகவா?

மக்கள் அளித்த வாக்கையும் நம்பிக்கையையும் மறந்து கட்சி மாறுவது சரியான அரசியல் அணுகுமுறையா? **மக்கள் சிந்தனை** பார்வையில் ஒரு விமர்சனம்.

**Hashtags:**
#மதுராந்தகம் #இடைத்தேர்தல் #Madurantakam
"""

REAL_TITLE = ("மதுராந்தகத்தில் இடைத்தேர்தலே தேவையில்லை! அதிமுகவிலிருந்து தவெகவுக்கு தாவியது "
              "மக்கள் நலனுக்கா? அல்லது சுயலாபத்திற்காகவா?")
#: Verbatim from the run that prompted this. Not a simulation of corruption — the corruption.
AS_THE_AGENT_TYPED_IT = ("மதுராந்தகத்தில்இட்டைத்தெற்றலே தவெய்ல்ளை! அதிமுகவிலிருந்து "
                         "தவெகவுக்கு தவையது மக்கள நலனுக்கா?")


# --------------------------------------------------------------------------- #
# The original, out of the document
# --------------------------------------------------------------------------- #
def test_the_corrupted_title_resolves_to_the_real_one():
    assert _copied_exactly(AS_THE_AGENT_TYPED_IT, [BRIEF]) == REAL_TITLE


def test_a_value_that_spans_paragraphs_comes_through_whole():
    """A description of three paragraphs resolved to its first one when the agent only said
    where it began — which is exactly what it pointed at, and a third of what it meant."""
    got = _copied_exactly(
        "மதுராந்தகத்தில்இட்டைத்தெற்றலே வரவேண்டி அவசியமே illi! ... (full description)",
        [BRIEF],
        ends_with="மக்கள சிந்தனை பார்வையில் ஒரு விமர்சனம்.")

    assert got.startswith("மதுராந்தகத்தில் இடைத்தேர்தல் வரவேண்டிய அவசியமே இல்லை!")
    assert got.endswith("பார்வையில் ஒரு விமர்சனம்.")
    assert "அதிமுகவைச் சேர்ந்த" in got, "the middle paragraph has to be in it"
    assert "#மதுராந்தகம்" not in got, "it must stop where the agent said it stops"
    assert "தேவையில்லை" not in got, "and must not reach back into the title"


def test_nothing_like_it_leaves_the_agent_s_own_words_alone():
    """Better a value that is the agent's than a confident substitution of the wrong paragraph."""
    assert _copied_exactly("A title about gardening in Devon", [BRIEF]) is None
    assert _copied_exactly("anything", []) is None
    assert _copied_exactly("", [BRIEF]) is None


def test_an_exact_quote_is_returned_unchanged():
    assert _copied_exactly(REAL_TITLE, [BRIEF]) == REAL_TITLE


def test_the_ending_quote_is_ignored_when_it_matches_nothing():
    """A `to` that cannot be found must not throw the whole value away."""
    got = _copied_exactly(AS_THE_AGENT_TYPED_IT, [BRIEF], ends_with="about gardening in Devon")
    assert got == REAL_TITLE


# --------------------------------------------------------------------------- #
# Saying which values were copied
# --------------------------------------------------------------------------- #
def test_a_copied_value_is_marked_and_a_written_one_is_not():
    values, copied = _declared(
        '{"title": {"copied": true, "value": "மதுராந்தகத்தில்", "to": "சுயலாபத்திற்காகவா?"},'
        ' "description": "A summary I wrote myself."}',
        ["title", "description"])

    assert values["description"] == "A summary I wrote myself."
    assert "description" not in copied, "the agent wrote this one; it stands as written"
    assert copied["title"] == ("", "சுயலாபத்திற்காகவா?")


def test_a_single_line_value_needs_no_ending_quote():
    values, copied = _declared('{"title": {"copied": true, "value": "மதுராந்தகத்தில்"}}',
                               ["title"])
    assert copied == {"title": ("", "")}
    assert values["title"] == "மதுராந்தகத்தில்"


def test_a_plain_string_still_works():
    """Every workflow that already declares values must keep working unchanged."""
    values, copied = _declared('{"title": "A short", "description": "About it"}',
                               ["title", "description"])
    assert values == {"title": "A short", "description": "About it"}
    assert copied == {}


def test_an_empty_copied_value_is_not_an_answer():
    values, copied = _declared('{"title": {"copied": true, "value": ""}}', ["title"])
    assert values == {} and copied == {}


def test_the_old_reader_still_returns_flat_values():
    """`_declared_values` is used elsewhere and must not start returning wrappers."""
    assert _declared_values('{"title": {"copied": true, "value": "x"}}', ["title"]) == {
        "title": "x"}


# --------------------------------------------------------------------------- #
# The agent is told why, not just what
# --------------------------------------------------------------------------- #
def test_the_instruction_says_retyping_damages_the_text():
    """An agent told only "use this format" will use it when convenient. It is told what goes
    wrong, because that is the reason to bother."""
    import inspect

    from boss_engine.services import agent_steps

    source = inspect.getsource(agent_steps.run)
    assert "DO NOT RETYPE IT" in source
    assert "non-Latin" in source
    assert '\\"copied\\": true' in source or '"copied": true' in source


def test_the_resolution_runs_before_the_value_is_passed_on():
    import inspect

    from boss_engine.services import agent_steps

    source = inspect.getsource(agent_steps.run)
    resolve = source.index("_copied_exactly(")
    hand_on = source.index("produced.update(declared)")
    assert resolve < hand_on, "a copied value must be resolved before the next step reads it"


# --------------------------------------------------------------------------- #
# Pointing with digits, because a quote is retyped too
# --------------------------------------------------------------------------- #
# Pointing at the value fixed the corruption — the Tamil that came back was clean. It was also
# the WRONG LINE, because the pointer itself had to be retyped: the agent quoted
# "மதுராந்தகத்தில்இட்டற்றலெ...?", its own damaged copy of the title, and the closest match to that
# was the first line of the description. A line number is ASCII and cannot be damaged.
def test_a_line_number_resolves_to_that_exact_line():
    from boss_engine.services.agent_steps import _lines_of

    lines = BRIEF.split("\n")
    where = lines.index(REAL_TITLE) + 1
    assert _lines_of([BRIEF], str(where)) == REAL_TITLE


def test_a_range_comes_through_whole():
    from boss_engine.services.agent_steps import _lines_of

    got = _lines_of(["one", "a\nb\nc\nd\ne"][1:], "2-4")
    assert got == "b\nc\nd"


def test_a_dash_the_model_typed_as_an_em_dash_still_works():
    """Refusing a range over the width of a hyphen would be absurd."""
    from boss_engine.services.agent_steps import _lines_of

    document = "a\nb\nc\nd"
    assert _lines_of([document], "2–3") == "b\nc"
    assert _lines_of([document], " 2 - 3 ") == "b\nc"


def test_a_range_past_the_end_resolves_to_nothing():
    """Rather than silently clamping to the last line and passing on the wrong value."""
    from boss_engine.services.agent_steps import _lines_of

    assert _lines_of(["a\nb"], "5-9") is None
    assert _lines_of(["a\nb"], "0") is None
    assert _lines_of(["a\nb"], "3-1") is None
    assert _lines_of(["a\nb"], "") is None
    assert _lines_of([], "1") is None


def test_the_last_document_read_is_the_one_meant():
    """An agent that read a brief and then something else is pointing at what it just looked at."""
    from boss_engine.services.agent_steps import _lines_of

    assert _lines_of(["first\nfirst2", "second\nsecond2"], "1") == "second"


def test_a_pointer_needs_no_sample_of_the_text():
    """Insisting on a quote alongside the numbers would be asking for the one thing that cannot
    be copied safely."""
    values, copied = _declared('{"title": {"copied": true, "lines": "8"}}', ["title"])
    assert copied["title"][0] == "8"
    assert "title" in values


def test_the_numbered_view_is_what_the_agent_points_at():
    from boss_engine.services.drive_media import _with_line_numbers

    numbered = _with_line_numbers("தலைப்பு\n\nவிளக்கம்")
    assert numbered.split("\n")[0] == "1: தலைப்பு"
    assert numbered.split("\n")[2] == "3: விளக்கம்"


def test_a_very_long_document_is_not_numbered_twice_over():
    """The numbered copy doubles the payload, and a document this long is not one an agent is
    picking a title out of."""
    from boss_engine.services.drive_media import MAX_NUMBERED_CHARACTERS, _with_line_numbers

    assert _with_line_numbers("x" * (MAX_NUMBERED_CHARACTERS + 1)) == ""


def test_numbers_are_preferred_over_a_quote():
    import inspect

    from boss_engine.services import agent_steps

    source = inspect.getsource(agent_steps.run)
    by_number = source.index("_lines_of(")
    by_quote = source.index("_copied_exactly(")
    assert by_number < by_quote, "the pointer that cannot be damaged has to be tried first"


def test_the_agent_is_told_to_leave_the_heading_out_and_take_all_of_it():
    """Pointing at lines 12-13 gave a title of "**Title:**\\nமதுராந்தகத்தில்…" — the label
    published as part of the value — and a description that stopped after its first paragraph."""
    import inspect

    from boss_engine.services import agent_steps

    source = inspect.getsource(agent_steps.run)
    assert "Leave out the heading" in source
    assert "take ALL of it" in source
    assert "check both ends" in source
