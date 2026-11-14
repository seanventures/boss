"""SUB-AGENTS ON THE KERNEL (AP-P7, BOSSAPP-1058; docs/design/AGENT_PLATFORM.md §4.5, §5 Phase 7,
decisions D12 and D13).

A supervisor is just an agent whose spec lists sub-agents. This module is the `subagents`
middleware that gives such a run its coordination tools, and the rules every spawn passes:

    task(subagent, brief, mode)        SYNCHRONOUS: a child run in a fresh context (`isolated`,
                                       the default) or a copy of this conversation (`fork`); its
                                       one report, capped (default 1,500 tokens), is the tool
                                       result. `forward_verbatim` on the sub-agent's spec entry
                                       ends the parent with the report as its answer, unedited.
    start_subagent / check_subagent /  ASYNCHRONOUS: the child is a durable run on a worker
    update_subagent / cancel_subagent  (AP-P2), returned as an A2A v1.0 Task (protocols/
    / list_subagents                   a2a_types: id = the child run, contextId = its thread,
                                       INPUT_REQUIRED = an open interrupt). The parent's thread is
                                       told when one finishes (services/subagent_runs.notify_parent).

WHAT EVERY CHILD GETS, whatever target runs it (`SubagentTarget`: a local kernel run, or P5's A2A
client adapter for a remote agent):

  * AN EXCHANGED RUN TOKEN. The `identity` port performs the RFC 8693 exchange from the parent's
    run token (identity/runs.spawn_child): a delegation NARROWER than the parent's (a read-only
    child gets only the parent's read permissions), bound to the child's own key, under a run id
    nested in the parent's — so revoking the parent's Passport or grant cascades to the child in
    the same transaction (identity/cascade.py).
  * A BUDGET SLICE. Multi-agent costs about 15x the tokens of chat (Anthropic, design §1.5), so it
    is OFF by default (spec `coordination.enabled`) and a supervisor with no token or $ cap is
    refused. Each child is reserved a slice of what is left (its `budget_share`, else an equal
    share of `max_subagents`) and ends, capped, when it has spent it.
  * INTERRUPT PROPAGATION. A child that waits on a person opens its Decision in the PARENT run's
    name (the parent thread's inbox), and the parent suspends on that same interrupt id. The
    answer resumes the parent, whose held `task` call resumes the child — which checks the
    approval against its own held call's arguments (the hash binding never moves).
  * A CHILD SPAN. The child's run span is a child of the parent's `task` tool span (kernel/trace).

EFFORT-SCALING CAPS (Anthropic's "50 sub-agents for a simple query"): `max_subagents` per run,
`max_depth`, `max_parallel` per step (and live async children), `max_tokens` / `max_usd` across
all children. A spawn beyond a cap is REFUSED, never queued, with a `subagent.capped` event and the
refusal on the tool's span.

SINGLE WRITER PER BRANCH (D12, Cognition/Anthropic): a sub-agent with write tools on a branch the
parent writes, or any other live run writes, is refused at spawn (the `branches` port). Parallel
READERS are allowed and are what `task` parallelises: read-only children asked for in one model
turn run concurrently (at most `max_parallel`), writers never do.
"""
from __future__ import annotations

import contextlib
import contextvars
import copy
import dataclasses
import hashlib
import json
import logging
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Any, Callable, Iterator, Mapping, Optional, Protocol

from .middleware import Jump, Middleware
from .state import RunState
from .types import ToolCall, ToolResult, ToolSchema, Usage

log = logging.getLogger("boss.kernel.subagents")

TASK = "task"
START = "start_subagent"
CHECK = "check_subagent"
UPDATE = "update_subagent"
CANCEL = "cancel_subagent"
LIST = "list_subagents"
SYNC_TOOLS = (TASK,)
ASYNC_TOOLS = (START, CHECK, UPDATE, CANCEL, LIST)
TOOLS = SYNC_TOOLS + ASYNC_TOOLS

#: Every tool only a kernel run serves (never MCP, never the legacy chat loop): these, and
#: kernel/planning's write_todos (the run filesystem's are runfs.RUN_FS_TOOLS).
RUN_ONLY_TOOLS = TOOLS + ("write_todos",)

GENERAL_PURPOSE = "general-purpose"
MODES = ("isolated", "fork")
DEFAULT_REPORT_TOKENS = 1500
#: The catalogue permission a spawn needs inside a delegated run (delegable, identity catalogue).
PERMISSION = "agent:invoke"
#: Where the coordination keeps its state on the run (carried by every checkpoint).
_KEY = "subagents"

#: The caps a spawn is held to, in the order they are checked.
CAPS = ("disabled", "no_budget", "max_depth", "max_subagents", "max_parallel", "max_tokens",
        "max_usd")


# ── The spec, as the kernel reads it ───────────────────────────────────────────────────────────

@dataclass(frozen=True)
class SubagentDef:
    """One entry of a spec's `subagents` list (agents/spec.py validates the document)."""

    ref: str
    description: str = ""
    mode: str = "isolated"
    report_tokens: int = DEFAULT_REPORT_TOKENS
    forward_verbatim: bool = False
    writes: bool = False
    branch: Optional[str] = None
    budget_share: Optional[float] = None
    tools: tuple = ()
    asynchronous: bool = False

    @classmethod
    def from_spec(cls, entry: Mapping[str, Any]) -> "SubagentDef":
        mode = str(entry.get("mode") or "isolated")
        return cls(ref=str(entry["ref"]), description=str(entry.get("description") or ""),
                   mode="fork" if mode in ("fork", "shared") else "isolated",
                   report_tokens=int(entry.get("report_tokens") or DEFAULT_REPORT_TOKENS),
                   forward_verbatim=bool(entry.get("forward_verbatim")),
                   writes=bool(entry.get("writes")), branch=entry.get("branch") or None,
                   budget_share=float(entry["budget_share"])
                   if entry.get("budget_share") is not None else None,
                   tools=tuple(str(t) for t in entry.get("tools") or ()),
                   asynchronous=bool(entry.get("async")))


