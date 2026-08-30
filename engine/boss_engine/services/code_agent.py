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
import os
import re
from pathlib import Path
import subprocess
import threading
import logging
import time
from typing import Optional, Protocol

from ..config import Settings
from ..schemas import FileChange
from .agent_turns import StreamParser, capture_enabled, parse_stream
from .llm_telemetry import record_agent_turns, record_llm_call


_log = logging.getLogger(__name__)


def _argv_safe(prompt: str) -> str:
    """Strip NUL bytes from a prompt before it becomes a command-line argument.

    A NUL cannot appear in an argv entry: `subprocess.Popen` rejects the WHOLE command with
    "embedded null byte" before the process is even started. Measured on a real run — the agent
    built a SQLite-backed app, a binary blob reached the review prompt through the diff, and all
    THREE reviewers (security, consistency, adversarial) failed in 2ms each. The review panel
    silently did not run, and the only trace was a review_panel phase that took 20 seconds
    instead of minutes.

    Dropping the byte is the right repair, not escaping it: a NUL carries no meaning to a model,
    it is an artefact of binary content leaking into text. Everything around it is still
    perfectly readable, and a review that happens on slightly-cleaned text beats a review that
    does not happen at all.
    """
    if "\x00" not in (prompt or ""):
        return prompt
    cleaned = prompt.replace("\x00", "")
    _log.warning(
        "prompt contained %d NUL byte(s) — stripped before dispatch. Binary content reached a "
        "prompt (usually a binary file in the diff); the invocation would otherwise have died "
        "instantly with 'embedded null byte' and the stage would not have run at all.",
        len(prompt) - len(cleaned),
    )
    return cleaned


def _drain(child, *, timeout: Optional[float], on_line=None) -> tuple[str, str]:
    """Read a child's stdout/stderr as they arrive, returning the full text of each.

    Same contract as ``communicate(timeout=...)`` — including raising ``TimeoutExpired`` — with
    one addition: every stdout line is handed to ``on_line`` the moment it is read.

    BOTH pipes are drained concurrently. Reading one to completion while the other fills is the
    classic subprocess deadlock: the child blocks writing to a full stderr buffer, we block
    reading a stdout that will never advance, and the build hangs until its timeout with no way
    to tell that from slow work. That is the failure mode this whole change exists to remove,
    so it must not be reintroduced by the fix.
    """
    out_lines: list[str] = []
    err_chunks: list[str] = []

    def _pump(stream, sink, live: bool) -> None:
        try:
            for line in iter(stream.readline, ""):
                sink.append(line)
                if live and on_line is not None:
                    try:
                        on_line(line)
                    except Exception:  # noqa: BLE001 — a consumer must never kill the build
                        pass
        except Exception:  # noqa: BLE001 — a broken pipe ends this reader, nothing more
            pass
        finally:
            try:
                stream.close()
            except Exception:  # noqa: BLE001
                pass

    threads = [
        threading.Thread(target=_pump, args=(child.stdout, out_lines, True),
                         name="claude-stdout", daemon=True),
        threading.Thread(target=_pump, args=(child.stderr, err_chunks, False),
                         name="claude-stderr", daemon=True),
    ]
    for t in threads:
        t.start()
    try:
        child.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        raise
    # The process has exited; its writers are closed, so the readers finish promptly. Bounded
    # anyway — a wedged reader must not become a new way for a build to hang forever.
    for t in threads:
        t.join(timeout=30)
    return "".join(out_lines), "".join(err_chunks)


def _parse_cli_output(stdout: str) -> Optional[dict]:
    """The CLI's result object, from either output format.

    Returns None when stdout is neither a single JSON object nor a stream containing a terminal
    result event — the caller turns that into the same "returned non-JSON" error it always did.

    Captured turns ride along under `_turns`, a private key the callers pop off before anyone
    else sees the dict. Threading them through a second return value would have meant changing
    every call site of a function that sits in the middle of the build pipeline.
    """
    if not (stdout or "").strip():
        return None
    try:
        data = json.loads(stdout)
        if isinstance(data, dict):
            return data                      # classic --output-format json
    except Exception:
        pass
    result, turns = parse_stream(stdout)
    if result is None:
        return None
    result["_turns"] = turns
    return result
from .scheduling import low_priority
from .model_rate_limiter import (

    NullRateLimiter,
    get_rate_limiter,
    is_rate_limit_error,
    provider_of,
)
from . import repo_retriever
from .redaction import redact as _redact_pii
from .sandbox import (
    CommandResult, DiffSummary, EphemeralSandbox, _shared_tool_caches,
)

