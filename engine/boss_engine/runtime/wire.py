"""The run plane's wire shapes: kernel values as JSON and back, one definition for both ends (the
control plane's services/run_plane.py and the in-VM boss-runtime's remote stores)."""
from __future__ import annotations

import dataclasses
import json
from datetime import datetime
from typing import Any, Optional


def jsonable(value: Any) -> Any:
    return json.loads(json.dumps(value, default=str))


# ── Event + SinkMeta (the ledger) ───────────────────────────────────────────────────────────

def event_out(event: Any) -> dict:
    out = dataclasses.asdict(event)
    out["at"] = event.at.isoformat()
    return jsonable(out)


def event_in(data: dict) -> Any:
    from ..kernel.types import Event, Usage

    body = dict(data)
    body["at"] = datetime.fromisoformat(str(body["at"]))
    if isinstance(body.get("usage"), dict):
        body["usage"] = Usage(**{k: v for k, v in body["usage"].items()
                                 if k in {f.name for f in dataclasses.fields(Usage)}})
    names = {f.name for f in dataclasses.fields(Event)}
    return Event(**{k: v for k, v in body.items() if k in names})


def meta_out(meta: Any) -> dict:
    return jsonable(dataclasses.asdict(meta)) if meta is not None else {}


def meta_in(data: Optional[dict]) -> Any:
    from ..kernel.ledger import SinkMeta

    names = {f.name for f in dataclasses.fields(SinkMeta)}
    return SinkMeta(**{k: v for k, v in (data or {}).items() if k in names})


# ── Spans ───────────────────────────────────────────────────────────────────────────────────

def span_out(span: Any) -> dict:
    return jsonable(dataclasses.asdict(span))


def span_in(data: dict) -> Any:
    from ..kernel.trace import Span

    names = {f.name for f in dataclasses.fields(Span)}
    return Span(**{k: v for k, v in data.items() if k in names})


# ── Tools ───────────────────────────────────────────────────────────────────────────────────

def schema_out(schema: Any) -> dict:
    return jsonable(dataclasses.asdict(schema))


def schema_in(data: dict) -> Any:
    from ..kernel.types import ToolSchema

    names = {f.name for f in dataclasses.fields(ToolSchema)}
    return ToolSchema(**{k: v for k, v in data.items() if k in names})


def result_out(result: Any) -> dict:
    return jsonable(dataclasses.asdict(result))


def result_in(data: dict) -> Any:
    from ..kernel.types import ToolResult

    names = {f.name for f in dataclasses.fields(ToolResult)}
    return ToolResult(**{k: v for k, v in data.items() if k in names})


# ── Interrupts ──────────────────────────────────────────────────────────────────────────────

def interrupt_out(item: Any) -> dict:
    return jsonable(dataclasses.asdict(item))


def interrupt_in(data: dict) -> Any:
    from ..kernel.interrupts import Interrupt

    names = {f.name for f in dataclasses.fields(Interrupt)}
    return Interrupt(**{k: v for k, v in data.items() if k in names})


__all__ = ["event_in", "event_out", "interrupt_in", "interrupt_out", "jsonable", "meta_in",
           "meta_out", "result_in", "result_out", "schema_in", "schema_out", "span_in",
           "span_out"]
