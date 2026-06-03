from __future__ import annotations

import httpx

from ..config import Settings
from .github_adapter import IntegrationConfigurationError


class SlackAdapter:
    def __init__(self, settings: Settings):
        self.settings = settings

    def post_message(self, channel: str, subject: str | None, body: str) -> str:
        if not self.settings.slack_real_send_allowed:
            raise IntegrationConfigurationError(
                "BOSS_SLACK_REAL_SEND_ALLOWED is false."
            )
        if not self.settings.slack_webhook_url:
            raise IntegrationConfigurationError(
                "BOSS_SLACK_WEBHOOK_URL is not configured."
            )

        text = body.strip()
        if subject:
            text = f"*{subject.strip()}*\n{text}"

        response = httpx.post(
            self.settings.slack_webhook_url,
            json={"text": text},
            timeout=30.0,
        )
        response.raise_for_status()
        return f"slack-webhook:{channel}"
