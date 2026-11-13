"""What an extension author writes against (boss.extension/v1). The only BOSS import an extension needs.

    from boss_engine.extensions.sdk import Extension

    class DeployNotes(Extension):
        def on_tool(self, name, args, ctx):          # tools.register
            ctx.state.put("last", args)
            return {"ok": True}
        def on_events(self, events, ctx):            # events.subscribe
            ...
        def on_job(self, name, ctx):                 # jobs.schedule
            ...
        def on_webhook(self, name, request, ctx):    # webhooks.receive
            return {"status": 200, "body": {"ok": True}}

The extension runs in an isolated child process. `ctx.state` and `ctx.log` are requests to BOSS,
which checks each against the manifest's capabilities (state.read / state.write) and scopes it to
THIS extension's namespace: there is no way to name another extension's keys.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Optional


class ExtensionError(RuntimeError):
    """Raised inside an extension to fail a call with a sentence the caller sees."""


class State:
    """The extension's own key-value store, scoped by BOSS to (this extension, this project).
    A call with no project (a scheduled job) sees the install-wide namespace."""

    def __init__(self, ask: Callable[[dict], dict]) -> None:
        self._ask = ask

    def get(self, key: str, default: Any = None) -> Any:
        found = self._ask({"op": "state.get", "key": key})
        return found["value"] if found.get("found") else default

    def put(self, key: str, value: Any) -> None:
        self._ask({"op": "state.put", "key": key, "value": value})

    def delete(self, key: str) -> None:
        self._ask({"op": "state.delete", "key": key})

    def list(self, prefix: str = "") -> list[str]:
        return list(self._ask({"op": "state.list", "prefix": prefix}).get("keys") or [])


@dataclass
class Context:
    extension_id: str
    version: str
    project_id: Optional[str]
    state: State
    log: Callable[[str], Any]
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass
class WebhookRequest:
    hook: str
    body: bytes
    headers: dict[str, str]

    def json(self) -> Any:
        import json

        return json.loads(self.body.decode("utf-8") or "null")


class Extension:
    """Override the handlers for the capabilities the manifest declares. A handler that is not
    overridden answers an error the caller sees ("does not handle ...")."""

    def on_tool(self, name: str, args: dict[str, Any], ctx: Context) -> Any:
        raise ExtensionError(f"this extension does not handle tool {name}")

    def on_events(self, events: list[dict[str, Any]], ctx: Context) -> Any:
        raise ExtensionError("this extension does not handle events")

    def on_job(self, name: str, ctx: Context) -> Any:
        raise ExtensionError(f"this extension does not handle job {name}")

    def on_webhook(self, name: str, request: WebhookRequest, ctx: Context) -> Any:
        raise ExtensionError(f"this extension does not handle webhook {name}")
