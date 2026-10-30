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
import contextvars
import json
import os
import re
from pathlib import Path
import subprocess
import threading
import logging
import time
from typing import Any, Callable, Optional, Protocol

from sqlalchemy import text

from ..config import Settings
from ..schemas import FileChange
from .verification_target import run_until_failure
from .repo_files import MAX_SCAN_BYTES, RepoFileRefused, RepoFiles
from .agent_turns import StreamParser, capture_enabled, parse_stream
from .llm_telemetry import record_agent_turns, record_llm_call
from . import harness_failures
from .engine_contract import EngineCapabilities


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


#: THE SHARED TASK CONTEXT GOES IN THE SYSTEM PROMPT, SO EVERY STAGE OF A BUILD SHARES ONE
#: CACHED PREFIX. The prompt cache is keyed on the request prefix: tools, then the system prompt,
#: then the messages. The CLI's default system prompt carries the cwd, env and GIT STATUS, which
#: change between stages (QA's tests are uncommitted files when implement starts), so every
#: stage's session wrote its own cache from byte zero: 2.08M cache-write tokens across the 14
#: sessions of one HelpDesk build, and no reuse between them. With the dynamic sections moved
#: into the first user message (--exclude-dynamic-system-prompt-sections) and the governed
#: pipeline's byte-identical TASK CONTEXT block appended to the system prompt, tools + system +
#: task context are the same bytes for every stage started within the cache TTL.
SHARED_CONTEXT_START = "=== TASK CONTEXT"
SHARED_CONTEXT_END = "=== END TASK CONTEXT ===\n\n"
#: Over this the block stays in the prompt (argv bound, as for the prompt itself).
SHARED_CONTEXT_MAX_BYTES = 96_000
#: A resumed session must send the SAME system prompt or its whole conversation is re-written to
#: the cache: the block each session started with, by session id (bounded).
_SESSION_SHARED: dict[str, str] = {}
_SESSION_SHARED_MAX = 256


def split_shared_context(prompt: str) -> tuple[str, str]:
    """(the shared TASK CONTEXT block, the stage's own prompt). ("", prompt) when the prompt
    does not start with the block."""
    text = prompt or ""
    if not text.startswith(SHARED_CONTEXT_START):
        return "", text
    at = text.find(SHARED_CONTEXT_END)
    if at < 0:
        return "", text
    cut = at + len(SHARED_CONTEXT_END)
    return text[:cut], text[cut:]


def _shared_for(prompt: str, resume_session_id: Optional[str]) -> tuple[str, str]:
    """What goes in the system prompt for this call, and what stays in the prompt."""
    shared, own = split_shared_context(prompt)
    if not shared and resume_session_id:
        return _SESSION_SHARED.get(resume_session_id, ""), prompt
    if len(shared.encode("utf-8", "ignore")) > SHARED_CONTEXT_MAX_BYTES:
        return "", prompt
    return shared, own


def _remember_shared(session_id: Optional[str], shared: str) -> None:
    if not session_id or not shared:
        return
    if len(_SESSION_SHARED) >= _SESSION_SHARED_MAX:
        _SESSION_SHARED.pop(next(iter(_SESSION_SHARED)), None)
    _SESSION_SHARED[session_id] = shared


#: How long a code agent may say NOTHING before it is treated as stalled.
#:
#: Generous on purpose: a big install or a long model turn can be quiet for a while, and killing
#: real work is worse than waiting. But an agent that has emitted nothing for ten minutes is not
#: working slowly — measured, the stall that prompted this was silent for forty-two.
#: Set BOSS_CODE_AGENT_IDLE_TIMEOUT_SECONDS=0 to disable and go back to the run timeout alone.
_IDLE_TIMEOUT_ENV = "BOSS_CODE_AGENT_IDLE_TIMEOUT_SECONDS"
_IDLE_TIMEOUT_DEFAULT = 600


#: The default bound on a build agent's silent thinking per step, in tokens.
THINKING_TOKEN_CAP_DEFAULT = 12000
#: The bound per step for a writing stage running at low effort.
LOW_EFFORT_THINKING_CAP = 4000
#: Per-stage bounds. The security seat answers in ONE turn, so the per-step cap is its whole
#: budget: a HelpDesk-lite security check thought ~11k tokens for 5.5 min while the other two
#: seats (same diff, running beside it) were done in 1.9 and 2.9; it gated the whole panel.
#: The reviewer and the consistency check read across many turns, so their cap bounds the
#: reasoning behind each step and the verdict, never how many files they may open.
STAGE_THINKING_CAPS = {"security_check": 6000, "review_code": 6000, "consistency_check": 6000}
_THINKING_CAP_ENV = "BOSS_CODE_AGENT_MAX_THINKING_TOKENS"


def thinking_token_cap() -> int:
    """The build agent's thinking cap: the env override, else the default; 0 means no cap."""
    raw = (os.environ.get(_THINKING_CAP_ENV) or "").strip()
    if not raw:
        return THINKING_TOKEN_CAP_DEFAULT
    try:
        return max(0, int(raw))
    except ValueError:
        return THINKING_TOKEN_CAP_DEFAULT


#: The CLI's --effort levels (claude --help, 2.1.x).
EFFORT_LEVELS = ("low", "medium", "high", "xhigh", "max")
#: Stages that WRITE (tests, code, fixes): the plan is already decided, so they run at the
#: project's writing effort (build_effort_writing, default low). Measured on a HelpDesk-size
#: build: ~89% of output tokens were reasoning and narration, ~11% file content.
WRITING_STAGES = frozenset({"write_tests", "implement", "solo", "repair", "guest"})
#: Stages that JUDGE or DESIGN: their thinking is the product, so they run at high.
REASONING_STAGES = frozenset({"standards", "spec", "plan", "design", "review_plan",
                              "review_code", "security_check", "consistency_check",
                              "check_tests", "quality"})
WRITING_EFFORT_DEFAULT = "low"
REASONING_EFFORT = "high"


def stage_effort(stage: Optional[str], writing: Optional[str] = None) -> Optional[str]:
    """The --effort for a build stage, or None for anything that is not a build stage (a chat,
    research) so the CLI keeps its own default there."""
    name = (stage or "").strip().lower()
    if name in WRITING_STAGES:
        level = (writing or WRITING_EFFORT_DEFAULT).strip().lower()
        return level if level in EFFORT_LEVELS else WRITING_EFFORT_DEFAULT
    if name in REASONING_STAGES:
        return REASONING_EFFORT
    return None


def effort_map(writing: Optional[str] = None) -> dict:
    """Every build stage's effort, for the record on the run."""
    return {st: stage_effort(st, writing) for st in sorted(WRITING_STAGES | REASONING_STAGES)}


def _stage_deadline() -> Optional[float]:
    """The wall-clock deadline (epoch seconds) of the stage this thread runs, if it has one."""
    from .llm_telemetry import get_llm_context

    raw = get_llm_context().get("stage_deadline")
    try:
        return float(raw) if raw else None
    except (TypeError, ValueError):
        return None


def _cap_to_deadline(timeout: int, *, read_only: bool) -> int:
    """A writing session's timeout, cut to its stage's deadline (never below a minute). A
    reviewer is never cut: the budget is the writing stage's."""
    deadline = None if read_only else _stage_deadline()
    if deadline is None:
        return timeout
    return max(60, min(int(timeout), int(deadline - time.time())))


def _mark_live(parsed: Optional[dict], live_written: dict) -> None:
    """ALREADY PERSISTED, TURN BY TURN, WHILE THE STAGE RAN: tell the caller, so it does not
    write every row a second time. Only rows a write REPORTED count (see _live_sink)."""
    if isinstance(parsed, dict) and int((live_written or {}).get("rows") or 0) > 0:
        parsed["_turns"] = []
        parsed["_turns_live"] = True


def _current_stage() -> str:
    from .llm_telemetry import get_llm_context

    return str(get_llm_context().get("stage") or "").strip().lower()


#: Read-only stages that PLAN rather than judge. They read the whole repository to write the
#: standards/spec/plan, so on a large repo they need far longer than a reviewer reading a diff
#: (design hit the 420 s review cap on the BOSS repo, crun_23d665a9). They get their own cap.
PLANNING_STAGES = frozenset({"design", "standards", "spec", "plan", "review_plan"})


def read_only_timeout(settings: Any, timeout: int, stage: Optional[str] = None) -> int:
    """A read-only session's timeout: a planning stage is capped by
    `code_agent_design_timeout_seconds`, every other read-only seat (a reviewer) by
    `code_agent_review_timeout_seconds`. `stage` defaults to the thread's llm context."""
    st = (stage if stage is not None else _current_stage()).strip().lower()
    if st in PLANNING_STAGES:
        cap = int(getattr(settings, "code_agent_design_timeout_seconds", 1200) or 1200)
    else:
        cap = int(getattr(settings, "code_agent_review_timeout_seconds", 900) or 900)
    return min(int(timeout), cap)


def _current_effort() -> Optional[str]:
    """The effort for the stage this thread is running (llm_telemetry context)."""
    from .llm_telemetry import get_llm_context

    ctx = get_llm_context()
    return stage_effort(ctx.get("stage"), ctx.get("effort_writing"))


