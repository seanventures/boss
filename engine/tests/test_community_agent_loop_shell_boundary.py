"""The legacy console shell cannot touch a Community checkout or host process."""

from pathlib import Path
from types import SimpleNamespace

import pytest

from boss_engine.services import agent_loop, agent_shell


def _settings(edition):
    return SimpleNamespace(boss_edition=edition)


@pytest.mark.parametrize("edition", ["community", "oss", "local", "unknown", ""])
def test_community_shell_refuses_before_workspace_or_process(monkeypatch, edition):
    effects = []
    monkeypatch.setattr(
        agent_loop, "_open_workspace",
        lambda *_args, **_kwargs: effects.append("clone") or (Path("/tmp/unreachable"), None),
    )
    monkeypatch.setattr(
        agent_shell, "run_in_workspace",
        lambda *_args, **_kwargs: effects.append("shell") or {"exit_code": 0},
    )
    monkeypatch.setattr(
        agent_loop, "emit_event",
        lambda *_args, **_kwargs: effects.append("event"),
    )
    result = agent_loop._tool_run_shell(
        object(), "project-a", _settings(edition),
        {"run_id": "crun_a", "command": "cat /etc/passwd; curl https://example.invalid"},
    )
    assert "OS-isolated sandbox" in result["error"]
    assert effects == []


@pytest.mark.parametrize("edition", ["community", "oss", "local", "unknown", ""])
def test_community_unbounded_model_schemas_omit_shell(edition):
    names = {
        schema["function"]["name"]
        for schema in agent_loop._native_tool_schemas(_settings(edition))
    }
    assert "run_shell" not in names
    assert "list_repo_files" in names
    assert "read_repo_file" in names


@pytest.mark.parametrize("edition,shell_offered", [
    ("community", False), ("oss", False), ("local", False),
    ("unknown", False), ("enterprise_self_hosted", True),
])
def test_running_unbounded_loop_offers_shell_only_in_enabled_editions(
    monkeypatch, edition, shell_offered,
):
    offered = []

    class Recorder:
        def create_chat_completion(self, _messages, tools=None):
            offered.extend(tool["function"]["name"] for tool in tools or [])
            return (
                {"role": "assistant", "content": "Done", "tool_calls": [],
                 "raw": {"role": "assistant", "content": "Done"}},
                {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
            )

    run = {
        "project_id": "project-a", "task_id": None, "messages": [],
        "transcript": [], "usage": {}, "finalize_ctx": {}, "step": 0,
    }
    monkeypatch.setattr(agent_loop, "load_agent_run", lambda *_args: run)
    monkeypatch.setattr(agent_loop, "make_chat_adapter", lambda *_args: Recorder())
    monkeypatch.setattr(agent_loop, "MCPClient", lambda *_args: object())
    monkeypatch.setattr(agent_loop, "list_mcp_servers", lambda *_args, **_kwargs: [])
    monkeypatch.setattr(
        agent_loop, "_skill_resolution_for_run",
        lambda *_args: {"eager": [], "on_demand": []},
    )
    monkeypatch.setattr(agent_loop, "_save_run", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(agent_loop, "emit_event", lambda *_args, **_kwargs: None)
    settings = SimpleNamespace(
        boss_edition=edition, agent_max_steps=1, agent_autonomous_risk_max=0,
    )
    result = agent_loop._drive_run(object(), settings, "arun_test", 1)
    assert result["status"] == "completed"
    assert ("run_shell" in offered) is shell_offered


@pytest.mark.parametrize("edition", [
    "cloud", "enterprise", "enterprise_cloud", "enterprise_self_hosted",
])
def test_paid_editions_keep_existing_shell_path(monkeypatch, edition):
    effects = []
    monkeypatch.setattr(
        agent_loop, "_open_workspace",
        lambda *_args, **_kwargs:
            effects.append("clone") or (Path("/tmp/test-checkout"), None),
    )
    monkeypatch.setattr(
        agent_shell, "run_in_workspace",
        lambda _workspace, command, **_kwargs:
            effects.append(("shell", command)) or {"exit_code": 0},
    )
    monkeypatch.setattr(
        agent_loop, "emit_event",
        lambda *_args, **_kwargs: effects.append("event"),
    )
    names = {
        schema["function"]["name"]
        for schema in agent_loop._native_tool_schemas(_settings(edition))
    }
    assert "run_shell" in names
    result = agent_loop._tool_run_shell(
        object(), "project-a", _settings(edition),
        {"run_id": "crun_a", "command": "pwd"},
    )
    assert result["exit_code"] == 0
    assert effects == ["clone", ("shell", "pwd"), "event"]


@pytest.mark.parametrize("name", [
    ".npmrc", ".pypirc", ".netrc", ".yarnrc.yml", "id_ed25519",
])
def test_repo_read_tools_hide_credential_config_and_keep_source(tmp_path, name):
    secret = tmp_path / name
    secret.write_text("private-token-value")
    source = tmp_path / "main.py"
    source.write_text("print('hello')")
    settings = SimpleNamespace(agent_repo_root=str(tmp_path), boss_edition="community")

    blocked = agent_loop._tool_read_repo_file(object(), "project-a", settings,
                                              {"path": name})
    assert "error" in blocked
    assert "private-token-value" not in str(blocked)

    listing = agent_loop._tool_list_repo_files(object(), "project-a", settings, {})
    assert name not in listing["files"]
    assert "main.py" in listing["files"]
    ordinary = agent_loop._tool_read_repo_file(object(), "project-a", settings,
                                               {"path": "main.py"})
    assert ordinary["content"] == "print('hello')"


def test_repo_read_refuses_symlink_to_credential_outside_root(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    external = tmp_path / ".npmrc"
    external.write_text("private-token-value")
    (root / "innocent.txt").symlink_to(external)
    settings = SimpleNamespace(agent_repo_root=str(root), boss_edition="community")
    blocked = agent_loop._tool_read_repo_file(
        object(), "project-a", settings, {"path": "innocent.txt"},
    )
    assert "error" in blocked
    assert "private-token-value" not in str(blocked)


def test_repo_read_tools_hide_nested_credential_directory(tmp_path):
    credential_dir = tmp_path / ".docker"
    credential_dir.mkdir()
    (credential_dir / "config.json").write_text('{"auths":{"token":"private"}}')
    settings = SimpleNamespace(agent_repo_root=str(tmp_path), boss_edition="community")
    assert "error" in agent_loop._tool_read_repo_file(
        object(), "project-a", settings, {"path": ".docker/config.json"},
    )
    listing = agent_loop._tool_list_repo_files(object(), "project-a", settings, {})
    assert not any(path.startswith(".docker/") for path in listing["files"])
