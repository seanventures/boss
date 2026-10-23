"""An agent's decision is a model's, checked against a threshold — never a keyword's.

Operator, 2026-10-18: "If we have a predefined list we can ask decision models like Jev to
confirm the decision or an accuracy threshold, but it cannot / should not work just on keywords
without context or models." agent_decision.decide is the one seam every agent-facing decision
goes through; these pin its contract: a model chooses one candidate with a confidence; below the
bar it asks ONE question and nothing is chosen; with no model it says so and nothing is chosen.
"""
from types import SimpleNamespace

from boss_engine.services import agent_decision as ad
from boss_engine.services.comms import CommsService

from ._decision_stub import DecisionModel

CANDIDATES = [ad.Candidate("operate", "retry the existing step"),
              ad.Candidate("new_code_change", "start a new build"),
              ad.Candidate("run_discovery", "research a topic", available=False,
                           why_not="discovery is a workflow step")]
SETTINGS = SimpleNamespace()


def _decide(model, message="retry the Build step of wfr_abc", **kw):
    return ad.decide(None, SETTINGS, "p", situation="a test", message=message,
                     candidates=CANDIDATES, adapter=model, **kw)


def test_the_model_chooses_and_its_confidence_is_kept():
    got = _decide(DecisionModel("operate", confidence=0.91))
    assert got.decided and got.action == "operate" and got.confidence == 0.91


def test_below_the_threshold_it_asks_one_question_and_chooses_nothing_to_do():
    got = _decide(DecisionModel("new_code_change", confidence=0.5,
                                question="A new build, or retry the Build step of wfr_abc?"))
    assert got.status == ad.CLARIFY and not got.decided
    assert got.question == "A new build, or retry the Build step of wfr_abc?"


def test_an_unsure_model_with_no_question_still_gets_one_asked():
    got = _decide(DecisionModel("new_code_change", confidence=0.2))
    assert got.status == ad.CLARIFY and got.question.endswith("?")


def test_an_answer_outside_the_candidates_is_refused_not_coerced():
    got = _decide(DecisionModel("create_code_task", confidence=0.99))
    assert got.status == ad.CLARIFY and got.action is None


def test_no_model_means_no_action_and_an_honest_status():
    class Down:
        def create_text_response(self, *_a, **_k):
            raise RuntimeError("no model connected")

    got = _decide(Down())
    assert got.status == ad.NO_MODEL and not got.decided
    assert "Repo and models" in ad.NO_MODEL_REPLY


def test_out_of_credit_says_so_up_front_not_just_no_model_is_answering():
    """BOSSAPP-604: when the provider's own words say plainly that the account is out of
    credit, the person is told THAT — with the provider named — not a generic sentence that
    reads as 'nothing is connected' and sends them to re-check a connection that was fine."""
    class OutOfCredit:
        _boss_choice = SimpleNamespace(engine="anthropic_api")

        def create_text_response(self, *_a, **_k):
            raise RuntimeError("Your credit balance is too low to access the Claude API.")

    got = _decide(OutOfCredit())
    assert got.status == ad.NO_MODEL and not got.decided
    assert got.provider == "anthropic_api"
    reply = ad.no_model_reply(got)
    assert "credit balance too low on anthropic_api" in reply
    assert reply != ad.NO_MODEL_REPLY

    # A plain connection failure (no credit phrase) still gets the generic, honest sentence.
    class Unreachable:
        _boss_choice = SimpleNamespace(engine="anthropic_api")

        def create_text_response(self, *_a, **_k):
            raise RuntimeError("connection refused")

    plain = _decide(Unreachable())
    assert ad.no_model_reply(plain) == ad.NO_MODEL_REPLY


def test_prose_around_the_json_is_read_and_garbage_is_asked_once_more():
    model = DecisionModel("x", reply='Sure! {"action": "operate", "confidence": 0.8} hope it helps')
    assert _decide(model).action == "operate"
    model = DecisionModel("x", reply="I think you want to retry it")
    got = _decide(model)
    assert got.status == ad.NO_MODEL and len(model.prompts) == 2


def test_the_model_sees_the_context_the_hints_and_what_is_unavailable():
    model = DecisionModel("operate")
    _decide(model, context={"in view": {"what": "workflow run wfr_abc", "status": "failed"}},
            hints=["a keyword reading of the words suggests 'create_code_task'"],
            history=[{"role": "user", "content": "the Build step failed"}])
    prompt = model.prompts[0]
    assert "wfr_abc" in prompt and "pattern guesses, not decisions" in prompt
    assert "NOT AVAILABLE HERE: discovery is a workflow step" in prompt
    assert "PERSON: the Build step failed" in prompt


