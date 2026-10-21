# Knowledge Graph Phase 0: spike report

Companion to [KNOWLEDGE_GRAPH_SPEC.md](KNOWLEDGE_GRAPH_SPEC.md) §3.2 and §9. The spec leaves two
decisions open for Phase 0: Graphiti (A) or a native build (B), and AGE as the system of record
or a projection. This report answers both with numbers. Every number can be reproduced with the
scripts named in each section.

## Recommendations

1. **Option B (native).** It uses about 14× fewer model tokens per episode, which is 7.6× cheaper
   on Sonnet 5 and 12× cheaper on qwen3-32b. It makes one model call instead of two or more. It handles supersession equally well and is about 114 lines of BOSS code
   on the KnowledgeStore. Option A needs a Postgres/AGE driver for Graphiti (about 3,000–3,500
   lines, judged by its existing drivers) plus retrofits for governance. Precision is
   **pending**: no model key was available, see §1.4.
2. **`kg_nodes`/`kg_edges` stay the system of record, and traversal uses recursive CTEs.** At
   1M nodes and 5M edges, the CTE answers 2–4 hop questions in milliseconds. AGE's
   variable-length MATCH takes seconds to tens of seconds (§2). Keep AGE installed and
   optional, as a projection for ad-hoc Cypher. It goes on no hot path.
3. **Operator decision: the AGE version.** AGE 1.8.x does not exist for PostgreSQL 16. The
   newest PG16 release is 1.6.0, which is what the image ships. 1.8.0 exists only for PG 15,
   18 and 19 (§3).

## 1. Graphiti spike: A vs B

**Setup** (`scripts/knowledge/spike/`):
- **Corpus.** 200 NorthPeak run-record episodes, built from 50 hand-labelled facts (`corpus.py`).
  - Every fact-bearing sentence states one label. Every other sentence (timings, costs, hosts,
    chatter) is non-durable, so extracting it counts as an error.
  - Four facts supersede earlier ones: TTL 15 → 10 min, runbook owner, webhook signing, alert
    threshold.
- **Model.** Both options call the same metered **scripted conformance engine** (`oracle.py`).
  It has the BOSS chat-adapter interface that `model_routing.governed(...)` wraps, so it
  measures calls, tokens and latency structure exactly as the gateway would meter them.
  - Simulated model latency is 0.25 s per call plus 5 ms per output token.
  - Prices come from `openai_adapter.PRICE_PER_MTOK`: Sonnet 5 at $2/$10 per Mtok, qwen3-32b at
    $0.29/$0.59.
  - Embeddings are priced at $0.02/Mtok (an assumption).
- **Option B** (`option_b.py`): one extraction call per episode, schema-validated, then
  exact-key resolution. A fact with the same (subject, predicate) supersedes the old one by
  closing its validity. Writes go through `PostgresKnowledgeStore`.
- **Option A** (`option_a.py`): graphiti-core 0.31.2 with a `GatewayLLMClient` and a
  `GatewayEmbedder` that route every call to the BOSS adapter. It ingests into Neo4j because
  Graphiti has no Postgres driver.

### 1.1 Results (200 episodes)

| | **B native** | **A Graphiti** | A ÷ B |
|---|---|---|---|
| Model calls per episode | **1.00** | 2.39 (entities 1, edges 1, node dedupe 0.15, edge dedupe 0.24) | 2.4× |
| Input tokens per episode | **241** | 4,800 | 19.9× |
| Output tokens per episode | **107** | 221 | 2.1× |
| Embedding inputs per episode | **0** (facts are embedded later, cached by hash) | 11.2 | — |
| Cost per 1,000 episodes, Sonnet 5 | **$1.55** | $11.81 | 7.6× |
| Cost per 1,000 episodes, qwen3-32b | **$0.13** | $1.52 | 11.7× |
| Latency per episode, mean / p95 (simulated model) | **0.79 s / 1.13 s** | 1.87 s / 2.69 s | 2.4× |
| Supersessions handled (old closed, new current) | 4/4 | 4/4 | — |
| Labels recovered (harness check, not precision) | 50/50 | 50/50 | — |
| **Extraction precision on the 50 labels** | **PENDING** | **PENDING** | — |

Graphiti's dedupe calls happen only when its search finds candidates. The spike's stand-in
embeddings carry no meaning, so candidate retrieval relied on full text. With real embeddings
Graphiti finds more candidates and makes more dedupe calls, so A's call and token counts are a
**lower bound**.

### 1.2 Code size and maintenance

