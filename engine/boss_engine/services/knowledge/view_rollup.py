"""View rollups for the Graph area (KNOWLEDGE_GRAPH_PRODUCT_SPEC.md §2.4).

``kg_view_tree``, ``kg_view_edges`` and ``kg_view_sync`` are derived and rebuildable: the
overview and neighbourhood routes read them instead of aggregating 234k edges live (measured on
Fineract: 523 ms ad hoc for the area level, 601 ms to build the whole class level).

- Area: the first path segment of the file (the Gradle/Maven module, e.g. ``fineract-provider``).
  If one segment holds more than 80% of the symbols, the first segment below the longest common
  prefix of that segment's files instead (a single-module repo is not one bubble).
- Package: ``attrs.module``, falling back to the directory. Its key carries its area,
  ``pkg:<repo>:<area>:<package>``: on Fineract 189 of 1,151 packages span more than one area, so
  ``pkg:<repo>:<package>`` would collide in ``kg_view_tree``'s primary key.
- A method's owning class is its node_key minus the last ``.member``. A method whose class the
  index has no node for hangs on its package, never on a key nothing resolves.
- Edges at levels area, package and class; kinds ``calls`` (calls + dispatches_to), ``imports``,
  ``extends`` (extends + implements). Rules and Learning are not rolled up: they are read live.
- Counts: ``class_count`` is types (class, interface, enum, record: the index's symbol_kind
  ``class``), ``method_count`` is methods and functions, ``child_count`` the rows under it.

A rebuild computes every row into temp tables and applies the difference, so a re-index that
changed a few files rewrites a few rows (Fineract: 1.7-1.9 s first build or unchanged, inside a
Docker Desktop VM).

Runs at the end of a successful repository index job and from the worker's reconcile when
``kg_view_sync.nodes`` differs from the live code count. Target under 3 s on Fineract.
"""
from __future__ import annotations

import time
from collections import Counter, defaultdict
from collections.abc import Callable, Iterable
from typing import Any

from sqlalchemy import text
from sqlalchemy.engine import Connection

#: One top-level segment holding more than this share of a repo's symbols is not an area
#: breakdown: the areas are taken one level below that segment's common prefix.
DOMINANT_SHARE = 0.8
#: The area of a file at the root of its repository (or directly in the common prefix).
ROOT_AREA = "(root)"

#: kg_nodes kinds the product shows as Code (spec §2.1): the code storage layer plus the infra
#: topology. The rollup's staleness test and the overview's totals count these.
CODE_KINDS = ("config_key", "endpoint", "file", "module", "queue", "symbol", "table",
              "service", "resource", "deployment", "datastore", "external", "repo")

#: kg_edges kinds folded into each rollup edge kind.
ROLLUP_KINDS = {"calls": "calls", "dispatches_to": "calls", "imports": "imports",
                "extends": "extends", "implements": "extends"}


def area_rule(weighted_paths: Iterable[tuple[str, int]]) -> Callable[[str], str]:
    """The area of a path, for one repository, from its ``(path, symbols)`` pairs (§2.4).

    The first segment, unless one segment holds more than ``DOMINANT_SHARE`` of the symbols: then
    paths under it take the segment just below its longest common directory prefix."""
    pairs = [(p, max(int(w or 0), 0)) for p, w in weighted_paths if p]
    top: Counter[str] = Counter()
    for path, weight in pairs:
        if "/" in path:
            top[path.split("/", 1)[0]] += weight
    total = sum(w for _, w in pairs)
    prefix: list[str] = []
    if top and total:
        segment, held = top.most_common(1)[0]
        if held > DOMINANT_SHARE * total:
            dirs = [p.split("/")[:-1] for p, _ in pairs if p.startswith(segment + "/")]
            prefix = list(dirs[0]) if dirs else [segment]
            for parts in dirs[1:]:
                n = 0
                while n < min(len(prefix), len(parts)) and prefix[n] == parts[n]:
                    n += 1
                del prefix[n:]
            prefix = prefix or [segment]

    def area(path: str) -> str:
        parts = (path or "").split("/")
        if prefix and parts[:len(prefix)] == prefix:
            # a file directly in the common prefix is the prefix's own area
            return parts[len(prefix)] if len(parts) > len(prefix) + 1 else prefix[-1]
        return parts[0] if len(parts) > 1 else ROOT_AREA

    return area


