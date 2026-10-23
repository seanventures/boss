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
| Lifecycle graph | Captured when BOSS opens a pull request, plus decision capture. `capture.py`, `runtime.py`. | A full sync from BOSS records: runs, PRs, decisions, findings, incidents, runbooks, alerts and releases. `lifecycle.py`. | — | **Keep A**, and add event-driven ingest (below). B's full record sync is left out (see below). |
| Durable changed-symbol edges | A PR links symbols **by name**, to the first symbol of that name in the project. The link exists only as a `kg_edges` row, which a re-index or a forget removes. | `kg_change_links`. Changed edges are derived from it. | `test_knowledge_change_links_db`. The same-name test fails on A. | **Port from B** (`76cb7672f`): natural keys, re-derived after every index. |
| Archivist: distil, resolve, tiers | An injected extractor. Contradiction closes the older fact (same-predicate rule). The tier is set by the pipeline. `archivist.py`. | A model-backed Archivist with a judge for same and contradicts, structure-only on budget refusal, and 95.5% scripted precision on 105 facts. `archivist.py`, `facts.py`. | A has no model path. Its `SpendingExtractor` raises. | **Keep A.** The fail-closed budget gate is ported (`2fa93fb27`). The model-backed Archivist waits on an approved precision run. |
| Scrubbing | Shared secret patterns, plus email, phone and an unchecked 13–19 digit card number. `scrub.py`. | `sk_live_`/`rk_test_`/`sk-proj-`/`whsec_` keys, GitHub, Google, Slack, JWT, URL passwords and secret assignments; Luhn-checked cards, national ids, IPs. | `test_knowledge_scrub_secrets`: `sk_live_...` passed A's pattern. | **Port from B** (`f0e8ea091`). B's per-org PII settings are not ported (A has no settings row). |
| Forget by lineage, retention | `forget_source` by capture lineage. `retention_deadline` exists, but no sweep calls it. | `kg_fact_sources` lineage, forget with embeddings, a retention purge per org. | — | **Keep A.** A retention sweep is a follow-up. |
| Embeddings / HNSW | `kg_vec_<dim>` side table with HNSW when pgvector is present; exact cosine otherwise. Search was text only. | `kg_embeddings` with a pgvector column and an HNSW index per model, through a governed embedder. | `test_knowledge_hybrid_db` uses the HNSW index on the new image. | **Keep A's store**. Hybrid search is on by default (`955d22e4d`, operator decision 2026-10-22). |
| Event-driven ingest | None | A database trigger on run finish and incident resolution, a 30 s drain, and a 10-min fallback sweep. | `test_knowledge_ingest_db` | **Port from B** (`58c2caf12`). It captures structure only, with no model. |
| `kg_propose_fact` grants | A seed pass (`grants.py`) gives `knowledge.propose` to every builder and reviewer role. The tool needs `knowledge:manage`, so no agent could reach it. The seed pass wrote `role_skills` without `seed_exclusions`. | Exactly six roles (pm, engineering_captain, qa, adversarial_reviewer, security_reviewer, sre) through `allowed_skills`. A write bundle. `knowledge.read` is in the baseline. | `test_kg_propose_fact_is_granted_to_exactly_the_six_roles`. The seed-writer, reachability and registry tests fail on A. | **Port from B** (`e62227979`). |
| Per-org budget defaults, and B's default-cap fix | None: A never spends. | `kg_knowledge_settings` with $2/day, $20/month and $0.20/run. B fixed caps that never bit for an org with no settings row. | — | **Port the class, not the table.** A's gate fails closed (`2fa93fb27`). The per-org caps come with a model-backed Archivist. |
| Retrieval, RRF, packs, budgets | Text search, tier-first rank, packs under a token budget, `log_usage`. | Graph, text, vector and trusted lists with RRF; recipes per role and stage; PageRank module map; a reranker gated by budget; `kg_pack_log`. | `test_knowledge_hybrid_db` | **Merge both.** Here: text, graph and vector lists fused by RRF, with A's packs and tier rank. B's recipes, PageRank and reranker are left out. |
| Permissions and Passport-scoped reads | `knowledge:read` and `knowledge:manage`. Presets did **not** hold `knowledge:read`, so delegated kg tools were refused. A tier ceiling on packs. | The same permissions, plus every preset and an archivist preset, `read_permissions` per fact, and team scope. | `test_every_shipped_role_and_any_new_agent_gets_a_preset` fails on A. | **Keep A's permissions** and port B's presets (`e62227979`). Per-fact `read_permissions` is left out. |
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
3. **Meaning-based search is on by default.** Retrieval fuses text, graph and vector results by RRF. With no embedding model configured (`BOSS_KNOWLEDGE_EMBEDDING_MODEL` is empty by default), it runs on text and graph, and status says so.
4. **Cypher works.** There is one AGE graph per project, and it is kept current from the same writes plus a reconcile sweep. Read-only Cypher is available at `POST /v1/knowledge/cypher` and through `kg_cypher`:
   - writes are refused, by keyword and by a READ ONLY savepoint;
   - a 5 s timeout and a row limit apply;
   - a query reads only its own project's graph;
   - under a stage scope, the `Code` label is refused in words.