def test_jev_answers_when_it_is_the_decider():
    from boss_engine.services.typed_decisions import Decision, DecisionBatch

    class Jev:
        def decide(self, context, questions):
            assert set(questions[0].options) == {c.key for c in CANDIDATES}
            return DecisionBatch({"action": Decision("operate", 0.88)}, "typesafe", "jev-1", 5)

    got = ad.decide(None, SETTINGS, "p", situation="t", message="retry it",
                    candidates=CANDIDATES, decider=Jev())
    assert got.decided and got.by["backend"] == "typesafe"


def test_a_decision_reply_is_decided_by_a_model_not_its_first_word():
    """"I don't approve dec_x, reject it" used to APPROVE: the first decision word won."""
    comms = CommsService(SimpleNamespace())
    parsed = comms._parse_inbound_message("email", "Re: dec_abc123",
                                          "I don't approve dec_abc123, reject it.", None)
    assert parsed.get("action") is None and parsed["action_hint"] == "approve"
    model = DecisionModel("reject", confidence=0.93)
    got = comms.decide_reply(None, "p", dict(parsed), "Re: dec_abc123",
                             "I don't approve dec_abc123, reject it.", adapter=model)
    assert got["action"] == "reject"
    assert "first decision word in the reply is 'approve'" in model.prompts[0]


def test_an_unclear_or_unanswerable_reply_transitions_nothing():
    comms = CommsService(SimpleNamespace())
    parsed = comms._parse_inbound_message("email", "dec_abc123", "approve? not sure yet", None)
    unsure = comms.decide_reply(None, "p", dict(parsed), "dec_abc123", "approve? not sure yet",
                                adapter=DecisionModel("approve", confidence=0.4,
                                                      question="Approve dec_abc123 now?"))
    assert "action" not in unsure and unsure["parse_error"] == "decision_reply_unclear"
    assert unsure["remediation"] == "Approve dec_abc123 now?"

    class Down:
        def create_text_response(self, *_a, **_k):
            raise RuntimeError("no model")

    none = comms.decide_reply(None, "p", dict(parsed), "dec_abc123", "approve", adapter=Down())
    assert "action" not in none and none["parse_error"] == "decision_reply_needs_a_model"


def test_a_recovery_the_model_is_unsure_of_is_one_question_with_its_suggestion():
    from boss_engine.services import step_recovery as sr

    asked = sr.unsure_ask({"headline": "push refused 403"},
                          {"action": "retry", "confidence": 0.4, "cause": "push refused 403",
                           "reasoning": "maybe transient"}, agent="Strata")
    assert asked["action"] == sr.ASK_PERSON and asked["question"].count("?") == 1
    assert asked["suggested"]["action"] == "retry"


def test_with_no_model_a_recovery_asks_and_names_the_triage_suggestion():
    from boss_engine.services import step_recovery as sr

    asked = sr.no_model_ask({"headline": "port 8000 held"},
                            {"action": "clean_and_retry", "cause": "port 8000 held by helpdesk-ai"},
                            error="RuntimeError: no model connected")
    assert asked["action"] == sr.ASK_PERSON
    assert "connected model" in asked["reasoning"] and "cleaning it up" in asked["question"]


def test_an_unsure_choice_that_changes_nothing_is_taken_not_asked():
    """"Answer or look into it?" is not worth a question: neither acts."""
    cands = [ad.Candidate("answer", "answer it", acts=False),
             ad.Candidate("investigate", "look into it", acts=False),
             ad.Candidate("operate", "retry the step")]
    got = ad.decide(None, SETTINGS, "p", situation="t", message="why did it fail",
                    candidates=cands, adapter=DecisionModel("investigate", confidence=0.5))
    assert got.decided and got.action == "investigate"
    got = ad.decide(None, SETTINGS, "p", situation="t", message="do it",
                    candidates=cands, adapter=DecisionModel("operate", confidence=0.5))
    assert got.status == ad.CLARIFY


def test_a_tool_less_cli_reply_in_the_tool_protocol_reads_as_its_answer():
    from boss_engine.services.claude_cli_adapter import _unwrapped_final

    assert _unwrapped_final('{"thought": "t", "final": "The step is queued."}') == \
        "The step is queued."
    assert _unwrapped_final('{"thought": "Offer the retry.", "tool_calls": [{"name": "x"}]}') \
        == "Offer the retry."
    assert _unwrapped_final('{"action": "retry", "confidence": 0.9}') == \
        '{"action": "retry", "confidence": 0.9}', "other JSON answers are left alone"
    assert _unwrapped_final("plain text") == "plain text"


def test_a_captain_sentence_is_the_boss_agents_to_decide():
    """BOSSAPP-759: the captain no longer decides sentences at all (its decide_free_text over
    agent_decision is gone). A sentence typed to Metatron is an Ask BOSS turn — the one model
    decision in IntentRouter.route_turn; a captain power is a typed command."""
    from boss_engine.services import metatron_captain as mc

    assert not hasattr(mc, "decide_free_text") and not hasattr(mc, "interpret_turn")
