"""Sandbox-scoped tool layer for the engine-native coding agent.

Why this exists
---------------
`NativeCodeAgent` used to be a single chat completion that returned whole-file
JSON rewrites. That is adequate for a greenfield single-file task (which is all
the VAG bake-off suite measures) and inadequate for a real repository: the model
cannot look at a file it was not handed, cannot search, and rewrites files it has
only partially seen. `code_agent.py`'s own docstring always intended a native
agent implementation behind the same seam; this module is its tool half.

Design rules
------------
- **Confinement is the invariant.** Every path is resolved and must land inside
  the sandbox root. Absolute paths, ``..`` traversal and symlinks that escape are
  refused with a message the model can act on, never silently clamped.
- **read_only means read only.** Review stages get the read tools and nothing
  else, so "the reviewer cannot edit" stays a property of the tool schema rather
  than a request in a prompt. This is the same separation the governed pipeline
  enforces at the gate; here it is enforced at the boundary.
- **Bounded output.** Every tool truncates. An unbounded ``grep`` on a monorepo
  would blow the context window in one turn and the run would die far from the
  cause.
- **Commands reuse the sandbox allowlist.** `bash` goes through the same
  executable allowlist, no-shell rule and timeout that `EphemeralSandbox.run`
  applies to verification commands. There is exactly one allowlist in BOSS and
  this is it.
- **Stdlib only.** The engine's dependency set is fixed; nothing here adds to it.
"""
from __future__ import annotations

from dataclasses import dataclass
import fnmatch
import os
from pathlib import Path
import re
import shlex
import subprocess
import time
from typing import Callable, Optional

from . import owned_process

_MAX_READ_BYTES = 60_000
_MAX_GREP_RESULTS = 120
_MAX_LIST_ENTRIES = 300
_MAX_GLOB_RESULTS = 200
_MAX_CMD_OUTPUT = 12_000
_DEFAULT_CMD_TIMEOUT = 120

#: Directories that are never worth an agent's attention and are expensive to walk.
SKIP_DIRS = {
    ".git", "node_modules", "dist", "build", ".next", ".venv", "venv",
    "__pycache__", "vendor", "target", "coverage", ".turbo", ".pytest_cache",
    ".mypy_cache", ".ruff_cache", ".gradle", ".idea",
}


#: Autonomy levels, least to most capable. See Settings.agent_autonomy_level.
AUTONOMY_LEVELS = ("read_only", "edit", "standard", "full")


def _csv(raw) -> set[str]:
    return {item.strip().lower() for item in str(raw or "").split(",") if item.strip()}


def resolve_autonomy(settings, *, read_only: bool) -> str:
    """The level actually in force for this step.

    A review step is `read_only` no matter what the operator configured: "the reviewer
    cannot edit" is a governance invariant of the governed pipeline, not a preference.
    """
    if read_only:
        return "read_only"
    level = str(getattr(settings, "agent_autonomy_level", "standard") or "standard").lower()
    return level if level in AUTONOMY_LEVELS else "standard"


def blocklisted_commands(settings) -> set[str]:
    return _csv(getattr(settings, "agent_command_blocklist", ""))


def _base_executable(argv0: str) -> str:
    """The name a blocklist entry has to match, after the usual ways of hiding it.

    `/bin/rm`, `./rm` and `rm` are one command. Factory's blocklist resolves wrapper
    shells, absolute paths and command substitution specifically to defeat bypasses;
    shell syntax is already refused outright here, so path shapes are what is left.
    """
    name = (argv0 or "").strip()
    if name.startswith("./"):
        name = name[2:]
    name = name.rsplit("/", 1)[-1].rsplit("\\", 1)[-1]
    return name.lower()


class ToolError(RuntimeError):
    """Raised for a refusal the MODEL should see and correct (bad path, missing file).

    Tool errors are returned to the model as the tool result rather than raised
    out of the loop: a refused call is the next turn, not a dead end.
    """


@dataclass
class ToolOutcome:
    text: str
    is_error: bool = False