def live_code_count(connection: Connection, project_id: str) -> int:
    return int(connection.execute(text(
        "SELECT count(*) FROM kg_nodes WHERE project_id = :p AND kind = ANY(:k) AND valid_to IS NULL"),
        {"p": project_id, "k": list(CODE_KINDS)}).scalar() or 0)


def sync_row(connection: Connection, project_id: str) -> dict[str, Any] | None:
    row = connection.execute(text(
        "SELECT project_id, built_at, nodes, edges, ms, tree_rows, view_edges FROM kg_view_sync "
        "WHERE project_id = :p"), {"p": project_id}).mappings().first()
    return dict(row) if row else None


def rebuild(connection: Connection, project_id: str) -> dict[str, Any]:
    """Delete and re-insert the project's rollup rows in one transaction (applied as the
    difference: rows that did not change are not rewritten).

    Returns the ``kg_view_sync`` row it wrote: ``{project_id, built_at, nodes, edges, ms}`` (plus
    ``tree_rows`` and ``view_edges``). One rebuild per project at a time (a transaction-scoped
    advisory lock), so two concurrent rebuilds never collide on the keys."""
    started = time.monotonic()
    p = {"p": project_id}
    connection.execute(text("SELECT pg_advisory_xact_lock(hashtext('kg_view_rollup:' || :p))"), p)
    # The joins and groupings below hold ~50 MB on Fineract: in memory instead of spilled to disk
    # (the 4 MB default doubled the build), and no JIT for a one-off statement. This transaction only.
    connection.execute(text("SET LOCAL work_mem = '128MB'"))
    connection.execute(text("SET LOCAL jit = off"))

    # 1. Every file's area, decided per repository in Python (one row per file path).
    paths = connection.execute(text(
        """
        SELECT coalesce(repo, '') AS repo, path, count(*) FILTER (WHERE kind = 'symbol') AS symbols
        FROM kg_nodes
        WHERE project_id = :p AND valid_to IS NULL AND kind IN ('symbol', 'file') AND path IS NOT NULL
        GROUP BY 1, 2
        """), p).all()
    by_repo: dict[str, list[tuple[str, int]]] = defaultdict(list)
    for repo, path, symbols in paths:
        by_repo[repo].append((path, int(symbols)))
    repos, files, areas = [], [], []
    for repo, pairs in by_repo.items():
        rule = area_rule(pairs)
        for path, _ in pairs:
            repos.append(repo)
            files.append(path)
            areas.append(rule(path))

    connection.execute(text("DROP TABLE IF EXISTS kgv_path"))
    connection.execute(text(
        "CREATE TEMP TABLE kgv_path (repo TEXT, path TEXT, area TEXT, PRIMARY KEY (repo, path)) "
        "ON COMMIT DROP"))
    if files:
        connection.execute(text(
            "INSERT INTO kgv_path (repo, path, area) "
            "SELECT * FROM unnest(CAST(:r AS TEXT[]), CAST(:f AS TEXT[]), CAST(:a AS TEXT[]))"),
            {"r": repos, "f": files, "a": areas})

    # 2. Every symbol and file with its class, package and area keys, in one statement.
    connection.execute(text("DROP TABLE IF EXISTS kgv_own"))
    connection.execute(text(
        r"""
        CREATE TEMP TABLE kgv_own ON COMMIT DROP AS
        WITH b AS (
            -- A file's attrs carry its parse facts (large, TOASTed): only symbols' attrs are read.
            SELECT n.id, n.kind, x.repo, n.path, n.node_key, n.name,
                   s.attrs->>'symbol_kind' AS sk,
                   coalesce(s.attrs->>'type_kind', s.attrs->>'symbol_kind') AS tk,
                   CASE WHEN s.attrs->>'line' ~ '^[0-9]+$' THEN (s.attrs->>'line')::INTEGER END AS line,
                   CASE WHEN s.attrs->>'symbol_kind' = 'method'
                        THEN regexp_replace(n.node_key, '\.[^.:]*$', '')
                        WHEN n.kind = 'symbol' THEN n.node_key END AS ck,
                   coalesce(nullif(s.attrs->>'module', ''),
                            CASE WHEN strpos(n.path, '/') > 0 THEN regexp_replace(n.path, '/[^/]*$', '')
                                 ELSE '.' END) AS pkg,
                   x.area
            FROM kg_nodes n
            JOIN kgv_path x ON x.repo = coalesce(n.repo, '') AND x.path = n.path
            CROSS JOIN LATERAL (SELECT CASE WHEN n.kind = 'symbol' THEN n.attrs END AS attrs) s
            WHERE n.project_id = :p AND n.valid_to IS NULL AND n.kind IN ('symbol', 'file')
        ),
        types AS (SELECT node_key FROM b WHERE kind = 'symbol' AND sk IS DISTINCT FROM 'method'),
        -- a file's package is the package of the symbols it defines (its directory when none)
        file_pkg AS (SELECT DISTINCT ON (repo, path) repo, path, pkg FROM b
                     WHERE kind = 'symbol' ORDER BY repo, path, line NULLS LAST)
        SELECT b.id, b.kind, b.repo, b.node_key, b.name, b.sk, b.tk, b.line, b.area,
               coalesce(f.pkg, b.pkg) AS pkg,
               'area:' || b.repo || ':' || b.area AS area_key,
               'pkg:' || b.repo || ':' || b.area || ':' || coalesce(f.pkg, b.pkg) AS pkg_key,
               -- The class-level key: itself for a type or function; a method's owning class when
               -- the index has that class, else NULL (it rolls up at the package level only, and
               -- hangs on its package in the tree).
               CASE WHEN b.kind = 'symbol' AND b.sk IS DISTINCT FROM 'method' THEN b.ck
                    WHEN t.node_key IS NOT NULL THEN b.ck END AS cls
        FROM b
        LEFT JOIN types t ON b.sk = 'method' AND t.node_key = b.ck
        LEFT JOIN file_pkg f ON b.kind = 'file' AND f.repo = b.repo AND f.path = b.path
        """), p)
    connection.execute(text("CREATE INDEX ON kgv_own (id)"))
    # Row counts for the planner: hash joins below, not spilled sorts (2.5x on Fineract).
    connection.execute(text("ANALYZE kgv_own (id)"))

    # 3. The tree, built into a temp table: methods, classes, packages, areas. Symbols only (a
    #    file is not a tree row).
    connection.execute(text("DROP TABLE IF EXISTS kgv_tree"))
    connection.execute(text(
        r"""
        CREATE TEMP TABLE kgv_tree ON COMMIT DROP AS
        SELECT m.node_key AS key, CASE WHEN m.cls IS NOT NULL THEN m.cls ELSE m.pkg_key END AS parent_key,
               'method'::TEXT AS level, m.id AS node_id, regexp_replace(m.name, '^.*\.', '') AS label,
               'method'::TEXT AS symbol_kind, 0::INTEGER AS child_count, 0::INTEGER AS class_count,
               0::INTEGER AS method_count,
               lpad(coalesce(m.line, 0)::TEXT, 9, '0') || ' ' || lower(m.name) AS sort_key,
               m.repo, m.area, m.pkg AS package
        FROM kgv_own m WHERE m.kind = 'symbol' AND m.sk = 'method'
        UNION ALL
        SELECT c.node_key, c.pkg_key, 'class', c.id, c.name, c.tk,
               coalesce(k.n, 0)::INTEGER, CASE WHEN c.sk = 'class' THEN 1 ELSE 0 END, coalesce(k.n, 0)::INTEGER,
               lower(c.name), c.repo, c.area, c.pkg
        FROM kgv_own c
        LEFT JOIN (SELECT cls, count(*) AS n FROM kgv_own WHERE kind = 'symbol' AND sk = 'method'
                   AND cls IS NOT NULL GROUP BY cls) k ON k.cls = c.node_key
        WHERE c.kind = 'symbol' AND c.sk IS DISTINCT FROM 'method'
        UNION ALL
        SELECT pkg_key, min(area_key), 'package', NULL, min(pkg), NULL,
               count(*) FILTER (WHERE sk IS DISTINCT FROM 'method' OR cls IS NULL)::INTEGER,
               count(*) FILTER (WHERE sk = 'class')::INTEGER,
               count(*) FILTER (WHERE sk IN ('method', 'function'))::INTEGER,
               lower(min(pkg)), min(repo), min(area), min(pkg)
        FROM kgv_own WHERE kind = 'symbol' GROUP BY pkg_key
        UNION ALL
        SELECT area_key, NULL, 'area', NULL, min(area), NULL,
               count(DISTINCT pkg_key)::INTEGER,
               count(*) FILTER (WHERE sk = 'class')::INTEGER,
               count(*) FILTER (WHERE sk IN ('method', 'function'))::INTEGER,
               lower(min(area)), min(repo), min(area), NULL
        FROM kgv_own WHERE kind = 'symbol' GROUP BY area_key
        """))

    # 4. The edges: one pass over kg_edges grouped at the class level, then summed up to the
    #    package and area levels. Self-loops at a level are dropped (a class calling itself).
    connection.execute(text("DROP TABLE IF EXISTS kgv_pairs"))
    connection.execute(text(
        """
        CREATE TEMP TABLE kgv_pairs ON COMMIT DROP AS
        SELECT CASE WHEN e.kind IN ('calls', 'dispatches_to') THEN 'calls'
                    WHEN e.kind = 'imports' THEN 'imports' ELSE 'extends' END AS kind,
               s.cls AS s_cls, d.cls AS d_cls, s.pkg_key AS s_pkg, d.pkg_key AS d_pkg,
               s.area_key AS s_area, d.area_key AS d_area, count(*) AS n
        FROM kg_edges e
        JOIN kgv_own s ON s.id = e.src_id
        JOIN kgv_own d ON d.id = e.dst_id
        WHERE e.project_id = :p AND e.valid_to IS NULL
          AND e.kind IN ('calls', 'dispatches_to', 'imports', 'extends', 'implements')
        GROUP BY 1, 2, 3, 4, 5, 6, 7
        """), p)
    connection.execute(text("DROP TABLE IF EXISTS kgv_edges"))
    connection.execute(text(
        " UNION ALL ".join(
            f"""
            SELECT '{level}'::TEXT AS level, {src} AS src_key, {dst} AS dst_key, kind, sum(n)::INTEGER AS weight
            FROM kgv_pairs WHERE {src} IS NOT NULL AND {dst} IS NOT NULL AND {src} <> {dst}
            GROUP BY {src}, {dst}, kind
            """ for level, src, dst in (("class", "s_cls", "d_cls"), ("package", "s_pkg", "d_pkg"),
                                        ("area", "s_area", "d_area"))
        ).join(("CREATE TEMP TABLE kgv_edges ON COMMIT DROP AS ", ""))))

    # 5. Apply only the difference: a re-index changes few rows, so most of the 80k on Fineract
    #    are neither rewritten nor left behind as dead tuples (the table stays its own size).
    tree_cols = ("parent_key", "level", "node_id", "label", "symbol_kind", "child_count", "class_count",
                 "method_count", "sort_key", "repo", "area", "package")
    first = connection.execute(text(
        "SELECT NOT EXISTS (SELECT 1 FROM kg_view_tree WHERE project_id = :p) "
        "AND NOT EXISTS (SELECT 1 FROM kg_view_edges WHERE project_id = :p)"), p).scalar()
    if first:   # the project's first build: plain inserts (an upsert costs a third more per row)
        tree_conflict = edge_conflict = ""
    else:
        connection.execute(text(
            "DELETE FROM kg_view_tree t WHERE t.project_id = :p "
            "AND NOT EXISTS (SELECT 1 FROM kgv_tree n WHERE n.key = t.key)"), p)
        connection.execute(text(
            "DELETE FROM kg_view_edges t WHERE t.project_id = :p AND NOT EXISTS (SELECT 1 FROM kgv_edges n "
            "WHERE n.level = t.level AND n.src_key = t.src_key AND n.dst_key = t.dst_key AND n.kind = t.kind)"), p)
        tree_conflict = (
            f"ON CONFLICT (project_id, key) DO UPDATE SET {', '.join(f'{c} = EXCLUDED.{c}' for c in tree_cols)} "
            f"WHERE ({', '.join(f'kg_view_tree.{c}' for c in tree_cols)}) "
            f"IS DISTINCT FROM ({', '.join(f'EXCLUDED.{c}' for c in tree_cols)})")
        edge_conflict = ("ON CONFLICT (project_id, level, src_key, dst_key, kind) DO UPDATE SET weight = EXCLUDED.weight "
                         "WHERE kg_view_edges.weight IS DISTINCT FROM EXCLUDED.weight")
    connection.execute(text(
        f"INSERT INTO kg_view_tree (project_id, key, {', '.join(tree_cols)}) "
        f"SELECT :p, key, {', '.join(tree_cols)} FROM kgv_tree ORDER BY key {tree_conflict}"), p)
    connection.execute(text(
        "INSERT INTO kg_view_edges (project_id, level, src_key, dst_key, kind, weight) "
        "SELECT :p, level, src_key, dst_key, kind, weight FROM kgv_edges "
        f"ORDER BY level, src_key, dst_key, kind {edge_conflict}"), p)

    counts = connection.execute(text(
        """
        SELECT (SELECT count(*) FROM kgv_tree) AS tree_rows,
               (SELECT count(*) FROM kgv_edges) AS view_edges,
               (SELECT count(*) FROM kg_edges WHERE project_id = :p AND valid_to IS NULL) AS edges
        """), p).mappings().one()
    nodes = live_code_count(connection, project_id)
    ms = int((time.monotonic() - started) * 1000)
    row = connection.execute(text(
        """
        INSERT INTO kg_view_sync (project_id, built_at, nodes, edges, ms, tree_rows, view_edges)
        VALUES (:p, now(), :nodes, :edges, :ms, :tree_rows, :view_edges)
        ON CONFLICT (project_id) DO UPDATE SET built_at = EXCLUDED.built_at, nodes = EXCLUDED.nodes,
            edges = EXCLUDED.edges, ms = EXCLUDED.ms, tree_rows = EXCLUDED.tree_rows,
            view_edges = EXCLUDED.view_edges
        RETURNING project_id, built_at, nodes, edges, ms, tree_rows, view_edges
        """), {**p, "nodes": nodes, "edges": int(counts["edges"]), "ms": ms,
               "tree_rows": int(counts["tree_rows"]), "view_edges": int(counts["view_edges"])}
    ).mappings().one()
    for table in ("kgv_edges", "kgv_pairs", "kgv_tree", "kgv_own", "kgv_path"):
        connection.execute(text(f"DROP TABLE IF EXISTS {table}"))
    return dict(row)