#: The CLI's sub-agent tool (Claude Code 2.x names it `Agent`; `Task` is its older alias).
SUB_AGENT_TOOL = "Agent"
#: Never available to a build, sub-agent or not: the build reaches the network only through
#: the fence, and a sub-agent must not be able to ask for what its parent was never given.
SUB_AGENT_DENIED_TOOLS = "WebFetch,WebSearch"


def sub_agent_tools(allowed: str) -> str:
    """`allowed` plus the sub-agent tool, once."""
    names = [t.strip() for t in str(allowed or "").split(",") if t.strip()]
    if SUB_AGENT_TOOL not in names:
        names.append(SUB_AGENT_TOOL)
    return ",".join(names)


def sub_agent_rule(n: int) -> str:
    """How a writing stage fans out: independent parts in parallel, integration by the parent."""
    return (
        f"\n\nPARALLEL SUB-AGENTS (use them, up to {n} at once):\n"
        f"- This is a large change. Split INDEPENDENT parts across up to {n} parallel sub-agents "
        "with the Agent tool IN ONE TURN, e.g. server routes, data layer and migrations, UI "
        "screens, and test suites, each told exactly which files it owns.\n"
        "- Give each sub-agent the relevant part of the spec and plan and the files it must NOT "
        "touch. Two sub-agents never write the same file.\n"
        "- When they return, YOU integrate: read what they wrote, fix the seams, then run the "
        "full build and every test yourself. You are responsible for the whole change passing.\n"
        "- Small or tightly coupled work: do it yourself; a sub-agent is for parallel volume.\n"
    )


def _idle_timeout_seconds() -> float:
    raw = (os.environ.get(_IDLE_TIMEOUT_ENV) or "").strip()
    if not raw:
        return float(_IDLE_TIMEOUT_DEFAULT)
    try:
        return max(0.0, float(raw))
    except ValueError:
        return float(_IDLE_TIMEOUT_DEFAULT)


def _end_child(child, reap_seconds: float = 10.0) -> None:
    """Kill a child whose pipes _drain's reader threads own, and reap it. Never raises.

    NEVER `child.communicate()` here. The readers close stdout/stderr when they finish, and a
    prompt sent on stdin was written and CLOSED before the drain began; Popen.communicate()
    then calls `stdin.flush()` on that closed pipe and raises ValueError("I/O operation on
    closed file."), which escaped as the whole build's failure in place of the timeout it was
    handling (crun_944194c4c2ec4534b84e: an implement stage that ran past its limit with a
    >96 KB prompt on stdin failed the run as "I/O operation on closed file."). Reaping is
    `wait`, bounded: a zombie must not hold the seat, and nothing may hang the run."""
    try:
        child.kill()
    except Exception:  # noqa: BLE001 — already gone
        pass
    try:
        child.wait(timeout=reap_seconds)
    except Exception:  # noqa: BLE001 — best effort; the caller is already failing
        pass


def is_partial_line(line: str) -> bool:
    """A token-level `stream_event` line (--include-partial-messages)."""
    return '"type":"stream_event"' in (line or "")[:64]


class LivePulse:
    """A long model stretch is never silent: "Thinking… 2m" / "Writing… 1m", at most every 30s.

    A HelpDesk AI build had two silent stretches of 7 and 12 minutes, the model thinking before
    it acted, and nobody watching could tell that from a hang. With partial messages the CLI
    says what it is doing token by token; this turns that into one line per half minute.
    """

    EVERY_SECONDS = 30.0

    def __init__(self, clock=time.monotonic):
        self._clock = clock
        self.reset()

    def reset(self) -> None:
        """A whole turn arrived: the stretch is over."""
        self._phase = ""
        self._since = 0.0
        self._last = 0.0

    def feed(self, line: str) -> Optional[dict]:
        phase = ("thinking" if '"thinking_delta"' in line
                 else "writing" if ('"text_delta"' in line or '"input_json_delta"' in line)
                 else "")
        if not phase:
            return None
        now = self._clock()
        if phase != self._phase:
            self._phase, self._since, self._last = phase, now, now
            return None
        if now - self._last < self.EVERY_SECONDS:
            return None
        self._last = now
        elapsed = int(now - self._since)
        mins, secs = divmod(elapsed, 60)
        took = f"{mins}m {secs:02d}s" if mins else f"{secs}s"
        return {"direction": "completion", "level": "info",
                "content": f"{phase.capitalize()}… {took} so far"}


def _drain(child, *, timeout: Optional[float], on_line=None,
           on_err_line=None) -> tuple[str, str]:
    """Read a child's stdout/stderr as they arrive, returning the full text of each.

    Same contract as ``communicate(timeout=...)`` — including raising ``TimeoutExpired`` — with
    one addition: every stdout line is handed to ``on_line`` (every stderr line to
    ``on_err_line``) the moment it is read.

    A consumer that raises HarnessFailure STOPS the child: the engine said it cannot run (its
    sandbox did not start — harness_failures), so the session is ended at that line and the
    failure is raised here, instead of the agent working on blind until it gives up.

    BOTH pipes are drained concurrently. Reading one to completion while the other fills is the
    classic subprocess deadlock: the child blocks writing to a full stderr buffer, we block
    reading a stdout that will never advance, and the build hangs until its timeout with no way
    to tell that from slow work. That is the failure mode this whole change exists to remove,
    so it must not be reintroduced by the fix.
    """
    out_lines: list[str] = []
    err_chunks: list[str] = []
    #: When the child last said anything. A stalled agent is ALIVE and SILENT, which the
    #: overall timeout cannot distinguish from slow work — see the wait loop below.
    last_output_at = [time.monotonic()]
    #: The harness failure a consumer saw, if any (the child is ended at once).
    stopped: list[BaseException] = []

    def _pump(stream, sink, consumer) -> None:
        try:
            for line in iter(stream.readline, ""):
                last_output_at[0] = time.monotonic()
                # A PARTIAL message (one per token with --include-partial-messages) is a pulse
                # for the live view, never part of the output: keeping them would multiply the
                # stored stdout a hundredfold and change nothing the final parse reads.
                if not is_partial_line(line):
                    sink.append(line)
                if consumer is not None and not stopped:
                    try:
                        consumer(line)
                    except harness_failures.HarnessFailure as failure:
                        stopped.append(failure)
                        try:
                            child.kill()   # the whole owned tree; the wait below returns
                        except Exception:  # noqa: BLE001 — already gone
                            pass
                    except Exception:  # noqa: BLE001 — a consumer must never kill the build
                        pass
        except Exception:  # noqa: BLE001 — a broken pipe ends this reader, nothing more
            pass
        finally:
            try:
                stream.close()
            except Exception:  # noqa: BLE001
                pass

    # CARRY THE CALLER'S CONTEXT INTO THE READER THREADS.
    #
    # `record_agent_turns` resolves project_id / run_id / db_engine from a ContextVar, and a new
    # thread starts with a FRESH context — it does not inherit the creator's. So the first
    # version wrote its prompt row (main thread, before these start) and then silently lost
    # every streamed turn: `begin(None)` falls back to a default engine rather than raising, so
    # the rows went nowhere attributable and nothing errored. Live tracing looked wired up and
    # produced exactly one row per stage.
    #
    # `copy_context()` is evaluated HERE, on the calling thread, which is the only place the
    # context exists.
    ctx = contextvars.copy_context()
    threads = [
        threading.Thread(target=ctx.run, args=(_pump, child.stdout, out_lines, on_line),
                         name="claude-stdout", daemon=True),
        threading.Thread(target=contextvars.copy_context().run,
                         args=(_pump, child.stderr, err_chunks, on_err_line),
                         name="claude-stderr", daemon=True),
    ]
    for t in threads:
        t.start()
    # A STALLED AGENT IS ALIVE AND SILENT, AND NOTHING WAS WATCHING FOR THAT.
    #
    # `child.wait(timeout=...)` asks one question — has it finished — and the answer is "no"
    # whether the agent is thinking hard or has wedged. Measured on a real run: the QA agent
    # emitted `turn.tool_call.Read` at 05:08:01 and then nothing at all until the job's lease
    # was reclaimed at 05:50:22. Forty-two minutes of dead air inside a seventy-minute build,
    # and the only thing that eventually reacted was a lease expiring — the operator watched
    # this happen repeatedly and reported it long before anyone measured it.
    #
    # The stream is the liveness signal. It is already being read line by line for the live
    # transcript, so "has it said anything recently" costs one timestamp. A silent child is
    # killed and surfaced as a timeout, which the pipeline already knows how to retry —
    # minutes, not the better part of an hour.
    idle_limit = _idle_timeout_seconds()
    try:
        if not idle_limit:
            try:
                child.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                _end_child(child)
                raise
        else:
            deadline = (time.monotonic() + timeout) if timeout else None
            while True:
                try:
                    child.wait(timeout=min(15.0, idle_limit))
                    break
                except subprocess.TimeoutExpired:
                    now = time.monotonic()
                    if deadline and now > deadline:
                        _end_child(child)
                        raise
                    silent_for = now - last_output_at[0]
                    if silent_for > idle_limit:
                        _log.error(
                            "code agent produced no output for %ds (limit %ds) — killing it as "
                            "stalled rather than waiting out the run timeout",
                            int(silent_for), int(idle_limit),
                        )
                        _end_child(child)
                        raise subprocess.TimeoutExpired(
                            cmd="claude", timeout=silent_for,
                            output=f"agent stalled: no output for {int(silent_for)}s",
                        )
    except subprocess.TimeoutExpired:
        raise
    # The process has exited; its writers are closed, so the readers finish promptly. Bounded
    # anyway — a wedged reader must not become a new way for a build to hang forever.
    for t in threads:
        t.join(timeout=30)
    if stopped:
        raise stopped[0]
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
from .agent_env import agent_environment
from . import model_proxy as _model_proxy
from .workspace_perms import tidies_workspace
from . import owned_process

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


