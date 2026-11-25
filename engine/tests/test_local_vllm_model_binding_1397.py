"""BOSSAPP-1397: Local > Other URL (vllm/ollama) connects without an API key, binds its
connected base_url/model for host & guest builds, and explains missing config plainly."""
from __future__ import annotations

import json
from datetime import datetime, timezone

from sqlalchemy import create_engine, text

from boss_engine.config import Settings
from boss_engine.services import build_failure, model_failures
from boss_engine.services.code_agent import _bind_lane_credentials, _build_code_agent
from boss_engine.services.governed_code_task_service import _guest_engine_fields


def _seed_connected_local(entitlement_tables, *, source_id: str, provider: str, base_url: str, model: str):
    db = create_engine("sqlite+pysqlite:///:memory:")
    with db.begin() as c:
        entitlement_tables(c, with_settings=False)
        c.execute(
            text(
                "INSERT INTO entitlement_sources ("
                "  project_id, source_id, provider, connection_status, credential_secret_ref_id,"
                "  supported_models, connection_metadata, updated_at"
                ") VALUES ("
                "  :p, :sid, :prov, 'connected', NULL, :models, :meta, :at"
                ")"
            ),
            {
                "p": "p1397",
                "sid": source_id,
                "prov": provider,
                "models": json.dumps([model]),
                "meta": json.dumps({"base_url": base_url, "model": model}),
                "at": datetime(2026, 11, 25, tzinfo=timezone.utc),
            },
        )
    return db


def test_connected_keyless_vllm_binds_base_url_and_model_for_host_and_guest(entitlement_tables, monkeypatch):
    monkeypatch.delenv("VLLM_BASE_URL", raising=False)
    monkeypatch.delenv("VLLM_API_KEY", raising=False)
    db = _seed_connected_local(
        entitlement_tables,
        source_id="setup-vllm",
        provider="vllm",
        base_url="http://gpubox:8000/v1",
        model="Qwen/Qwen2.5-Coder-32B",
    )
    settings = Settings(_env_file=None)
    assert settings.vllm_base_url == ""

    with db.begin() as c:
        bound = _bind_lane_credentials(settings, "vllm", connection=c, project_id="p1397")
        guest = _guest_engine_fields(settings, "vllm", None, connection=c, project_id="p1397")

    assert bound.vllm_base_url == "http://gpubox:8000/v1"
    assert bound.openai_model == "Qwen/Qwen2.5-Coder-32B"
    agent = _build_code_agent(bound, "vllm")
    assert agent.engine == "vllm"
    assert agent.settings.openai_base_url == "http://gpubox:8000/v1"
    assert agent.settings.openai_model == "Qwen/Qwen2.5-Coder-32B"

    assert guest["engine"] == "openai"
    assert guest["openai_base_url"] == "http://gpubox:8000/v1"
    assert guest["model"] == "Qwen/Qwen2.5-Coder-32B"


def test_connected_keyless_ollama_uses_custom_url_and_model(entitlement_tables, monkeypatch):
    monkeypatch.delenv("OLLAMA_BASE_URL", raising=False)
    db = _seed_connected_local(
        entitlement_tables,
        source_id="setup-ollama",
        provider="ollama",
        base_url="http://mac-studio:11434/v1",
        model="qwen3:32b",
    )
    settings = Settings(_env_file=None)
    with db.begin() as c:
        bound = _bind_lane_credentials(settings, "ollama", connection=c, project_id="p1397")
        guest = _guest_engine_fields(settings, "ollama", None, connection=c, project_id="p1397")

    assert bound.ollama_base_url == "http://mac-studio:11434/v1"
    assert bound.openai_model == "qwen3:32b"
    assert guest["openai_base_url"] == "http://mac-studio:11434/v1"
    assert guest["model"] == "qwen3:32b"


def test_unconfigured_vllm_error_explains_plainly_without_env_var_or_retry():
    raw = "VLLM_BASE_URL is not configured."
    action = build_failure.action_for(
        "proj_1",
        source="code_task",
        run_id="crun_1",
        failure_class="needs_human",
        reason_code="model_not_configured",
        error=raw,
    )
    assert action["retry"] is None
    assert action["fix"]["href"] == "?project_id=proj_1&area=setup&focus=models"
    assert "Setup" in action["fix"]["label"] and "Models" in action["fix"]["label"]
    assert "VLLM_BASE_URL" not in action["fix"]["detail"]
    assert "VLLM_BASE_URL" not in (action.get("retry_reason") or "")

    # Even if an old run recorded failure_class="infra" with raw VLLM_BASE_URL text:
    raw_action = build_failure.action_for(
        "proj_1",
        source="code_task",
        run_id="crun_2",
        failure_class="infra",
        reason_code="unknown",
        error=raw,
    )
    assert raw_action["retry"] is None
    assert "VLLM_BASE_URL" not in raw_action["fix"]["detail"]

    said = model_failures.explain(raw)
    assert said["kind"] == "no_model"
    assert said["retry"] is False
    assert "Setup" in said["action"] and "Models" in said["action"]
    assert "VLLM_BASE_URL" not in said["headline"]

    settled = model_failures.settled_failure(raw)
    assert settled is not None
    assert "VLLM_BASE_URL" not in settled