| | B native | A Graphiti |
|---|---|---|
| BOSS code written for the spike | 114 lines (extract, validate, resolve, store) | 88 lines of glue (gateway clients) |
| Storage driver | none: the KnowledgeStore (Phase 0) | a Postgres/AGE + pgvector driver for Graphiti: each existing backend is 3,018–3,484 lines (neo4j 3,018, falkordb 3,484, kuzu 3,169, neptune 3,223) |
| Backend-specific branches outside the driver | 0 | 119 `GraphProvider` branches in 13 core files. Its search uses Neo4j full-text (`db.index.fulltext`, 12 index definitions) and Cypher-side vector similarity, neither of which AGE supports. Both would drop to SQL |
| Dependency | none new | graphiti-core, about 40,900 lines, moving fast (0.31.x) |

### 1.3 Governance findings (they decide this as much as the numbers)

- **Concurrency against the gateway.** Graphiti issues model calls concurrently. A governed BOSS
  adapter is bound to one SQLAlchemy connection, which carries the budget reservation and the
  receipt. A must either serialise Graphiti's calls (losing its concurrency) or open a
  connection and reservation per call.
- **Telemetry on by default.** graphiti-core sends PostHog analytics unless
  `GRAPHITI_TELEMETRY_ENABLED=false`. The spike caught it on its first run. That is data
  leaving the customer boundary, which the spec rules out (§2 non-goals). A would need a
  guard that this stays off.
- **Tenancy and Passport scope.** Graphiti scopes by `group_id`. BOSS's org/project/team scope
  and Passport ceilings would be enforced around Graphiti, not inside the store as spec §3.8
  requires. B enforces them in `PostgresKnowledgeStore`.
- **Fragility.** Graphiti's deprecated embedded Kuzu driver fails in 0.31.2 (a missing
  attribute, then a missing full-text index). The spike moved A to Neo4j.
- **No governed embeddings path exists in BOSS yet.** Both options need one. It is the first
  Phase 2/3 gateway task either way.

What B takes from Graphiti, with Apache-2.0 attribution: the bi-temporal fact model (valid
time vs recorded time), episode provenance, supersession instead of deletion, and its
resolution prompts as a starting point for Phase 2's model-judged resolution.

### 1.4 Precision: pending, and what it costs to finish

No model key was available to this session. The brief allows only the spike's small
extraction set as paid work, and without a key the scripted engine stands in. An oracle is
right by construction, so it says nothing about precision.

To finish, rerun `run_spike.py` with the oracle replaced by `model_routing.governed(...)` on a
real engine, both options, 200 episodes:
- B costs about $0.31 on Sonnet 5;
- A costs at least $2.36;
- a model judge over both options' outputs costs about $1.

**About $5 in total.** The scorer is ready: token-overlap matching to the 50 labels, with any
non-durable "fact" counted as an error. The gate is Phase 2's ≥ 85%. B stays the
recommendation unless A beats it on precision by enough to justify about 8× the cost and a
driver.

Reproduce:

```sh
GRAPHITI_TELEMETRY_ENABLED=false python scripts/knowledge/spike/run_spike.py \
  --pg postgresql+psycopg://USER:PASS@127.0.0.1:5432/kgspike \
  --neo4j bolt://127.0.0.1:7687 --neo4j-password <throwaway> --out spike_results.json
```

The run above is `scripts/knowledge/spike/results/spike_results.json`.

## 2. AGE vs recursive CTE at 1M nodes / 5M edges

**Setup** (`scripts/knowledge/bench_traversal.py`):
- 1,000,000 nodes and 5,000,000 `calls` edges, synthetic, with text ids shaped like `kg_nodes`
  and `kg_edges`.
- Out-degree is 5. Targets are skewed (`dst = N · U³`), so a few nodes are heavy hubs, as
  utility modules are in code graphs.
- The same graph is projected into AGE with its bulk loaders, plus GIN and B-tree indexes on
  vertex properties and edge endpoints.
- Three methods, each asked the same question: how many distinct nodes are reachable within k
  hops?
  - **CTE:** the store's own recursive CTE (`PostgresKnowledgeStore.traverse`), project- and
    validity-filtered on every hop.
  - **AGE VLE:** `MATCH (a)-[:E*1..k]->(b) RETURN DISTINCT b`.
  - **AGE fixed:** one fixed-length pattern per hop count, combined with UNION.
- 20 seeds per series, a 30 s timeout per query, on the BOSS DB image (PG 16.10, AGE 1.6.0),
  4 vCPU and 15 GB RAM, `shared_buffers` 1 GB.
