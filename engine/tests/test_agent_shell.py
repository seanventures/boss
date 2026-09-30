"""The agent gets a shell, in a disposable copy of the app's code.

Twelve hand-written tools could not answer most questions, because each answered exactly one
and the next question needed the next tool. A shell answers the ones nobody enumerated. That is
only safe if two things hold: it cannot reach outside the throwaway checkout, and it cannot
publish — real work still goes through the governed tools and their gates.

These pin both, plus the properties that make its output trustworthy: failures are reported
rather than swallowed, truncation is declared, and credentials never come back in the text.
"""

from __future__ import annotations

import subprocess

import pytest

from boss_engine.services import agent_shell
from boss_engine.services.agent_shell import (
    _scrub, ensure_workspace, run_in_workspace, shell_rejection, workspace_for,
)


@pytest.fixture()
def workspace(tmp_path):
    (tmp_path / "bugbox").mkdir()
    (tmp_path / "bugbox" / "package.json").write_text('{"name":"bugbox"}')
    (tmp_path / "README.md").write_text("hello")
    return tmp_path


# --------------------------------------------------------------------------- the guard ----

@pytest.mark.parametrize("command", [
    "grep -rn 'vite' .",
    "cat package.json",
    "ls -la bugbox",
    "npm run build",
    "git log --oneline -5",
    "rm -rf node_modules",            # destroying the throwaway checkout is fine and useful
    "sed -i 's/a/b/' src/App.tsx",    # editing it is the point of having a shell
])
def test_ordinary_work_is_allowed(command):
    """A guard that blocks real work teaches people to avoid the tool."""
    assert shell_rejection(command) is None


@pytest.mark.parametrize("command,needle", [
    ("git push origin HEAD", "git push"),
    ("gh pr create --fill", "gh publishing"),
    ("sudo rm -rf /var", "sudo"),
    ("shutdown -h now", "host power"),
    ("dd if=/dev/zero of=/dev/sda", "raw disk"),
    ("rm -rf /", "filesystem root"),
    ("git remote set-url origin https://x@github.com/a/b", "tokenless"),
])
def test_publishing_and_destruction_are_refused(command, needle):
    reason = shell_rejection(command)
    assert reason and needle in reason


def test_empty_is_refused():
    assert shell_rejection("   ") == "empty command"


# ---------------------------------------------------------------------- confinement ----

def test_it_cannot_step_outside_the_workspace(workspace):
    out = run_in_workspace(workspace, "ls", cwd="../..")
    assert out["error"] == "cwd must stay inside the workspace"


def test_it_runs_in_a_subdirectory_when_asked(workspace):
    out = run_in_workspace(workspace, "cat package.json", cwd="bugbox")
    assert out["exit_code"] == 0
    assert "bugbox" in out["stdout"]


def test_a_missing_directory_says_so(workspace):
    assert "no such directory" in run_in_workspace(workspace, "ls", cwd="nope")["error"]


def test_the_child_gets_no_credentials(workspace, monkeypatch):
    """A shell that can read the token is a shell that can use it somewhere else."""
    monkeypatch.setenv("GITHUB_TOKEN", "ghp_realtokenvalue123456")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-realkeyvalue1234567")
    out = run_in_workspace(workspace, "env")
    assert "ghp_realtokenvalue123456" not in out["stdout"]
    assert "sk-realkeyvalue1234567" not in out["stdout"]


def test_the_child_does_not_inherit_the_control_plane_dsn(workspace, monkeypatch):
    """DATABASE_URL matches none of TOKEN|SECRET|KEY|PASSWORD|CREDENTIAL.

    A denylist that misses the control-plane DSN lets the discuss shell dump every tenant's
    secret_refs with `psql` or a one-line Python client.
    """
    monkeypatch.setenv("DATABASE_URL", "postgresql://boss:boss@localhost:5432/boss")
    monkeypatch.setenv("BOSS_API_KEY", "not-a-token-name-but-still-a-secret")
    out = run_in_workspace(workspace, "env")
    assert "DATABASE_URL" not in out["stdout"]
    assert "postgresql://boss" not in out["stdout"]
    assert "BOSS_API_KEY" not in out["stdout"]


def test_the_console_shell_does_not_inherit_model_credentials(workspace, monkeypatch):
    """The coding agent needs model access; a console shell can use it to exfiltrate.

    A model invoking paid-edition `run_shell` can run `env` and reach the network. The
    shared code-agent allowlist intentionally admits these keys, so the shell needs a
    narrower second filter even when an operator configured an extra allowed name.
    """
    monkeypatch.setenv("ANTHROPIC_API_KEY", "anthropic-test-secret")
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "oauth-test-secret")
    monkeypatch.setenv("DATABASE_URL", "postgresql://boss:secret@localhost/boss")
    monkeypatch.setenv("BOSS_AGENT_ENV_ALLOW", "MY_TOOL_MODE,ANTHROPIC_API_KEY")
    monkeypatch.setenv("MY_TOOL_MODE", "operator-opt-in")

    out = run_in_workspace(workspace, "env")

    assert out["exit_code"] == 0
    for name in (
        "ANTHROPIC_API_KEY", "CLAUDE_CODE_OAUTH_TOKEN", "DATABASE_URL", "MY_TOOL_MODE",
    ):
        assert name not in out["stdout"]
    assert f"HOME={workspace}" in out["stdout"]
    assert "GIT_TERMINAL_PROMPT=0" in out["stdout"]


