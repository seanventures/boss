"""The embedding lane resolves OLLAMA_BASE_URL at call time, like chat/setup/detect/connect.

Every other Ollama lane resolves `found.base_url or setup_service.ollama_base_url(settings)`,
so inside a Community container an empty URL field means the HOST's Ollama
(http://host.docker.internal:11434/v1, the compose default). The embedding lane was the one
place that did not: `model_routing.project_embedder` built the EmbeddingAdapter with
`base_url=str(slot.get('base_url') or '')` and nothing else, and the adapter fell back to a
hardcoded `DEFAULT_OLLAMA_URL = http://localhost:11434`. So an operator who saves an Ollama
embedding slot with the URL left empty (which `embedding_slot._validate` allows) had every
embedding call go to localhost:11434 inside the container — nothing — instead of the setting.

These tests build the embedder the way the router does (no network, no real host: a neutral
RFC 5737 address stands in for the host's Ollama) and read the base URL the adapter's OpenAI
client would actually dial. They fail on this checkout because `project_embedder` omits the
call-time resolution; they pass once it resolves an empty slot URL through the same helper.
"""
from __future__ import annotations

from boss_engine.config import Settings
from boss_engine.services import model_routing, setup_service
from boss_engine.services.knowledge import embedding_slot

# Neutral stand-ins for the host's Ollama seen from the container (RFC 5737 TEST-NET-1/2),
# never a real host. The point is only that the target follows the setting, not localhost.
OLLAMA_SETTING_A = "http://192.0.2.10:11434/v1"
OLLAMA_SETTING_B = "http://198.51.100.20:11434/v1"


def _empty_url_ollama_slot() -> dict:
    """An Ollama embedding slot saved with the URL field left empty, through the real validator
    (so the precondition the diagnosis names — `base_url == ''` is allowed — is exercised)."""
    slot = embedding_slot._validate("ollama", model="nomic-embed-text", dimension=None,
                                    base_url="", endpoint="", deployment="", api_version="")
    assert slot["base_url"] == "", "the operator left the Ollama URL empty; the slot stores ''"
    return slot


def _dialled_base_url(settings: Settings, slot: dict) -> tuple[str, dict]:
    """Where the embedder `project_embedder` builds would actually send its embedding call.

    No connection or project row is touched: an Ollama slot carries no key, so nothing is read
    from the vault, and the governed wrapper only dials when `embed()` is called."""
    embedder = model_routing.project_embedder(object(), settings, "proj_embed_test", slot)
    inner = embedder._adapter._inner                 # the EmbeddingAdapter under the governor
    client, _model = inner._client()                 # constructs the OpenAI client, no network
    return str(client.base_url).lower(), {"inner": inner}


def test_project_embedder_sends_ollama_to_the_setting_not_localhost(monkeypatch):
    monkeypatch.setenv("OLLAMA_BASE_URL", OLLAMA_SETTING_A)
    settings = Settings()
    # The setting is what chat/setup/detect/connect resolve; the embedder must reach the same place.
    assert setup_service.ollama_base_url(settings) == OLLAMA_SETTING_A

    slot = _empty_url_ollama_slot()
    dialled, extra = _dialled_base_url(settings, slot)

    assert "192.0.2.10" in dialled, (
        "model_routing.project_embedder built an Ollama embedder that dials "
        f"{dialled!r}; with an empty slot URL it must resolve OLLAMA_BASE_URL "
        f"({OLLAMA_SETTING_A}), as every other Ollama lane does")
    assert "localhost" not in dialled and "127.0.0.1" not in dialled, (
        "model_routing.project_embedder fell back to the hardcoded localhost Ollama instead of "
        f"OLLAMA_BASE_URL; inside a Community container that reaches nothing (dialled {dialled!r})")
    # INVARIANT (project_embedder docstring): the key is resolved from the vault for this
    # project, never from the environment. An Ollama slot carries no key, and resolving the HOST
    # from the setting must not start pulling a key from the environment.
    assert extra["inner"]._api_key is None, (
        "model_routing.project_embedder put a key on the Ollama embedder; the slot carries none "
        "and the key must come from the vault, never the environment")


def test_changing_the_setting_moves_the_embedder_and_nothing_is_baked_into_the_slot(monkeypatch):
    slot = _empty_url_ollama_slot()

    monkeypatch.setenv("OLLAMA_BASE_URL", OLLAMA_SETTING_A)
    first, _ = _dialled_base_url(Settings(), slot)

    monkeypatch.setenv("OLLAMA_BASE_URL", OLLAMA_SETTING_B)
    second, _ = _dialled_base_url(Settings(), slot)

    assert "192.0.2.10" in first and "198.51.100.20" in second, (
        "model_routing.project_embedder does not follow OLLAMA_BASE_URL at call time: "
        f"the target did not move with the setting (first {first!r}, second {second!r})")
    # Resolution is at call time: nothing is written back into the stored slot, so one saved
    # slot serves whatever the setting says after a restart.
    assert slot["base_url"] == "", (
        "model_routing.project_embedder baked a host into the stored Ollama slot; the empty URL "
        f"must stay empty so OLLAMA_BASE_URL still decides (slot base_url is {slot['base_url']!r})")
