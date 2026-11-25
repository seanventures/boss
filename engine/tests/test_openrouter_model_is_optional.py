"""BOSSAPP-1340: Add a model > OpenRouter pre-fills nothing; empty means OpenRouter's own routing."""
import json

import pytest

from boss_engine.services import openrouter_catalog, setup_actions, setup_checks, setup_service, setup_view
from boss_engine.services.setup_checks import HttpAnswer
from boss_engine.services.setup_service import SetupError, model_key_details


def test_openrouter_has_no_example_model_in_the_catalogue():
    entry = next(e for e in setup_view.CATALOGUE if e["provider"] == "openrouter")
    assert entry["example_model"] == ""
    assert entry["model_optional"] is True


def test_empty_openrouter_model_uses_the_providers_own_routing():
    details = model_key_details(provider="openrouter")
    assert details["model"] == "openrouter/auto"
    assert details["endpoint"].endswith("/chat/completions")


def test_a_named_openrouter_model_is_kept():
    assert model_key_details(provider="openrouter", model="openai/gpt-5")["model"] == "openai/gpt-5"


def test_other_providers_still_need_a_model_when_they_have_none_to_default_to():
    with pytest.raises(SetupError):
        model_key_details(provider="custom", base_url="https://x.example/v1")
    assert "openrouter" in setup_service.PROVIDERS


def test_openrouter_model_catalogue_returns_per_million_prices():
    setup_actions._LISTS.clear()
    setup_actions._LIST_PRICES.clear()
    openrouter_catalog.remember([])

    def fake_http(method, url, *, headers=None, json=None, timeout=10):
        return HttpAnswer(200, {}, {
            "data": [
                {"id": "openai/gpt-5", "pricing": {"prompt": "0.0000025", "completion": "0.00001"}},
                {"id": "deepseek/deepseek-chat", "pricing": {"prompt": "0.00000014", "completion": "0.00000028"}},
            ]
        })

    names, prices = setup_checks.list_models_with_prices(
        api="openai", base_url="https://openrouter.ai/api/v1", http=fake_http)
    assert names == ["deepseek/deepseek-chat", "openai/gpt-5"]
    assert prices["openai/gpt-5"] == {"prompt_usd_per_mtok": 2.5, "completion_usd_per_mtok": 10.0}
    assert prices["deepseek/deepseek-chat"] == {"prompt_usd_per_mtok": 0.14, "completion_usd_per_mtok": 0.28}

    cat_names, cat_prices = setup_actions.model_catalogue_with_prices(
        None, object(), "p_1", "openrouter", http=fake_http)
    assert cat_names == names
    assert cat_prices == prices
    assert setup_actions.model_catalogue(None, object(), "p_1", "openrouter", http=fake_http) == names


def test_whitespace_openrouter_model_uses_providers_own_routing():
    details = model_key_details(provider="openrouter", model="   ")
    assert details["model"] == "openrouter/auto"
    assert details["endpoint"].endswith("/chat/completions")


def test_non_finite_and_negative_openrouter_prices_are_rejected_and_json_safe():
    assert openrouter_catalog._per_mtok("NaN") is None
    assert openrouter_catalog._per_mtok("Infinity") is None
    assert openrouter_catalog._per_mtok("-Infinity") is None
    assert openrouter_catalog._per_mtok("-1") is None
    assert openrouter_catalog._per_mtok("0") == 0.0

    def fake_http(method, url, *, headers=None, json=None, timeout=10):
        return HttpAnswer(200, {}, {
            "data": [
                {"id": "nan/model", "pricing": {"prompt": "NaN", "completion": "0"}},
                {"id": "inf/model", "pricing": {"prompt": "Infinity", "completion": "0.000001"}},
                {"id": "badcomp/model", "pricing": {"prompt": "0.000001", "completion": "NaN"}},
                {"id": "free/model", "pricing": {"prompt": "0", "completion": "0"}},
            ]
        })

    names, prices = setup_checks.list_models_with_prices(
        api="openai", base_url="https://openrouter.ai/api/v1", http=fake_http)
    assert names == ["badcomp/model", "free/model", "inf/model", "nan/model"]
    assert "nan/model" not in prices
    assert "inf/model" not in prices
    assert prices["badcomp/model"] == {"prompt_usd_per_mtok": 1.0, "completion_usd_per_mtok": 0.0}
    assert prices["free/model"] == {"prompt_usd_per_mtok": 0.0, "completion_usd_per_mtok": 0.0}
    encoded = json.dumps({"models": names, "prices": prices}, allow_nan=False)
    assert "NaN" not in encoded and "Infinity" not in encoded
