from __future__ import annotations

import random
import time
from typing import Optional, Tuple

from openai import APIConnectionError, APITimeoutError, AzureOpenAI, OpenAI, RateLimitError

from ..config import Settings
from .github_adapter import IntegrationConfigurationError
from .redaction import redact as _redact

_RETRYABLE = (RateLimitError, APITimeoutError, APIConnectionError)


def _with_retry(call, *, tries: int = 8, base: float = 2.0, stats: Optional[dict] = None):
    """Call an OpenAI request, retrying on 429 / transient errors with exponential backoff +
    jitter. Honours a Retry-After header when the provider supplies one. ``stats`` (when
    given) receives {"retries": n} — how many retries the call needed (0 = first try)."""
    for attempt in range(tries):
        if stats is not None:
            stats["retries"] = attempt
        try:
            return call()
        except _RETRYABLE as exc:
            if attempt == tries - 1:
                raise
            delay = base * (2 ** attempt) + random.uniform(0, 1)
            retry_after = getattr(getattr(exc, "response", None), "headers", {}) or {}
            try:
                delay = max(delay, float(retry_after.get("retry-after")))
            except (TypeError, ValueError):
                pass
            time.sleep(min(delay, 45.0))

# USD per 1,000,000 tokens: (input, output). Turns real token usage into a
# dollar cost (micros) for budget settlement and reporting. Extend/correct per
# deployment via BOSS_MODEL_PRICES_JSON (a {"model": [in, out]} JSON object) —
# open-model prices vary by host (DeepSeek direct vs OpenRouter vs Fireworks).
PRICE_PER_MTOK: dict[str, tuple[float, float]] = {
    "gpt-4o": (2.50, 10.00),
    "gpt-4o-mini": (0.15, 0.60),
    "gpt-4.1": (2.00, 8.00),
    "gpt-4.1-mini": (0.40, 1.60),
    "gpt-5.2-codex": (2.50, 10.00),
    "deepseek-chat": (0.27, 1.10),
    "deepseek-reasoner": (0.55, 2.19),
    "qwen2.5-coder-32b-instruct": (0.90, 0.90),
}
DEFAULT_PRICE = (2.50, 10.00)


def _price_overrides() -> dict[str, tuple[float, float]]:
    from ..config import get_settings

    raw = (get_settings().model_prices_json or "").strip()
    if not raw:
        return {}
    if raw != _price_overrides_cache.get("raw"):
        try:
            import json as _json

            parsed = {
                str(k): (float(v[0]), float(v[1]))
                for k, v in _json.loads(raw).items()
            }
        except Exception:  # noqa: BLE001 — a bad override must not break cost accounting
            parsed = {}
        _price_overrides_cache.update({"raw": raw, "parsed": parsed})
    return _price_overrides_cache.get("parsed", {})


_price_overrides_cache: dict = {}


def price_for_model(model: str) -> tuple[float, float, bool]:
    """(input_$/Mtok, output_$/Mtok, known). Overrides win; provider-prefixed names
    (OpenRouter's "deepseek/deepseek-chat") also match on their basename. known=False
    means the DEFAULT_PRICE fallback was used — treat that cost as an estimate."""
    table = {**PRICE_PER_MTOK, **_price_overrides()}
    for key in (model, model.rsplit("/", 1)[-1]):
        if key in table:
            return (*table[key], True)
    return (*DEFAULT_PRICE, False)


def estimate_cost_usd_micros(usage: dict) -> int:
    """micros = prompt_tokens * input_$/Mtok + completion_tokens * output_$/Mtok.

    If the backend already reported an exact cost (e.g. the Claude CLI returns
    total_cost_usd per call), honor that instead of the token price table."""
    if usage.get("cost_usd_micros") is not None:
        return int(usage["cost_usd_micros"])
    inp, out, _ = price_for_model(str(usage.get("model") or ""))
    prompt = int(usage.get("prompt_tokens") or 0)
    completion = int(usage.get("completion_tokens") or 0)
    return int(round(prompt * inp + completion * out))


def _usage_from_response(response, model: str) -> dict:
    u = getattr(response, "usage", None)
    prompt = getattr(u, "prompt_tokens", None) or getattr(u, "input_tokens", None) or 0
    completion = (
        getattr(u, "completion_tokens", None) or getattr(u, "output_tokens", None) or 0
    )
    total = getattr(u, "total_tokens", None) or (int(prompt) + int(completion))
    return {
        "model": model,
        "prompt_tokens": int(prompt),
        "completion_tokens": int(completion),
        "total_tokens": int(total),
    }


