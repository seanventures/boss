# Knowledge Graph Phase 1: code graph, lifecycle graph, MCP tools

Companion to [KNOWLEDGE_GRAPH_SPEC.md](KNOWLEDGE_GRAPH_SPEC.md) §3.3, §4, §5 and §6. This page
records what Phase 1 built, the acceptance numbers measured on the pinned Saleor commit, and the
open decisions.

## Acceptance (Saleor `9ae360ae76b4`, 4,380 Python files, 861k lines; 4 vCPU, 15 GB RAM)

| Criterion | Target | Measured |
|---|---|---|
| Full index | ≤ 15 min | **26 s** (4.6 s parsing on 4 processes; 28,491 symbols, 38,381 nodes, 131,085 edges) |
| Incremental update per commit | ≤ 30 s | **≤ 5.2 s** over 10 consecutive commits (median 1.3 s); the largest commit changed 26 files and re-resolved 1,616 |
| `kg_blast_radius` on 30 hand-checked symbols | ≥ 90% | **29/30 (96.7%)** before the property rule, **30/30** after (precision 1.00, recall 1.00) |

**Correctness of incremental updates.** After replaying the 10 commits, the graph holds exactly
the nodes and edges a fresh full index of the head commit produces: 38,381 and 131,085. Along
the way, 102 nodes and 588 edges had their validity closed; nothing was deleted. The
end-to-end test `test_an_incremental_update_closes_what_went_away_and_converges_on_a_full_index`
checks the same property on every run.

**About the blast-radius number.** The 30 symbols are a seeded random sample: 14 functions, 10
classes and 6 methods. Each has a short name defined once in Saleor and appears in 3–20 files.
- The question asked was: which files depend directly on this symbol (depth 1)?
- Every candidate line from `git grep -w` was read by hand. The rules and the full list are in
  [`scripts/knowledge/results/blast_radius_saleor_truth.json`](../../scripts/knowledge/results/blast_radius_saleor_truth.json).
- One hand-check was wrong at first: three importing files were missed because the evidence
  listing was truncated. The tool found them, they were verified, and the list was corrected
  before any indexer change.
- The one real miss was a Python `@property` read without a call (`CheckoutInfo.valid_pick_up_points`).
  Properties are now marked, and an attribute read links to a property when exactly one property
  in the repository has that name.
- The sample was also used to find that gap, so 30/30 is optimistic. A fresh held-out sample
  would be the honest next check.

Reproduce:

```sh
python3 scripts/knowledge/measure_code_index.py --dsn postgresql+psycopg://USER:PASS@127.0.0.1:5432/kgp1 \
  --repo-dir ../saleor --repo saleor/saleor --base HEAD~10 --head HEAD --out code_index_saleor.json
python3 scripts/knowledge/eval_blast_radius.py --dsn postgresql+psycopg://USER:PASS@127.0.0.1:5432/kgp1 \
  --truth scripts/knowledge/results/blast_radius_saleor_truth.json
```

## What was built

- **Code indexer** (`services/knowledge/code_parse.py`, `code_index.py`). Tree-sitter for Python,
  TypeScript/JavaScript, Go, Java and C#.
  - **Extracted:** files, modules, symbols (functions, classes, methods, properties), imports,
    calls and other references, HTTP endpoints (Django/FastAPI/Flask, Express, net/http and gin,
    Spring, ASP.NET), tables (Django models and SQL in strings), queues (Celery) and webhooks,
    and configuration keys (env and settings reads).
  - **Name resolution** follows imports, package re-exports, relative imports, Go module paths,
    same-package names in Go, Java and C#, and `self`/`this`. As a last resort, a call on an
    object of unknown type links to the one definition with that name in the repository, with
    confidence 0.5.
  - **Cross-repository edges.** Tables, queues, webhooks and config keys are keyed per project,
    not per repository, so two repositories that share a table or a webhook meet at the same node.
- **Lifecycle graph** (`lifecycle.py`). BOSS's records become graph nodes:
  - runs and their specs;
  - PRs and the files and symbols they changed;
  - decisions, with actor and reason;
  - review findings, attached to files;
  - incidents, linked to their root cause, the symbol or file involved, and the fix PR;
  - alert rules, runbooks and releases.

  Code is linked only when the code graph already holds it.
- **Queries and MCP tools** (`code_queries.py`, `tools/knowledge_tools.py`):
  - `kg_symbol`, `kg_callers`, `kg_blast_radius` and `kg_why`, all reads, over the store's
    scoped recursive CTE (the Phase 0 decision);
  - they are granted to every role through the `knowledge.read` baseline skill.
- **Keeping it current** (`index_jobs.py`): a `knowledge.index` job, triggered three ways.
  - **After each BOSS push**, enqueued once per run. It indexes the default branch from a partial
    clone, maps the PR's diff hunks onto the definitions they touched, and re-syncs the lifecycle
    graph.
  - **Hourly**, an incremental pass over each indexed repository. This also picks up merges.
  - **Nightly**, a full reconcile.

  The worker's tick queues the scheduled passes, and the API runs each job.

## Schema changes

- `kg_nodes`: new nullable columns `repo` and `path`, with index `(project_id, repo, path)`. These
  are how a file's nodes are found again on re-index.
- `kg_edges`: a partial index on `src_id` for current edges.
- New tables:
  - **`kg_code_files`**: the per-file cache of content hash and parsed facts, with a GIN-indexed
    list of the names each file refers to, so a commit finds the files whose edges it can change;
  - **`kg_code_index_state`**: per repository, the last indexed commit, the last full reconcile,
    and the last run's stats.
- New edge kind: `references`, for a non-call use of a symbol (a type, a base class, a value).
- New job kind: `knowledge.index`, mapped to `repository:read`.

## Open decisions

1. **A `knowledge:read` permission.** The MCP tools use `repository:read`, which every agent
   preset holds, because what they reveal is the repository's structure and history. Spec §5's
   `knowledge:read` / `knowledge:manage` arrive with the HTTP API in Phase 3. Adding them to the
   identity catalogue touches every role and the generated seed.
2. **The graph describes the default branch.** A push records which symbols its PR changed, but
   the graph itself moves only when the default branch moves (on the hourly pass after a merge).
   Indexing PR branches into the graph would need per-branch state.
3. **Changed symbols are stored on the run** (`code_task_runs.state.changed_symbols`). That write
   does not bump `state_version`, so a later full rewrite of the run's state can drop it. The
   only cost is a missing PR → symbol link; the PR → file links survive.
4. **The unique-name fallback** (confidence 0.5) can link a call on an unknown object to the
   wrong definition when the repository has only one definition by that name. The
   blast-radius sample found no case of it.
