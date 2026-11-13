"""A skill is a thing an operator can author, not an id only BOSS can implement.

`project_skills` recorded that a project MAY use a skill; it never said what the skill DOES.
Behaviour lived in a Python executor registered inside BOSS, so every capability a customer
wanted was a pull request against the product — the wrong boundary for the thing most worth
customising, because the operator knows their tools, their accounts and their house rules.
"""
import pytest

from boss_engine.services import skill_definitions as sd
from boss_engine.services.skill_executors import build_sandbox_command, registered_executor_ids


# --------------------------------------------------------------------------- #
# The sharp edge: an operator-authored command is code that runs.
# --------------------------------------------------------------------------- #
def test_an_input_cannot_become_a_second_command():
    """THE test on this file. Inputs come from a workflow's run input — an operator, or an
    earlier step — and a template built by concatenation turns `; rm -rf /` into a command."""
    built = build_sandbox_command(
        "ffmpeg -i {video} -ss {start} out.mp4",
        {"video": "a.mp4; rm -rf /", "start": "$(whoami)"})
    assert "; rm -rf /" not in built.replace("'a.mp4; rm -rf /'", "")
    assert "'a.mp4; rm -rf /'" in built
    assert "'$(whoami)'" in built


def test_a_missing_input_becomes_an_empty_argument_not_a_broken_command():
    built = build_sandbox_command("tool {a} {b}", {"a": "x"})
    assert built == "tool x ''"


def test_the_workdir_is_always_available():
    # `shlex.quote` leaves an already-safe value alone, so this reads as plain text — which is
    # the point: quoting appears exactly where it is needed and nowhere else.
    assert build_sandbox_command("cp {workdir}/in {workdir}/out", {}) == "cp /work/in /work/out"


def test_the_image_must_be_pinned_by_digest():
    """A floating tag means the thing that ran last week is not the thing that runs today,
    which is a moving target with a sandbox around it."""
    with pytest.raises(sd.SkillDefinitionError) as caught:
        sd.validate_config("sandboxed_command",
                           {"image": "ffmpeg:latest", "command": "ffmpeg -version"}, [])
    assert "digest" in str(caught.value)


def test_a_digest_pinned_image_is_accepted():
    config = sd.validate_config(
        "sandboxed_command",
        {"image": "ghcr.io/acme/ffmpeg@sha256:" + "a" * 64, "command": "ffmpeg -version"}, [])
    assert config["timeout_seconds"] == 300
    # OFF UNLESS ASKED FOR: a command that can reach the internet can exfiltrate what it was
    # given, and most media work needs no network at all.
    assert config["network"] is False


def test_a_placeholder_must_be_a_declared_input():
    """Otherwise it substitutes empty at run time and silently produces the wrong output, which
    is worse than refusing."""
    with pytest.raises(sd.SkillDefinitionError) as caught:
        sd.validate_config(
            "sandboxed_command",
            {"image": "ghcr.io/a/b@sha256:" + "0" * 64, "command": "tool {nope}"}, ["video"])
    assert "nope" in str(caught.value)


@pytest.mark.parametrize("timeout", [1, 4, 3601, 99999])
def test_the_timeout_is_bounded(timeout):
    with pytest.raises(sd.SkillDefinitionError):
        sd.validate_config(
            "sandboxed_command",
            {"image": "ghcr.io/a/b@sha256:" + "0" * 64, "command": "tool",
             "timeout_seconds": timeout}, [])


def test_it_refuses_to_run_where_there_is_no_isolation():
    """The local provider is a temp directory and a subprocess ON THE CONTROL PLANE'S HOST.
    Running a customer's command there is remote code execution against the box holding every
    project's credentials, so this refuses rather than degrades."""
    from boss_engine.services.skill_executors import _isolation_available

    class _Local:
        governed_sandbox_provider = "local"
        sandbox_agent_url = "http://127.0.0.1:8088"

    ready, why = _isolation_available(_Local())
    assert ready is False
    assert "control plane" in why

    class _MicroVM:
        governed_sandbox_provider = "microvm"
        sandbox_agent_url = "http://sandbox:8088"

    assert _isolation_available(_MicroVM())[0] is True


def test_the_executor_is_registered():
    assert "skill.sandboxed_command" in registered_executor_ids()


# --------------------------------------------------------------------------- #
# The object itself
# --------------------------------------------------------------------------- #
def test_the_behaviours_and_no_integration_kind():
    """Five behaviours, and an integration is still not one of them.

    In BOSS a connection grants authority to a skill; it is not something you can invoke on its
    own, so binding "the Slack integration" as a step has never been a thing to offer.

    `agent_program` was the fourth, and it earned it: a program an AGENT wrote mid-run and the
    workflow kept. It is not a `sandboxed_command` under another name — that kind demands an image
    pinned by digest because a PERSON chose it, and an agent's program runs in the deployment's own
    container boundary instead. Different author, different guarantee, different shape.

    `guide` is the fifth (agent-loadout spec §4.6, added in 1b03384c): a SKILL.md playbook an
    operator uploaded. It runs nothing, so it earns its place only while it stays that way — it
    takes no config at all, because any setting would be a way to make it DO something.
    """
    assert sd.BEHAVIOURS == ("builtin", "mcp_tool", "sandboxed_command", "agent_program", "guide")
    assert "integration" not in sd.BEHAVIOURS
    assert sd.validate_config("guide", {}, []) == {}
    with pytest.raises(sd.SkillDefinitionError, match="takes no config"):
        sd.validate_config("guide", {"command": "rm -rf /"}, [])


