"""The embedding model's adapter: the one place an embeddings request is sent (operator decision 2026-10-22).

An ADAPTER FACTORY, like openai_adapter.py: it defines what the router builds. Nothing calls it
directly. `model_routing.project_embedder` builds it from the project's Embedding model slot
(knowledge/embedding_slot.py) and wraps it in `GovernedChatAdapter`, so every call is reserved
against the budget (category `knowledge`), receipted and settled
(engine/tests/test_embedding_model_slot.py holds that).

Providers:
  * `azure`   Azure OpenAI: an endpoint, a deployment and an API version; the key is in the vault.
  * `openai`  OpenAI, or any OpenAI-compatible endpoint (a base URL); the key is in the vault.
  * `ollama`  a local Ollama (e.g. `nomic-embed-text`), through its OpenAI-compatible /v1 API.
  * `scripted` BOSS's conformance engine: deterministic vectors and no model. It runs only under
              BOSS_SCRIPTED_ENGINE=1 (tests and the conformance gate), never as an operator's choice.

The vector's length is the model's own unless the slot asks for one (`request_dimensions`,
the `dimensions` parameter text-embedding-3 models take): Setup learns it from the check.

`create_embeddings(texts) -> (vectors, usage)`. `usage["billing"]` says whether the call was
money: Ollama and the scripted engine are local, every hosted endpoint is metered.
"""
from __future__ import annotations

import hashlib
import math
from typing import Any, Optional

from .github_adapter import IntegrationConfigurationError

PROVIDERS = ("azure", "openai", "ollama", "scripted")
LOCAL_PROVIDERS = frozenset({"ollama", "scripted"})
DEFAULT_AZURE_API_VERSION = "2024-10-21"
DEFAULT_OLLAMA_URL = "http://localhost:11434"

#: List price, USD micros per input token (USD per million tokens), of the hosted embedding models
#: BOSS knows. A model it does not know is priced as the dearest, so a reservation never runs short.
PRICE_MICROS_PER_TOKEN = {
    "text-embedding-3-small": 0.02,
    "text-embedding-3-large": 0.13,
    "text-embedding-ada-002": 0.10,
}
UNKNOWN_PRICE_MICROS_PER_TOKEN = 0.13

#: The scripted engine's model that answers chat only: its embedding check fails, as a real
#: chat-only model's does.
SCRIPTED_CHAT_ONLY = "scripted-chat-only"
#: The scripted engine's own vector length, when the slot asks for none (a real model has one too).
SCRIPTED_NATIVE_DIMENSION = 8


class EmbeddingsUnsupported(RuntimeError):
    """The provider or model does not do embeddings."""


def cost_usd_micros(model: str, tokens: int) -> int:
    price = PRICE_MICROS_PER_TOKEN.get(str(model or "").strip().lower(), UNKNOWN_PRICE_MICROS_PER_TOKEN)
    return int(math.ceil(max(0, int(tokens)) * price))


def _scripted_vector(text: str, dimension: int) -> list[float]:
    """The same text always gets the same unit vector; a word lands on a hashed dimension."""
    vector = [0.0] * dimension
    for word in str(text).lower().split():
        slot = int(hashlib.sha256(word.encode()).hexdigest(), 16) % dimension
        vector[slot] += 1.0
    norm = math.sqrt(sum(x * x for x in vector)) or 1.0
    return [x / norm for x in vector] if any(vector) else [1.0 / math.sqrt(dimension)] * dimension


