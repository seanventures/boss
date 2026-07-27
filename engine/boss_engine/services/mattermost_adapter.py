from __future__ import annotations

import httpx

from ..config import Settings
from .github_adapter import IntegrationConfigurationError


class MattermostAdapter:
    """Official Mattermost bot API boundary for governed outbox delivery."""

    def __init__(self, settings: Settings):
        self.settings = settings

    def post_message(self, channel: str, subject: str | None, body: str) -> str:
        if not self.settings.mattermost_real_send_allowed:
            raise IntegrationConfigurationError(
                "BOSS_MATTERMOST_REAL_SEND_ALLOWED is false."
            )
        if not self.settings.mattermost_base_url:
            raise IntegrationConfigurationError(
                "BOSS_MATTERMOST_BASE_URL is not configured."
            )
        if not self.settings.mattermost_bot_token:
            raise IntegrationConfigurationError(
                "BOSS_MATTERMOST_BOT_TOKEN is not configured."
            )
        channel_id = channel.removeprefix("mattermost:").strip()
        if not channel_id:
            raise IntegrationConfigurationError(
                "Mattermost delivery requires mattermost:<channel_id>."
            )
        message = body.strip()
        if subject:
            message = f"**{subject.strip()}**\n{message}"
        response = httpx.post(
            f"{self.settings.mattermost_base_url.rstrip('/')}/api/v4/posts",
            headers={
                "Authorization": f"Bearer {self.settings.mattermost_bot_token}",
                "Content-Type": "application/json",
            },
            json={"channel_id": channel_id, "message": message},
            timeout=30.0,
        )
        response.raise_for_status()
        post_id = str(response.json().get("id") or "unknown")
        return f"mattermost-post:{post_id}"