@dataclass(frozen=True)
class Coordination:
    """A spec's `coordination` block: OFF unless the spec opts in, and never without a budget."""

    enabled: bool = False
    max_subagents: int = 3
    max_depth: int = 1
    max_parallel: int = 3
    max_tokens: Optional[int] = None
    max_usd: Optional[float] = None
    general_purpose: bool = True
    async_tools: bool = False
    topology: Optional[str] = None
    rounds: Optional[int] = None

    @classmethod
    def from_spec(cls, block: Optional[Mapping[str, Any]]) -> "Coordination":
        block = dict(block or {})
        known = {f.name for f in dataclasses.fields(cls)}
        values = {k: v for k, v in block.items() if k in known and v is not None}
        if "async" in block:
            values["async_tools"] = bool(block["async"])
        return cls(**values)

    @property
    def budgeted(self) -> bool:
        return bool(self.max_tokens) or bool(self.max_usd)

    @property
    def usd_micros(self) -> Optional[int]:
        return int(float(self.max_usd) * 1_000_000) if self.max_usd else None


# ── What a target is asked and what it answers ─────────────────────────────────────────────────

@dataclass
class SubagentRequest:
    """One spawn, as a target sees it. `harden(spec)` applies the platform's guarantees to a
    local child's RunSpec (run id, slice, propagating interrupts, cancel, trace, depth); a remote
    target reads the same fields (brief, budget, traceparent) and forwards what its protocol
    carries."""

    target: str
    brief: str
    mode: str
    child_run_id: str
    parent_run_id: str
    project_id: str
    call_id: str
    depth: int
    report_tokens: int
    token_budget: Optional[int]
    usd_micros_budget: Optional[int]
    messages: list
    writes: bool = False
    branch: Optional[str] = None
    traceparent: Optional[str] = None
    thread_id: Optional[str] = None
    harden: Callable[[Any], Any] = field(default=lambda spec: spec, repr=False)

    def summary(self) -> dict:
        """What the parent's checkpoint keeps to rebuild this request on a resume."""
        return {"target": self.target, "brief": self.brief, "mode": self.mode,
                "child_run_id": self.child_run_id, "call_id": self.call_id,
                "depth": self.depth, "report_tokens": self.report_tokens,
                "token_budget": self.token_budget, "usd_micros_budget": self.usd_micros_budget,
                "writes": self.writes, "branch": self.branch}


@dataclass
class SubagentOutcome:
    status: str                     # completed | suspended | failed | cancelled | budget_wait
    child_run_id: str
    text: str = ""
    usage: Usage = field(default_factory=Usage)
    pending: Optional[dict] = None
    error: str = ""
    structured: Optional[dict] = None
    steps: int = 0
    stopped: str = ""

    @classmethod
    def from_result(cls, result: Any) -> "SubagentOutcome":
        return cls(status=str(result.status), child_run_id=str(result.run_id),
                   text=result.text or "", usage=result.usage, pending=result.pending,
                   error=result.error or "", structured=result.structured,
                   steps=int(result.steps or 0), stopped=str(result.stopped or ""))

    @property
    def tokens(self) -> int:
        u = self.usage
        return int(u.input_tokens + u.output_tokens + u.cache_read_tokens + u.cache_write_tokens)


class SubagentTarget(Protocol):
    """What runs a sub-agent: `LocalTarget` (a kernel run in this process) or a remote agent (P5's
    A2A client adapter implements the same methods over the wire). `parallel_safe`: may run
    beside its siblings on a thread. The async methods are optional (an AsyncPort serves them
    for local children)."""

    name: str
    description: str
    writes: bool
    parallel_safe: bool

    def run(self, request: SubagentRequest) -> SubagentOutcome: ...

    def resume(self, request: SubagentRequest, *, answers: dict) -> SubagentOutcome: ...


class AsyncPort(Protocol):
    """Async children as durable runs on workers (services/subagent_runs.DbAsyncSubagents) or an
    A2A peer. Every method returns an A2A Task as a dict (`Task.to_dict()`)."""

    def start(self, request: SubagentRequest) -> dict: ...

    def check(self, task_id: str) -> dict: ...

    def update(self, task_id: str, message: str, *, strategy: str = "enqueue") -> dict: ...

    def cancel(self, task_id: str) -> dict: ...

    def list(self, parent_run_id: str) -> list: ...

    def notifications(self, parent_run_id: str) -> list: ...


class BranchPolicy(Protocol):
    """Who writes which branch (D12). `claim` returns "" or the refusal."""

    def holder(self, branch: str) -> Optional[str]: ...

    def claim(self, branch: str, run_id: str) -> str: ...

    def release(self, run_id: str) -> None: ...


class IdentityPort(Protocol):
    """The token exchange for a child (services/subagent_runs.OboExchange): a context manager the
    child runs inside, under its own narrower run token. Raises PermissionError when the exchange
    is refused (the spawn is refused with the reason)."""

    def child(self, request: SubagentRequest) -> Any: ...


class MemoryBranches:
    """The single-writer registry in memory (tests, a process with no database)."""

    def __init__(self, live: Optional[Callable[[str], bool]] = None):
        self.held: dict = {}
        self.live = live or (lambda run_id: True)
        self._lock = threading.Lock()

    def holder(self, branch: str) -> Optional[str]:
        with self._lock:
            run_id = self.held.get(branch)
        return run_id if run_id and self.live(run_id) else None

    def claim(self, branch: str, run_id: str) -> str:
        with self._lock:
            current = self.held.get(branch)
            if current and current != run_id and self.live(current):
                return f"branch {branch!r} is being written by run {current}"
            self.held[branch] = run_id
        return ""

    def release(self, run_id: str) -> None:
        with self._lock:
            for branch in [b for b, r in self.held.items() if r == run_id]:
                self.held.pop(branch, None)


# ── A local child: a kernel run in this process ────────────────────────────────────────────────

class LocalTarget:
    """A sub-agent run by the kernel here. `build(request, parent_run) -> RunSpec` makes the
    child's spec (its engine, tools, policies, system prompt); the middleware's `harden` then
    applies what no builder may skip."""

    def __init__(self, name: str, build: Callable[[SubagentRequest, Any], Any], *,
                 description: str = "", writes: bool = False, parallel_safe: bool = True):
        self.name, self.build = name, build
        self.description, self.writes, self.parallel_safe = description, writes, parallel_safe
        self.parent_run: Any = None

    def spec(self, request: SubagentRequest) -> Any:
        return request.harden(self.build(request, self.parent_run))

    def run(self, request: SubagentRequest) -> SubagentOutcome:
        from .loop import run as kernel_run

        return SubagentOutcome.from_result(kernel_run(self.spec(request)))

    def resume(self, request: SubagentRequest, *, answers: dict) -> SubagentOutcome:
        from .loop import resume as kernel_resume

        return SubagentOutcome.from_result(kernel_resume(self.spec(request), answers=answers))


