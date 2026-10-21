"""BOSS knowledge infrastructure (docs/product/KNOWLEDGE_GRAPH_SPEC.md §5).

Phase 0 holds the storage interface; capture, code index, Archivist, retrieval and learning
modules join this package in later phases.
"""
from .store import (  # noqa: F401
    EDGE_KINDS,
    NODE_KINDS,
    Hit,
    KnowledgeStore,
    NotImplementedInPhase,
    Pattern,
    PostgresKnowledgeStore,
    Scope,
    reciprocal_rank_fusion,
)
