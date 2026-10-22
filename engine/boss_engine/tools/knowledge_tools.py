"""Knowledge graph MCP tools (BOSSAPP-622).

Read tools stay read. kg_propose_fact writes an inferred fact and nothing else.
"""
from __future__ import annotations

from .registry import ToolSpec
from ..services.knowledge.service import KnowledgeService

_OBJECT = {"type": "object", "properties": {}, "additionalProperties": False}
_SERVICE = KnowledgeService()


def _props(*names: str) -> dict:
    return {
        "type": "object",
        "properties": {name: {"type": "string"} for name in names},
        "additionalProperties": False,
    }


def _search(ctx, *, project_id, actor, args):
    query = str(args.get("query") or "").strip()
    if not query:
        return {"error": "query is required"}
    with ctx.begin() as connection:
        return _SERVICE.search(connection, project_id=project_id, query=query)


def _symbol(ctx, *, project_id, actor, args):
    name = str(args.get("name") or "").strip()
    if not name:
        return {"error": "name is required"}
    with ctx.begin() as connection:
        return _SERVICE.symbol(connection, project_id=project_id, name=name)


def _callers(ctx, *, project_id, actor, args):
    node_id = str(args.get("node_id") or "").strip()
    if not node_id:
        return {"error": "node_id is required"}
    with ctx.begin() as connection:
        return _SERVICE.callers(connection, project_id=project_id, node_id=node_id)


def _blast(ctx, *, project_id, actor, args):
    node_id = str(args.get("node_id") or "").strip()
    if not node_id:
        return {"error": "node_id is required"}
    with ctx.begin() as connection:
        return _SERVICE.blast(connection, project_id=project_id, node_id=node_id)


def _why(ctx, *, project_id, actor, args):
    node_id = str(args.get("node_id") or "").strip()
    if not node_id:
        return {"error": "node_id is required"}
    with ctx.begin() as connection:
        try:
            return _SERVICE.why(connection, project_id=project_id, node_id=node_id)
        except LookupError:
            return {"error": "node not found"}


def _precedent(ctx, *, project_id, actor, args):
    query = str(args.get("query") or "").strip()
    if not query:
        return {"error": "query is required"}
    with ctx.begin() as connection:
        return _SERVICE.precedent(connection, project_id=project_id, query=query)


def _propose(ctx, *, project_id, actor, args):
    # The payload cannot choose a tier. The service stores inferred.
    with ctx.begin() as connection:
        return _SERVICE.propose_fact(
            connection,
            project_id=project_id,
            statement=str(args.get("statement") or ""),
            subject=str(args.get("subject") or ""),
            predicate=str(args.get("predicate") or ""),
            obj=str(args.get("object") or ""),
            kind=str(args.get("kind") or "lesson"),
        )


def register_knowledge_tools(reg) -> None:
    specs = [
        ("kg_search", "Search knowledge facts in this project.", _props("query"), _search, "read"),
        ("kg_symbol", "Look up a code symbol by name.", _props("name"), _symbol, "read"),
        ("kg_callers", "List symbols that call a node.", _props("node_id"), _callers, "read"),
        ("kg_blast_radius", "Walk call edges out from a symbol.", _props("node_id"), _blast, "read"),
        ("kg_why", "Facts that cite a node.", _props("node_id"), _why, "read"),
        ("kg_precedent", "Similar past facts for a decision, with ids to cite.", _props("query"), _precedent, "read"),
        (
            "kg_propose_fact",
            "Propose an inferred fact. Governed and approved tiers are refused.",
            _props("statement", "subject", "predicate", "object", "kind"),
            _propose,
            "write",
        ),
    ]
    for name, description, parameters, handler, kind in specs:
        reg.register(
            ToolSpec(
                name=name,
                description=description,
                parameters=parameters,
                handler=handler,
                kind=kind,
                reversibility="safe",
                requires_durable_workspace=True,
                permission="knowledge:read" if kind == "read" else "knowledge:manage",
            )
        )