#: Files a repository uses to tell an agent how to work in it, most-specific first.
#: AGENTS.md is the cross-tool convention (Factory, Codex, Cursor); CLAUDE.md is read for
#: compatibility so a repo already set up for Claude Code needs no second file.
REPO_GUIDE_FILENAMES = ("AGENTS.md", "agents.md", "Agents.md", "CLAUDE.md", "Claude.md")


def load_repo_guide(cwd, *, max_chars: int = 32_000) -> Optional[str]:
    """Read the target repository's agent instructions from its working tree.

    `RepoContext.repo_guide` was declared and injected into the prompt for months but
    never populated - the prompt branch could not fire. Reading it from the checked-out
    tree (rather than at RepoContext construction, which happens before the clone) makes
    it correct for the branch actually being worked on.

    Returns None when the repo has no such file; that is the common case, not an error.
    """
    if cwd is None:
        return None
    with RepoFiles(Path(cwd)) as files:
        for name in REPO_GUIDE_FILENAMES:
            try:
                text = files.read_text(name, max_bytes=max_chars * 4, truncate=True).strip()
            except FileNotFoundError:
                continue
            if not text:
                continue
            out = "--- " + name + " ---\n" + text
            if len(out) > max_chars:
                out = out[:max_chars] + "\n...[truncated: repo guide exceeds the configured cap]"
            return out
    return None


@dataclass
class CodeTaskSpec:
    task_id: str
    title: str
    instructions: str
    acceptance_criteria: list[str] = field(default_factory=list)
    # Authoritative verification commands BOSS re-runs after the agent stops.
    test_commands: list[str] = field(default_factory=list)
    # The instructions of the agent the task runs AS (agent-loadout §4.8): its operator's
    # working instructions for work, under a heading naming it and its version. "" otherwise.
    agent_instructions: str = ""


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


#: HOW A WRITING STAGE WORKS: fewer, larger steps. Measured on a HelpDesk build's implement
#: stage: 1,337 turns, 227 Reads, 268 Bash (build/test/lint/typecheck re-runs after each small
#: change), 48 Edits, and ~89% of output tokens spent on reasoning and narration rather than
#: file content. Every turn is a full model round trip. Writing stages only: a reviewer reads.
_WRITING_RULE = (
    "\nWORKING RULES (writing stages):\n"
    "- No narration between tool calls. Act; summarise once, in one line, at the end.\n"
    "- Read in batches: every file you need for the next piece of work in ONE turn.\n"
    "- Write each new file WHOLE with one Write. Use Edit only to change a few lines of an "
    "existing file, never to build a file up piece by piece.\n"
    "- Do not re-read a file you just wrote or edited: you already know what is in it.\n"
    "- Run the full test/build commands when a chunk of work is finished (a whole module, a "
    "group of routes, a screen), not after every change. To check one thing, run only that "
    "test file. Fix all failures a run shows before running it again.\n"
)


_SANDBOX_FACTS: dict = {}

#: BOSS's own install is a clean `npm install` with no peer-dependency escape hatch. HelpDesk AI
#: (crun_6eaa478dc33a42c6bdb7) put eslint 9 beside eslint-plugin-react-hooks 4 (peer eslint <= 8):
#: the agent's tree installed, BOSS's clean install failed with ERESOLVE, and the build blocked.
CLEAN_INSTALL_FACT = (
    "- BOSS installs with a clean `npm install`; peer-dependency errors fail the build. Pick "
    "versions that agree (e.g. eslint 9 needs eslint-plugin-react-hooks >=5).")


#: Said when the sandbox has no C/C++ toolchain (read from this process, like every fact).
NO_COMPILER_FACT = (
    "- The build sandbox has no C/C++ compiler: avoid packages with native builds "
    "(better-sqlite3, bcrypt, sharp…); use pure-JS alternatives (pg, node:sqlite, bcryptjs).")


def java_fact() -> str:
    """The Java line of the sandbox facts, when this install turned Java on (toolchains), else
    "". An agent writing a Spring app otherwise guesses a JDK and reaches for ./gradlew."""
    from . import toolchains

    have = toolchains.installed().get("java")
    if have is None:
        return ""
    versions = ", ".join(f"{label} {have[key]}" for key, label in
                         (("jdk", "JDK"), ("maven", "Maven"), ("gradle", "Gradle")) if have.get(key))
    return (f"- Java is installed ({versions or 'JDK 21, Maven, Gradle'}). Build with the image's "
            "`mvn` or `gradle`, never ./gradlew: the wrapper downloads Gradle from "
            "services.gradle.org, which this sandbox cannot reach. Dependencies resolve from "
            "Maven Central and the Gradle plugin portal only. BOSS runs `mvn -B -ntp verify` or "
            "`gradle build` as the gate, and serves the app on $PORT (Spring Boot: "
            "-Dserver.port).")


def sandbox_facts() -> str:
    """WHAT THE BUILD SANDBOX HAS, read from this process's own environment (the agent runs
    here), said up front. A HelpDesk build spent steps discovering each of these: curl missing
    (3 failed probes), argon2 with no arm64 prebuild (node FATAL, repros in /tmp), a Node without
    --experimental-strip-types, and Playwright's browser downloaded at run time (181 s)."""
    if "text" in _SANDBOX_FACTS:
        return _SANDBOX_FACTS["text"]
    import platform
    import shutil as _shutil

    lines = []
    try:
        node = owned_process.run(["node", "--version"], capture_output=True, text=True,
                                 timeout=5).stdout.strip()
    except Exception:  # noqa: BLE001 — no node: say nothing about it
        node = ""
    if node:
        lines.append(f"- Node {node} with npm.")
    java = java_fact()
    if java:
        lines.append(java)
    lines.append("- curl is available." if _shutil.which("curl")
                 else "- curl is NOT installed: probe HTTP with node (fetch) instead.")
    browsers = os.environ.get("PLAYWRIGHT_BROWSERS_PATH") or ""
    if browsers and Path(browsers).is_dir():
        lines.append(f"- A Chromium for Playwright is preinstalled at {browsers} "
                     "(PLAYWRIGHT_BROWSERS_PATH is set); do not download browsers.")
    arch = {"aarch64": "arm64", "x86_64": "x64"}.get(platform.machine(), platform.machine())
    lines.append(f"- {platform.system().lower()}-{arch}: a native npm module with no prebuilt binary for it fails to "
                 "build here (argon2 does). Use pure-JS packages: bcryptjs for password hashing.")
    if not any(_shutil.which(tool) for tool in ("make", "cc", "gcc", "g++")):
        # onboarding_path_builder's agent (crun_decc93dbb0154e768871) picked better-sqlite3,
        # whose install runs node-gyp, which needs make: the sandbox has none.
        lines.append(NO_COMPILER_FACT)
    lines.append(CLEAN_INSTALL_FACT)
    from ..templates.run_env import OPTIONAL_SETTINGS_RULE

    lines.append("- " + OPTIONAL_SETTINGS_RULE)
    text = "\nSANDBOX (facts, no need to check them):\n" + "\n".join(lines) + "\n"
    _SANDBOX_FACTS["text"] = text
    return text


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
    scanned_bytes = 0
    with RepoFiles(cwd) as reader:
        # A symlink/special leaf is listed by name but never opened: its existence must not
        # abort the context pack (and so the stage) - crun_0cd67b0a died here on BOSS's own
        # tests/e2e/stubs/ssh-only/ssh. A regular file that read_text refuses still raises.
        for rel, kind in sorted(reader.walk_kinds(skip=skip)):
            files.append(rel)
            if kind != "file" or Path(rel).suffix not in exts:
                continue
            # The read budget bounds what is READ, not whether the stage may run: past it,
            # files are listed by name only. (BOSS's own tree holds ~40 MB of source heads; a
            # refusal here failed its implement stage once the symlink no longer did.)
            remaining = MAX_SCAN_BYTES - scanned_bytes
            if remaining <= 0:
                continue
            content = reader.read_text(rel, max_bytes=min(head_bytes, remaining), truncate=True,
                                       errors="ignore")
            scanned_bytes += len(content.encode("utf-8"))
            entries.append({"rel": rel, "content": content, "size": len(content.encode("utf-8"))})
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
        parallel_agents: int = 0,
    ) -> dict:
        """Run one isolated, role-scoped invocation against an existing workdir.
        Returns {summary, usage, cost_usd_micros, turns, model, session_id?}. The role
        prompt is the guidance; the model impersonates the role. read_only stages must
        not edit files; model overrides which model runs this step (low-cost vs strong
        routing). resume_session_id continues a prior SAME-role session (cache reuse)."""
        ...


