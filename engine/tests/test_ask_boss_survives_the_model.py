"""Ask BOSS must survive the model, the provider, and its own tool list.

Every failure here was observed in production, not imagined:

  * a Groq 400 killing a whole turn because the MODEL emitted `functions/read_logs`;
  * a question answered with "I drafted 3 sequenced phases";
  * "reached the step limit while working" instead of what had been found.

The common shape is that a recoverable fault in one layer is reported to the operator as a
final answer. The operator cannot act on any of them, and each one costs a whole turn.
"""

import pytest

from openai import APIConnectionError, BadRequestError, RateLimitError

from boss_engine.services.openai_adapter import _with_retry, is_model_output_rejection
from boss_engine.tools.registry import ToolRegistry


def _bad_request(message: str) -> BadRequestError:
    """A BadRequestError carrying `message`, constructed without a live HTTP response."""
    exc = BadRequestError.__new__(BadRequestError)
    Exception.__init__(exc, message)
    return exc


# --------------------------------------------------------------------------- #
# The 400 that was really a generation flake
# --------------------------------------------------------------------------- #
GROQ_REAL = (
    "litellm.BadRequestError: GroqException - {\"error\":{\"message\":\"Tool call validation "
    "failed: tool call validation failed: attempted to call tool 'functions/read_logs' which "
    "was not in request.tools\",\"type\":\"invalid_request_error\",\"code\":\"tool_use_failed\"}}"
)


def test_the_real_groq_error_is_recognised_as_a_model_flake():
    assert is_model_output_rejection(_bad_request(GROQ_REAL)) is True


@pytest.mark.parametrize("message", [
    "Tool call validation failed",
    "attempted to call tool 'x' which was not in request.tools",
    'code: "tool_use_failed"',
    'failed_generation: {"name": "functions/query_db"}',
])
def test_each_signature_is_recognised(message):
    assert is_model_output_rejection(_bad_request(message)) is True


@pytest.mark.parametrize("message", [
    "Invalid schema for function 'query_db': 'type' is a required property",
    "Unsupported parameter: 'temperature' is not supported with this model",
    "context_length_exceeded: your messages resulted in 200000 tokens",
    "model `gpt-4o` does not exist",
])
def test_a_genuine_bad_request_is_NOT_retried(message):
    """The narrowness is the point. Retrying a real 400 eight times burns money to arrive at
    exactly the same error, and hides it behind a delay."""
    assert is_model_output_rejection(_bad_request(message)) is False


def test_other_error_types_are_not_swept_up():
    assert is_model_output_rejection(RateLimitError.__new__(RateLimitError)) is False
    assert is_model_output_rejection(ValueError("nope")) is False


def test_the_flake_is_retried_and_succeeds():
    calls = {"n": 0}

    def flaky():
        calls["n"] += 1
        if calls["n"] < 3:
            raise _bad_request(GROQ_REAL)
        return "answered"

    assert _with_retry(flaky, tries=5, base=0) == "answered"
    assert calls["n"] == 3


def test_a_real_bad_request_fails_immediately():
    calls = {"n": 0}

    def broken():
        calls["n"] += 1
        raise _bad_request("Invalid schema for function 'query_db'")

    with pytest.raises(BadRequestError):
        _with_retry(broken, tries=5, base=0)
    assert calls["n"] == 1, "a genuine 400 must not be retried at all"


def test_it_still_gives_up_eventually():
    def always():
        raise _bad_request(GROQ_REAL)

    with pytest.raises(BadRequestError):
        _with_retry(always, tries=2, base=0)


def test_transient_errors_are_still_retried():
    """The new branch must not shadow the existing one."""
    calls = {"n": 0}

    def flaky():
        calls["n"] += 1
        if calls["n"] < 2:
            raise APIConnectionError.__new__(APIConnectionError)
        return "ok"

    assert _with_retry(flaky, tries=4, base=0) == "ok"


# --------------------------------------------------------------------------- #
# The tool name the model actually emitted
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("emitted,expected", [
    ("functions/read_logs", "read_logs"),
    ("functions.read_logs", "read_logs"),
    ("tools/query_db", "query_db"),
    ("tools.query_db", "query_db"),
    ("  read_logs  ", "read_logs"),
    ("read_logs", "read_logs"),
])
def test_model_namespacing_is_stripped(emitted, expected):
    assert ToolRegistry.canonical_tool_name(emitted) == expected


def test_stripping_is_idempotent():
    once = ToolRegistry.canonical_tool_name("functions/read_logs")
    assert ToolRegistry.canonical_tool_name(once) == once


def test_a_tool_named_after_a_prefix_is_not_mangled():
    """`functions_summary` must survive; only a real namespace separator is stripped."""
    assert ToolRegistry.canonical_tool_name("functions_summary") == "functions_summary"


def test_a_namespaced_name_resolves_to_the_real_tool():
    from boss_engine.tools.builtin import build_default_registry

    reg = build_default_registry()
    real = reg.names()[0]
    assert reg.get(f"functions/{real}").name == real


def test_an_unknown_tool_names_what_IS_available():
    """'unknown tool: functions/read_logs' with no list sent the operator to the source."""
    from boss_engine.tools.builtin import build_default_registry
    from boss_engine.tools.registry import ToolError

    with pytest.raises(ToolError) as err:
        build_default_registry().get("definitely_not_a_tool")
    assert "Available:" in str(err.value)


# --------------------------------------------------------------------------- #
# The diagnostic tools must actually be reachable from a question
# --------------------------------------------------------------------------- #
def test_the_tools_the_operator_needs_are_in_the_discuss_registry():
    from boss_engine.tools.builtin import build_discuss_read_registry

    names = set(build_discuss_read_registry().names())
    for needed in ("read_logs", "query_db", "inspect_self", "find_starved_work",
                   "consistency_report"):
        assert needed in names, f"{needed} is not reachable from a question"
