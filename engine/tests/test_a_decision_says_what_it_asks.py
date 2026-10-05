"""A decision's card says what it asks, in words, and where it came from — never a raw code.

On goldeneye the Command Center's one approval read "Missing active reservation for OpenAI
call" with `missing_reservation` as its detail: a producer's log line and its reason code.
decision_words maps every producer once; anything unmapped is still humanized.
"""
from __future__ import annotations

from boss_engine.services import decision_words as words


def _d(question="", action=None, reason=None, **rec):
    recommendation = {**rec, **({"action": action} if action else {}),
                      **({"reason": reason} if reason else {})}
    return {"question": question, "recommendation": recommendation}


def test_a_spend_guard_question_is_a_sentence_and_names_its_source():
    got = words.headline(_d("Missing active reservation for OpenAI call",
                            "create_budget_reservation", "missing_reservation"))
    assert got == {"title": "Allow an OpenAI call without a reservation", "source": "Spend guard"}
    assert words.headline(_d("Missing active reservation for PR proposal",
                             "create_budget_reservation"))["title"] == \
        "Allow a PR proposal without a reservation"
    assert words.headline(_d("Missing active reservation for outbound communication",
                             reason="missing_reservation"))["title"] == \
        "Allow an outbound message without a reservation"
    assert words.headline(_d("Missing active reservation for infrastructure provisioning",
                             "create_budget_reservation"))["title"] == \
        "Allow infrastructure provisioning without a reservation"


def test_known_producers_have_their_words():
    assert words.headline(_d("x", "stack_deploy_requires_approval")) == \
        {"title": "Approve a deploy", "source": "Deploys"}
    assert words.headline(_d("x", "agent_tool_requires_approval"))["source"] == "Agent tools"
    assert words.headline(_d("x", "resume_code_task")) == \
        {"title": "Approve the plan", "source": "Factory"}
    assert words.headline(_d("Over budget", reason="budget_denied")) == \
        {"title": "Budget refused work", "source": "Budget"}
    assert words.headline(_d("Out of credit", "restore_model_capacity_or_raise_budget"))[
        "source"] == "Models"


def test_an_unknown_code_is_humanized_and_a_sentence_is_left_alone():
    assert words.headline(_d("some_new_code"))["title"] == "Some new code"
    assert words.headline(_d("Ship the release notes?"))["title"] == "Ship the release notes?"
    assert words.plain_detail("missing_reservation") == "Missing reservation"
    assert words.plain_detail("The step ran out of time.") == "The step ran out of time."
    assert words.headline(_d(""))["title"] == "A decision is waiting"
