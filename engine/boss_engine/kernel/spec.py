"""RunSpec: everything one kernel run is made of, named in one place.

The engine (or one built by the router), the tools, the five policies, who the run is (project,
act, lineage), where its state and events go, and the caller's hooks. Nothing here is decided
by the kernel: a caller that wants agent_loop's rules passes agent_loop's preset
(presets.py), and the kernel follows them.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional, Union

from .engine import CancelToken
from .policies import Policies


@dataclass
class RunSpec:
    engine: Any
    tools: Any
    policies: Policies
    project_id: str
    #: The system prompt: a string (one cached block) or ordered engine.Block values.
    system: Union[None, str, list] = None
    #: The conversation so far, ending with the new user message (OpenAI chat shape).
    messages: list = field(default_factory=list)
    run_id: Optional[str] = None
    #: The retry chain's first run; defaults to this run.
    lineage_id: Optional[str] = None
    attempt: int = 1
    act_id: Optional[str] = None
    actor: str = "agent"
    stage: Optional[str] = None
    scaffold_level: str = "light"
    #: Save a checkpoint after every step (a suspension, budget wait or cancel always saves).
    persist: bool = False
    #: checkpoint.CheckpointStore; DEFAULT_STORE (this process) when None.
    store: Any = None
    #: ledger.DbSink / MemorySink; None records events on the result only.
    sink: Any = None
    #: governance.BudgetGate: when set, every model call is reserved, receipted and settled.
    gate: Any = None
    #: A ledger reader (ledger_tool.DbLedgerReader / MemoryLedgerReader): offers read_run_ledger.
    ledger_reader: Any = None
    on_event: Optional[Callable[[Any], None]] = None
    on_delta: Optional[Callable[[str], None]] = None
    cancel: Optional[CancelToken] = None
    #: A git worktree: after a step that ran a write tool its tree sha is recorded.
    workspace: Optional[Path] = None
    #: The message the model reads after a held call a person approved has run
    #: (agent_loop's "APPROVED & EXECUTED" note): (pending, rendered result) -> text, or None.
    resume_note: Optional[Callable[[dict, str], str]] = None
    #: The caller's own state, carried in the checkpoint untouched.
    extra: dict = field(default_factory=dict)
    sleep: Callable[[float], None] = time.sleep
    clock: Callable[[], float] = time.monotonic


__all__ = ["RunSpec"]
