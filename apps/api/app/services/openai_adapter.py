from __future__ import annotations

from typing import Optional, Tuple

from openai import AzureOpenAI, OpenAI

from ..config import Settings
from .github_adapter import IntegrationConfigurationError

# USD per 1,000,000 tokens: (input, output). Turns real token usage into a
# dollar cost (micros) for budget settlement and reporting.
PRICE_PER_MTOK: dict[str, tuple[float, float]] = {
    "gpt-4o": (2.50, 10.00),
    "gpt-4o-mini": (0.15, 0.60),
    "gpt-4.1": (2.00, 8.00),
    "gpt-4.1-mini": (0.40, 1.60),
    "gpt-5.2-codex": (2.50, 10.00),
}
DEFAULT_PRICE = (2.50, 10.00)


def estimate_cost_usd_micros(usage: dict) -> int:
    """micros = prompt_tokens * input_$/Mtok + completion_tokens * output_$/Mtok.

    If the backend already reported an exact cost (e.g. the Claude CLI returns
    total_cost_usd per call), honor that instead of the token price table."""
    if usage.get("cost_usd_micros") is not None:
        return int(usage["cost_usd_micros"])
    model = str(usage.get("model") or "")
    inp, out = PRICE_PER_MTOK.get(model, DEFAULT_PRICE)
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

    def create_text_response(
        self,
        prompt: str,
        system_prompt: Optional[str] = None,
        model: Optional[str] = None,
    ) -> Tuple[str, str, dict]:
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
            response = client.chat.completions.create(model=deployment, messages=messages)
            text = response.choices[0].message.content or ""
            return response.id, text, _usage_from_response(response, deployment)

        if not self.settings.openai_api_key:
            raise IntegrationConfigurationError("OPENAI_API_KEY is not configured.")

        client = OpenAI(api_key=self.settings.openai_api_key)
        used_model = model or self.settings.openai_model
        response = client.responses.create(
            model=used_model,
            instructions=system_prompt,
            input=prompt,
        )
        return response.id, response.output_text, _usage_from_response(response, used_model)

    def create_chat_completion(
        self,
        messages: list[dict],
        tools: Optional[list[dict]] = None,
        tool_choice: str = "auto",
    ) -> Tuple[dict, dict]:
        """One turn of a tool-calling conversation.

        Returns (message, usage) where message = {content, tool_calls, raw}.
        tool_calls is a list of {id, name, arguments(JSON string)} or None.
        """
        if self._azure_enabled():
            client = AzureOpenAI(
                api_key=self.settings.azure_openai_key,
                azure_endpoint=self.settings.azure_openai_endpoint,
                api_version=self.settings.azure_openai_api_version,
            )
            model = self.settings.azure_openai_deployment or "gpt-4o"
        elif self.settings.openai_api_key:
            client = OpenAI(api_key=self.settings.openai_api_key)
            model = self.settings.openai_model
        else:
            raise IntegrationConfigurationError("OPENAI_API_KEY is not configured.")

        kwargs: dict = {"model": model, "messages": messages}
        if tools:
            kwargs["tools"] = tools
            kwargs["tool_choice"] = tool_choice
        response = client.chat.completions.create(**kwargs)
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
        return normalized, _usage_from_response(response, model)
