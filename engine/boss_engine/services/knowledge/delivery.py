"""The two delivery paths of a context pack (KNOWLEDGE_GRAPH_SPEC.md §3.5): same content, every engine.

1. **Tools.** An engine with tool use (the engine-native agent loop, which offers the
   `knowledge.read` bundle) calls `kg_search` with its role and stage, and gets the `Pack`.
2. **Injection.** A CLI harness stage (code_agent._invoke) has no BOSS tools: BOSS's own MCP
   server is not among a harness's servers. Its stage prompt gets the same `Pack`'s text, right
   after the shared TASK CONTEXT block (so the cached prefix is unchanged).

Both call `retrieval.knowledge_pack` with the same reader, role, stage, task and budget, so the
content is the same by construction (`test_the_tool_and_the_injected_pack_are_the_same`).

The reader is the acting agent's: in a delegated run, its run token's scope (the Passport ceiling
intersected with the grant); for an agent principal outside a run, its Passport ceiling. A person
or BOSS itself reads unfiltered, because the route or tool gate has already checked them.
"""
from __future__ import annotations

from typing import Any, Optional

from sqlalchemy import text
from sqlalchemy.engine import Connection

from .retrieval import Pack, Reader, knowledge_pack
from .store import Scope

#: The injected pack's budget, in tokens, when the caller names none.
DEFAULT_STAGE_BUDGET = 1200


def reader_for(c: Connection, project_id: str, auth: Any = None, *, team_id: Optional[str] = None) -> Reader:
    """The Reader for whoever is acting: `auth` (an AuthContext), else the current one."""
    if auth is None:
        try:
            from ...identity import lineage
            from ...tools.registry import CURRENT_TOOL_AUTH

            auth = CURRENT_TOOL_AUTH.get() or lineage.current_context()
        except Exception:  # noqa: BLE001
            auth = None
    if auth is None:
        return Reader(Scope(project_id), team_id=team_id)
    principal = getattr(auth, "principal", None)
    org = getattr(principal, "org_id", None)
    run_token = getattr(auth, "run_token", None)
    if run_token is not None:
        return Reader(Scope(project_id, org_id=org), permissions=frozenset(run_token.scope), team_id=team_id,
                      agent=str(getattr(run_token, "sub", "") or ""))
    kind = str(getattr(getattr(principal, "kind", None), "value", getattr(principal, "kind", "")) or "")
    if kind == "agent":
        held = frozenset(str(r[0]) for r in c.execute(text(
            "SELECT permission_key FROM identity_agent_ceilings WHERE agent_id = :a"), {"a": principal.id}))
        return Reader(Scope(project_id, org_id=org), permissions=held, team_id=team_id, agent=principal.id)
    return Reader(Scope(project_id, org_id=org), team_id=team_id)


def pack(c: Connection, project_id: str, role: str, stage: str, task: dict, *,
         budget_tokens: int = DEFAULT_STAGE_BUDGET, run_id: Optional[str] = None, via: str = "pack",
         auth: Any = None, reader: Optional[Reader] = None, overlay: Any = None) -> Pack:
    """The one pack both paths deliver."""
    if overlay is None and run_id:
        from .overlay import for_run

        overlay = for_run(run_id)
    return knowledge_pack(c, reader or reader_for(c, project_id, auth), role, stage, task, int(budget_tokens),
                          run_id=run_id, step=f"{role}:{stage}", via=via, overlay=overlay)


def _task_from_prompt(prompt: str) -> dict:
    """The task a stage works on, read from its shared TASK CONTEXT block (title and instructions)."""
    body = prompt or ""
    at = body.find("TASK: ")
    end = body.find("ACCEPTANCE CRITERIA:", at)
    return {"text": (body[at + 6:end] if at >= 0 and end > at else body[:1500]).strip()[:2000]}


def inject(prompt: str, *, has_kg_tools: bool, task_source: Optional[str] = None) -> str:
    """The stage prompt with the knowledge pack after its TASK CONTEXT block, for an engine without
    the kg tools. Reads project, run, stage, role and touched files from the LLM-call context
    (llm_telemetry.set_llm_context). Never fails a stage: no context, no database, or no knowledge
    means the prompt is returned unchanged."""
    if has_kg_tools:
        return prompt
    try:
        from ..llm_telemetry import get_llm_context

        ctx = get_llm_context()
        engine, project_id = ctx.get("db_engine"), ctx.get("project_id")
        if engine is None or not project_id:
            return prompt
        task = _task_from_prompt(task_source or prompt)
        task["touched"] = list(ctx.get("touched") or [])
        with engine.begin() as c:
            p = pack(c, str(project_id), str(ctx.get("role_key") or ""), str(ctx.get("stage") or ""), task,
                     budget_tokens=int(ctx.get("knowledge_budget_tokens") or DEFAULT_STAGE_BUDGET),
                     run_id=ctx.get("run_id"), via="pack")
        if not p.text:
            return prompt
        from ..code_agent import split_shared_context

        shared, own = split_shared_context(prompt)
        return shared + p.text + "\n" + own
    except Exception:  # noqa: BLE001 - knowledge is advisory; a stage never fails for it
        return prompt
