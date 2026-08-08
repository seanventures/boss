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
# handler(ctx, *, project_id, actor, args) -> dict
ToolHandler = Callable[..., dict]


@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    parameters: dict  # JSON Schema (object)
    handler: ToolHandler
    kind: ToolKind = "read"

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
                )
            )
            return fn

        return deco

    # ---- lookup / listing -------------------------------------------------
    def get(self, name: str) -> ToolSpec:
        try:
            return self._tools[name]
        except KeyError as exc:
            raise ToolError(f"unknown tool: {name}") from exc

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
    def invoke(self, name: str, ctx: Any, *, project_id: str, actor: str, args: dict) -> dict:
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
        try:
            result = spec.handler(ctx, project_id=project_id, actor=actor, args=args or {})
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