def inherit(*, system: Optional[str] = None, role_prompt: str = "",
            max_steps: Optional[int] = None) -> Callable:
    """The default builder: the parent's engine and policies (verification left to the parent),
    the parent's own tools seen through the child's grant — read-only unless the sub-agent is a
    writer, and only the names its spec entry lists when it lists any."""
    from .policies import AcceptAny

    def build(request: SubagentRequest, parent_run: Any) -> Any:
        from .spec import RunSpec

        pspec = parent_run.spec
        policies = dataclasses.replace(pspec.policies, verify=AcceptAny())
        stop = policies.stop
        if max_steps and dataclasses.is_dataclass(stop) and hasattr(stop, "max_steps"):
            policies = dataclasses.replace(policies, stop=dataclasses.replace(
                stop, max_steps=min(int(stop.max_steps), int(max_steps))))
        allow = tuple(getattr(parent_run, "_subagent_allow", {}).get(request.target, ()))
        return RunSpec(engine=pspec.engine, tools=ChildTools(pspec.tools, writes=request.writes,
                                                             allow=allow),
                       policies=policies, project_id=request.project_id,
                       system=system or child_system(request, role_prompt=role_prompt),
                       sink=pspec.sink, gate=pspec.gate,
                       screen=pspec.screen, db=pspec.db, ledger_reader=None)

    return build


def child_system(request: SubagentRequest, *, role_prompt: str = "") -> str:
    head = (role_prompt.strip() + "\n\n") if role_prompt.strip() else ""
    return (f"{head}You are the `{request.target}` sub-agent working for a supervisor agent. Do "
            "only what the brief asks, using your tools. When you are done, reply with ONE report "
            f"of at most {request.report_tokens} tokens: what you found, the evidence (the tool "
            "results it rests on), and what you could not establish. Do not address the end "
            "user; the supervisor reads your report.")


class ChildTools:
    """The parent's tool set seen through a child's grant: only reads for a read-only child
    (D12: parallel readers, one writer), only the listed names when the spec lists any."""

    def __init__(self, inner: Any, *, writes: bool = False, allow: tuple = ()):
        self.inner, self.writes, self.allow = inner, writes, frozenset(allow)

    def _ok(self, schema: ToolSchema) -> bool:
        if schema.name in TOOLS:
            return False
        if self.allow and schema.name not in self.allow:
            return False
        return self.writes or schema.kind != "write"

    def schemas(self, *, step: int, state: RunState) -> list:
        return [s for s in self.inner.schemas(step=step, state=state) if self._ok(s)]

    def invoke(self, call: ToolCall, *, state: RunState,
               approved: Optional[dict] = None) -> ToolResult:
        from .toolset import unknown

        if not any(s.name == call.name for s in self.schemas(step=state.step, state=state)):
            return unknown(call)
        return self.inner.invoke(call, state=state, approved=approved)


# ── The child's guarantees ─────────────────────────────────────────────────────────────────────

class SliceGuard(Middleware):
    """A child's budget slice: before each model call, a child that has spent its tokens (or its
    dollars) ends — capped, with what it has — instead of spending its siblings' share."""

    name = "subagent_slice"
    version = "1"
    can_jump_to = ("end",)

    def __init__(self, tokens: Optional[int], usd_micros: Optional[int]):
        self.tokens, self.usd_micros = tokens, usd_micros

    def before_model(self, run: Any, call: Any) -> Optional[Jump]:
        usage = run.state.usage
        spent = usage.input_tokens + usage.output_tokens + usage.cache_read_tokens \
            + usage.cache_write_tokens
        over = (self.tokens is not None and spent >= self.tokens) or (
            self.usd_micros is not None and usage.cost_usd_micros >= self.usd_micros)
        if not over:
            return None
        run.rec.emit("budget.stop", {"why": "subagent_slice", "tokens": spent,
                                     "slice_tokens": self.tokens,
                                     "slice_usd_micros": self.usd_micros})
        text = run.state.final_text or _last_text(run.state.messages) or (
            "(stopped at the budget slice the supervisor gave this sub-agent)")
        return Jump("end", status="completed", stopped="budget_slice", text=text, capped=True)


def _last_text(messages: list) -> str:
    for message in reversed(messages):
        if isinstance(message, dict) and message.get("role") == "assistant" \
                and str(message.get("content") or "").strip():
            return str(message["content"])
    return ""


class ChildInterrupts:
    """A child's interrupt port: its Decision is opened in the PARENT run's name, so it lands in
    the parent thread's inbox and its answer resumes the parent (which resumes the child)."""

    def __init__(self, parent_port: Any, parent_state: RunState, *, target: str,
                 child_run_id: str, call_id: str):
        self.parent_port, self.parent_state = parent_port, parent_state
        self.target, self.child_run_id, self.call_id = target, child_run_id, call_id

    def open(self, state: Any, item: Any) -> str:
        item.payload = {**dict(item.payload or {}),
                        "subagent": {"target": self.target, "child_run_id": self.child_run_id,
                                     "parent_call_id": self.call_id}}
        summary = str(item.payload.get("question") or item.payload.get("summary") or "")
        if summary and not summary.startswith("[sub-agent"):
            key = "question" if item.payload.get("question") else "summary"
            item.payload[key] = f"[sub-agent {self.target}] {summary}"
        view = _ParentView(run_id=self.parent_state.run_id,
                           project_id=self.parent_state.project_id,
                           thread_id=self.parent_state.thread_id)
        return self.parent_port.open(view, item)


@dataclass
class _ParentView:
    run_id: str
    project_id: str
    thread_id: Optional[str] = None


def child_store(store: Any) -> Any:
    """Where a child checkpoints: the parent's Postgres checkpointer without its agent_runs
    mirror (a child is not the parent's row), the parent's own store otherwise."""
    from .checkpoint import DEFAULT_STORE

    if store is None:
        return DEFAULT_STORE
    if getattr(store, "durable", False) and hasattr(store, "bind"):
        cls = type(store)
        return cls(store.bind, thread_id=None, ns="subagent", durability=store.durability,
                   commit=store.commit, fence=store.fence)
    if hasattr(store, "save") and hasattr(store, "load") and not hasattr(store, "finalize_ctx") \
            and not hasattr(store, "transcript"):
        return store
    return DEFAULT_STORE


