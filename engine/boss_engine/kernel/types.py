"""The kernel's vocabulary: events, usage, tool calls, model turns, steps and results.

Every loop BOSS owns (chat, decisions, workflow agent steps, the pre-check, the scaffolded coder)
described the same things with its own dicts: a usage dict summed by hand in four places, a
"transcript" list in two shapes, tool results as whatever the tool returned. One set of frozen
types is what lets one loop, one ledger and one conformance suite exist
(docs/architecture/HARNESS_KERNEL_DESIGN.md §2.2).

Plain dataclasses on purpose: no services import, no I/O, JSON-able by `as_dict`, so a checkpoint
and a ledger row are the same data the loop held.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field, replace
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Literal, Mapping, Optional

EventKind = Literal[
    "run.started", "run.resumed", "run.suspended", "run.completed", "run.failed", "run.cancelled",
    "model.request", "model.response", "model.retry", "model.error",
    "tool.requested", "tool.result", "tool.refused", "tool.deferred",
    "schema.invalid", "schema.repaired",
    "context.compacted", "context.window",
    "verify.result", "finding",
    "decision", "escalation", "budget.wait", "budget.stop",
    "workspace.snapshot", "workspace.rewind",
    "delegate.turn",
    "outcome",
]

#: The closed set, for the ledger writer's check: a kind outside it is a bug, not a new kind.
EVENT_KINDS: frozenset = frozenset(EventKind.__args__)  # type: ignore[attr-defined]

Billing = Literal["metered", "subscription", "local"]


@dataclass(frozen=True)
class Usage:
    """What one model call (or a sum of them) spent. `input_tokens` is new, uncached input only,
    as llm_telemetry already records it; cached reads and writes are counted on their own."""

    model: str = ""
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    cost_usd_micros: int = 0
    billing: Billing = "metered"
    price_estimated: bool = False

    def __add__(self, other: "Usage") -> "Usage":
        return Usage(
            model=other.model or self.model,
            input_tokens=self.input_tokens + other.input_tokens,
            output_tokens=self.output_tokens + other.output_tokens,
            cache_read_tokens=self.cache_read_tokens + other.cache_read_tokens,
            cache_write_tokens=self.cache_write_tokens + other.cache_write_tokens,
            cost_usd_micros=self.cost_usd_micros + other.cost_usd_micros,
            billing=other.billing if other.model else self.billing,
            price_estimated=self.price_estimated or other.price_estimated,
        )

    def as_adapter_usage(self) -> dict:
        """The dict shape the adapters, budgets and llm_telemetry already read."""
        return {"model": self.model, "prompt_tokens": self.input_tokens,
                "completion_tokens": self.output_tokens,
                "total_tokens": self.input_tokens + self.output_tokens,
                "cache_read_tokens": self.cache_read_tokens,
                "cache_write_tokens": self.cache_write_tokens,
                "cost_usd_micros": self.cost_usd_micros, "billing": self.billing}

    @classmethod
    def from_adapter_usage(cls, usage: Any, *, cost_usd_micros: Optional[int] = None) -> "Usage":
        u = dict(usage) if isinstance(usage, Mapping) else {}
        billing = str(u.get("billing") or "metered")
        cost = cost_usd_micros if cost_usd_micros is not None else u.get("cost_usd_micros")
        return cls(model=str(u.get("model") or ""),
                   input_tokens=int(u.get("prompt_tokens") or u.get("input_tokens") or 0),
                   output_tokens=int(u.get("completion_tokens") or u.get("output_tokens") or 0),
                   cache_read_tokens=int(u.get("cache_read_tokens") or 0),
                   cache_write_tokens=int(u.get("cache_write_tokens") or 0),
                   cost_usd_micros=int(cost or 0),
                   billing=billing if billing in ("metered", "subscription", "local") else "metered",
                   price_estimated=bool(u.get("price_estimated")))


@dataclass(frozen=True)
class Event:
    """One thing that happened in a run. `(run_id, seq)` is unique; `data` is redacted and clipped
    by the ledger writer before it is stored, never by the loop."""

    kind: str
    run_id: str
    seq: int
    step: int
    at: datetime
    data: Mapping[str, Any] = field(default_factory=dict)
    act_id: Optional[str] = None
    usage: Optional[Usage] = None
    latency_ms: int = 0
    policy: Mapping[str, str] = field(default_factory=dict)
    workspace_ref: Optional[str] = None

    def as_dict(self) -> dict:
        out = asdict(self)
        out["at"] = self.at.isoformat()
        return out


def now() -> datetime:
    return datetime.now(timezone.utc)


@dataclass
class ToolCall:
    """A call the model asked for. `arguments` is None when `raw_arguments` was not a JSON
    object: the kernel refuses it (schema.invalid) instead of running the tool on `{}`."""

    id: str
    name: str
    arguments: Optional[dict]
    raw_arguments: str = ""

    def as_openai(self) -> dict:
        return {"id": self.id, "type": "function",
                "function": {"name": self.name, "arguments": self.raw_arguments}}


StopReasonKind = Literal["end_turn", "tool_use", "max_tokens", "refusal", "error"]


@dataclass
class ModelTurn:
    """What `Engine.complete` returns. `raw` is the provider's own assistant message, replayed
    verbatim into the next request so nothing the provider needs is lost in translation."""

    text: str
    tool_calls: list
    stop_reason: StopReasonKind
    usage: Usage
    raw: dict

    @property
    def empty(self) -> bool:
        return not (self.text or "").strip() and not self.tool_calls


ToolStatus = Literal["ok", "error", "refused", "deferred", "skipped"]


@dataclass(frozen=True)
class ToolSchema:
    """A tool as offered to a model. `parameters` is the JSON schema the kernel validates the
    call's arguments against before the tool runs."""

    name: str
    description: str
    parameters: Mapping[str, Any]
    kind: Literal["read", "write"] = "read"
    reversibility: str = "safe"
    deferred: bool = False
    #: The catalogue permission a call needs (IDENTITY.md §6.4). The mandatory `authz`
    #: middleware decides it against the run token inside a delegated run (toolset.UNDECLARED
    #: is refused there: deny by default; toolset.GATED_BY_REGISTRY hands over to the
    #: registry's gate; None is a caller's own function, decided by its toolset). Outside a
    #: delegated run it is not read.
    permission: Optional[str] = None
    #: How this tool's result arrives from outside the operator (low_trust.outside_origin), or
    #: None for the operator's own data. The mandatory `tool_screen` middleware screens, marks
    #: and remembers a result whose tool names an origin.
    origin: Optional[str] = None
    #: The result was screened where it was produced (mcp_registry's receipt door): the
    #: middleware still marks and remembers it, but never screens it twice.
    prescreened: bool = False
    #: A write's idempotency class (kernel/idempotency.py): what a resumed run may do with a
    #: call of it whose outcome was never recorded. None on a write reads as irreversible.
    idempotency: Optional[str] = None

    def as_openai(self, *, strict: bool = False) -> dict:
        fn: dict = {"name": self.name, "description": self.description,
                    "parameters": dict(self.parameters)}
        if strict:
            fn["strict"] = True
        return {"type": "function", "function": fn}

    @classmethod
    def from_openai(cls, schema: Mapping[str, Any], *, kind: str = "read",
                    reversibility: str = "safe", permission: Optional[str] = None,
                    origin: Optional[str] = None, prescreened: bool = False,
                    idempotency: Optional[str] = None) -> "ToolSchema":
        fn = schema.get("function") if isinstance(schema.get("function"), Mapping) else schema
        return cls(name=str(fn.get("name") or ""), description=str(fn.get("description") or ""),
                   parameters=dict(fn.get("parameters") or {"type": "object", "properties": {}}),
                   kind="write" if kind == "write" else "read", reversibility=reversibility,
                   permission=permission, origin=origin, prescreened=prescreened,
                   idempotency=idempotency)


