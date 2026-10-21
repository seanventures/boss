"""Questions agents ask of the code and lifecycle graph: the MCP tools' engine (spec §3.5, Phase 1).

* `symbol`: where a symbol is defined, what it is, and how connected it is.
* `callers`: who calls or refers to it, transitively up to a depth.
* `blast_radius`: what a change to a symbol or file can break: the symbols, files, endpoints
  and tests that depend on it.
* `why`: the history behind a symbol or file: the PRs that changed it and the specs they
  implemented, the decisions and who made them, review findings, and incidents with their root
  causes and fixes.

Every traversal is the store's scoped, time-aware recursive CTE (Phase 0 benchmark); every
answer is limited to the caller's project.
"""
from __future__ import annotations

from collections import defaultdict
from typing import Any

from sqlalchemy import text
from sqlalchemy.engine import Connection

from .store import Pattern, PostgresKnowledgeStore, Scope

DEPENDENCY_EDGES = ("calls", "references", "imports")
CALLER_EDGES = ("calls", "references")
MAX_LIST = 200


class AmbiguousTarget(LookupError):
    def __init__(self, target: str, candidates: list[dict]):
        super().__init__(f"{target!r} matches {len(candidates)} symbols; pass one of their keys")
        self.candidates = candidates


def is_test_path(path: str | None) -> bool:
    p = f"/{path or ''}"
    name = p.rsplit("/", 1)[-1]
    return "/tests/" in p or "/test/" in p or name.startswith("test_") or name.endswith(
        ("_test.py", "_test.go", ".test.ts", ".test.js", ".spec.ts", ".spec.js", "Test.java", "Tests.cs"))


