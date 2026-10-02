"""The scripted model speaks OpenAI's protocol and the fake `claude` speaks Claude Code's.

The incident: R4's auto-pick called `adapter.complete()`, which no adapter defines; a bare
`except` turned the AttributeError into "no model was available", and every test passed because
none of them crossed the real adapter. A harness model that is monkeypatched in-process repeats
that blindness. These tests hold the stub to the wire formats the product's real adapters parse,
and hold every scripted rule to being reachable — a rule shadowed by an earlier one is a stage
that silently gets the wrong answer.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import urllib.request
from pathlib import Path

import pytest

import model_stub

STUBS = Path(model_stub.__file__).resolve().parent
FAKE_CLAUDE = STUBS / "bin" / "claude"


@pytest.fixture()
def stub():
    server = model_stub.make_server("127.0.0.1", 0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_address[1]}", server.RequestHandlerClass.script
    server.shutdown()


def _post(url: str, body: dict) -> dict:
    request = urllib.request.Request(url, data=json.dumps(body).encode(),
                                     headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(request, timeout=10) as response:
        return json.loads(response.read())


def test_every_scripted_rule_is_reachable_from_its_own_markers():
    """For EACH rule: a prompt made of exactly its markers is answered by that rule, not by an
    earlier one. Order is what decides, so this is what keeps a new rule from stealing a stage."""
    script = model_stub.Script()
    for rule in script.rules:
        markers = [m for m in rule["when"] if not m.startswith("re:")]
        regexes = [m[3:].replace("\\", "") for m in rule["when"] if m.startswith("re:")]
        prompt = "\n".join(markers + regexes)
        channel = rule.get("channel", "chat")
        chosen, matched = script.choose(prompt, "chat" if channel == "any" else channel)
        assert matched and chosen["id"] == rule["id"], (rule["id"], chosen["id"])


def test_the_real_openai_adapter_gets_a_scripted_answer_through_chat_completions(stub):
    url, _ = stub
    from boss_engine.config import Settings
    from boss_engine.services.openai_adapter import OpenAIAdapter

    settings = Settings(OPENAI_API_KEY="e2e", OPENAI_BASE_URL=f"{url}/v1", OPENAI_MODEL="e2e-stub")
    message, usage = OpenAIAdapter(settings).create_chat_completion(
        [{"role": "user", "content": "End with EXACTLY one final line:\n  CONSISTENT: YES\n  CONSISTENT: NO"}])
    assert message["content"].strip().endswith("CONSISTENT: YES")
    assert usage.get("model")


def test_an_unscripted_prompt_gets_the_default_and_is_logged_whole(stub):
    url, _ = stub
    _post(f"{url}/v1/chat/completions", {"model": "m", "messages": [
        {"role": "user", "content": "a prompt nobody wrote a rule for"}]})
    log = json.loads(urllib.request.urlopen(f"{url}/_log").read())["entries"]
    assert log[-1]["matched"] is False and log[-1]["rule"] == "default"
    assert log[-1]["prompt"] == "a prompt nobody wrote a rule for"


def test_a_files_rule_drives_the_native_agent_with_write_file_calls_then_text(stub):
    url, _ = stub
    tools = [{"type": "function", "function": {"name": "write_file"}}]
    first = _post(f"{url}/v1/chat/completions", {"model": "m", "tools": tools, "messages": [
        {"role": "user", "content": "QA has ALREADY written the tests; implement"}]})
    calls = first["choices"][0]["message"]["tool_calls"]
    assert [json.loads(c["function"]["arguments"])["path"] for c in calls] == ["e2e_feature.py"]
    second = _post(f"{url}/v1/chat/completions", {"model": "m", "tools": tools, "messages": [
        {"role": "user", "content": "QA has ALREADY written the tests; implement"},
        {"role": "assistant", "content": None, "tool_calls": calls},
        {"role": "tool", "tool_call_id": calls[0]["id"], "content": "ok"}]})
    assert second["choices"][0]["message"]["content"].startswith("Implemented")


def test_routing_answers_with_an_agent_that_was_actually_offered(stub):
    url, _ = stub
    prompt = ("Pick the ONE agent best suited to carry out this instruction.\n\n"
              "THE AGENTS, and what each one is for and holds\n  video_editor: edits\n"
              "    holds: media.cut\n  writer: writes\n")
    reply = _post(f"{url}/v1/chat/completions", {"model": "m", "messages": [
        {"role": "user", "content": prompt}]})
    assert json.loads(reply["choices"][0]["message"]["content"])["role_key"] == "video_editor"


def test_an_override_makes_one_test_fail_on_purpose_and_reset_undoes_it(stub):
    url, _ = stub
    _post(f"{url}/_override", {"rules": [{"id": "garbage", "when": ["Pick the ONE agent"],
                                          "reply": "not json at all"}]})
    reply = _post(f"{url}/v1/chat/completions", {"model": "m", "messages": [
        {"role": "user", "content": "Pick the ONE agent"}]})
    assert reply["choices"][0]["message"]["content"] == "not json at all"
    _post(f"{url}/_reset", {})
    reply = _post(f"{url}/v1/chat/completions", {"model": "m", "messages": [
        {"role": "user", "content": "Pick the ONE agent"}]})
    assert "role_key" in reply["choices"][0]["message"]["content"]


def _claude(url: str, argv: list[str], cwd: Path, stdin: str = "") -> subprocess.CompletedProcess:
    env = {"PATH": os.environ.get("PATH", ""), "HOME": str(cwd), "BOSS_E2E_MODEL_STUB_URL": url}
    return subprocess.run([sys.executable, str(FAKE_CLAUDE), *argv], cwd=str(cwd), env=env,
                          input=stdin, capture_output=True, text=True, timeout=30)


@pytest.mark.parametrize("output_format", ["json", "stream-json"])
def test_the_fake_claude_is_parsed_by_the_real_code_agent_parser(stub, tmp_path, output_format):
    """Both formats ClaudeCodeAgent._invoke asks for, read back by its own _parse_cli_output."""
    url, _ = stub
    from boss_engine.services.code_agent import _parse_cli_output

    argv = ["-p", "--output-format", output_format, "--max-turns", "5",
            "--permission-mode", "acceptEdits", "--", "You are a QA engineer. author tests"]
    if output_format == "stream-json":
        argv.insert(3, "--verbose")
    done = _claude(url, argv, tmp_path)
    assert done.returncode == 0, done.stderr
    parsed = _parse_cli_output(done.stdout)
    assert parsed and parsed["result"].startswith("Wrote test_e2e_feature.py")
    assert (tmp_path / "test_e2e_feature.py").is_file()


def test_the_fake_claude_writes_nothing_on_a_read_only_seat(stub, tmp_path):
    url, _ = stub
    done = _claude(url, ["-p", "--output-format", "json", "--permission-mode", "default", "--",
                         "You are a QA engineer. author tests"], tmp_path)
    assert done.returncode == 0
    assert not (tmp_path / "test_e2e_feature.py").exists()


def test_the_fake_claude_reads_a_long_prompt_from_stdin_and_reports_signed_in(stub, tmp_path):
    url, _ = stub
    done = _claude(url, ["-p", "--output-format", "json"], tmp_path,
                   stdin="QA has ALREADY written the tests " + "x" * 100_000)
    assert json.loads(done.stdout)["result"].startswith("Implemented")
    status = _claude(url, ["auth", "status"], tmp_path)
    assert json.loads(status.stdout)["loggedIn"] is True


def test_the_fake_claude_refuses_to_guess_without_a_model(tmp_path):
    done = subprocess.run([sys.executable, str(FAKE_CLAUDE), "-p", "--", "hi"], cwd=str(tmp_path),
                          env={"PATH": os.environ.get("PATH", ""), "HOME": str(tmp_path)},
                          capture_output=True, text=True, timeout=30)
    assert done.returncode == 2 and "e2e harness" in done.stderr


def test_an_agent_session_is_scripted_turn_by_turn_with_values_from_earlier_turns(stub):
    """agent_work steps call skills as tools over several turns; turn two must use the artifact
    turn one's tool returned, or the video example edits a file that does not exist."""
    url, _ = stub
    tools = [{"type": "function", "function": {"name": "skill_media_fetch_link"}},
             {"type": "function", "function": {"name": "skill_video_trim"}}]
    ask = {"role": "user", "content": "WHAT I WANT DONE\nTrim the video to its first three seconds"
           "\n  video_url: https://media.e2e.test/media/clip.mp4"}
    first = _post(f"{url}/v1/chat/completions", {"model": "m", "tools": tools, "messages": [ask]})
    call = first["choices"][0]["message"]["tool_calls"][0]
    assert call["function"]["name"] == "skill_media_fetch_link"
    assert json.loads(call["function"]["arguments"]) == {
        "url": "https://media.e2e.test/media/clip.mp4"}
    history = [ask, {"role": "assistant", "content": None, "tool_calls": [call]},
               {"role": "tool", "tool_call_id": call["id"],
                "content": '{"media_artifact_id": "art_123", "name": "clip.mp4"}'}]
    second = _post(f"{url}/v1/chat/completions", {"model": "m", "tools": tools, "messages": history})
    trim = second["choices"][0]["message"]["tool_calls"][0]
    assert trim["function"]["name"] == "skill_video_trim"
    assert json.loads(trim["function"]["arguments"])["media_artifact_id"] == "art_123"
    history += [{"role": "assistant", "content": None, "tool_calls": [trim]},
                {"role": "tool", "tool_call_id": trim["id"],
                 "content": '{"media_artifact_id": "art_456"}'}]
    last = _post(f"{url}/v1/chat/completions", {"model": "m", "tools": tools, "messages": history})
    assert '"title": "E2E clip"' in last["choices"][0]["message"]["content"]


