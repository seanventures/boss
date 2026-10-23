# BOSSAPP-622 status

> **Reconciled 2026-10-22** with the earlier stack (PRs #11–#14). See
> [KNOWLEDGE_GRAPH_RECONCILE.md](KNOWLEDGE_GRAPH_RECONCILE.md) for what came from each and what
> was decided. Since then:
> - the code graph resolves across files: Saleor 29/30 and Medusa 14/15 on the blind keys, and a
>   full Saleor index in 13.5 s;
> - the database image is Postgres 16 Alpine with pgvector and AGE by default, upgraded by image
>   swap;
> - retrieval fuses text, graph and vector results;
> - each project has an AGE graph with read-only Cypher;
> - stage knowledge scopes, event-driven ingest and durable change links are in.
>
> Lines below that say otherwise describe PR #15 as opened.

The knowledge plane in this tree is the native path from the Phase 0 spike.
Graphiti was not adopted. `kg_edges` stays the system of record. Traversal is
a recursive CTE. Embeddings are `double precision[]`.

Shipped in code, with tests:

- store writes, tenancy, temporal close, hybrid rank, capture replay
- Python AST index, and tree-sitter for TypeScript, JavaScript, Go, Java, and C# when those grammars import (the declaration scan remains the fallback)
- Archivist distillation through an injected extractor, scrubbing, trust tiers
- retrieval packs, passport ceiling, precedent text on the typed-decision prompt
- a typed decision asks for `cited_fact_ids` when precedent is present, and the step output records them
- workflow decisions load that precedent, and a finished or deadlettered run attributes the facts it was shown
- lifecycle capture links a pull request, decision, or release to symbols in the same project
- `knowledge.read` for every role and `knowledge.propose` for builder and reviewer roles, with executors
- learning weights, decay, promotion candidates
- HTTP routes under `/v1/knowledge` and the `kg_*` tools
- a console explorer, settings, and "Based on" when a decision cites facts
- a pull-request proposal indexes the files it wrote, links those symbols, and cites matching facts on the approval card
- an agent turn starts with a budgeted quoted pack, and a completed turn counts the facts it was shown
- ADR folder ingest and connector documents that do not open a network
- a case-study harness that exits before any model call
- an opt-in Debian image, `infra/postgres/Dockerfile`, with pgvector and Apache AGE 1.6.0. That is the newest stable AGE release published for PostgreSQL 16. AGE 1.7.0 is published for PostgreSQL 18. AGE 1.8.0 is not published for PostgreSQL 16. Compose still uses `postgres:16-alpine` and `postgres:16`
- an HNSW side table `kg_vec_<dimensions>` when the vector extension is installed. Alpine keeps `vector_index` at `not_created`. Search uses the index when iterative scans are available, and exact cosine otherwise
- a line extractor scored against 100 labelled facts. That score is not a model precision

Checked outside the unit suite on 2026-10-21:

- `boss-postgres-kg:16` built. `shared_preload_libraries` is `age`. A one-row Alpine database was dumped and restored into it. The row read back as `alpine-row`. `vector` is 0.8.7 and `age` is 1.6.0. The upgrade method was dump/restore.
- On that image, AGE neighbors of a three-node chain matched `traverse()` on the recursive CTE. `traverse()` still refuses a cypher pattern. No 1M-node benchmark was run.
- `EXPLAIN` of the vector query uses `Index Scan using kg_vec_8_hnsw`.
- Saleor `5acd08ffe7389d7323344dd3e1398d8a46367919` (https://github.com/saleor/saleor). The Python AST parser walked 4380 Python files (28,220,975 bytes) in 4.631 seconds and recorded 28,231 symbols. Eleven files use PEP 695 syntax (`def name[T](...)`), which Python 3.11 does not parse, so those files were skipped. A reparse of `saleor/plugins/avatax/tests/test_avatax.py` (224,633 bytes) took 0.036543 seconds. Thirty declarations read from `saleor/checkout/models.py`, `saleor/order/models.py`, and `saleor/product/models.py` were all present in the parse (30/30). The names are in `docs/product/knowledge-saleor-handcheck.json`. A full `index_files` write of that tree did not finish: after about twelve minutes the writer was blocked and Postgres was idle in the same transaction, with a few hundred nodes inserted. No database index time is claimed. There is no published 30-symbol blast-radius gold set, so call-graph blast radius was not scored.
- The line extractor on the 100-text conformance set: 100 labelled facts, 80 true positives, 0 false positives, 20 false negatives, precision 1.0, recall 0.8. `model_precision` is null. No model credit was spent. One hundred scrubbed captures kept none of the planted `AKIAIOSFODNN7EXAMPLE`, `sk-abcdefghijklmnopqrstuvwxyz`, `ada@example.com`, or bearer token.
- Retrieval on the opt-in image, nearest-rank p95 of 40 queries, after loading 20,000 fact nodes, 20,000 edges, and 20,000 embeddings: text 4.001 ms, exact cosine 9.028 ms, HNSW 6.846 ms, hybrid 11.017 ms, traverse 1.393 ms. `scale_target_met` is false. The scale target is 10M nodes, 50M edges, 5M facts, and hybrid p95 under 300 ms. This run is not that target.

Not run, so the release ship gate is open:

- the 1M-node AGE benchmark
- 200-episode extraction and the 50-fact model comparison
- case-study smoke and the primary criteria on both engines
- model precision on 100 labelled facts
- retrieval p95 at 10M nodes, 50M edges, and 5M facts
- a finished database write of the Saleor tree
- a customer-shaped Confluence, Jira, and Slack pilot

`ship_gate` on `GET /v1/knowledge/graph-status` stays `case_study_primary_not_run`.
No model credit was spent. No solve count or dollar figure is recorded.

The tree-sitter parser, the AGE image, the HNSW side table, the Saleor
measurement, the line-extractor score, and the retrieval sample are in this
branch.
