"""EphemeralSandbox — a per-task, isolated working tree where a CodeAgent can
clone a repo, edit files, build, and run tests, then hand BOSS a real `git diff`.

This is the *front half* of the coding flow the architecture doc describes: the
place (and the ONLY place) code is cloned, edited, and tested. It deliberately
reuses the executable-allowlist + timeout safety proven in
``sandbox_validation.py`` so an agent can run build/test commands without
arbitrary shell access.

Phase 0 runs this in BOSS infra (a local temp dir). Phase 2 runs the same code
inside the customer's trust boundary (the BOSS Runner) — nothing here assumes
where it executes.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import tempfile
import time
from typing import Optional

from ..config import Settings
from ..schemas import FileChange


class SandboxError(RuntimeError):
    pass


@dataclass
class CommandResult:
    command: str
    argv: list[str]
    exit_code: Optional[int]
    duration_ms: int
    timed_out: bool
    stdout: str
    stderr: str

    @property
    def ok(self) -> bool:
        return self.exit_code == 0 and not self.timed_out


@dataclass
class DiffSummary:
    files_changed: int
    additions: int
    deletions: int
    paths: list[str] = field(default_factory=list)


class EphemeralSandbox:
    """One isolated clone per task. Use as a context manager so the working tree
    is always torn down, even on failure::

        with EphemeralSandbox(settings, task_id="tsk_123") as sbx:
            sbx.provision(owner, repo, base_branch="main", token=tok)
            sbx.run("pytest -q")
            files = sbx.diff()
    """

    def __init__(self, settings: Settings, *, task_id: str):
        self.settings = settings
        self.task_id = task_id
        self.path: Optional[Path] = None
        self.base_branch: Optional[str] = None
        self.work_branch: Optional[str] = None
        self.base_sha: Optional[str] = None
        self._owns_dir = False

    # -- lifecycle ----------------------------------------------------------
    def __enter__(self) -> "EphemeralSandbox":
        return self

    def __exit__(self, *exc) -> None:
        self.teardown()

    def _root(self) -> Path:
        configured = self.settings.sandbox_root_path
        if configured:
            root = Path(configured).expanduser().resolve()
        else:
            root = Path(tempfile.gettempdir()) / "boss-sandboxes"
        root.mkdir(parents=True, exist_ok=True)
        return root

    def provision(
        self,
        owner: str,
        repo: str,
        *,
        base_branch: str = "main",
        work_branch: Optional[str] = None,
        token: Optional[str] = None,
        repo_host: str = "github",
    ) -> Path:
        """Shallow-clone ``owner/repo`` into a fresh temp dir and check out a work
        branch. ``token`` (PAT or short-lived GitHub-App installation token) is
        used only for the clone URL and is never written to disk."""
        if self.path is not None:
            raise SandboxError("Sandbox already provisioned.")
        root = self._root()
        # mkdtemp gives us a unique, private directory we fully own.
        self.path = Path(tempfile.mkdtemp(prefix=f"{self.task_id}-", dir=root))
        self._owns_dir = True
        self.base_branch = base_branch

        # BOSS-267: the host's authenticated clone-URL form lives in ONE place now.
        from .repo_host import clone_url_for
        clone_url = clone_url_for(repo_host, owner, repo, token or "")

        depth = max(1, int(self.settings.sandbox_clone_depth or 1))
        self._git(
            ["clone", "--depth", str(depth), "--branch", base_branch, clone_url, "."],
            redact=token,
        )
        # Local identity for the bot author (commits are attributed to BOSS).
        self._git(["config", "user.name", "boss-agent"])
        self._git(["config", "user.email", "boss-agent@users.noreply.github.com"])
        # Per-clone ignore for build/test artifacts so `git add -A` never sweeps
        # them into the diff (uses .git/info/exclude — untracked, repo-safe).
        self._write_local_excludes()
        self.base_sha = self._git(["rev-parse", "HEAD"]).strip()

        self.work_branch = work_branch or f"boss/{self.task_id}"
        # A feedback-iteration clones its own accumulated branch (base_branch == work_branch),
        # so it is already checked out — `checkout -b` would fail on an existing branch. In
        # that case stay on it (diff() is relative to HEAD, so we produce only the new delta).
        if self.work_branch != base_branch:
            self._git(["checkout", "-b", self.work_branch])
        return self.path

    # Common build/test artifacts a coding agent's test run can generate — kept
    # out of the diff so PRs contain only real source changes.
    _ARTIFACT_PATTERNS = (
        "__pycache__/", "*.pyc", "*.pyo", ".pytest_cache/", ".mypy_cache/",
        ".ruff_cache/", "node_modules/", ".DS_Store", "dist/", "build/",
        "*.egg-info/", ".coverage", "coverage.xml", ".tox/",
        # IaC verify lane (spec 05 T3): terraform/tofu init+plan artifacts must never
        # ride the PR diff (the lane also redirects TF_DATA_DIR out of the checkout).
        ".terraform/", ".terraform.lock.hcl", "*.tfplan",
    )

    def _write_local_excludes(self) -> None:
        assert self.path is not None
        exclude_file = self.path / ".git" / "info" / "exclude"
        try:
            existing = exclude_file.read_text(encoding="utf-8") if exclude_file.exists() else ""
            exclude_file.write_text(
                existing.rstrip("\n") + "\n# BOSS sandbox artifact ignores\n"
                + "\n".join(self._ARTIFACT_PATTERNS) + "\n",
                encoding="utf-8",
            )
        except OSError:
            pass  # best-effort; diff still works, just noisier

    def teardown(self) -> None:
        if self.path and self._owns_dir and self.path.exists():
            shutil.rmtree(self.path, ignore_errors=True)
        self.path = None

    # -- commands -----------------------------------------------------------
    def run(self, command: str, *, timeout: Optional[int] = None) -> CommandResult:
        """Run an allowlisted build/test command inside the sandbox cwd."""
        self._require_provisioned()
        argv = shlex.split(command)
        if not argv:
            raise SandboxError("Empty command.")
        self._assert_allowed_executable(argv[0])
        limit = timeout or self.settings.sandbox_validation_timeout_seconds
        started = time.monotonic()
        try:
            completed = subprocess.run(
                argv,
                cwd=self.path,
                env={**os.environ, "CI": "1"},
                capture_output=True,
                text=True,
                timeout=limit,
                check=False,
            )
            return CommandResult(
                command=command,
                argv=argv,
                exit_code=completed.returncode,
                duration_ms=int((time.monotonic() - started) * 1000),
                timed_out=False,
                stdout=self._truncate(completed.stdout),
                stderr=self._truncate(completed.stderr),
            )
        except subprocess.TimeoutExpired as error:
            return CommandResult(
                command=command,
                argv=argv,
                exit_code=None,
                duration_ms=int((time.monotonic() - started) * 1000),
                timed_out=True,
                stdout=self._truncate(error.stdout or ""),
                stderr=self._truncate(error.stderr or ""),
            )
        except OSError as error:
            return CommandResult(
                command=command,
                argv=argv,
                exit_code=127,
                duration_ms=int((time.monotonic() - started) * 1000),
                timed_out=False,
                stdout="",
                stderr=str(error),
            )

    # -- diff ---------------------------------------------------------------
    def has_changes(self) -> bool:
        self._require_provisioned()
        # staged + unstaged + untracked
        status = self._git(["status", "--porcelain"])
        return bool(status.strip())

    def diff_summary(self) -> DiffSummary:
        self._require_provisioned()
        self._git(["add", "-A"])
        numstat = self._git(["diff", "--cached", "--numstat"]).strip()
        additions = deletions = 0
        paths: list[str] = []
        for line in numstat.splitlines():
            parts = line.split("\t")
            if len(parts) != 3:
                continue
            add, dele, path = parts
            additions += 0 if add == "-" else int(add)
            deletions += 0 if dele == "-" else int(dele)
            paths.append(path)
        return DiffSummary(
            files_changed=len(paths),
            additions=additions,
            deletions=deletions,
            paths=paths,
        )

    def unified_diff(self) -> str:
        """The raw patch — stays in the customer boundary; BOSS keeps only the
        summary unless the customer opts in (see architecture §5.1)."""
        self._require_provisioned()
        self._git(["add", "-A"])
        return self._git(["diff", "--cached"], raw=True)

    def diff(self) -> list[FileChange]:
        """The changed tree as BOSS ``FileChange`` objects, ready to feed the
        existing ``propose_pull_request`` path. Binary/deleted files are skipped
        (the contents API path handles text upserts)."""
        self._require_provisioned()
        summary = self.diff_summary()
        changes: list[FileChange] = []
        for path in summary.paths:
            file_path = self.path / path  # type: ignore[operator]
            if not file_path.exists() or not file_path.is_file():
                continue  # deletion/rename target absent — skip for now
            try:
                content = file_path.read_text(encoding="utf-8")
            except (UnicodeDecodeError, OSError):
                continue  # binary or unreadable — not committed via contents API
            changes.append(FileChange(path=path, content=content))
        return changes

    # -- internals ----------------------------------------------------------
    def _require_provisioned(self) -> None:
        if self.path is None:
            raise SandboxError("Sandbox is not provisioned.")

    def _git(
        self, args: list[str], *, raw: bool = False, redact: Optional[str] = None
    ) -> str:
        assert self.path is not None
        try:
            completed = subprocess.run(
                ["git", "-C", str(self.path), *args],
                capture_output=True,
                text=True,
                timeout=max(self.settings.sandbox_validation_timeout_seconds, 120),
                check=False,
            )
        except subprocess.TimeoutExpired as error:
            raise SandboxError(f"git {args[0]} timed out") from error
        if completed.returncode != 0:
            message = (completed.stderr or completed.stdout or "").strip()
            if redact:
                message = message.replace(redact, "***")
            raise SandboxError(f"git {args[0]} failed: {message}")
        return completed.stdout if raw else completed.stdout

    def _assert_allowed_executable(self, executable: str) -> None:
        # Repo-local build wrappers are the norm for Java/Gradle/Maven projects
        # (./mvnw, ./gradlew). Allow a single leading "./" and validate the
        # basename against the allowlist; still reject absolute paths, parent
        # traversal, and nested paths.
        candidate = executable[2:] if executable.startswith("./") else executable
        if "/" in candidate or "\\" in candidate or candidate.startswith(".."):
            raise SandboxError(
                "Sandbox commands must use allowlisted executable names or repo-local "
                "wrappers (./mvnw, ./gradlew), not arbitrary paths."
            )
        allowed = {
            item.strip()
            for item in self.settings.sandbox_validation_allowed_commands.split(",")
            if item.strip()
        }
        if candidate not in allowed:
            raise SandboxError(f"Executable is not allowlisted: {executable}")

    def _truncate(self, output: str | bytes) -> str:
        if isinstance(output, bytes):
            output = output.decode("utf-8", errors="replace")
        limit = self.settings.sandbox_validation_output_limit_chars
        return output if len(output) <= limit else output[:limit] + "\n…[truncated]"
