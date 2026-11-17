"""BOSSAPP-1340: Add a model > OpenRouter pre-fills nothing; empty means OpenRouter's own routing."""
import pytest

from boss_engine.services import setup_service, setup_view
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
