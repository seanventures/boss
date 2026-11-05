"""THE ENGINE CONTRACT: what BOSS needs from any coding engine, and what BOSS does when an engine
cannot give it.

BOSS is a harness independent of any model. Its BEHAVIOUR (stages, gates, fix rounds, the pull
request, events, the build profile, failure messages) must be identical on every engine; only
quality and speed may vary with the model. Until this module existed the pipeline had been tuned
and verified on Claude Code alone, and several of its branches quietly depended on what only that
CLI provides:

  * a warm fix round, the implement continuation and the test-budget trim ran only when the
    engine returned a `session_id` (Claude's `--resume`); on every other engine they were skipped;
  * the build profile, the transcript levels and the gates counted tools by Claude's names
    ("Write", "Edit", "Bash", "Read"), so a native or CLI engine's `write_file`/`apply_patch`/
    `shell` were invisible to them;
  * a turn cap was recognised only in Claude's words ("ran out of turns before finishing");
  * a stage's wall-time deadline bounded only the Claude session.

The contract is the minimal interface, the adapter boundary normalises everything an engine
reports into BOSS's common schema, and `ContractedAgent` supplies BOSS-side fallbacks for what an
engine lacks. `make_code_agent` wraps every engine in it, so no caller ever sees a raw adapter.

    run_role(prompt, cwd, *, read_only, model, resume_session_id, max_turns, parallel_agents)
        -> RoleResult (see RESULT_KEYS)

Capabilities an engine may lack, and BOSS's fallback for each:

    resume        -> a NEW session seeded with what the earlier one was asked and reported
                     (`seeded_prompt`); the caller still gets a session id to continue from.
    read_only     -> the reviewer runs on a throwaway COPY of the tree; nothing it writes reaches
                     the build (`_isolated_copy`).
    effort        -> none (the stage still runs; only depth may differ). Mapped per engine where
                     an engine has an equivalent.
    sub_agents    -> none: one session, which is the default anyway; BOSS's own split
                     (parallel_build) is engine-independent.
    turn_limit    -> the wall-time deadline still bounds the session.
    reports_cost  -> `cost_unreported: True`, never an invented number.
    streams_turns -> one prompt row and one completion row per session, so every stage still has
                     a transcript in the common schema.
"""
from __future__ import annotations

import logging
import os
import shutil
import tempfile
import threading
import uuid
from collections import OrderedDict
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Optional, Protocol, runtime_checkable

log = logging.getLogger(__name__)

# ── Tool names: one vocabulary for every engine ───────────────────────────────────────────────

#: BOSS's canonical tool names. They are the names the profile, the transcript levels, the
#: gates and the trims were written against, so normalising every engine ONTO them changes
#: nothing for the engine they came from and makes every other engine visible.
READ, WRITE, EDIT, BASH, GREP, GLOB, LS = "Read", "Write", "Edit", "Bash", "Grep", "Glob", "LS"
AGENT, WEB_FETCH, WEB_SEARCH, TODO = "Agent", "WebFetch", "WebSearch", "TodoWrite"
CANONICAL_TOOLS = frozenset({READ, WRITE, EDIT, BASH, GREP, GLOB, LS, AGENT, WEB_FETCH,
                             WEB_SEARCH, TODO, "MultiEdit", "NotebookEdit", "NotebookRead",
                             "TaskOutput"})
WRITE_TOOLS = frozenset({WRITE, EDIT, "MultiEdit", "NotebookEdit"})

