# BOSSAPP-622 status

The knowledge plane in this tree is the native path from the Phase 0 spike.
Graphiti was not adopted. `kg_edges` stays the system of record. Traversal is
a recursive CTE. Embeddings are `double precision[]`.

Shipped in code, with tests:

- store writes, tenancy, temporal close, hybrid rank, capture replay
- Python AST index and declaration scans for TypeScript, JavaScript, Go, Java, and C#
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

Checked outside the unit suite: `boss-postgres-kg:16` built from
`infra/postgres/Dockerfile`. A one-row Alpine database was dumped and restored
into it, the row read back, and `CREATE EXTENSION vector` succeeded. Apache AGE
is not in that image.

Not run, so the release ship gate is open:

- Apache AGE 1.8 install and the 1M-node benchmark
- 200-episode extraction and the 50-fact precision comparison
- Saleor full index and the 30-symbol blast-radius check
- case-study smoke and the primary criteria on both engines
- fact precision on 100 labelled facts
- retrieval p95 at the scale target
- a customer-shaped Confluence, Jira, and Slack pilot

`ship_gate` on `GET /v1/knowledge/status` stays `case_study_primary_not_run`.
No model credit was spent. No solve count or dollar figure is recorded.
