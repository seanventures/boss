# BOSS Knowledge Graph: product and engineering spec

Status: draft for operator approval · Owner: operator · Epic: see Plane (label `knowledge-graph`)
Companion: [KNOWLEDGE_GRAPH_CASE_STUDY.md](KNOWLEDGE_GRAPH_CASE_STUDY.md) (the proof plan).

## 0. Read this first (for the implementing agent)

- **Build on what exists.** BOSS already has three pieces of this. Extend them; do not create parallel systems.
  - `engine/boss_engine/services/knowledge_graph.py`, with the tables `kg_nodes`, `kg_edges` and `kg_eval_snapshots` (`db/schema.sql` ~4818):
    - an SRE topology graph;
    - provenance source, temporal validity (`valid_from`/`valid_to`), confidence, a strict project scope;
    - `record_root_cause`/`known_causes`, and a retrieval-coverage evaluation.
  - `engine/boss_engine/services/agent_memory.py`, with the tables `agent_memory` and `agent_session_ledger`:
    - lessons arrive `proposed` and bind nothing until reviewed;
    - `pack_for_turn` injects them under a hard character budget;
    - `prune`, `forget`.
  - `engine/boss_engine/services/knowledge_context.py`: context packing with secret redaction.
- **Every model call is governed.** Embeddings and extraction calls go through the model gateway and the budget (`model_gateway.py`, `budget.py`, `receipts.py`, `llm_telemetry.py`), so they are reserved, receipted and routed. Never call a provider SDK directly.
- **Model-agnostic.** Behaviour must be identical on Claude Code, Codex, Cursor, native, and OpenAI-compatible or Ollama engines. Engines without tool use get the same knowledge as an injected context pack.
- **Agents reason; no keyword rules.** Extraction, resolution and retrieval ranking may use a model. Keywords are only hints.
- **Fix classes, with tests.** Targeted tests per change. No real host addresses in tracked files: use `203.0.113.x` and `example.com`. The P0 test `test_the_public_tree_names_no_private_infrastructure` must pass.
- **One database.** Postgres stays the only database (the operator's decision, 2026-10-20). The graph uses Apache AGE and vectors use pgvector, both inside the existing Postgres.

## 1. Why

BOSS will be adopted by enterprises on real codebases (monoliths, many repositories, legacy migrations), not only small greenfield apps. On such codebases:
- agents spend turns rediscovering structure;
- they repeat mistakes a teammate or an earlier run already made;
- they cannot see *why* code is the way it is.

Coding-agent vendors attack this with code graphs. LatentGraph (closed, hosted) reports, on its own tests:
- 50% → 70% of 20 tasks solved;
- 19.9 → 14.0 min per task;
- 5.31M → 2.46M tokens;
- on 14 SWE-bench Pro tasks, $3.93 → $1.33 per resolved task.

Its most valuable layer is "intent mined from PR history".

BOSS has an advantage nobody else has: **it writes that history itself**. Every BOSS run produces:
- the spec and the panel's minutes;
- the person's sign-off and its reason;
- reviewers' findings (Rook's revert-proven root causes, Sentry's advisories);
- the release card;
- incidents with root causes, runbooks and alert rules;
- receipts with costs.

Linking those governed records to the code they touched, and distilling agents' work into curated, temporal facts, gives a **knowledge infrastructure that grows from the work itself**. Every later agent, on any model, and the decision model (Jev), then works from precedent.

## 2. Goals and non-goals

### Goals
1. **G1 Code graph.** Symbols, calls, imports, modules and services for every repository BOSS builds on, multi-repo, updated incrementally on every commit BOSS makes. Large repos are first-class: target 1M+ lines across 10+ repos per org.
2. **G2 Lifecycle graph.** BOSS's own records become nodes, linked to code nodes and to each other: spec, decision, PR, review finding, release, incident, root cause, runbook, alert rule, receipt.
3. **G3 Learned knowledge.** An Archivist pipeline distils agent work into atomic, sourced, temporal facts and lessons, with dedupe and contradiction handling. It never dumps transcripts.
4. **G4 Governed trust.** Trust tiers, human approval for binding knowledge, scope by org/project/team, Passport ceilings on reads, secret and PII scrubbing, retention, right to forget, full audit.
5. **G5 Retrieval for every engine.** Hybrid retrieval (graph traversal + vectors + full-text), assembled into budgeted role/stage context packs, served as MCP tools and as injected packs.
6. **G6 Decisions use precedent.** Jev and the agents' decision step cite the facts they used.
7. **G7 Learning loop.** Outcomes (gates, reviews, incidents, cost) feed back into fact weights. Unhelpful facts decay.
8. **G8 Proven.** The case study (companion doc) shows a measured gain before general release.

### Non-goals (v1)
- A hosted service, or any dependency that sends code or graph data outside the customer's boundary.
- Training model weights on customer content.
- A second database (Neo4j and others are out for now; keep a storage interface so an adapter can be added later).
- A general enterprise search product. Knowledge serves BOSS's agents and people *inside BOSS*.

## 3. Architecture

```
Sources ──► Capture (events) ──► Archivist pipeline ──► Store ──► Retrieval ──► Consumers
 code (tree-sitter)            distil · resolve ·        Postgres:              agents (MCP tools
 BOSS lifecycle records        validate · embed          · AGE graph            + injected packs),
 agent turns (distilled)                                 · pgvector             Jev, reviewers,
 docs via MCP connectors                                 · tsvector (FTS)       release card, console
 ops signals                                             · relational facts
                         ▲                                                          │
                         └──────────── Learning loop (outcome attribution) ◄────────┘
```

### 3.1 Storage (one Postgres)

- **Image change (risk: high, do first).** The current DB image is `postgres:16` on **Alpine (musl)**, and neither `vector` nor `age` is available (`pg_available_extensions` is empty for both, checked 2026-10-20).
  - Build a BOSS DB image from the official Debian `postgres:16` with **pgvector** and **Apache AGE 1.6.0**. AGE ships per-PostgreSQL branches: 1.6.0 is the newest release for PG16, and 1.8.x exists only for PG15/18/19. **Decided (operator, 2026-10-21): stay on PostgreSQL 16 with AGE 1.6.0; do not move to PostgreSQL 18.**
  - **Collation hazard.** musl and glibc sort text differently, so moving an existing data directory from Alpine to Debian can silently corrupt text indexes. The upgrade must **dump and restore**, or `REINDEX` every text index, under `boss upgrade`'s protected backup. Add a test that proves the upgrade path on a populated database.
  - Fresh installs get the new image directly.
- **Graph: AGE graph `boss_kg`.** Labels map to node kinds (§4).
  - **Decided (Phase 0 benchmark, operator 2026-10-21):** `kg_nodes`/`kg_edges` are the **system of record**, and **traversal uses the recursive CTE** on them (`PostgresKnowledgeStore.traverse`). At 1M nodes and 5M edges the CTE answers 2–4 hops in 1–11 ms at p95. AGE's variable-length MATCH takes seconds or times out ([spike report](KNOWLEDGE_GRAPH_SPIKE.md) §2). **AGE stays installed only for ad-hoc Cypher** (console exploration, analyst queries); no product path depends on it. `KnowledgeGraphService`'s public API keeps working.
  - Known AGE limits to design around:
    - it is an openCypher subset;
    - it has no native vector or full-text index, so pair it with pgvector and `tsvector` columns;
    - non-autocommit clients must commit after creating a graph or label.
- **Vectors.** A `kg_embeddings` table (node or fact id, model id, dimension, vector, content hash) with an HNSW index per (model, dimension). The embedding model is per org and recorded on every row, so switching models re-embeds in the background without mixing vector spaces.
- **Full text.** A `tsvector` column on fact and node text, with a GIN index.
- **Storage interface.** `KnowledgeStore` (Python protocol): `upsert_node`, `upsert_edge`, `close_validity`, `traverse(cypher|pattern, scope)`, `vector_search`, `text_search`, `hybrid_search`. The Postgres implementation is the only one in v1.

### 3.2 Graphiti: what to take

[Graphiti](https://github.com/getzep/graphiti) (Apache-2.0, ~32k stars, active) is an *ingestion and memory framework*, not an embedding model.
- **What it does:** an LLM extracts entities and relations from text "episodes" into a **bi-temporal** graph (when a fact was true, and when the system learned it). It resolves duplicates, invalidates superseded facts, and does hybrid search (semantic + BM25 + graph).
- **Providers:** OpenAI, Azure OpenAI, Anthropic, Gemini, and OpenAI-compatible endpoints (Ollama, vLLM).
- **Backends:** Neo4j, FalkorDB, Neptune, Kuzu (deprecated). **No Postgres or AGE driver.** It has a driver abstraction (`graphiti_core/driver/`: `driver.py`, `graph_operations`, `search_interface`).

**Phase 0 compared:**
- **(A) Adopt Graphiti with a BOSS-written AGE + pgvector driver,** and route its LLM and embedding clients through BOSS's model gateway (custom client classes) so every call is budgeted and receipted. Contribute the driver upstream.
- **(B) Native implementation** that reuses Graphiti's *data model and extraction approach* (bi-temporal edges, episode provenance, resolution prompts, with Apache-2.0 attribution) inside BOSS services.

**Decided: (B) native** (operator, 2026-10-21, on the [spike report](KNOWLEDGE_GRAPH_SPIKE.md)). B costs 7.6× less per episode on Sonnet 5, makes one model call instead of two or more, and is about 114 lines on the KnowledgeStore; A needs a 3,000-line driver and governance retrofits. graphiti-core is **not** a BOSS dependency, and its telemetry (PostHog, on by default) is never enabled anywhere. Extraction precision was left pending in Phase 0 (no model key). The governed precision run on the spike's 200 episodes and 50 labels (about $5) is **Phase 2's acceptance test**.

### 3.3 Capture

- **Event-sourced and idempotent.** Each source emits `knowledge.captured` events to a queue table, keyed by `(source_kind, source_id, content_hash)`. Replays converge.
- **Sources, v1:**
  1. **Code.** Tree-sitter parsers for Python, TypeScript/JavaScript, Go, Java and C#; others later. Run on every commit BOSS pushes and on a nightly full reconcile per repository.
  2. **Lifecycle.** Workflow runs and steps, specs and PRDs, meeting minutes, decisions (with reason and actor), PRs and diffs (`github_adapter.py`), review findings, release cards, incidents, root causes (`knowledge_graph.record_root_cause`), runbooks, alert rules, receipts (cost only).
  3. **Agent turns.** Distilled only (§3.4). Raw turns stay where they are today.
  4. **Docs via MCP connectors** (Phase 5): Confluence, Jira, Slack, ADR folders in repositories.
  5. **Ops signals.** Alert firings and recoveries (`app_alerts`), deploys.
- **Scrubbing before anything is stored or embedded.** Secrets use `knowledge_context._redact_secrets`, extended. PII uses a configurable detector (emails, phone numbers, card-like numbers). Scrubbed spans are counted, never logged.

### 3.4 Archivist pipeline (distil → resolve → validate → embed)

- **The Archivist role.** A new role agent (`archivist`) with its own Passport: read-only on sources, write-only to the knowledge store. Add an avatar in `apps/web/lib/avatar-kit/cast.ts` and regenerate `docs/cast`.
- **Distil.** Per finished run, incident or review, one budgeted extraction call produces **atomic facts**:
  `{subject, predicate, object, statement, kind, evidence:[source refs], valid_from, confidence}`.
  Kinds:
  - `invariant`: "size_band() treats 15+ items as large";
  - `decision`: "auth changes need security sign-off; signed by X on D";
  - `lesson`: "running the frontend checks first catches 80% of review rounds";
  - `pattern`, `coupling` (implicit runtime links: queues, shared tables, channels), `ownership`, `risk`.
- **Resolve.** Entity resolution maps mentions to code nodes and existing entities, using exact symbol match first, then embeddings plus a model judgement. Duplicates merge. A contradiction **closes** the older fact's validity (`valid_to`) and links `superseded_by`; it never deletes.
- **Trust tiers:**

  | Tier | Origin | Weight | Can bind agents? |
  |---|---|---|---|
  | `observed` | code, tests, metrics, CI | high | yes, as context |
  | `governed` | a person's decision or sign-off in BOSS | highest | yes |
  | `inferred` | Archivist or agent distillation | low, quarantined until it is used successfully or confirmed | no |
  | `approved` | an `inferred` fact a person promoted (an existing `agent_memory` review) | high | yes |

  An `inferred` fact can never override `governed` or `approved` ones. **Poisoning defence:** an agent cannot write `approved` or `governed`, and the tier is set by the pipeline, not the payload.
- **Embed.** Only distilled facts and node summaries are embedded, never raw logs. Embeddings are cached by content hash.

### 3.5 Retrieval

- **Hybrid.** Candidate generation from:
  - graph neighbourhood of the task's touched symbols and files (from the plan or diff);
  - vector top-k;
  - full-text top-k.

  Then rank: model-free reciprocal-rank fusion first, with an optional reranker call when the budget allows.
- **Context packs per role and stage.** One builder, `knowledge_pack(role, stage, task, budget_tokens)`:
  - **Design or implement:** the module map around the touched area (a compact, PageRank-ranked repo map in the style of Aider), invariants, couplings, recent decisions.
  - **Review (Rook, Sentry):** invariants, past findings on these files, security decisions.
  - **SRE (Beacon):** service topology, known causes, runbooks, recent incidents.
  - **Jev:** precedent decisions in similar situations, with outcomes.

  The pack respects a hard token budget, as `agent_memory.pack_for_turn` already does for characters, and records which fact ids it included (for §3.7).
- **Two delivery paths, same content:**
  1. **MCP tools on BOSS's MCP server:** `kg_search`, `kg_symbol`, `kg_callers`, `kg_blast_radius`, `kg_why` (facts and decisions behind a symbol or file), `kg_precedent`, `kg_propose_fact` (writes `inferred` only).
  2. **An injected pack** in the stage prompt, for engines without tool use.
- **Scope.** Every read is filtered by org, project and team *and* by the reading agent's Passport ceiling. Facts carry the scope of their most restrictive source.
- **Branch overlay (Phase 3; operator decision 2026-10-21).** The stored code graph describes the default branch only. During a build, queries see the default branch **plus that build's own diff**: an overlay derived from the run's working tree, held only for that run and discarded when it ends. It is never written into the stored graph.

### 3.6 Decisions (Jev and agents)

- The single model-decided step (`intent_router.py`, `typed_decisions.py`) receives a **precedent section**: the top-k similar past situations, what was decided, and the outcome.
- Decisions record `cited_fact_ids`. The console shows "based on" under a decision.
- The battle test (`scripts/agent-battle-test.sh`) gains scenarios where the right answer depends on precedent. The score must not regress, and should rise.

### 3.7 Learning loop

- Every pack and tool response logs `(run_id, step, fact_ids)`.
- When the run ends, outcome signals are attributed:
  - gate pass or fail;
  - review findings that cite the same files;
  - an incident within N days on the touched symbols;
  - cost and turns versus the workflow baseline.
- Fact weight is a Bayesian-smoothed usefulness score with time decay. Facts unused for 180 days with low weight are archived (still queryable, never injected). `agent_memory.prune` semantics are extended to facts.
- `inferred` facts used in successful runs ≥ N times without contradiction become **candidates for promotion**: a card in the Decisions queue, where a person approves or rejects them.

### 3.8 Governance and operations

- **Audit.** Every write, promotion and forget is an event. Each fact shows its sources and history.
- **Retention.** Per org. The default keeps facts until superseded, plus 1 year. Raw source pointers follow the source's own retention.
- **Right to forget.** Forgetting a source removes or redacts every derived fact and embedding (lineage-driven) and logs it.
- **Tenancy.** Strict project and org scope on every query, enforced in the store, not by callers (as `knowledge_graph._require_project` does today).
- **Cost control.**
  - Archivist and embedding calls have their own budget category (`knowledge`), with daily and monthly caps and a per-run ceiling.
  - Ingestion degrades to structure-only (code graph and lifecycle links, no model calls) when the budget refuses.
  - The console shows the cost of knowledge.
- **Scale targets, v1:**
  - 10M nodes, 50M edges, 5M facts per org;
  - p95 retrieval under 300 ms;
  - incremental code update under 30 s per commit on a 1M-line repository;
  - ingest lag under 5 min after a run ends.

  Indexes: AGE label and property indexes, HNSW (pgvector), GIN (tsvector), and B-tree on (project, kind, valid_to).
- **Backups.** Everything is in Postgres, so the existing protected backup and restore cover it. Add a restore test with AGE and vector data.

## 4. Data model

**Node kinds:**
- existing: `service`, `resource`, `repo`, `deployment`, `datastore`, `external`, `incident`, `root_cause`;
- code: `module`, `file`, `symbol` (function, class, method), `endpoint`, `table`, `queue`, `config_key`;
- lifecycle: `spec`, `decision`, `pull_request`, `review_finding`, `release`, `runbook`, `alert_rule`, `run`, `person` (an id reference only), `agent`, `team`;
- knowledge: `fact`, `topic` (community summaries).

**Edge kinds:**
- existing: `depends_on`, `runs_on`, `deployed_from`, `owns`, `emits`, `caused_by`, `correlates_with`;
- code: `defines`, `calls`, `imports`, `reads`, `writes`, `publishes`, `subscribes`, `exposes`, `tests`;
- lifecycle: `specifies`, `implements`, `changed` (PR→symbol), `reviewed`, `found_in`, `shipped`, `fired_on`, `fixed_by`, `decided_by`, `approved_by`, `about` (fact→node), `supersedes`, `evidenced_by`.

**Common fields on every node and edge:**
- `id`, `project_id`, `org_id`, `scope`;
- `source` (provenance ref), `trust_tier`, `confidence`;
- `valid_from`, `valid_to`, `recorded_at`;
- `weight`, `created_by` (agent, person or system), `content_hash`.

## 5. Interfaces

- **Engine.**
  - `services/knowledge/` package: `store.py` (`KnowledgeStore` and the Postgres implementation), `capture.py`, `code_index.py` (tree-sitter), `archivist.py`, `retrieval.py` (packs, hybrid), `learning.py`.
  - `knowledge_graph.py` becomes a facade over the store, with its public API unchanged.
- **API.**
  - `GET /v1/knowledge/search`, `GET /v1/knowledge/nodes/{id}` (with `why`), `GET /v1/knowledge/blast-radius`;
  - `POST /v1/knowledge/facts/{id}/promote|reject|forget`;
  - `GET /v1/knowledge/status` (ingest lag, sizes, cost).

  All are permission-gated (new permissions `knowledge:read` and `knowledge:manage`; `manage` needs step-up).
- **MCP.** The tools in §3.5, registered in `engine/boss_engine/tools/builtin.py` and granted per role via `role_skills` (`knowledge.read` for all roles as a fundamental; `knowledge.propose` for builder and reviewer roles).
- **Console.**
  1. Knowledge explorer (Observability or a new area): search, a node page with "why", history and sources.
  2. "Why" and blast radius on PR, release and decision cards.
  3. A promotion queue in Decisions.
  4. Settings: embedding model, budget, retention, sources.

  Follow the console UX rules: short labels with tooltips, one surface per job.

## 6. Phases and acceptance criteria

| Phase | Scope | Accept when |
|---|---|---|
| **0 · Spike + baseline** (≈3 d) | New DB image (pgvector + AGE) with a tested Alpine→Debian upgrade; the Graphiti A/B spike (§3.2); the `KnowledgeStore` interface; case-study harness and baseline runs (companion doc §4, arm A only) | The image upgrade test passes on a populated DB; the spike report picks A or B with numbers; baseline numbers are recorded |
| **1 · Code + lifecycle graph** (≈1 wk) | Tree-sitter indexer (5 languages), incremental on BOSS commits; lifecycle nodes and edges from existing records; `kg_symbol`, `kg_callers`, `kg_blast_radius`, `kg_why` | On Saleor (companion doc): full index ≤ 15 min, incremental ≤ 30 s per commit; blast radius matches a hand-checked 30-symbol set ≥ 90% |
| **2 · Archivist + trust** (≈1 wk) | Distil, resolve, tiers, scrubbing, retention, forget; Archivist role and avatar | The governed precision run on the Phase 0 spike set (200 episodes, 50 labels, about $5) is recorded; fact precision ≥ 85% on 100 labelled facts; 0 secrets in the store in the leak test; adversarial poisoning test: an inferred fact never outranks a governed one |
| **3 · Retrieval + decisions** (≈1 wk) | Hybrid search, role and stage packs, MCP tools plus injection, Jev precedent with citations; the per-run branch overlay (§3.5); the HTTP API with `knowledge:read` / `knowledge:manage` | p95 under 300 ms at the scale target (synthetic load); battle test does not regress, and precedent scenarios are ≥ 80% |
| **4 · Learning loop + console** (≈1 wk) | Attribution, weights and decay, promotion queue, explorer, "why" on cards | Learning-curve criterion met in the case study (companion doc §5) |
| **5 · Enterprise sources** (≈1 wk) | MCP ingest from Confluence, Jira, Slack and ADR folders; multi-repo cross edges | One customer-shaped pilot repo set ingests end to end |

**Ship gate for release:** the case study's primary criteria (companion doc §5) are met on **both** engines.

## 7. Testing

- **Targeted tests per change,** each failing before the fix:
  - store (scope enforcement, temporal close, idempotent upsert);
  - indexer (fixtures per language);
  - Archivist (golden extraction set, using a scripted conformance engine for determinism);
  - retrieval (budget respected, Passport filter);
  - forget lineage;
  - backup and restore with AGE and vectors;
  - the upgrade path.
- **Security:** a secret-leak scan of the store after ingesting seeded secrets; a cross-tenant read attempt must fail; a poisoning attempt via `kg_propose_fact`.
- **Load:** a synthetic graph at the scale targets, plus a retrieval latency benchmark.

## 8. Risks

| Risk | Mitigation |
|---|---|
| Alpine→Debian collation corruption on upgrade | Dump/restore or REINDEX under protected backup; tested in Phase 0 |
| AGE performance on deep traversals | Benchmark in Phase 0; recursive CTEs over `kg_edges` as a fallback for hot paths |
| Knowledge swamp, stale or wrong facts | Atomic distillation only, resolution, temporal validity, decay, tiers, human promotion |
| Context bloat hurting quality | Hard token budgets per pack; the case study measures quality as well as cost |
| Cost of extraction and embeddings | Its own budget category, content-hash caching, structure-only mode when refused |
| Prompt injection via ingested docs | Ingested text is data: packs label it as quoted source material; tool outputs never carry instructions; Archivist output is schema-validated |

## 9. Decisions recorded

- 2026-10-20 (operator): one database. Postgres with Apache AGE and pgvector; no Neo4j.
- 2026-10-20 (operator): build it ourselves; LatentGraph is not a dependency (closed and hosted).
- 2026-10-21 (operator, on the Phase 0 spike): **native (B)**, not Graphiti. No graphiti-core dependency; its telemetry is never enabled. The precision run moves to Phase 2 as its acceptance test.
- 2026-10-21 (operator, on the Phase 0 benchmark): **PostgreSQL 16 with AGE 1.6.0**; no move to PostgreSQL 18. `kg_nodes`/`kg_edges` are the system of record; traversal uses the recursive CTE; AGE is kept only for ad-hoc Cypher.
- 2026-10-21 (operator): the case-study **smoke stage is not approved yet**. It runs after Phases 1–3, on the open-model engine (E2) first.
