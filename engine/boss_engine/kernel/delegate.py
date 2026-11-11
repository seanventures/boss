"""Delegate mode: a frontier CLI owns its loop; BOSS wraps it and records the same events.

Claude Code, the Agent SDK and Codex run their own tool loop, sub-agents and compaction. BOSS
does not re-implement those, and never switches them off (`--tools ""`, conversations re-rendered
as text, tool calls asked for as JSON in prose): it gives the session the right context, its
tools over MCP, budgets, independent verification and the ledger (design §2.9).

This module is the CONTRACT only (K1). The sessions themselves come with K8 (subscription chat,
`ClaudeChatSession`) and K11 (the coders' sessions as ledger writers). Today
`ClaudeCodeAgent._invoke` already is a delegate session; its stream-json turns reach the ledger
as `delegate.turn` events through agent_turns (kernel/ledger.record_turn_events).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional, Protocol

from .engine import CancelToken
from .types import Event, Usage


@dataclass
class DelegateBrief:
    prompt: str
    #: Goes to --append-system-prompt: the byte-identical prefix every stage shares (cache).
    shared_context: str
    cwd: Path
    read_only: bool
    #: BOSS's tools as an MCP server scoped to this session (code_task_mcp.py).
    mcp: Any = None
    max_turns: int = 48
    #: Passed as --max-budget-usd. Supported by the CLI and never passed today (inventory §2).
    max_budget_usd: Optional[float] = None
    allowed_tools: str = ""
    model: Optional[str] = None
    effort: Optional[str] = None


@dataclass
class DelegateResult:
    status: str
    text: str
    session_id: Optional[str] = None
    usage: Usage = field(default_factory=Usage)
    turns: int = 0
    workspace_ref: Optional[str] = None


class DelegateSession(Protocol):
    engine: str

    def start(self, brief: DelegateBrief, *, on_event: Callable[[Event], None],
              cancel: CancelToken) -> DelegateResult: ...

    def resume(self, session_id: str, message: str, **kw: Any) -> DelegateResult: ...


__all__ = ["DelegateBrief", "DelegateResult", "DelegateSession"]
