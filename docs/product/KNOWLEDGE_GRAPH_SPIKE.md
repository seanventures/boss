# Knowledge graph Phase 0 spike record

BOSSAPP-622. This is the decision record for the open Graphiti question.
It is not a claim that the 200-episode extraction was run.

Status: provisional
Recommendation: native (B)
Basis: governance
Blockers: no_age_pgvector_driver, model_calls_not_through_gateway, tenancy_not_enforced_in_store

Traversal system of record: kg_edges
Traversal projection: recursive_cte
AGE benchmark: not_measured

Extraction precision, latency, and cost were not measured.

A one-row Alpine to Debian dump/restore passed, and CREATE EXTENSION vector
succeeded on boss-postgres-kg:16. Apache AGE was not installed. Not run:
the 200-episode extraction, the 1M-node traversal benchmark, and the
case-study smoke stage. The harness refuses to spend. No model credit was
spent. HNSW is not created yet. Apache AGE is not the system of record;
kg_edges is.
