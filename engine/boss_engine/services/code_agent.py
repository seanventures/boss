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
import time
from typing import Optional, Protocol

from ..config import Settings
from ..schemas import FileChange
from .llm_telemetry import record_llm_call
from .model_rate_limiter import (
    NullRateLimiter,
    get_rate_limiter,
    is_rate_limit_error,
    provider_of,
)
from . import repo_retriever
from .redaction import redact as _redact_pii
from .sandbox import CommandResult, DiffSummary, EphemeralSandbox


# Core Terraform module files (spec 05 §3.2 / T4 "module-aware context packer"): a
# module is cross-referential, so these are ALWAYS shown to the model — past the
# 40-blob cap, and (truncated, with a marker) past the per-file size cap.
_TF_CORE_BASENAMES = {"main.tf", "variables.tf", "outputs.tf", "versions.tf", "providers.tf"}


def _is_core_tf_file(rel_path: str) -> bool:
    base = rel_path.rsplit("/", 1)[-1]
    return base in _TF_CORE_BASENAMES or base.endswith(".tfvars")


def _as_text(v) -> str:
    """Coerce a model's summary/result field to text. Models sometimes return a JSON
    list (e.g. a bulleted list) or object instead of a string; don't crash on it."""
    if isinstance(v, str):
        return v
    if isinstance(v, list):
        return "\n".join(_as_text(x) for x in v)
    if isinstance(v, dict):
        return json.dumps(v)
    return "" if v is None else str(v)


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
        self, prompt: str, cwd: Path, *, read_only: bool = False, model: Optional[str] = None,
        allowed_tools: Optional[str] = None,
    ) -> dict:
        timeout = int(self.settings.code_agent_timeout_seconds or 900)
        # A full web build (in-container / local_subprocess) needs well beyond the legacy 900s that
        # the prod env still pins (BOSS_CODE_AGENT_TIMEOUT_SECONDS=900) — a big single-page site was
        # dying mid-implement. Floor the BUILD timeout so it can finish; review stages stay capped.
        if not read_only:
            timeout = max(timeout, 2400)
        if read_only:
            # A review shouldn't run as long as an implementation — cap it tighter so a slow
            # reviewer can't stall the panel.
            timeout = min(timeout, int(getattr(self.settings, "code_agent_review_timeout_seconds", 420) or 420))
        # Review stages run read-only (no Edit/Write tools, no auto-accept) so a reviewer
        # cannot mutate the change it is judging. `allowed_tools` lets non-coding callers
        # (e.g. the Discovery research pipeline) opt into WebSearch/WebFetch instead.
        if allowed_tools is not None:
            allowed = allowed_tools
        else:
            allowed = "Read,Grep,Glob,Bash" if read_only else self.settings.code_agent_allowed_tools
        permission = "default" if read_only else "acceptEdits"
        _max_turns = int(self.settings.code_agent_max_turns or 40)
        if read_only:
            # Reviews read + judge; they don't need the full edit-loop turn budget.
            _max_turns = min(_max_turns, int(getattr(self.settings, "code_agent_review_max_turns", 16) or 16))
        cmd = [
            self._claude_bin(),
            "-p", prompt,
            "--output-format", "json",
            "--max-turns", str(_max_turns),
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
            # The CLI still prints its result JSON on stdout when it exits
            # non-zero; the human-readable reason lives in `result` (e.g.
            # "You've hit your monthly spend limit …"). Surface THAT instead of
            # an opaque truncated JSON prefix so the console error is legible.
            detail = (proc.stderr or proc.stdout or "")[:300]
            try:
                data = json.loads(proc.stdout)
            except Exception:
                data = None
            if isinstance(data, dict):
                reason = str(data.get("result") or data.get("error") or "").strip()
                subtype = str(data.get("subtype") or data.get("stop_reason") or "").strip()
                if reason or subtype:
                    detail = " — ".join(p for p in (subtype, reason[:220]) if p)
            raise RuntimeError(
                f"claude code agent exited {proc.returncode}: {detail}"
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
        # Cache tokens tracked separately (BOSS-280 P1) — see claude_cli_adapter._usage.
        prompt_tokens = int(u.get("input_tokens") or 0)
        cache_read = int(u.get("cache_read_input_tokens") or 0)
        cache_write = int(u.get("cache_creation_input_tokens") or 0)
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
            "cache_read_tokens": cache_read,
            "cache_write_tokens": cache_write,
            "total_tokens": prompt_tokens + cache_read + cache_write + completion,
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
        if getattr(self, "redact", False):  # data-boundary: mask PII for restricted tasks
            prompt = _redact_pii(prompt)[0]
        started = time.monotonic()
        try:
            data = self._invoke(prompt, cwd, read_only=read_only, model=model)
        except Exception as exc:
            failed_model = model or getattr(self.settings, "claude_cli_model", None) or ""
            record_llm_call(engine_name=self.engine, usage={"model": failed_model},
                            latency_ms=int((time.monotonic() - started) * 1000),
                            ok=False, error=str(exc))
            raise
        usage, cost_micros = self._usage(data, self.settings)
        turns = int(data.get("num_turns") or 0)
        record_llm_call(engine_name=self.engine, usage=usage, cost_usd_micros=cost_micros,
                        latency_ms=int((time.monotonic() - started) * 1000), turns=turns)
        return {
            "summary": _as_text(data.get("result")).strip() or "(no summary returned)",
            "usage": usage,
            "cost_usd_micros": cost_micros,
            "turns": turns,
            "model": model or usage.get("model"),
        }

    def run_research(self, prompt: str, *, model: Optional[str] = None) -> dict:
        """Web-enabled, read-only research invocation: `claude -p` with WebSearch/WebFetch
        (no repo, no edits). Used by the Discovery pipeline to gather real market signals.
        Returns {summary, usage, cost_usd_micros, turns, model}."""
        import tempfile
        work = Path(tempfile.gettempdir())
        started = time.monotonic()
        data = self._invoke(
            prompt, work, read_only=True, model=model,
            allowed_tools="WebSearch,WebFetch,Read,Grep,Glob",
        )
        usage, cost_micros = self._usage(data, self.settings)
        record_llm_call(engine_name=self.engine, usage=usage, cost_usd_micros=cost_micros,
                        latency_ms=int((time.monotonic() - started) * 1000),
                        turns=int(data.get("num_turns") or 0), stage="research")
        return {
            "summary": _as_text(data.get("result")).strip() or "",
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
        started = time.monotonic()
        data = self._invoke(prompt, sandbox.path)
        usage, cost_micros = self._usage(data, self.settings)
        summary = _as_text(data.get("result")).strip() or "(no summary returned)"
        turns = int(data.get("num_turns") or 0)
        record_llm_call(engine_name=self.engine, usage=usage, cost_usd_micros=cost_micros,
                        latency_ms=int((time.monotonic() - started) * 1000), turns=turns)

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
DIFF_REVIEW_SYSTEM_PROMPT = (
    "You are BOSS's read-only code-review role. Review only the supplied unified diff; "
    "never ask to check out or execute contributor code. Return compact JSON with keys "
    "summary (string), value_verdict (worth_reviewing or needs_revision), and roi_notes "
    "(array of strings). Focus on correctness, security, tests, maintenance cost, and product value."
)


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

    def _repo_context(self, cwd: Path, *, task: str = "", limit: int = 120) -> str:
        # IaC authoring (spec 05 §3.2 / T4): .tf/.tfvars/.hcl are context. APP-5 (#95): frontend exts
        # added so the design system is visible, and selection is retrieval-driven (pin the design
        # source-of-truth + Terraform core, rank the rest by relevance) instead of first-N-by-path.
        exts = {".py", ".js", ".ts", ".tsx", ".jsx", ".go", ".java", ".rb", ".rs", ".json", ".md",
                ".txt", ".cfg", ".toml", ".yaml", ".yml", ".gradle", ".xml", ".css", ".scss",
                ".html", ".vue", ".svelte", ".tf", ".tfvars", ".hcl"}
        skip = {"node_modules", "dist", "build", ".next", ".git", ".venv", "venv",
                "__pycache__", "vendor", "target", "coverage", ".turbo"}
        files: list[str] = []
        entries: list[dict] = []
        for p in sorted(cwd.rglob("*")):
            if not p.is_file() or skip & set(p.parts):
                continue
            rel = p.relative_to(cwd).as_posix()
            files.append(rel)
            if p.suffix not in exts:
                continue
            try:
                entries.append({"rel": rel, "content": p.read_text(errors="ignore"),
                                "size": p.stat().st_size})
            except OSError:
                continue
        # Pin the design-system source of truth (when the repo has one) so a UI task always sees the
        # tokens + component kit; Terraform core stays pinned too. The 40/20KB caps are gone as the
        # selection mechanism — max_files/max_bytes are a large safety ceiling.
        ui = any(repo_retriever.is_design_file(e["rel"]) for e in entries)
        selection = repo_retriever.select_files(entries, task, ui=ui, pin=_is_core_tf_file,
                                                 max_files=limit)
        return repo_retriever.render_context(files, selection)

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
        started = time.monotonic()
        try:
            msg, usage = self._llm.create_chat_completion(
                [{"role": "system", "content": system},
                 {"role": "user", "content": f"{prompt}\n\n{self._repo_context(cwd, task=prompt)}"}],
                model_override=model or None,
                redact=getattr(self, "redact", False),  # data-boundary: mask PII for restricted tasks
            )
        except Exception as exc:
            record_llm_call(engine_name=self.engine, usage={"model": model or ""},
                            latency_ms=int((time.monotonic() - started) * 1000),
                            ok=False, error=str(exc))
            raise
        cost_micros = estimate_cost_usd_micros(usage)
        record_llm_call(engine_name=self.engine, usage=usage, cost_usd_micros=cost_micros,
                        latency_ms=int((time.monotonic() - started) * 1000), turns=1)
        data = self._safe_json(msg.get("content") or "")
        if not read_only:
            self._apply_files(cwd, data.get("files", []))
        return {
            "summary": _as_text(data.get("summary")).strip() or "(no summary)",
            "usage": usage,
            "cost_usd_micros": cost_micros,
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


# Direct-provider lanes: each is a NativeCodeAgent bound to that provider's DEDICATED
# credential pair — never the platform-managed OPENAI_* pair (broker-owned on managed
# deployments), and never Azure precedence. These are the VAG cheap-model lanes: route
# steps with e.g. "openrouter:deepseek/deepseek-chat" or "groq:qwen/qwen3-32b".
_PROVIDER_LANES: dict[str, tuple[str, str]] = {
    # engine name -> (api-key settings field, base-url settings field)
    "openrouter": ("openrouter_api_key", "openrouter_base_url"),
    "groq": ("groq_api_key", "groq_base_url"),
    "deepseek": ("deepseek_api_key", "deepseek_base_url"),
    # Local / self-hosted lanes (OSS): same mechanics, no cloud credential.
    # ollama defaults to the local daemon; vllm requires VLLM_BASE_URL.
    "ollama": ("ollama_api_key", "ollama_base_url"),
    "vllm": ("vllm_api_key", "vllm_base_url"),
}


class PacedCodeAgent:
    """Wraps a CodeAgent so every model invocation goes through the rate limiter (#111):
    bounded concurrency + pacing + adaptive backoff, keyed by the agent's provider. Delegates
    every other attribute to the wrapped agent, so it's a transparent stand-in."""

    def __init__(self, inner: "CodeAgent", limiter):
        self._inner = inner
        self._limiter = limiter
        self.engine = getattr(inner, "engine", None)
        self._provider = provider_of(self.engine)

    def __getattr__(self, name):  # delegate anything not overridden (called only on miss)
        return getattr(self._inner, name)

    def _paced(self, fn, *args, **kwargs):
        with self._limiter.slot(self._provider):
            try:
                out = fn(*args, **kwargs)
            except Exception as exc:  # noqa: BLE001 — classify then re-raise unchanged
                if is_rate_limit_error(exc):
                    self._limiter.report_rate_limited(self._provider)
                raise
            self._limiter.report_ok(self._provider)
            return out

    def run_role(self, *args, **kwargs):
        return self._paced(self._inner.run_role, *args, **kwargs)

    def run_research(self, *args, **kwargs):
        return self._paced(self._inner.run_research, *args, **kwargs)


def _wrap_paced(agent: "CodeAgent", settings: Settings) -> "CodeAgent":
    limiter = get_rate_limiter(settings)
    if isinstance(limiter, NullRateLimiter):  # burst mode: no wrapper overhead
        return agent
    return PacedCodeAgent(agent, limiter)


def make_code_agent(settings: Settings, engine: Optional[str] = None) -> CodeAgent:
    """Build a CodeAgent. ``engine`` overrides ``settings.code_agent_engine`` so the
    governed pipeline can run builder roles on one model and adversary roles on another.
    The returned agent is paced by the model rate limiter (#111) unless burst mode is on."""
    return _wrap_paced(_build_code_agent(settings, engine), settings)


def _build_code_agent(settings: Settings, engine: Optional[str] = None) -> CodeAgent:
    engine = (engine or settings.code_agent_engine or "claude_code").lower()
    if engine == "claude_code":
        return ClaudeCodeAgent(settings)
    if engine == "azure":
        # Explicit Azure OpenAI frontier lane — interchangeable with claude_code as the
        # gate/verifier (VAG). NativeCodeAgent -> OpenAIAdapter routes to the AzureOpenAI
        # client whenever the AZURE_OPENAI_* creds are present; require them here so a
        # misrouted `azure` step fails loud instead of silently falling to a non-Azure host.
        if not (settings.azure_openai_key and settings.azure_openai_endpoint
                and settings.azure_openai_deployment):
            raise RuntimeError(
                "azure engine requires AZURE_OPENAI_KEY, AZURE_OPENAI_ENDPOINT and "
                "AZURE_OPENAI_DEPLOYMENT to be configured.")
        agent = NativeCodeAgent(settings)
        agent.engine = "azure"
        return agent
    if engine in ("native", "openai"):
        return NativeCodeAgent(settings)
    if engine in _PROVIDER_LANES:
        key_field, url_field = _PROVIDER_LANES[engine]
        api_key = getattr(settings, key_field)
        if not api_key:
            raise RuntimeError(f"{key_field.upper()} is not configured.")
        if not getattr(settings, url_field):
            raise RuntimeError(f"{url_field.upper()} is not configured.")
        bound = settings.model_copy(update={
            "openai_api_key": api_key,
            "openai_base_url": getattr(settings, url_field),
            "azure_openai_key": None,
            "azure_openai_endpoint": None,
            "azure_openai_deployment": None,
        })
        agent = NativeCodeAgent(bound)
        agent.engine = engine
        return agent
    raise NotImplementedError(f"Unknown code_agent_engine: {engine!r}")
