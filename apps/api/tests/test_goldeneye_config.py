from types import SimpleNamespace

from apps.api.app.config import Settings
from apps.api.app.services import openai_adapter
from apps.api.app.services.openai_adapter import OpenAIAdapter


def test_settings_accepts_goldeneye_injected_env_aliases(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql+psycopg://boss:boss@goldeneye/boss")
    monkeypatch.setenv("OPENAI_API_KEY", "goldeneye-llm-key")
    monkeypatch.setenv("OPENAI_BASE_URL", "https://llm.local.sean.network/v1")
    monkeypatch.setenv("OPENAI_MODEL", "llama-3.3-70b-versatile")
    monkeypatch.setenv("SMTP_HOST", "smtp.local.sean.network")
    monkeypatch.setenv("SMTP_PORT", "2525")
    monkeypatch.setenv("MAIL_FROM", "boss@sean.network")
    monkeypatch.setenv("OIDC_ISSUER", "https://keycloak.local.sean.network/realms/boss")
    monkeypatch.setenv("OIDC_CLIENT_ID", "boss")
    monkeypatch.setenv("OIDC_CLIENT_SECRET", "secret-ref")
    monkeypatch.setenv("KEYCLOAK_URL", "https://keycloak.local.sean.network")
    monkeypatch.setenv("KEYCLOAK_REALM", "boss")
    monkeypatch.setenv("ADMIN_USERNAME", "owner")
    monkeypatch.setenv("ADMIN_EMAIL", "owner@example.test")
    monkeypatch.setenv("ADMIN_PASSWORD", "vault-generated")

    settings = Settings(_env_file=None)

    assert settings.database_url == "postgresql+psycopg://boss:boss@goldeneye/boss"
    assert settings.openai_api_key == "goldeneye-llm-key"
    assert settings.openai_base_url == "https://llm.local.sean.network/v1"
    assert settings.openai_model == "llama-3.3-70b-versatile"
    assert settings.email_smtp_host == "smtp.local.sean.network"
    assert settings.email_smtp_port == 2525
    assert settings.email_from_address == "boss@sean.network"
    assert settings.oidc_issuer == "https://keycloak.local.sean.network/realms/boss"
    assert settings.oidc_audience == "boss"
    assert settings.oidc_client_secret == "secret-ref"
    assert settings.keycloak_url == "https://keycloak.local.sean.network"
    assert settings.keycloak_realm == "boss"
    assert settings.admin_username == "owner"
    assert settings.admin_email == "owner@example.test"
    assert settings.admin_password == "vault-generated"


def test_goldeneye_env_aliases_win_over_blank_boss_compatibility_aliases(monkeypatch):
    monkeypatch.setenv("OPENAI_MODEL", "llama-3.3-70b-versatile")
    monkeypatch.setenv("BOSS_OPENAI_MODEL", "")
    monkeypatch.setenv("SMTP_HOST", "smtp.local.sean.network")
    monkeypatch.setenv("BOSS_EMAIL_SMTP_HOST", "")
    monkeypatch.setenv("SMTP_PORT", "2525")
    monkeypatch.setenv("BOSS_EMAIL_SMTP_PORT", "587")
    monkeypatch.setenv("MAIL_FROM", "boss@sean.network")
    monkeypatch.setenv("BOSS_EMAIL_FROM_ADDRESS", "")
    monkeypatch.setenv("OIDC_ISSUER", "https://keycloak.local.sean.network/realms/boss")
    monkeypatch.setenv("BOSS_OIDC_ISSUER", "")
    monkeypatch.setenv("OIDC_CLIENT_ID", "boss")
    monkeypatch.setenv("BOSS_OIDC_AUDIENCE", "")

    settings = Settings(_env_file=None)

    assert settings.openai_model == "llama-3.3-70b-versatile"
    assert settings.email_smtp_host == "smtp.local.sean.network"
    assert settings.email_smtp_port == 2525
    assert settings.email_from_address == "boss@sean.network"
    assert settings.oidc_issuer == "https://keycloak.local.sean.network/realms/boss"
    assert settings.oidc_audience == "boss"


def test_openai_compatible_gateway_uses_chat_completions(monkeypatch):
    captured = {}

    class FakeOpenAI:
        def __init__(self, **kwargs):
            captured["client_kwargs"] = kwargs
            self.chat = SimpleNamespace(
                completions=SimpleNamespace(create=self._create_chat_completion)
            )

        def _create_chat_completion(self, **kwargs):
            captured["chat_kwargs"] = kwargs
            message = SimpleNamespace(content="planned")
            choice = SimpleNamespace(message=message)
            usage = SimpleNamespace(prompt_tokens=10, completion_tokens=2, total_tokens=12)
            return SimpleNamespace(id="chatcmpl_test", choices=[choice], usage=usage)

    monkeypatch.setattr(openai_adapter, "OpenAI", FakeOpenAI)
    settings = Settings(
        _env_file=None,
        OPENAI_API_KEY="goldeneye-llm-key",
        OPENAI_BASE_URL="https://llm.local.sean.network/v1",
        OPENAI_MODEL="llama-3.3-70b-versatile",
    )

    response_id, text, usage = OpenAIAdapter(settings).create_text_response(
        "Draft a plan.",
        system_prompt="You are BOSS.",
    )

    assert response_id == "chatcmpl_test"
    assert text == "planned"
    assert usage["model"] == "llama-3.3-70b-versatile"
    assert captured["client_kwargs"] == {
        "api_key": "goldeneye-llm-key",
        "base_url": "https://llm.local.sean.network/v1",
    }
    assert captured["chat_kwargs"]["model"] == "llama-3.3-70b-versatile"
    assert captured["chat_kwargs"]["messages"] == [
        {"role": "system", "content": "You are BOSS."},
        {"role": "user", "content": "Draft a plan."},
    ]