# --------------------------------------------------------------------------- #
# Which agent subprocesses are OURS, and what seat each one occupies.
# --------------------------------------------------------------------------- #
# `_kill_stray_review_processes` reaps review seats that outlived their deadline by scanning
# `ps` for a claude binary under a sandbox root. It cannot tell a review seat from an IMPLEMENT
# agent — they are the same binary under the same root — so it killed a build that was working:
#
#     claude code agent exited -9: [substrate=local_subprocess]
#
# Its own docstring records the same bug twice before, each time patched by tightening the
# pattern. The pattern was never the problem. A process is safe to kill because of WHO STARTED
# IT AND WHY, which no amount of matching on a command line can establish.
_ACTIVE_SEATS: dict[int, str] = {}
_SEATS_LOCK = threading.Lock()


def register_seat(pid: int, seat: str) -> None:
    with _SEATS_LOCK:
        _ACTIVE_SEATS[pid] = seat


def release_seat(pid: int) -> None:
    with _SEATS_LOCK:
        _ACTIVE_SEATS.pop(pid, None)


def seat_of(pid: int) -> Optional[str]:
    """The seat this pid occupies, or None if we did not start it."""
    with _SEATS_LOCK:
        return _ACTIVE_SEATS.get(pid)


def is_protected(pid: int) -> bool:
    """True when killing this pid would abort work that is legitimately running.

    Anything we started that is NOT a review seat — an implement agent above all.
    """
    seat = seat_of(pid)
    return seat is not None and seat != "review"



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


# Appended to EVERY agent invocation. Measured across four runs, the agent issued exactly
# 1.00 tool calls per turn — 65, 114, 59 and 51 turns, never once batching. Each turn is a
# full model round trip (~8-12s), so reading 19 files cost 19 round trips and ~8 minutes of
# pure latency. Interactive Claude Code reads the same repo in 2-3 minutes because it fires
# several reads in ONE turn. Nothing in BOSS ever asked for that, and headless `claude -p`
# defaults to one-at-a-time. This is the single largest latency item in the pipeline and it
# costs nothing to fix: the calls are independent, so issuing them together is free.
_PARALLEL_TOOLS_RULE = (
    "\n\nTOOL EFFICIENCY (important):\n"
    "- When you need several INDEPENDENT pieces of information, issue those tool calls "
    "TOGETHER IN ONE TURN — e.g. read 5 files in a single turn, not 5 turns. Every turn is a "
    "network round trip; batching independent reads is the difference between seconds and "
    "minutes.\n"
    "- Prefer Grep/Glob to locate code over opening files speculatively one by one.\n"
    "- Only serialize when a call genuinely depends on the previous result.\n"
    # 40 of 110 turns in a measured run called NO tool at all — pure narration between
    # actions, ~9.2s each, the single largest time bucket after the round trips themselves.
    # The work is judged by the diff and by BOSS re-running the tests, never by the prose.
    "- Do NOT narrate your plan between actions. BOSS judges the DIFF and re-runs the tests "
    "itself; commentary costs a full round trip and is never read. Act, then summarise ONCE "
    "at the end.\n"
)


_SHARED_ROOT_HINTS = ("readme", "package.json", "pyproject", "requirements", "makefile",
                      "docker", ".gitignore", "conftest", "setup.cfg", "tsconfig")


def _scope_to_task_app(entries: list[dict], files: list[str], task: str) -> tuple[list[dict], list[str]]:
    """Restrict context to the app the task is ABOUT, when the repo holds several.

    boss-sandbox now carries two applications. For "Expand N-Queens app to support 1-10
    queens" the packer offered 26 N-Queens files AND 18 Sudoku ones, so ~40% of the agent's
    context was an application it had no business reading — and it spent 8 minutes and 30
    exploration calls orienting. Generic term-overlap scores sudoku_core/profile.py about as
    highly as nqueens_core/profile.py.

    When task words match top-level directories, keep those directories (plus genuinely
    shared root files) and drop the sibling apps. If nothing matches, change nothing.
    """
    words = {w for w in re.split(r"[^a-z0-9]+", (task or "").lower()) if len(w) > 3}
    if not words:
        return entries, files
    tops = {f.split("/", 1)[0] for f in files if "/" in f}
    matched = {
        top for top in tops
        for w in words
        if w in re.sub(r"[^a-z0-9]+", "", top.lower()) or re.sub(r"[^a-z0-9]+", "", top.lower()) in w
    }
    if not matched or matched == tops:
        return entries, files          # nothing to disambiguate

    # Name fragments of the OTHER apps, so their files can be spotted inside shared dirs.
    # The shared dirs THEMSELVES must be excluded: `tests` is in `tops` and unmatched, so
    # including it made every path under tests/ look like a sibling app's file and dropped
    # the named app's own tests (tests/test_nqueens_board.py) from an N-Queens task.
    _shared = {"tests", "test", "spec", "docs", "doc", "scripts"}
    other_tokens = {re.sub(r"[^a-z0-9]+", "", t.lower()).replace("core", "").replace("web", "")
                    for t in (tops - matched - _shared)}
    other_tokens = {t for t in other_tokens if len(t) > 3}

    def keep(rel: str) -> bool:
        low = rel.lower()
        top = rel.split("/", 1)[0]
        if "/" not in rel:             # root-level file
            return True
        if top in matched:
            return True
        if any(h in low for h in _SHARED_ROOT_HINTS):
            return True
        if top in ("tests", "test", "spec", "docs"):
            # Shared test/doc dirs hold BOTH apps' files (tests/test_nqueens_*.py next to
            # tests/test_streak.py), so keeping the whole directory re-imports the sibling app
            # through the back door. Drop the ones clearly named for another app.
            return not any(tok in low for tok in other_tokens)
        return top not in tops

    kept_entries = [e for e in entries if keep(e["rel"])]
    kept_files = [f for f in files if keep(f)]
    return (kept_entries or entries), (kept_files or files)