# Every model-proxy token a session was given is revoked when `_invoke` returns or raises, and
# what crossed the proxy rides back on the result as `_proxy_usage` (model_proxy).
_model_proxy_scoped = _model_proxy.scoped(attach_usage=True)


# --------------------------------------------------------------------------- #
#  Claude Code headless implementation                                        #
# --------------------------------------------------------------------------- #
class ClaudeCodeAgent:
    """Drives `claude -p` inside the sandbox with its native editing tools.

    Uses the Max-plan CLI as the coding brain. The CLI edits files directly in
    the sandbox working tree; BOSS then reads the diff and re-runs the tests.
    """

    engine = "claude_code"
    capabilities = EngineCapabilities(resume=True, read_only=True, effort=True, sub_agents=True,
                                      turn_limit=True, reports_cost=True, streams_turns=True,
                                      budget_limit=True)
    #: `_invoke` appends the working rules and sandbox facts itself (engine_contract).
    adds_working_rules = True

    def __init__(self, settings: Settings):
        self.settings = settings
        #: The MCP servers the agent this task runs as holds (code_task_mcp, skills-mcp E6).
        #: None: not run as an agent, and the CLI's flags are as they always were.
        self.mcp_harness = None

    def _claude_bin(self) -> str:
        configured = getattr(self.settings, "claude_cli_path", None) or "claude"
        if configured == "claude":
            local = Path.home() / ".local" / "bin" / "claude"
            if local.exists():
                return str(local)
        return configured

    def _child_env(self) -> dict:
        """The agent's environment, plus the credential this install actually holds.

        `agent_environment` decides what may be INHERITED and denies by default — that stays
        exactly as it is. This adds one value BOSS resolved itself, for the same reason the
        caches are added: it is something BOSS computed rather than something the process
        happened to be started with.
        """
        env = agent_environment(os.environ, _shared_tool_caches())
        # The lane binder runs only where a connection and project are in scope and binds its
        # answer onto settings; a sandboxed agent process has neither, so the store is asked
        # directly — through the one resolution every `claude` child uses
        # (claude_credential.resolve_claude_credential): the subscription login or the console's
        # Anthropic API key, in the operator's order, exactly one of them in the child's env.
        # It raises rather than start the CLI with no login when the store did not answer.
        cred = self._resolve_credential()
        cred.apply(env)
        if cred.is_key:
            # AN API KEY NEVER REACHES THE SESSION (model_proxy). The CLI gets a run token and the
            # loopback proxy's base URL; the proxy injects the key. The agent's own commands
            # inherit this environment, so what they could read is a token that only works on
            # the model API paths, for this session, and dies when it ends. A subscription login
            # is not an API key and is not proxied (MODEL_CONNECTIONS.md).
            from . import model_proxy

            model_proxy.shield(env, "anthropic", cred.value, settings=self.settings)
        #: Who pays for this run's calls (included_spend.py), stamped on its usage.
        self._billing = cred.billing
        # A BOUND ON SILENT THINKING. Left to decide for itself, the model thought for 7 and 12
        # minutes before its first action in a HelpDesk AI build (19 of the build's first 52
        # minutes were silence, with no tool call, no output and no error), and nobody watching
        # can tell that from a hang. The design stages have already done the deep reasoning; a
        # cap keeps each step's thinking to minutes, not quarters of an hour.
        # BOSS_CODE_AGENT_MAX_THINKING_TOKENS=0 leaves the CLI's own default.
        cap = thinking_token_cap()
        if cap and "MAX_THINKING_TOKENS" not in env:
            # A writing stage at low effort thinks briefly per step: ~870 thinking tokens on
            # every one of a HelpDesk implement's 424 steps, reads and `ls` included.
            if _current_effort() == "low":
                cap = min(cap, LOW_EFFORT_THINKING_CAP)
            stage_cap = STAGE_THINKING_CAPS.get(_current_stage())
            if stage_cap:
                cap = min(cap, stage_cap)
            env["MAX_THINKING_TOKENS"] = str(cap)
        return env

    def _resolve_credential(self):
        """Which Anthropic credential this engine's child runs on (claude_credential). The
        claude_agent_sdk engine overrides this: the API key only, never the login."""
        from .claude_credential import resolve_claude_credential

        return resolve_claude_credential(self.settings)

    def _build_prompt(self, spec: CodeTaskSpec, repo: RepoContext,
                      cwd: Optional[Path] = None) -> str:
        criteria = "\n".join(f"- {c}" for c in spec.acceptance_criteria) or "- (none specified)"
        tests = "\n".join(f"- {c}" for c in spec.test_commands) or "- (none specified)"
        # BOSS-408: an explicitly supplied guide wins; otherwise read it from the
        # working tree so the field is populated instead of permanently None.
        guide_text = repo.repo_guide or load_repo_guide(
            cwd, max_chars=int(getattr(self.settings, "repo_guide_max_chars", 32_000)))
        guide = (
            "\n\nREPO GUIDE (the repository's own instructions - follow them):\n"
            + guide_text + "\n"
        ) if guide_text else ""
        if spec.agent_instructions:
            guide += "\n\n" + spec.agent_instructions + "\n"
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
            except RepoFileRefused:
                raise
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

    @_model_proxy_scoped
    def _invoke(
        self, prompt: str, cwd: Path, *, read_only: bool = False, model: Optional[str] = None,
        allowed_tools: Optional[str] = None, resume_session_id: Optional[str] = None,
        max_turns: Optional[int] = None, seat: str = "agent",
        on_turn: Optional[Callable[[dict], None]] = None, parallel_agents: int = 0,
        max_budget_usd: Optional[float] = None,
    ) -> dict:
        # A run whose engine already could not run starts no new session (harness_failures).
        harness_failures.raise_if_tripped()
        timeout = int(self.settings.code_agent_timeout_seconds or 900)
        # A full web build (in-container / local_subprocess) needs well beyond the legacy 900s that
        # the prod env still pins (BOSS_CODE_AGENT_TIMEOUT_SECONDS=900) — a big single-page site was
        # dying mid-implement. Floor the BUILD timeout so it can finish; review stages stay capped.
        if not read_only:
            timeout = max(timeout, 2400)
        if read_only:
            # A review shouldn't run as long as an implementation — cap it tighter so a slow
            # reviewer can't stall the panel. Planning stages (design/spec/plan) read the whole
            # repo and get their own, longer cap.
            timeout = read_only_timeout(self.settings, timeout)
        # Review stages run read-only (no Edit/Write tools, no auto-accept) so a reviewer
        # cannot mutate the change it is judging. `allowed_tools` lets non-coding callers
        # (e.g. the Discovery research pipeline) opt into WebSearch/WebFetch instead.
        if allowed_tools is not None:
            allowed = allowed_tools
        else:
            allowed = "Read,Grep,Glob,Bash" if read_only else self.settings.code_agent_allowed_tools
        # PARALLEL SUB-AGENTS (build_speed). A sub-agent runs INSIDE this same CLI process: same
        # uid, same cwd, same environment, same network fence and the same --allowedTools and
        # permission mode, so it can do nothing the parent cannot. Granted only to writing stages
        # (never a reviewer: review independence), and the network tools are refused outright so
        # a general-purpose sub-agent cannot ask for them.
        fan_out = 0 if read_only else max(0, int(parallel_agents or 0))
        if fan_out:
            allowed = sub_agent_tools(allowed)
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
        # EFFORT PER STAGE (stage_effort): writing stages at the project's writing effort,
        # design and review at high, anything else at the CLI's own default.
        effort = _current_effort()
        # A STAGE'S WALL-TIME BUDGET (governed build_implement_budget_minutes): a writing session
        # never runs past the stage's deadline, whatever the per-session timeout says.
        timeout = _cap_to_deadline(timeout, read_only=read_only)
        shared_ctx, own_prompt = _shared_for(_argv_safe(prompt), resume_session_id)

        def base_cmd(stream: bool) -> list[str]:
            """The argv for one attempt. Rebuilt per attempt so capture can be dropped."""
            argv = [
                self._claude_bin(),
                "-p",
                # `stream-json` emits a line per turn AND still ends with the identical result
                # event `--output-format json` would have returned, which is why the transcript
                # could be added without changing a single caller in the build pipeline.
                *(("--output-format", "stream-json", "--verbose", "--include-partial-messages")
                  if stream else ("--output-format", "json")),
                "--max-turns", str(_max_turns),
                # A SPEND CEILING FOR THIS SESSION (engine_contract budget_limit): the CLI stops
                # itself there, so a caller's cap bounds the turn while it runs, not after it.
                *(("--max-budget-usd", f"{max(float(max_budget_usd), 0.01):.2f}")
                  if max_budget_usd is not None else ()),
                "--allowedTools", allowed,
                "--permission-mode", permission,
            ]
            if fan_out:
                argv += ["--disallowedTools", SUB_AGENT_DENIED_TOOLS]
            if self.mcp_harness is not None:
                # ONLY THE AGENT'S OWN SERVERS, Ask/Off tools refused (code_task_mcp).
                argv += self.mcp_harness.argv()
            if resume_session_id:
                # Token optimization: continue the prior session instead of cold-starting —
                # the follow-up rides the warm prompt cache (repo context + conversation are
                # cache READS, not fresh ingestion). Used by the repair loop.
                argv += ["--resume", resume_session_id]
            if model:
                argv += ["--model", model]
            if effort:
                argv += ["--effort", effort]
            # One cached prefix for every stage (SHARED_CONTEXT_START): static system prompt,
            # then the run's shared task context.
            argv += ["--exclude-dynamic-system-prompt-sections"]
            if shared_ctx:
                argv += ["--append-system-prompt", shared_ctx]
            # `--` ends option parsing: a prompt that BEGINS with a dash (e.g. a
            # "--- SECTION ---" header) must never be read as a CLI option.
            # Single choke point: every stage (build AND review) goes through _invoke, so the
            # batching rule is applied uniformly here rather than in each prompt builder.
            # THE SAME 128 KiB ARGV CEILING. A repo-context pack plus a long spec crosses it,
            # and the whole command is then rejected before the process starts — measured on the
            # chat path as `OSError: [Errno 7] Argument list too long: 'nice'`. Over the limit
            # the prompt goes on stdin; under it, nothing changes.
            body = (own_prompt + _PARALLEL_TOOLS_RULE
                    + ("" if read_only else _WRITING_RULE + sandbox_facts())
                    + (sub_agent_rule(fan_out) if fan_out else ""))
            if len(body.encode("utf-8", "ignore")) > 96_000:
                return argv, body
            return argv + ["--", body], None
        last_error = ""
        # Whether a refused login has already been asked about once (model_failures.
        # confirm_login_rejection): one "Not logged in" is a report, two are a verdict.
        login_rechecked = False

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
            # COUNTS ROWS ACTUALLY WRITTEN, not turns parsed. The caller skips the final batch
            # write when the transcript is already stored, and deciding that from "the parser
            # saw turns" lost the whole transcript when the writes themselves went nowhere: the
            # reader thread had an empty context, `begin(None)` fell back to a default engine
            # instead of raising, and a run whose call reported 48 turns stored 4. Only a write
            # that REPORTS having written may cancel the fallback.
            written = {"rows": 0}

            # Turn -1, written before the first line arrives: "what was it asked" is the first
            # question anyone has, and it should not wait for the answer.
            try:
                record_agent_turns([], engine_name=self.engine, model=live_model,
                                   prompt=prompt)
            except Exception:  # noqa: BLE001
                pass

            pulse = LivePulse()

            def _on_line(line: str) -> None:
                if is_partial_line(line):
                    beat = pulse.feed(line)
                    if beat:
                        try:
                            record_agent_turns([beat], engine_name=self.engine, model=live_model)
                        except Exception:  # noqa: BLE001
                            pass
                    return
                pulse.reset()
                produced = parser.feed(line)
                if not produced:
                    return
                try:
                    written["rows"] += int(
                        record_agent_turns(produced, engine_name=self.engine,
                                           model=live_model) or 0)
                except Exception:  # noqa: BLE001
                    pass
                # WATCHING IT WORK, not waiting for it to finish. An investigation runs for
                # minutes; without this the operator sees a spinner and concludes it is stuck,
                # which is the specific impression this whole effort is trying to remove.
                # Best-effort by construction: a progress indicator that can fail the work it
                # is reporting on is worse than no indicator.
                for turn in produced:
                    try:
                        if on_turn:
                            on_turn(turn)
                    except Exception:  # noqa: BLE001
                        pass
                # THE HARNESS ANSWERING IN THE COMMAND'S PLACE (a sandbox that did not start)
                # stops the session here — raised through _drain, which ends the child.
                for turn in produced:
                    harness_failures.check_turn(turn, engine=self.engine)

            return _on_line, parser, written

        for attempt in range(4):
            # CAPTURE MUST NEVER COST A BUILD.
            #
            # `stream-json` is the only reason this function's flags changed, and it runs on
            # every customer build. If a CLI version ever rejects the flag or emits a stream
            # this cannot read, the right outcome is a build that succeeds without a
            # transcript — not a build that fails with one. `capturing` is cleared below on the
            # first such failure and these flags rebuild without it.
            cmd, stdin_prompt = base_cmd(capturing)
            # Live tracing only makes sense while we are asking for the stream format. Without
            # `capturing` there are no per-turn events to read.
            on_line, live_parser, live_written = (
                _live_sink(model or getattr(self.settings, "claude_cli_model", "") or "")
                if capturing and capture_enabled() else (None, None, {"rows": 0})
            )
            try:
                # Popen rather than run(), so the pid is known and can be registered. A
                # reaper that scans `ps` cannot tell which seat a claude process occupies;
                # this is how it finds out.
                # Yields CPU/IO to the control plane it shares a container with. See
                # services/scheduling.py: this is scheduling WEIGHT under contention, not a
                # change to what runs — with nothing else competing it is exactly as fast.
                # OWNED: its own session, a token every descendant inherits, and the build it
                # belongs to (owned_process.owner_scope). `child.kill()` below — idle, timeout —
                # and the leader's own exit now end the whole tree, not one pid: the dev server
                # an agent left running with `npm run dev &` dies with it.
                child = owned_process.popen(
                    low_priority(cmd),
                    stdin=(subprocess.PIPE if stdin_prompt else subprocess.DEVNULL),
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
                    # WHAT THE AGENT CAN SEE IS A DECISION, NOT AN INHERITANCE.
                    #
                    # This was `{**os.environ, ...}`: the agent got the control plane's whole
                    # environment, DATABASE_URL included. On 2026-09-02 its QA step ran the API
                    # test suite — the job we gave it — and the suite cleared the tables it
                    # found, which were production's. 204 of them.
                    #
                    # Deny by default now. See services/agent_env.py for what survives and why.
                    #
                    # AND WHAT BOSS HOLDS, not only what it inherited. `agent_environment`
                    # copies CLAUDE_CODE_OAUTH_TOKEN out of this PROCESS's environment — where
                    # it is not, on any install that keeps credentials in a secret store. The
                    # token was resolved into `settings` by `_bind_lane_credentials` and then
                    # never reached the child, so a governed run died on the CLI's own words:
                    #
                    #     claude code agent exited 1: Not logged in · Please run /login
                    #
                    # about a subscription that was connected, proved and stored. Same gap as
                    # the chat adapter had, one layer down.
                    env=self._child_env(),
                )
                register_seat(child.pid, seat)
                if stdin_prompt is not None and child.stdin is not None:
                    # Written and closed before draining: the CLI reads its prompt to EOF, and
                    # leaving the pipe open would deadlock both sides waiting on each other.
                    try:
                        child.stdin.write(stdin_prompt)
                    finally:
                        child.stdin.close()
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
                    out, err = _drain(child, timeout=timeout, on_line=on_line,
                                      on_err_line=lambda line: harness_failures.check_stream(
                                          line, engine=self.engine))
                except subprocess.TimeoutExpired:
                    # _drain has killed and reaped it; its readers own (and closed) the pipes.
                    _end_child(child, reap_seconds=2.0)
                    raise
                finally:
                    release_seat(child.pid)
                proc = subprocess.CompletedProcess(cmd, child.returncode, out, err)
            except FileNotFoundError as exc:
                missing = ("claude CLI not found. Install Claude Code and ensure `claude` is on "
                           "PATH.")
                harness_failures.note_engine_error(missing, engine=self.engine)
                raise RuntimeError(missing) from exc
            except subprocess.TimeoutExpired as exc:
                raise RuntimeError(f"claude code agent timed out after {timeout}s") from exc
            if proc.returncode == 0:
                parsed = _parse_cli_output(proc.stdout)
                _mark_live(parsed, live_written)
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
                if isinstance(parsed, dict):
                    parsed["_billing"] = getattr(self, "_billing", "")
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
                    # Its turns were written live too: the batch fallback re-wrote all 321 of a
                    # HelpDesk session's turns at its turn cap (crun_566f8767, 05:41).
                    _mark_live(data, live_written)
                    data["_billing"] = getattr(self, "_billing", "")
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
            # A REFUSED LOGIN IS CONFIRMED BEFORE IT IS BELIEVED. Every caller stops for good
            # on one (the run fails, Setup shows the subscription broken), and on boss-1 a
            # single research call said "Not logged in" between successes on the same login
            # seconds either side. Ask once more — with the credential resolved afresh, since
            # the env is rebuilt per attempt — and only a second refusal is raised as one.
            from .model_failures import LOGIN_RECHECK_SECONDS, is_login_rejected

            if is_login_rejected(detail) and not login_rechecked:
                login_rechecked = True
                time.sleep(LOGIN_RECHECK_SECONDS)
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
            # The engine's own words, read before they are raised: a harness failure (a refused
            # login, an account with no credit, a crash) ends the run with its verdict.
            harness_failures.note_engine_error(
                f"{last_error}\n{(proc.stderr or '')[-2000:]}", engine=self.engine)
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
        if isinstance(data.get("_rate_limit"), dict):
            usage["rate_limit"] = data["_rate_limit"]
        if data.get("_billing"):
            usage["billing"] = data["_billing"]
        return usage, cost_micros

    @tidies_workspace
    def run_role(
        self, prompt: str, cwd: Path, *, read_only: bool = False, model: Optional[str] = None,
        resume_session_id: Optional[str] = None, max_turns: Optional[int] = None,
        parallel_agents: int = 0, max_budget_usd: Optional[float] = None,
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
                                seat=("review" if read_only else "agent"),
                                parallel_agents=0 if read_only else int(parallel_agents or 0),
                                **({"max_budget_usd": max_budget_usd}
                                   if max_budget_usd is not None else {}))
        except Exception as exc:
            failed_model = model or getattr(self.settings, "claude_cli_model", None) or ""
            record_llm_call(engine_name=self.engine, usage={"model": failed_model},
                            latency_ms=int((time.monotonic() - started) * 1000),
                            ok=False, error=str(exc),
                            billing=getattr(self, "_billing", None) or None)
            raise
        usage, cost_micros = self._usage(data, self.settings)
        _remember_shared(data.get("session_id"), _shared_for(prompt, resume_session_id)[0])
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
            "stop_reason": ("turn_cap" if data.get("subtype") == "error_max_turns" else "done"),
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
        tests = run_until_failure(sandbox, spec.test_commands)
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