def is_stale(connection: Connection, project_id: str, sync: dict[str, Any] | None = None) -> bool:
    """The rollup no longer describes the live code graph: never built, or its node count differs."""
    sync = sync if sync is not None else sync_row(connection, project_id)
    live = live_code_count(connection, project_id)
    if sync is None:
        return live > 0
    return int(sync["nodes"]) != live


def rebuild_after_index(engine: Any, project_id: str, run: dict[str, Any] | None = None) -> dict[str, Any]:
    """The end of a successful index: rebuild in its own transaction, unless the run changed
    nothing and the rollup is current. A failure is reported in the answer and left to the
    worker's reconcile; it never fails the index that succeeded."""
    try:
        with engine.begin() as connection:
            if (run or {}).get("mode") == "unchanged" and not is_stale(connection, project_id):
                return {"project_id": project_id, "mode": "current"}
            return rebuild(connection, project_id)
    except Exception as error:  # noqa: BLE001 - the reconcile retries it
        return {"project_id": project_id, "error": f"{type(error).__name__}: {str(error)[:200]}"}


def reconcile(engine: Any, *, project_id: str | None = None) -> dict[str, int]:
    """The worker's reconcile: rebuild each project's rollup that is stale. One project's failure
    never blocks the others."""
    from .. import projects_archive

    out = {"current": 0, "rebuilt": 0, "failed": 0}
    with engine.begin() as connection:
        projects = projects_archive.live_ids(connection, [project_id] if project_id else connection.execute(text(
            "SELECT p.id FROM projects p WHERE EXISTS (SELECT 1 FROM kg_nodes n WHERE n.project_id = p.id "
            "AND n.kind = 'symbol' AND n.valid_to IS NULL) OR EXISTS (SELECT 1 FROM kg_view_sync s "
            "WHERE s.project_id = p.id)")).scalars().all())
    for pid in projects:
        try:
            with engine.begin() as connection:
                if not is_stale(connection, pid):
                    out["current"] += 1
                    continue
                rebuild(connection, pid)
            out["rebuilt"] += 1
        except Exception:  # noqa: BLE001 - one project never blocks the others
            out["failed"] += 1
    return out
