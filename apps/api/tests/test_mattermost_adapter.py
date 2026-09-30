from types import SimpleNamespace

import pytest

from apps.api.app.services.comms import CommsService
from apps.api.app.services.github_adapter import IntegrationConfigurationError
from apps.api.app.services.mattermost_adapter import MattermostAdapter


class _Response:
    def raise_for_status(self):
        return None

    def json(self):
        return {"id": "post-123"}


def _settings(**overrides):
    values = {
        "mattermost_real_send_allowed": True,
        "mattermost_base_url": "https://chat.example.test",
        "mattermost_bot_token": "secret-token",
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def test_mattermost_adapter_posts_as_bot_without_exposing_token(monkeypatch):
    captured = {}

    def fake_post(url, *, headers, json, timeout):
        captured.update(
            {"url": url, "headers": headers, "json": json, "timeout": timeout}
        )
        return _Response()

    monkeypatch.setattr("httpx.post", fake_post)
    result = MattermostAdapter(_settings()).post_message(
        "mattermost:channel-7",
        "Deploy status",
        "Production rollout is healthy.",
    )

    assert result == "mattermost-post:post-123"
    assert captured["url"].endswith("/api/v4/posts")
    assert captured["json"]["channel_id"] == "channel-7"
    assert "Deploy status" in captured["json"]["message"]
    assert captured["headers"]["Authorization"] == "Bearer secret-token"


def test_mattermost_adapter_fails_closed_without_rollout_or_credentials():
    with pytest.raises(IntegrationConfigurationError):
        MattermostAdapter(
            _settings(mattermost_real_send_allowed=False)
        ).post_message("mattermost:channel-7", None, "hello")
    with pytest.raises(IntegrationConfigurationError):
        MattermostAdapter(
            _settings(mattermost_bot_token=None)
        ).post_message("mattermost:channel-7", None, "hello")


def test_comms_routes_mattermost_channels_through_governed_adapter(monkeypatch):
    service = CommsService(_settings())
    provider = service._provider_for_channel("mattermost:channel-7")

    assert provider == {
        "key": "mattermost",
        "label": "Mattermost",
        "effect_kind": "mattermost.message.post",
        "skill_id": "mattermost.post_real",
    }

    monkeypatch.setattr(
        service.mattermost,
        "post_message",
        lambda channel, subject, body: (
            f"routed:{channel}:{subject}:{body}"
        ),
    )
    assert service._send_message(
        provider,
        "mattermost:channel-7",
        "Deploy",
        "Healthy",
    ) == "routed:mattermost:channel-7:Deploy:Healthy"