class OpenAIAdapter:
    def __init__(self, settings: Settings):
        self.settings = settings

    def _azure_enabled(self) -> bool:
        return bool(
            self.settings.azure_openai_key
            and self.settings.azure_openai_endpoint
            and self.settings.azure_openai_deployment
        )

    def _openai_client(self) -> OpenAI:
        """Plain-OpenAI client honoring OPENAI_BASE_URL, so the HOST-side native engine can
        target any OpenAI-compatible provider (OpenRouter/Fireworks/DeepSeek/local) — the
        same knob the guest path already honors. Empty base_url => SDK default (api.openai.com).
        Azure keeps precedence via _azure_enabled(); base_url is ignored when Azure is configured."""
        kwargs: dict = {"api_key": self.settings.openai_api_key}
        base = (self.settings.openai_base_url or "").strip()
        if base:
            kwargs["base_url"] = base
        return OpenAI(**kwargs)

    def create_text_response(
        self,
        prompt: str,
        system_prompt: Optional[str] = None,
        model: Optional[str] = None,
        redact: bool = False,
    ) -> Tuple[str, str, dict]:
        # Data-boundary safety: redact PII before the prompt crosses to the external model.
        if redact:
            prompt, _ = _redact(prompt)
        if self._azure_enabled():
            client = AzureOpenAI(
                api_key=self.settings.azure_openai_key,
                azure_endpoint=self.settings.azure_openai_endpoint,
                api_version=self.settings.azure_openai_api_version,
            )
            messages: list[dict] = []
            if system_prompt:
                messages.append({"role": "system", "content": system_prompt})
            messages.append({"role": "user", "content": prompt})
            deployment = self.settings.azure_openai_deployment or "gpt-4o"
            stats: dict = {}
            response = _with_retry(
                lambda: client.chat.completions.create(model=deployment, messages=messages),
                stats=stats,
            )
            text = response.choices[0].message.content or ""
            usage = _usage_from_response(response, deployment)
            usage["retries"] = stats.get("retries", 0)
            return response.id, text, usage

        if not self.settings.openai_api_key:
            raise IntegrationConfigurationError("OPENAI_API_KEY is not configured.")

        # NOTE: the Responses API below is OpenAI-specific — most OpenAI-compatible hosts
        # (DeepSeek/OpenRouter/Fireworks) only implement chat.completions. The coding
        # pipeline (NativeCodeAgent) uses create_chat_completion, which is portable.
        client = self._openai_client()
        used_model = model or self.settings.openai_model
        stats = {}
        response = _with_retry(lambda: client.responses.create(
            model=used_model,
            instructions=system_prompt,
            input=prompt,
        ), stats=stats)
        usage = _usage_from_response(response, used_model)
        usage["retries"] = stats.get("retries", 0)
        return response.id, response.output_text, usage

    def create_chat_completion(
        self,
        messages: list[dict],
        tools: Optional[list[dict]] = None,
        tool_choice: str = "auto",
        model_override: Optional[str] = None,
        redact: bool = False,
    ) -> Tuple[dict, dict]:
        """One turn of a tool-calling conversation.

        Returns (message, usage) where message = {content, tool_calls, raw}.
        tool_calls is a list of {id, name, arguments(JSON string)} or None.
        """
        if redact:  # data-boundary safety: mask PII in message content before the external call
            messages = [
                {**m, "content": _redact(m["content"])[0]} if isinstance(m.get("content"), str) else m
                for m in messages
            ]
        if self._azure_enabled():
            client = AzureOpenAI(
                api_key=self.settings.azure_openai_key,
                azure_endpoint=self.settings.azure_openai_endpoint,
                api_version=self.settings.azure_openai_api_version,
            )
            model = self.settings.azure_openai_deployment or "gpt-4o"
        elif self.settings.openai_api_key:
            client = self._openai_client()
            model = self.settings.openai_model
        else:
            raise IntegrationConfigurationError("OPENAI_API_KEY is not configured.")

        if model_override:
            model = model_override
        kwargs: dict = {"model": model, "messages": messages}
        if tools:
            kwargs["tools"] = tools
            kwargs["tool_choice"] = tool_choice
        stats: dict = {}
        response = _with_retry(lambda: client.chat.completions.create(**kwargs), stats=stats)
        msg = response.choices[0].message
        tool_calls = None
        if getattr(msg, "tool_calls", None):
            tool_calls = [
                {
                    "id": tc.id,
                    "name": tc.function.name,
                    "arguments": tc.function.arguments,
                }
                for tc in msg.tool_calls
            ]
        normalized = {
            "content": msg.content,
            "tool_calls": tool_calls,
            "raw": msg.model_dump(exclude_none=True),
        }
        usage = _usage_from_response(response, model)
        usage["retries"] = stats.get("retries", 0)
        return normalized, usage
