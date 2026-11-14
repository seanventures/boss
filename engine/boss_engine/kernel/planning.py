"""PLANNING AND SELF-CHECK ON THE KERNEL (AP-P7, BOSSAPP-1058; design §5 Phase 7).

  Planning   the opt-in `write_todos` tool (deepagents made its to-do tool opt-in in v0.7 and calls
             it "basically a no-op" that still keeps an agent on track). The list lives in the
             run's state (`state.extra["todos"]`, carried by every checkpoint), is shown to the
             model after each write, and is in the trace twice: the tool span's input, and a
             `todos.updated` event per write. Off unless the spec's `planning.write_todos` is on.

  Rubric     the self-check against the spec's `output_contract` (analogous to deepagents'
             RubricMiddleware). A final answer is checked against the contract's `sections`
             (each must be present as a heading or a labelled line), its JSON `schema` (when it
             names one) and its `artifacts` (each named). A miss is asked again — the MODEL
             evaluates and rewrites its own answer inside the governed loop (every attempt
             reserved and receipted like any model call; no side call escapes the budget) —
             at most `max_attempts` times; then the answer stands with its misses recorded as
             findings. Each check is a `rubric.result` event.
"""
from __future__ import annotations

import json
import re
from typing import Any, Mapping, Optional

from .middleware import Jump, Middleware
from .state import RunState
from .types import Finding, ToolCall, ToolResult, ToolSchema

WRITE_TODOS = "write_todos"
STATUSES = ("pending", "in_progress", "completed")
MAX_TODOS = 30

TODO_SCHEMA = ToolSchema(
    name=WRITE_TODOS,
    description=("Write your plan as a to-do list (the WHOLE list each time: it replaces the "
                 "last). Use it for work with several steps; mark one item in_progress while "
                 "you do it and completed when it is done."),
    parameters={"type": "object", "additionalProperties": False, "required": ["todos"],
                "properties": {"todos": {"type": "array", "maxItems": MAX_TODOS, "items": {
                    "type": "object", "additionalProperties": False,
                    "required": ["content", "status"],
                    "properties": {"content": {"type": "string", "maxLength": 400},
                                   "status": {"type": "string", "enum": list(STATUSES)}}}}}},
    kind="read")


class _TodoTools:
    def __init__(self, run: Any):
        self.run = run

    def schemas(self, *, step: int, state: RunState) -> list:
        return [TODO_SCHEMA]

    def invoke(self, call: ToolCall, *, state: RunState,
               approved: Optional[dict] = None) -> ToolResult:
        todos = [{"content": str(t.get("content") or "")[:400],
                  "status": str(t.get("status") or "pending")}
                 for t in (call.arguments or {}).get("todos") or [] if isinstance(t, dict)]
        todos = todos[:MAX_TODOS]
        state.extra["todos"] = todos
        counts = {s: sum(1 for t in todos if t["status"] == s) for s in STATUSES}
        self.run.rec.emit("todos.updated", {"todos": todos, **counts})
        return ToolResult(call.id, call.name, "ok", {"ok": True, "todos": todos, **counts})


class Planning(Middleware):
    """Offers `write_todos` (opt-in by the spec) and keeps the list in the run's state."""

    name = "planning"
    version = "planning@1"

    def run_tools(self, run: Any) -> _TodoTools:
        return _TodoTools(run)


# ── The rubric ─────────────────────────────────────────────────────────────────────────────────

def contract_misses(text: str, contract: Mapping[str, Any]) -> list:
    """What `text` lacks of the output contract: a section, a schema field, an artifact."""
    misses: list = []
    body = text or ""
    lowered = body.lower()
    for section in contract.get("sections") or []:
        name = str(section).strip()
        if not name:
            continue
        pattern = re.compile(r"(^|\n)\s*(#+\s*|\*\*)?" + re.escape(name.lower().replace("_", " "))
                             + r"|(^|\n)\s*(#+\s*|\*\*)?" + re.escape(name.lower()))
        if not pattern.search(lowered):
            misses.append(f"section '{name}' is missing")
    schema = contract.get("schema")
    if isinstance(schema, Mapping) and schema:
        parsed = _json_of(body)
        if not isinstance(parsed, dict):
            misses.append("the answer is not the JSON object the contract's schema asks for")
        else:
            from .schema_check import check_arguments

            why = check_arguments(parsed, dict(schema))
            if why:
                misses.append(f"the JSON breaks the contract's schema at {why}")
    for artifact in contract.get("artifacts") or []:
        name = str(artifact).strip()
        if name and name.lower() not in lowered and _split(name) not in lowered:
            misses.append(f"artifact '{name}' is not delivered or named")
    return misses


def _split(name: str) -> str:
    return re.sub(r"(?<!^)(?=[A-Z])", " ", name).lower()


def _json_of(text: str) -> Any:
    raw = (text or "").strip()
    if raw.startswith("```"):
        raw = raw.strip("`")
        raw = raw[raw.find("\n") + 1:] if "\n" in raw else raw
    try:
        return json.loads(raw)
    except ValueError:
        start, end = raw.find("{"), raw.rfind("}")
        if 0 <= start < end:
            try:
                return json.loads(raw[start:end + 1])
            except ValueError:
                return None
    return None


REASK = ("SELF-CHECK against your output contract. Before this answer is accepted, evaluate it "
         "against what the contract requires and rewrite it so it meets every point:\n{misses}\n"
         "Reply with the corrected answer only.")


class Rubric(Middleware):
    """See the module docstring. A spec middleware: its after_model runs before `verify`
    accepts the answer, so a miss is asked again first."""

    name = "rubric"
    version = "rubric@1"
    can_jump_to = ("model",)

    def __init__(self, contract: Mapping[str, Any], *, max_attempts: int = 2):
        self.contract = dict(contract or {})
        self.max_attempts = max(0, int(max_attempts))

    def after_model(self, run: Any, turn: Any) -> Optional[Jump]:
        if turn.tool_calls or not self.contract:
            return None
        state = run.state
        misses = contract_misses(turn.text or "", self.contract)
        attempts = int(state.extra.get("_rubric_attempts") or 0)
        run.rec.emit("rubric.result", {"passed": not misses, "misses": misses[:10],
                                       "attempt": attempts + 1})
        if not misses:
            return None
        if attempts >= self.max_attempts or state.answering:
            for miss in misses:
                state.findings.append(Finding(source="verifier", severity="major",
                                              message=f"output contract: {miss}"))
            return None
        state.extra["_rubric_attempts"] = attempts + 1
        state.messages.append({"role": "user", "content": REASK.format(
            misses="\n".join(f"- {m}" for m in misses))})
        return Jump("model")


def from_spec(spec_body: Mapping[str, Any]) -> tuple:
    """The planning and self-check middleware a published spec asks for (both opt-in)."""
    out: list = []
    if bool((spec_body.get("planning") or {}).get("write_todos")):
        out.append(Planning())
    check = spec_body.get("self_check") or {}
    contract = spec_body.get("output_contract") or {}
    if bool(check.get("enabled")) and contract:
        out.append(Rubric(contract, max_attempts=int(check.get("max_attempts") or 2)))
    return tuple(out)


__all__ = ["Planning", "REASK", "Rubric", "STATUSES", "TODO_SCHEMA", "WRITE_TODOS",
           "contract_misses", "from_spec"]