def _thread_safe(store: Any) -> bool:
    """A store a child may write from another thread: not one bound to a single connection."""
    bind = getattr(store, "bind", None)
    if bind is not None:
        return hasattr(bind, "connect")  # an Engine, not a Connection
    return not hasattr(store, "connection")


def _bind_thread_safe(db: Any) -> bool:
    """The run's decision database: None or an Engine may be used from a child's thread; a
    Connection may not."""
    return db is None or hasattr(db, "connect")


def cap_report(text: str, tokens: int) -> tuple:
    """The report as the parent reads it: at most `tokens` (chars/4), marked when cut."""
    limit = max(1, int(tokens)) * 4
    if len(text) <= limit:
        return text, False
    return (text[:limit].rstrip() + f"\n[report clipped at {tokens} tokens; ask the sub-agent "
            "a narrower question for the rest]"), True


def _schema(name: str, description: str, properties: dict, required: tuple) -> ToolSchema:
    return ToolSchema(name=name, description=description,
                      parameters={"type": "object", "properties": properties,
                                  "required": list(required), "additionalProperties": False},
                      kind="read", permission=PERMISSION)


def tool_specs(names: list, *, describe: str = "", modes: tuple = MODES) -> list:
    """The coordination tools as offered (and as catalogued in the registry)."""
    target = {"type": "string", "description": "which sub-agent: " + (describe or "by name")}
    if names:
        target["enum"] = list(names)
    brief = {"type": "string", "description": "everything the sub-agent needs: the question, "
             "the scope, what to return. It sees nothing else of this conversation in "
             "`isolated` mode."}
    task_id = {"type": "string", "description": "the Task id start_subagent returned"}
    return [
        _schema(TASK, "Delegate a self-contained piece of work to a sub-agent and wait for its "
                "report. Several `task` calls in one message run in parallel (read-only "
                "sub-agents). Use it for research, review and investigation; never split one "
                "code change across writers.",
                {"subagent": target, "brief": brief,
                 "mode": {"type": "string", "enum": list(modes),
                          "description": "isolated (default): a fresh context; fork: the "
                                         "sub-agent starts from this conversation"}},
                ("subagent", "brief")),
        _schema(START, "Start a sub-agent on long work in the background; returns its Task "
                "(id, state). You are told when it finishes; check_subagent reads it.",
                {"subagent": target, "brief": brief}, ("subagent", "brief")),
        _schema(CHECK, "The current Task of a background sub-agent: its state, and its report "
                "once completed.", {"task_id": task_id}, ("task_id",)),
        _schema(UPDATE, "Send a background sub-agent more instructions (queued after its "
                "current run, or interrupting it).",
                {"task_id": task_id, "message": {"type": "string"},
                 "strategy": {"type": "string", "enum": ["enqueue", "interrupt"]}},
                ("task_id", "message")),
        _schema(CANCEL, "Stop a background sub-agent.", {"task_id": task_id}, ("task_id",)),
        _schema(LIST, "Every background sub-agent this run started, with its state.", {}, ()),
    ]


# ── The middleware ─────────────────────────────────────────────────────────────────────────────

