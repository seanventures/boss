# Cloud session brief: BOSS Knowledge Graph, Phase 0

You are picking up the BOSS Knowledge Graph epic.

**Read first:**
1. `docs/product/KNOWLEDGE_GRAPH_SPEC.md`, all of it, especially §0.
2. `docs/product/KNOWLEDGE_GRAPH_CASE_STUDY.md`.

**Repository:** `github.com/seanventures/boss`. Branch from `main` as `feat/knowledge-graph-p0`. Open a PR against `main` when Phase 0 is done.

## Your scope: Phase 0 only
1. **DB image.** A BOSS Postgres 16 image on Debian with pgvector and Apache AGE 1.8.x.
   - An upgrade path from the current Alpine image that is safe for collation (dump/restore or REINDEX under `boss upgrade`'s protected backup).
   - A test that upgrades a *populated* database and verifies text indexes and row counts.
   - Update the install compose files and docs.
2. **`KnowledgeStore` interface** plus the Postgres implementation skeleton (spec §3.1, §5).
   - `knowledge_graph.py` keeps its public API, with its existing tests green.
3. **Graphiti spike** (spec §3.2): option A (Graphiti with an AGE + pgvector driver, LLM and embedding calls through BOSS's model gateway) against option B (native).
   - Use the same 200 episodes and 50 hand-labelled facts.
   - Write `docs/product/KNOWLEDGE_GRAPH_SPIKE.md` with precision, latency, cost and code size, and a recommendation.
4. **AGE performance check.** 1M nodes and 5M edges, synthetic. Measure 2–4 hop traversals in AGE against recursive CTEs on `kg_edges`. Record the numbers in the spike doc.
5. **Case-study harness** (case study §7): the tasks folder with briefs and hidden suites for T1–T4 at least, `run.py` and `report.py`.
   - **Do NOT run the smoke stage.** It spends model credit. Stop and report for the operator's approval.

## Rules
- Every model call (including embeddings) goes through BOSS's model gateway and budget.
- Model-agnostic: no Claude-only paths. Agents reason; no keyword rules.
- Targeted tests only, each failing before its change. The P0 test `test_the_public_tree_names_no_private_infrastructure` must pass.
- No real host addresses or secrets in tracked files (use `203.0.113.x` and `example.com`).
- No installs on the operator's machines. Do not approve decisions. Open a PR; do not merge it.
- Keep reports short: what was built, the numbers, open decisions.

## Report back (in your final message and in the PR description; you have no access to Plane)
- the PR link;
- the spike recommendation (A or B) with its numbers;
- the AGE vs CTE numbers;
- the harness status;
- the estimated cost of the smoke stage.