@dataclass
class ToolResult:
    """What a tool call came to. `content` goes to the model clipped by the context policy and to
    the ledger whole (redacted). `deferred` means the run suspends: on a person's approval
    (`decision_id`) or on the budget (`budget`)."""

    call_id: str
    name: str
    status: ToolStatus
    content: Any
    untrusted: bool = False
    decision_id: Optional[str] = None
    budget: Optional[dict] = None
    pending: Optional[dict] = None


@dataclass(frozen=True)
class Finding:
    """The one structured feedback shape: a test, lint, review, gate or schema result a model can
    act on, instead of 3,000 characters of a test tail."""

    source: Literal["test", "lint", "review", "gate", "schema", "tool", "verifier"]
    severity: Literal["blocker", "major", "minor", "info"]
    message: str
    path: Optional[str] = None
    line: Optional[int] = None
    quote: str = ""
    test_id: Optional[str] = None
    assertion: Optional[str] = None


class ControllerState(Enum):
    CONTINUE = "continue"
    COMPACT = "compact"
    ESCALATE = "escalate"
    SUSPEND = "suspend"
    ANSWER = "answer"
    STOP = "stop"


@dataclass(frozen=True)
class StopReason:
    """Why the loop must answer now (`answer`) or end now (`stop`): steps, turns, cost, budget,
    deadline. `answer` means one more model call with no tools; `stop` means none."""

    kind: Literal["answer", "stop"]
    why: str


@dataclass(frozen=True)
class Acceptance:
    """VerifyPolicy's verdict on a final answer: accepted (with its structure) or refused with
    the reason the model is told when it is asked again."""

    ok: bool
    why: str = ""
    structured: Optional[dict] = None


@dataclass
class Step:
    """One model round-trip and the tools it called."""

    index: int
    turn: ModelTurn
    results: list
    state: ControllerState


#: `denied`: a resume whose decision was not the approval the held call waits for; the run stays
#: suspended and nothing ran.
RunStatus = Literal["completed", "suspended", "budget_wait", "failed", "cancelled", "denied"]


@dataclass
class KernelResult:
    """The end of one `run` or `resume`. `pending` is what a suspended run waits on, in the
    shape agent_runs.pending already has; `exhausted` says why an answer was forced
    ("steps", "turns", "cost", "budget", "deadline") as EngineAgent's AgentTurn did."""

    status: RunStatus
    run_id: str
    text: str = ""
    structured: Optional[dict] = None
    findings: list = field(default_factory=list)
    pending: Optional[dict] = None
    usage: Usage = field(default_factory=Usage)
    steps: int = 0
    exhausted: str = ""
    stopped: str = ""
    error: str = ""
    trace: list = field(default_factory=list)
    events: list = field(default_factory=list)


def evolve(obj: Any, **changes: Any) -> Any:
    """`dataclasses.replace`, re-exported so callers need not import dataclasses for it."""
    return replace(obj, **changes)


__all__ = ["Acceptance", "Billing", "ControllerState", "EVENT_KINDS", "Event", "EventKind",
           "Finding", "KernelResult", "ModelTurn", "RunStatus", "Step", "StopReason",
           "ToolCall", "ToolResult", "ToolSchema", "ToolStatus", "Usage", "evolve", "now"]
