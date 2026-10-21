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
| Latency benchmark | **done** at 1M nodes / 5M edges / 500k facts (below); not at the 10M target |
| Precedent battle scenarios on the scripted decider | **done**: 83% (below) |
| Jev citations wired into `intent_router` / `typed_decisions`, console "based on" | **not done**. `decisions.cited_fact_ids` exists and precedent returns it; nothing writes it yet |
| Fresh blind TypeScript check (15 symbols, non-Medusa repo) | **not done**: no score to report |

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

## Phase 3 numbers

**Latency** (`scripts/knowledge/bench_retrieval.py`, local Postgres 16 + pgvector, 200 queries,
3 touched symbols each, 1,200-token packs, no embedder):

| Graph | search p50 / p95 | pack p50 / p95 | packs over budget |
|---|---|---|---|
| 100k nodes, 500k edges, 50k facts | 45 / 61 ms | 61 / 80 ms | 0 |
| 1M nodes, 5M edges, 500k facts | **46 / 58 ms** | **49 / 62 ms** | **0** |

- **This is not the spec's scale target** (10M nodes, 50M edges, 5M facts). Building that needs
  about an hour and more disk than this session had. The 1M run is a tenth of the target.
- p95 stayed flat from 100k to 1M once three planner traps were fixed. Each was an OR over
  columns, or a missing `(project_id, node_key)` index. The fixes are new indexes in
  `db/schema.sql`.
- Full-text candidate ranking is the term that grows with the corpus. Re-run the script at
  `--nodes 10000000` before signing off on the < 300 ms criterion.
- The vector generator was not exercised: no embedder was configured, and none may be called.

**Precedent battle test, scripted** (`tests/agent-battle/precedent.yaml`, 6 scenarios;
`test_knowledge_precedent_battle.py`):
- **5 of 6 correct (83%; the bar is 80%), and the expected precedent was cited 6 of 6 times.**
- The miss (`dependency-bump-passed`) is the scripted decider's word-overlap tie-break. The pack
  had the right precedent and outcome.
- The decider reads only Jev's pack. So this measures retrieval, not a model's judgement.

**Real-agent battle-test cost (not run, not approved):**
- The set is 18 scenarios and 129 cases today, plus these 6 precedent scenarios (about 30 cases):
  roughly 160 cases in all.
- At about 3 agent calls per case (about 24k tokens in, 1.5k out) on claude-sonnet-5 ($2 / $10
  per Mtok), plus a qwen3-32b judge call, the estimate is:
  - **about $0.065 per case**;
  - **about $10 for the full set**;
  - **about $2 for the precedent scenarios alone**.
- Prompt caching would lower both.

## Open decisions

1. **The real-model precision run (Phase 2).** It is approved but blocked: this session had no
   `GROQ_API_KEY` or `ANTHROPIC_API_KEY`, and the AWS credentials were rejected by Bedrock. $0 spent.
2. **The real-agent battle-test run** (about $10 for the full set, about $2 for the precedent
   scenarios alone).
3. **Status route.** The graph status is at `/v1/knowledge/graph-status`, because
   `/v1/knowledge/status` is BOSS-356's. Rename one of them?
4. **Agents with a custom (hand-set) Passport ceiling** do not gain `knowledge:read` on upgrade,
   so their kg tools refuse. Grant it in a migration?
5. **The scale-target benchmark** (10M nodes) needs a bigger box.

## Next (in order)

1. Pipeline wiring:
   - set `touched` (from the plan or diff) and `role_key` in the LLM context per stage;
   - `overlay.hold` after implement, and `overlay.discard` at run end.
2. Jev citations: the intent router's decision step reads the jev pack, writes
   `decisions.cited_fact_ids`, and the console shows "based on".
3. The blind TypeScript check on a fresh non-Medusa repository. Commit the answer key first.
4. The 10M benchmark.
5. **Stage knowledge scopes** (operator, 2026-10-21; do this before item 4). The code graph is large, so which layers a stage may read must be enforced in the data layer, not left to the prompt.
   - **Layers.** Tag every node and fact with a `layer`:
     - `code`: files, symbols, calls, imports, endpoints, tables, queues, config keys;
     - `lifecycle`: spec, PR, decision, review finding, release;
     - `ops`: service, deployment, incident, root cause, alert rule, runbook, signal;
     - `knowledge`: distilled facts and topics.

     Add partial indexes per layer. Partition the code layer by repository, so a query on other layers never touches code rows. Prove it with a test where the latency of an ops query does not change as the code graph grows from 0 to 1M nodes.
   - **Stage policy is declared, not inferred.** Each workflow step kind declares `knowledge: {layers, tools, budget_tokens, code_seed, max_hops, max_nodes}`. It is editable in the workflow editor, and the defaults are:

     | Stage | Layers | Code access |
     |---|---|---|
     | triage (alert / Beacon) | `ops`, `lifecycle`, `knowledge` | **none** |
     | investigate / RCA | `ops`, `lifecycle`, `knowledge`, `code` | **seeded only**, from the incident's evidence (failing route → endpoint → handler symbols; the release diff's changed symbols); `max_hops` 2, `max_nodes` 200; never a whole-graph scan |
     | design / implement | `code`, `lifecycle`, `knowledge` | seeded from the plan or diff (touched symbols), bounded |
     | review | `code` (changed symbols ± 1 hop), `lifecycle`, `knowledge` | the diff only |
     | jev / decide | `lifecycle`, `knowledge`, `ops` | none |

     Replace the role/stage-name matching in `recipe_for` with this declared policy. A step without a declaration gets its kind's default. The role is only a fallback.
   - **Enforced where data is read.** The `Reader` carries the stage policy, the same way it carries the Passport.
     - Every `KnowledgeStore` query adds `layer = ANY(:layers)`, plus the seed and hop bounds for `code`.
     - MCP tools outside the stage's layers are not offered in that stage. If one is called anyway, it refuses in words, for example: "Code isn't available while triaging; it opens in Investigate."
     - The injected pack is built under the same policy, so every engine sees the same scope.
   - **Escalation is a step change, never a bypass.** If a triage agent reasons that it needs code, it asks to move the run to Investigate, through Jev or a decision. Nothing widens the scope inside a step.
   - **Tests:**
     - a triage pack has 0 code-layer items, and the tool call is refused;
     - an RCA pack contains the implicated symbols and nothing outside the hop or node bounds;
     - a review pack is limited to the diff ± 1 hop;
     - a policy edit is audited;
     - the ops-latency-vs-code-size test above.


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