def _pack(
    cwd: Path,
    *,
    task: str = "",
    limit: int = 120,
    max_bytes: int = 400_000,
    head_bytes: int = 20_000,
) -> str:
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
    # Scope to the app the task names BEFORE ranking, so a sibling application cannot
    # consume half the context budget.
    entries, files = _scope_to_task_app(entries, files, task)
    ui = any(repo_retriever.is_design_file(e["rel"]) for e in entries)
    selection = repo_retriever.select_files(
        entries,
        task,
        ui=ui,
        pin=_is_core_tf_file,
        max_files=limit,
        max_bytes=max_bytes,
        head_bytes=head_bytes,
    )
    return repo_retriever.render_context(files, selection)


def build_repo_context(
    cwd: Path,
    *,
    task: str = "",
    limit: int = 120,
    max_bytes: int = 400_000,
    head_bytes: int = 20_000,
) -> str:
    """Relevance-ranked repo context for a task.

    BOSS has owned a module-aware context packer since spec 09, but only NativeCodeAgent
    used it — ClaudeCodeAgent started every stage with a bare prompt and rediscovered the
    repository with its own tools. Measured on the implement stage of crun_08c43028:
    ~27 of its tool calls were exploration (Read/ls/cat) against 16 real edits. That
    rediscovery is paid again by every stage of every run.
    """
    return _pack(
        cwd, task=task, limit=limit, max_bytes=max_bytes, head_bytes=head_bytes
    )