#: Every other engine's spelling, lower-cased. NativeCodeAgent (code_agent_tools), Codex
#: (shell/apply_patch/local_shell/exec_command), Aider/Cursor/Grok and generic MCP-style names.
_TOOL_ALIASES: dict[str, str] = {
    # read
    "read": READ, "read_file": READ, "view": READ, "view_file": READ, "open_file": READ,
    "cat": READ, "readfile": READ,
    # write
    "write": WRITE, "write_file": WRITE, "create_file": WRITE, "create": WRITE,
    "writefile": WRITE, "new_file": WRITE,
    # edit
    "edit": EDIT, "edit_file": EDIT, "str_replace": EDIT, "str_replace_editor": EDIT,
    "str_replace_based_edit_tool": EDIT, "apply_patch": EDIT, "patch": EDIT,
    "replace": EDIT, "replace_in_file": EDIT, "search_replace": EDIT, "multiedit": "MultiEdit",
    # shell
    "bash": BASH, "shell": BASH, "sh": BASH, "local_shell": BASH, "exec_command": BASH,
    "run_command": BASH, "run_shell_command": BASH, "terminal": BASH, "execute": BASH,
    "run_terminal_cmd": BASH,
    # search
    "grep": GREP, "search": GREP, "rg": GREP, "ripgrep": GREP, "grep_search": GREP,
    "search_files": GREP, "codebase_search": GREP,
    "glob": GLOB, "find_files": GLOB, "file_search": GLOB,
    "ls": LS, "list_dir": LS, "list_directory": LS, "list_files": LS,
    # sub-agents (Claude Code's `Task` is the older name of `Agent`)
    "task": AGENT, "agent": AGENT, "subagent": AGENT, "spawn_agent": AGENT,
    # web
    "webfetch": WEB_FETCH, "web_fetch": WEB_FETCH, "fetch": WEB_FETCH,
    "websearch": WEB_SEARCH, "web_search": WEB_SEARCH,
    "todowrite": TODO, "todo_write": TODO, "update_plan": TODO,
}

#: Argument keys the profile reads (`file_path`, `command`), from every engine's spelling.
_PATH_KEYS = ("file_path", "path", "filename", "file", "target_file")
_COMMAND_KEYS = ("command", "cmd", "script")


def canonical_tool_name(name: Any) -> str:
    """BOSS's name for a tool, whatever the engine called it ('' stays '').

    A name BOSS does not know is kept as the engine spelled it (an MCP tool, say): it is still
    counted as a tool call, it simply is not one the profile classifies.
    """
    raw = str(name or "").strip()
    if not raw or raw in CANONICAL_TOOLS:
        return raw
    return _TOOL_ALIASES.get(raw.lower(), raw)


def canonical_tool_input(tool_name: str, args: Any) -> Any:
    """The tool's arguments with the keys the profile reads under BOSS's names.

    Only adds keys; never drops what the engine sent. A non-dict (a clipped string) is
    returned unchanged.
    """
    if not isinstance(args, Mapping):
        return args
    out = dict(args)
    name = canonical_tool_name(tool_name)
    if name in (READ, WRITE, EDIT, "MultiEdit", "NotebookEdit") and "file_path" not in out:
        for key in _PATH_KEYS[1:]:
            if isinstance(out.get(key), str):
                out["file_path"] = out[key]
                break
    if name == BASH and "command" not in out:
        for key in _COMMAND_KEYS[1:]:
            value = out.get(key)
            if isinstance(value, list):
                value = " ".join(str(v) for v in value)
            if isinstance(value, str):
                out["command"] = value
                break
    return out


def normalise_turn(turn: Mapping[str, Any]) -> dict[str, Any]:
    """One transcript turn in the common schema (agent_turns.record_turns writes only these)."""
    out = dict(turn)
    if out.get("tool_name"):
        out["tool_name"] = canonical_tool_name(out["tool_name"])
        if out.get("direction") == "tool_call":
            out["content"] = canonical_tool_input(out["tool_name"], out.get("content"))
    return out


# ── Results ───────────────────────────────────────────────────────────────────────────────────

#: Every engine's run_role returns at least these keys (ContractedAgent fills what is missing).
RESULT_KEYS = ("summary", "usage", "cost_usd_micros", "cost_unreported", "turns", "tool_calls",
               "model", "session_id", "stop_reason", "engine")

