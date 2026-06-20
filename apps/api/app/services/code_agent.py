"""CodeAgent — the swappable interface that turns a task spec into a real,
tested code change inside an :class:`EphemeralSandbox`.

Design (architecture doc §3, "Own Layer 2"):
  - ``CodeAgent`` is the seam. BOSS owns it; the model is rented; the sandbox is
    isolated. The first implementation (``ClaudeCodeAgent``) wraps Claude Code
    headless WITH its file/bash tools *enabled*, scoped to the sandbox cwd — the
    inverse of ``claude_cli_adapter`` (which denies tools to use Claude as a pure
    brain). Phase 1 adds a native Agent-SDK implementation behind the same seam.
  - The agent's *self-report* is never trusted for pass/fail. BOSS re-runs the
    configured test commands via the sandbox and that is the authoritative
    result that gates the PR.
  - The change of record is the git diff, not the model's text.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import json
from pathlib import Path
import subprocess
from typing import Optional, Protocol

from ..config import Settings
from ..schemas import FileChange
from .sandbox import CommandResult, DiffSummary, EphemeralSandbox


@dataclass
class CodeTaskSpec:
    task_id: str
    title: str
    instructions: str
    acceptance_criteria: list[str] = field(default_factory=list)
    # Authoritative verification commands BOSS re-runs after the agent stops.
    test_commands: list[str] = field(default_factory=list)


@dataclass
class RepoContext:
    owner: str
    repo: str
    base_branch: str = "main"
    repo_guide: Optional[str] = None  # CLAUDE.md / AGENTS.md, if ingested


@dataclass
class CodeAgentResult:
    summary: str
    diff: list[FileChange]
    diff_summary: DiffSummary
    tests: list[CommandResult]
    tests_passed: bool
    cost_usd_micros: int
    usage: dict
    transcript: str
    turns: int
    engine: str

    def to_metadata(self) -> dict:
        """Governance metadata only — the summary BOSS keeps (never the patch)."""
        return {
            "engine": self.engine,
            "summary": self.summary,
            "files_changed": self.diff_summary.files_changed,
            "additions": self.diff_summary.additions,
            "deletions": self.diff_summary.deletions,
            "paths": self.diff_summary.paths,
            "tests_passed": self.tests_passed,
            "tests": [
                {
                    "command": t.command,
                    "exit_code": t.exit_code,
                    "timed_out": t.timed_out,
                    "duration_ms": t.duration_ms,
                }
                for t in self.tests
            ],
            "cost_usd_micros": self.cost_usd_micros,
            "turns": self.turns,
        }


class CodeAgent(Protocol):
    def run(
        self, spec: CodeTaskSpec, repo: RepoContext, sandbox: EphemeralSandbox
    ) -> CodeAgentResult: ...

    def run_role(
        self, prompt: str, cwd: Path, *, read_only: bool = False, model: Optional[str] = None
    ) -> dict:
        """Run one isolated, role-scoped invocation against an existing workdir.
        Returns {summary, usage, cost_usd_micros, turns, model}. The role prompt is the
        guidance; the model impersonates the role. read_only stages must not edit files;
        model overrides which model runs this step (low-cost vs strong routing)."""
        ...


# --------------------------------------------------------------------------- #
#  Claude Code headless implementation                                        #
# --------------------------------------------------------------------------- #
class ClaudeCodeAgent:
    """Drives `claude -p` inside the sandbox with its native editing tools.

    Uses the Max-plan CLI as the coding brain. The CLI edits files directly in
    the sandbox working tree; BOSS then reads the diff and re-runs the tests.
    """

    engine = "claude_code"

    def __init__(self, settings: Settings):
        self.settings = settings

    def _claude_bin(self) -> str:
        configured = getattr(self.settings, "claude_cli_path", None) or "claude"
        if configured == "claude":
            local = Path.home() / ".local" / "bin" / "claude"
            if local.exists():
                return str(local)
        return configured

    def _build_prompt(self, spec: CodeTaskSpec, repo: RepoContext) -> str:
        criteria = "\n".join(f"- {c}" for c in spec.acceptance_criteria) or "- (none specified)"
        tests = "\n".join(f"- {c}" for c in spec.test_commands) or "- (none specified)"
        guide = f"\n\nREPO GUIDE:\n{repo.repo_guide}\n" if repo.repo_guide else ""
        return (
            "You are a senior software engineer working in a checked-out git repo "
            "(your current working directory). Implement the task below by editing "
            "files directly with your tools, then run the tests to verify.\n\n"
            f"TASK: {spec.title}\n\n"
            f"{spec.instructions}\n\n"
            f"ACCEPTANCE CRITERIA:\n{criteria}\n\n"
            f"TEST/BUILD COMMANDS (run these and make them pass):\n{tests}\n"
            f"{guide}\n"
            "RULES:\n"
            "- Make the smallest correct change that satisfies the criteria.\n"
            "- Edit files in place; do NOT create a branch, commit, or push — BOSS "
            "handles git, the PR, and the merge gate.\n"
            "- Run the test/build commands and iterate until they pass.\n"
            "- When done, end with a concise summary of WHAT you changed and WHY.\n"
        )

    def _invoke(
        self, prompt: str, cwd: Path, *, read_only: bool = False, model: Optional[str] = None
    ) -> dict:
        timeout = int(self.settings.code_agent_timeout_seconds or 900)
        # Review stages run read-only (no Edit/Write tools, no auto-accept) so a reviewer
        # cannot mutate the change it is judging.
        allowed = "Read,Grep,Glob,Bash" if read_only else self.settings.code_agent_allowed_tools
        permission = "default" if read_only else "acceptEdits"
        cmd = [
            self._claude_bin(),
            "-p", prompt,
            "--output-format", "json",
            "--max-turns", str(int(self.settings.code_agent_max_turns or 40)),
            "--allowedTools", allowed,
            "--permission-mode", permission,
        ]
        model = model or getattr(self.settings, "claude_cli_model", None)
        if model:
            cmd += ["--model", model]
        try:
            proc = subprocess.run(
                cmd,
                stdin=subprocess.DEVNULL,
                capture_output=True,
                text=True,
                timeout=timeout,
                cwd=str(cwd),
            )
        except FileNotFoundError as exc:
            raise RuntimeError(
                "claude CLI not found. Install Claude Code and ensure `claude` is on PATH."
            ) from exc
        except subprocess.TimeoutExpired as exc:
            raise RuntimeError(f"claude code agent timed out after {timeout}s") from exc
        if proc.returncode != 0:
            raise RuntimeError(
                f"claude code agent exited {proc.returncode}: "
                f"{(proc.stderr or proc.stdout or '')[:300]}"
            )
        try:
            return json.loads(proc.stdout)
        except Exception as exc:
            raise RuntimeError(
                f"claude code agent returned non-JSON: {proc.stdout[:300]}"
            ) from exc

    @staticmethod
    def _usage(data: dict, settings: Settings) -> tuple[dict, int]:
        u = data.get("usage") or {}
        prompt_tokens = (
            int(u.get("input_tokens") or 0)
            + int(u.get("cache_read_input_tokens") or 0)
            + int(u.get("cache_creation_input_tokens") or 0)
        )
        completion = int(u.get("output_tokens") or 0)
        model_usage = data.get("modelUsage") or {}
        primary = next((n for n in model_usage if "haiku" not in n), None)
        primary = primary or next(iter(model_usage), None) or (
            getattr(settings, "claude_cli_model", None) or "claude-code"
        )
        cost = data.get("total_cost_usd")
        cost_micros = int(round(float(cost) * 1_000_000)) if cost is not None else 0
        usage = {
            "model": primary,
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion,
            "total_tokens": prompt_tokens + completion,
            "cost_usd_micros": cost_micros,
        }
        return usage, cost_micros

    def run_role(
        self, prompt: str, cwd: Path, *, read_only: bool = False, model: Optional[str] = None
    ) -> dict:
        """Run a single role-scoped agent invocation against an existing workdir and
        return {summary, usage, cost_usd_micros, turns, model}. Used by the governed
        pipeline to dispatch each step as an ISOLATED context — a fresh `claude -p` process
        with no shared session state between roles. `model` routes low-cost vs strong."""
        data = self._invoke(prompt, cwd, read_only=read_only, model=model)
        usage, cost_micros = self._usage(data, self.settings)
        return {
            "summary": (data.get("result") or "").strip() or "(no summary returned)",
            "usage": usage,
            "cost_usd_micros": cost_micros,
            "turns": int(data.get("num_turns") or 0),
            "model": model or usage.get("model"),
        }

    def run(
        self, spec: CodeTaskSpec, repo: RepoContext, sandbox: EphemeralSandbox
    ) -> CodeAgentResult:
        if sandbox.path is None:
            raise RuntimeError("Sandbox must be provisioned before running the agent.")
        prompt = self._build_prompt(spec, repo)
        data = self._invoke(prompt, sandbox.path)
        usage, cost_micros = self._usage(data, self.settings)
        summary = (data.get("result") or "").strip() or "(no summary returned)"
        turns = int(data.get("num_turns") or 0)

        # Authoritative verification: BOSS re-runs the test commands itself.
        tests = [sandbox.run(cmd) for cmd in spec.test_commands]
        tests_passed = all(t.ok for t in tests) if tests else False

        diff_summary = sandbox.diff_summary()
        diff = sandbox.diff()

        return CodeAgentResult(
            summary=summary,
            diff=diff,
            diff_summary=diff_summary,
            tests=tests,
            tests_passed=tests_passed,
            cost_usd_micros=cost_micros,
            usage=usage,
            transcript=summary,
            turns=turns,
            engine=self.engine,
        )


# --------------------------------------------------------------------------- #
#  Native chat-API implementation (model-agnostic: OpenAI / Azure)            #
# --------------------------------------------------------------------------- #
class NativeCodeAgent:
    """Model-agnostic code agent on the raw chat API (OpenAI or Azure, via OpenAIAdapter).

    The host-side analogue of the guest native loop: the role prompt is the *guidance*; the
    model impersonates the role. It asks the model for whole-file writes as JSON, applies them
    in the sandbox working tree, and (for ``run``) re-runs the tests and iterates. No CLI or
    keychain dependency — works with any chat model the operator's lane routes to.
    """

    engine = "native"

    _EDIT_SYS = (
        "You are an expert engineer working in a checked-out git repository (files shown below). "
        'Reply ONLY as compact JSON: {"summary": str, "files": [{"path": str, "content": str}], '
        '"done": bool}. Each file is written in FULL (not a patch). Return an empty files list to '
        "make no changes. Output nothing outside the JSON."
    )
    _REVIEW_SYS = (
        "You are an expert reviewer working in a checked-out git repository (files shown below). "
        'Reply ONLY as compact JSON: {"summary": str}. Do NOT propose file changes. Put any '
        "required verdict line inside the summary string."
    )

    def __init__(self, settings: Settings):
        self.settings = settings
        from .openai_adapter import OpenAIAdapter

        self._llm = OpenAIAdapter(settings)

    def _repo_context(self, cwd: Path, *, limit: int = 40) -> str:
        exts = {".py", ".js", ".ts", ".go", ".java", ".rb", ".rs", ".json", ".md", ".txt",
                ".cfg", ".toml", ".yaml", ".yml", ".gradle", ".xml"}
        files: list[str] = []
        for p in sorted(cwd.rglob("*")):
            if not p.is_file() or ".git" in p.parts:
                continue
            files.append(p.relative_to(cwd).as_posix())
        blobs: list[str] = []
        for rel in files:
            fp = cwd / rel
            try:
                if fp.suffix in exts and fp.stat().st_size < 20000:
                    blobs.append(f"--- {rel} ---\n{fp.read_text(errors='ignore')}")
            except OSError:
                continue
        listing = "\n".join(files) or "(empty repo)"
        return f"REPO FILES:\n{listing}\n\nFILE CONTENTS:\n" + "\n\n".join(blobs[:limit])

    @staticmethod
    def _safe_json(content: str) -> dict:
        s = (content or "").strip()
        if s.startswith("```"):
            s = s.split("```", 2)[1] if "```" in s[3:] else s[3:]
            if s.startswith("json"):
                s = s[4:]
        try:
            return json.loads(s)
        except Exception:
            i, j = s.find("{"), s.rfind("}")
            if i != -1 and j > i:
                try:
                    return json.loads(s[i:j + 1])
                except Exception:
                    pass
            return {"summary": (content or "")[:400], "files": []}

    def _apply_files(self, cwd: Path, files: list) -> list[str]:
        written: list[str] = []
        for f in files or []:
            path = (f.get("path") or "").lstrip("/")
            if not path or ".." in path:
                continue
            fp = cwd / path
            fp.parent.mkdir(parents=True, exist_ok=True)
            fp.write_text(f.get("content", ""), encoding="utf-8")
            written.append(path)
        return written

    def run_role(
        self, prompt: str, cwd: Path, *, read_only: bool = False, model: Optional[str] = None
    ) -> dict:
        from .openai_adapter import estimate_cost_usd_micros

        system = self._REVIEW_SYS if read_only else self._EDIT_SYS
        msg, usage = self._llm.create_chat_completion(
            [{"role": "system", "content": system},
             {"role": "user", "content": f"{prompt}\n\n{self._repo_context(cwd)}"}],
            model_override=model or None,
        )
        data = self._safe_json(msg.get("content") or "")
        if not read_only:
            self._apply_files(cwd, data.get("files", []))
        return {
            "summary": (data.get("summary") or "").strip() or "(no summary)",
            "usage": usage,
            "cost_usd_micros": estimate_cost_usd_micros(usage),
            "turns": 1,
            "model": usage.get("model"),
        }

    def run(
        self, spec: CodeTaskSpec, repo: RepoContext, sandbox: EphemeralSandbox
    ) -> CodeAgentResult:
        if sandbox.path is None:
            raise RuntimeError("Sandbox must be provisioned before running the agent.")
        criteria = "\n".join(f"- {c}" for c in spec.acceptance_criteria) or "- (none)"
        tline = "\n".join(f"- {c}" for c in spec.test_commands) or "- (none)"
        base = (f"TASK: {spec.title}\n\n{spec.instructions}\n\nACCEPTANCE CRITERIA:\n{criteria}\n\n"
                f"TEST COMMANDS (make these pass):\n{tline}\n")
        summary = ""
        total_cost = 0
        usage_last: dict = {}
        feedback = ""
        turns = 0
        for _ in range(4):
            turns += 1
            r = self.run_role(
                base + (f"\n\nThe previous attempt's tests FAILED:\n{feedback}\nFix it."
                        if feedback else ""),
                sandbox.path,
            )
            summary = r["summary"] or summary
            total_cost += int(r.get("cost_usd_micros") or 0)
            usage_last = r.get("usage") or usage_last
            tests = [sandbox.run(cmd) for cmd in spec.test_commands]
            if (all(t.ok for t in tests) if tests else False):
                break
            feedback = "\n\n".join(
                f"$ {t.command} (exit {t.exit_code})\n{((t.stdout or '') + (t.stderr or ''))[-1500:]}"
                for t in tests if not t.ok
            )[:6000]
        tests = [sandbox.run(cmd) for cmd in spec.test_commands]
        tests_passed = all(t.ok for t in tests) if tests else False
        return CodeAgentResult(
            summary=summary or "(no summary)", diff=sandbox.diff(),
            diff_summary=sandbox.diff_summary(), tests=tests, tests_passed=tests_passed,
            cost_usd_micros=total_cost, usage=usage_last, transcript=summary,
            turns=turns, engine=self.engine,
        )


def make_code_agent(settings: Settings) -> CodeAgent:
    engine = (settings.code_agent_engine or "claude_code").lower()
    if engine == "claude_code":
        return ClaudeCodeAgent(settings)
    if engine in ("native", "openai", "azure"):
        return NativeCodeAgent(settings)
    raise NotImplementedError(f"Unknown code_agent_engine: {settings.code_agent_engine!r}")
