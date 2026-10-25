"""The API-key proof sends the output-cap field the model accepts (audit P2-9).

The one-turn proof always sent `max_tokens`. OpenAI's reasoning models (o1, o3, o4-mini) and
the gpt-5 family answer that with a 400 naming `max_completion_tokens`, so a good key read
"Check failed" and was not saved. Now the proof sends `max_completion_tokens` to those models,
and any 400 about the cap field is retried once with the field the provider named.
"""
from __future__ import annotations

import pytest

from boss_engine.services import setup_checks
from boss_engine.services.setup_checks import HttpAnswer

ENDPOINT = "https://api.openai.com/v1/chat/completions"
OK = HttpAnswer(200, {}, {"choices": [{"message": {"content": "OK"}}],
                          "usage": {"prompt_tokens": 3, "completion_tokens": 1}})


def _http(answers: list[HttpAnswer], sent: list[dict]):
    def http(method, url, *, headers, json_body=None, timeout=30.0):
        sent.append(json_body)
        return answers.pop(0)
    return http


@pytest.mark.parametrize("model", ["gpt-5", "gpt-5-mini", "gpt-5.1", "o1", "o3-mini",
                                   "o4-mini", "openai/gpt-5"])
def test_reasoning_and_gpt5_models_get_max_completion_tokens(model):
    sent: list[dict] = []
    result = setup_checks.complete_once(api="openai", endpoint=ENDPOINT, api_key="sk-x",
                                        model=model, http=_http([OK], sent))
    assert result["ok"] is True
    assert len(sent) == 1
    assert sent[0]["max_completion_tokens"] == 5 and "max_tokens" not in sent[0]


@pytest.mark.parametrize("model", ["gpt-4o", "gpt-4.1-mini", "llama3", "omni-moderation"])
def test_other_models_keep_max_tokens(model):
    sent: list[dict] = []
    setup_checks.complete_once(api="openai", endpoint=ENDPOINT, api_key="sk-x", model=model,
                               http=_http([OK], sent))
    assert sent[0]["max_tokens"] == 5 and "max_completion_tokens" not in sent[0]


def test_anthropic_always_sends_max_tokens():
    sent: list[dict] = []
    setup_checks.complete_once(api="anthropic", endpoint="https://api.anthropic.com/v1/messages",
                               api_key="k", model="o1-lookalike", http=_http([OK], sent))
    assert sent[0]["max_tokens"] == 5


def test_a_400_naming_max_completion_tokens_retries_once_with_it():
    refusal = HttpAnswer(400, {}, {"error": {
        "type": "invalid_request_error", "code": "unsupported_parameter",
        "message": "Unsupported parameter: 'max_tokens' is not supported with this model. "
                   "Use 'max_completion_tokens' instead."}})
    sent: list[dict] = []
    result = setup_checks.complete_once(api="openai", endpoint=ENDPOINT, api_key="sk-x",
                                        model="some-new-reasoner", http=_http([refusal, OK], sent))
    assert result["ok"] is True and result["state"] == "working"
    assert [sorted(k for k in body if k.startswith("max_")) for body in sent] == [
        ["max_tokens"], ["max_completion_tokens"]]


def test_a_compatible_server_that_refuses_max_completion_tokens_gets_max_tokens():
    refusal = HttpAnswer(400, {}, {"error": {
        "message": "Unrecognized request argument supplied: max_completion_tokens"}})
    sent: list[dict] = []
    result = setup_checks.complete_once(api="openai", endpoint=ENDPOINT, api_key="sk-x",
                                        model="gpt-5-compat", http=_http([refusal, OK], sent))
    assert result["ok"] is True
    assert "max_tokens" in sent[1] and "max_completion_tokens" not in sent[1]


def test_an_unrelated_400_is_not_retried():
    refusal = HttpAnswer(400, {}, {"error": {"message": "The model `nope` does not exist"}})
    sent: list[dict] = []
    result = setup_checks.complete_once(api="openai", endpoint=ENDPOINT, api_key="sk-x",
                                        model="nope", http=_http([refusal], sent))
    assert result["ok"] is False and len(sent) == 1