def test_an_agent_program_needs_a_language_and_a_source():
    with pytest.raises(sd.SkillDefinitionError, match="source is required"):
        sd.validate_config("agent_program", {"language": "python"}, [])
    with pytest.raises(sd.SkillDefinitionError, match="language must be one of"):
        sd.validate_config("agent_program", {"language": "haskell", "source": "x"}, [])

    kept = sd.validate_config(
        "agent_program",
        {"language": "python", "source": "print(1)", "timeout_seconds": 30,
         "written_by": "video_editor", "written_for": "video_edit_and_publish"},
        [])
    assert kept["source"] == "print(1)"
    assert kept["timeout_seconds"] == 30
    # WHO WROTE IT, on the record. This is the one kind where that is not obvious from the fact
    # that it exists, and an operator reading a skill an agent invented needs to know.
    assert kept["written_by"] == "video_editor"
    assert kept["written_for"] == "video_edit_and_publish"


def test_an_agent_cannot_save_a_program_below_the_approval_floor(monkeypatch):
    """`code.remember` is tier 1, so it runs with no person. The tier it STORES must not be.

    The schema does not list `risk_tier`, but the executor read it off the call. A model
    that passed 1 saved a program the next run executed with no approval — the click the
    tier-2 floor exists to require.
    """
    from boss_engine.config import Settings
    from boss_engine.services.skill_executors import (
        SkillExecutorContext, execute_code_remember)

    captured: dict = {}

    def fake_draft(_connection, **kwargs):
        captured.update(kwargs)
        return {"decision_id": "dec_t", "status": "pending"}

    monkeypatch.setattr(
        "boss_engine.services.skill_definitions.draft_for_approval", fake_draft)
    ctx = SkillExecutorContext(
        settings=Settings(), connection=None, project_id="p", actor="video_editor")
    execute_code_remember(ctx, {
        "skill_key": "hold_the_slide",
        "summary": "holds a still for four seconds",
        "language": "python",
        "source": "print(1)",
        "risk_tier": 1,
    })
    assert captured["risk_tier"] == 2
    # A PROPOSAL, never a save (BOSSAPP-843): drafts carry no trust and write nothing.
    assert "trusted" not in captured and captured["mode"] == "upsert"

    execute_code_remember(ctx, {
        "skill_key": "hold_the_slide",
        "summary": "holds a still for four seconds",
        "language": "python",
        "source": "print(1)",
        "risk_tier": 3,
    })
    assert captured["risk_tier"] == 3


def test_an_agent_program_the_size_of_a_repository_is_refused():
    with pytest.raises(sd.SkillDefinitionError, match="repository, not"):
        sd.validate_config("agent_program",
                           {"language": "python", "source": "#" * 60_001}, [])


def test_an_mcp_skill_must_name_a_server_and_a_tool():
    with pytest.raises(sd.SkillDefinitionError):
        sd.validate_config("mcp_tool", {"tool_name": "publish"}, [])
    assert sd.validate_config(
        "mcp_tool", {"mcp_server_id": "mcp_1", "tool_name": "publish"}, [])["tool_name"] == "publish"


def test_the_approval_floor_is_imported_not_restated():
    """A second copy of the floor is a second chance for it to drift from the one every
    capability obeys."""
    import inspect

    source = inspect.getsource(sd)
    assert "from .workflow_capabilities import MANUAL_APPROVAL_RISK_FLOOR" in source


def test_the_definition_endpoints_do_not_shadow_the_catalogue():
    """A GET on /v1/skills has served the runtime skill catalogue since long before this.

    Registering a second GET on the same path does not error — FastAPI matches the first route
    and the new one is never reached, so an endpoint ships, returns 200, and answers a different
    question than the one asked. That is what happened, and it was caught by calling the
    deployment rather than by reading the code.
    """
    from pathlib import Path

    api = (Path(__file__).resolve().parents[2] / "apps" / "api" / "app" / "main.py") \
        .read_text(encoding="utf-8")
    assert '@app.get("/v1/skill-definitions")' in api
    assert '@app.post("/v1/skill-definitions", status_code=201)' in api
    assert '@app.delete("/v1/skill-definitions/{skill_key}")' in api
    # The catalogue keeps its path, and there is exactly one GET on it — matched on the path
    # rather than the whole decorator line, which carries a response_model.
    assert api.count('@app.get("/v1/skills"') == 1
    assert '@app.post("/v1/skills"' not in api
