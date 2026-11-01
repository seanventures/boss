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
        found = _SERVICE.search(connection, project_id=project_id, query=query, policy=policy, mode=mode)
        # A rule or learning a search returned to a run or a conversation is "consulted" (§2.5).
        _log_consulted(connection, project_id, [hit.get("node_id") for hit in found.get("hits") or []])
        if not found.get("hits"):
            found["searched"] = _searched(policy, mode, query, "kg_search", found)
        return found


def _searched(policy, mode, query, tool, found=None) -> dict:
    """What an empty answer covered (find.searched): layers, fields, meaning — so an agent says
    "nothing in these fields", never "the graph holds nothing"."""
    from ..services.knowledge.find import searched
    from ..services.knowledge.scopes import KEYWORD_EXCLUDED_LAYERS, LAYERS

    layers = set(policy.layers if policy is not None else LAYERS)
    if mode == "keyword":
        layers -= set(KEYWORD_EXCLUDED_LAYERS)
    retrieval = (found or {}).get("retrieval") or {}
    meaning = None if not retrieval else retrieval.get("mode") == "hybrid"
    return searched(layers=layers, meaning=meaning, query=query, tool=tool)


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
            found = _SERVICE.why(connection, project_id=project_id, node_id=node_id, policy=policy)
        except LookupError:
            # Not a node id: an incident or ticket key (INC-2041), a run id, words. Say which
            # nodes carry it (the one written-item search), so the agent asks kg_why of those.
            hits = _SERVICE.search(connection, project_id=project_id, query=node_id, limit=8,
                                   policy=policy, mode=mode).get("hits") or []
            if not hits:
                return {"error": f"{node_id!r} is not a node id, and no node mentions it.",
                        "searched": _searched(policy, mode, node_id, "kg_why")}
            return {"error": f"{node_id!r} is not a node id. These nodes carry it: call kg_why with one of their ids.",
                    "matches": [{"node_id": h["node_id"], "kind": h.get("kind"), "name": h.get("name"),
                                 "match": h.get("match")} for h in hits]}
        _log_consulted(connection, project_id, [hit.get("node_id") for hit in found.get("why") or []])
        return found


def _log_consulted(connection, project_id, ids) -> None:
    """What a read tool returned to a run — or to a chat turn, whose conversation is its anchor —
    is "consulted" (spec §2.5): the edge to each rule or learning now, with the agent whose turn it
    was. log_use ignores ids that are neither, and a caller in neither a run nor a conversation
    logs nothing. Logging never fails the read."""
    ids = [str(i) for i in ids if i]
    if not ids:
        return
    from ..services.knowledge.learning_writeback import log_use

    try:
        with connection.begin_nested():
            log_use(connection, project_id=project_id, ids=ids, use_kind="consulted")
    except Exception:  # noqa: BLE001 — the answer is the tool's job; the usage row is a label
        import logging

        logging.getLogger(__name__).info("consulted not logged in %s", project_id, exc_info=True)


def _neighbors(ctx, *, project_id, actor, args, policy=None, mode="on"):
    from ..services.knowledge.neighbors import typed_neighbors

    node = str(args.get("node") or args.get("node_id") or "").strip()
    if not node:
        return {"error": "node is required: a node id kg_search returned, or a node's key or exact name"}
    kinds = args.get("edge_kinds")
    if isinstance(kinds, str):
        kinds = [k for k in kinds.split(",") if k.strip()]
    with ctx.begin() as connection:
        try:
            found = typed_neighbors(connection, project_id=project_id, node=node,
                                    hops=int(args.get("hops") or 1),
                                    edge_kinds=kinds if isinstance(kinds, list) else None,
                                    policy=policy, limit=int(args.get("limit") or 40))
        except (LookupError, ValueError) as error:
            return {"error": str(error)}
        # The rules, clauses and learnings it returned are "consulted" (spec §2.5).
        _log_consulted(connection, project_id, [n["id"] for n in found["neighbors"]])
        return found


