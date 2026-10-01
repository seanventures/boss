"""A local Ollama connection is priced at zero and used only where it should be.

- The zero price is keyed to the local endpoint ("<base url>#<model>"), so the same model name
  served by a hosted provider keeps its real (or default) price.
- On the native (OpenAI-compatible) lane, a local Ollama never displaces a hosted connection,
  whichever was saved last.
"""
from __future__ import annotations

import pytest

from boss_engine.config import get_settings
from boss_engine.services import openai_adapter
from boss_engine.services.code_agent import choose_native_source, is_local_model_endpoint
from boss_engine.services.openai_adapter import estimate_cost_usd_micros, price_for_model

LOCAL = "http://host.docker.internal:11434/v1"
MODEL = "qwen2.5-coder:7b"


@pytest.fixture
def local_price(monkeypatch):
    monkeypatch.setenv("BOSS_MODEL_PRICES_JSON", '{"%s#%s": [0, 0]}' % (LOCAL, MODEL))
    get_settings.cache_clear()
    yield
    monkeypatch.undo()
    get_settings.cache_clear()


def test_the_zero_price_applies_to_the_local_endpoint_only(local_price) -> None:
    assert price_for_model(MODEL, endpoint=LOCAL) == (0.0, 0.0, True)
    assert price_for_model(MODEL, endpoint=LOCAL + "/") == (0.0, 0.0, True)
    # The same name on a hosted endpoint, or with no endpoint, is not zero.
    hosted = price_for_model(MODEL, endpoint="https://openrouter.ai/api/v1")
    assert (hosted[0], hosted[1]) == openai_adapter.DEFAULT_PRICE and hosted[2] is False
    bare = price_for_model(MODEL)
    assert (bare[0], bare[1]) == openai_adapter.DEFAULT_PRICE


def test_usage_carries_the_endpoint_into_the_cost(local_price) -> None:
    usage = {"model": MODEL, "prompt_tokens": 1_000_000, "completion_tokens": 1_000_000}
    assert estimate_cost_usd_micros({**usage, "base_url": LOCAL}) == 0
    assert estimate_cost_usd_micros({**usage, "base_url": "https://api.example.invalid/v1"}) > 0
    assert estimate_cost_usd_micros(usage) > 0


def _row(source_id: str, base_url: str) -> dict:
    return {"source_id": source_id, "connection_metadata": {"base_url": base_url}}


def _choose(rows: list[dict], env_hosted: bool = False) -> dict | None:
    return choose_native_source(rows, env_hosted=env_hosted,
                                metadata=lambda row: row["connection_metadata"])


@pytest.mark.parametrize("ollama_saved_last", [True, False])
def test_a_hosted_connection_wins_the_native_lane_in_either_order(ollama_saved_last) -> None:
    ollama = _row("setup-custom", LOCAL)
    hosted = _row("setup-openai", "https://api.openai.com/v1")
    newest_first = [ollama, hosted] if ollama_saved_last else [hosted, ollama]
    assert _choose(newest_first) is hosted


def test_ollama_wins_the_native_lane_only_when_nothing_hosted_is_connected() -> None:
    ollama = _row("setup-custom", LOCAL)
    assert _choose([ollama]) is ollama
    # A hosted OpenAI key in the environment counts too: the lane falls back to it.
    assert _choose([ollama], env_hosted=True) is None
    # A custom endpoint that is not local is a hosted connection like any other.
    proxy = _row("setup-custom", "https://llm.example.invalid/v1")
    assert _choose([proxy], env_hosted=True) is proxy


def test_local_endpoints() -> None:
    assert is_local_model_endpoint(LOCAL)
    assert is_local_model_endpoint("http://127.0.0.1:11434/v1")
    assert not is_local_model_endpoint("https://api.openai.com/v1")
    assert not is_local_model_endpoint("")
