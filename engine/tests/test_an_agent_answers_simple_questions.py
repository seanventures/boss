"""An agent may answer a question it knows the answer to.

    "how to get youtube channel client id and client secret?"
    "can you provide me the link to Google Cloud Console??"

The second is a lookup. It came back with governance advice, scope minimism, consent-screen
guidance, a note that the role had no prior recorded position to cite — and the link, in the
next turn, below four paragraphs. The operator's summary was exact: "the question is simple
give me the google cloud console link ... why this is so difficult".

ONE RULE CAUSED BOTH SYMPTOMS:

    "Ground every factual claim in a tool result you actually received this turn."

No tool returns the address of the Google Cloud Console, so the agent was forbidden from
simply saying it — and compensated with the only thing it was allowed to produce, which was
role framing. A developer then goes to a search engine for something the model already knew,
which is the opposite of what this console is for.

The rule is right about STATE — what is running, what it cost, what is connected — and wrong
about the world. Those are now separate rules.
"""

from __future__ import annotations

import inspect

from boss_engine.agent.engine_agent import _SYSTEM_PROMPT
from boss_engine.tools import builtin


def test_claims_about_this_system_still_require_a_tool():
    """The protection that matters: never guess at state."""
    assert "must come from a tool result you received this turn" in _SYSTEM_PROMPT
    assert "Never" in _SYSTEM_PROMPT and "guess at state" in _SYSTEM_PROMPT


def test_general_knowledge_does_not_require_a_tool():
    assert "GENERAL KNOWLEDGE NEEDS NO TOOL" in _SYSTEM_PROMPT


def test_it_names_the_failure_rather_than_stating_a_principle():
    """A rule with its own counter-example attached survives being paraphrased away."""
    assert "Google Cloud Console" in _SYSTEM_PROMPT
    assert "search engine" in _SYSTEM_PROMPT


def test_uncertainty_is_a_clause_not_a_refusal():
    assert "do not withhold" in _SYSTEM_PROMPT


def test_the_answer_comes_first():
    assert "ANSWER THE QUESTION THAT WAS ASKED" in _SYSTEM_PROMPT
    assert "on the first line" in _SYSTEM_PROMPT


def test_a_buried_answer_is_named_as_the_failure_it_is():
    assert "reads as evasion" in _SYSTEM_PROMPT


def test_an_empty_lookup_is_not_reported():
    """"I have no recorded position on this" was the tell — narrating a lookup that found
    nothing, on a question that never needed one."""
    assert "NEVER NARRATE AN EMPTY LOOKUP" in _SYSTEM_PROMPT


def _recall_description() -> str:
    """The description AS THE MODEL RECEIVES IT — concatenated, not the source's line breaks.

    Asserting on source text made "turns on JUDGEMENT" fail because it spans two adjacent
    string literals: a true statement about the instruction, failing on how it was typed.
    """
    src = inspect.getsource(builtin)
    tool = src[src.index('"recall_role_experience"'):]
    raw = " ".join(tool[:tool.index("_obj(")].split())
    # Adjacent string literals leave `" "` between them in the source; the model receives one
    # continuous sentence, so the test has to read it that way.
    return raw.replace('" "', "")


def test_role_recall_is_for_judgement_not_for_lookups():
    described = _recall_description()
    assert "DO NOT call it for a lookup" in described
    assert "turns on JUDGEMENT" in described
    # It carries the failure it was written for, so a paraphrase cannot quietly drop the point.
    assert "a question that wanted a URL" in described


def test_role_recall_still_earns_its_place_on_a_judgement_question():
    """Not disabled — a generic answer from a named expert is still worse than no expert."""
    assert "worse than no expert" in _recall_description()
