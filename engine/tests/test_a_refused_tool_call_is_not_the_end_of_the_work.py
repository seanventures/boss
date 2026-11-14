"""A provider refused one tool call and BOSS threw away a finished video edit.

The run had already imported the source, trimmed 3:42–4:50 and reframed it to portrait. Then the
agent called `media.read_document` with `file_id` alone — which the executor accepts — and the
provider checked the call against the tool's declared parameters first:

    Tool call validation failed: parameters for tool skill_media_read_document did not match
    schema: errors: [missing properties: 'url']

Two separate defects, and both of them matter beyond this skill:

  * THE DECLARATION LIED. `input_schema` is not a hint, it is the contract the provider
    ENFORCES. A skill whose executor accepts either of two inputs must not declare one of them
    required.
  * A MALFORMED CALL KILLED THE STEP. The exception lands before any assistant turn exists, so
    nothing caught it and the step deadlettered — losing work that had already succeeded.
"""
import pytest

from boss_engine.services.agent_loop import (
    MAX_REFUSED_TOOL_CALLS,
    _skill_tool_parameters,
    _the_provider_refused_the_tool_call,
)


# --------------------------------------------------------------------------- #
# Telling a bad call apart from a bad day
# --------------------------------------------------------------------------- #
GROQ_REFUSAL = (
    "BadRequestError: Error code: 400 - {'error': {'message': 'litellm.BadRequestError: "
    "GroqException - {\"error\":{\"message\":\"Tool call validation failed: tool call validation "
    "failed: parameters for tool skill_media_read_document did not match schema: errors: "
    "[missing properties: 'url']\",\"type\":\"invalid_request_error\",\"code\":\"tool_use_failed\"}}"
)


def test_a_refused_call_is_recognised():
    detail = _the_provider_refused_the_tool_call(Exception(GROQ_REFUSAL))
    assert detail is not None
    assert "did not match schema" in detail


def test_an_outage_is_not_mistaken_for_a_bad_call():
    """A real failure must stay a failure — an agent cannot word its way out of a dead upstream,
    and retrying one as though it could is how a broken provider looks like a broken agent."""
    for real in ("503 Service Unavailable", "connection reset by peer",
                 "RateLimitError: 429 too many requests", "context_length_exceeded"):
        assert _the_provider_refused_the_tool_call(Exception(real)) is None


def test_the_retries_are_bounded():
    assert 1 <= MAX_REFUSED_TOOL_CALLS <= 5, "a correction, not a loop"


# --------------------------------------------------------------------------- #
# The loop carries on, and keeps what it had
# --------------------------------------------------------------------------- #
class _AdapterThatRefusesOnce:
    """A provider that rejects the first call outright, then behaves."""

    def __init__(self, refusals: int):
        self.remaining = refusals
        self.saw: list[dict] = []

    def create_chat_completion(self, messages, tools=None):
        self.saw = list(messages)
        if self.remaining > 0:
            self.remaining -= 1
            raise RuntimeError(GROQ_REFUSAL)
        return ({"raw": {"role": "assistant", "content": "Done."},
                 "content": "Done.", "tool_calls": None}, {})


def _drive(adapter, cap=6):
    """The shape of the loop's completion phase, as the service runs it."""
    from boss_engine.services import agent_loop

    refused = 0
    messages: list[dict] = [{"role": "user", "content": "edit the video"}]
    for _ in range(cap):
        try:
            message, _usage = adapter.create_chat_completion(messages, tools=[])
        except Exception as exc:
            complaint = agent_loop._the_provider_refused_the_tool_call(exc)
            if complaint is None or refused >= agent_loop.MAX_REFUSED_TOOL_CALLS:
                raise
            refused += 1
            messages.append({"role": "user", "content": f"REFUSED: {complaint}"})
            continue
        messages.append(message["raw"])
        if not message.get("tool_calls"):
            return message.get("content"), refused, messages
    raise AssertionError("never finished")


def test_the_agent_is_told_what_was_wrong_and_finishes():
    adapter = _AdapterThatRefusesOnce(refusals=1)
    text, refused, messages = _drive(adapter)
    assert text == "Done."
    assert refused == 1
    assert any("REFUSED" in str(m.get("content")) for m in messages)
    assert messages[0]["content"] == "edit the video", "the work so far must survive"


def test_a_model_that_cannot_get_it_right_still_stops():
    with pytest.raises(RuntimeError):
        _drive(_AdapterThatRefusesOnce(refusals=99))


