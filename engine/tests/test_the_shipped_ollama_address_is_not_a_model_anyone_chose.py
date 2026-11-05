"""boss-1, 2026-11-05: Repo and models listed "Ollama · Can't reach" and counted it as needing
the operator, on a box with no Ollama. The Community compose sets OLLAMA_BASE_URL to the host's
address, which differs from config.py's default, so it read as a deliberate setting."""
from boss_engine.config import Settings
from boss_engine.services.setup_service import _set_deliberately


def _settings(**values):
    return Settings().model_copy(update=values)


def test_the_compose_default_is_not_deliberate():
    for url in ("http://host.docker.internal:11434/v1", "http://host.docker.internal:11434/v1/",
                "http://host.docker.internal:11434"):
        assert _set_deliberately(_settings(ollama_base_url=url), "ollama_base_url") is False, url


def test_the_code_default_is_not_deliberate():
    assert _set_deliberately(Settings(), "ollama_base_url") is False


def test_an_address_somebody_typed_is_a_model():
    assert _set_deliberately(_settings(ollama_base_url="http://gpubox:11434/v1"), "ollama_base_url") is True