#: Why a session stopped. "turn_cap" and "deadline" are partial attempts, never verdicts.
#: "awaiting_approval" is a build-agent tool call that is waiting, not a finished session
#: and not a failure (tool_gateway): normalise must not rewrite it to "done".
STOP_REASONS = ("done", "turn_cap", "deadline", "error", "awaiting_approval")

#: THE ONE PHRASE a partial attempt carries, whatever engine made it. The pipeline's
#: `check_is_inconclusive` and the implement continuation read it; before this, only Claude's
#: own words were recognised and a native engine's turn cap read as a finished judgement.
PARTIAL_MARKER = "(ran out of turns before finishing — partial attempt; resume to continue)"

#: The usage fields the ledger, the profile and the gates read.
USAGE_KEYS = ("model", "prompt_tokens", "completion_tokens", "cache_read_tokens",
              "cache_write_tokens", "total_tokens", "cost_usd_micros")


def normalise_usage(usage: Any, *, model: str = "", cost_usd_micros: int = 0) -> dict[str, Any]:
    """Usage in BOSS's schema, from Claude's, OpenAI's or nothing."""
    u = dict(usage) if isinstance(usage, Mapping) else {}
    out: dict[str, Any] = {k: v for k, v in u.items()}
    out["model"] = str(u.get("model") or model or "")
    out["prompt_tokens"] = int(u.get("prompt_tokens") or u.get("input_tokens") or 0)
    out["completion_tokens"] = int(u.get("completion_tokens") or u.get("output_tokens") or 0)
    out["cache_read_tokens"] = int(u.get("cache_read_tokens")
                                   or u.get("cache_read_input_tokens") or 0)
    out["cache_write_tokens"] = int(u.get("cache_write_tokens")
                                    or u.get("cache_creation_input_tokens") or 0)
    out["total_tokens"] = int(u.get("total_tokens") or (
        out["prompt_tokens"] + out["completion_tokens"] + out["cache_read_tokens"]
        + out["cache_write_tokens"]))
    out["cost_usd_micros"] = int(u.get("cost_usd_micros") or cost_usd_micros or 0)
    return out


def normalise_result(result: Any, *, engine: str, model: Optional[str] = None,
                     caps: Optional["EngineCapabilities"] = None) -> dict[str, Any]:
    """A run_role result in the common schema. Keeps every key the engine returned."""
    r = dict(result) if isinstance(result, Mapping) else {"summary": str(result or "")}
    caps = caps or EngineCapabilities()
    cost = int(r.get("cost_usd_micros") or 0)
    r["model"] = str(r.get("model") or model or "")
    if r["model"].strip().lower() == engine.strip().lower():
        r["model"] = ""   # an engine's name is not a model (see ContractedAgent.run_role)
    r["usage"] = normalise_usage(r.get("usage"), model=r["model"], cost_usd_micros=cost)
    r["cost_usd_micros"] = cost
    r["cost_unreported"] = bool(r.get("cost_unreported", not caps.reports_cost))
    r["turns"] = max(int(r.get("turns") or 0), 1)
    r["tool_calls"] = int(r.get("tool_calls") or 0)
    r.setdefault("session_id", None)
    r["engine"] = str(r.get("engine") or engine)
    summary = str(r.get("summary") or "").strip()
    stop = str(r.get("stop_reason") or "").strip()
    if stop not in STOP_REASONS:
        stop = "turn_cap" if "ran out of turns before finishing" in summary.lower() else "done"
    if stop in ("turn_cap", "deadline") and "ran out of turns before finishing" not in summary.lower():
        summary = (summary + "\n" if summary else "") + PARTIAL_MARKER
    r["stop_reason"] = stop
    r["summary"] = summary or f"({engine} produced no summary)"
    return r


