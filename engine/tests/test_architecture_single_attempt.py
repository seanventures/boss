"""A scoped architecture request cannot invisibly retry/resample; ordinary calls retain retries."""
from types import SimpleNamespace

import httpx
import pytest
from openai import APIConnectionError

from boss_engine.config import Settings
from boss_engine.services import openai_adapter as adapter


def transient():
    return APIConnectionError(request=httpx.Request("POST", "https://owned.invalid"))


def test_single_attempt_refuses_hidden_retry_and_restores_ordinary_retry(monkeypatch):
    monkeypatch.setattr(adapter.time, "sleep", lambda *a: None)
    calls = []
    def flaky():
        calls.append(1)
        if len(calls) % 2:
            raise transient()
        return "response"
    with adapter.single_attempt(), pytest.raises(APIConnectionError):
        adapter._with_retry(flaky, tries=3)
    assert len(calls) == 1
    calls.clear()
    assert adapter._with_retry(flaky, tries=2) == "response"
    assert len(calls) == 2


def test_single_attempt_sets_sdk_retry_zero_only_inside_scope(monkeypatch):
    monkeypatch.setattr(adapter, "OpenAI", lambda **kw: kw)
    bound = adapter.OpenAIAdapter(Settings().model_copy(update={"openai_api_key": "owned-test-only"}))
    assert "max_retries" not in bound._openai_client()
    with adapter.single_attempt():
        assert bound._openai_client()["max_retries"] == 0
    assert "max_retries" not in bound._openai_client()


def test_azure_architecture_request_also_disables_sdk_retry(monkeypatch):
    seen = []
    def client(**kw):
        seen.append(kw)
        def fail(**ignored):
            raise transient()
        return SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=fail)))
    monkeypatch.setattr(adapter, "AzureOpenAI", client)
    bound = adapter.OpenAIAdapter(Settings().model_copy(update={"azure_openai_key": "owned-test-only",
        "azure_openai_endpoint": "https://owned.invalid", "azure_openai_deployment": "owned"}))
    with adapter.single_attempt(), pytest.raises(APIConnectionError):
        bound.create_text_response("owned prompt")
    assert len(seen) == 1 and seen[0]["max_retries"] == 0
