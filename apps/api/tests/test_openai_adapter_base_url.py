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


def test_provider_lane_engines_bind_dedicated_credentials(monkeypatch):
    """VAG cheap-model lanes: openrouter/groq/deepseek engines use their OWN credential
    pair — never the platform-managed OPENAI_* pair, and Azure precedence is disabled."""
    import pytest

    from apps.api.app.config import Settings
    from apps.api.app.services.code_agent import make_code_agent

    lanes = [
        ("openrouter", "OPENROUTER_API_KEY", "https://openrouter.ai/api/v1"),
        ("groq", "GROQ_API_KEY", "https://api.groq.com/openai/v1"),
        ("deepseek", "DEEPSEEK_API_KEY", "https://api.deepseek.com/v1"),
    ]
    for env in ("OPENROUTER_API_KEY", "OPENROUTER_BASE_URL", "GROQ_API_KEY",
                "GROQ_BASE_URL", "DEEPSEEK_API_KEY", "DEEPSEEK_BASE_URL"):
        monkeypatch.delenv(env, raising=False)
    for engine_name, key_alias, default_url in lanes:
        settings = Settings(**{
            key_alias: f"sk-{engine_name}-test",
            "OPENAI_API_KEY": "sk-platform-DO-NOT-USE",
            "AZURE_OPENAI_KEY": "azkey",
            "AZURE_OPENAI_ENDPOINT": "https://x.azure.com",
            "AZURE_OPENAI_DEPLOYMENT": "gpt-4o",
        })
        agent = make_code_agent(settings, engine=engine_name)
        assert agent.engine == engine_name
        bound = agent.settings
        assert bound.openai_api_key == f"sk-{engine_name}-test", engine_name
        assert bound.openai_base_url == default_url, engine_name
        assert not (bound.azure_openai_key or bound.azure_openai_deployment), engine_name
        # the original settings object is untouched (platform lane unaffected)
        assert settings.openai_api_key == "sk-platform-DO-NOT-USE"

        with pytest.raises(RuntimeError, match=key_alias):
            make_code_agent(Settings(**{key_alias: ""}), engine=engine_name)


def test_native_max_output_tokens_cap_passed(monkeypatch):
    """Metered gateways reserve credits for the requested max — the adapter caps it."""
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
    adapter = OpenAIAdapter(_settings(native_max_output_tokens=8192))
    adapter.create_chat_completion([{"role": "user", "content": "hi"}])
    assert captured["max_tokens"] == 8192
    captured.clear()
    adapter = OpenAIAdapter(_settings(native_max_output_tokens=0))  # 0 disables
    adapter.create_chat_completion([{"role": "user", "content": "hi"}])
    assert "max_tokens" not in captured


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
