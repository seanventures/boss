"""Unit tests for AWS Bedrock adapter + bare-metal instance helpers.

All network/boto3 calls are mocked — no live AWS traffic.
"""

from __future__ import annotations

import pytest

from apps.api.app.config import Settings
from apps.api.app.services.aws_bedrock_adapter import AwsBedrockAdapter
from apps.api.app.services.aws_metal import (
    is_bare_metal_instance_type,
    recommend_metal_types,
)
from apps.api.app.services.github_adapter import IntegrationConfigurationError


class FakeBedrockClient:
    """Records boto3-style method calls and replays canned responses."""

    def __init__(self, responses: dict | None = None):
        self.responses = responses or {}
        self.calls: list[tuple[str, dict]] = []

    def __getattr__(self, method_name: str):
        def _call(**kwargs):
            self.calls.append((method_name, kwargs))
            value = self.responses.get(method_name)
            if isinstance(value, Exception):
                raise value
            if callable(value):
                return value(**kwargs)
            return value or {}

        return _call

    def calls_for(self, method_name: str) -> list[dict]:
        return [kwargs for name, kwargs in self.calls if name == method_name]


def _settings(**over) -> Settings:
    base = {
        "BOSS_AWS_ACCESS_KEY_ID": "AKIATESTKEYID01",
        "BOSS_AWS_REGION": "us-east-1",
    }
    base.update(over)
    return Settings(**base)


def _adapter(**over) -> AwsBedrockAdapter:
    return AwsBedrockAdapter(_settings(**over))


def _install_client(
    monkeypatch,
    adapter: AwsBedrockAdapter,
    clients: dict[str, FakeBedrockClient],
) -> None:
    def fake_client(
        service_name,
        access_key_id=None,
        secret_access_key=None,
        region=None,
        session_token=None,
    ):
        # Ensure credentials were resolved before client construction.
        assert access_key_id or adapter.settings.aws_access_key_id
        if service_name not in clients:
            raise AssertionError(f"Unexpected Bedrock client: {service_name}")
        return clients[service_name]

    monkeypatch.setattr(adapter, "_client", fake_client)


# ---------------------------------------------------------------------------
# Credentials
# ---------------------------------------------------------------------------


def test_missing_access_key_raises(monkeypatch):
    monkeypatch.delenv("BOSS_AWS_ACCESS_KEY_ID", raising=False)
    monkeypatch.delenv("BOSS_AWS_SECRET_ACCESS_KEY", raising=False)
    adapter = AwsBedrockAdapter(Settings(BOSS_AWS_ACCESS_KEY_ID=None))
    with pytest.raises(IntegrationConfigurationError, match="ACCESS_KEY_ID"):
        adapter._resolve_credentials(secret_access_key="secret")


def test_missing_secret_raises(monkeypatch):
    monkeypatch.delenv("BOSS_AWS_SECRET_ACCESS_KEY", raising=False)
    adapter = _adapter()
    with pytest.raises(IntegrationConfigurationError, match="SECRET_ACCESS_KEY"):
        adapter._resolve_credentials(access_key_id="AKIATEST")


def test_explicit_creds_override_settings(monkeypatch):
    monkeypatch.delenv("BOSS_AWS_SECRET_ACCESS_KEY", raising=False)
    adapter = _adapter()
    key, secret, region, token = adapter._resolve_credentials(
        access_key_id="AKIEXPLICIT",
        secret_access_key="explicit-secret",
        region="eu-west-1",
        session_token="tok",
    )
    assert key == "AKIEXPLICIT"
    assert secret == "explicit-secret"
    assert region == "eu-west-1"
    assert token == "tok"


# ---------------------------------------------------------------------------
# Converse
# ---------------------------------------------------------------------------


def test_converse_returns_content_usage_and_provider_ref(monkeypatch):
    adapter = _adapter()
    runtime = FakeBedrockClient(
        {
            "converse": {
                "output": {
                    "message": {
                        "role": "assistant",
                        "content": [{"text": "Hello from Bedrock"}],
                    }
                },
                "usage": {"inputTokens": 12, "outputTokens": 4},
                "stopReason": "end_turn",
                "ResponseMetadata": {"RequestId": "req-abc-123"},
            }
        }
    )
    _install_client(monkeypatch, adapter, {"bedrock-runtime": runtime})

    result = adapter.converse(
        model_id="anthropic.claude-3-haiku-20240307-v1:0",
        messages=[{"role": "user", "content": "hi"}],
        max_tokens=256,
        temperature=0.2,
        system="You are helpful.",
        secret_access_key="test-secret",
    )

    assert result["content"] == "Hello from Bedrock"
    assert result["usage"]["input_tokens"] == 12
    assert result["usage"]["output_tokens"] == 4
    assert result["usage"]["total_tokens"] == 16
    assert result["model_id"] == "anthropic.claude-3-haiku-20240307-v1:0"
    assert result["provider_ref"] == "req-abc-123"
    assert result["stop_reason"] == "end_turn"
    # Secrets never appear in the response payload.
    dumped = str(result)
    assert "test-secret" not in dumped
    assert "SecretAccessKey" not in dumped

    calls = runtime.calls_for("converse")
    assert len(calls) == 1
    payload = calls[0]
    assert payload["modelId"] == "anthropic.claude-3-haiku-20240307-v1:0"
    assert payload["messages"] == [
        {"role": "user", "content": [{"text": "hi"}]}
    ]
    assert payload["inferenceConfig"]["maxTokens"] == 256
    assert payload["inferenceConfig"]["temperature"] == 0.2
    assert payload["system"] == [{"text": "You are helpful."}]


