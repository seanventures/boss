"""The agent definition: `boss.agent/v1` (docs/design/AGENT_PLATFORM.md §4.1).

`spec` is pure (no database): load and validate a document, dump it back, compile it into what
the runtime reads. `versions` keeps the published versions in Postgres and projects a published
version onto the live grant tables (`roles`, `role_skills`, `role_mcp_servers`) in one
transaction. `migration` turns the shipped role and agent YAMLs into v1 specs.
"""
from .spec import (API_VERSION, KIND, AgentSpec, CompiledAgent, SpecError, compile_spec, dump_spec,
                   load_spec)

__all__ = ["API_VERSION", "KIND", "AgentSpec", "CompiledAgent", "SpecError", "compile_spec", "dump_spec",
           "load_spec"]
