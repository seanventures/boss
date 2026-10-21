"""The knowledge graph's MCP tools (KNOWLEDGE_GRAPH_SPEC.md §3.5, §5; Phase 1).

Four read tools over the code graph and the lifecycle graph, the same for every engine: an engine
with tool use calls them, and the injected context pack (Phase 3) carries the same answers to one
without. They are granted to every role through the `knowledge.read` baseline skill.

They are gated by `repository:read`: what they reveal is the repository's structure and history,
and every agent preset holds it. A dedicated `knowledge:read` permission arrives with the HTTP API.
"""
from __future__ import annotations

from typing import Any

from ..services.knowledge import Scope
from ..services.knowledge.archivist import org_settings
from ..services.knowledge.code_queries import AmbiguousTarget, KnowledgeQueries
from ..services.knowledge.facts import KINDS, FactStore, InvalidFact, Origin
from ..services.knowledge.scrub import scrub
from .registry import ToolRegistry

_QUERIES = KnowledgeQueries()
_TARGET = {"type": "string", "description": "a symbol (its key, qualified name or name) or a file path"}
_REPO = {"type": "string", "description": "restrict to one repository (owner/repo)"}
_PROJECT = {"type": "string", "description": "override the default project"}


def _obj(props: dict, required: list[str]) -> dict:
    return {"type": "object", "properties": props, "required": required, "additionalProperties": False}


def _answer(fn) -> dict[str, Any]:
    try:
        return fn()
    except AmbiguousTarget as exc:
        return {"error": str(exc), "candidates": exc.candidates}
    except LookupError as exc:
        return {"error": str(exc)}


def register_knowledge_tools(reg: ToolRegistry) -> None:
    @reg.tool(
        "kg_symbol",
        "Find a code symbol (function, class, method) in the knowledge graph: where it is defined "
        "(repository, file, lines), what kind it is, and how many callers and callees it has. Use it "
        "before reading files to locate code by name.",
        _obj({"name": {"type": "string", "description": "a name, a qualified name (Class.method) or a key"},
              "repo": _REPO, "project_id": _PROJECT}, ["name"]),
        kind="read", permission="repository:read")
    def kg_symbol(ctx, *, project_id, actor, args):
        scope = Scope(args.get("project_id") or project_id)
        with ctx.begin() as c:
            return _answer(lambda: _QUERIES.symbol(c, scope, args["name"], repo=args.get("repo")))

    @reg.tool(
        "kg_callers",
        "Who calls or refers to a symbol (or anything in a file), transitively up to `depth` hops "
        "(1–4, default 1), with the file and hop count of each caller.",
        _obj({"target": _TARGET, "depth": {"type": "integer", "minimum": 1, "maximum": 4},
              "repo": _REPO, "project_id": _PROJECT}, ["target"]),
        kind="read", permission="repository:read")
    def kg_callers(ctx, *, project_id, actor, args):
        scope = Scope(args.get("project_id") or project_id)
        with ctx.begin() as c:
            return _answer(lambda: _QUERIES.callers(c, scope, args["target"], depth=int(args.get("depth") or 1),
                                                    repo=args.get("repo")))

    @reg.tool(
        "kg_blast_radius",
        "What a change to a symbol or file can break: the files and symbols that depend on it "
        "(calls, references, imports) by hop count, the tests among them, and the HTTP endpoints "
        "whose handlers are affected. Check it before changing shared code.",
        _obj({"target": _TARGET, "depth": {"type": "integer", "minimum": 1, "maximum": 4},
              "include_tests": {"type": "boolean"}, "repo": _REPO, "project_id": _PROJECT}, ["target"]),
        kind="read", permission="repository:read")
    def kg_blast_radius(ctx, *, project_id, actor, args):
        scope = Scope(args.get("project_id") or project_id)
        with ctx.begin() as c:
            return _answer(lambda: _QUERIES.blast_radius(
                c, scope, args["target"], depth=int(args.get("depth") or 2), repo=args.get("repo"),
                include_tests=args.get("include_tests", True) is not False))

    @reg.tool(
        "kg_why",
        "Why a symbol or file is the way it is: the pull requests that changed it and the specs they "
        "implemented, the decisions behind them and who made them (with their reason), review findings "
        "on it, and incidents whose root cause or fix touched it.",
        _obj({"target": _TARGET, "repo": _REPO, "project_id": _PROJECT}, ["target"]),
        kind="read", permission="repository:read")
    def kg_why(ctx, *, project_id, actor, args):
        scope = Scope(args.get("project_id") or project_id)
        with ctx.begin() as c:
            return _answer(lambda: _QUERIES.why(c, scope, args["target"], repo=args.get("repo")))

    # Phase 2: the Archivist's facts. An agent may propose; it can never set the tier.
    @reg.tool(
        "kg_facts",
        "Current knowledge-graph facts (invariants, decisions, lessons, patterns, couplings, ownership, "
        "risks), optionally about one subject or of one kind. Highest trust first: governed, approved, "
        "observed, then inferred (quarantined, unconfirmed). Each fact carries its tier.",
        _obj({"subject": {"type": "string"}, "kind": {"type": "string", "enum": list(KINDS)},
              "include_inferred": {"type": "boolean"}, "project_id": _PROJECT}, []),
        kind="read", permission="repository:read")
    def kg_facts(ctx, *, project_id, actor, args):
        scope = Scope(args.get("project_id") or project_id)
        with ctx.begin() as c:
            return {"facts": FactStore().current(c, scope, subject=args.get("subject"), kind=args.get("kind"),
                                                 include_inferred=args.get("include_inferred", True))}

    @reg.tool(
        "kg_propose_fact",
        "Propose a fact you learned (subject, predicate, object, statement, kind, evidence). It is stored "
        "as `inferred`: quarantined, never binding, and never above governed or approved facts until a "
        "person approves it.",
        _obj({"subject": {"type": "string"}, "predicate": {"type": "string"}, "object": {"type": "string"},
              "statement": {"type": "string"}, "kind": {"type": "string", "enum": list(KINDS)},
              "evidence": {"type": "array", "items": {"type": "string"}},
              "confidence": {"type": "number", "minimum": 0, "maximum": 1}, "project_id": _PROJECT},
             ["subject", "predicate", "statement", "kind"]),
        kind="write", reversibility="safe", permission="repository:read")   # quarantined, forgettable
    def kg_propose_fact(ctx, *, project_id, actor, args):
        scope = Scope(args.get("project_id") or project_id)
        who = str(actor or "agent")
        with ctx.begin() as c:
            policy = org_settings(c, scope)
            fields = {}
            for k in ("subject", "predicate", "object", "statement", "kind", "confidence", "evidence"):
                v = args.get(k)
                if isinstance(v, str):
                    v, _ = scrub(v, policy["pii"])
                elif isinstance(v, list):
                    v = [scrub(str(x), policy["pii"])[0] for x in v]
                fields[k] = v
            try:
                # The origin is fixed here: whatever the payload claims, an agent's fact is inferred.
                return FactStore().write(c, scope, fields, Origin("agent", "agent", who, actor=who))
            except InvalidFact as exc:
                return {"error": str(exc)}
