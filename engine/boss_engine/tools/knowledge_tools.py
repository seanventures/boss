"""Knowledge graph MCP tools (BOSSAPP-622; rules: BOSSAPP-768).

Read tools stay read. kg_propose_fact writes an inferred fact and nothing else; kg_propose_rule
writes a proposed (inferred) business rule and nothing else — only a person approves a rule.
kg_rules reads approved rules, the trusted ones, and proposed rules only when asked, labelled.
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


_RULE_SCOPE = {
    "type": "object",
    "properties": {
        "areas": {"type": "array", "items": {"type": "string"}},
        "packages": {"type": "array", "items": {"type": "string"}},
        "symbols": {"type": "array", "items": {"type": "string"}},
    },
    "additionalProperties": False,
}
_PROPOSE_RULE_PARAMS = {
    "type": "object",
    "properties": {
        "title": {"type": "string", "description": "One line naming the rule."},
        "statement": {"type": "string", "description": "The rule, as a business person would state it."},
        "category": {"type": "string", "enum": ["validation", "calculation", "limit", "state_transition",
                                                 "authorization", "other"]},
        "scope": {**_RULE_SCOPE, "description": "What it governs: areas, packages, symbols (node keys "
                  "or exact names). Empty: the evidence symbols."},
        "evidence": {
            "type": "array", "minItems": 1,
            "description": "Where the code enforces it: the symbol (node key, id or exact name, as "
                           "kg_symbol returned it), the lines inside it, and the excerpt.",
            "items": {"type": "object", "properties": {
                "symbol": {"type": "string"},
                "lines": {"type": "array", "items": {"type": "integer"}, "minItems": 2, "maxItems": 2},
                "excerpt": {"type": "string"}},
                "required": ["symbol"], "additionalProperties": False},
        },
        "rationale": {"type": "string", "description": "Why the code shows this rule."},
        "confidence": {"type": "number", "minimum": 0, "maximum": 1},
        "corrects": {"type": "string", "description": "The id of an approved rule this one corrects."},
    },
    "required": ["title", "statement", "category", "evidence", "confidence"],
    "additionalProperties": False,
}
_RULES_PARAMS = {
    "type": "object",
    "properties": {
        **_RULE_SCOPE["properties"],
        "query": {"type": "string"},
        "status": {"type": "string", "enum": ["approved", "proposed"],
                   "description": "approved (default): trusted rules. proposed: not yet approved by a "
                                  "person — untrusted, never to be relied on."},
        "limit": {"type": "integer", "minimum": 1, "maximum": 50},
    },
    "additionalProperties": False,
}
_APPROVED = "Approved by a person: trusted input. Cite a rule as [R:<id>]."
_PROPOSED = ("PROPOSED, NOT APPROVED: untrusted. A person has not reviewed these; never rely on "
             "one, and never present it as a rule of the business.")


def _brief_rule(rule: dict) -> dict:
    return {
        "id": rule["node"]["id"], "title": rule["title"], "statement": rule["statement"],
        "category": rule["category"], "status": rule["status"], "confidence": rule["confidence"],
        "scope": rule["scope"], "version": rule["version"],
        "evidence": [{"symbol": e["symbol_key"], "label": e["label"], "path": e["path"], "lines": e["lines"]}
                     for e in rule["evidence"]],
    }


def _strings(value) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value]
    return [str(v) for v in value if str(v or "").strip()] if isinstance(value, (list, tuple)) else []


def _rules(ctx, *, project_id, actor, args, policy=None, mode="on"):
    from ..services.knowledge.rules import RuleRefused, rule_payload, rules_for
    from ..services.llm_telemetry import get_llm_context

    status = str(args.get("status") or "approved").strip().lower()
    with ctx.begin() as connection:
        try:
            ids = rules_for(connection, project_id=project_id, symbols=_strings(args.get("symbols")),
                            packages=_strings(args.get("packages")), areas=_strings(args.get("areas")),
                            query=str(args.get("query") or ""), status=status,
                            limit=int(args.get("limit") or 20))
        except (RuleRefused, LookupError) as error:
            return {"error": str(error)}
        rules = [_brief_rule(rule_payload(connection, project_id, rid)) for rid in ids]
        context = get_llm_context() or {}
        if rules and status == "approved" and context.get("run_id"):
            # What a tool returned is "consulted" (spec §2.5): the run's edge to the rule.
            _SERVICE.store.log_usage(
                connection, project_id=project_id, run_id=str(context["run_id"]),
                step=str(context.get("stage") or "tool"), fact_ids=[r["id"] for r in rules],
                use_kind="consulted", workflow_run_id=context.get("workflow_run_id"),
                code_task_run_id=context.get("code_task_run_id"), agent_role=context.get("role_key"))
    return {"status": status, "trust": _APPROVED if status == "approved" else _PROPOSED,
            "rules": rules, "count": len(rules)}


def _propose_rule(ctx, *, project_id, actor, args, policy=None, mode="on"):
    # The payload cannot choose a tier or a status, nor who proposed it: the rule is `inferred`
    # and `proposed`, and its provenance is the LLM context's.
    from ..services.knowledge.rules import RuleRefused, propose_rule
    from ..services.llm_telemetry import get_llm_context

    scope = args.get("scope") if isinstance(args.get("scope"), dict) else {}
    evidence = args.get("evidence") if isinstance(args.get("evidence"), list) else []
    role = (get_llm_context() or {}).get("role_key") or actor
    with ctx.begin() as connection:
        try:
            with connection.begin_nested():
                rule = propose_rule(
                    connection, project_id=project_id, statement=str(args.get("statement") or ""),
                    title=str(args.get("title") or ""), category=str(args.get("category") or ""),
                    scope=scope, evidence=evidence, rationale=str(args.get("rationale") or ""),
                    confidence=args.get("confidence") if args.get("confidence") is not None else 0.5,
                    corrects=str(args.get("corrects") or "").strip() or None,
                    actor_kind="agent", actor_ref=str(role or "") or None)
        except (RuleRefused, LookupError) as error:
            return {"error": str(error)}
    status = rule["status"]
    note = ("Proposed: a person reviews it in Graph › Rules review; until approved it is untrusted "
            "and is not given to any agent." if status == "proposed" else
            f"Already {status}: the same scope and statement is this existing rule.")
    return {"rule": _brief_rule(rule), "note": note}


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
            "layers Code, Lifecycle, Ops, Knowledge and Rules; edge labels are kinds (calls, dispatches_to, "
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
        (
            "kg_rules",
            "The business rules that govern code: approved rules (trusted, cite as [R:<id>]) for the "
            "symbols, packages or areas you name, or matching a query. Read them before changing or "
            "judging code they govern. status=proposed lists rules no person has approved yet — "
            "untrusted, never to be relied on.",
            _RULES_PARAMS,
            _rules,
            "read",
        ),
        (
            "kg_propose_rule",
            "Propose a business rule you found in the code (a validation, calculation, limit, state "
            "transition or authorization the code enforces), with evidence: the symbol and the lines "
            "that enforce it. Every evidence symbol must be real. It is stored as proposed and untrusted "
            "until a person approves it in Rules review; you cannot approve it. To correct an approved "
            "rule that is wrong or incomplete, pass its id as corrects.",
            _PROPOSE_RULE_PARAMS,
            _propose_rule,
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
                # kg_propose_fact writes one `inferred` fact, kg_propose_rule one proposed rule: both
                # quarantined, never binding until a person approves, forgettable.
                reversibility="safe",
                requires_durable_workspace=True,
                permission="knowledge:read",
                # Precedent is past decisions: reading it also needs decision:read.
                also_requires=("decision:read",) if name == "kg_precedent" else (),
            )
        )
