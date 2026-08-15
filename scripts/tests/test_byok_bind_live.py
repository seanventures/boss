"""Unit tests for scripts/qa/byok_bind_live.py (no network, no live keys)."""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
MODULE_PATH = ROOT / "scripts" / "qa" / "byok_bind_live.py"


def _load_module():
    spec = importlib.util.spec_from_file_location("byok_bind_live", MODULE_PATH)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


bind = _load_module()

SECRET = "sk-test-never-print-this-value-xyz"
GROQ_SECRET = "gsk_test-never-print-this-groq-value"
LAST4 = SECRET[-4:]


def test_discover_prefers_openai_then_groq():
    source = bind.discover_source(
        {
            "OPENAI_API_KEY": SECRET,
            "GROQ_API_KEY": GROQ_SECRET,
        }
    )
    assert source.env_name == "OPENAI_API_KEY"
    assert source.present is True
    assert source.provider == "openai"
    assert source.secret_value == SECRET

    groq_only = bind.discover_source({"GROQ_API_KEY": GROQ_SECRET})
    assert groq_only.env_name == "GROQ_API_KEY"
    assert groq_only.provider == "openai_compatible"
    assert groq_only.base_url == bind.DEFAULT_GROQ_BASE_URL

    alias = bind.discover_source({"GROQ": GROQ_SECRET})
    assert alias.env_name == "GROQ"
    assert alias.provider == "openai_compatible"


def test_openai_custom_base_url_uses_compatible_provider():
    source = bind.discover_source(
        {
            "OPENAI_API_KEY": SECRET,
            "OPENAI_BASE_URL": "https://models.example/v1",
        }
    )
    assert source.provider == "openai_compatible"
    assert source.base_url == "https://models.example/v1"


def test_official_openai_base_url_stays_openai():
    source = bind.discover_source(
        {
            "OPENAI_API_KEY": SECRET,
            "OPENAI_BASE_URL": "https://api.openai.com/v1",
        }
    )
    assert source.provider == "openai"
    assert source.base_url is None


def test_discover_missing_source():
    source = bind.discover_source({})
    assert source.present is False
    assert source.env_name is None
    assert source.secret_value == ""


def test_dry_run_plan_never_includes_secret_or_last4():
    source = bind.discover_source({"OPENAI_API_KEY": SECRET})
    plan = bind.dry_run_plan(
        source,
        api_url="https://boss-api.local.sean.network",
        project_id="boss-dogfood",
        actor="executive_console",
    )
    dumped = json.dumps(plan)
    assert SECRET not in dumped
    assert LAST4 not in dumped
    assert "secret_value" not in dumped
    assert plan["dry_run"] is True
    assert plan["source_env"] == "OPENAI_API_KEY"
    assert plan["source_present"] is True
    assert plan["provider"] == "openai"
    assert plan["backend"] == "local_vault"
    assert plan["endpoint"] == "POST /v1/providers/easy-connect"
    assert plan["secret_ref_id"] is None


def test_public_success_prints_only_secret_ref_and_provider():
    payload = {
        "secret_ref_id": "sref_abc123",
        "provider_account": {"provider": "openai", "id": "pa_1"},
        "secret": {"secret_ref_id": "sref_abc123", "last4": LAST4},
        "last4": LAST4,
        "raw_secret_stored": False,
    }
    public = bind.public_success(payload, secrets=[SECRET])
    assert public == {"secret_ref_id": "sref_abc123", "provider": "openai"}
    dumped = json.dumps(public)
    assert LAST4 not in dumped
    assert SECRET not in dumped
    assert "last4" not in dumped


def test_sanitize_drops_identifying_fields():
    payload = {
        "secret_ref_id": "sref_1",
        "secret": {"last4": LAST4, "fingerprint": "abcd", "value": SECRET},
        "detail": f"echo {SECRET} last4={LAST4}",
    }
    cleaned = bind.sanitize_payload(payload, secrets=[SECRET])
    dumped = json.dumps(cleaned)
    assert SECRET not in dumped
    assert LAST4 not in dumped
    assert "last4" not in dumped
    assert "fingerprint" not in dumped
    assert "value" not in dumped