def _truncate(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n…[truncated, {len(text) - limit} more characters]"


class SandboxToolset:
    """The tools a coding agent may use, all confined to ``root``."""

    def __init__(
        self,
        root: Path,
        *,
        read_only: bool = False,
        command_runner: Optional[Callable[[str, Optional[int]], "object"]] = None,
        settings=None,
        autonomy: Optional[str] = None,
    ) -> None:
        self.root = Path(root).resolve()
        self.autonomy = autonomy or resolve_autonomy(settings, read_only=read_only)
        # `read_only` stays the authoritative flag for the write tools so the governance
        # invariant does not depend on the autonomy string being spelled correctly.
        self.read_only = read_only or self.autonomy == "read_only"
        self._command_runner = command_runner
        self._settings = settings
        #: Paths written or edited this session — used by the caller to prove the
        #: implementer did not touch the tests without re-reading the whole tree.
        self.written_paths: list[str] = []

    # ---- path safety -----------------------------------------------------
    def _resolve(self, raw: str) -> Path:
        candidate = (raw or "").strip()
        if not candidate:
            raise ToolError("path is required.")
        p = Path(candidate)
        # `~` is not absolute to pathlib, so without this it silently became
        # `<root>/~/secrets` and the model got "does not exist" for what was really a
        # refused escape. Say what actually happened.
        if candidate.startswith("~"):
            raise ToolError(
                f"{candidate!r} is a home-directory path and resolves outside the "
                "repository. Use a path relative to the repository root."
            )
        if p.is_absolute():
            raise ToolError(
                f"{candidate!r} is an absolute path. Use a path relative to the "
                "repository root (the directory you are working in)."
            )
        resolved = (self.root / p).resolve()
        try:
            resolved.relative_to(self.root)
        except ValueError:
            raise ToolError(
                f"{candidate!r} resolves outside the repository root and is refused. "
                "Stay inside the working tree."
            ) from None
        return resolved

    def _rel(self, p: Path) -> str:
        try:
            return p.relative_to(self.root).as_posix()
        except ValueError:
            return p.as_posix()

    def _walk(self):
        for dirpath, dirnames, filenames in os.walk(self.root):
            dirnames[:] = sorted(d for d in dirnames if d not in SKIP_DIRS)
            for name in sorted(filenames):
                yield Path(dirpath) / name

    # ---- read tools ------------------------------------------------------
    def read_file(self, path: str, offset: Optional[int] = None,
                  limit: Optional[int] = None) -> str:
        target = self._resolve(path)
        if not target.exists():
            raise ToolError(f"{self._rel(target)} does not exist.")
        if target.is_dir():
            raise ToolError(f"{self._rel(target)} is a directory — use list_dir.")
        try:
            text = target.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            raise ToolError(f"cannot read {self._rel(target)}: {exc}") from None
        lines = text.splitlines()
        start = max(0, (offset or 1) - 1)
        end = len(lines) if not limit else min(len(lines), start + int(limit))
        body = "\n".join(f"{i + 1}\t{lines[i]}" for i in range(start, end))
        header = f"{self._rel(target)} ({len(lines)} lines)"
        if start or end < len(lines):
            header += f" — showing {start + 1}-{end}"
        return _truncate(f"{header}\n{body}", _MAX_READ_BYTES)

    def list_dir(self, path: str = ".") -> str:
        target = self._resolve(path) if path not in ("", ".", "./") else self.root
        if not target.exists():
            raise ToolError(f"{self._rel(target)} does not exist.")
        if not target.is_dir():
            raise ToolError(f"{self._rel(target)} is a file — use read_file.")
        entries = []
        for child in sorted(target.iterdir(), key=lambda c: (c.is_file(), c.name)):
            if child.name in SKIP_DIRS:
                continue
            entries.append(f"{child.name}/" if child.is_dir() else child.name)
            if len(entries) >= _MAX_LIST_ENTRIES:
                entries.append(f"…[truncated at {_MAX_LIST_ENTRIES} entries]")
                break
        return f"{self._rel(target) or '.'}/\n" + "\n".join(entries or ["(empty)"])

    def glob(self, pattern: str) -> str:
        pat = (pattern or "").strip()
        if not pat:
            raise ToolError("pattern is required, e.g. '**/*.py' or 'src/**/*.ts'.")
        hits = []
        for f in self._walk():
            rel = self._rel(f)
            if fnmatch.fnmatch(rel, pat) or fnmatch.fnmatch(f.name, pat):
                hits.append(rel)
                if len(hits) >= _MAX_GLOB_RESULTS:
                    hits.append(f"…[truncated at {_MAX_GLOB_RESULTS} matches]")
                    break
        return "\n".join(hits) if hits else f"no files match {pat!r}"

    def grep(self, pattern: str, path: str = ".", glob: Optional[str] = None,
             max_results: Optional[int] = None) -> str:
        pat = (pattern or "").strip()
        if not pat:
            raise ToolError("pattern is required.")
        try:
            rx = re.compile(pat)
        except re.error as exc:
            raise ToolError(f"invalid regular expression {pat!r}: {exc}") from None
        base = self._resolve(path) if path not in ("", ".", "./") else self.root
        cap = min(int(max_results or _MAX_GREP_RESULTS), _MAX_GREP_RESULTS)
        out: list[str] = []
        roots = [base] if base.is_dir() else [base.parent]
        for f in (self._walk() if base == self.root else _walk_under(roots[0], base)):
            rel = self._rel(f)
            if glob and not (fnmatch.fnmatch(rel, glob) or fnmatch.fnmatch(f.name, glob)):
                continue
            try:
                content = f.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            if "\x00" in content[:1024]:
                continue
            for n, line in enumerate(content.splitlines(), 1):
                if rx.search(line):
                    out.append(f"{rel}:{n}: {line.strip()[:300]}")
                    if len(out) >= cap:
                        out.append(f"…[truncated at {cap} matches — narrow the pattern]")
                        return "\n".join(out)
        return "\n".join(out) if out else f"no matches for {pat!r}"

    # ---- write tools (refused in read_only mode) -------------------------
    def _require_writable(self, tool: str) -> None:
        if self.read_only:
            raise ToolError(
                f"{tool} is not available: this is a read-only review step. "
                "Report what you found; do not change files."
            )

    def write_file(self, path: str, content: str) -> str:
        self._require_writable("write_file")
        target = self._resolve(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        existed = target.exists()
        target.write_text(content if content is not None else "", encoding="utf-8")
        rel = self._rel(target)
        self.written_paths.append(rel)
        n = len((content or "").splitlines())
        return f"{'overwrote' if existed else 'created'} {rel} ({n} lines)"

    def edit_file(self, path: str, old_string: str, new_string: str,
                  replace_all: bool = False) -> str:
        self._require_writable("edit_file")
        target = self._resolve(path)
        if not target.exists():
            raise ToolError(f"{self._rel(target)} does not exist — use write_file to create it.")
        original = target.read_text(encoding="utf-8", errors="replace")
        if old_string == "":
            raise ToolError("old_string must not be empty — use write_file to create a file.")
        count = original.count(old_string)
        if count == 0:
            raise ToolError(
                f"old_string was not found in {self._rel(target)}. Read the file again and "
                "copy the exact text, including indentation."
            )
        if count > 1 and not replace_all:
            raise ToolError(
                f"old_string appears {count} times in {self._rel(target)}. Include more "
                "surrounding context to make it unique, or pass replace_all=true."
            )
        updated = (original.replace(old_string, new_string)
                   if replace_all else original.replace(old_string, new_string, 1))
        target.write_text(updated, encoding="utf-8")
        rel = self._rel(target)
        self.written_paths.append(rel)
        return f"edited {rel} ({count if replace_all else 1} replacement(s))"

    def bash(self, command: str, timeout: Optional[int] = None) -> str:
        self._require_writable("bash")
        cmd = (command or "").strip()
        if not cmd:
            raise ToolError("command is required.")
        if self.autonomy == "edit":
            raise ToolError(
                "bash is not available at autonomy level 'edit': this step may change files "
                "but may not run commands. BOSS runs the verification commands itself."
            )
        self._assert_not_blocked(cmd)
        limit = int(timeout or _DEFAULT_CMD_TIMEOUT)
        if self._command_runner is not None:
            try:
                result = self._command_runner(cmd, limit)
            except Exception as exc:  # surfaced to the model, not the loop
                raise ToolError(str(exc)) from None
            out = ((getattr(result, "stdout", "") or "")
                   + (getattr(result, "stderr", "") or ""))
            code = getattr(result, "exit_code", None)
            return _truncate(f"$ {cmd}\n(exit {code})\n{out}", _MAX_CMD_OUTPUT)
        return _truncate(self._run_locally(cmd, limit), _MAX_CMD_OUTPUT)

    def _assert_not_blocked(self, command: str) -> None:
        """Refuse a command the operator has put out of reach.

        Order matters and is deliberate: BLOCKLIST beats everything including the
        `full` autonomy level and any allowlist entry, then the denylist, then the
        sandbox allowlist (applied later, by the runner). There is no configuration
        that disables the blocklist — that is what makes it different from a denylist.
        """
        from .sandbox import looks_like_shell

        if looks_like_shell(command):
            # Checked here too, not only in the runner: an injected command runner may
            # have laxer rules, and `rm -rf / && echo ok` must not reach it.
            raise ToolError(
                f"{command!r} uses shell syntax and commands run WITHOUT a shell. "
                "Give one plain command."
            )
        try:
            argv = shlex.split(command)
        except ValueError as exc:
            raise ToolError(f"could not parse {command!r}: {exc}") from None
        if not argv:
            raise ToolError("empty command.")
        name = _base_executable(argv[0])
        if name in blocklisted_commands(self._settings):
            raise ToolError(
                f"{name!r} is on the agent command blocklist and cannot be run at any "
                "autonomy level. If this task genuinely needs it, it needs a human."
            )
        if name in _csv(getattr(self._settings, "agent_command_denylist", "")):
            raise ToolError(
                f"{name!r} is on the agent command denylist for this deployment."
            )

    def _extra_allowed(self) -> set[str]:
        return _csv(getattr(self._settings, "agent_command_allowlist", ""))

    def _run_locally(self, command: str, timeout: int) -> str:
        from .sandbox import assert_allowed_executable, looks_like_shell
        from .scheduling import low_priority

        if looks_like_shell(command):
            raise ToolError(
                f"{command!r} uses shell syntax and commands run WITHOUT a shell. "
                "Operators like && || | ; > $ ( ) are not interpreted. Give one plain "
                "command, or call a script the repo already has."
            )
        argv = shlex.split(command)
        if not argv:
            raise ToolError("empty command.")
        if _base_executable(argv[0]) not in self._extra_allowed():
            try:
                assert_allowed_executable(argv[0], self._settings)
            except Exception as exc:
                raise ToolError(str(exc)) from None
        started = time.monotonic()
        try:
            # YIELD TO THE CONTROL PLANE. The agent's `bash` runs npm/vite/tsc/pytest as a
            # child of the API process, and those are among the most parallel workloads
            # there are. Measured on a production deployment before this rule existed:
            # `/v1/code-tasks?limit=5` went 0.37s -> 47.1s while a code task ran. An
            # unniced build makes the console unusable; see
            # tests/test_builds_yield_to_the_control_plane.py.
            from .agent_env import agent_environment

            proc = owned_process.run(
                low_priority(argv), cwd=str(self.root), capture_output=True, text=True,
                timeout=timeout, start_new_session=True,
                env=agent_environment(os.environ, {"CI": "1"}),
            )
        except subprocess.TimeoutExpired:
            return f"$ {command}\n(timed out after {timeout}s)"
        except FileNotFoundError:
            raise ToolError(f"{argv[0]!r} is not installed in the sandbox.") from None
        ms = int((time.monotonic() - started) * 1000)
        return (f"$ {command}\n(exit {proc.returncode}, {ms}ms)\n"
                f"{(proc.stdout or '') + (proc.stderr or '')}")

    # ---- dispatch --------------------------------------------------------
    def call(self, name: str, args: dict) -> ToolOutcome:
        """Invoke a tool by name. A refusal comes back as a result, not an exception."""
        fn = {
            "read_file": lambda a: self.read_file(
                a.get("path", ""), a.get("offset"), a.get("limit")),
            "list_dir": lambda a: self.list_dir(a.get("path", ".")),
            "glob": lambda a: self.glob(a.get("pattern", "")),
            "grep": lambda a: self.grep(
                a.get("pattern", ""), a.get("path", "."), a.get("glob"),
                a.get("max_results")),
            "write_file": lambda a: self.write_file(a.get("path", ""), a.get("content", "")),
            "edit_file": lambda a: self.edit_file(
                a.get("path", ""), a.get("old_string", ""), a.get("new_string", ""),
                bool(a.get("replace_all"))),
            "bash": lambda a: self.bash(a.get("command", ""), a.get("timeout")),
        }.get(name)
        if fn is None:
            return ToolOutcome(f"unknown tool {name!r}.", is_error=True)
        try:
            return ToolOutcome(fn(args or {}))
        except ToolError as exc:
            return ToolOutcome(f"error: {exc}", is_error=True)
        except Exception as exc:  # never kill the loop on one bad call
            return ToolOutcome(f"error: {type(exc).__name__}: {exc}", is_error=True)


def _walk_under(_root: Path, base: Path):
    for dirpath, dirnames, filenames in os.walk(base):
        dirnames[:] = sorted(d for d in dirnames if d not in SKIP_DIRS)
        for name in sorted(filenames):
            yield Path(dirpath) / name


# ---- schemas ------------------------------------------------------------
_READ_TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": "Read a file from the repository. Returns numbered lines.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Path relative to the repo root."},
                    "offset": {"type": "integer", "description": "First line to show (1-based)."},
                    "limit": {"type": "integer", "description": "How many lines to show."},
                },
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_dir",
            "description": "List the entries of a directory. Directories end with '/'.",
            "parameters": {
                "type": "object",
                "properties": {"path": {"type": "string", "description": "Defaults to the repo root."}},
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "glob",
            "description": "Find files by glob pattern, e.g. '**/*.py' or 'src/**/*.tsx'.",
            "parameters": {
                "type": "object",
                "properties": {"pattern": {"type": "string"}},
                "required": ["pattern"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "grep",
            "description": ("Search file contents with a regular expression. Returns "
                            "'path:line: text'. Prefer this over opening files one by one."),
            "parameters": {
                "type": "object",
                "properties": {
                    "pattern": {"type": "string", "description": "Python regular expression."},
                    "path": {"type": "string", "description": "Directory to search. Defaults to the repo root."},
                    "glob": {"type": "string", "description": "Only search files matching this glob."},
                    "max_results": {"type": "integer"},
                },
                "required": ["pattern"],
            },
        },
    },
]

_WRITE_TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "edit_file",
            "description": ("Replace an exact string in an existing file. Preferred over "
                            "write_file for changes to files that already exist."),
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "old_string": {"type": "string", "description": "Exact text to replace, including indentation."},
                    "new_string": {"type": "string", "description": "Replacement text."},
                    "replace_all": {"type": "boolean", "description": "Replace every occurrence."},
                },
                "required": ["path", "old_string", "new_string"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "write_file",
            "description": "Write a file in full, creating it if needed. Use for NEW files.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "content": {"type": "string"},
                },
                "required": ["path", "content"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "bash",
            "description": ("Run one allowlisted command (no shell operators) in the repo root, "
                            "e.g. 'python3 -m unittest discover -v' or 'npm test'."),
            "parameters": {
                "type": "object",
                "properties": {
                    "command": {"type": "string"},
                    "timeout": {"type": "integer", "description": "Seconds. Defaults to 120."},
                },
                "required": ["command"],
            },
        },
    },
]


def tool_schemas(read_only: bool, autonomy: Optional[str] = None) -> list[dict]:
    """The tool schema offered to the model.

    The schema IS the permission boundary — a tool the model was never told about is a
    tool it cannot try to use, which beats refusing the call after the fact. Review steps
    never see the write half; `edit` sees writes but no `bash`.
    """
    level = (autonomy or ("read_only" if read_only else "standard")).lower()
    if read_only or level == "read_only":
        return list(_READ_TOOLS)
    write = [t for t in _WRITE_TOOLS
             if not (level == "edit" and t["function"]["name"] == "bash")]
    return list(_READ_TOOLS) + write