class EmbeddingAdapter:
    def __init__(self, provider: str, model: str, dimension: int, *, base_url: str = "",
                 endpoint: str = "", deployment: str = "", api_version: str = "",
                 api_key: Optional[str] = None, request_dimensions: Optional[int] = None):
        self.provider = str(provider or "").strip().lower()
        if self.provider not in PROVIDERS:
            raise IntegrationConfigurationError(f"'{provider}' is not an embedding provider BOSS knows.")
        self.model = str(model or deployment or "").strip()
        self.dimension = int(dimension)
        self.base_url = str(base_url or "").strip().rstrip("/")
        self.endpoint = str(endpoint or "").strip().rstrip("/")
        self.deployment = str(deployment or "").strip()
        self.api_version = str(api_version or "").strip() or DEFAULT_AZURE_API_VERSION
        self._api_key = api_key
        self.request_dimensions = int(request_dimensions) if request_dimensions else None

    def _billing(self) -> str:
        from .included_spend import BILLING_LOCAL, BILLING_METERED

        return BILLING_LOCAL if self.provider in LOCAL_PROVIDERS else BILLING_METERED

    def create_embeddings(self, texts: list[str], model: Optional[str] = None) -> tuple[list[list[float]], dict]:
        texts = [str(t) for t in texts]
        if self.provider == "scripted":
            return self._scripted(texts)
        client, used = self._client()
        extra = {"dimensions": self.request_dimensions} if self.request_dimensions else {}
        response = client.embeddings.create(model=used, input=texts, **extra)
        vectors = [list(item.embedding) for item in sorted(response.data, key=lambda d: d.index)]
        tokens = int(getattr(getattr(response, "usage", None), "prompt_tokens", 0) or 0)
        usage = {"model": used, "provider": self.provider, "prompt_tokens": tokens, "completion_tokens": 0,
                 "total_tokens": tokens, "operation": "embeddings", "billing": self._billing(),
                 "cost_usd_micros": 0 if self.provider in LOCAL_PROVIDERS else cost_usd_micros(used, tokens)}
        return vectors, usage

    def _scripted(self, texts: list[str]) -> tuple[list[list[float]], dict]:
        from . import scripted_engine

        if not scripted_engine.enabled():
            raise IntegrationConfigurationError(
                "the scripted embedding model is BOSS's conformance engine and plays no model; it runs "
                f"only under the conformance gate ({scripted_engine.ENABLE_ENV}=1). Choose a real provider.")
        if self.model == SCRIPTED_CHAT_ONLY:
            raise EmbeddingsUnsupported(
                f"404 model '{self.model}' does not support embeddings (scripted)")
        tokens = sum(len(t.split()) for t in texts)
        usage = {"model": self.model, "provider": "scripted", "prompt_tokens": tokens, "completion_tokens": 0,
                 "total_tokens": tokens, "operation": "embeddings", "billing": self._billing(),
                 "cost_usd_micros": 0}
        size = self.request_dimensions or SCRIPTED_NATIVE_DIMENSION
        return [_scripted_vector(t, size) for t in texts], usage

    def _client(self) -> tuple[Any, str]:
        from .env_refs import check_outbound

        if self.provider == "azure":
            from openai import AzureOpenAI

            if not (self.endpoint and self.deployment):
                raise IntegrationConfigurationError("Azure OpenAI needs an endpoint and a deployment name.")
            if not self._api_key:
                raise IntegrationConfigurationError("Azure OpenAI needs its key (stored in the vault).")
            check_outbound(self.endpoint, {"api-key": self._api_key})
            return AzureOpenAI(api_key=self._api_key, azure_endpoint=self.endpoint,
                               api_version=self.api_version, max_retries=0), self.deployment
        from openai import OpenAI

        if self.provider == "ollama":
            base = (self.base_url or DEFAULT_OLLAMA_URL)
            base = base if base.endswith("/v1") else f"{base}/v1"
            return OpenAI(api_key="ollama", base_url=base, max_retries=0), self.model
        if not self._api_key and not self.base_url:
            raise IntegrationConfigurationError("OpenAI needs its key (stored in the vault).")
        key = self._api_key or "not-needed"
        check_outbound(self.base_url or "https://api.openai.com/v1", {"Authorization": f"Bearer {key}"})
        kwargs: dict[str, Any] = {"api_key": key, "max_retries": 0}
        if self.base_url:
            kwargs["base_url"] = self.base_url
        return OpenAI(**kwargs), self.model
