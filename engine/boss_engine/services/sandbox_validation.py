from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
import shlex
import subprocess
import time

from ..config import Settings
from . import owned_process
from .agent_env import agent_environment
from .scheduling import low_priority


class SandboxValidationConfigurationError(RuntimeError):
    pass


@dataclass
class SandboxValidationCommandResult:
    command: str
    argv: list[str]
    exit_code: int | None
    duration_ms: int
    timed_out: bool
    stdout: str
    stderr: str


@dataclass
class SandboxValidationResult:
    status: str
    summary: str
    repository_path: str
    branch_name: str
    head_sha: str | None
    completed: bool
    commands: list[SandboxValidationCommandResult]


class SandboxValidationAdapter:
    def __init__(self, settings: Settings):
        self.settings = settings

    @property
    def enabled(self) -> bool:
        return self.settings.sandbox_local_validation_enabled

    def configured_commands(self) -> list[str]:
        raw = self.settings.sandbox_validation_commands.strip()
        if not raw:
            return []
        separator = "\n" if "\n" in raw else ";"
        return [item.strip() for item in raw.split(separator) if item.strip()]

    def run(self, branch_name: str) -> SandboxValidationResult:
        repo_path = self._resolve_repo_path()
        self._validate_branch_name(branch_name)
        commands = self.configured_commands()
        if not commands:
            raise SandboxValidationConfigurationError(
                "BOSS_SANDBOX_VALIDATION_COMMANDS is empty."
            )

        head_sha = self._prepare_repository(repo_path, branch_name)
        results: list[SandboxValidationCommandResult] = []
        status = "passed"
        summary = "Sandbox local validation passed."

        for command in commands:
            result = self._run_command(repo_path, command)
            results.append(result)
            if result.timed_out:
                status = "failed"
                summary = f"Sandbox local validation timed out: {command}."
                break
            if result.exit_code != 0:
                status = "failed"
                summary = (
                    f"Sandbox local validation failed: {command} exited "
                    f"{result.exit_code}."
                )
                break

        return SandboxValidationResult(
            status=status,
            summary=summary,
            repository_path=str(repo_path),
            branch_name=branch_name,
            head_sha=head_sha,
            completed=status == "passed",
            commands=results,
        )

    def to_payload(self, result: SandboxValidationResult) -> dict:
        return {
            "status": result.status,
            "summary": result.summary,
            "repository_path": result.repository_path,
            "branch_name": result.branch_name,
            "head_sha": result.head_sha,
            "completed": result.completed,
            "commands": [
                {
                    "command": item.command,
                    "argv": item.argv,
                    "exit_code": item.exit_code,
                    "duration_ms": item.duration_ms,
                    "timed_out": item.timed_out,
                    "stdout": item.stdout,
                    "stderr": item.stderr,
                }
                for item in result.commands
            ],
        }

    def _resolve_repo_path(self) -> Path:
        configured = self.settings.sandbox_validation_repo_path
        if configured is None:
            raise SandboxValidationConfigurationError(
                "BOSS_SANDBOX_VALIDATION_REPO_PATH is not configured."
            )
        repo_path = configured.expanduser().resolve()
        if not repo_path.exists() or not repo_path.is_dir():
            raise SandboxValidationConfigurationError(
                f"Sandbox validation repo path does not exist: {repo_path}"
            )
        if not (repo_path / ".git").exists():
            raise SandboxValidationConfigurationError(
                f"Sandbox validation repo path is not a git repository: {repo_path}"
            )
        return repo_path

    def _validate_branch_name(self, branch_name: str) -> None:
        if not branch_name.strip():
            raise SandboxValidationConfigurationError("Branch name is required.")
        forbidden = {"..", "~", "^", ":", "\\", "?", "*", "[", " "}
        if branch_name.startswith("/") or branch_name.endswith("/"):
            raise SandboxValidationConfigurationError("Branch name is unsafe.")
        if any(token in branch_name for token in forbidden):
            raise SandboxValidationConfigurationError("Branch name is unsafe.")

    def _prepare_repository(self, repo_path: Path, branch_name: str) -> str:
        self._run_git(repo_path, ["status", "--porcelain"], require_clean_stdout=True)
        if self.settings.sandbox_validation_fetch_remote:
            self._run_git(repo_path, ["fetch", "--prune", "origin", branch_name])
            self._run_git(repo_path, ["checkout", "--detach", "FETCH_HEAD"])
        else:
            self._run_git(repo_path, ["checkout", branch_name])
        return self._run_git(repo_path, ["rev-parse", "HEAD"]).strip()

    def _run_git(
        self,
        repo_path: Path,
        args: list[str],
        *,
        require_clean_stdout: bool = False,
    ) -> str:
        try:
            completed = owned_process.run(
                ["git", "-C", str(repo_path), *args],
                capture_output=True,
                text=True,
                timeout=min(self.settings.sandbox_validation_timeout_seconds, 60),
                check=False,
            )
        except subprocess.TimeoutExpired as error:
            raise SandboxValidationConfigurationError(
                f"Git command timed out: git {' '.join(args)}"
            ) from error
        if completed.returncode != 0:
            message = completed.stderr.strip() or completed.stdout.strip()
            raise SandboxValidationConfigurationError(
                f"Git command failed: git {' '.join(args)}. {message}"
            )
        stdout = completed.stdout.strip()
        if require_clean_stdout and stdout:
            raise SandboxValidationConfigurationError(
                "Sandbox validation repository has uncommitted changes."
            )
        return stdout

    def _run_command(
        self, repo_path: Path, command: str
    ) -> SandboxValidationCommandResult:
        argv = shlex.split(command)
        if not argv:
            raise SandboxValidationConfigurationError("Validation command is empty.")
        self._assert_allowed_executable(argv[0])

        started = time.monotonic()
        try:
            # Validation runs the app's build/test commands in-container. See
            # services/scheduling.py.
            completed = owned_process.run(
                low_priority(argv),
                cwd=repo_path,
                # THIS RUNS THE PROJECT'S OWN TEST COMMAND. It used to run it with the control
                # plane's full environment, so `make test` inside a sandbox was pointed at the
                # production database by default. Same denial list as the agent subprocess —
                # see services/agent_env.py.
                env=agent_environment(os.environ, {"CI": "1"}),
                capture_output=True,
                text=True,
                timeout=self.settings.sandbox_validation_timeout_seconds,
                check=False,
            )
            duration_ms = int((time.monotonic() - started) * 1000)
            return SandboxValidationCommandResult(
                command=command,
                argv=argv,
                exit_code=completed.returncode,
                duration_ms=duration_ms,
                timed_out=False,
                stdout=self._truncate(completed.stdout),
                stderr=self._truncate(completed.stderr),
            )
        except subprocess.TimeoutExpired as error:
            duration_ms = int((time.monotonic() - started) * 1000)
            return SandboxValidationCommandResult(
                command=command,
                argv=argv,
                exit_code=None,
                duration_ms=duration_ms,
                timed_out=True,
                stdout=self._truncate(error.stdout or ""),
                stderr=self._truncate(error.stderr or ""),
            )
        except OSError as error:
            duration_ms = int((time.monotonic() - started) * 1000)
            return SandboxValidationCommandResult(
                command=command,
                argv=argv,
                exit_code=127,
                duration_ms=duration_ms,
                timed_out=False,
                stdout="",
                stderr=str(error),
            )

    def _assert_allowed_executable(self, executable: str) -> None:
        if "/" in executable or "\\" in executable:
            raise SandboxValidationConfigurationError(
                "Validation commands must use allowlisted executable names, not paths."
            )
        allowed = {
            item.strip()
            for item in self.settings.sandbox_validation_allowed_commands.split(",")
            if item.strip()
        }
        if executable not in allowed:
            raise SandboxValidationConfigurationError(
                f"Validation executable is not allowlisted: {executable}"
            )

    def _truncate(self, output: str | bytes) -> str:
        if isinstance(output, bytes):
            output = output.decode("utf-8", errors="replace")
        limit = self.settings.sandbox_validation_output_limit_chars
        if len(output) <= limit:
            return output
        return f"{output[:limit]}\n[truncated to {limit} chars]"
