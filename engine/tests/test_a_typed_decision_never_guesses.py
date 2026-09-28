"""A typed decision is one of the offered answers or no answer — never a coerced guess."""
from boss_engine.services.pipeline_profiles import PROFILES, infer_risk_tier, model_risk_tier
from boss_engine.services.typed_decisions import (
    Decision, DecisionBatch, Question, _parse,
)

Q = [Question("sensitive", "Is it sensitive?", {"yes": "", "no": ""})]


def test_an_offered_answer_is_kept_with_its_confidence():
    got = _parse('```json\n{"sensitive": {"answer": "no", "confidence": 0.9}}\n```', Q)
    assert got["sensitive"].answer == "no" and got["sensitive"].confidence == 0.9


def test_an_answer_that_is_not_an_option_is_refused():
    got = _parse('{"sensitive": {"answer": "maybe", "confidence": 0.9}}', Q)
    assert got["sensitive"].answer is None and "not an option" in got["sensitive"].error


def test_prose_is_no_answer():
    assert _parse("It is probably fine.", Q)["sensitive"].answer is None


class _Fixed:
    def __init__(self, tier, sensitive):
        self._d = {"tier": Decision(tier, 0.9), "sensitive": Decision(sensitive, 0.9)}

    def decide(self, context, questions):
        return DecisionBatch(self._d, "fake", "fake", 0)


def test_a_sensitive_verdict_holds_the_floor_even_in_lean():
    r = model_risk_tier({"title": "let people sign in with Google"}, PROFILES["lean"],
                        _Fixed("1", "yes"))
    assert r["tier"] >= 2 and r["sensitive"]


def test_a_failed_model_falls_back_to_the_keyword_tier():
    spec = {"title": "rotate the stripe secret"}
    r = model_risk_tier(spec, PROFILES["balanced"], _Fixed(None, None))
    assert not r["ok"] and r["tier"] == infer_risk_tier(spec, PROFILES["balanced"])


def test_an_explicit_tier_still_wins():
    r = model_risk_tier({"title": "x", "risk_tier": 1}, PROFILES["balanced"], _Fixed("2", "no"))
    assert r["tier"] == 1


# --- TypeSafe backend, against the response shape in docs.typesafe.ai/api.md ------------- #
from types import SimpleNamespace  # noqa: E402

from boss_engine.services.typed_decisions import (  # noqa: E402
    SecondOpinionDecider, TypeSafeDecider,
)

_TS = SimpleNamespace(typesafe_api_url="https://ts.invalid/v1/systemone", typesafe_model="jev-latest",
                      typesafe_api_key="k")


class _Resp:
    def __init__(self, status, body):
        self.status_code, self._body, self.headers, self.text = status, body, {}, str(body)

    def json(self):
        return self._body


class _Client:
    def __init__(self, *responses):
        self.responses, self.sent = list(responses), []

    def post(self, url, json, headers):
        self.sent.append(json)
        return self.responses.pop(0)


def _jev(choice, confidence):
    return _Resp(200, {"model": "jev-1.13.0", "usage": {"input_tokens": 1000},
                       "answers": {"sensitive": {"type": "choice", "choice": choice,
                                                 "confidence": confidence}}})


def test_typesafe_asks_a_choice_question_with_our_options():
    client = _Client(_jev("no", 0.93))
    batch = TypeSafeDecider(_TS, client=client).decide("restyle the login button", Q)
    sent = client.sent[0]["questions"]["sensitive"]
    assert sent["type"] == "choice" and set(sent["criteria"]) == {"yes", "no"}
    assert batch.decisions["sensitive"].answer == "no" and batch.cost_usd_micros == 42


def test_typesafe_backs_off_on_overloaded(monkeypatch):
    monkeypatch.setattr("time.sleep", lambda s: None)
    client = _Client(_Resp(529, {}), _jev("yes", 0.9))
    assert TypeSafeDecider(_TS, client=client).decide("x", Q).decisions["sensitive"].answer == "yes"


def test_an_unsure_fast_answer_is_asked_again():
    careful = _Fixed(None, "yes")
    both = SecondOpinionDecider(TypeSafeDecider(_TS, client=_Client(_jev("no", 0.4))), careful, 0.7)
    batch = both.decide("store the stripe key in vault", Q)
    assert batch.decisions["sensitive"].answer == "yes" and batch.usage["escalated"] == ["sensitive"]


