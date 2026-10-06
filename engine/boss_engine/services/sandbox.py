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
import logging
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import tempfile
import time
from typing import Any, Optional

from ..config import Settings
from . import owned_process
from ..schemas import FileChange
from .agent_env import agent_environment
from .scheduling import low_priority


#: Words that are shells, not programs. Seeing one as argv[0] means the caller wrote a shell
#: command and sandbox.run does not use a shell.
_SHELL_BUILTINS = frozenset({
    "[", "[[", "test", "if", "then", "else", "fi", "for", "while", "do", "done",
    "cd", "export", "source", ".", "eval", "exec", "set", "unset",
})

#: Operators that are shell syntax when they stand ALONE as a word.
#:
#: Scanning the raw string for these characters was wrong: it rejected
#: `python3 -c 'print(42)'`, where the parens are inside a quoted ARGUMENT and mean nothing to
#: anyone. shlex already knows what is quoted, so ask it — an operator that survives tokenising
#: as its own word is a real operator; one inside a quoted string is just text.
_SHELL_OPERATORS = frozenset({"&&", "||", "|", ";", ";;", ">", ">>", "<", "&"})


#: Characters that mean something to a shell when they are NOT inside quotes.
_SHELL_METACHARS = "&|;<>`"


_log = logging.getLogger(__name__)

#: Where package and browser downloads are kept BETWEEN sandboxes.
#:
#: Every stage gets a fresh shallow clone in a fresh temp dir — `provision` is per task, and
#: the warm-pool / per-repo cache that would avoid this is a microVM feature that is still
#: stubbed. So `npm install` and `playwright install` run again for each one, and with no
#: shared cache they re-download rather than re-link. Playwright browsers alone are hundreds of
#: megabytes.
#:
#: THIS CHANGES NOTHING ABOUT WHAT IS INSTALLED. npm still resolves from the lockfile and
#: verifies integrity; Playwright still checks it has the exact browser build it expects. The
#: cache only decides whether the bytes come off the network or off the disk, so a stale or
#: missing cache is a slow run, never a wrong one.
#:
#: Outside the clone on purpose: anything under the working tree is deleted with it, and
#: `git add -A` would otherwise sweep it into the diff.
_TOOL_CACHE_ROOT = os.environ.get("BOSS_SANDBOX_TOOL_CACHE", "/var/tmp/boss-tool-cache")


def _shared_tool_caches() -> dict:
    """Env pointing npm and Playwright at a cache that outlives one sandbox."""
    try:
        npm = Path(_TOOL_CACHE_ROOT) / "npm"
        pw = Path(_TOOL_CACHE_ROOT) / "playwright"
        npm.mkdir(parents=True, exist_ok=True)
        pw.mkdir(parents=True, exist_ok=True)
    except Exception as exc:  # noqa: BLE001
        # An unwritable cache path must not stop a build: fall back to the tool defaults and
        # simply be as slow as before.
        _log.warning("sandbox tool cache unavailable at %s (%s); running uncached",
                     _TOOL_CACHE_ROOT, exc)
        return {}
    return {
        "npm_config_cache": str(npm),
        "PLAYWRIGHT_BROWSERS_PATH": str(pw),
    }



def _looks_like_shell(command: str) -> bool:
    import shlex as _shlex

    try:
        # posix=False KEEPS the quotes on each token, which is the whole point: it is the only
        # way to tell `python3 -c 'print(42)'` (parens are an argument) from `echo $(date)`
        # (substitution the shell would perform). posix=True strips quotes and makes them
        # indistinguishable, which rejected legitimate commands and broke two suites.
        tokens = _shlex.split(command or "", posix=False)
    except ValueError:
        return False  # unbalanced quotes — a different error, reported elsewhere
    if not tokens:
        return False
    if tokens[0].strip("'\"") in _SHELL_BUILTINS:
        return True
    for token in tokens:
        if token[:1] in ("'", '"'):
            continue  # a quoted argument is text, whatever it contains
        if token in _SHELL_OPERATORS:
            return True
        if any(ch in _SHELL_METACHARS for ch in token) or "$(" in token:
            return True
    return False


