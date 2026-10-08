"""BOSS Engine — the OSS-shippable core of the BOSS autonomous software factory.

This package is the workflow engine: it orchestrates coding tasks, discovery,
PRD meetings, previews, governance/gates, sandboxes and microVMs — everything
that turns a request into shipped software. It has **no dependency on the HTTP
API or the web console**; those live in `apps/` and are the internal/enterprise
layer that sits *on top of* this engine.

The engine is consumed three ways, all over the same in-process helper methods
(never over HTTP):

  1. `boss_engine.context.EngineContext` — the dependency container. Wires the
     Settings, the SQLAlchemy engine and every service exactly as the API's
     `main.py` used to, so callers get the engine without FastAPI.
  2. `boss_engine.tools` — the *tool layer*: every engine helper (create a code
     task, list what's running, preview a task, read the DB, check the microVM
     fleet) exposed as a named, schema-typed tool. Shared by the agent and MCP.
  3. `boss_engine.agent` — the engine-native BOSS Agent (a tool-calling loop)
     and `boss_engine.mcp` — the same tools exposed to external agents
     (GitHub Copilot, Claude) over MCP JSON-RPC.

The service graph is physically owned by this package. The internal API and
worker consume the same module objects through compatibility shims, preserving
one implementation and one module identity while they migrate their imports.
"""
from __future__ import annotations

__version__ = "1.0.0-rc.5"

# Inside BOSS's image, BOSS code never runs as root (an exec as root became `boss` here first).
from ._runtime_user import become_runtime_user as _become_runtime_user  # noqa: E402

_become_runtime_user()

# rc6 N-1: a host credential is sent only to its own provider's endpoints. Every request this
# process sends over http.client/urllib and httpx passes that check (services/env_refs.py).
from .services.env_refs import install_outbound_guard as _install_outbound_guard  # noqa: E402

_install_outbound_guard()

__all__ = ["__version__"]
