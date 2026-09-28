"""The engine tool layer — registry, specs and schema emission.

Every engine capability the BOSS Agent (or an external MCP client) can use is a
`ToolSpec`: a name, a human description, a JSON-Schema for its arguments, a
`kind` ("read" or "write"), and a `handler`. The handler is a plain Python
function that receives the `EngineContext` and the parsed arguments and returns
a JSON-serializable dict — it calls engine helpers in-process, never HTTP.

One registry, two consumers:
  - `registry.openai_schema()` feeds the engine-native agent's tool-calling loop
    (OpenAI/Anthropic "type: function" shape).
  - `registry.mcp_schema()` feeds the MCP server (`name`/`description`/
    `inputSchema` shape) so GitHub/Claude agents can drive the engine.

`kind` matters: "read" tools are always safe to call; "write" tools create or
mutate workflow state (start a code task, launch a preview) and callers may
require confirmation before invoking them.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Literal, Optional

ToolKind = Literal["read", "write"]

#: WHAT PRESSING THIS COSTS IF IT WAS THE WRONG CALL.
#:
#: `kind` says whether a tool changes anything. It does not say whether the change can be taken
#: back, and that is the question the agent loop actually needs: `retarget_app` and `cancel_job`
#: are both writes, and one of them threw away twenty-seven minutes of a build because nothing
#: distinguished them. The membership lived in two hand-maintained name sets in the agent, so a
#: tool added tomorrow was silently in neither.
#:
#:   safe          — changes state, and the change can be undone or simply redone.
#:   costs_money   — spends real budget per call. Reversible in state, not in spend.
#:   destroys_work — throws away work in flight, or removes a control somebody put there.
#:   irreversible  — cannot be undone by any means BOSS has.
#:
#: `destroys_work` and `irreversible` are never executed by the model. They are offered.
Reversibility = Literal["safe", "costs_money", "destroys_work", "irreversible"]

#: The two the model may never press itself, whatever it was asked.
NEVER_SELF_EXECUTE: frozenset[str] = frozenset({"destroys_work", "irreversible"})

# handler(ctx, *, project_id, actor, args) -> dict
ToolHandler = Callable[..., dict]


@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    parameters: dict  # JSON Schema (object)
    handler: ToolHandler
    kind: ToolKind = "read"
    #: Only meaningful for writes. Reads change nothing, so they are `safe` by construction.
    reversibility: Reversibility = "safe"

    @property
    def self_executable(self) -> bool:
        """May the model run this itself, given permission to write?"""
        return self.reversibility not in NEVER_SELF_EXECUTE

    def openai(self) -> dict:
        """OpenAI/Anthropic function-calling schema (used by the agent loop)."""
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters,
            },
        }

    def mcp(self) -> dict:
        """MCP tools/list schema (used by the MCP server)."""
        return {
            "name": self.name,
            "description": self.description,
            "inputSchema": self.parameters,
        }


class ToolError(Exception):
    """Raised when a tool is unknown or its handler fails cleanly."""


@dataclass
class ToolRegistry:
    _tools: dict[str, ToolSpec] = field(default_factory=dict)

    # ---- registration -----------------------------------------------------
    def register(self, spec: ToolSpec) -> ToolSpec:
        if spec.name in self._tools:
            raise ValueError(f"tool already registered: {spec.name}")
        self._tools[spec.name] = spec
        return spec

    def tool(
        self,
        name: str,
        description: str,
        parameters: Optional[dict] = None,
        *,
        kind: ToolKind = "read",
        reversibility: Optional[Reversibility] = None,
    ) -> Callable[[ToolHandler], ToolHandler]:
        """Decorator form: @registry.tool("list_x", "…", {...}, kind="read")."""

        def deco(fn: ToolHandler) -> ToolHandler:
            self.register(
                ToolSpec(
                    name=name,
                    description=description,
                    parameters=parameters or _empty_object_schema(),
                    handler=fn,
                    kind=kind,
                    # A read changes nothing, so it needs no declaration. A write that does not
                    # declare defaults to `safe` — and a registry test requires every write to
                    # say so on purpose, so the default can never quietly cover a new
                    # destructive tool.
                    reversibility=reversibility or "safe",
                )
            )
            return fn

        return deco

    # ---- lookup / listing -------------------------------------------------
    #: Namespace prefixes some models put in front of a tool name. OpenAI's legacy function
    #: calling used `functions.foo`, and models trained on it still emit `functions.foo` or
    #: `functions/foo` — observed live as `functions/read_logs`, which Groq rejected outright.
    #: Where a provider does NOT validate, the malformed name reaches us instead, and failing
    #: it as "unknown tool" would blame the operator for a model's namespacing habit.
    _NAME_PREFIXES = ("functions/", "functions.", "tools/", "tools.")

    @classmethod
    def canonical_tool_name(cls, name: str) -> str:
        """Strip a model-added namespace prefix. Idempotent, and safe on a plain name."""
        cleaned = (name or "").strip()
        for prefix in cls._NAME_PREFIXES:
            if cleaned.lower().startswith(prefix):
                return cleaned[len(prefix):]
        return cleaned

    def get(self, name: str) -> ToolSpec:
        try:
            return self._tools[name]
        except KeyError:
            pass
        canonical = self.canonical_tool_name(name)
        try:
            return self._tools[canonical]
        except KeyError as exc:
            # Name the tools we DO have. "unknown tool: functions/read_logs" with no list sent
            # the operator to the source to find out what was available.
            available = ", ".join(self.names()[:40])
            raise ToolError(
                f"unknown tool: {name!r}"
                + (f" (also tried {canonical!r})" if canonical != name else "")
                + f". Available: {available}"
            ) from exc

    def names(self) -> list[str]:
        return sorted(self._tools)

    def specs(self, *, kind: Optional[ToolKind] = None) -> list[ToolSpec]:
        items = [self._tools[n] for n in self.names()]
        return [s for s in items if kind is None or s.kind == kind]

    # ---- schema emission --------------------------------------------------
    def openai_schema(self, *, kind: Optional[ToolKind] = None) -> list[dict]:
        return [s.openai() for s in self.specs(kind=kind)]

    def mcp_schema(self, *, kind: Optional[ToolKind] = None) -> list[dict]:
        return [s.mcp() for s in self.specs(kind=kind)]

    # ---- invocation -------------------------------------------------------
    def invoke(
        self,
        name: str,
        ctx: Any,
        *,
        project_id: str,
        actor: str,
        args: dict,
        operator_confirmed: bool = False,
    ) -> dict:
        spec = self.get(name)
        if spec.kind == "write" and not getattr(ctx, "durable_workspace_configured", True):
            return {
                "ok": False,
                "tool": name,
                "status": "blocked",
                "error": "durable_workspace_required",
                "message": (
                    "This BOSS command creates durable work, receipts, approvals, and leases. "
                    "Configure DATABASE_URL for self-hosted Postgres and run "
                    "`boss-engine bootstrap` before invoking write tools."
                ),
            }
        # THE ACK, ENFORCED AT THE DOOR RATHER THAN PER TOOL.
        #
        # The agent loop already refuses to press these itself. The model, MCP, and CLI can
        # supply args, so confirmation must arrive through a separate trusted call path.
        #
        # `run_on_host` has always checked this for itself; the check belongs to the CLASS, not
        # to whichever tool remembered.
        safe_args = dict(args or {})
        safe_args.pop("_operator_confirmed", None)
        if operator_confirmed:
            safe_args["_operator_confirmed"] = True
        if not spec.self_executable and not operator_confirmed:
            return {
                "ok": False, "tool": name, "status": "confirmation_required",
                # `requires_confirmation` and `do_this` are the shape `run_on_host` has always
                # refused with, and its refusal was better than a bare error: it tells the model
                # what to do next instead of leaving it to guess. Every tool in this class now
                # refuses that well, rather than only the one that remembered to.
                "requires_confirmation": True,
                "error": "operator_confirmation_required",
                "reversibility": spec.reversibility,
                "do_this": (
                    f"call propose_action with tool='{name}' and these exact args, plus a label "
                    "saying what it does and why. The console renders it as a button; nothing "
                    "runs until the operator clicks it."
                ),
                # VERBATIM. A confirmation button is worthless if what runs is not what was
                # shown, so the arguments come back untouched for the proposal to carry.
                "proposed_args": safe_args,
                "message": (
                    f"'{name}' is declared '{spec.reversibility}' — it throws away work or "
                    "cannot be undone — so it runs only when an operator clicks it."
                ),
            }
        try:
            result = spec.handler(ctx, project_id=project_id, actor=actor, args=safe_args)
        except ToolError:
            raise
        except Exception as exc:  # normalize handler failures into a tool result
            return {"ok": False, "tool": name, "error": f"{type(exc).__name__}: {exc}"}
        if not isinstance(result, dict):
            result = {"result": result}
        result.setdefault("ok", True)
        result.setdefault("tool", name)
        return result


def _empty_object_schema() -> dict:
    return {"type": "object", "properties": {}, "additionalProperties": False}