# ── Capabilities ──────────────────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class EngineCapabilities:
    """What an engine can do natively. A False is never a failure: BOSS has a fallback for it."""

    #: Continues an earlier session by id (Claude's --resume).
    resume: bool = False
    #: Enforces a read-only mode for review seats.
    read_only: bool = False
    #: Honours a per-stage effort level / thinking cap.
    effort: bool = False
    #: Runs sub-agents inside one session.
    sub_agents: bool = False
    #: Obeys a max-turns budget.
    turn_limit: bool = False
    #: Reports what a session cost.
    reports_cost: bool = False
    #: Emits one transcript row per turn/tool call.
    streams_turns: bool = False
    #: Stops itself when a session's spend reaches a ceiling it is given (`max_budget_usd`).
    #: Without it the ceiling is enforced at the caller's next stage boundary.
    budget_limit: bool = False

    def as_dict(self) -> dict[str, bool]:
        return {k: bool(getattr(self, k)) for k in self.__dataclass_fields__}


def capabilities_of(agent: Any) -> EngineCapabilities:
    caps = getattr(agent, "capabilities", None)
    if isinstance(caps, EngineCapabilities):
        return caps
    inner = getattr(agent, "_inner", None)
    if inner is not None and inner is not agent:
        return capabilities_of(inner)
    return EngineCapabilities()


@runtime_checkable
class EngineAdapter(Protocol):
    """The minimal interface every engine adapter satisfies (CLI harnesses, NativeCodeAgent,
    the scripted conformance engine)."""

    engine: str
    capabilities: EngineCapabilities

    def run_role(self, prompt: str, cwd: Path, *, read_only: bool = False,
                 model: Optional[str] = None, resume_session_id: Optional[str] = None,
                 max_turns: Optional[int] = None, parallel_agents: int = 0) -> dict: ...

    def run(self, spec: Any, repo: Any, sandbox: Any) -> Any: ...


# ── Session fallback ──────────────────────────────────────────────────────────────────────────

#: Session ids BOSS issues for an engine that has no sessions of its own.
SESSION_PREFIX = "boss-sess-"
_SEED_PROMPT_CHARS = 24_000
_SEED_SUMMARY_CHARS = 4_000
_LEDGER_CAP = 512


class SessionLedger:
    """What each session was first asked and what it reported, keyed by session id.

    Process-local and bounded: a fallback, not a store. A session it does not know (another
    process, an evicted entry) is continued cold with the new prompt alone, which is what an
    engine with no sessions would have done anyway.
    """

    def __init__(self, cap: int = _LEDGER_CAP):
        self._cap = cap
        self._lock = threading.Lock()
        self._rows: "OrderedDict[str, dict[str, Any]]" = OrderedDict()

    def get(self, sid: Optional[str]) -> Optional[dict[str, Any]]:
        if not sid:
            return None
        with self._lock:
            row = self._rows.get(sid)
            return {"first_prompt": row["first_prompt"], "reports": list(row["reports"])} if row else None

    def record(self, sid: str, *, prompt: str, summary: str, parent: Optional[str] = None) -> None:
        with self._lock:
            base = self._rows.get(parent) if parent else None
            row = self._rows.get(sid) or {
                "first_prompt": (base or {}).get("first_prompt") or prompt,
                "reports": list((base or {}).get("reports") or []),
            }
            row["reports"].append(str(summary or "")[:_SEED_SUMMARY_CHARS])
            row["reports"] = row["reports"][-6:]
            self._rows[sid] = row
            self._rows.move_to_end(sid)
            while len(self._rows) > self._cap:
                self._rows.popitem(last=False)


LEDGER = SessionLedger()


def seeded_prompt(prompt: str, prior: Optional[Mapping[str, Any]]) -> str:
    """A new session that carries an earlier one: what it was asked, what it reported, then now."""
    if not prior:
        return prompt
    first = str(prior.get("first_prompt") or "")
    if len(first) > _SEED_PROMPT_CHARS:
        first = first[:_SEED_PROMPT_CHARS] + "\n…(clipped)"
    reports = "\n---\n".join(str(r) for r in prior.get("reports") or [] if str(r).strip())
    return (
        "CONTINUING AN EARLIER SESSION. This engine cannot resume a session, so BOSS gives you "
        "what that session held. The files in your working directory already contain its work.\n"
        "=== WHAT THE EARLIER SESSION WAS ASKED ===\n" + first + "\n"
        "=== WHAT IT REPORTED (most recent last) ===\n" + (reports or "(nothing)") + "\n"
        "=== YOUR TASK NOW ===\n" + prompt
    )