class Subagents(Middleware):
    """See the module docstring. Persistent state lives in `state.extra["subagents"]`; the
    futures of children running beside each other are transient (one step)."""

    name = "subagents"
    version = "subagents@1"
    can_jump_to = ("end",)

    def __init__(self, coordination: Coordination, *, targets: Optional[dict] = None,
                 defs: Optional[dict] = None, depth: int = 0, branch: Optional[str] = None,
                 parent_writes: bool = False, branches: Any = None, identity: Any = None,
                 async_port: Any = None, general_purpose: Any = None,
                 child_middleware: Callable[[SubagentRequest], tuple] = lambda r: ()):
        self.coordination = coordination
        self.defs: dict = dict(defs or {})
        self.targets: dict = dict(targets or {})
        if coordination.general_purpose and GENERAL_PURPOSE not in self.targets:
            self.targets[GENERAL_PURPOSE] = general_purpose or LocalTarget(
                GENERAL_PURPOSE, inherit(), description="a general-purpose read-only "
                "researcher with your own read tools")
        for name, definition in self.defs.items():
            # A spec entry with no target of its own: a local child on the parent's engine and
            # tools, through its grant (read-only unless the entry says it writes).
            self.targets.setdefault(name, LocalTarget(
                name, inherit(), description=definition.description, writes=definition.writes,
                parallel_safe=not definition.writes))
        for name in self.targets:
            self.defs.setdefault(name, SubagentDef(ref=name, writes=bool(
                getattr(self.targets[name], "writes", False))))
        self.depth = int(depth)
        self.branch = branch
        self.parent_writes = bool(parent_writes)
        self.branches = branches
        self.identity = identity
        self.async_port = async_port
        self.child_middleware = child_middleware
        self._futures: dict = {}
        self._lock = threading.Lock()

    # ── plumbing ──────────────────────────────────────────────────────────────────────────
    @staticmethod
    def ex(run: Any) -> dict:
        extra = run.state.extra
        if not isinstance(extra.get(_KEY), dict):
            extra[_KEY] = {"spawned": 0, "tokens": 0, "usd_micros": 0, "reserved_tokens": 0,
                           "reserved_usd": 0, "children": {}, "async": {}, "caps": [],
                           "steps": {}, "done": {}}
        return extra[_KEY]

    def run_tools(self, run: Any) -> "SubagentToolSet":
        for target in self.targets.values():
            if isinstance(target, LocalTarget):
                target.parent_run = run
        run._subagent_allow = {name: d.tools for name, d in self.defs.items()}
        return SubagentToolSet(self, run)

    def offered(self) -> list:
        names = sorted(self.targets)
        describe = "; ".join(f"{n}: {(self.defs[n].description or getattr(self.targets[n], 'description', '') or '').strip()[:160]}"
                             for n in names)
        specs = tool_specs(names, describe=describe)
        keep = set(SYNC_TOOLS) | (set(ASYNC_TOOLS) if self.coordination.async_tools
                                  and self.async_port is not None else set())
        return [s for s in specs if s.name in keep]

    # ── hooks ─────────────────────────────────────────────────────────────────────────────
    def before_agent(self, run: Any) -> None:
        self.ex(run)
        if self.parent_writes and self.branch and self.branches is not None:
            refusal = self.branches.claim(self.branch, run.state.run_id)
            if refusal:
                log.warning("subagents: %s", refusal)
        return None

    def before_model(self, run: Any, call: Any) -> Optional[Jump]:
        ex = self.ex(run)
        self._collect_orphans(run)
        forward = ex.pop("forward", None)
        if isinstance(forward, dict):
            # FORWARD-VERBATIM: the child's report IS the answer — no supervisor "translation".
            run.rec.emit("subagent.finished", {"forwarded": True,
                                               "child_run_id": forward.get("child_run_id")})
            return Jump("end", status="completed", text=str(forward.get("text") or ""))
        if self.async_port is not None:
            try:
                notes = self.async_port.notifications(run.state.run_id) or []
            except Exception as exc:  # noqa: BLE001 — a missed note is read on check
                log.warning("subagents: notifications not read (non-fatal): %s", exc)
                notes = []
            for note in notes:
                run.state.messages.append({"role": "user", "content": note_text(note)})
        return None

    def after_model(self, run: Any, turn: Any) -> None:
        """Read-only `task` calls asked for in ONE turn start together (at most max_parallel),
        each admitted here exactly as it would be on its own; the tool calls then collect."""
        calls = [c for c in (turn.tool_calls or []) if c.name == TASK
                 and isinstance(c.arguments, dict)]
        if len(calls) < 2 or self.coordination.max_parallel < 2:
            return None
        if not _thread_safe(getattr(run, "store", None)) or not _bind_thread_safe(run.spec.db):
            return None  # one connection is never shared by threads: the children run in turn
        tracer = run.chain.get("trace")
        for call in calls:
            target = self.targets.get(str(call.arguments.get("subagent") or ""))
            if target is None or not getattr(target, "parallel_safe", False) \
                    or self.defs[target.name].writes:
                continue
            admitted = self._admit(run, call, step=run.state.step)
            if isinstance(admitted, ToolResult):
                with self._lock:
                    self._futures[call.id] = admitted
                continue
            if tracer is not None and callable(getattr(tracer, "reserve", None)):
                admitted.traceparent = tracer.reserve(run, call.id)
            ctx = contextvars.copy_context()
            future = self._pool(run).submit(ctx.run, self._execute, run, target, admitted)
            with self._lock:
                self._futures[call.id] = (admitted, future)
        return None

    def _collect_orphans(self, run: Any) -> None:
        """Children started beside a call the step then skipped (a sibling waited on a person):
        their spend is settled and their report kept, so asking the same brief again reads it
        instead of running it twice."""
        with self._lock:
            left = list(self._futures.items())
            self._futures.clear()
        for call_id, item in left:
            if not isinstance(item, tuple):
                continue
            request, future = item
            try:
                outcome = future.result(timeout=900)
            except Exception as exc:  # noqa: BLE001
                outcome = SubagentOutcome(status="failed", child_run_id=request.child_run_id,
                                          error=str(exc)[:300])
            self._settle(run, request, outcome)
            if outcome.status == "completed":
                report, clipped = cap_report(outcome.text or "", request.report_tokens)
                self.ex(run)["done"][_brief_key(request)] = {
                    "subagent": request.target, "child_run_id": request.child_run_id,
                    "status": "ok", "report": report, "tokens": outcome.tokens,
                    **({"clipped": True} if clipped else {})}

    def after_agent(self, run: Any, result: Any) -> None:
        self._collect_orphans(run)
        pool = getattr(self, "_executor", None)
        if pool is not None:
            pool.shutdown(wait=False)
            self._executor = None
        if str(getattr(result, "status", "")) in ("completed", "failed", "cancelled") \
                and self.branches is not None:
            self.branches.release(run.state.run_id)
        return None

    def _pool(self, run: Any) -> ThreadPoolExecutor:
        if getattr(self, "_executor", None) is None:
            self._executor = ThreadPoolExecutor(
                max_workers=max(1, int(self.coordination.max_parallel)),
                thread_name_prefix="subagent")
        return self._executor

    # ── admission: every cap, the single writer, the slice ────────────────────────────────
    def _cap(self, run: Any, call: ToolCall, cap: str, limit: Any, message: str) -> ToolResult:
        ex = self.ex(run)
        event = {"cap": cap, "limit": limit, "tool": call.name, "call_id": call.id,
                 "target": (call.arguments or {}).get("subagent"), "step": run.state.step}
        ex["caps"].append(event)
        run.rec.emit("subagent.capped", event)
        return ToolResult(call.id, call.name, "refused", {
            "ok": False, "status": "refused", "error": "subagent_cap", "cap": cap,
            "limit": limit, "message": message})

    def _admit(self, run: Any, call: ToolCall, *, step: int,
               asynchronous: bool = False) -> Any:
        """A SubagentRequest for this spawn, or the refusal (a ToolResult) that names the cap."""
        co, ex, args = self.coordination, self.ex(run), dict(call.arguments or {})
        name = str(args.get("subagent") or "")
        target = self.targets.get(name)
        if target is None:
            return ToolResult(call.id, call.name, "refused", {
                "ok": False, "status": "refused", "error": "unknown_subagent",
                "message": f"there is no sub-agent {name!r}; use one of {sorted(self.targets)}"})
        if not co.enabled:
            return self._cap(run, call, "disabled", False, "multi-agent coordination is off "
                             "for this agent (its spec's coordination.enabled is false).")
        if not co.budgeted:
            return self._cap(run, call, "no_budget", None, "this agent's spec gives its "
                             "sub-agents no budget (coordination.max_tokens or max_usd); a "
                             "sub-agent never runs without a slice.")
        if self.depth >= int(co.max_depth):
            return self._cap(run, call, "max_depth", co.max_depth, f"sub-agents may nest "
                             f"{co.max_depth} level(s) deep; this run is at depth {self.depth}.")
        if int(ex["spawned"]) >= int(co.max_subagents):
            return self._cap(run, call, "max_subagents", co.max_subagents,
                             f"this run has started its {co.max_subagents} sub-agents; answer "
                             "with what they reported.")
        in_step = int(ex["steps"].get(str(step), 0))
        live_async = sum(1 for t in ex["async"].values() if not t.get("ended"))
        if in_step >= int(co.max_parallel) or (asynchronous and live_async >= co.max_parallel):
            return self._cap(run, call, "max_parallel", co.max_parallel,
                             f"at most {co.max_parallel} sub-agents run at once.")
        tokens = usd = None
        if co.max_tokens:
            left = int(co.max_tokens) - int(ex["tokens"]) - int(ex["reserved_tokens"])
            if left <= 0:
                return self._cap(run, call, "max_tokens", co.max_tokens,
                                 "the sub-agents' token budget is spent.")
            share = self.defs[name].budget_share or 1.0 / max(1, int(co.max_subagents))
            tokens = max(1, min(left, int(int(co.max_tokens) * share)))
        if co.usd_micros:
            left_usd = int(co.usd_micros) - int(ex["usd_micros"]) - int(ex["reserved_usd"])
            if left_usd <= 0:
                return self._cap(run, call, "max_usd", co.max_usd,
                                 "the sub-agents' dollar budget is spent.")
            share = self.defs[name].budget_share or 1.0 / max(1, int(co.max_subagents))
            usd = max(1, min(left_usd, int(int(co.usd_micros) * share)))
        definition = self.defs[name]
        writes = bool(definition.writes)
        branch = definition.branch or (self.branch if writes else None)
        if writes and branch and self.branches is not None:
            # SINGLE WRITER PER BRANCH (D12): the parent's own branch, or any live run's.
            holder = self.branches.holder(branch)
            if holder is not None or (self.parent_writes and branch == self.branch):
                who = "this run (the parent)" if holder in (None, run.state.run_id) else \
                    f"run {holder}"
                return self._cap(run, call, "single_writer", branch,
                                 f"a writer sub-agent on {branch!r} is refused: {who} is "
                                 "writing it. One writer per branch; give the work to the "
                                 "writer, or use a read-only sub-agent.")
        mode = str(args.get("mode") or definition.mode or "isolated")
        mode = mode if mode in MODES else "isolated"
        child_run_id = f"{run.state.run_id}.sub{int(ex['spawned']) + 1}_{uuid.uuid4().hex[:6]}"
        request = SubagentRequest(
            target=name, brief=str(args.get("brief") or ""), mode=mode,
            child_run_id=child_run_id, parent_run_id=run.state.run_id,
            project_id=run.state.project_id, call_id=call.id, depth=self.depth + 1,
            report_tokens=int(definition.report_tokens or DEFAULT_REPORT_TOKENS),
            token_budget=tokens, usd_micros_budget=usd,
            messages=self._starting_messages(run, args, mode), writes=writes, branch=branch,
            thread_id=run.state.thread_id)
        request.harden = lambda spec, _r=request: self._harden(run, _r, spec)
        ex["spawned"] = int(ex["spawned"]) + 1
        ex["steps"][str(step)] = in_step + 1
        ex["reserved_tokens"] = int(ex["reserved_tokens"]) + int(tokens or 0)
        ex["reserved_usd"] = int(ex["reserved_usd"]) + int(usd or 0)
        run.rec.emit("subagent.started", {"target": name, "child_run_id": child_run_id,
                                          "call_id": call.id, "mode": mode, "depth": self.depth + 1,
                                          "slice_tokens": tokens, "slice_usd_micros": usd,
                                          "writes": writes, "branch": branch,
                                          "async": asynchronous})
        if writes and branch and self.branches is not None:
            self.branches.claim(branch, child_run_id)
        return request

    def _starting_messages(self, run: Any, args: dict, mode: str) -> list:
        brief = {"role": "user", "content": str(args.get("brief") or "")}
        if mode != "fork":
            return [brief]
        # FORK: the conversation so far, up to (not including) the turn that asked for this —
        # its tool calls are unanswered there — then the brief.
        messages = copy.deepcopy(list(run.state.messages))
        while messages and not (messages[-1].get("role") == "assistant"
                                and messages[-1].get("tool_calls")):
            messages.pop()
        if messages:
            messages.pop()
        return messages + [brief]

    def _settle(self, run: Any, request: SubagentRequest, outcome: SubagentOutcome) -> None:
        ex = self.ex(run)
        ex["reserved_tokens"] = max(0, int(ex["reserved_tokens"]) - int(request.token_budget or 0))
        ex["reserved_usd"] = max(0, int(ex["reserved_usd"]) - int(request.usd_micros_budget or 0))
        ex["tokens"] = int(ex["tokens"]) + outcome.tokens
        ex["usd_micros"] = int(ex["usd_micros"]) + int(outcome.usage.cost_usd_micros or 0)
        if outcome.status != "suspended" and request.writes and self.branches is not None:
            self.branches.release(request.child_run_id)

    # ── the child's spec, hardened ────────────────────────────────────────────────────────
    def _harden(self, run: Any, request: SubagentRequest, spec: Any) -> Any:
        from .engine import CancelToken

        pspec, parent_token = run.spec, run.token
        spec.run_id = request.child_run_id
        spec.lineage_id = request.child_run_id
        spec.project_id = request.project_id
        if not spec.messages:
            spec.messages = list(request.messages)
        spec.actor = f"agent:{request.target}"
        spec.extra = {**dict(spec.extra or {}),
                      "subagent": {"parent_run_id": request.parent_run_id,
                                   "target": request.target, "depth": request.depth,
                                   "mode": request.mode, "call_id": request.call_id},
                      **({"traceparent": request.traceparent} if request.traceparent else {})}
        if spec.extra.get("agent_spec") is None:
            spec.extra["agent_spec"] = {"key": request.target}
        spec.store = child_store(pspec.store)
        spec.persist = bool(pspec.persist)
        if spec.sink is None:
            spec.sink = pspec.sink
        if spec.db is None:
            spec.db = pspec.db
        if pspec.interrupts is not None:
            spec.interrupts = ChildInterrupts(pspec.interrupts, run.state, target=request.target,
                                              child_run_id=request.child_run_id,
                                              call_id=request.call_id)
        spec.drain = pspec.drain
        own = spec.cancel
        spec.cancel = CancelToken(should_stop=lambda: parent_token.is_set() or (
            own is not None and own.is_set()), clock=spec.clock)
        if request.usd_micros_budget is not None:
            stop = spec.policies.stop
            if dataclasses.is_dataclass(stop) and hasattr(stop, "max_usd_micros"):
                cap = stop.max_usd_micros
                tighter = request.usd_micros_budget if cap is None else min(
                    int(cap), int(request.usd_micros_budget))
                spec.policies = dataclasses.replace(spec.policies, stop=dataclasses.replace(
                    stop, max_usd_micros=tighter))
        extra_mw = tuple(self.child_middleware(request) or ())
        nested: tuple = ()
        if request.depth < int(self.coordination.max_depth):
            # A child may coordinate in turn (depth permitting), inside its own slice.
            nested = (Subagents(dataclasses.replace(
                self.coordination, max_tokens=request.token_budget,
                max_usd=(request.usd_micros_budget / 1_000_000)
                if request.usd_micros_budget else None),
                targets={k: v for k, v in self.targets.items() if k != GENERAL_PURPOSE},
                defs=self.defs, depth=request.depth, branch=request.branch,
                parent_writes=request.writes, branches=self.branches, identity=self.identity),)
        spec.middleware = tuple(spec.middleware or ()) + extra_mw + nested + (
            SliceGuard(request.token_budget, request.usd_micros_budget),)
        return spec

    # ── running a child ───────────────────────────────────────────────────────────────────
    def _execute(self, run: Any, target: Any, request: SubagentRequest,
                 answers: Optional[dict] = None) -> SubagentOutcome:
        """The child, run (or resumed) inside its exchanged authority. An exception is the
        child's failure, never the parent's."""
        try:
            with self._authority(request):
                if answers is not None:
                    outcome = target.resume(request, answers=answers)
                else:
                    outcome = target.run(request)
            if outcome.status != "suspended" and self.identity is not None \
                    and callable(getattr(self.identity, "release", None)):
                # The child's credentials die with it (a suspended child keeps them: it is
                # still a live child of this run, and the parent's revocation reaches it).
                self.identity.release(request)
            return outcome
        except PermissionError as exc:
            return SubagentOutcome(status="refused", child_run_id=request.child_run_id,
                                   error=f"the token exchange refused this sub-agent: {exc}")
        except Exception as exc:  # noqa: BLE001
            log.warning("subagent %s failed: %s", request.child_run_id, exc)
            return SubagentOutcome(status="failed", child_run_id=request.child_run_id,
                                   error=f"{type(exc).__name__}: {exc}"[:600])

    @contextlib.contextmanager
    def _authority(self, request: SubagentRequest) -> Iterator[Any]:
        if self.identity is None:
            yield None
            return
        with self.identity.child(request) as ctx:
            yield ctx

    def task(self, run: Any, call: ToolCall, approved: Optional[dict]) -> ToolResult:
        ex = self.ex(run)
        if approved is not None:
            return self._resume_child(run, call, approved)
        with self._lock:
            prefetched = self._futures.pop(call.id, None)
        if isinstance(prefetched, ToolResult):
            return prefetched
        if prefetched is not None:
            request, future = prefetched
            outcome = future.result()
            target = self.targets[request.target]
        else:
            args = dict(call.arguments or {})
            name = str(args.get("subagent") or "")
            mode = str(args.get("mode") or self.defs.get(name, SubagentDef(ref=name)).mode)
            cached = ex["done"].get(_key(name, mode, str(args.get("brief") or "")))
            if cached is not None:
                return ToolResult(call.id, call.name, "ok", {**cached, "reused": True})
            admitted = self._admit(run, call, step=run.state.step)
            if isinstance(admitted, ToolResult):
                return admitted
            request, target = admitted, self.targets[admitted.target]
            outcome = self._execute(run, target, request)
        return self._result(run, call, request, outcome)

    def _resume_child(self, run: Any, call: ToolCall, answer: dict) -> ToolResult:
        ex = self.ex(run)
        saved = ex["children"].get(call.id)
        if not isinstance(saved, dict) or saved.get("status") != "suspended":
            return ToolResult(call.id, call.name, "error", {
                "ok": False, "error": "no_suspended_subagent",
                "message": "this call has no sub-agent waiting on an answer"})
        request = SubagentRequest(
            target=saved["target"], brief=saved["brief"], mode=saved["mode"],
            child_run_id=saved["child_run_id"], parent_run_id=run.state.run_id,
            project_id=run.state.project_id, call_id=call.id, depth=int(saved["depth"]),
            report_tokens=int(saved["report_tokens"]), token_budget=saved.get("token_budget"),
            usd_micros_budget=saved.get("usd_micros_budget"), messages=[],
            writes=bool(saved.get("writes")), branch=saved.get("branch"),
            thread_id=run.state.thread_id)
        request.harden = lambda spec, _r=request: self._harden(run, _r, spec)
        interrupt_id = str(saved.get("interrupt_id") or answer.get("interrupt_id") or "")
        clean = dict(answer)
        if not (clean.get("rejected") or clean.get("answered") or "happened" in clean):
            clean["approved"] = True
        target = self.targets[request.target]
        run.rec.emit("subagent.started", {"target": request.target, "resumed": True,
                                          "child_run_id": request.child_run_id,
                                          "call_id": call.id, "interrupt_id": interrupt_id})
        # The child's slice was released when it suspended; it is reserved again for the rest.
        ex["reserved_tokens"] = int(ex["reserved_tokens"]) + int(request.token_budget or 0)
        ex["reserved_usd"] = int(ex["reserved_usd"]) + int(request.usd_micros_budget or 0)
        outcome = self._execute(run, target, request, answers={interrupt_id: clean})
        return self._result(run, call, request, outcome, resumed=True)

    def _result(self, run: Any, call: ToolCall, request: SubagentRequest,
                outcome: SubagentOutcome, *, resumed: bool = False) -> ToolResult:
        ex = self.ex(run)
        self._settle(run, request, outcome)
        definition = self.defs.get(request.target) or SubagentDef(ref=request.target)
        base = {"subagent": request.target, "child_run_id": request.child_run_id,
                "tokens": outcome.tokens, "steps": outcome.steps}
        if outcome.status == "suspended":
            items = list((outcome.pending or {}).get("interrupts") or [])
            first = dict(items[0]) if items else {}
            ex["children"][call.id] = {**request.summary(), "status": "suspended",
                                       "interrupt_id": first.get("id"),
                                       "decision_id": first.get("decision_id")}
            run.rec.emit("subagent.finished", {**base, "status": "suspended",
                                               "interrupt_id": first.get("id"),
                                               "decision_id": first.get("decision_id")})
            declared = {k: first.get(k) for k in ("id", "kind", "schema", "payload", "binding")
                        if first.get(k) is not None}
            return ToolResult(call.id, call.name, "deferred", {
                **base, "status": "deferred_for_approval", "decision_id": first.get(
                    "decision_id"), "interrupt_id": first.get("id"),
                "note": f"the {request.target} sub-agent is waiting on a person: "
                        f"{(first.get('payload') or {}).get('summary') or first.get('tool')}"},
                decision_id=first.get("decision_id"),
                pending={"interrupt": declared,
                         **({"question": True} if first.get("kind") == "question" else {}),
                         "subagent": {"target": request.target,
                                      "child_run_id": request.child_run_id,
                                      "child_tool": first.get("tool")}})
        ex["children"][call.id] = {**request.summary(), "status": outcome.status}
        report, clipped = cap_report(outcome.text or "", request.report_tokens)
        event = {**base, "status": outcome.status, "clipped": clipped, "resumed": resumed,
                 "stopped": outcome.stopped}
        run.rec.emit("subagent.finished", event)
        if outcome.status in ("completed",):
            content = {**base, "status": "ok", "report": report,
                       **({"stopped": outcome.stopped} if outcome.stopped else {}),
                       **({"clipped": True} if clipped else {})}
            if definition.forward_verbatim:
                content["forward_verbatim"] = True
                ex["forward"] = {"text": outcome.text or "", "child_run_id": request.child_run_id}
            ex["done"][_brief_key(request)] = content
            return ToolResult(call.id, call.name, "ok", content)
        if outcome.status == "refused":
            return ToolResult(call.id, call.name, "refused", {
                **base, "ok": False, "status": "refused", "error": "exchange_refused",
                "message": outcome.error})
        return ToolResult(call.id, call.name, "error", {
            **base, "ok": False, "status": outcome.status,
            "error": outcome.error or f"the sub-agent ended {outcome.status}",
            "report": report})

    # ── async ─────────────────────────────────────────────────────────────────────────────
    def async_call(self, run: Any, call: ToolCall) -> ToolResult:
        ex, args, port = self.ex(run), dict(call.arguments or {}), self.async_port
        if port is None:
            from .toolset import unknown

            return unknown(call)
        try:
            if call.name == START:
                admitted = self._admit(run, call, step=run.state.step, asynchronous=True)
                if isinstance(admitted, ToolResult):
                    return admitted
                target = self.targets[admitted.target]
                starter = getattr(target, "start", None)
                with self._authority(admitted):
                    task = starter(admitted) if callable(starter) else port.start(admitted)
                ex["async"][str(task.get("id"))] = {"target": admitted.target,
                                                    "call_id": call.id,
                                                    "token_budget": admitted.token_budget}
                return ToolResult(call.id, call.name, "ok", {"task": task,
                                                            "child_run_id": task.get("id")})
            task_id = str(args.get("task_id") or "")
            if call.name == LIST:
                tasks = port.list(run.state.run_id)
                return ToolResult(call.id, call.name, "ok", {"tasks": tasks})
            if task_id not in ex["async"]:
                return ToolResult(call.id, call.name, "refused", {
                    "ok": False, "status": "refused", "error": "unknown_task",
                    "message": f"{task_id!r} is not a sub-agent this run started"})
            if call.name == CHECK:
                task = port.check(task_id)
            elif call.name == UPDATE:
                task = port.update(task_id, str(args.get("message") or ""),
                                   strategy=str(args.get("strategy") or "enqueue"))
                if task.get("id") and task.get("id") != task_id:
                    ex["async"][str(task["id"])] = {**ex["async"][task_id], "supersedes": task_id}
                    ex["async"][task_id]["ended"] = True
            else:
                task = port.cancel(task_id)
            state = str(((task.get("status") or {}).get("state")) or "")
            if state in ("TASK_STATE_COMPLETED", "TASK_STATE_FAILED", "TASK_STATE_CANCELED",
                         "TASK_STATE_REJECTED"):
                ex["async"][task_id]["ended"] = True
            return ToolResult(call.id, call.name, "ok", {"task": task})
        except PermissionError as exc:
            return ToolResult(call.id, call.name, "refused", {
                "ok": False, "status": "refused", "error": "exchange_refused",
                "message": str(exc)})


