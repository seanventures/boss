# Knowledge Graph Phase 3: retrieval and decisions

Companion to [KNOWLEDGE_GRAPH_SPEC.md](KNOWLEDGE_GRAPH_SPEC.md) §3.5–§3.6 and §6. This page is the
handover note for `feat/knowledge-graph-p3` (stacked on `feat/knowledge-graph-p2`). It says what is
done, what is next, and how to resume.

## Status

| Item | State |
|---|---|
| Hybrid retrieval (graph + vector + full text + trusted, RRF, optional reranker gated by budget) | **done**: `services/knowledge/retrieval.py` |
| `knowledge_pack(role, stage, task, budget_tokens)` with the spec's four recipes; hard budget; logs ids | **done** |
| Passport-scoped reads in retrieval (project, org, team, Passport ceiling) | **done**, in every generator, the tools and the API |
| MCP tools `kg_search`, `kg_precedent`; pack injection for engines without tools | **done**: `delivery.py`, `knowledge_tools.py`, hook in `code_agent._invoke` |
| `knowledge:read` / `knowledge:manage` and the HTTP API from spec §5 | **done**: `packages/identity/catalogue.yaml` (regenerated), `routes_knowledge.py`, `test_knowledge_api.py` |
| Branch overlay (per run, in memory, never stored) | **done**: `overlay.py` |
| Jev citations, TS blind check, latency benchmark | only if budget remains |

## What was built

- **Schema** (`db/schema.sql`):
  - stored `search` tsvectors with GIN indexes on `kg_facts` and `kg_nodes`, and a GIN expression
    index on `decisions`;
  - `kg_facts.read_permissions`: the Passport permissions every source of the fact needs (their
    union, so the most restrictive source wins). `FactStore._source` keeps it up to date;
  - `kg_facts.team_id`;
  - `kg_pack_log`: `(run_id, step, role, stage, via, fact_ids, node_ids, decision_ids, tokens, budget)`
    for every pack and tool answer (§3.7);
  - `decisions.cited_fact_ids`.
- **Retrieval** (`retrieval.search`):
  - The candidates are:
    - **graph**: facts about the touched symbols and files and their two-hop neighbourhood, with
      fan-out capped at 50 per node per hop;
    - **text**: OR-of-words over the stored tsvector;
    - **vector**: the caller's governed embedder, when one is given;
    - **trusted**: governed and approved facts of the recipe's kinds.
  - Candidates are fused by RRF, then weighted by tier.
  - The reranker runs only when `budget_allows` the call, and it orders facts only within a tier.
- **Packs** (`retrieval.knowledge_pack`):
  - The recipes are build (invariants, module map, couplings, recent decisions), review (invariants,
    past review findings, security decisions), sre (topology, known causes, runbooks, recent
    incidents) and jev (precedent with outcomes, decisions).
  - The module map is PageRank over the neighbourhood subgraph.
  - Facts are added line by line while `estimate_tokens(text) <= budget_tokens`.
- **Precedent** (`retrieval.precedent`): similar past decisions by full text. Each comes with what was
  decided, by whom, and its outcome (taken from the runs the decision started). It needs `decision:read`.
- **Tests**: `apps/api/tests/test_knowledge_retrieval.py` (31). They cover:
  - fusion;
  - the poisoning order, including a hostile reranker;
  - the budget gate on the reranker;
  - packs never over budget, at 6 budgets × 4 recipes, with the log matching the pack;
  - the recipe contents;
  - the Passport, project, org and team filters;
  - source permissions;
  - precedent.

## Delivery (item 4)

- `kg_search(query, touched, role, stage, budget_tokens)` returns the same `Pack` that
  `delivery.inject` puts into a CLI harness stage. Both go through `delivery.pack`.
  - A CLI harness has no BOSS kg tools (BOSS's MCP server is not among a harness's servers), so
    `code_agent._invoke` always injects. The pack goes after the shared TASK CONTEXT block, so the
    cached prefix is unchanged.
  - The engine-native agent loop holds the tools through `knowledge.read`.
  - Test: `test_the_tool_and_the_injected_pack_are_the_same`.
- `kg_precedent(situation, k)` needs `decision:read` and logs the decision ids it returned.
- The reader is the acting agent's: a run token's scope, else its Passport ceiling
  (`identity_agent_ceilings`), else unfiltered for a person or BOSS. See `delivery.reader_for`.
- What the stage sets in the LLM-call context (`set_llm_context`) feeds the injected pack: `role_key`,
  `stage`, `touched`, `knowledge_budget_tokens`. **Next:** the pipeline should set `touched` from
  the plan or diff. Today it is empty unless a caller sets it, so a pack without it leans on full
  text and trusted facts.
- **Branch overlay** (`overlay.py`): `hold(build(c, project, run_id, {path: bytes|None}))`, or
  `from_worktree`. The overlay seeds the query with the build's changed files and symbols and
  marks symbols changed or new in the module map. A fact about a symbol the build deleted is
  flagged `stale_in_this_build`. It lives only in memory for the run and is never stored;
  `discard(run_id)` drops it. **Next:** call `hold` after each implement stage and `discard`
  when the run ends.

## Permissions and API (item 5)

- **`knowledge:read`** (project scope, delegable) is held by owner, admin, operator, viewer and
  auditor, and by every Passport preset. It gates every kg tool; `kg_precedent` also needs
  `decision:read`.
  - An existing agent whose ceiling is a preset gains it through `passports._grow_to_preset`.
  - **An agent with a hand-set (custom) ceiling does not.** Its kg tools refuse until a person adds
    the permission. This is an open decision for the operator.
- **`knowledge:manage`** (project scope, step-up, not delegable) is held by owner and admin. It
  gates promote, reject and forget, and the settings write.
- **Routes:**
  - `GET /v1/knowledge/search`: facts, or a pack when `budget_tokens` is given;
  - `GET /v1/knowledge/nodes/{id}`, with `why` and the node's facts;
  - `GET /v1/knowledge/blast-radius`;
  - `POST /v1/knowledge/facts/{id}/promote|reject|forget`. Promote and reject need a person, not an
    agent token;
  - `GET /v1/knowledge/graph-status`: ingest lag, sizes, cost and caps;
  - `GET` / `PUT /v1/knowledge/settings`.
- **Deviation from spec §5:** `/v1/knowledge/status` already exists (BOSS-356's pack-context
  probe), so the graph status is at `/v1/knowledge/graph-status`. This is an open decision.

## How to resume

```
git fetch origin && git checkout feat/knowledge-graph-p3
pg_ctlcluster 16 main start          # apt-get install postgresql-16-pgvector if missing
python3 -m venv /tmp/venv && . /tmp/venv/bin/activate
pip install -r apps/api/requirements.txt -r engine/requirements.txt pytest pytest-asyncio && pip install -e engine
DATABASE_URL=postgresql+psycopg://boss:boss@127.0.0.1:5432/boss_test python -m pytest -q apps/api/tests/test_knowledge_retrieval.py
```

The standing rules from Phase 2 still hold:
- no paid model runs;
- targeted tests only;
- `test_the_public_tree_names_no_private_infrastructure` must pass;
- only 203.0.113.x and example.com in tracked files;
- open PRs and do not merge.