def test_a_confident_fast_answer_is_not():
    both = SecondOpinionDecider(TypeSafeDecider(_TS, client=_Client(_jev("no", 0.95))),
                                _Fixed(None, "yes"), 0.7)
    assert both.decide("x", Q).decisions["sensitive"].answer == "no"


def test_typesafe_down_means_claude_decides():
    both = SecondOpinionDecider(TypeSafeDecider(_TS, client=_Client(_Resp(401, {}))),
                                _Fixed(None, "yes"), 0.7)
    assert both.decide("x", Q).decisions["sensitive"].answer == "yes"


def test_no_key_is_a_clear_error():
    import pytest
    with pytest.raises(RuntimeError, match="TYPESAFE_API_KEY"):
        TypeSafeDecider(SimpleNamespace(**{**vars(_TS), "typesafe_api_key": None}), api_key=None)


# --- The workflow step --------------------------------------------------------------------- #
from boss_engine.services import typed_decisions  # noqa: E402
from boss_engine.services.workflow_capability_runner import (  # noqa: E402
    CapabilityExecutionError, execute_step,
)
from boss_engine.services.workflows import (  # noqa: E402
    WorkflowValidationError, validate_workflow_spec,
)


def _route_step(**over):
    binding = {"typed_decision": {"question": "Which team handles this ticket?",
                                  "options": {"billing": "Payments", "technical": "Bugs"},
                                  "min_confidence": 0.8},
               "inputs": {"state": {"from_input": "ticket"}}}
    binding["typed_decision"].update(over)
    return {"id": "route", "kind": "capability", "binding": binding, "next": ["pick"]}


def _spec(step):
    return {"entry_step": "route", "steps": [
        step,
        {"id": "pick", "kind": "condition",
         "expression": {"path": "steps.route.output.answer", "op": "eq", "value": "billing"},
         "on_true": "a", "on_false": "b"},
        {"id": "a", "kind": "task"}, {"id": "b", "kind": "task"}]}


def test_a_decision_step_publishes_and_needs_no_approval():
    step = validate_workflow_spec(_spec(_route_step()))["steps"][0]
    assert step["approval"]["mode"] == "auto"


def test_a_decision_step_with_one_option_does_not_publish():
    import pytest
    with pytest.raises(WorkflowValidationError, match="between 2"):
        validate_workflow_spec(_spec(_route_step(options={"only": "x"})))


class _Answers:
    def __init__(self, answer, confidence):
        self.answer, self.confidence = answer, confidence

    def decide(self, context, questions):
        self.context = context
        return DecisionBatch({"decision": Decision(self.answer, self.confidence)}, "fake", "m", 1)


def _run(monkeypatch, decider, ticket="My card was charged twice"):
    monkeypatch.setattr(typed_decisions, "make_decider", lambda settings: decider)
    return execute_step(None, SimpleNamespace(settings=object()), project_id="p", actor="a",
                        step=validate_workflow_spec(_spec(_route_step()))["steps"][0],
                        context={"input": {"ticket": ticket}, "steps": {}})


def test_the_step_answers_from_its_wired_state(monkeypatch):
    decider = _Answers("billing", 0.93)
    out = _run(monkeypatch, decider)
    assert out["answer"] == "billing" and out["confident"] and "charged twice" in decider.context


def test_an_unsure_answer_says_so_instead_of_failing(monkeypatch):
    out = _run(monkeypatch, _Answers("billing", 0.5))
    assert out["answer"] == "billing" and out["confident"] is False


def test_no_answer_fails_the_step(monkeypatch):
    import pytest
    with pytest.raises(CapabilityExecutionError, match="no usable answer"):
        _run(monkeypatch, _Answers(None, None))


# --- The worker has no claude binary -------------------------------------------------------- #
def test_without_the_cli_decisions_use_the_api_model(monkeypatch):
    from boss_engine.config import Settings
    from boss_engine.services.typed_decisions import ClaudeDecider

    monkeypatch.setattr("shutil.which", lambda name: None)
    decider = ClaudeDecider(Settings(OPENAI_API_KEY="sk-test"), model="opus")
    assert decider.backend == "openai" and decider.model == ""


def test_without_the_cli_or_a_key_the_error_says_so(monkeypatch):
    import pytest
    from boss_engine.config import Settings
    from boss_engine.services.typed_decisions import ClaudeDecider

    monkeypatch.setattr("shutil.which", lambda name: None)
    monkeypatch.setattr("boss_engine.services.claude_cli_adapter.openai_fallback_usable",
                        lambda settings: False)
    with pytest.raises(RuntimeError, match="claude CLI is not installed"):
        ClaudeDecider(Settings(), model="opus")