def _precedent(ctx, *, project_id, actor, args, policy=None, mode="on"):
    query = str(args.get("query") or "").strip()
    if not query:
        return {"error": "query is required"}
    with ctx.begin() as connection:
        found = _SERVICE.precedent(connection, project_id=project_id, query=query, policy=policy, mode=mode)
        # What earlier runs learned (and the decisions it found), returned to this run or
        # conversation: "consulted" (spec §2.5), its edge now.
        _log_consulted(connection, project_id, list(found.get("cited_fact_ids") or [])
                       or [item["id"] for item in found.get("learned") or []])
        if not found.get("cited_fact_ids") and not found.get("learned"):
            found["searched"] = _searched(policy, mode, query, "kg_precedent")
        return found


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
_APPROVED = ("Approved by a person: trusted input. Cite a rule as [R:<id>] (a data-quality rule "
             "read from a rulebook too).")
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
        if rules and status == "approved":
            # What a tool returned is "consulted" (spec §2.5): the run's (or conversation's) edge
            # to the rule. A proposed rule is untrusted and is not "used".
            _log_consulted(connection, project_id, [r["id"] for r in rules])
    out = {"status": status, "trust": _APPROVED if status == "approved" else _PROPOSED,
           "rules": rules, "count": len(rules)}
    if not rules:
        named = [f"{k}: {', '.join(_strings(args.get(k)))}" for k in ("symbols", "packages", "areas")
                 if _strings(args.get(k))]
        out["searched"] = {
            "layers": ["rules"], "status": status,
            "note": (f"No {status} rule " + ("governs " + "; ".join(named) if named else "") +
                     (" or " if named and args.get("query") else "") +
                     (f"matches {args.get('query')!r} (id, title, statement, attributes)" if args.get("query") else "") +
                     ". This covers rules only: learnings (root causes, precedents) are in kg_search "
                     "and kg_precedent."),
        }
    return out


def _propose_rule(ctx, *, project_id, actor, args, policy=None, mode="on"):
    # The payload cannot choose a tier or a status, nor who proposed it: the rule is `inferred`
    # and `proposed`, and its provenance is the LLM context's.
    from ..services.knowledge.rules import RuleRefused, propose_rule
    from ..services.llm_telemetry import get_llm_context

    scope = args.get("scope") if isinstance(args.get("scope"), dict) else {}
    evidence = args.get("evidence") if isinstance(args.get("evidence"), list) else []
    # The turn's speaker (an addressed agent, or BOSS itself — AgentService.chat), else the
    # stage's agent, else the caller.
    held = get_llm_context() or {}
    role = held.get("speaker_role") or held.get("role_key") or actor
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
        ("kg_search", "Search this project's knowledge graph: code, rules, the documents it holds (a "
         "regulation's or a rulebook's clauses, a bank's data-quality rules and their thresholds, critical "
         "data elements) and what agents learned (root causes, precedents, investigations, fixes) by exact "
         "id (an incident or ticket key like INC-2041, a rule id like NPB-DQ-LN-003, a kgn_ node id), words "
         "and meaning — in titles, bodies and attributes (evidence), and the learnings a run or "
         "conversation whose brief names it produced. The same search as the console's Graph. Cite a rule "
         "as [R:<id>], a learning as [L:<id>], a clause or other document node as [D:<id>].",
         _props("query"), _search, "read"),
        ("kg_neighbors", "A node's typed neighbours, one or two hops out: for a data-quality rule the CDE "
         "it CHECKS, the columns and tables it APPLIES_TO, the report lines it REACHES, its Warning and "
         "Error thresholds (OF) and the clause it was read from (evidenced_by); for any node what its "
         "edges join. Pass the id kg_search returned (or a key such as NPB-DQ-LN-003); hops is 1 or 2; "
         "edge_kinds narrows the edges followed.",
         {"type": "object", "properties": {
             "node": {"type": "string", "description": "A node id, key or exact name."},
             "hops": {"type": "integer", "minimum": 1, "maximum": 2},
             "edge_kinds": {"type": "array", "items": {"type": "string"}},
             "limit": {"type": "integer", "minimum": 1, "maximum": 60}},
          "required": ["node"], "additionalProperties": False},
         _neighbors, "read"),
        ("kg_symbol", "Look up a code symbol by name.", _props("name"), _symbol, "read"),
        ("kg_callers", "List symbols that call a node, directly or by dispatch through a supertype.", _props("node_id"), _callers, "read"),
        ("kg_blast_radius", "Walk call edges out from a symbol.", _props("node_id"), _blast, "read"),
        ("kg_why", "Facts that cite a node. Given an id that is not a node (INC-2041), lists the nodes "
         "that carry it.", _props("node_id"), _why, "read"),
        ("kg_precedent", "What earlier runs learned and decided that matches a symptom, an incident id "
         "(INC-2041) or a decision — precedents, root causes, investigations, fixes — with ids to cite.",
         _props("query"), _precedent, "read"),
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
                # quarantined, never binding until a person approves, forgettable. Being queued writes
                # (like file_platform_gap/record_learning), the write gate runs them in-turn instead of
                # returning status='confirmation_required' on a looking-only turn — the Rules-review
                # approval is the only person-gate they need, never a second in-chat confirmation.
                queues_for_review=(kind == "write"),
                reversibility="safe",
                requires_durable_workspace=True,
                permission="knowledge:read",
                # Precedent is past decisions: reading it also needs decision:read.
                also_requires=("decision:read",) if name == "kg_precedent" else (),
            )
        )
