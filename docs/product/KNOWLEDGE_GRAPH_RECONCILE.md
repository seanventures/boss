# BOSSAPP-622: reconciling the two knowledge-graph implementations

Branch `reconcile/knowledge-graph`, from PR #15's head (`f0441372b`, on main `e710eb6df`, v1.0-rc10.9).

The two implementations:

- **A, the base.** PR #15, `ai/grok/bossapp-622-knowledge-port`, one commit. This is the active implementation.
- **B, the earlier stack.** PRs #11–#14, `feat/knowledge-graph-p0` to `p3`. The design is `docs/product/KNOWLEDGE_GRAPH_SPEC.md` on `docs/knowledge-graph`, and the status is `docs/product/KNOWLEDGE_GRAPH_PHASE3.md` on `p3`.

The operator decided that A is the base. What B has and A lacks comes in as small, tested commits, adapted to A's store and API. B's modules are not added beside A's. Every port has a test that fails without it. Where the test exposed a regression that A introduced, that is fixed first.

## The matrix

"A" is PR #15 as opened. "B" is `feat/knowledge-graph-p3`. "Here" is this branch.

| Capability | A (PR #15) | B (PRs #11–#14) | Evidence | Decision |
|---|---|---|---|---|
| Store and schema | One table pair. `kg_nodes` / `kg_edges` carry trust tiers, temporal close and `search_tsv`. `kg_embeddings` is `double precision[]`. `knowledge/store.py`. | Separate tables: `kg_facts`, `kg_fact_sources`, `kg_code_files`, `kg_change_links`, `kg_ingest_*`, `kg_pack_log`. `knowledge/store.py`, `facts.py`. | Both keep `kg_nodes`/`kg_edges` as the system of record, with a recursive CTE. | **Keep A.** B's tables come in only where a port needs them (`kg_change_links`, `kg_ingest_queue`). The code-file cache lives on A's `file` nodes. |
| Code indexer | Python through the stdlib AST, other languages through tree-sitter. Calls resolve **inside one file only**, and symbols are keyed by short name. `knowledge/code_index.py`. | Cross-file resolver with a per-file cache, using tree-sitter for every language including Python. `knowledge/code_parse.py`, `code_index.py`. | Blind answer keys (written before the tool ran; results are in `scripts/knowledge/results/`). On Saleor, **A 3/30** (only the 3 empty keys), **here 29/30**, B 29/30. On Medusa, **A 0/15**, **here 14/15**, B 14/15. A full Saleor write under A had not finished after 12 min. Here, 4,380 files take 13.5 s, or 17.4 s with the AGE projection. | **Port from B** (`04b169b3a`). B's resolver and its tree-sitter extractors are adapted to A's store (bulk upserts) and API (`index_files`, `parse_source`). |
| TypeScript monorepo fixes | None: imports are module names. | Re-exports, `export *` chains, `export * as`, workspace package names with `exports` and dist→src, tsconfig `paths`, namespace types (`HttpTypes.X`), and an npm scope's `@` kept (`code_parse.py`, `JsWorkspace`). | `test_a_typescript_monorepo_resolves_workspace_packages_aliases_and_re_exports`, and Medusa 14/15. | **Port from B**, in the same commit. |
| Python parser: AST vs tree-sitter | AST | tree-sitter-python | Same 30-symbol Saleor key: AST 29/30 (3.1 s to parse), tree-sitter 29/30 (4.8 s), with the same miss. | **Keep A's AST**, rewritten to produce B's facts. tree-sitter-python is not ported. Caveat: the API image runs Python 3.11, so 12 Saleor files with PEP 695 syntax record `error = syntax`. None of them is in the key. |
| Lifecycle graph | Captured when BOSS opens a pull request, plus decision capture. `capture.py`, `runtime.py`. A PR is keyed `pr-<n>`. | A full sync from BOSS records: runs, PRs, decisions, findings, incidents, runbooks, alerts and releases. `lifecycle.py`. | `test_knowledge_pr_ids_db` | **Keep A**, and add event-driven ingest (below). A PR is keyed `pr-<owner>/<repo>#<n>`, and old ids are migrated (`d369d50dd`). B's full record sync is left out (see below). |
| Durable changed-symbol edges | A PR links symbols **by name**, to the first symbol of that name in the project. The link exists only as a `kg_edges` row, which a re-index or a forget removes. | `kg_change_links`. Changed edges are derived from it. | `test_knowledge_change_links_db`. The same-name test fails on A. | **Port from B** (`76cb7672f`): natural keys, re-derived after every index. |
| Archivist: distil, resolve, tiers | An injected extractor. Contradiction closes the older fact (same-predicate rule). The tier is set by the pipeline. `archivist.py`. | A model-backed Archivist with a judge for same and contradicts, structure-only on budget refusal, and 95.5% scripted precision on 105 facts. `archivist.py`, `facts.py`. | A has no model path. Its `SpendingExtractor` raises. | **Keep A.** The fail-closed budget gate is ported (`2fa93fb27`). The model-backed Archivist waits on an approved precision run. |
| Scrubbing | Shared secret patterns, plus email, phone and an unchecked 13–19 digit card number. `scrub.py`. | `sk_live_`/`rk_test_`/`sk-proj-`/`whsec_` keys, GitHub, Google, Slack, JWT, URL passwords and secret assignments; Luhn-checked cards, national ids, IPs. | `test_knowledge_scrub_secrets`: `sk_live_...` passed A's pattern. | **Port from B** (`f0e8ea091`). B's per-org PII settings are not ported (A has no settings row). |
| Forget by lineage, retention | `forget_source` by capture lineage. `retention_deadline` exists, but no sweep calls it. | `kg_fact_sources` lineage, forget with embeddings, a retention purge per org. | — | **Keep A.** A retention sweep is a follow-up. |
| Embeddings / HNSW | `kg_vec_<dim>` side table with HNSW when pgvector is present; exact cosine otherwise. Search was text only. | `kg_embeddings` with a pgvector column and an HNSW index per model, through a governed embedder. | `test_knowledge_hybrid_db` uses the HNSW index on the new image. `test_embedding_model_slot`. | **Keep A's store**. Hybrid search is on by default (`955d22e4d`). The embedding model is its own Setup slot, and every call is governed (`8ca2d6f83`). Both are operator decisions of 2026-10-22. |
| Event-driven ingest | None | A database trigger on run finish and incident resolution, a 30 s drain, and a 10-min fallback sweep. | `test_knowledge_ingest_db` | **Port from B** (`58c2caf12`). It captures structure only, with no model. |
| `kg_propose_fact` grants | A seed pass (`grants.py`) gives `knowledge.propose` to every builder and reviewer role. The tool needs `knowledge:manage`, so no agent could reach it. The seed pass wrote `role_skills` without `seed_exclusions`. | Exactly six roles (pm, engineering_captain, qa, adversarial_reviewer, security_reviewer, sre) through `allowed_skills`. A write bundle. `knowledge.read` is in the baseline. | `test_kg_propose_fact_is_granted_to_exactly_the_six_roles`. The seed-writer, reachability and registry tests fail on A. | **Port from B** (`e62227979`). |
| Per-org budget defaults, and B's default-cap fix | None: A never spends. | `kg_knowledge_settings` with $2/day, $20/month and $0.20/run. B fixed caps that never bit for an org with no settings row. | — | **Port the class, not the table.** A's gate fails closed (`2fa93fb27`). The per-org caps come with a model-backed Archivist. |
| Retrieval, RRF, packs, budgets | Text search, tier-first rank, packs under a token budget, `log_usage`. | Graph, text, vector and trusted lists with RRF; recipes per role and stage; PageRank module map; a reranker gated by budget; `kg_pack_log`. | `test_knowledge_hybrid_db` | **Merge both.** Here: text, graph and vector lists fused by RRF, with A's packs and tier rank. B's recipes, PageRank and reranker are left out. |
| Permissions and Passport-scoped reads | `knowledge:read` and `knowledge:manage`. Presets did **not** hold `knowledge:read`, so delegated kg tools were refused. A tier ceiling on packs. | The same permissions, plus every preset and an archivist preset, `read_permissions` per fact, and team scope. | `test_every_shipped_role_and_any_new_agent_gets_a_preset` fails on A. `test_custom_ceilings_gain_knowledge_read_db`. | **Keep A's permissions** and port B's presets (`e62227979`). An agent with a custom ceiling gains `knowledge:read` once (`d5b52fa3a`). Per-fact `read_permissions` is left out. |
| Branch overlay | None | `overlay.py`: an in-memory overlay per run. It is not wired into any stage (PHASE3 "Next" 1). | — | **Dropped for now** (operator, 2026-10-22). It is unwired in B too, and A indexes a PR's files when it opens. |
| Stage knowledge scopes | None | Designed only (PHASE3 "Next" 5). | `test_knowledge_stage_scopes_db` | **Build it here, on A** (`2c4bc7eeb`): a generated layer column, declared policies, enforcement in the store, tool refusal, and the scoped pack. |
| Jev precedent citations | Wired: typed-decision prompt, `cited_fact_ids`, attribution. | Not done ("Next" 2). | A's tests | **Keep A.** Precedent is read under the `decide` scope. |
| Console: explorer, promotion queue, "based on" | Present. The settings panel and promotion queue read a shadowed route. | None | `test_knowledge_routes_are_reachable` | **Keep A**, and fix the route (`4ab487783`). |
| Precedent on PR cards and agent turns | Present. The PR-card wrapper hid the decision from the producer scan. | Injection into harness stages (`delivery.py`). | `test_every_decision_boss_raises_has_its_buttons_decided` | **Keep A**, with the fix (`3806da09a`). |
| DB image | Opt-in Debian `infra/postgres/Dockerfile`; compose stayed on `postgres:16-alpine`. | Debian by default, with a collation-safe dump/restore migration. | `test_db_image_upgrade_real_docker` | **Decided** (operator, 2026-10-22). See below. |
| Benchmarks | `bench.py`: 20k-node retrieval sample, p95 hybrid 11 ms. | 1M nodes and 5M edges: search p95 58 ms and pack p95 62 ms; AGE vs CTE (the CTE is 1.6–64× faster than AGE fixed patterns; AGE variable-length MATCH timed out at 4 hops). | B's numbers are for B's schema. | **Leave out B's scripts.** Port B's index fixes (`04b169b3a`). The CTE stays the read path, as B measured. |
| Case-study harness | A stub that refused every run, with a T1 suite named `test_*.py`. | estimate, validate-suites, run on the public API, T1–T4 hidden suites, a self-test and a report. | `scripts/tests/test_case_study_harness.py` | **Port from B** (`775e80ab8`), plus the `knowledge_graph` on/off switch its control arm needs. |

## Decided: the database image (operator, 2026-10-22)

This replaces "keep A's opt-in image, leave it open". Commits `a714b07f8`, `2db2b9dcc`, `955d22e4d` and `5ffe21501`.

1. **One image.** It is `boss-community-db:pg16.12-vector0.8.1-age1.6.0`, built from `scripts/install/db-image/Dockerfile`: multi-stage, `FROM postgres:16.12-alpine3.23`, with pgvector 0.8.1 (built without `-march=native`) and AGE 1.6.0, and `shared_preload_libraries=age`. It is the default `db` and `build-services` image in `community-compose.yml`. install.sh builds every service that has a build section, so install.sh, upgrade.sh, remote.sh and `boss update` all use it. The opt-in Debian path is removed. Test harness images (`tests/e2e`, the disposable real-Postgres tests) stay on stock `postgres:16-alpine`, and the schema step skips the extensions there.
2. **The upgrade is an image swap.** The base and libc are the same, and the schema step creates `vector` and `age`. On real containers: a volume populated on `postgres:16-alpine` reopens with the same row counts, `bt_index_check` passes on every B-tree index, and both extensions load. A fresh volume gets both extensions too. `boss upgrade`'s protected backup still runs before the swap.
3. **Meaning-based search is on by default.** Retrieval fuses text, graph and vector results by RRF. With no embedding model set in Setup, or one whose check failed, it runs on text and graph, and status says so (see "Decided: the embedding model" below).
4. **Cypher works.** There is one AGE graph per project, and it is kept current from the same writes plus a reconcile sweep. Read-only Cypher is available at `POST /v1/knowledge/cypher` and through `kg_cypher`:
   - writes are refused, by keyword and by a READ ONLY savepoint;
   - a 5 s timeout and a row limit apply;
   - a query reads only its own project's graph;
   - under a stage scope, the `Code` label is refused in words.
5. **Disk.** `df` was checked before each build. The build left no dangling layers.

## Decided: the embedding model and the open items (operator, 2026-10-22)

Each is a separate commit with a test that fails without it.

1. **The embedding model is its own slot** (`8ca2d6f83`). It mirrors the decision model's slot.
   - **Since 2026-10-23 it is ONE slot for the whole org**, in **Setup › Repo and models › Embedding model** (the Models step, not Advanced): an `org_settings` row and one org-scoped key, used by every project. An older install's per-project row is read as a fallback and promoted to the org on the next save or check. Azure takes one pasted URL (the endpoint or the whole Target URI) and the key; the dimension is learned from the check (an override sits under "More"). Changing it needs `model_provider:manage` for the whole org.
   - It offers three providers:
     - Azure OpenAI: an endpoint, a deployment and an API version;
     - OpenAI, or any OpenAI-compatible endpoint (a base URL);
     - Ollama (local), for example `nomic-embed-text`.
   - One project setting, `knowledge.embedding_model`, stores the provider, the model and its dimension. The key goes into the vault and is never returned, in whole or in part.
   - **Save & check** sends one tiny embedding through `model_routing.project_embedder`:
     - `GovernedChatAdapter` now governs `create_embeddings`;
     - the call is reserved in the budget category `knowledge`, receipted and settled;
     - the row shows the model's real reply, or its error with the key scrubbed out.
   - `services/embedding_adapter.py` is the only place an embeddings request is sent, and only the router builds it. The knowledge package imports no provider SDK.
   - **Meaning-based search turns off automatically** when there is no slot, when the slot has not been checked, or when its check failed (a provider or model with no embeddings). Retrieval then runs on text and the graph. Setup, knowledge status and every search say why in words. A search whose embedding call fails is narrowed the same way; it never errors.
   - **Switching re-embeds in the background.** The vector space is keyed `provider:model:dimension`. On the worker's knowledge tick, `embeddings.embed_due` drops the project's vectors of every other space and embeds what the new space lacks, 64 summaries per tick. Setup shows how many are left.
   - `BOSS_KNOWLEDGE_EMBEDDING_MODEL` is removed.
   - Tests use the scripted engine, so no paid call is made:
     - `engine/tests/test_embedding_model_slot.py`;
     - `apps/api/tests/test_setup_embedding_model.py`;
     - web `setup-embedding-model`, the Setup copy budget and the knowledge console.
2. **Repository-qualified PR ids** (`d369d50dd`). A pull request is `pr-<owner>/<repo>#<n>` (`capture.pull_request_source_id`).
   - `kg_qualify_pr_source_ids()` (db/schema.sql) rewrites old `pr-<n>` ids in `kg_captures`, `kg_change_links` and `kg_nodes`. It does so where the project's runs name exactly one repository for that number.
   - A number that two repositories share is left alone: nothing says which repository it was.
3. **`knowledge:read` for agents with a custom Passport ceiling** (`d5b52fa3a`). `passports.grant_baseline_to_custom_ceilings` runs in M0's seed.
   - It adds `knowledge:read` once per org (`schema_once`), and the change is audited as `sys_migration`.
   - A person who removes it afterwards is not overruled.
4. **The stale committed matrices are regenerated** (`9b7209d1f`), with their generators:
   - `tests/identity/matrix.yaml` (1,615 → 1,628 operations) and `read-routes.gen.ts`;
   - `docs/community/mcp-api-capability-matrix.json` (992 → 1,622 operations).
   - `test_operator_boundary`'s two counts were a snapshot of the stale matrix. They are re-snapshotted to 71 confirmation-gated and 68 operator-only. 65 of the operator-only operations come from main. The other three are this branch's fact promote and reject (a person's step-up) and `DELETE /v1/setup/embedding-model`. All three are kept out of MCP on purpose.
5. **The branch overlay is dropped for now.** It stays unwired, as it is in B.
6. **Next after merge:** a model-backed Archivist. It is gated on the approved precision run, capped at $0.50.

## What was left out, and why

- **B's model-backed Archivist, its per-org settings (caps, retention, PII) and the precision run.** This tree's distil path calls no model, and the precision run is not approved. The gate is ported fail-closed.
- **B's full lifecycle sync** (decisions with approvers, runbooks, alert rules, releases). A captures PRs and decisions, and now runs and incidents. The rest is a follow-up.
- **The branch overlay.** Dropped for now (operator, 2026-10-22). B does not wire it either.
- **B's recipes, PageRank module map and reranker, and per-fact `read_permissions` and team scope.** A's packs and tier ceiling stay. Each is a separable follow-up.
- **B's benchmark scripts.** They target B's schema. The index fixes they found are ported.
- **A workflow-editor control for a step's knowledge scope, an audit record for a scope edit, and partitioning the code layer by repository.**

## Open decisions for the operator

1. **The real-model precision run** (B's Phase 2, approved at a $0.50 cap). It must pass before a model-backed Archivist ships. That Archivist is next after merge.

## Test results (2026-10-22, after the operator's decisions; local; no model was called)

How they ran:
- One Python 3.13 environment for the branch and for main, with the pinned `fastapi==0.115.12` and the tree-sitter grammars of `engine/requirements.txt`.
- Each suite had its own fresh database in `boss-community-db-1` (stock Alpine), with that tree's `db/schema.sql` applied.
- The pgvector and AGE tests ran against a disposable container of the new image, which was removed afterwards.
- Main is `origin/main` `e710eb6df`, run on the same files.
- A failure counts as pre-existing when it also fails on main.

| Suite | This branch | Main `e710eb6df` |
|---|---|---|
| Knowledge tests (`test_knowledge_*`, the embedding slot, custom ceilings), stock Alpine | 144 passed, 6 skipped (AGE only) | — |
| The same, plus the image swap and the case-study harness, new image | 162 passed, 2 skipped (the harness's scratch-Postgres run) | — |
| Engine suite (`engine/tests`) | 10,911 passed, 94 failed or errored | 10,746 passed, 110 failed or errored |
| API set (42 files: knowledge, identity authz, typed decisions, workflow runtime, skills, factory, govern, Setup and the model router; plus the new `test_setup_embedding_model`) | 298 passed, 20 failed | 293 passed, 21 failed |
| Web: `tsc --noEmit` | clean | clean |
| Web: setup, knowledge, decision inbox, identity, cast and read-gate tests | 1,013 passed, 2 failed | 1,001 passed, 2 failed (the same two) |
| P0 `test_the_public_tree_names_no_private_infrastructure` | passes | — |
| MCP parity (`scripts/tests/test_mcp_api_parity.py`) | 3 passed | — |

**No failure is new on the branch.**
- Engine: the comparison flagged three, and none is a regression.
  - `test_knowledge_store_phase0::test_schema_has_the_embedding_table_and_a_search_vector` read every schema in `information_schema`, so it also read a scratch schema another test had left behind. It is fixed in `74da2c001`.
  - `test_an_index_converges_to_schema_sql` and `test_commands_can_run_where_the_control_plane_is_not::test_the_container_really_cannot_reach_the_network` failed only while two suites ran at once. Both pass on a re-run.
- API: no new failure. `test_identity_authz_matrix::test_the_committed_matrix_is_current` failed on main and passes here, because the matrix is regenerated.
- The engine suite's "fixed" list also holds tests that read git or Docker, which cannot run in main's exported copy. They are not counted as fixes.
- Web: the same two tests fail on both, `identity-contract` (the permission count) and `identity-readers-subscribe`.
