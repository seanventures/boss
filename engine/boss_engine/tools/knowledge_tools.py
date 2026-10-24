"""Knowledge graph MCP tools (BOSSAPP-622).

Read tools stay read. kg_propose_fact writes an inferred fact and nothing else.
"""
from __future__ import annotations

from .registry import ToolSpec
from ..services.knowledge.runtime import knowledge_mode
from ..services.knowledge.scopes import KEYWORD_TOOLS, current_policy, offers_tool
from ..services.knowledge.service import KnowledgeService

_OBJECT = {"type": "object", "properties": {}, "additionalProperties": False}
_SERVICE = KnowledgeService()


def _props(*names: str) -> dict:
    return {
        "type": "object",
        "properties": {name: {"type": "string"} for name in names},
        "additionalProperties": False,
    }


#: What a kg tool says when the project's knowledge_graph switch withholds it (runtime.MODES).
SWITCH_REFUSALS = {
    "off": "The knowledge graph is off for this project (setting knowledge_graph = off).",
    "keyword": "Only keyword search is on for this project (setting knowledge_graph = keyword): "
               "use kg_search.",
}


def _scoped(tool: str, handler):
    """Every kg tool reads under the project's knowledge switch, then under the stage's knowledge
    scope (services/knowledge/scopes.py). Off refuses every tool; keyword offers kg_search alone,
    as plain full-text search. A tool the stage does not offer, or a node outside its scope, is
    refused in words."""
    def run(ctx, *, project_id, actor, args):
        with ctx.begin() as connection:
            mode = knowledge_mode(connection, project_id)
        if mode == "off" or (mode == "keyword" and tool not in KEYWORD_TOOLS):
            return {"error": SWITCH_REFUSALS[mode]}
        policy = current_policy()
        if not offers_tool(policy, tool):
            return {"error": policy.refusal() if tool in _CODE_TOOLS else
                    f"{tool} isn't available in {policy.stage}."}
        try:
            return handler(ctx, project_id=project_id, actor=actor, args=args, policy=policy, mode=mode)
        except PermissionError as refused:
            return {"error": str(refused)}

    return run


_CODE_TOOLS = frozenset({"kg_symbol", "kg_callers", "kg_blast_radius"})


def _search(ctx, *, project_id, actor, args, policy=None, mode="on"):
    query = str(args.get("query") or "").strip()
    if not query:
        return {"error": "query is required"}
    with ctx.begin() as connection:
        return _SERVICE.search(connection, project_id=project_id, query=query, policy=policy, mode=mode)


def _symbol(ctx, *, project_id, actor, args, policy=None, mode="on"):
    name = str(args.get("name") or "").strip()
    if not name:
        return {"error": "name is required"}
    with ctx.begin() as connection:
        return _SERVICE.symbol(connection, project_id=project_id, name=name, policy=policy)


def _callers(ctx, *, project_id, actor, args, policy=None, mode="on"):
    node_id = str(args.get("node_id") or "").strip()
    if not node_id:
        return {"error": "node_id is required"}
    with ctx.begin() as connection:
        return _SERVICE.callers(connection, project_id=project_id, node_id=node_id, policy=policy)


def _blast(ctx, *, project_id, actor, args, policy=None, mode="on"):
    node_id = str(args.get("node_id") or "").strip()
    if not node_id:
        return {"error": "node_id is required"}
    with ctx.begin() as connection:
        return _SERVICE.blast(connection, project_id=project_id, node_id=node_id, policy=policy)


def _why(ctx, *, project_id, actor, args, policy=None, mode="on"):
    node_id = str(args.get("node_id") or "").strip()
    if not node_id:
        return {"error": "node_id is required"}
    with ctx.begin() as connection:
        try:
            return _SERVICE.why(connection, project_id=project_id, node_id=node_id, policy=policy)
        except LookupError:
            return {"error": "node not found"}


def _precedent(ctx, *, project_id, actor, args, policy=None, mode="on"):
    query = str(args.get("query") or "").strip()
    if not query:
        return {"error": "query is required"}
    with ctx.begin() as connection:
        return _SERVICE.precedent(connection, project_id=project_id, query=query, policy=policy, mode=mode)


def _cypher(ctx, *, project_id, actor, args, policy=None, mode="on"):
    query = str(args.get("query") or "").strip()
    if not query:
        return {"error": "query is required"}
    columns = args.get("columns") or None
    if columns is not None and not isinstance(columns, list):
        return {"error": "columns must be a list of names, one per RETURN item"}
    with ctx.begin() as connection:
        try:
            return _SERVICE.cypher(connection, project_id=project_id, query=query,
                                   params=args.get("params") if isinstance(args.get("params"), dict) else None,
                                   columns=columns, limit=int(args.get("limit") or 50), policy=policy)
        except (ValueError, LookupError, TimeoutError, RuntimeError) as error:
            return {"error": str(error)}


def _schema(ctx, *, project_id, actor, args, policy=None, mode="on"):
    with ctx.begin() as connection:
        return _SERVICE.schema(connection, project_id=project_id, policy=policy)


def _propose(ctx, *, project_id, actor, args, policy=None, mode="on"):
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
        ("kg_callers", "List symbols that call a node, directly or by dispatch through a supertype.", _props("node_id"), _callers, "read"),
        ("kg_blast_radius", "Walk call edges out from a symbol.", _props("node_id"), _blast, "read"),
        ("kg_why", "Facts that cite a node.", _props("node_id"), _why, "read"),
        ("kg_precedent", "Similar past facts for a decision, with ids to cite.", _props("query"), _precedent, "read"),
        (
            "kg_schema",
            "What this project's knowledge graph holds, counted from its rows: vertex labels and node "
            "kinds, relationship types with counts and the labels they join, the properties a query "
            "can read, and the repositories indexed. Read it before a kg_cypher query.",
            _OBJECT,
            _schema,
            "read",
        ),
        (
            "kg_cypher",
            "Read-only Cypher over this project's knowledge graph (Apache AGE). Vertex labels are the "
            "layers Code, Lifecycle, Ops and Knowledge; edge labels are kinds (calls, dispatches_to, "
            "overrides, extends, implements, about, changed...); kg_schema lists what this graph has, with counts. Use "
            "relationships for structure questions (who implements, extends or calls X), not name "
            "matching. A result with truncated=true is a sample, not every match. Name one column per "
            "RETURN item. Writes are refused; 5 s and a row limit apply.",
            {"type": "object", "properties": {
                "query": {"type": "string"},
                "columns": {"type": "array", "items": {"type": "string"}},
                "params": {"type": "object"},
                "limit": {"type": "integer", "minimum": 1, "maximum": 1000}},
             "required": ["query"], "additionalProperties": False},
            _cypher,
            "read",
        ),
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
                handler=_scoped(name, handler),
                kind=kind,
                # kg_propose_fact writes one `inferred` fact: quarantined, never binding, forgettable.
                reversibility="safe",
                requires_durable_workspace=True,
                permission="knowledge:read",
                # Precedent is past decisions: reading it also needs decision:read.
                also_requires=("decision:read",) if name == "kg_precedent" else (),
            )
        )
