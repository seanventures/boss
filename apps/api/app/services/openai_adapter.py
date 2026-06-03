from __future__ import annotations

from typing import Optional, Tuple

from openai import OpenAI

from ..config import Settings
from .github_adapter import IntegrationConfigurationError


class OpenAIAdapter:
    def __init__(self, settings: Settings):
        self.settings = settings

    def create_text_response(
        self, prompt: str, system_prompt: Optional[str] = None
    ) -> Tuple[str, str]:
        if not self.settings.openai_api_key:
            raise IntegrationConfigurationError("OPENAI_API_KEY is not configured.")

        client = OpenAI(api_key=self.settings.openai_api_key)
        response = client.responses.create(
            model=self.settings.openai_model,
            instructions=system_prompt,
            input=prompt,
        )
        return response.id, response.output_text
