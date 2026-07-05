"""Hybrid substrate (governed pipeline × micro-VM): the pure pieces — guest engine/env
mapping and route-name resolution. The full orchestration is validated live (each working
stage in its own VM, git as the state carrier, judgment host-side)."""
from apps.api.app.config import Settings
from apps.api.app.services.governed_code_task_service import _guest_engine_fields


def _settings(**over):
    base = dict(
        OPENAI_API_KEY="sk-platform", OPENAI_BASE_URL="",
        OPENROUTER_API_KEY="sk-or", GROQ_API_KEY="gsk-groq", DEEPSEEK_API_KEY="sk-ds",
        CLAUDE_CODE_OAUTH_TOKEN="oauth-tok",
    )
    base.update(over)
    return Settings(**base)


def test_echo_lane_has_no_creds():
    assert _guest_engine_fields(_settings(), "echo", None) == {"engine": "echo"}


def test_claude_code_lane_rides_oauth_token():
    fields = _guest_engine_fields(_settings(), "claude_code", None)
    assert fields == {"engine": "claude_code", "claude_oauth_token": "oauth-tok"}


def test_provider_lanes_map_to_guest_openai_with_lane_creds():
    s = _settings()
    for lane, key, url in (
        ("openrouter", "sk-or", "https://openrouter.ai/api/v1"),
        ("groq", "gsk-groq", "https://api.groq.com/openai/v1"),
        ("deepseek", "sk-ds", "https://api.deepseek.com/v1"),
    ):
        fields = _guest_engine_fields(s, lane, "some/model")
        assert fields["engine"] == "openai", lane
        assert fields["openai_api_key"] == key, lane
        assert fields["openai_base_url"] == url, lane
        assert fields["model"] == "some/model", lane
        # the platform OPENAI key must never leak into a lane's guest env
        assert fields["openai_api_key"] != "sk-platform", lane


def test_platform_lane_falls_back_to_openai_model_default():
    fields = _guest_engine_fields(_settings(), "native", None)
    assert fields["engine"] == "openai"
    assert fields["openai_api_key"] == "sk-platform"
    assert fields["model"]  # settings default model, never empty
