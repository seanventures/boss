"""VAG B1: the host-side OpenAIAdapter honors OPENAI_BASE_URL, so the native engine can
target any OpenAI-compatible provider (OpenRouter/Fireworks/DeepSeek) like the guest does."""
from types import SimpleNamespace

from apps.api.app.services import openai_adapter
from apps.api.app.services.openai_adapter import OpenAIAdapter


def _settings(**over):
    base = dict(
        openai_api_key="sk-test", openai_base_url="", openai_model="gpt-5.2-codex",
        azure_openai_key=None, azure_openai_endpoint=None, azure_openai_deployment=None,
        azure_openai_api_version="2024-06-01",
    )
    base.update(over)
    return SimpleNamespace(**base)


class _FakeOpenAI:
    """Captures constructor kwargs and returns a canned chat completion."""

    last_kwargs: dict = {}

    def __init__(self, **kwargs):
        _FakeOpenAI.last_kwargs = kwargs
        msg = SimpleNamespace(content="{}", tool_calls=None,
                              model_dump=lambda exclude_none=True: {})
        self.chat = SimpleNamespace(completions=SimpleNamespace(
            create=lambda **kw: SimpleNamespace(
                id="r1", choices=[SimpleNamespace(message=msg)],
                usage=SimpleNamespace(prompt_tokens=1, completion_tokens=1, total_tokens=2),
                _kw=kw,
            )
        ))


def test_empty_base_url_omits_kwarg(monkeypatch):
    monkeypatch.setattr(openai_adapter, "OpenAI", _FakeOpenAI)
    adapter = OpenAIAdapter(_settings())
    adapter.create_chat_completion([{"role": "user", "content": "hi"}])
    assert "base_url" not in _FakeOpenAI.last_kwargs  # SDK default = api.openai.com


def test_base_url_passed_to_client(monkeypatch):
    monkeypatch.setattr(openai_adapter, "OpenAI", _FakeOpenAI)
    adapter = OpenAIAdapter(_settings(openai_base_url="https://openrouter.ai/api/v1"))
    adapter.create_chat_completion([{"role": "user", "content": "hi"}])
    assert _FakeOpenAI.last_kwargs["base_url"] == "https://openrouter.ai/api/v1"


def test_model_override_flows_through(monkeypatch):
    monkeypatch.setattr(openai_adapter, "OpenAI", _FakeOpenAI)
    captured: dict = {}

    class _Capture(_FakeOpenAI):
        def __init__(self, **kwargs):
            super().__init__(**kwargs)
            inner = self.chat.completions.create

            def create(**kw):
                captured.update(kw)
                return inner(**kw)

            self.chat.completions.create = create

    monkeypatch.setattr(openai_adapter, "OpenAI", _Capture)
    adapter = OpenAIAdapter(_settings(openai_base_url="https://openrouter.ai/api/v1"))
    adapter.create_chat_completion([{"role": "user", "content": "hi"}],
                                   model_override="deepseek/deepseek-chat")
    assert captured["model"] == "deepseek/deepseek-chat"


def test_openrouter_engine_binds_dedicated_credentials(monkeypatch):
    """VAG cheap-model lane: engine 'openrouter' uses OPENROUTER_* creds — never the
    platform-managed OPENAI_* pair, and Azure precedence is disabled on its adapter."""
    import pytest

    from apps.api.app.config import Settings
    from apps.api.app.services.code_agent import make_code_agent

    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    monkeypatch.delenv("OPENROUTER_BASE_URL", raising=False)
    settings = Settings(
        OPENROUTER_API_KEY="sk-or-test",
        OPENAI_API_KEY="sk-platform-DO-NOT-USE",
        AZURE_OPENAI_KEY="azkey", AZURE_OPENAI_ENDPOINT="https://x.azure.com",
        AZURE_OPENAI_DEPLOYMENT="gpt-4o",
    )
    agent = make_code_agent(settings, engine="openrouter")
    assert agent.engine == "openrouter"
    bound = agent.settings
    assert bound.openai_api_key == "sk-or-test"
    assert bound.openai_base_url == "https://openrouter.ai/api/v1"
    assert not (bound.azure_openai_key or bound.azure_openai_deployment)  # no Azure precedence
    # the original settings object is untouched (platform lane unaffected)
    assert settings.openai_api_key == "sk-platform-DO-NOT-USE"

    with pytest.raises(RuntimeError, match="OPENROUTER_API_KEY"):
        make_code_agent(Settings(OPENROUTER_API_KEY=""), engine="openrouter")


def test_azure_precedence_ignores_base_url(monkeypatch):
    used = {"azure": False}

    class _FakeAzure(_FakeOpenAI):
        def __init__(self, **kwargs):
            used["azure"] = True
            super().__init__()

    monkeypatch.setattr(openai_adapter, "AzureOpenAI", _FakeAzure)
    monkeypatch.setattr(openai_adapter, "OpenAI", _FakeOpenAI)
    adapter = OpenAIAdapter(_settings(
        openai_base_url="https://openrouter.ai/api/v1",
        azure_openai_key="k", azure_openai_endpoint="https://x.azure.com",
        azure_openai_deployment="gpt-4o",
    ))
    adapter.create_chat_completion([{"role": "user", "content": "hi"}])
    assert used["azure"] is True