5. **Disk.** `df` was checked before each build. The build left no dangling layers.

## What was left out, and why

- **B's model-backed Archivist, its per-org settings (caps, retention, PII) and the precision run.** This tree's distil path calls no model, and the precision run is not approved. The gate is ported fail-closed.
- **B's full lifecycle sync** (decisions with approvers, runbooks, alert rules, releases). A captures PRs and decisions, and now runs and incidents. The rest is a follow-up.
- **The branch overlay.** Dropped for now (operator, 2026-10-22). B does not wire it either.
- **B's recipes, PageRank module map and reranker, and per-fact `read_permissions` and team scope.** A's packs and tier ceiling stay. Each is a separable follow-up.
- **B's benchmark scripts.** They target B's schema. The index fixes they found are ported.
- **B's governed embedding call** (`model_routing` `create_embeddings`). With no model configured, search degrades and says why. A named model without that call says so too.
- **A workflow-editor control for a step's knowledge scope, an audit record for a scope edit, and partitioning the code layer by repository.**

## Open decisions for the operator

1. **The embedding model**, and whether to port B's governed embedding call so a configured model is used.
2. **The real-model precision run** (B's Phase 2, approved at a $0.50 cap), before a model-backed Archivist ships.
3. **Regenerating the stale committed matrices on main.** `tests/identity/matrix.yaml` (and `read-routes.gen.ts`) and `docs/community/mcp-api-capability-matrix.json` were already stale at `e710eb6df`. The tests that check them fail on main and here alike. This branch's new routes are not in them.
5. **PR source ids.** A keys a PR as `pr-<number>` without its repository, so two repositories in one project can collide.
6. **Agents with a custom Passport ceiling** do not gain `knowledge:read` (B's open decision 4).

## Test results (2026-10-22, local; no model was called)

How they ran:
- Postgres tests used `boss_test` (and a sibling `boss_test_*` database for parallel runs) in the `boss-community-db-1` container (stock Alpine), after `db/schema.sql` was applied.
- The pgvector and AGE tests ran against a disposable container of the new image.
- A failure counts as pre-existing when it also fails on main `e710eb6df`, run on the same files against the same database.

| Suite | PR #15 as opened | This branch |
|---|---|---|
| Knowledge tests (`engine/tests/test_knowledge_*`), stock Alpine | 79 passed (0 run against Postgres in PR #15's own report) | 130 passed, 6 skipped (AGE-only) |
| Knowledge tests + image swap + case-study harness, new image (pgvector, AGE) | — | 142 passed, 2 skipped |
| Engine suite (`engine/tests`, 12.5k tests) | 145 failed, 11,636 passed | 140 failed before the last four fixes. The 8 failures that this branch alone had are fixed (7) or also fail on main (1, a time-of-day cell test). Every remaining failure also fails on main. |
| PR #15 regressions in the engine suite (fail on PR #15, pass on main) | 13 (in 10 files), including P0 `test_the_public_tree_names_no_private_infrastructure` | 0 |
| API set (knowledge, identity route inventory, typed decisions, workflow runtime, skills, factory, govern), 36 files | 14 failed, 290 passed: the same 14 as main | 14 failed, 310 passed: the same 14 as main |
| Web: `tsc --noEmit` | clean | clean |
| Web: knowledge, decision inbox, identity and cast tests | 4 failed (3 PR #15 regressions, 1 also on main) | 1 failed (`identity-contract` count, also on main) |

Failures on main that remain here:
- Engine:
  - `test_every_template_runs_end_to_end` (12)
  - `test_an_agent_works_from_an_instruction` (6)
  - `test_an_ask_about_this_answers_first_in_one_call` (6)
  - `test_a_busy_budget_is_not_the_agents_problem` (3)
  - `test_sandbox_s1_go` (3)
  - `test_every_decision_boss_raises_has_its_buttons_decided` (2: `self_fix_install.request_removal`)
  - `test_deps_audit_strict` (2)
  - `test_identity_fxa_upgrade_promise` (2)
  - `test_every_template_says_what_it_needs` (2)
  - single tests in about a dozen more files
- API:
  - `test_skill_resolution` (5)
  - `test_identity_authz_*` (6): the committed route matrix is stale
  - `test_community_openapi_routes` (2): `api/openapi.community.yaml` is missing
  - `test_workflow_runtime` (1)