class CodeAgent(Protocol):
    def run(
        self, spec: CodeTaskSpec, repo: RepoContext, sandbox: EphemeralSandbox
    ) -> CodeAgentResult: ...

    def run_role(
        self, prompt: str, cwd: Path, *, read_only: bool = False, model: Optional[str] = None,
        resume_session_id: Optional[str] = None, max_turns: Optional[int] = None,
    ) -> dict:
        """Run one isolated, role-scoped invocation against an existing workdir.
        Returns {summary, usage, cost_usd_micros, turns, model, session_id?}. The role
        prompt is the guidance; the model impersonates the role. read_only stages must
        not edit files; model overrides which model runs this step (low-cost vs strong
        routing). resume_session_id continues a prior SAME-role session (cache reuse)."""
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

    def _build_prompt(self, spec: CodeTaskSpec, repo: RepoContext,
                      cwd: Optional[Path] = None) -> str:
        criteria = "\n".join(f"- {c}" for c in spec.acceptance_criteria) or "- (none specified)"
        tests = "\n".join(f"- {c}" for c in spec.test_commands) or "- (none specified)"
        guide = f"\n\nREPO GUIDE:\n{repo.repo_guide}\n" if repo.repo_guide else ""
        # Hand the agent a relevance-ranked map of the repo instead of making it discover the
        # codebase with Read/ls/cat on every run. Bounded (a focused pack, not the whole tree)
        # so it orients the agent rather than flooding the context it re-sends each turn.
        ctx = ""
        limit = int(getattr(self.settings, "code_agent_context_files", 14) or 0)
        from .openai_adapter import groq_qwen_context_limits

        limit, max_bytes, head_bytes = groq_qwen_context_limits(
            getattr(self.settings, "openai_model", None),
            base_url=str(getattr(self.settings, "openai_base_url", "") or ""),
            engine=str(getattr(self, "engine", "") or ""),
            file_limit=limit,
        )
        if cwd is not None and limit > 0:
            try:
                packed = build_repo_context(
                    cwd,
                    task=f"{spec.title}\n{spec.instructions}",
                    limit=limit,
                    max_bytes=max_bytes,
                    head_bytes=head_bytes,
                )
            except Exception:
                packed = ""
            if packed:
                ctx = ("\n\nREPO CONTEXT (pre-selected as most relevant — start here rather "
                       "than exploring from scratch; open anything else you need):\n" + packed + "\n")
        guide = ctx + guide
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
        allowed_tools: Optional[str] = None, resume_session_id: Optional[str] = None,
        max_turns: Optional[int] = None, seat: str = "agent",
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
        if max_turns:
            # Explicit per-invocation budget. A reviewer that exhausted its turns returns NO
            # verdict, and a verdict-less review used to read as "the code failed review" — so
            # the caller escalates the budget and re-runs the REVIEW rather than repairing code
            # that was never actually judged.
            _max_turns = int(max_turns)
            timeout = max(timeout, min(3600, int(max_turns) * 30))
        # STREAM THE TURNS, OR DO NOT.
        #
        # `--output-format json` returns ONE object: the final result, a token count and
        # "num_turns: 45". That is the whole reason the longest phase of every build was
        # unobservable — forty-five turns of reasoning, edits and test runs reduced to a
        # number. `stream-json` emits a line per turn AND still ends with the identical result
        # event, so the transcript costs nothing but parsing.
        #
        # Behind a switch that defaults ON but degrades to the old shape: `_parse_cli_output`
        # accepts either form, so if a CLI version stops streaming, builds keep working and
        # only the transcript is lost.
        capturing = capture_enabled()
        model = model or getattr(self.settings, "claude_cli_model", None)

        def base_cmd(stream: bool) -> list[str]:
            """The argv for one attempt. Rebuilt per attempt so capture can be dropped."""
            argv = [
                self._claude_bin(),
                "-p",
                # `stream-json` emits a line per turn AND still ends with the identical result
                # event `--output-format json` would have returned, which is why the transcript
                # could be added without changing a single caller in the build pipeline.
                *(("--output-format", "stream-json", "--verbose")
                  if stream else ("--output-format", "json")),
                "--max-turns", str(_max_turns),
                "--allowedTools", allowed,
                "--permission-mode", permission,
            ]
            if resume_session_id:
                # Token optimization: continue the prior session instead of cold-starting —
                # the follow-up rides the warm prompt cache (repo context + conversation are
                # cache READS, not fresh ingestion). Used by the repair loop.
                argv += ["--resume", resume_session_id]
            if model:
                argv += ["--model", model]
            # `--` ends option parsing: a prompt that BEGINS with a dash (e.g. a
            # "--- SECTION ---" header) must never be read as a CLI option.
            # Single choke point: every stage (build AND review) goes through _invoke, so the
            # batching rule is applied uniformly here rather than in each prompt builder.
            return argv + ["--", _argv_safe(prompt) + _PARALLEL_TOOLS_RULE]
        last_error = ""

        def _live_sink(live_model: str):
            """Persist turns AS THEY ARRIVE, so a running stage can be watched.

            Returns (on_line, parser). One parser per attempt: a retry re-runs the CLI from
            scratch, and carrying tool-id state across that would attribute one attempt's tool
            results to another attempt's calls.

            Everything here is best-effort. Live tracing is a diagnostic, and a diagnostic that
            can fail a build is worse than no diagnostic — the final `record_agent_turns` on
            the complete stdout remains the source of truth either way.
            """
            parser = StreamParser()
            # Turn -1, written before the first line arrives: "what was it asked" is the first
            # question anyone has, and it should not wait for the answer.
            try:
                record_agent_turns([], engine_name=self.engine, model=live_model,
                                   prompt=prompt)
            except Exception:  # noqa: BLE001
                pass

            def _on_line(line: str) -> None:
                produced = parser.feed(line)
                if not produced:
                    return
                try:
                    record_agent_turns(produced, engine_name=self.engine, model=live_model)
                except Exception:  # noqa: BLE001
                    pass

            return _on_line, parser

        for attempt in range(3):
            # CAPTURE MUST NEVER COST A BUILD.
            #
            # `stream-json` is the only reason this function's flags changed, and it runs on
            # every customer build. If a CLI version ever rejects the flag or emits a stream
            # this cannot read, the right outcome is a build that succeeds without a
            # transcript — not a build that fails with one. `capturing` is cleared below on the
            # first such failure and these flags rebuild without it.
            cmd = base_cmd(capturing)
            # Live tracing only makes sense while we are asking for the stream format. Without
            # `capturing` there are no per-turn events to read.
            on_line, live_parser = (
                _live_sink(model or getattr(self.settings, "claude_cli_model", "") or "")
                if capturing and capture_enabled() else (None, None)
            )
            try:
                # Popen rather than run(), so the pid is known and can be registered. A
                # reaper that scans `ps` cannot tell which seat a claude process occupies;
                # this is how it finds out.
                # Yields CPU/IO to the control plane it shares a container with. See
                # services/scheduling.py: this is scheduling WEIGHT under contention, not a
                # change to what runs — with nothing else competing it is exactly as fast.
                child = subprocess.Popen(
                    low_priority(cmd),
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                    cwd=str(cwd),
                    # THE AGENT GETS THE SHARED TOOL CACHE TOO.
                    #
                    # `sandbox.run()` has always pointed npm and Playwright at a cache that
                    # outlives one sandbox; this subprocess did not, because it was spawned
                    # with no env at all and inherited the worker's. So the pipeline's own
                    # `playwright install chromium` finished in 11.6s from the warm cache while
                    # the AGENT — running the same command inside a fresh sandbox, which is
                    # where nearly all of that time is actually spent — re-downloaded Chromium
                    # from scratch on every build. Measured on a real run: ~30 of 150 turns
                    # went on installing dependencies and waiting for a browser download,
                    # including a background install the agent then had to poll with `ps`.
                    env={**os.environ, **_shared_tool_caches()},
                )
                register_seat(child.pid, seat)
                try:
                    # READ THE STREAM AS A STREAM.
                    #
                    # `communicate()` blocks until the process exits and hands back the whole
                    # buffer, so a 27-minute implement was a black box for 27 minutes and a
                    # complete transcript afterwards. We were asking the CLI for
                    # `--output-format stream-json` — one JSON object per turn, emitted AS the
                    # turn happens — and then discarding the only property that makes it a
                    # stream. "What is it doing right now" had no answer for the operator, and
                    # none for BOSS either.
                    out, err = _drain(child, timeout=timeout, on_line=on_line)
                except subprocess.TimeoutExpired:
                    child.kill()
                    child.communicate()
                    raise
                finally:
                    release_seat(child.pid)
                proc = subprocess.CompletedProcess(cmd, child.returncode, out, err)
            except FileNotFoundError as exc:
                raise RuntimeError(
                    "claude CLI not found. Install Claude Code and ensure `claude` is on PATH."
                ) from exc
            except subprocess.TimeoutExpired as exc:
                raise RuntimeError(f"claude code agent timed out after {timeout}s") from exc
            if proc.returncode == 0:
                parsed = _parse_cli_output(proc.stdout)
                if parsed is not None and live_parser is not None and live_parser.turns:
                    # ALREADY PERSISTED, TURN BY TURN, WHILE THE STAGE RAN. Handing the same
                    # turns back would write every row twice. The flag tells the caller the
                    # transcript (including the prompt row) is already on disk.
                    parsed["_turns"] = []
                    parsed["_turns_live"] = True
                if parsed is None:
                    if capturing:
                        # Succeeded, but produced output this cannot read. That is a capture
                        # problem, not a build problem: drop the transcript and re-run plainly.
                        capturing = False
                        last_error = "unreadable stream output; retried without capture"
                        continue
                    raise RuntimeError(
                        f"claude code agent returned non-JSON: {proc.stdout[:300]}"
                    )
                return parsed
            # Non-zero exit: the CLI still prints its result JSON on stdout; the
            # human-readable reason lives in `result` (e.g. "You've hit your monthly
            # spend limit …"). Surface THAT instead of an opaque JSON prefix.
            detail = (proc.stderr or proc.stdout or "")[:300]
            data = _parse_cli_output(proc.stdout)
            if isinstance(data, dict):
                if data.get("subtype") == "error_max_turns":
                    # Ran out of turns ≠ failed: it's a PARTIAL attempt. Return it like a
                    # normal result so (a) the spend is recorded instead of vanishing from
                    # the ledger, and (b) the governed repair loop can run the authoritative
                    # tests, gather evidence, and RESUME this session to finish the work.
                    data.setdefault(
                        "result",
                        "(ran out of turns before finishing — partial attempt; resume to continue)",
                    )
                    return data
                reason = str(data.get("result") or data.get("error") or "").strip()
                subtype = str(data.get("subtype") or data.get("stop_reason") or "").strip()
                if reason or subtype:
                    detail = " — ".join(p for p in (subtype, reason[:220]) if p)
            last_error = f"claude code agent exited {proc.returncode}: {detail}"
            if capturing and data is None:
                # Non-zero exit AND nothing parseable — which is what a CLI that rejects
                # `--output-format stream-json` looks like. Every other failure mode still
                # prints a result object, so this is specifically the "flag not supported"
                # shape. Retry without capture before treating it as a real failure: a
                # transcript is never worth a customer's build.
                capturing = False
                continue
            # TRANSIENT infra errors (dropped connection, overloaded upstream, gateway
            # blips) must not fail a whole governed run — retry the invocation with a
            # short backoff. Anything non-transient (spend limit, bad flags, real agent
            # errors) raises immediately.
            transient = any(marker in detail.lower() for marker in (
                "connection closed mid-response", "connection error", "overloaded",
                "internal server error", "network error", "econnreset", "socket hang up",
                "529", "503", "bad gateway",
            ))
            if transient and attempt < 2:
                time.sleep(5 * (attempt + 1))
                continue
            raise RuntimeError(last_error)
        raise RuntimeError(last_error or "claude code agent failed")

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
        self, prompt: str, cwd: Path, *, read_only: bool = False, model: Optional[str] = None,
        resume_session_id: Optional[str] = None, max_turns: Optional[int] = None,
    ) -> dict:
        """Run a single role-scoped agent invocation against an existing workdir and
        return {summary, usage, cost_usd_micros, turns, model, session_id}. Used by the
        governed pipeline to dispatch each step as an ISOLATED context — a fresh
        `claude -p` process with no shared session state between roles. `model` routes
        low-cost vs strong. `resume_session_id` continues a prior session of the SAME
        role (repair loop) so the follow-up hits the warm prompt cache — never used
        across roles, which would break reviewer independence."""
        if getattr(self, "redact", False):  # data-boundary: mask PII for restricted tasks
            prompt = _redact_pii(prompt)[0]
        started = time.monotonic()
        try:
            # A read-only role IS the review seat — it judges, it does not write. That is
            # exactly the distinction the stray-process reaper needs and could not make from a
            # command line: a writing agent must never be reaped as though it were a reviewer.
            data = self._invoke(prompt, cwd, read_only=read_only, model=model,
                                resume_session_id=resume_session_id, max_turns=max_turns,
                                seat=("review" if read_only else "agent"))
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
        # The same invocation at the other resolution: one ledger row above, one row per turn
        # here. Popped off the dict so nothing downstream ever sees the transcript.
        # `_turns_live` means the transcript was already written turn by turn while the stage
        # ran, prompt row included. Writing it again here would duplicate every row.
        if not data.pop("_turns_live", False):
            record_agent_turns(data.pop("_turns", []), engine_name=self.engine,
                               model=model or usage.get("model"), prompt=prompt)
        data.pop("_turns", None)
        return {
            "summary": _as_text(data.get("result")).strip() or "(no summary returned)",
            "usage": usage,
            "cost_usd_micros": cost_micros,
            "turns": turns,
            "model": model or usage.get("model"),
            "session_id": data.get("session_id"),
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
            seat="research",
        )
        usage, cost_micros = self._usage(data, self.settings)
        record_llm_call(engine_name=self.engine, usage=usage, cost_usd_micros=cost_micros,
                        latency_ms=int((time.monotonic() - started) * 1000),
                        turns=int(data.get("num_turns") or 0), stage="research")
        if not data.pop("_turns_live", False):
            record_agent_turns(data.pop("_turns", []), engine_name=self.engine,
                               model=model or usage.get("model"), stage="research",
                               prompt=prompt)
        data.pop("_turns", None)
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
        prompt = self._build_prompt(spec, repo, cwd=sandbox.path)
        started = time.monotonic()
        data = self._invoke(prompt, sandbox.path)
        usage, cost_micros = self._usage(data, self.settings)
        summary = _as_text(data.get("result")).strip() or "(no summary returned)"
        turns = int(data.get("num_turns") or 0)
        record_llm_call(engine_name=self.engine, usage=usage, cost_usd_micros=cost_micros,
                        latency_ms=int((time.monotonic() - started) * 1000), turns=turns)
        record_agent_turns(data.pop("_turns", []), engine_name=self.engine,
                           model=usage.get("model"), prompt=prompt)

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

    def _repo_context(
        self,
        cwd: Path,
        *,
        task: str = "",
        limit: int = 120,
        model: Optional[str] = None,
    ) -> str:
        from .openai_adapter import groq_qwen_context_limits

        configured = int(getattr(self.settings, "code_agent_context_files", 14) or 0)
        if configured <= 0:
            return ""
        file_limit, max_bytes, head_bytes = groq_qwen_context_limits(
            model or getattr(self.settings, "openai_model", None),
            base_url=str(getattr(self.settings, "openai_base_url", "") or ""),
            engine=str(getattr(self, "engine", "") or ""),
            file_limit=min(int(limit or configured), configured),
        )
        if file_limit <= 0:
            return ""
        return build_repo_context(
            cwd,
            task=task,
            limit=file_limit,
            max_bytes=max_bytes,
            head_bytes=head_bytes,
        )

    def _repo_context_impl(self, cwd: Path, *, task: str = "", limit: int = 120) -> str:
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
        self, prompt: str, cwd: Path, *, read_only: bool = False, model: Optional[str] = None,
        resume_session_id: Optional[str] = None,  # accepted for interface parity; native has no session store
    ) -> dict:
        from .openai_adapter import estimate_cost_usd_micros

        system = self._REVIEW_SYS if read_only else self._EDIT_SYS
        started = time.monotonic()
        try:
            msg, usage = self._llm.create_chat_completion(
                [{"role": "system", "content": system},
                 {"role": "user", "content": (
                     f"{prompt}\n\n{self._repo_context(cwd, task=prompt, model=model)}"
                 )}],
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


class _SlotWatchdog:
    """Says "still waiting for a model slot" WHILE the wait is happening.

    A post-mortem record explains a stall after it has cost the time. This one speaks during
    it, at WARNING so the Activity Log keeps it unconditionally — the difference between an
    operator watching a phase sit at 12 minutes with no idea why, and one reading
    "waiting 10m for an anthropic slot, in_flight=1/1". Never raises, never blocks the
    acquire: one daemon thread that only logs.
    """

    INTERVAL_S = 60.0

    def __init__(self, limiter, provider: str) -> None:
        self._limiter = limiter
        self._provider = provider
        self._done = threading.Event()
        self._thread: Optional[threading.Thread] = None

    def start(self) -> None:
        try:
            self._thread = threading.Thread(
                target=self._run, name="model-slot-watchdog", daemon=True)
            self._thread.start()
        except Exception:  # noqa: BLE001 — a diagnostic never breaks a dispatch
            self._thread = None

    def _run(self) -> None:
        waited = 0.0
        while not self._done.wait(self.INTERVAL_S):
            waited += self.INTERVAL_S
            try:
                state = self._limiter.lane_state(self._provider)
                _log.warning(
                    "still waiting for a %s model slot after %.0fm "
                    "(in_flight=%s/%s, backoff=%ss, pacing=%ss)",
                    self._provider, waited / 60,
                    state.get("in_flight"), state.get("limit"),
                    state.get("backoff_remaining_seconds"),
                    state.get("pacing_wait_seconds"),
                )
            except Exception:  # noqa: BLE001
                return

    def stop(self) -> None:
        self._done.set()


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
        # QUEUEING IS NOT WORKING, AND THIS IS THE PATH THAT RUNS.
        #
        # Every run on this deployment uses substrate=local, which dispatches through here —
        # NOT through the guest/microVM path. Instrumenting only that one produced a run with
        # zero stage timings, which is how this gap was found.
        #
        # `slot()` blocks on concurrency, pacing and AIMD backoff. Folded into one duration,
        # a call that queued four minutes and then ran for one is indistinguishable from a
        # call that worked for five — and the fixes for those are opposite.
        # A DISPATCH THAT NEVER GOT A SLOT USED TO LEAVE NO TRACE AT ALL.
        #
        # Every failure INSIDE `fn` records an `llm_call` with ok=False. But `slot()` blocks
        # and can time out BEFORE `fn` is ever called, and that path recorded nothing: no
        # call, no turns, not even the pacing line below — which is emitted inside the `with`.
        # So a stage could burn the full 600s acquire deadline, be classified as inconclusive
        # ("could not acquire a …" is a known non-completion marker), get re-run, and repeat —
        # and the only evidence anywhere was a phase duration landing on a multiple of 600s.
        # That is precisely how 27 silent minutes look from the console. `acquired_at` is the
        # discriminator: still None means we never dispatched, and that now gets recorded.
        started = time.monotonic()
        acquired_at: Optional[float] = None
        watchdog = _SlotWatchdog(self._limiter, self._provider)
        try:
            watchdog.start()
            with self._limiter.slot(self._provider) as slot:
                acquired_at = time.monotonic()
                watchdog.stop()
                try:
                    out = fn(*args, **kwargs)
                except Exception as exc:  # noqa: BLE001 — classify then re-raise unchanged
                    if is_rate_limit_error(exc):
                        self._limiter.report_rate_limited(self._provider)
                    self._log_pacing(slot, started, acquired_at, failed=True)
                    raise
                self._limiter.report_ok(self._provider)
                self._log_pacing(slot, started, acquired_at, failed=False)
                return out
        except Exception as exc:  # noqa: BLE001 — record, then re-raise unchanged
            if acquired_at is None:
                self._record_never_dispatched(exc, time.monotonic() - started)
            raise
        finally:
            watchdog.stop()

    def _record_never_dispatched(self, exc: Exception, waited: float) -> None:
        """Put a never-dispatched call in the ledger, so the wait is attributable.

        Written as an `llm_call` with ok=False rather than only a log line: the run's ledger is
        where "what did this stage do" is answered, and a stage that waited ten minutes for a
        lane and gave up did something worth seeing there. The lane's own numbers go with it —
        full, backing off, or pacing are three different faults with three different fixes.
        """
        try:
            state = {}
            try:
                state = self._limiter.lane_state(self._provider)
            except Exception:  # noqa: BLE001 — a limiter without the accessor still records
                pass
            _log.warning(
                "model dispatch NEVER STARTED provider=%s engine=%s waited=%.1fs "
                "in_flight=%s/%s backoff=%ss pacing=%ss: %s",
                self._provider, self.engine, waited,
                state.get("in_flight"), state.get("limit"),
                state.get("backoff_remaining_seconds"), state.get("pacing_wait_seconds"),
                exc,
            )
            record_llm_call(
                engine_name=self.engine,
                usage={"model": getattr(self.settings, "claude_cli_model", "") or ""},
                latency_ms=int(waited * 1000), ok=False,
                error=f"never dispatched: {exc}",
            )
        except Exception:  # noqa: BLE001 — a diagnostic never breaks a dispatch
            pass

    def _log_pacing(self, slot, started: float, work_started: float, *, failed: bool) -> None:
        """One line per dispatch: how long the lane held it, how long the work took."""
        try:
            waited = float(getattr(slot, "waited_seconds", 0.0) or 0.0)
            work = max(0.0, time.monotonic() - work_started)
            total = max(1e-6, time.monotonic() - started)
            _log.info(
                "model dispatch provider=%s engine=%s waited=%.1fs work=%.1fs "
                "queueing=%.0f%%%s",
                self._provider, self.engine, waited, work, 100.0 * waited / total,
                " failed" if failed else "",
            )
        except Exception:  # noqa: BLE001 — a diagnostic never breaks a dispatch
            pass

    def run_role(self, *args, **kwargs):
        return self._paced(self._inner.run_role, *args, **kwargs)

    def run_research(self, *args, **kwargs):
        return self._paced(self._inner.run_research, *args, **kwargs)


def _wrap_paced(agent: "CodeAgent", settings: Settings) -> "CodeAgent":
    limiter = get_rate_limiter(settings)
    if isinstance(limiter, NullRateLimiter):  # burst mode: no wrapper overhead
        return agent
    return PacedCodeAgent(agent, limiter)


def make_code_agent(
    settings: Settings,
    engine: Optional[str] = None,
    *,
    connection=None,
    project_id: Optional[str] = None,
) -> CodeAgent:
    """Build a CodeAgent. ``engine`` overrides ``settings.code_agent_engine`` so the
    governed pipeline can run builder roles on one model and adversary roles on another.
    The returned agent is paced by the model rate limiter (#111) unless burst mode is on.

    When ``connection`` + ``project_id`` are supplied, lane keys come from
    ``resolve_project_credential`` (SecretRef first, env last-resort). Bound
    but unresolvable refs fail closed instead of silently billing a different key.
    """
    bound = _bind_lane_credentials(
        settings, engine, connection=connection, project_id=project_id
    )
    return _wrap_paced(_build_code_agent(bound, engine), bound)


def _bind_lane_credentials(
    settings: Settings,
    engine: Optional[str],
    *,
    connection=None,
    project_id: Optional[str] = None,
) -> Settings:
    if connection is None or not project_id:
        return settings
    from .credential_resolve import resolve_project_credential
    from .model_rate_limiter import provider_of

    lane = (engine or settings.code_agent_engine or "claude_code").lower()
    if lane == "echo":
        return settings
    if lane == "claude_code":
        token = resolve_project_credential(
            connection,
            settings,
            project_id=project_id,
            provider=provider_of(lane),
            env_fallback_attr="claude_code_oauth_token",
        )
        return settings.model_copy(update={"claude_code_oauth_token": token})
    if lane == "azure":
        key = resolve_project_credential(
            connection,
            settings,
            project_id=project_id,
            provider=provider_of(lane),
            env_fallback_attr="azure_openai_key",
        )
        return settings.model_copy(update={"azure_openai_key": key})
    if lane in _PROVIDER_LANES:
        key_field, _url_field = _PROVIDER_LANES[lane]
        key = resolve_project_credential(
            connection,
            settings,
            project_id=project_id,
            provider=provider_of(lane),
            env_fallback_attr=key_field,
        )
        return settings.model_copy(update={key_field: key})
    if lane in ("native", "openai"):
        key = resolve_project_credential(
            connection,
            settings,
            project_id=project_id,
            provider=provider_of(lane),
            env_fallback_attr="openai_api_key",
        )
        return settings.model_copy(update={"openai_api_key": key})
    return settings


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
