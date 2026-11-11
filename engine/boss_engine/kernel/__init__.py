"""THE HARNESS KERNEL (BOSSAPP-958 / 961): one tool loop, five policies, one run ledger.

docs/architecture/HARNESS_KERNEL_DESIGN.md is the design; this package is steps K0 (the ledger:
ledger.py, ledger_read.py, ledger_tool.py, workspace.py) and K1 (the kernel library). Nothing in
BOSS runs on it yet: each old loop moves behind its own flag (K2 decisions, K3 the pre-check,
K4 agent steps, K6 chat, K11 the coder), proven equal first by test_kernel_conformance.py.

A LEAF PACKAGE. No module here imports `boss_engine.services` when it is imported — only inside
the functions that need a service — so the services can import the kernel (llm_telemetry and
the tool-call audit write the ledger through it) without a cycle.

    from boss_engine import kernel
    result = kernel.run(kernel.RunSpec(engine=..., tools=..., policies=kernel.presets.agent_loop(12),
                                       project_id=..., system=..., messages=[...]))
"""
from __future__ import annotations

from . import presets
from .anthropic_engine import AnthropicMessagesEngine
from .engine import Block, CancelToken, EngineTraits, ModelRequest, OpenAIChatEngine
from .governance import GovernedEngine
from .loop import cancel, resume, run
from .policies import (AcceptAny, Caps, ClipContext, OfferAll, ParsedAnswer, Policies, Recover)
from .scoped_tools import ScopedTools
from .spec import RunSpec
from .toolset import Composite, FunctionToolSet, ReadOnlyToolSet, RegistryToolSet, Tool
from .types import (Acceptance, ControllerState, Event, Finding, KernelResult, ModelTurn, Step,
                    StopReason, ToolCall, ToolResult, ToolSchema, Usage)

__all__ = ["AcceptAny", "Acceptance", "AnthropicMessagesEngine", "Block", "CancelToken", "Caps",
           "ClipContext", "Composite",
           "ControllerState", "EngineTraits", "Event", "Finding", "FunctionToolSet",
           "GovernedEngine", "KernelResult", "ModelRequest", "ModelTurn", "OfferAll",
           "OpenAIChatEngine", "ParsedAnswer", "Policies", "ReadOnlyToolSet", "Recover",
           "RegistryToolSet", "RunSpec", "ScopedTools", "Step", "StopReason", "Tool", "ToolCall",
           "ToolResult", "ToolSchema", "Usage", "cancel", "presets", "resume", "run"]
