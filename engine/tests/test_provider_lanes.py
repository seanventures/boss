"""Local / open-model provider lanes (Plane #297). No DB, no network."""
from __future__ import annotations

import pytest

from boss_engine.config import Settings
from boss_engine.services.code_agent import _PROVIDER_LANES, _build_code_agent


def test_local_lanes_registered():
    assert _PROVIDER_LANES["ollama"] == ("ollama_api_key", "ollama_base_url")
    assert _PROVIDER_LANES["vllm"] == ("vllm_api_key", "vllm_base_url")


def test_ollama_lane_binds_local_daemon(monkeypatch):
    monkeypatch.delenv("OLLAMA_BASE_URL", raising=False)
    monkeypatch.delenv("OLLAMA_API_KEY", raising=False)
    settings = Settings()
    agent = _build_code_agent(settings, "ollama")
    assert agent.engine == "ollama"
    assert agent.settings.openai_base_url == "http://localhost:11434/v1"
    assert agent.settings.openai_api_key == "ollama"
    # the lane must never inherit Azure precedence
    assert not agent.settings.azure_openai_key


def test_vllm_lane_requires_base_url(monkeypatch):
    monkeypatch.delenv("VLLM_BASE_URL", raising=False)
    settings = Settings()
    with pytest.raises(RuntimeError, match="VLLM_BASE_URL"):
        _build_code_agent(settings, "vllm")


def test_vllm_lane_binds_configured_endpoint(monkeypatch):
    monkeypatch.setenv("VLLM_BASE_URL", "http://gpubox:8000/v1")
    settings = Settings()
    agent = _build_code_agent(settings, "vllm")
    assert agent.engine == "vllm"
    assert agent.settings.openai_base_url == "http://gpubox:8000/v1"
