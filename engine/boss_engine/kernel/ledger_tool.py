"""read_run_ledger: the tool a run asks "what did I already try, and what did it cost?" (§3.5).

READ-ONLY AND SCOPED TO THE CALLER'S OWN RUN. The tool takes no run id: it answers about the run
asking, its retry lineage, or one stage of it, inside the run's own project. A model therefore
cannot read another run's or another project's ledger through it, whatever it writes in the
arguments — the scope is the RunState's, decided by the kernel, never by the model.

It is a kernel built-in (a ToolSet joined by Composite), not a registry tool: chat and MCP have
no run to scope it to. Delegate sessions get it over a per-session MCP instance bound to their
run when K8/K11 build those (design §9.6 question 3).
"""
from __future__ import annotations

from typing import Any, Optional

from .state import RunState
from .toolset import result_from
from .types import ToolCall, ToolResult, ToolSchema

NAME = "read_run_ledger"

SCHEMA = ToolSchema(
    name=NAME,
    description=(
        "What THIS run (or its earlier attempts) already tried, what it cost, which findings are "
        "still open, or its events in order. Read it before repeating a call that failed: the "
        "answer lists each distinct attempt once, with how many times and how it ended."),
    parameters={
        "type": "object",
        "properties": {
            "scope": {"type": "string", "enum": ["run", "lineage", "stage"],
                      "description": "this run, every attempt of it, or one stage of it"},
            "question": {"type": "string",
                         "enum": ["spend", "tried", "open_findings", "timeline"]},
            "stage": {"type": ["string", "null"], "description": "the stage, for scope=stage"},
            "after_seq": {"type": ["integer", "null"],
                          "description": "timeline only: events after this sequence number"},
        },
        "required": ["scope", "question", "stage", "after_seq"],
        "additionalProperties": False,
    },
    kind="read",
)


class DbLedgerReader:
    """Answers from the database ledger (ledger_read), on a short read transaction."""

    def __init__(self, engine: Any):
        self.engine = engine

    def answer(self, state: RunState, scope: str, question: str, stage: Optional[str],
               after_seq: int) -> str:
        from .ledger_read import Scope, answer

        if scope == "lineage":
            target = Scope("lineage", state.lineage_id, state.project_id)
        elif scope == "stage":
            target = Scope("stage", state.run_id, state.project_id, stage=stage or "")
        else:
            target = Scope("run", state.run_id, state.project_id)
        with self.engine.connect() as c:
            return answer(c, target, question, after_seq=after_seq)


class MemoryLedgerReader:
    """Answers from the run's own state, for a run with no database (this attempt only)."""

    def answer(self, state: RunState, scope: str, question: str, stage: Optional[str],
               after_seq: int) -> str:
        from .ledger_read import Attempt, render_spend, render_tried

        if question == "spend":
            return render_spend(state.usage)
        if question == "open_findings":
            return "\n".join(f"- [{f.severity}] {f.source}: {f.message}"
                             for f in state.findings) or "no open findings"
        attempts: dict = {}
        for n, entry in enumerate(state.trace):
            key = (entry.get("tool"), repr(sorted((entry.get("args") or {}).items())))
            if key in attempts:
                attempts[key]["times"] += 1
                attempts[key]["result"] = str(entry.get("status") or "")
                continue
            attempts[key] = {"kind": "tool.result", "what": str(entry.get("tool")),
                             "args_sha256": None, "result": str(entry.get("status") or ""),
                             "times": 1, "last_seq": n, "run_id": state.run_id}
        if question == "timeline":
            return "\n".join(f"step {e.get('step')} {e.get('tool')}: {e.get('status')}"
                             for e in state.trace[int(after_seq or 0):]) or "no tool calls yet"
        rows = [Attempt(**a) for a in reversed(list(attempts.values()))]
        return render_tried(rows) + "\n" + render_spend(state.usage)


class LedgerTool:
    """The ToolSet holding read_run_ledger, answered by `reader`."""

    def __init__(self, reader: Any):
        self.reader = reader

    def schemas(self, *, step: int, state: RunState) -> list:
        return [SCHEMA]

    def invoke(self, call: ToolCall, *, state: RunState,
               approved: Optional[dict] = None) -> ToolResult:
        args = dict(call.arguments or {})
        try:
            body = self.reader.answer(state, str(args.get("scope") or "run"),
                                      str(args.get("question") or "tried"), args.get("stage"),
                                      int(args.get("after_seq") or 0))
        except Exception as exc:  # noqa: BLE001 — a reader that fails is a tool error, not a crash
            return result_from(call, {"ok": False, "error": f"{type(exc).__name__}: {exc}"[:300]})
        return result_from(call, {"ok": True, "answer": body})


__all__ = ["DbLedgerReader", "LedgerTool", "MemoryLedgerReader", "NAME", "SCHEMA"]
