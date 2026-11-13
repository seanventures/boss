"""The child side of the extension broker: `python -m boss_engine.extensions.child DIR ENTRY CHECKSUM`.

The same shape as boss.plugin/v1's child (platform/runner.py): started with an EMPTY environment
(PATH, PYTHONPATH to the engine, HOME at a throwaway directory), it holds no database connection,
no secret and no BOSS credential. It reads ONE request line, runs the one handler, and may ask the
parent for state/log while it does (each ask checked by the parent against the manifest). Then it
writes one result line and exits.

THE CHECKSUM GATE runs here too, before a byte of the extension is imported: the tree on disk must
still hash to what the operator approved, or the child answers `refused` and imports nothing.
"""
from __future__ import annotations

import base64
import hashlib
import importlib.util
import json
import sys
import traceback
from pathlib import Path
from typing import Any


class _Channel:
    def __init__(self) -> None:
        self.inp, self.out = sys.stdin, sys.stdout

    def send(self, obj: dict) -> None:
        self.out.write(json.dumps(obj, default=str) + "\n")
        self.out.flush()

    def ask(self, obj: dict) -> dict:
        self.send(obj)
        reply = json.loads(self.inp.readline() or "{}")
        if "error" in reply:
            raise PermissionError(reply["error"])
        return reply


def _load(directory: str, entrypoint: str, checksum: str) -> Any:
    from .sdk import Extension

    module_name, _, cls_name = entrypoint.partition(":")
    path = Path(directory, *module_name.split("."))
    file = next((c for c in (path.with_suffix(".py"), path / "__init__.py") if c.is_file()), None)
    if file is None:
        raise RuntimeError(f"{entrypoint} is not a file in the bundle")
    if directory not in sys.path:
        sys.path.insert(0, directory)   # the bundle's own sibling modules
    unique = "boss_ext_" + hashlib.sha256(f"{directory}:{checksum}".encode()).hexdigest()[:12]
    spec = importlib.util.spec_from_file_location(f"{unique}_{module_name.replace('.', '_')}", file)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    cls = getattr(module, cls_name, None)
    if not (isinstance(cls, type) and issubclass(cls, Extension)):
        raise RuntimeError(f"{entrypoint} is not an Extension subclass")
    return cls()


def main(directory: str, entrypoint: str, checksum: str) -> int:
    from ..platform.registry import tree_checksum
    from .sdk import Context, State, WebhookRequest

    ch = _Channel()
    # The protocol owns stdout: an extension's own print() goes to stderr (discarded by the
    # parent), so it can never forge a result line.
    sys.stdout = sys.stderr
    request = json.loads(sys.stdin.readline() or "{}")
    actual = tree_checksum(Path(directory))
    if actual != checksum:
        ch.send({"op": "refused", "code": "checksum_changed",
                 "error": "the extension's files changed after an operator approved them"})
        return 3
    try:
        ext = _load(directory, entrypoint, checksum)
    except Exception as exc:  # noqa: BLE001 — a broken bundle is a failed call, said
        ch.send({"op": "error", "code": "load_failed", "error": f"{type(exc).__name__}: {exc}"[:2000]})
        return 2
    c = request.get("context") or {}
    ctx = Context(extension_id=str(c.get("extension_id") or ""), version=str(c.get("version") or ""),
                  project_id=c.get("project_id"), state=State(ch.ask),
                  log=lambda line: ch.ask({"op": "log", "line": str(line)[:2000]}),
                  extra=c.get("extra") or {})
    method, params = request.get("method"), request.get("params") or {}
    try:
        if method == "tool.call":
            result = ext.on_tool(str(params["name"]), dict(params.get("arguments") or {}), ctx)
        elif method == "events.deliver":
            result = ext.on_events(list(params.get("events") or []), ctx)
        elif method == "job.run":
            result = ext.on_job(str(params["name"]), ctx)
        elif method == "webhook.receive":
            req = WebhookRequest(hook=str(params["hook"]),
                                 body=base64.b64decode(params.get("body_b64") or ""),
                                 headers=dict(params.get("headers") or {}))
            result = ext.on_webhook(req.hook, req, ctx)
        else:
            ch.send({"op": "error", "code": "unknown_method", "error": f"unknown method {method}"})
            return 2
    except PermissionError as exc:
        ch.send({"op": "error", "code": "permission_denied", "error": str(exc)[:2000]})
        return 1
    except Exception as exc:  # noqa: BLE001
        ch.send({"op": "error", "code": "handler_failed",
                 "error": f"{type(exc).__name__}: {exc}"[:2000],
                 "trace": traceback.format_exc()[-2000:]})
        return 1
    ch.send({"op": "result", "result": result})
    return 0


if __name__ == "__main__":
    raise SystemExit(main(*sys.argv[1:4]))
