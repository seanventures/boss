# Knowledge Graph Phase 3: retrieval and decisions

Companion to [KNOWLEDGE_GRAPH_SPEC.md](KNOWLEDGE_GRAPH_SPEC.md) §3.5–§3.6 and §6. This page is the
handover note for `feat/knowledge-graph-p3` (stacked on `feat/knowledge-graph-p2`). It says what is
done, what is next, and how to resume.

## Status

| Item | State |
|---|---|
| Hybrid retrieval (graph + vector + full text + trusted, RRF, optional reranker gated by budget) | **done**: `services/knowledge/retrieval.py` |
| `knowledge_pack(role, stage, task, budget_tokens)` with the spec's four recipes; hard budget; logs ids | **done** |
| Passport-scoped reads in retrieval (project, org, team, Passport ceiling) | **done** in the store layer; wiring to the API and tools: see below |
| MCP tools `kg_search`, `kg_precedent`; pack injection for engines without tools | next |
| `knowledge:read` / `knowledge:manage` and the HTTP API from spec §5 | next |
| Branch overlay, Jev citations, TS blind check, latency benchmark | only if budget remains |

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