#: How long to keep reading a killed command's output before giving up on it. The command is
#: already dead; this only bounds how long a grandchild holding the pipe open can cost us.
_DRAIN_SECONDS = 10


def looks_like_shell(command: str) -> bool:
    """Public alias — the engine-native agent's tool layer applies the same rule."""
    return _looks_like_shell(command)


def assert_allowed_executable(executable: str, settings) -> None:
    """Validate one executable name against the sandbox allowlist.

    Module-level so the coding agent's `bash` tool and `EphemeralSandbox.run`
    share ONE allowlist. A second copy of this rule is a second place for it to
    drift, and the drift would not be visible until something ran that should
    not have.
    """
    candidate = executable[2:] if executable.startswith("./") else executable
    if "/" in candidate or "\\" in candidate or candidate.startswith(".."):
        raise SandboxError(
            "Sandbox commands must use allowlisted executable names or repo-local "
            "wrappers (./mvnw, ./gradlew), not arbitrary paths."
        )
    allowed = {
        item.strip()
        for item in settings.sandbox_validation_allowed_commands.split(",")
        if item.strip()
    }
    if candidate not in allowed:
        if candidate in _SHELL_BUILTINS or _looks_like_shell(executable):
            raise SandboxError(
                f"{executable!r} is shell syntax, and sandbox commands run WITHOUT a "
                "shell — the first word is executed directly, so operators like "
                "[ ] && || | ; > cannot work. Give one plain command "
                "(e.g. 'npm run verify') and put any conditional logic inside a script "
                "the repo already has.")
        raise SandboxError(
            f"Executable is not allowlisted: {executable}. Allowed: "
            f"{', '.join(sorted(allowed)) or '(none configured)'}.")


def _kill_process_group(proc) -> None:
    """Kill the command AND anything it started.

    Killing only the process we spawned leaves Playwright's webServer, a vite dev server, or a
    stray node running — each of which holds the stdout pipe open, which is the thing that
    turns a timeout into a permanent hang. `start_new_session=True` at spawn is what makes
    there be a group to kill.
    """
    import signal

    # `os.getpgid(proc.pid)` IS NOT ENOUGH, and the case where it fails is the case that
    # matters. By the time a command has hung this way the parent has usually already exited
    # and been reaped, so its pid no longer resolves — getpgid raises ProcessLookupError — while
    # the process GROUP is very much alive with the grandchild in it. Falling back to
    # `proc.kill()` there kills a process that is already dead and leaves the server holding the
    # pipe, which is silently doing nothing in the one situation this function exists for.
    #
    # Spawned with `start_new_session=True`, the child is the group leader, so the group id IS
    # its pid — usable whether or not the process itself still exists.
    for target in (_pgid_of(proc), proc.pid):
        if target is None:
            continue
        try:
            os.killpg(target, signal.SIGKILL)
            return
        except (ProcessLookupError, PermissionError, OSError):
            continue
    try:
        proc.kill()
    except Exception:  # noqa: BLE001 — best effort; the drain that follows is still bounded
        pass