def test_the_message_handed_back_says_nothing_ran():
    """An agent told only "that failed" may redo work it already did. The video had been
    trimmed and reframed before this happened."""
    import inspect

    from boss_engine.services import agent_loop

    from boss_engine.kernel.policies import REFUSED_CALL_PROMPT

    # BOTH LOOPS (K4): the old loop's words, and the kernel's RecoveryPolicy an agent run uses.
    for source in (inspect.getsource(agent_loop._drive_run_legacy), REFUSED_CALL_PROMPT):
        assert "nothing happened" in source
        assert "do " in source and "not repeat it" in source


# --------------------------------------------------------------------------- #
# The declaration is the contract the provider enforces
# --------------------------------------------------------------------------- #
def test_an_optional_input_is_not_declared_required():
    schema = _skill_tool_parameters({"url": "string?", "file_id": "string?",
                                     "language": "string?"})
    assert "required" not in schema, schema
    assert set(schema["properties"]) == {"url", "file_id", "language"}


def test_read_document_requires_neither_url_nor_file_id():
    """Because the executor has always accepted either. This is the exact schema that refused
    the call — `url: string` — and it must not come back."""
    from pathlib import Path

    import yaml

    spec = yaml.safe_load(
        Path("packages/specpack/skills/video.yaml").read_text(encoding="utf-8"))
    skills = spec.get("skills") or spec.get("items") or []
    declared = next(s for s in skills if s["id"] == "media.read_document")

    parameters = _skill_tool_parameters(declared["input_schema"])
    assert "required" not in parameters, (
        "the provider will refuse a call that omits it, and the executor does not need it")
    assert "url" in parameters["properties"] and "file_id" in parameters["properties"]


def test_the_executor_says_what_it_will_accept():
    """The error an agent reads has to name both ways in, or it will guess one."""
    import inspect

    from boss_engine.services import skill_executors

    source = inspect.getsource(skill_executors.execute_media_read_document)
    assert "either url" in source and "file_id" in source


def test_giving_only_a_file_id_is_enough():
    """The call that was refused. It reaches the reader now."""
    import unittest.mock as mock

    from boss_engine.services import skill_executors

    seen: dict = {}

    def _fake_read_document(connection, settings, **kwargs):
        seen.update(kwargs)
        return {"text": "மக்கள் சிந்தனை", "name": "brief.pdf", "characters": 14,
                "mime_type": "application/pdf", "read_via": "drive_conversion",
                "file_id": kwargs["file_id"]}

    context = mock.Mock(connection=None, settings=None, project_id="p")
    with mock.patch("boss_engine.services.drive_media.read_document", _fake_read_document):
        out = skill_executors.execute_media_read_document(
            context, {"file_id": "1TdxfWZUmGDtAUQBIX0p"})

    assert seen["file_id"] == "1TdxfWZUmGDtAUQBIX0p"
    assert out["text"] == "மக்கள் சிந்தனை"
    assert out["read_via"] == "drive_conversion"


# --------------------------------------------------------------------------- #
# A failure that leaves no trace of why
# --------------------------------------------------------------------------- #
def test_a_failed_tool_keeps_its_reason_in_the_transcript():
    """240 characters of a skill invocation is its id, project, skill and status — the reason it
    failed falls off the end. The agent reads the whole result, so it knew; the transcript is
    what a PERSON reads, and it showed a failed step with no cause."""
    from boss_engine.services.agent_loop import _preview_of

    failed = {"id": "skinv_8b57fa80ccdc478c8364", "project_id": "boss-dogfood",
              "skill_id": "media.read_document", "caller": "video_editor",
              "created_by_job_id": None, "mode": "real", "status": "failed",
              "permission_decision": "allow", "attempts": 2, "max_attempts": 2,
              "reason": "'prompt' is application/octet-stream, which this cannot read as text."}

    kept = _preview_of(failed)
    assert "application/octet-stream" in kept, "the cause has to survive"


def test_a_success_is_still_only_a_preview():
    """Keeping every successful result whole would bloat every run record for no one's benefit."""
    from boss_engine.services.agent_loop import _preview_of

    assert len(_preview_of({"status": "succeeded", "text": "x" * 5000})) <= 240


def test_a_reason_in_tamil_is_not_escaped_into_noise():
    """`ensure_ascii` would turn a Tamil failure message into \\uXXXX codepoints — technically
    the same string and unreadable by the person it is for."""
    from boss_engine.services.agent_loop import _preview_of

    assert "மக்கள்" in _preview_of({"status": "failed", "reason": "மக்கள் சிந்தனை"})