class KnowledgeQueries:
    def __init__(self, store: PostgresKnowledgeStore | None = None):
        self.store = store or PostgresKnowledgeStore()

    # --- targets ----------------------------------------------------------------------------------
    def find_symbols(self, c: Connection, scope: Scope, query: str, *, repo: str | None = None,
                     limit: int = 20) -> list[dict]:
        """Exact key, exact qualified name, or a name ending in `.query` / `::query`."""
        self.store.require_scope(c, scope)
        rows = c.execute(text(
            """SELECT id, node_key, name, repo, path, attrs FROM kg_nodes
               WHERE project_id = :p AND kind = 'symbol' AND valid_to IS NULL
                 AND (node_key = :q OR name = :q OR name LIKE :suffix)
                 AND (CAST(:repo AS TEXT) IS NULL OR repo = :repo)
               ORDER BY (node_key = :q) DESC, (name = :q) DESC, length(name), node_key LIMIT :n"""),
            {"p": scope.project_id, "q": query, "suffix": "%." + query.replace("%", r"\%"), "repo": repo,
             "n": max(1, min(limit, MAX_LIST))}).mappings().all()
        return [self._symbol_row(r) for r in rows]

    @staticmethod
    def _symbol_row(r) -> dict:
        attrs = r["attrs"] or {}
        return {"key": r["node_key"], "name": r["name"], "repo": r["repo"], "path": r["path"],
                "line": attrs.get("line"), "end_line": attrs.get("end_line"), "kind": attrs.get("symbol_kind"),
                "lang": attrs.get("lang")}

    def resolve(self, c: Connection, scope: Scope, target: str, *, repo: str | None = None) -> dict:
        """A symbol (key or name) or a file path -> the node ids a traversal starts from."""
        self.store.require_scope(c, scope)
        files = c.execute(text(
            "SELECT id, node_key, repo, path FROM kg_nodes WHERE project_id = :p AND kind = 'file' AND valid_to IS NULL "
            "AND (node_key = :t OR path = :t) AND (CAST(:repo AS TEXT) IS NULL OR repo = :repo)"),
            {"p": scope.project_id, "t": target, "repo": repo}).mappings().all()
        if len(files) == 1:
            f = files[0]
            inside = c.execute(text(
                "SELECT id FROM kg_nodes WHERE project_id = :p AND repo = :r AND path = :path AND valid_to IS NULL "
                "AND kind IN ('symbol', 'module')"), {"p": scope.project_id, "r": f["repo"], "path": f["path"]}).scalars().all()
            return {"kind": "file", "key": f["node_key"], "repo": f["repo"], "path": f["path"], "seeds": [f["id"], *inside]}
        matches = self.find_symbols(c, scope, target, repo=repo, limit=50)
        exact = [m for m in matches if m["key"] == target] or [m for m in matches if m["name"] == target]
        pool = exact or matches
        if not pool:
            raise LookupError(f"no symbol or file {target!r} in the knowledge graph")
        if len(pool) > 1:
            raise AmbiguousTarget(target, pool[:20])
        sym = pool[0]
        # A class stands for its members too: a change to a class's method is a change to the class.
        seeds = c.execute(text(
            "SELECT id FROM kg_nodes WHERE project_id = :p AND kind = 'symbol' AND valid_to IS NULL AND repo = :r "
            "AND path = :path AND (node_key = :k OR node_key LIKE :members)"),
            {"p": scope.project_id, "r": sym["repo"], "path": sym["path"], "k": sym["key"],
             "members": sym["key"].replace("%", r"\%") + ".%"}).scalars().all()
        return {"kind": "symbol", **sym, "seeds": seeds}

    # --- tools ------------------------------------------------------------------------------------
    def symbol(self, c: Connection, scope: Scope, query: str, *, repo: str | None = None) -> dict:
        found = self.find_symbols(c, scope, query, repo=repo)
        keys = [f["key"] for f in found]
        degree = {}
        if keys:
            for r in c.execute(text(
                    """SELECT n.node_key,
                              count(*) FILTER (WHERE e.dst_id = n.id AND e.kind IN ('calls', 'references')) AS callers,
                              count(*) FILTER (WHERE e.src_id = n.id AND e.kind IN ('calls', 'references')) AS callees
                       FROM kg_nodes n JOIN kg_edges e ON (e.dst_id = n.id OR e.src_id = n.id) AND e.valid_to IS NULL
                       WHERE n.project_id = :p AND n.node_key = ANY(:k) GROUP BY n.node_key"""),
                    {"p": scope.project_id, "k": keys}).mappings():
                degree[r["node_key"]] = {"callers": r["callers"], "callees": r["callees"]}
        return {"query": query, "symbols": [{**f, **degree.get(f["key"], {"callers": 0, "callees": 0})} for f in found]}

    def callers(self, c: Connection, scope: Scope, target: str, *, depth: int = 1, repo: str | None = None) -> dict:
        t = self.resolve(c, scope, target, repo=repo)
        walk = self.store.traverse(c, scope, Pattern(seeds=t["seeds"], depth=max(1, min(depth, 4)),
                                                     edge_kinds=CALLER_EDGES, direction="in", limit=5000))
        seeds = set(t["seeds"])
        rows = [n for n in walk["nodes"] if n["id"] not in seeds]
        return {"target": _public(t), "depth": walk["depth"], "count": len(rows),
                "callers": [{"key": n["node_key"], "name": n["name"], "kind": n["kind"], "path": n["path"],
                             "depth": n["depth"]} for n in rows[:MAX_LIST]]}

    def blast_radius(self, c: Connection, scope: Scope, target: str, *, depth: int = 2, repo: str | None = None,
                     include_tests: bool = True) -> dict:
        t = self.resolve(c, scope, target, repo=repo)
        walk = self.store.traverse(c, scope, Pattern(seeds=t["seeds"], depth=max(1, min(depth, 4)),
                                                     edge_kinds=DEPENDENCY_EDGES, direction="in", limit=20000))
        seeds = set(t["seeds"])
        reached = [n for n in walk["nodes"] if n["id"] not in seeds]
        files: dict[str, int] = {}
        for n in reached:
            if n["path"] and n["path"] != t.get("path"):
                files[n["path"]] = min(files.get(n["path"], 99), n["depth"])
        symbols = [n for n in reached if n["kind"] == "symbol"]
        tests = sorted(p for p in files if is_test_path(p))
        if not include_tests:
            files = {p: d for p, d in files.items() if not is_test_path(p)}
            symbols = [s for s in symbols if not is_test_path(s["path"])]
        endpoints = []
        ids = [n["id"] for n in reached if n["kind"] == "symbol"] + list(seeds)
        if ids:
            endpoints = [dict(r) for r in c.execute(text(
                "SELECT DISTINCT d.name, d.node_key FROM kg_edges e JOIN kg_nodes d ON d.id = e.dst_id "
                "WHERE e.project_id = :p AND e.kind = 'exposes' AND e.valid_to IS NULL AND e.src_id = ANY(:ids)"),
                {"p": scope.project_id, "ids": ids}).mappings()]
        by_depth: dict[int, list[str]] = defaultdict(list)
        for path, d in sorted(files.items()):
            by_depth[d].append(path)
        return {"target": _public(t), "depth": walk["depth"],
                "files": len(files), "symbols": len(symbols), "tests": len(tests), "endpoints": endpoints[:MAX_LIST],
                "files_by_depth": {d: paths[:MAX_LIST] for d, paths in sorted(by_depth.items())},
                "symbols_list": [{"key": s["node_key"], "path": s["path"], "depth": s["depth"]} for s in symbols[:MAX_LIST]]}

    def direct_dependent_files(self, c: Connection, scope: Scope, target: str, *, repo: str | None = None) -> set[str]:
        """Depth-1 dependents as a set of files (the Phase 1 acceptance measure)."""
        radius = self.blast_radius(c, scope, target, depth=1, repo=repo)
        return set(radius["files_by_depth"].get(1, []))

    def why(self, c: Connection, scope: Scope, target: str, *, repo: str | None = None) -> dict:
        t = self.resolve(c, scope, target, repo=repo)
        ids = list(t["seeds"])
        if t["kind"] == "symbol":
            # Lifecycle records point at files as well as symbols: include the symbol's file.
            fid = c.execute(text("SELECT id FROM kg_nodes WHERE project_id = :p AND kind = 'file' AND repo = :r "
                                 "AND path = :path AND valid_to IS NULL"),
                            {"p": scope.project_id, "r": t["repo"], "path": t["path"]}).scalar()
            if fid:
                ids.append(fid)

        def incoming(node_ids, kinds):
            if not node_ids:
                return []
            return [dict(r) for r in c.execute(text(
                "SELECT s.id, s.kind, s.node_key, s.name, s.attrs, e.kind AS edge, e.attrs AS edge_attrs "
                "FROM kg_edges e JOIN kg_nodes s ON s.id = e.src_id WHERE e.project_id = :p AND e.valid_to IS NULL "
                "AND e.dst_id = ANY(:ids) AND e.kind = ANY(:kinds)"),
                {"p": scope.project_id, "ids": list(node_ids), "kinds": list(kinds)}).mappings()]

        def outgoing(node_ids, kinds):
            if not node_ids:
                return []
            return [dict(r) for r in c.execute(text(
                "SELECT e.src_id, d.id, d.kind, d.node_key, d.name, d.attrs, e.kind AS edge FROM kg_edges e "
                "JOIN kg_nodes d ON d.id = e.dst_id WHERE e.project_id = :p AND e.valid_to IS NULL "
                "AND e.src_id = ANY(:ids) AND e.kind = ANY(:kinds)"),
                {"p": scope.project_id, "ids": list(node_ids), "kinds": list(kinds)}).mappings()]

        prs = incoming(ids, ("changed",))
        pr_ids = [p["id"] for p in prs]
        specs = outgoing(pr_ids, ("implements",))
        runs = outgoing(pr_ids, ("evidenced_by",))
        decisions = outgoing([r["id"] for r in runs], ("decided_by",))
        findings = incoming(ids, ("found_in",))
        causes = incoming(ids, ("about",))
        incidents = incoming([x["id"] for x in causes], ("caused_by",))
        fixes = outgoing([i["id"] for i in incidents], ("fixed_by",))
        fixing_prs = incoming(pr_ids, ("fixed_by",))
        return {
            "target": _public(t),
            "pull_requests": [_brief(p) for p in prs],
            "specs": [_brief(s) for s in specs],
            "decisions": [_brief(d) for d in decisions],
            "review_findings": [_brief(f) for f in findings],
            "root_causes": [_brief(x) for x in causes],
            "incidents": [_brief(i) for i in incidents + fixing_prs],
            "fixes": [_brief(f) for f in fixes],
        }


def _public(t: dict) -> dict:
    return {k: v for k, v in t.items() if k != "seeds"}


def _brief(row: dict) -> dict[str, Any]:
    attrs = row.get("attrs") or {}
    keep = {k: attrs[k] for k in ("url", "status", "severity", "reason", "approved_by", "text", "line", "path",
                                  "instructions", "at", "first_seen") if attrs.get(k) not in (None, "", [])}
    return {"key": row["node_key"], "kind": row["kind"], "name": row["name"], **keep}