def _pgid_of(proc):
    """The process group, while the process still exists to ask about."""
    try:
        return os.getpgid(proc.pid)
    except (ProcessLookupError, PermissionError, OSError, AttributeError):
        return None


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
        egress: Optional[Any] = None,
    ) -> Path:
        """Shallow-clone ``owner/repo`` into a fresh temp dir and check out a work
        branch. ``token`` (PAT or short-lived GitHub-App installation token) is
        used only for the clone URL and is never written to disk.

        ``egress`` (identity.runs.CloneAccess) is the identity path: the clone goes through the
        boss-egress proxy with a credential-free URL, git asks the run's shim for the per-run
        ``boss_egr_`` credential, and NO GitHub token exists anywhere in the sandbox
        (IDENTITY.md §7.10). ``token`` is then ignored."""
        if self.path is not None:
            raise SandboxError("Sandbox already provisioned.")
        root = self._root()
        # mkdtemp gives us a unique, private directory we fully own.
        self.path = Path(tempfile.mkdtemp(prefix=f"{self.task_id}-", dir=root))
        self._owns_dir = True
        # Claimed while in use: the reaper sweeps unclaimed sandboxes a crash left behind.
        from .resource_ledger import claim_dir
        claim_dir(self.path)
        self.base_branch = base_branch

        # BOSS-267: the host's authenticated clone-URL form lives in ONE place now.
        # The coding agent works in this clone, so the token never lands in it (IDENTITY.md
        # §7.10): the URL is credential-free and the token rides in the environment of this
        # object's OWN git processes only (never the agent's commands, never `.git/config`).
        from .repo_host import git_access
        legacy_access = git_access(repo_host, owner, repo, token or "")
        clone_url = legacy_access.url
        self._git_env = dict(legacy_access.env)
        egress_config: list[str] = []
        if egress is not None:
            token = None
            self._git_env = {}
            clone_url = egress.clone_url
            for key, value in egress.git_config:
                egress_config += ["-c", f"{key}={value}"]

        depth = max(1, int(self.settings.sandbox_clone_depth or 1))
        self._git(
            [*egress_config, "clone", "--depth", str(depth), "--branch", base_branch, clone_url, "."],
            redact=token,
        )
        if egress is not None:
            # Keep pushes and fetches on the egress path inside this clone (still no secret on disk:
            # the helper asks the shim at the moment git needs it).
            for key, value in egress.git_config:
                self._git(["config", "--add" if key == "credential.helper" else "--replace-all", key, value])
        # Local identity for the bot author (commits are attributed to BOSS).
        self._git(["config", "user.name", "boss-agent"])
        self._git(["config", "user.email", "boss-agent@users.noreply.github.com"])
        # Per-clone ignore for build/test artifacts so `git add -A` never sweeps
        # them into the diff (uses .git/info/exclude — untracked, repo-safe).
        self._write_local_excludes()
        self.base_sha = self._git(["rev-parse", "HEAD"]).strip()

        # FETCH THE INTEGRATION BRANCH so an agent can reconcile against it.
        # A lineage branch is cloned --depth 1 --branch X, so the agent's world is that branch
        # and nothing else. Asked to merge two apps where one lives on the integration branch,
        # it saw one app and correctly left one app — twice, for $20, because the instruction
        # named a directory that was not in its working tree. GitHub's update-branch declines
        # when there is a real conflict, so tier 1 cannot help either.
        # Fetching it costs one shallow fetch and gives the agent something to actually merge.
        # Best-effort: a repo without the branch, or no network for it, proceeds as before.
        self.integration_ref = None
        self.integration_fetch_error = None
        integration = (getattr(self.settings, "sandbox_integration_branch", None)
                       or "main")
        if base_branch != integration:
            try:
                self._git(["fetch", "--depth", str(depth), "origin",
                           f"{integration}:refs/remotes/origin/{integration}"],
                          redact=token)
                self.integration_ref = f"origin/{integration}"
                self.integration_fetch_error = None
            except Exception as exc:  # noqa: BLE001 — never fail provisioning over this
                self.integration_ref = None
                # RECORD THE FAILURE. The first version swallowed this silently, so when a
                # consolidation produced one app instead of two there was no way to tell
                # whether the fetch had happened, the agent had ignored it, or the prompt was
                # wrong — three very different fixes, and no evidence to choose between them.
                # A best-effort step still has to say whether it succeeded.
                self.integration_fetch_error = f"{type(exc).__name__}: {exc}"[:200]

        self.work_branch = work_branch or f"boss/{self.task_id}"
        # A feedback-iteration clones its own accumulated branch (base_branch == work_branch),
        # so it is already checked out — `checkout -b` would fail on an existing branch. In
        # that case stay on it (diff() is relative to HEAD, so we produce only the new delta).
        self.resumed_from_checkpoint = False
        if self.work_branch != base_branch:
            # RESUME THE WORK BRANCH IF IT ALREADY EXISTS.
            #
            # Stages checkpoint by pushing their files to this branch, so "a failure costs the
            # CURRENT stage, never the run" (see _checkpoint_stage). That promise was only half
            # built: the write side pushed, and this side branched from `base_branch` and
            # ignored the pushed commits entirely. So every re-delivery — a deploy that killed
            # the worker mid-stage, a crash, a lost lease — cloned a tree with none of the
            # completed work in it and re-derived it from nothing. Measured in the comment on
            # the write-tests prompt: ~4.6 min and ~$1 to rewrite a suite that already existed.
            #
            # Checking the branch out instead restores every checkpointed stage, which is what
            # makes the checkpoint worth taking. A branch that does not exist yet — the normal
            # first run — falls through to exactly the old behaviour.
            if self._remote_branch_exists(self.work_branch, token=token):
                self._git(["fetch", "--depth", str(depth), "origin",
                           f"{self.work_branch}:refs/remotes/origin/{self.work_branch}"],
                          redact=token)
                self._git(["checkout", "-b", self.work_branch,
                           f"refs/remotes/origin/{self.work_branch}"])
                self.resumed_from_checkpoint = True
                _log.info(
                    "sandbox resumed task=%s branch=%s — work branch already existed, "
                    "checkpointed stages restored instead of re-run",
                    self.task_id, self.work_branch,
                )
            else:
                self._git(["checkout", "-b", self.work_branch])
        return self.path

    def _remote_branch_exists(self, branch: str, *, token: Optional[str]) -> bool:
        """Ask the remote, rather than assuming. Never fatal: if the check itself fails we
        branch from base as before, which is the pre-existing behaviour and always safe."""
        try:
            out = self._git(["ls-remote", "--heads", "origin", branch], redact=token)
            return bool((out or "").strip())
        except Exception:  # noqa: BLE001
            return False

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
        if self.path and self._owns_dir:
            from .resource_ledger import release_dir
            release_dir(self.path, "teardown")
        self.path = None

    # -- commands -----------------------------------------------------------
    def run(self, command: str, *, timeout: Optional[int] = None, cwd: Optional[str] = None) -> CommandResult:
        """Run an allowlisted build/test command inside the sandbox cwd (or an app subdir)."""
        self._require_provisioned()
        argv = shlex.split(command)
        if not argv:
            raise SandboxError("Empty command.")
        # Check the WHOLE command first. "npm run build && npm test" has a perfectly good
        # argv[0], so an argv[0]-only check passes it and then silently runs just the first
        # half — a verify that reports success having skipped the tests.
        if _looks_like_shell(command):
            raise SandboxError(
                f"{command!r} uses shell syntax, and sandbox commands run WITHOUT a shell. "
                "Operators like && || | ; > $ ( ) are not interpreted — the command would "
                "run only up to the first one. Give one plain command, or put the sequence "
                "in a script the repo already has (e.g. an npm script).")
        self._assert_allowed_executable(argv[0])
        limit = timeout or self.settings.sandbox_validation_timeout_seconds
        started = time.monotonic()
        workdir = cwd or self.path
        restore_note = ""
        try:
            from .official_canary import maybe_restore_for_command

            restore_note = maybe_restore_for_command(workdir, command)
        except Exception as exc:  # noqa: BLE001 — verify must still run
            restore_note = f"[boss] sandbox.restore failed {type(exc).__name__}: {exc}\n"
        env = agent_environment(os.environ, {"CI": "1", **_shared_tool_caches()})
        # Image-global `playwright` (browser install CLI) has no `test` command.
        # Prefer the app's node_modules/.bin so @playwright/test / vite win.
        try:
            local_bin = str(Path(workdir) / "node_modules" / ".bin")
        except TypeError:
            local_bin = ""
        if local_bin and Path(local_bin).is_dir():
            env["PATH"] = local_bin + os.pathsep + env.get("PATH", "")
        try:
            # npm install / build / playwright run in the API's own container; at normal
            # priority they starve it. See services/scheduling.py.
            # NOT subprocess.run. THE TIMEOUT DOES NOT BOUND IT.
            #
            # A build sat silent for eighteen minutes and forty-three seconds on one command:
            #
            #   turn 99  tool_call  Bash  {"command": "rm -rf .e2e-data && npm run verify",
            #                              "timeout": 180000}      23:49:36
            #   (no tool_result, ever)                             cancelled 00:08:19
            #
            # 46 tool calls in that run, 45 results. The three-minute timeout should have ended
            # it at 23:52:36 and did not.
            #
            # `subprocess.run(timeout=...)` kills the DIRECT CHILD and then calls communicate()
            # a second time to reap the pipes — with no timeout on that call. `npm run verify`
            # runs Playwright, Playwright starts the app's webServer, and that grandchild
            # inherits the stdout pipe. Killing npm leaves the server alive holding the write
            # end open, so the pipe never reaches EOF and the second communicate() blocks
            # forever. The stage stops producing turns and nothing anywhere times out.
            #
            # Two changes fix it. `start_new_session` puts the command in its own process
            # group, so the kill reaches the whole tree rather than just the process we
            # spawned; and the post-kill drain is BOUNDED, so even a pipe held open by
            # something we failed to kill costs ten seconds instead of the rest of the run.
            proc = owned_process.popen(  # noqa: S603 — argv is allowlisted above
                low_priority(argv),
                cwd=workdir,
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                start_new_session=True,
            )
            # THE COMMAND ENDING ENDS THE READ (owned_process.communicate). A command that
            # left `npm run dev &` behind finished; waiting for EOF on a pipe the dev server
            # holds turned that into a timeout. Its exit now ends the tree, and a timeout kills
            # the tree with a bounded drain.
            try:
                _out, _err = owned_process.communicate(proc, timeout=limit)
            except subprocess.TimeoutExpired as _exc:
                _kill_process_group(proc)
                raise subprocess.TimeoutExpired(argv, limit, output=_exc.output or "",
                                                stderr=_exc.stderr or "")
            completed = subprocess.CompletedProcess(argv, proc.returncode, _out, _err)
            _ms = int((time.monotonic() - started) * 1000)
            # WHICH COMMAND ATE THE FIVE MINUTES.
            #
            # `duration_ms` was returned to the caller and, for everything except the recorded
            # test commands, dropped. So a stage that spent four of its five minutes installing
            # dependencies into a freshly cloned temp dir — which every stage gets, because
            # provision shallow-clones per task and the per-repo cache is a microVM feature
            # that is still stubbed — looked exactly like a stage that spent five minutes
            # running tests. One line per command, so the next slow run says so itself.
            _log.info(
                "sandbox cmd task=%s exit=%s %dms  %s",
                self.task_id, completed.returncode, _ms, command[:160],
            )
            return CommandResult(
                command=command,
                argv=argv,
                exit_code=completed.returncode,
                duration_ms=_ms,
                timed_out=False,
                stdout=restore_note + self._truncate(completed.stdout),
                stderr=self._truncate(completed.stderr),
            )
        except subprocess.TimeoutExpired as error:
            return CommandResult(
                command=command,
                argv=argv,
                exit_code=None,
                duration_ms=int((time.monotonic() - started) * 1000),
                timed_out=True,
                stdout=restore_note + self._truncate(error.stdout or ""),
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
            extra_env = getattr(self, "_git_env", None)
            completed = owned_process.run(
                ["git", "-C", str(self.path), *args],
                capture_output=True,
                text=True,
                timeout=max(self.settings.sandbox_validation_timeout_seconds, 120),
                check=False,
                **({"env": {**os.environ, **extra_env}} if extra_env else {}),
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
        assert_allowed_executable(executable, self.settings)

    def _truncate(self, output: str | bytes) -> str:
        if isinstance(output, bytes):
            output = output.decode("utf-8", errors="replace")
        limit = self.settings.sandbox_validation_output_limit_chars
        return output if len(output) <= limit else output[:limit] + "\n…[truncated]"