def test_extract_blocker_flags_from_kms_503():
    flags = bind.extract_blocker_flags(
        503,
        {
            "detail": (
                "local_vault requires BOSS_KMS_MASTER_KEY or "
                "BOSS_KMS_MASTER_KEY_FILE "
                "(or BOSS_KMS_ALLOW_EPHEMERAL_MASTER=true for tests only)"
            )
        },
    )
    assert "BOSS_KMS_MASTER_KEY" in flags
    assert "BOSS_KMS_MASTER_KEY_FILE" in flags
    assert "BOSS_KMS_ALLOW_EPHEMERAL_MASTER" in flags
    assert "local_vault" in flags


def test_extract_blocker_flags_from_schema_503():
    flags = bind.extract_blocker_flags(
        503,
        {"detail": 'Required database relation is not applied on this runtime.'},
    )
    assert "secret_refs" in flags
    assert "provider_accounts" in flags


def test_extract_blocker_flags_from_422():
    flags = bind.extract_blocker_flags(
        422,
        {"detail": "Community paste-and-connect uses local_vault only"},
    )
    assert "local_vault" in flags
    assert "ProviderEasyConnectRequest" in flags


def test_write_blocker_doc_uses_flag_names_only(tmp_path):
    path = tmp_path / "BYOK_BIND_BLOCKER.md"
    bind.write_blocker_doc(
        path,
        status=503,
        flags=["BOSS_KMS_MASTER_KEY", "secret_refs", "local_vault"],
        endpoint="POST /v1/providers/easy-connect",
        provider="openai",
    )
    text = path.read_text(encoding="utf-8")
    assert "BOSS_KMS_MASTER_KEY" in text
    assert "secret_refs" in text
    assert "local_vault" in text
    assert SECRET not in text
    assert LAST4 not in text
    assert "last4" not in text.lower()
    assert "Not GA" in text


def test_build_request_body_includes_secret_only_in_memory():
    source = bind.discover_source({"GROQ_API_KEY": GROQ_SECRET})
    body = bind.build_request_body(
        source,
        project_id="boss-dogfood",
        actor="executive_console",
        account_label=None,
    )
    assert body["secret_value"] == GROQ_SECRET
    assert body["provider"] == "openai_compatible"
    assert body["backend"] == "local_vault"
    assert body["base_url"] == bind.DEFAULT_GROQ_BASE_URL
    assert body["project_id"] == "boss-dogfood"
    public = bind.dry_run_plan(
        source, api_url="http://api", project_id="boss-dogfood", actor="ceo"
    )
    assert "secret_value" not in json.dumps(public)


def test_redact_never_leaks_key_or_last4():
    text = f"posted {SECRET} fingerprint last4={LAST4}"
    redacted = bind.redact_secrets(text, [SECRET])
    assert SECRET not in redacted
    assert LAST4 not in redacted


def test_cli_dry_run_default_exits_zero(monkeypatch, capsys):
    monkeypatch.setenv("OPENAI_API_KEY", SECRET)
    monkeypatch.delenv("GROQ_API_KEY", raising=False)
    monkeypatch.delenv("GROQ", raising=False)
    code = bind.main(["--project-id", "boss-dogfood"])
    assert code == 0
    out = capsys.readouterr().out
    assert SECRET not in out
    assert LAST4 not in out
    payload = json.loads(out)
    assert payload["dry_run"] is True
    assert payload["provider"] == "openai"
    assert payload["source_env"] == "OPENAI_API_KEY"


def test_cli_apply_without_source_exits_config_error(monkeypatch, capsys):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("GROQ_API_KEY", raising=False)
    monkeypatch.delenv("GROQ", raising=False)
    code = bind.main(["--apply"])
    assert code == 2
    err = capsys.readouterr().err
    assert "OPENAI_API_KEY" in err or "GROQ" in err
    assert SECRET not in err