def _brief_key(request: SubagentRequest) -> str:
    return _key(request.target, request.mode, request.brief)


def _key(target: str, mode: str, brief: str) -> str:
    raw = json.dumps([target, mode, brief], ensure_ascii=False)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def note_text(note: Mapping[str, Any]) -> str:
    """How a finished background sub-agent is announced in the parent's conversation."""
    task = note.get("task") if isinstance(note.get("task"), Mapping) else note
    status = (task.get("status") or {}) if isinstance(task, Mapping) else {}
    state = str(status.get("state") or "").replace("TASK_STATE_", "").lower()
    report = str(note.get("report") or "")
    return (f"[sub-agent update] background sub-agent {note.get('target') or ''} task "
            f"{task.get('id')} is {state or 'updated'}." + (f" Report:\n{report}" if report
                                                           else ""))


class SubagentToolSet:
    """The coordination tools, served INNERMOST (after the mandatory governance has decided the
    call: authz, the screen, the audit), as every kernel built-in is."""

    def __init__(self, middleware: Subagents, run: Any):
        self.mw, self.run = middleware, run

    def schemas(self, *, step: int, state: RunState) -> list:
        return self.mw.offered()

    def invoke(self, call: ToolCall, *, state: RunState,
               approved: Optional[dict] = None) -> ToolResult:
        if call.name == TASK:
            return self.mw.task(self.run, call, approved)
        return self.mw.async_call(self.run, call)


def from_spec(spec_body: Mapping[str, Any], *, targets: Optional[dict] = None,
              **kwargs: Any) -> Optional[Subagents]:
    """The middleware a published spec asks for — None when coordination is off (the default):
    an agent that does not opt in is never offered `task`."""
    co = Coordination.from_spec(spec_body.get("coordination"))
    if not co.enabled:
        return None
    defs = {d.ref: d for d in (SubagentDef.from_spec(e) for e in spec_body.get("subagents") or [])}
    return Subagents(co, targets=targets or {}, defs=defs, **kwargs)


__all__ = ["ASYNC_TOOLS", "AsyncPort", "BranchPolicy", "CAPS", "ChildInterrupts", "ChildTools",
           "Coordination", "GENERAL_PURPOSE", "IdentityPort", "LocalTarget", "MODES",
           "MemoryBranches", "PERMISSION", "RUN_ONLY_TOOLS", "SliceGuard", "SubagentDef", "SubagentOutcome",
           "SubagentRequest", "SubagentTarget", "SubagentToolSet", "Subagents", "TASK", "TOOLS",
           "cap_report", "child_store", "child_system", "from_spec", "inherit", "note_text",
           "tool_specs"]