# ── Read-only fallback ────────────────────────────────────────────────────────────────────────

_COPY_IGNORE = shutil.ignore_patterns("node_modules", ".git", ".venv", "venv", "__pycache__",
                                      "dist", ".next", "build")


def _isolated_copy(cwd: Path) -> Path:
    """A throwaway copy of the tree for a reviewer that cannot be made read-only. Heavy
    directories are linked, not copied (a reviewer reads them; anything written through the link
    is a reviewer misbehaving, which the copy cannot fully prevent, and is why an engine with a
    real read-only mode is preferred for review)."""
    # The system temp directory, never beside the workspace: the reviewer's cwd can be an app
    # folder inside the repository, and a copy there would be an untracked tree a parallel
    # writer could commit.
    dest = Path(tempfile.mkdtemp(prefix="boss-review-copy-"))
    tree = dest / Path(cwd).name
    shutil.copytree(cwd, tree, symlinks=True, ignore=_COPY_IGNORE, dirs_exist_ok=False)
    for heavy in ("node_modules", ".git"):
        src = Path(cwd) / heavy
        if src.exists() and not (tree / heavy).exists():
            try:
                os.symlink(src, tree / heavy)
            except OSError:
                pass
    return tree


# ── The wrapper every engine runs in ──────────────────────────────────────────────────────────

class EngineSessionFailed(RuntimeError):
    """An engine's session failed (it exited non-zero, or said it errored)."""