def test_cwd_cannot_walk_into_a_sibling_via_path_prefix(workspace):
    """`startswith` treats `/tmp/ws-evil` as inside `/tmp/ws`.

    Ask BOSS for cwd=`<workspace-name>-sibling` (or any prefix-sharing neighbor) and the
    shell leaves the throwaway checkout.
    """
    sibling = workspace.parent / (workspace.name + "-evil")
    sibling.mkdir()
    (sibling / "stolen.txt").write_text("secret")
    out = run_in_workspace(workspace, "cat stolen.txt", cwd="../" + sibling.name)
    assert out.get("error") == "cwd must stay inside the workspace"
    assert out.get("exit_code") is None


# ------------------------------------------------------------------------- results ----

def test_a_failing_command_is_a_result_not_an_error(workspace):
    """"npm test fails with X" is the answer to the question, not a broken tool."""
    out = run_in_workspace(workspace, "exit 3")
    assert out["exit_code"] == 3 and "error" not in out


def test_stderr_comes_back_too(workspace):
    out = run_in_workspace(workspace, "echo boom >&2")
    assert "boom" in out["stderr"]


def test_truncation_is_declared_and_keeps_the_end(workspace, monkeypatch):
    """Errors and summaries live at the BOTTOM of most output, and a silently cut result is
    read by the model as if it were complete."""
    monkeypatch.setattr(agent_shell, "OUTPUT_CHAR_CAP", 200)
    out = run_in_workspace(workspace, "seq 1 5000")
    assert out["truncated"] is True
    assert out["stdout"].rstrip().endswith("5000")


def test_a_hanging_command_times_out_rather_than_blocking_the_turn(workspace, monkeypatch):
    monkeypatch.setattr(agent_shell, "COMMAND_TIMEOUT_SECONDS", 1)
    out = run_in_workspace(workspace, "sleep 5")
    assert "timed out" in out["error"]


def test_a_refused_command_never_executes(workspace):
    out = run_in_workspace(workspace, "git push origin main")
    assert out["exit_code"] is None and "refused" in out["error"]


# ---------------------------------------------------------------------- credentials ----

def test_output_is_scrubbed_of_anything_credential_shaped():
    assert "ghp_" not in _scrub("remote: https://x:ghp_abcdefghijklmnopqrst@github.com/a/b")
    assert "@github.com" not in _scrub("https://user:secret@github.com/a/b")
    assert _scrub("sk-abcdefghijklmnopqrstuvwx") == "[redacted]"


def test_the_checkout_keeps_no_token_in_its_remote(tmp_path, monkeypatch):
    """The clone URL carries a token and .git/config would keep it where `cat` can reach it."""
    calls: list[list[str]] = []

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        (tmp_path / "ws" / "boss-agent" / ".git").mkdir(parents=True, exist_ok=True)
        return subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr(agent_shell, "WORKSPACE_ROOT", tmp_path / "ws")
    monkeypatch.setattr(agent_shell.subprocess, "run", fake_run)
    path, problem = ensure_workspace(
        "boss-agent", owner="example-user", repo="boss-sandbox", branch="boss/code/crun_1",
        clone_url="https://x:ghp_secret@github.com/example-user/boss-sandbox.git",
    )
    assert problem is None and path is not None
    rewrite = [c for c in calls if "set-url" in c]
    assert rewrite, "the remote must be rewritten after cloning"
    assert "ghp_secret" not in " ".join(rewrite[0])


def test_a_failed_checkout_never_echoes_the_clone_url(tmp_path, monkeypatch):
    monkeypatch.setattr(agent_shell, "WORKSPACE_ROOT", tmp_path / "ws")
    monkeypatch.setattr(
        agent_shell.subprocess, "run",
        lambda cmd, **kw: subprocess.CompletedProcess(
            cmd, 128, "", "fatal: could not read from https://x:ghp_secret@github.com/a/b"),
    )
    path, problem = ensure_workspace(
        "r1", owner="a", repo="b", branch="main",
        clone_url="https://x:ghp_secret@github.com/a/b.git",
    )
    assert path is None
    assert "ghp_secret" not in problem


def test_each_run_gets_its_own_workspace():
    assert workspace_for("crun_a") != workspace_for("crun_b")


def test_a_hostile_run_id_cannot_escape_the_workspace_root():
    """The property is CONTAINMENT, not the absence of dots. Separators are replaced, so
    "../../etc/passwd" becomes the harmless directory name ".._.._etc_passwd" — it still
    reads as traversal and cannot traverse."""
    escaped = workspace_for("../../etc/passwd").resolve()
    assert str(escaped).startswith(str(agent_shell.WORKSPACE_ROOT.resolve()))
    assert "/etc/passwd" not in str(escaped)


def test_workspace_lookup_is_fenced_to_the_calling_project():
    """run_id is a global primary key. Without AND project_id, project A opens project B's
    checkout (using the process GitHub token) and shells into it."""
    import inspect

    from boss_engine.services import agent_loop

    src = inspect.getsource(agent_loop._run_repo_and_branch)
    assert "project_id" in src
    assert "AND project_id" in src.replace("\n", " ")