- **Every AGE answer matched the CTE answer** (0 mismatches), so no method wins by being wrong.

Latency is p50 / p95 in milliseconds.

| Question | Hops | Nodes reached (mean / max) | **CTE** | AGE fixed patterns | AGE variable-length | AGE VLE timeouts (≥ 30 s) |
|---|---|---|---|---|---|---|
| Dependencies (out) | 2 | 30 / 30 | **1.4 / 1.8** | 2.4 / 3.4 | 4,597 / 4,748 | 0 of 20 |
| Dependencies (out) | 3 | 153 / 155 | **2.9 / 3.6** | 82 / 124 | 23,294 / 23,807 | 1 of 20 |
| Dependencies (out) | 4 | 758 / 772 | **8.1 / 11.3** | 520 / 588 | — | **20 of 20** |
| Blast radius (in) | 2 | 70 / 241 | **1.9 / 4.5** | 3.1 / 9.1 | 6,161 / 14,984 | 2 of 20 |
| Blast radius (in) | 3 | 336 / 1,251 | **4.5 / 10.5** | 12 / 19 | 17,875 / 27,285 | 12 of 20 |

- **The recursive CTE wins everywhere.** It is 1.6–64× faster than AGE's best form (fixed patterns)
  and 3,000× or more faster than variable-length MATCH. Its 4-hop p95 is 11 ms, well inside the
  §3.8 retrieval budget of p95 < 300 ms. It also applies the validity window and project
  filter on every hop, which the AGE queries here do not.
- **AGE's variable-length MATCH is not usable at this scale.** It enumerates paths, not nodes.
  The first call in a session also builds AGE's in-memory graph context, which took 16 s here.
  Fixed patterns are reasonable at 2 hops, but the gap grows with depth: 64× at 4 hops.
- **Storage:** relational tables and indexes take 838 MB; the AGE projection takes 716 MB. A
  projection roughly doubles graph storage.
- **Decision:** `kg_nodes`/`kg_edges` stay the system of record, and `traverse` stays on the
  CTE. AGE stays in the image as an optional projection for ad-hoc Cypher (console exploration,
  analyst queries). Product hot paths never depend on it. This also keeps the provenance and
  temporal fields in one place, with no sync job between two stores.

Reproduce:

```sh
python3 scripts/knowledge/bench_traversal.py --dsn postgresql://USER:PASS@127.0.0.1:5432/kgbench --seeds 20 --timeout 30
```

The run above is `scripts/knowledge/results/bench_1m.json`.

## 3. Database image and upgrade (Phase 0 item 1, for the record)

- **Image:** PostgreSQL 16 on Debian trixie (glibc 2.41), with pgvector 0.8.1 and AGE 1.6.0
  (`scripts/install/db-image/`).
  - The extensions are copied from their projects' own Debian images, pinned by digest, so the
    build needs no compiler and no apt.
  - Moving to another AGE release changes one `ARG`.
- **AGE version.** AGE publishes per-PostgreSQL branches:

  | PostgreSQL | Newest AGE |
  |---|---|
  | 15 | 1.8.0-rc0 |
  | **16** | **1.6.0** (no 1.7 or 1.8 tag) |
  | 17 | 1.7.0 |
  | 18 | 1.8.0 |
  | 19 | 1.8.0 |

  The spec's "1.8.x for PG16" does not exist. Getting 1.8 means PostgreSQL 18. That is a major
  upgrade, but the dump/restore path below already performs one, so it costs little extra.
  **Operator decision.**
- **Collation.** The hazard is real. Opening the Alpine (musl) data directory under glibc made
  `amcheck` report "high key invariant violated" on a text unique index
  (`test_opening_an_alpine_directory_under_glibc_corrupts_text_indexes`).
- **Upgrade path.** The upgrade never reopens the old directory:
  1. `install.sh` takes its recovery backup.
  2. `db_image_migrate.py` dumps every role and database with the image that wrote them.
  3. It restores them into a new volume that the Debian image initialises.
  4. It proves the copy: equal row counts per table, equal index counts, `amcheck` on every
     B-tree.
  5. The old volume stays unchanged, so a rollback still works. A half-finished or stale copy
     is refused, not guessed.
- **Guard.** The image refuses a data directory without its glibc marker.
- **Test.** The real-Docker test covers the populated upgrade (2 databases, 23,500 rows, roles
  and passwords, extensions afterwards).
