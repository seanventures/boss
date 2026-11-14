"""Shared by the AP-P2 durable-run tests and the child process they kill -9.

A tiny agent with three tools whose EFFECTS are lines appended to a ledger file — so a test can
count, across two processes, exactly how many times each tool really ran:

  read_a       a read (idempotent by construction)
  charge_card  a write declared IRREVERSIBLE; with `hang` it fires and then never returns, so a
               test can kill the process while it is in flight
  notify_b     a write declared KEYED: it records the idempotency key it ran under
"""
from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

from boss_engine.kernel import presets
from boss_engine.kernel.idempotency import current_idempotency_key
from boss_engine.kernel.toolset import FunctionToolSet, Tool
from boss_engine.kernel.types import ToolSchema

OBJ = {"type": "object", "properties": {"x": {"type": "string"}}, "additionalProperties": True}


def ledger_lines(path: Path) -> list:
    if not path.exists():
        return []
    return [line for line in path.read_text().splitlines() if line.strip()]


def count(path: Path, name: str) -> int:
    return sum(1 for line in ledger_lines(path) if line.split(":", 1)[0] == name)


def toolset(ledger: Path, *, hang: bool = False, extra: Any = ()) -> FunctionToolSet:
    def record(text: str) -> None:
        with ledger.open("a") as f:
            f.write(text + "\n")
            f.flush()

    def read_a(args: dict) -> dict:
        record("read_a")
        return {"ok": True, "balance": 120}

    def charge_card(args: dict) -> dict:
        record("charge_card")
        if hang:
            time.sleep(600)  # killed here, mid-call: the effect happened, no result recorded
        return {"ok": True, "charged": args.get("x") or "10"}

    def notify_b(args: dict) -> dict:
        record(f"notify_b:{current_idempotency_key()}")
        return {"ok": True, "sent": True}

    return FunctionToolSet([
        Tool(ToolSchema("read_a", "read the balance", OBJ), read_a),
        Tool(ToolSchema("charge_card", "charge the card", OBJ, kind="write",
                        idempotency="irreversible"), charge_card),
        Tool(ToolSchema("notify_b", "notify the customer", OBJ, kind="write",
                        idempotency="keyed"), notify_b),
        *extra,
    ])


def policies() -> Any:
    return presets.agent_loop(8)


def scripted_batch() -> list:
    from boss_engine.kernel.scripted import Call, Calls, Say

    return [Calls(Call("read_a", {"x": "a"}, id="call_read"),
                  Call("charge_card", {"x": "10"}, id="call_charge"),
                  Call("notify_b", {"x": "b"}, id="call_notify")),
            Say("done: charged and notified")]


def dumps(value: Any) -> str:
    return json.dumps(value, default=str)
