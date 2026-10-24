"""View rollups for the Graph area (KNOWLEDGE_GRAPH_PRODUCT_SPEC.md §2.4).

``kg_view_tree``, ``kg_view_edges`` and ``kg_view_sync`` are derived and rebuildable: the
overview and neighbourhood routes read them instead of aggregating 234k edges live (measured on
Fineract: 523 ms ad hoc for the area level, 601 ms to build the whole class level).

- Area: the first path segment of the file (the Gradle/Maven module, e.g. ``fineract-provider``).
  If one segment holds more than 80% of the symbols, the first segment below the longest common
  prefix instead.
- Package: ``attrs.module``, falling back to the directory. Its key carries its area,
  ``pkg:<repo>:<area>:<package>``: on Fineract 189 of 1,151 packages span more than one area, so
  ``pkg:<repo>:<package>`` would collide in ``kg_view_tree``'s primary key.
- A method's owning class is its node_key minus the last ``.member``.
- Edges at levels area, package and class; kinds ``calls`` (calls + dispatches_to), ``imports``,
  ``extends`` (extends + implements). Rules and Learning are not rolled up: they are read live.

Runs at the end of a successful repository index job and from the worker's reconcile when
``kg_view_sync.nodes`` differs from the live code count. Target under 3 s on Fineract.

Phase 0 freezes the signature; stream S1 implements it and owns the schema block.
"""
from __future__ import annotations

from typing import Any

from sqlalchemy.engine import Connection


def rebuild(connection: Connection, project_id: str) -> dict[str, Any]:
    """Delete and re-insert the project's rollup rows in one transaction.

    Returns the ``kg_view_sync`` row it wrote: ``{project_id, built_at, nodes, edges, ms}``.
    """
    raise NotImplementedError("view_rollup.rebuild: stream S1 (BOSSAPP-768)")