def test_converse_requires_model_and_messages(monkeypatch):
    adapter = _adapter()
    runtime = FakeBedrockClient({"converse": {}})
    _install_client(monkeypatch, adapter, {"bedrock-runtime": runtime})

    with pytest.raises(IntegrationConfigurationError, match="model_id"):
        adapter.converse(
            model_id="",
            messages=[{"role": "user", "content": "x"}],
            secret_access_key="s",
        )
    with pytest.raises(IntegrationConfigurationError, match="messages"):
        adapter.converse(
            model_id="anthropic.claude-3-haiku-20240307-v1:0",
            messages=[],
            secret_access_key="s",
        )


def test_converse_propagates_provider_errors(monkeypatch):
    adapter = _adapter()
    runtime = FakeBedrockClient(
        {"converse": RuntimeError("AccessDeniedException: not authorized")}
    )
    _install_client(monkeypatch, adapter, {"bedrock-runtime": runtime})

    with pytest.raises(IntegrationConfigurationError, match="Converse failed"):
        adapter.converse(
            model_id="anthropic.claude-3-haiku-20240307-v1:0",
            messages=[{"role": "user", "content": "hi"}],
            secret_access_key="s",
        )


# ---------------------------------------------------------------------------
# List foundation models
# ---------------------------------------------------------------------------


def test_list_foundation_models_maps_summaries(monkeypatch):
    adapter = _adapter()
    control = FakeBedrockClient(
        {
            "list_foundation_models": {
                "modelSummaries": [
                    {
                        "modelId": "anthropic.claude-3-haiku-20240307-v1:0",
                        "modelName": "Claude 3 Haiku",
                        "providerName": "Anthropic",
                        "inputModalities": ["TEXT"],
                        "outputModalities": ["TEXT"],
                        "inferenceTypesSupported": ["ON_DEMAND"],
                        "responseStreamingSupported": True,
                    }
                ]
            }
        }
    )
    _install_client(monkeypatch, adapter, {"bedrock": control})

    models = adapter.list_foundation_models(
        by_provider="Anthropic",
        secret_access_key="s",
    )
    assert len(models) == 1
    assert models[0]["model_id"] == "anthropic.claude-3-haiku-20240307-v1:0"
    assert models[0]["provider_name"] == "Anthropic"
    assert models[0]["input_modalities"] == ["TEXT"]
    assert control.calls_for("list_foundation_models") == [
        {"byProvider": "Anthropic"}
    ]


# ---------------------------------------------------------------------------
# Cost estimation
# ---------------------------------------------------------------------------


def test_estimate_cost_positive_for_known_models():
    adapter = _adapter()
    cost = adapter.estimate_cost_usd_micros(
        "anthropic.claude-3-5-sonnet-20241022-v2:0",
        input_tokens=1_000_000,
        output_tokens=1_000_000,
    )
    # 3.00 + 15.00 = 18.00 USD → 18 micros? Wait: price table is $/Mtok and the
    # openai_adapter convention multiplies tokens * rate directly yielding micros
    # only when rates are already scaled — same as openai: 1M tokens * $3 = 3_000_000
    # "micros" in that table's units (dollar-micros of the rate*token product).
    assert cost > 0
    # 1_000_000 * 3.00 + 1_000_000 * 15.00 = 18_000_000
    assert cost == 18_000_000


def test_estimate_cost_haiku_cheaper_than_sonnet():
    adapter = _adapter()
    haiku = adapter.estimate_cost_usd_micros(
        "anthropic.claude-3-haiku-20240307-v1:0", 1000, 1000
    )
    sonnet = adapter.estimate_cost_usd_micros(
        "anthropic.claude-3-5-sonnet-20241022-v2:0", 1000, 1000
    )
    assert haiku > 0
    assert sonnet > haiku


def test_estimate_cost_unknown_model_still_positive():
    adapter = _adapter()
    cost = adapter.estimate_cost_usd_micros("some.unknown-model-v1", 100, 50)
    assert cost > 0


# ---------------------------------------------------------------------------
# Bare metal helpers
# ---------------------------------------------------------------------------


def test_is_bare_metal_instance_type():
    assert is_bare_metal_instance_type("m5.metal") is True
    assert is_bare_metal_instance_type("c6i.metal") is True
    assert is_bare_metal_instance_type("u-6tb1.metal") is True
    assert is_bare_metal_instance_type("t3.micro") is False
    assert is_bare_metal_instance_type("m5.large") is False
    assert is_bare_metal_instance_type("") is False
    assert is_bare_metal_instance_type("metal-something") is True


def test_recommend_metal_types_default_and_region():
    default = recommend_metal_types()
    assert isinstance(default, list)
    assert len(default) >= 1
    assert all(is_bare_metal_instance_type(t) for t in default)

    us = recommend_metal_types("us-east-1")
    assert "m5.metal" in us
    assert all(is_bare_metal_instance_type(t) for t in us)


def test_client_requires_boto3(monkeypatch):
    """If boto3 cannot be imported, raise a clear configuration error."""
    import builtins

    adapter = _adapter()
    real_import = builtins.__import__

    def fake_import(name, *args, **kwargs):
        if name == "boto3":
            raise ImportError("No module named boto3")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fake_import)
    with pytest.raises(IntegrationConfigurationError, match="boto3"):
        adapter._client(
            "bedrock-runtime",
            access_key_id="AKIATEST",
            secret_access_key="secret",
            region="us-east-1",
        )


