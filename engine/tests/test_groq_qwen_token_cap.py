"""Groq/Qwen request-size caps — keep a 6k TPM lane from asking for 12k tokens."""

from types import SimpleNamespace

from boss_engine.services.openai_adapter import (
    GROQ_QWEN_CONTEXT_FILES,
    GROQ_QWEN_CONTEXT_HEAD_BYTES,
    GROQ_QWEN_CONTEXT_MAX_BYTES,
    GROQ_QWEN_MAX_OUTPUT_TOKENS,
    OpenAIAdapter,
    capped_max_output_tokens,
    groq_qwen_context_limits,
    is_groq_qwen_model_class,
)


def test_groq_qwen_model_class_detection():
    assert is_groq_qwen_model_class("qwen/qwen3-32b")
    assert is_groq_qwen_model_class("openai/gpt-oss", base_url="https://api.groq.com/openai/v1")
    assert is_groq_qwen_model_class("gpt-4o", engine="groq")
    assert is_groq_qwen_model_class("groq:qwen/qwen3-32b")
    assert not is_groq_qwen_model_class("gpt-4o", base_url="https://api.openai.com/v1")
    assert not is_groq_qwen_model_class("claude-sonnet-4", engine="claude_code")


def test_capped_max_output_tokens_for_groq_qwen():
    assert GROQ_QWEN_MAX_OUTPUT_TOKENS <= 2048
    assert capped_max_output_tokens(8192, "groq:qwen/qwen3-32b") == GROQ_QWEN_MAX_OUTPUT_TOKENS
    assert capped_max_output_tokens(8192, "qwen/qwen3-32b") == GROQ_QWEN_MAX_OUTPUT_TOKENS
    assert capped_max_output_tokens(512, "qwen/qwen3-32b") == 512
    assert capped_max_output_tokens(8192, "gpt-4o") == 8192
    assert capped_max_output_tokens(0, "qwen/qwen3-32b") == 0


def test_groq_qwen_context_pack_is_smaller_than_default():
    files, max_bytes, head = groq_qwen_context_limits("qwen/qwen3-32b", file_limit=14)
    assert files == GROQ_QWEN_CONTEXT_FILES <= 4
    assert max_bytes == GROQ_QWEN_CONTEXT_MAX_BYTES
    assert head == GROQ_QWEN_CONTEXT_HEAD_BYTES
    default_files, default_bytes, _ = groq_qwen_context_limits("gpt-4o", file_limit=14)
    assert default_files == 14
    assert default_bytes > max_bytes
    assert groq_qwen_context_limits("qwen", file_limit=0)[0] == 0


def test_adapter_passes_capped_max_tokens(monkeypatch):
    captured: dict = {}

    class _FakeOpenAI:
        def __init__(self, **kwargs):
            msg = SimpleNamespace(
                content="{}",
                tool_calls=None,
                model_dump=lambda exclude_none=True: {},
            )
            self.chat = SimpleNamespace(
                completions=SimpleNamespace(
                    create=lambda **kw: captured.update(kw)
                    or SimpleNamespace(
                        id="r1",
                        choices=[SimpleNamespace(message=msg)],
                        usage=SimpleNamespace(
                            prompt_tokens=1, completion_tokens=1, total_tokens=2
                        ),
                    )
                )
            )

    monkeypatch.setattr("boss_engine.services.openai_adapter.OpenAI", _FakeOpenAI)
    settings = SimpleNamespace(
        openai_api_key="sk-test",
        openai_base_url="https://api.groq.com/openai/v1",
        openai_model="qwen/qwen3-32b",
        native_max_output_tokens=8192,
        azure_openai_key=None,
        azure_openai_endpoint=None,
        azure_openai_deployment=None,
        code_agent_engine="groq",
    )
    OpenAIAdapter(settings).create_chat_completion([{"role": "user", "content": "hi"}])
    assert captured["max_tokens"] == GROQ_QWEN_MAX_OUTPUT_TOKENS
    assert captured["max_tokens"] < 6000
