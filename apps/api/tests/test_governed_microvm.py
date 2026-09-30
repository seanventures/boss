"""Hybrid substrate (governed pipeline × micro-VM): the pure pieces — guest engine/env
mapping and route-name resolution. The full orchestration is validated live (each working
stage in its own VM, git as the state carrier, judgment host-side)."""
import pytest

from apps.api.app.config import Settings
from apps.api.app.services.code_agent import NativeCodeAgent, PacedCodeAgent, make_code_agent
from apps.api.app.services.governed_code_task_service import (
    _guest_engine_fields,
    _repair_prompt,
)


def _settings(**over):
    base = dict(
        OPENAI_API_KEY="sk-platform", OPENAI_BASE_URL="",
        OPENROUTER_API_KEY="sk-or", GROQ_API_KEY="gsk-groq", DEEPSEEK_API_KEY="sk-ds",
        CLAUDE_CODE_OAUTH_TOKEN="oauth-tok",
    )
    base.update(over)
    return Settings(**base)


def _azure_settings(**over):
    return _settings(AZURE_OPENAI_KEY="az-key", AZURE_OPENAI_ENDPOINT="https://x.openai.azure.com",
                     AZURE_OPENAI_DEPLOYMENT="gpt-4o", **over)


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


def test_azure_guest_lane_maps_to_azure_fields_with_deployment_override():
    # A routed model (azure:<deploy>) overrides the default deployment; no base_url so the
    # guest adapter can't misroute the Azure lane to a plain-OpenAI host.
    fields = _guest_engine_fields(_azure_settings(), "azure", "my-deploy")
    assert fields["engine"] == "openai"
    assert fields["azure_key"] == "az-key"
    assert fields["azure_endpoint"] == "https://x.openai.azure.com"
    assert fields["azure_deployment"] == "my-deploy"  # routed model wins
    assert "openai_base_url" not in fields
    # no routed model -> the configured default deployment
    assert _guest_engine_fields(_azure_settings(), "azure", None)["azure_deployment"] == "gpt-4o"


def test_make_code_agent_azure_requires_creds():
    # Explicit azure lane fails loud without creds rather than silently using a non-Azure host.
    no_azure = _settings(AZURE_OPENAI_KEY="", AZURE_OPENAI_ENDPOINT="", AZURE_OPENAI_DEPLOYMENT="")
    with pytest.raises(RuntimeError, match="azure engine requires"):
        make_code_agent(no_azure, engine="azure")
    agent = make_code_agent(_azure_settings(), engine="azure")
    # make_code_agent now paces the agent (#111); unwrap to check the azure routing intent.
    inner = agent._inner if isinstance(agent, PacedCodeAgent) else agent
    assert isinstance(inner, NativeCodeAgent) and agent.engine == "azure"


def test_repair_prompt_appends_frozen_bar_and_evidence():
    base = "You are a software engineer. Implement X."
    p = _repair_prompt(base, "$ pytest (exit 1)\nAssertionError: 2 != 3", attempt=2)
    assert base in p                          # keeps the original task
    assert "REPAIR ATTEMPT 2" in p            # numbered so the model sees it is iterating
    assert "FROZEN" in p and "do NOT edit" in p  # the bar never moves
    assert "AssertionError: 2 != 3" in p      # concrete failing evidence carried through