class _ToolsUnsupported(Exception):
    """The endpoint cannot do function calling; use the legacy single-shot path."""


_TOOLS_UNSUPPORTED_MARKERS = ("tools", "tool_calls", "function calling", "function_call")
_TOOLS_UNSUPPORTED_SHAPES = (
    "400", "invalid", "unsupported", "not support", "unrecognized", "unknown parameter",
)
#: A transient failure must NEVER be read as a capability gap - a rate limit that
#: permanently downgraded the lane would be a silent quality regression, which is exactly
#: the failure mode BOSS refuses everywhere else.
_TRANSIENT_MARKERS = (
    "rate limit", "429", "timeout", "timed out", "connection", "502", "503", "504",
    "overloaded", "temporarily",
)


def _looks_like_tools_unsupported(exc: Exception) -> bool:
    text = f"{type(exc).__name__}: {exc}".lower()
    if any(w in text for w in _TRANSIENT_MARKERS):
        return False
    return (any(s in text for s in _TOOLS_UNSUPPORTED_SHAPES)
            and any(m in text for m in _TOOLS_UNSUPPORTED_MARKERS))


class NativeCodeAgent:
    """Model-agnostic code agent on the raw chat API (OpenAI or Azure, via OpenAIAdapter).

    The host-side analogue of the guest native loop: the role prompt is the *guidance*; the
    model impersonates the role. It asks the model for whole-file writes as JSON, applies them
    in the sandbox working tree, and (for ``run``) re-runs the tests and iterates. No CLI or
    keychain dependency — works with any chat model the operator's lane routes to.
    """

    engine = "native"
    #: No session store (BOSS seeds a new session instead), no effort or sub-agents; a real
    #: read-only toolset, a turn budget, an estimated cost and a per-turn transcript.
    capabilities = EngineCapabilities(read_only=True, turn_limit=True, reports_cost=True,
                                      streams_turns=True)

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

        # Hermetic callers pass self=None to exercise the packer without an LLM
        # adapter. A live agent still clamps to its configured file budget.
        settings = getattr(self, "settings", None)
        if settings is None:
            requested = int(limit or 0)
            if requested <= 0:
                return ""
            return build_repo_context(cwd, task=task, limit=requested)
        configured = int(getattr(settings, "code_agent_context_files", 14) or 0)
        if configured <= 0:
            return ""
        file_limit, max_bytes, head_bytes = groq_qwen_context_limits(
            model or getattr(settings, "openai_model", None),
            base_url=str(getattr(settings, "openai_base_url", "") or ""),
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
        return build_repo_context(cwd, task=task, limit=limit)

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
        with RepoFiles(cwd) as writer:
            for f in files or []:
                path = f.get("path") or ""
                writer.write_text(path, f.get("content", ""))
                written.append(path)
        return written

    # --- tool-calling loop (BOSS-407) ------------------------------------
    _TOOL_SYS_EDIT = (
        "You are a senior software engineer working in a checked-out git repository.\n"
        "Use your tools to find the relevant code, read it, and make the smallest correct "
        "change. Prefer edit_file over write_file for files that already exist. Run the "
        "test/build commands with bash and iterate until they pass.\n"
        "Do NOT create branches, commit, or push - BOSS handles git, the PR and the gate.\n"
        "When you are done, reply with a short summary of WHAT you changed and WHY, and "
        "make no further tool calls."
    )
    _TOOL_SYS_REVIEW = (
        "You are an expert reviewer working in a checked-out git repository.\n"
        "Use your read-only tools to investigate. You cannot change files, and you must "
        "not ask to - report what you find.\n"
        "When you are done, reply with your findings, including any verdict line the task "
        "asks for, and make no further tool calls."
    )

    def _tools_enabled(self) -> bool:
        return bool(getattr(self.settings, "native_agent_tools_enabled", True))

    def _max_turns(self, read_only: bool = False) -> int:
        attr = "native_agent_review_max_turns" if read_only else "native_agent_max_turns"
        return max(1, int(getattr(self.settings, attr, 12 if read_only else 40)
                          or (12 if read_only else 40)))

    def _run_tool_loop(
        self, prompt: str, cwd: Path, *, read_only: bool, model: Optional[str],
        max_turns: Optional[int] = None,
    ) -> dict:
        """Drive a real multi-turn tool-calling conversation against the sandbox.

        One `run_role` call is one AGENT SESSION, not one completion - that is the whole
        point of this method. The governed pipeline calls `run_role` once per step, so
        before this existed the native lane got exactly ONE model turn per step while the
        claude_code lane got up to 90.
        """
        from .code_agent_tools import SandboxToolset, tool_schemas
        from .openai_adapter import estimate_cost_usd_micros

        from .code_agent_tools import resolve_autonomy

        autonomy = resolve_autonomy(self.settings, read_only=read_only)
        toolset = SandboxToolset(
            cwd,
            read_only=read_only,
            command_runner=getattr(self, "command_runner", None),
            settings=self.settings,
            autonomy=autonomy,
        )
        schemas = tool_schemas(read_only, autonomy=autonomy)
        guide = load_repo_guide(
            cwd, max_chars=int(getattr(self.settings, "repo_guide_max_chars", 32_000)))
        system = (self._TOOL_SYS_REVIEW if read_only else self._TOOL_SYS_EDIT) + _PARALLEL_TOOLS_RULE
        if guide:
            system += "\n\nREPO GUIDE (the repository's own instructions - follow them):\n" + guide
        messages: list[dict] = [
            {"role": "system", "content": system},
            {"role": "user", "content": (
                f"{prompt}\n\n{self._repo_context(cwd, task=prompt, model=model)}")},
        ]

        total_cost = 0
        usage_last: dict = {}
        turns = 0
        summary = ""
        tool_calls_made = 0
        # THE CALLER'S TURN BUDGET when it gives one (a continuation, an escalated reviewer),
        # as every engine honours it; the lane's own default otherwise.
        max_turns = int(max_turns) if max_turns else self._max_turns(read_only)
        capped = True
        stop_reason = "turn_cap"
        # THE STAGE'S WALL-TIME DEADLINE bounds this session as it bounds a CLI session
        # (_cap_to_deadline): checked before every model turn.
        deadline = None if read_only else _stage_deadline()
        # THE TRANSCRIPT, in the common schema (agent_turns): the prompt row first, then one row
        # per completion, tool call and tool result, so the profile and the live view read a
        # native session exactly as they read a CLI one.
        transcript_index = [0]

        def _turn(row: dict) -> None:
            row["turn_index"] = transcript_index[0]
            transcript_index[0] += 1
            try:
                record_agent_turns([row], engine_name=self.engine, model=model or "")
            except Exception:  # noqa: BLE001 — a transcript never fails a build
                pass

        try:
            record_agent_turns([], engine_name=self.engine, model=model or "", prompt=prompt)
        except Exception:  # noqa: BLE001
            pass

        for _ in range(max_turns):
            if deadline is not None and time.time() >= deadline:
                stop_reason = "deadline"
                break
            turns += 1
            started = time.monotonic()
            try:
                msg, usage = self._llm.create_chat_completion(
                    messages,
                    tools=schemas,
                    model_override=model or None,
                    redact=getattr(self, "redact", False),
                )
            except Exception as exc:
                record_llm_call(engine_name=self.engine, usage={"model": model or ""},
                                latency_ms=int((time.monotonic() - started) * 1000),
                                ok=False, error=str(exc))
                # An endpoint without function calling must degrade VISIBLY and only on the
                # FIRST turn. A half-toolled conversation is worse than a clean single-shot
                # one, and a silent downgrade is the failure mode BOSS refuses elsewhere.
                if turns == 1 and _looks_like_tools_unsupported(exc):
                    _log.warning(
                        "native agent: endpoint rejected tool calling (%s) - falling back to "
                        "the legacy single-shot path for this step. Set "
                        "BOSS_NATIVE_AGENT_TOOLS_ENABLED=false to make that the default.",
                        type(exc).__name__)
                    raise _ToolsUnsupported() from exc
                raise
            cost = estimate_cost_usd_micros(usage)
            total_cost += int(cost or 0)
            usage_last = usage or usage_last
            record_llm_call(engine_name=self.engine, usage=usage, cost_usd_micros=cost,
                            latency_ms=int((time.monotonic() - started) * 1000), turns=1)

            calls = msg.get("tool_calls") or []
            said = _as_text(msg.get("content")).strip()
            if said:
                _turn({"direction": "completion", "content": said,
                       "tokens": int((usage or {}).get("completion_tokens") or 0)})
            if not calls:
                summary = said or summary
                capped = False
                stop_reason = "done"
                break

            messages.append(msg.get("raw") or {
                "role": "assistant",
                "content": msg.get("content"),
                "tool_calls": [
                    {"id": c["id"], "type": "function",
                     "function": {"name": c["name"], "arguments": c["arguments"]}}
                    for c in calls
                ],
            })
            for call in calls:
                tool_calls_made += 1
                try:
                    args = json.loads(call.get("arguments") or "{}")
                    if not isinstance(args, dict):
                        args = {}
                except Exception:
                    args = {}
                _turn({"direction": "tool_call", "tool_name": call.get("name") or "",
                       "content": args})
                _t0 = time.monotonic()
                outcome = toolset.call(call.get("name") or "", args)
                _turn({"direction": "tool_result", "tool_name": call.get("name") or "",
                       "content": outcome.text, "ok": not bool(getattr(outcome, "is_error", False)),
                       "latency_ms": int((time.monotonic() - _t0) * 1000)})
                messages.append({
                    "role": "tool",
                    "tool_call_id": call.get("id"),
                    "content": outcome.text,
                })

        if capped:
            # The turn cap is a real outcome, not a crash: BOSS re-runs the tests itself
            # and the gate decides. Say so in the summary so the evidence trail shows WHY
            # the step stopped where it did — in the ONE phrase the pipeline reads as a partial
            # attempt on every engine (engine_contract.PARTIAL_MARKER).
            from .engine_contract import PARTIAL_MARKER

            summary = (summary + " ") if summary else ""
            summary += (f"[boss] stopped at the {max_turns}-turn cap for the native lane; "
                        "the work done so far is in the diff. " + PARTIAL_MARKER)
            _log.warning("native agent hit the %s-turn cap (read_only=%s)", max_turns, read_only)

        if not summary:
            summary = f"(no summary; {tool_calls_made} tool call(s) made)"
        return {
            "summary": summary.strip(),
            "usage": usage_last,
            "cost_usd_micros": total_cost,
            "turns": turns,
            "model": usage_last.get("model") or model,
            "tool_calls": tool_calls_made,
            "files_touched": sorted(set(toolset.written_paths)),
            "stop_reason": stop_reason,
        }

    @tidies_workspace
    def run_role(
        self, prompt: str, cwd: Path, *, read_only: bool = False, model: Optional[str] = None,
        resume_session_id: Optional[str] = None,  # no session store: engine_contract seeds one
        max_turns: Optional[int] = None,
        **_ignored,  # parallel_agents: sub-agents are a Claude CLI capability
    ) -> dict:
        if self._tools_enabled():
            try:
                return self._run_tool_loop(prompt, cwd, read_only=read_only, model=model,
                                           max_turns=max_turns)
            except _ToolsUnsupported:
                pass  # fall through to the legacy path; already logged at WARNING
        return self._run_single_shot(prompt, cwd, read_only=read_only, model=model)

    def _run_single_shot(
        self, prompt: str, cwd: Path, *, read_only: bool = False, model: Optional[str] = None,
    ) -> dict:
        """Legacy path: one completion, whole-file JSON writes, no tools.

        Kept for endpoints without function calling. It is a floor, not the default -
        see `_run_tool_loop` for why.
        """
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
        written: list[str] = []
        if not read_only:
            written = self._apply_files(cwd, data.get("files", []))
        summary = _as_text(data.get("summary")).strip() or "(no summary)"
        # The same transcript shape as every engine: the prompt, then what it said and wrote.
        try:
            record_agent_turns(
                [{"direction": "completion", "content": summary, "turn_index": 0}]
                + [{"direction": "tool_call", "tool_name": "Write", "turn_index": i + 1,
                    "content": {"file_path": path}} for i, path in enumerate(written)],
                engine_name=self.engine, model=model or usage.get("model") or "", prompt=prompt)
        except Exception:  # noqa: BLE001 — a transcript never fails a build
            pass
        return {
            "summary": summary,
            "usage": usage,
            "cost_usd_micros": cost_micros,
            "turns": 1,
            "tool_calls": len(written),
            "model": usage.get("model"),
            "stop_reason": "done",
        }

    def run(
        self, spec: CodeTaskSpec, repo: RepoContext, sandbox: EphemeralSandbox
    ) -> CodeAgentResult:
        if sandbox.path is None:
            raise RuntimeError("Sandbox must be provisioned before running the agent.")
        # BOSS-407: the tool loop's `bash` runs through the sandbox's own runner.
        self.command_runner = lambda cmd, timeout=None: sandbox.run(cmd, timeout=timeout)
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
            tests = run_until_failure(sandbox, spec.test_commands)
            if (all(t.ok for t in tests) if tests else False):
                break
            feedback = "\n\n".join(
                f"$ {t.command} (exit {t.exit_code})\n{((t.stdout or '') + (t.stderr or ''))[-1500:]}"
                for t in tests if not t.ok
            )[:6000]
        tests = run_until_failure(sandbox, spec.test_commands)
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
    "gemini": ("google_api_key", "gemini_base_url"),
    "xai": ("xai_api_key", "xai_base_url"),
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

    _OWN = frozenset({"_inner", "_limiter", "engine", "_provider"})

    def __init__(self, inner: "CodeAgent", limiter):
        self._inner = inner
        self._limiter = limiter
        self.engine = getattr(inner, "engine", None)
        self._provider = provider_of(self.engine)

    def __getattr__(self, name):  # delegate anything not overridden (called only on miss)
        if name == "_inner":      # built without __init__ (copy, pickle): no endless recursion
            raise AttributeError(name)
        return getattr(self._inner, name)

    def __setattr__(self, name, value):
        # WRITES REACH THE AGENT TOO. The pipeline sets `redact` (data boundary) and
        # `command_runner` (the native lane's bash) on the agent it holds — this wrapper — and
        # the agent reads them off itself. Kept here, they never arrived: a restricted task's
        # prompts went out unredacted and native `bash` ran outside the sandbox runner.
        if name in self._OWN:
            object.__setattr__(self, name, value)
        else:
            setattr(self._inner, name, value)

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
                billing=getattr(self._inner, "_billing", None) or None,
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
    mcp: Optional[Any] = None,
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
    agent = _build_code_agent(bound, engine)
    if mcp is not None and hasattr(agent, "mcp_harness"):
        # `mcp` (code_task_mcp.HarnessMcp): the servers the agent this task runs as holds.
        agent.mcp_harness = mcp
    # THE ENGINE CONTRACT (engine_contract): every engine, whatever it is, returns the common
    # result schema, gets a session id the warm paths can continue, a read-only review and a
    # turn-cap marker the pipeline recognises. No caller ever holds a raw adapter.
    from .engine_contract import contracted

    return _wrap_paced(contracted(agent), bound)


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
        # The subscription login or the console's Anthropic API key, in the operator's order
        # (claude_credential). NOT `resolve_project_credential(provider="anthropic")`: the login
        # and a stored API key are both provider "anthropic", and the newest of them would be
        # handed to the CLI as its OAuth token.
        from .claude_credential import bind_claude_credential

        return bind_claude_credential(settings, connection=connection, project_id=project_id)
    if lane == "claude_agent_sdk":
        # The Anthropic API key ONLY (never the subscription login): Agent SDK usage is metered
        # on the operator's API account (agent_sdk_engine).
        from .claude_credential import bind_anthropic_api_key

        return bind_anthropic_api_key(settings, connection=connection, project_id=project_id)
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
        key_field, url_field = _PROVIDER_LANES[lane]
        selected = _project_api_model_binding(connection, settings, project_id, lane)
        if selected is not None:
            key, model, base_url = selected
            updates = {key_field: key}
            if model:
                updates["openai_model"] = model
            if base_url:
                updates[url_field] = base_url
            return settings.model_copy(update=updates)
        key = resolve_project_credential(
            connection, settings, project_id=project_id,
            provider=provider_of(lane), env_fallback_attr=key_field,
        )
        return settings.model_copy(update={key_field: key})
    if lane in ("native", "openai"):
        selected = _project_api_model_binding(connection, settings, project_id, "native")
        if selected is not None:
            key, model, base_url = selected
            updates = {"openai_api_key": key}
            if model:
                updates["openai_model"] = model
            if base_url:
                updates["openai_base_url"] = base_url
            return settings.model_copy(update=updates)
        key = resolve_project_credential(
            connection,
            settings,
            project_id=project_id,
            provider=provider_of(lane),
            env_fallback_attr="openai_api_key",
        )
        return settings.model_copy(update={"openai_api_key": key})
    return settings


LOCAL_MODEL_HOSTS = {"host.docker.internal", "localhost", "127.0.0.1", "::1"}


def is_local_model_endpoint(url: str) -> bool:
    """A model server on the install machine (Ollama), as the containers or the host see it."""
    from urllib.parse import urlparse

    try:
        host = urlparse((url or "").strip()).hostname
    except ValueError:
        return False
    return bool(host) and host in LOCAL_MODEL_HOSTS


def choose_native_source(rows: list[dict], *, env_hosted: bool, metadata) -> dict | None:
    """Which saved connection the native (OpenAI-compatible) lane uses.

    Newest first, as saved — except that a local model server (an "Anything OpenAI-compatible"
    connection to host.docker.internal) never displaces a hosted connection, whichever was
    saved last. A hosted key in the environment counts as hosted too: then the local row is
    skipped and the caller falls back to that key.
    """
    local = [row for row in rows
             if str(row.get("source_id") or "").startswith("setup-custom")
             and is_local_model_endpoint(str(metadata(row).get("base_url") or ""))]
    hosted = [row for row in rows if row not in local]
    if hosted:
        return hosted[0]
    if local and not env_hosted:
        return local[0]
    return None


def _project_api_model_binding(
    connection, settings: Settings, project_id: str, lane: str,
) -> tuple[str, str, str] | None:
    """Use one saved source's key, model, and URL as a single project binding.

    Reading the key by provider but the model from process defaults made a
    successful setup choose, for example, an xAI key with an OpenAI model ID.
    A custom endpoint could similarly be offered as `native` yet never run.
    """
    from .secret_backends import resolve_secret_ref

    from .setup_service import api_source_provider, last_check_broken

    providers = ("openai", "custom") if lane == "native" else (lane,)
    # Every connection of the lane's provider (`setup-<p>` and `setup-<p>-<n>`): one source per
    # connection. Newest first, but a connection whose last check was refused never wins over
    # one that works.
    rows = [row for row in connection.execute(text(
        "SELECT source_id, credential_secret_ref_id, supported_models, connection_metadata, "
        "observed_capacity "
        "FROM entitlement_sources WHERE project_id=:project_id "
        "AND source_id LIKE 'setup-%' "
        "AND credential_secret_ref_id IS NOT NULL "
        "ORDER BY updated_at DESC NULLS LAST, source_id"
    ), {"project_id": project_id}).mappings().all()
        if api_source_provider(row["source_id"]) in providers]
    rows.sort(key=lambda row: last_check_broken(row["connection_metadata"],
                                                row["observed_capacity"]))

    def object_value(value, expected):
        if isinstance(value, str):
            value = json.loads(value)
        return value if isinstance(value, expected) else expected()

    row = choose_native_source(
        [dict(item) for item in rows],
        env_hosted=bool(str(getattr(settings, "openai_api_key", "") or "").strip())
        and not is_local_model_endpoint(str(getattr(settings, "openai_base_url", "") or "")),
        metadata=lambda item: object_value(item.get("connection_metadata"), dict),
    ) if lane == "native" else (dict(rows[0]) if rows else None)
    if row is None:
        return None

    models = object_value(row["supported_models"], list)
    metadata = object_value(row["connection_metadata"], dict)
    model = next((item.strip() for item in models if isinstance(item, str) and item.strip()), "")
    base_url = metadata.get("base_url")
    return (resolve_secret_ref(connection, row["credential_secret_ref_id"], settings),
            model, base_url.strip() if isinstance(base_url, str) else "")


def _build_azure_agent(settings: Settings) -> CodeAgent:
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


def _build_provider_lane_agent(engine: str) -> Callable[[Settings], CodeAgent]:
    """A NativeCodeAgent bound to one provider's DEDICATED credential pair."""

    def build(settings: Settings) -> CodeAgent:
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

    return build


def _register_builtin_harnesses() -> None:
    """Declare the harnesses BOSS ships with (BOSS-409).

    `verified` is reserved for harnesses a governed code task has actually completed on.
    Everything registered here has; new adapters start at False and say so.
    """
    from .cli_harness import CLAUDE_CLI_RUNTIME, CLAUDE_FAILURE_PATTERNS
    from .harness_registry import register_harness

    # `runtime`: what each engine execs (harness_registry.RuntimeBinary) — held to the image by
    # test_every_engine_binary_is_in_the_image, looked for on PATH by the `sandbox.engine` check.
    register_harness(
        "claude_code", ClaudeCodeAgent, kind="cli", verified=True,
        requires=("CLAUDE_CODE_OAUTH_TOKEN or ANTHROPIC_API_KEY", "claude CLI on PATH"),
        description="Claude Code headless (`claude -p`) with its own file/bash tools, "
                    "scoped to the sandbox working tree.",
        runtime=CLAUDE_CLI_RUNTIME, failure_patterns=CLAUDE_FAILURE_PATTERNS,
    )
    from .agent_sdk_engine import ENGINE as _SDK_ENGINE, SDK_PACKAGE, build_agent

    # Unverified until a governed task completes on it: Auto never picks it, an operator does.
    register_harness(
        _SDK_ENGINE, build_agent, kind="sdk", verified=False,
        requires=("an Anthropic API key (Repo and models -> Anthropic API, or ANTHROPIC_API_KEY)",
                  f"the {SDK_PACKAGE} Python package"),
        description="Claude Agent SDK on an Anthropic API key (metered Agent SDK usage): "
                    "Claude Code's tools and sessions, scoped to the sandbox working tree.",
        # The SDK runs the CLI bundled in its own package (checked by `sdk_available`). No
        # bubblewrap/socat: the session holds a model-proxy run token, not the key, so the CLI's
        # own sandbox (CLAUDE_CODE_SUBPROCESS_ENV_SCRUB) is not needed and is not started.
        runtime=(), failure_patterns=CLAUDE_FAILURE_PATTERNS,
    )
    register_harness(
        "native", NativeCodeAgent, kind="native", verified=True, aliases=("openai",),
        requires=("OPENAI_API_KEY", "OPENAI_BASE_URL (optional)"),
        description="Engine-owned tool-calling agent over any OpenAI-compatible chat API.",
    )
    register_harness(
        "azure", _build_azure_agent, kind="native", verified=True,
        requires=("AZURE_OPENAI_KEY", "AZURE_OPENAI_ENDPOINT", "AZURE_OPENAI_DEPLOYMENT"),
        description="Azure OpenAI lane, used as an independent gate/verifier under VAG.",
    )
    # CLI harnesses: declared as specs, not written as classes. NONE is verified — a spec
    # taken from a CLI's documentation is a hypothesis until a governed task completes on it.
    from .cli_harness import BUILTIN_CLI_HARNESSES, GenericCliCodeAgent, load_specs

    def _cli_builder(harness_name: str):
        def build(settings: Settings) -> CodeAgent:
            spec = load_specs(settings).get(harness_name)
            if spec is None:  # pragma: no cover - only if an override deletes a built-in
                raise RuntimeError(f"no CLI harness spec for {harness_name!r}")
            if harness_name == "codex":
                # A purpose-written driver held to the engine contract: sessions, a transcript,
                # usage and effort from Codex's own interfaces (codex_code_agent).
                from .codex_code_agent import CodexCodeAgent

                return CodexCodeAgent(settings, spec)
            return GenericCliCodeAgent(settings, spec)
        return build

    for cli_name, cli_spec in BUILTIN_CLI_HARNESSES.items():
        if not cli_spec.registers_harness:
            # claude_code: already registered above, with a purpose-written driver. Its spec
            # exists so the console can detect and connect it, not to replace ClaudeCodeAgent
            # with a generic `claude -p` wrapper — and registering it here would collide with
            # the real one rather than override it, which is the correct outcome either way.
            continue
        register_harness(
            cli_name, _cli_builder(cli_name), kind="cli", verified=cli_spec.verified,
            requires=(f"{cli_spec.binary} on PATH", "signed in to that CLI"),
            description=cli_spec.description, runtime=tuple(cli_spec.runtime),
            failure_patterns=tuple(cli_spec.failure_patterns or ()),
        )
    # THE CONFORMANCE ENGINE: the contract with no model behind it (scripted_engine). Hidden from
    # operators and refused unless the conformance gate enabled it.
    from .scripted_engine import ENGINE as _SCRIPTED, build as _build_scripted

    register_harness(
        _SCRIPTED, _build_scripted, kind="conformance", verified=False,
        requires=("BOSS_SCRIPTED_ENGINE=1 (conformance gate only)",),
        description="BOSS's scripted conformance engine: a deterministic script per stage, "
                    "no model, no cost.",
    )
    for lane, (key_field, url_field) in _PROVIDER_LANES.items():
        local = lane in ("ollama", "vllm")
        register_harness(
            lane, _build_provider_lane_agent(lane), kind="native", verified=True,
            requires=(key_field.upper(), url_field.upper()),
            description=("Local/self-hosted OpenAI-compatible endpoint."
                         if local else
                         "Direct provider lane on its own dedicated credential pair."),
        )


def _build_code_agent(settings: Settings, engine: Optional[str] = None) -> CodeAgent:
    """Resolve a harness by name through the registry (BOSS-409).

    This used to be an if/elif chain, which meant "bring your own coding agent" required
    editing core. Warp makes the harness a declared field (`harness.type`); so does BOSS
    now — see `harness_registry.register_harness`.
    """
    from .harness_registry import build_harness

    name = (engine or settings.code_agent_engine or "claude_code").lower()
    return build_harness(name, settings)


# Declared last: `_register_builtin_harnesses` and the builders it references are
# defined above, and `make_code_agent` appears EARLIER in this module than
# `_build_code_agent` does.
_register_builtin_harnesses()