class ContractedAgent:
    """Holds an engine to the contract. Transparent otherwise: attributes read AND written on it
    reach the engine (the pipeline sets `redact` and `command_runner` on the agent it holds)."""

    _OWN = frozenset({"_inner", "engine", "capabilities"})

    def __init__(self, inner: Any):
        object.__setattr__(self, "_inner", inner)
        object.__setattr__(self, "engine", str(getattr(inner, "engine", "") or "unknown"))
        object.__setattr__(self, "capabilities", capabilities_of(inner))

    def __getattr__(self, name: str) -> Any:
        # Called only on a miss. `_inner` itself missing (copy.copy, pickle build the object
        # without __init__) must not recurse through this method for ever.
        if name == "_inner":
            raise AttributeError(name)
        return getattr(self._inner, name)

    def __setattr__(self, name: str, value: Any) -> None:
        if name in self._OWN:
            object.__setattr__(self, name, value)
        else:
            setattr(self._inner, name, value)

    def run_role(self, prompt: str, cwd: Path, *, read_only: bool = False,
                 model: Optional[str] = None, resume_session_id: Optional[str] = None,
                 max_turns: Optional[int] = None, parallel_agents: int = 0, **extra: Any) -> dict:
        caps = self.capabilities
        if model and str(model).strip().lower() == self.engine.lower():
            # An engine's NAME is not a model (a CLI harness used to report its name as the
            # model, and a follow-up turn passed it back as `--model codex`).
            model = None
        native_resume = None
        parent = resume_session_id
        run_prompt = prompt
        if resume_session_id:
            if caps.resume and not str(resume_session_id).startswith(SESSION_PREFIX):
                native_resume = resume_session_id
            else:
                # NO NATIVE SESSION: a new one, seeded with what the earlier one held.
                run_prompt = seeded_prompt(prompt, LEDGER.get(resume_session_id))
        if not read_only and not getattr(self._inner, "adds_working_rules", False):
            # THE SAME WORKING RULES AND SANDBOX FACTS ON EVERY ENGINE. Claude's driver appends
            # them itself; without this, every other engine built without being told the
            # sandbox has no C compiler or that BOSS's install is a clean `npm install`.
            run_prompt = run_prompt + working_rules()
        kwargs: dict[str, Any] = dict(read_only=read_only, model=model,
                                      resume_session_id=native_resume, max_turns=max_turns,
                                      parallel_agents=(int(parallel_agents or 0)
                                                       if caps.sub_agents and not read_only else 0))
        kwargs.update(extra)
        work = Path(cwd)
        copy: Optional[Path] = None
        if read_only and not caps.read_only:
            # NO READ-ONLY MODE: the reviewer judges a copy; nothing it writes reaches the build.
            # The engine is told it is a review (its review prompt, timeout and argv) and that
            # BOSS holds the read-only invariant by isolation, so it must not refuse the seat.
            copy = _isolated_copy(work)
            work = copy
            kwargs["_contract_read_only_copy"] = True
        try:
            raw = _call(self._inner.run_role, run_prompt, work, kwargs)
        finally:
            if copy is not None:
                shutil.rmtree(copy.parent, ignore_errors=True)
        result = normalise_result(raw, engine=self.engine, model=model, caps=caps)
        if result["stop_reason"] == "error":
            # A FAILED SESSION IS A FAILURE ON EVERY ENGINE. Claude's driver raises on a non-zero
            # exit; a generic CLI used to hand its error text on as if it were the agent's
            # answer, so the next stage judged a crash as work.
            raise EngineSessionFailed(
                f"{self.engine} failed: {result['summary'][:300]}")
        if read_only and not caps.read_only:
            result["read_only_by"] = "copy"
        sid = result.get("session_id")
        if not sid or not caps.resume:
            # A session id for every session, whatever the engine: the pipeline's warm paths
            # (fix rounds, continuation, the test-budget trim) then run on every engine.
            sid = f"{SESSION_PREFIX}{uuid.uuid4().hex[:16]}"
            result["session_id"] = sid
            result["session_fallback"] = True
        LEDGER.record(sid, prompt=prompt, summary=result["summary"], parent=parent)
        return result

    def run_research(self, *args: Any, **kwargs: Any) -> Any:
        fn = getattr(self._inner, "run_research", None)
        if fn is None:
            raise NotImplementedError(f"the {self.engine!r} engine has no research mode")
        return fn(*args, **kwargs)

    def run(self, spec: Any, repo: Any, sandbox: Any) -> Any:
        return self._inner.run(spec, repo, sandbox)


def working_rules() -> str:
    """The writing-stage rules and the sandbox's facts BOSS gives every engine."""
    try:
        from .code_agent import _WRITING_RULE, sandbox_facts

        return _WRITING_RULE + sandbox_facts()
    except Exception:  # noqa: BLE001 — facts are guidance, never a reason to fail a stage
        return ""


def _call(fn: Any, prompt: str, cwd: Path, kwargs: dict[str, Any]) -> Any:
    """Call an adapter's run_role with only the keyword arguments it accepts (an adapter written
    before the contract may not take max_turns or parallel_agents)."""
    import inspect

    try:
        params = inspect.signature(fn).parameters
    except (TypeError, ValueError):
        return fn(prompt, cwd, **{k: v for k, v in kwargs.items() if not k.startswith("_")})
    if any(p.kind is inspect.Parameter.VAR_KEYWORD for p in params.values()):
        accepted = kwargs
    else:
        accepted = {k: v for k, v in kwargs.items() if k in params}
    return fn(prompt, cwd, **accepted)


def adapter_of(agent: Any) -> Any:
    """The engine adapter itself, under any wrappers (pacing, the contract)."""
    seen = 0
    while seen < 8 and getattr(agent, "_inner", None) is not None:
        agent = agent._inner
        seen += 1
    return agent


def contracted(agent: Any) -> Any:
    """`agent` held to the contract (idempotent)."""
    if isinstance(agent, ContractedAgent):
        return agent
    inner = getattr(agent, "_inner", None)
    if isinstance(inner, ContractedAgent):
        return agent
    return ContractedAgent(agent)