def test_an_agent_step_calls_a_skill_made_during_the_run_by_the_name_it_was_given(stub):
    """The R4 journey's named agent holds the skill the R1 journey made a minute earlier
    (`e2e_skill_<stamp>`), which no script written beforehand can name. The rule captures it
    from the instruction and calls it as the agent loop offers it: `skill_<slug>`."""
    url, _ = stub
    tools = [{"type": "function", "function": {"name": "skill_e2e_skill_101500"}},
             {"type": "function", "function": {"name": "skill_other"}}]
    ask = {"role": "user", "content": "WHAT I WANT DONE\ne2e-journey-agent-step (E2E journey "
           "101500): use the skill e2e_skill_101500 and say what it returns."}
    first = _post(f"{url}/v1/chat/completions", {"model": "m", "tools": tools, "messages": [ask]})
    call = first["choices"][0]["message"]["tool_calls"][0]
    assert call["function"]["name"] == "skill_e2e_skill_101500"
    history = [ask, {"role": "assistant", "content": None, "tool_calls": [call]},
               {"role": "tool", "tool_call_id": call["id"], "content": '{"answer": 42}'}]
    last = _post(f"{url}/v1/chat/completions", {"model": "m", "tools": tools, "messages": history})
    assert "e2e journey asked" in last["choices"][0]["message"]["content"]
