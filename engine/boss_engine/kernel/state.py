"""The run's state between steps: what a checkpoint holds and what every policy reads.

The old loops kept this in locals (`refused_calls`, `empty_completions`, `tool_failures`,
`asked_again`, `spent`...), so a resumed run forgot all of it and each loop counted differently.
One object, serialised whole into the checkpoint, is what makes `resume` continue the same run
rather than start a similar one (design §2.7).
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Optional

from .engine import Block
from .types import Usage


@dataclass
class RunState:
    run_id: str
    project_id: str
    lineage_id: str
    system: list
    messages: list
    attempt: int = 1
    act_id: Optional[str] = None
    #: Model calls that count against the caps (a refused-call feedback round counts, as it did
    #: in agent_loop; a transient retry or a budget wait does not).
    step: int = 0
    usage: Usage = field(default_factory=Usage)
    last_cost: int = 0
    #: One entry per tool call: {step, tool, args, status, result}. EngineAgent's `trace` and
    #: agent_loop's `transcript` were this, in two shapes.
    trace: list = field(default_factory=list)
    #: Tool results as the model was shown them (a decision's check cites against these).
    rendered: list = field(default_factory=list)
    status: str = "running"
    pending: Optional[dict] = None
    answering: bool = False
    answer_prompted: bool = False
    exhausted: str = ""
    stopped: str = ""
    empties: int = 0
    reasks: int = 0
    refused_calls: int = 0
    invalid: dict = field(default_factory=dict)
    tool_failures: dict = field(default_factory=dict)
    #: Idempotency keys of write calls that already ran: a resume never runs one twice.
    executed: list = field(default_factory=list)
    findings: list = field(default_factory=list)
    notes: list = field(default_factory=list)
    out_of_time: bool = False
    waits: int = 0
    waited_seconds: float = 0.0
    final_text: Optional[str] = None
    structured: Optional[dict] = None
    workspace_ref: Optional[str] = None
    #: The last ledger sequence number this run used when no database allocates them.
    seq: int = 0
    #: The caller's own fields (agent_loop's finalize_ctx rides here, untouched by the kernel).
    extra: dict = field(default_factory=dict)

    def idempotency_key(self, step: int, call_id: str) -> str:
        return f"{self.run_id}:{step}:{call_id}"

    def openai_messages(self) -> list:
        """The whole conversation as agent_runs.messages holds it: the system message first."""
        system = "\n\n".join(b.text for b in self.system if isinstance(b, Block) and b.text)
        return ([{"role": "system", "content": system}] if system else []) + list(self.messages)

    def to_checkpoint(self) -> dict:
        data = asdict(self)
        data["system"] = [{"text": b.text, "cache": b.cache} for b in self.system]
        data["usage"] = asdict(self.usage)
        return data

    @classmethod
    def from_checkpoint(cls, data: dict) -> "RunState":
        data = dict(data)
        data["system"] = [Block(text=str(b.get("text") or ""), cache=bool(b.get("cache")))
                          for b in data.get("system") or []]
        data["usage"] = Usage(**dict(data.get("usage") or {}))
        known = {f for f in cls.__dataclass_fields__}  # type: ignore[attr-defined]
        return cls(**{k: v for k, v in data.items() if k in known})

    def add_usage(self, usage: Usage) -> None:
        self.usage = self.usage + usage
        self.last_cost = int(usage.cost_usd_micros)

    def note(self, text: str) -> None:
        self.notes.append(str(text))


def repair_orphans(messages: list) -> int:
    """Answer every tool call that has no tool message after it — a process that stopped between
    a call and its result left one — with a synthetic error, so the transcript a resumed run sends
    is never malformed (Exo's orphan repair, design §2.7). Returns how many were repaired."""
    import json

    answered = {str(m.get("tool_call_id")) for m in messages
                if isinstance(m, dict) and m.get("role") == "tool"}
    repaired = 0
    out: list = []
    for message in messages:
        out.append(message)
        if not (isinstance(message, dict) and message.get("role") == "assistant"):
            continue
        for call in message.get("tool_calls") or []:
            cid = str(call.get("id") or "")
            if cid and cid not in answered:
                out.append({"role": "tool", "tool_call_id": cid, "content": json.dumps(
                    {"status": "error",
                     "note": "did not complete before the process stopped"})})
                answered.add(cid)
                repaired += 1
    if repaired:
        # Placed right after the turn that made the call (providers require a turn's tool
        # messages before the next user or assistant message), never at the end.
        messages[:] = out
    return repaired


__all__ = ["RunState", "repair_orphans"]
