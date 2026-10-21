# Knowledge Graph Phase 2: the Archivist

Companion to [KNOWLEDGE_GRAPH_SPEC.md](KNOWLEDGE_GRAPH_SPEC.md) §3.4 and §3.8. This page records
what Phase 2 built, the acceptance results, the cost of the real-model precision run (not run, as
it is not approved), and what is still open. It is also the handover note: everything below is on
`feat/knowledge-graph-p2`, and the last section says where to pick up.

## Acceptance

| Criterion | Test | Result |
|---|---|---|
| 0 secrets in the store after ingesting seeded secrets | `test_no_secret_or_pii_reaches_the_store_even_when_the_model_echoes_it` | **pass**: 7 secret and PII shapes are seeded in the record and echoed back by the model; none is stored and none is sent to the model |
| Poisoning: `kg_propose_fact` and agent or model facts never become governed or approved, nor outrank them | `test_an_agent_or_the_model_can_never_write_governed_or_approved_nor_outrank_them` | **pass** |
| Forget by lineage | `test_forget_by_lineage_removes_derived_facts_and_their_embeddings` | **pass** |
| Fact precision ≥ 85% on 100 labelled facts, scripted | `test_fact_precision_on_100_labelled_facts` | **95.5%** (105 of 110 stored facts correct) |
| Real-model precision run | not run | **not approved**; its cost is below |

**Other tests:**
- contradictions close the older fact, and nothing is deleted;
- retention;
- structure-only when the knowledge budget refuses;
- every call is reserved in budget category `knowledge`;
- embeddings are cached by content hash, with the model recorded and an HNSW index per model;
- the tick distils each finished record once.

All are in `apps/api/tests/test_knowledge_archivist.py` (9 tests) and `test_knowledge_precision.py`.

**What the precision number means.** The 25 records and 105 labelled facts are in
`scripts/knowledge/precision/corpus.py`. The scripted model replays what an extraction model
returns, including realistic noise:
- facts of an unknown kind or with an empty statement, which validation drops;
- paraphrases of earlier facts, which merge after a model judgement;
- updates, which supersede the stale fact;
- a fact quoting a live payment key, which is scrubbed;
- one plausible but wrong fact in every fifth record, which nothing can catch.

So 95.5% measures the pipeline: that it loses no correct fact and lets no stale or malformed one
through, given a model that hallucinates at that rate. The model's own precision is what the real
run measures.

**The harness found a real leak.** Stripe-style `sk_live_…` keys slipped past the existing secret
pattern because of the `live_` segment. The pattern is fixed and the leak test now seeds that shape.

## Cost of the real-model precision run (awaiting approval)

Measured from the exact prompts and replies of the scripted run: 25 records, 118 calls (25
extractions and 93 judgements), about 31,200 input tokens and 9,600 output tokens.

| Model | Price per Mtok (in / out) | Cost |
|---|---|---|
| claude-sonnet-5 | $2 / $10 | **$0.16** |
| qwen3-32b | $0.29 / $0.59 | **$0.015** |

A real model's replies may run longer than the script's. **A $0.50 cap covers the run on either
model with a wide margin.** This is far below the ~$5 the Phase 0 plan assumed. That estimate
counted a larger corpus and extraction through Graphiti, which option B dropped.

To run it once approved: point `Archivist(settings)` at the project's model instead of the scripted
adapter (`adapter_factory=None`), ingest the same 25 records, then compare the stored facts with
`corpus.labels()`. Facts the model words differently need judging against the labels, by a person
or by the judgement call.

## What was built

- **Schema** (`db/schema.sql`):
  - `kg_facts`, with tier, confidence, `valid_from` / `valid_to`, `superseded_by`, and a content
    hash that is unique while the fact is current;
  - `kg_fact_sources` (the lineage);
  - `kg_fact_events` (the audit of every write, merge, supersession, approval, forget and purge);
  - `kg_knowledge_settings`, per org: retention, the knowledge budget's daily and monthly caps,
    the per-run ceiling, and the PII detector;
  - `kg_ingest_log`, per source: mode, cost, facts written, and scrub counts only;
  - `kg_embeddings` (pgvector). It is created only where pgvector exists, and the type is
    qualified with the extension's schema, so a search_path without it still works.
- **Tiers** (`services/knowledge/facts.py`). A write names an `Origin`, which only BOSS code
  builds. The tier comes from the origin:

  | Origin | Tier |
  |---|---|
  | `deterministic` | observed |
  | `decision` | governed |
  | `distilled` (the Archivist) | inferred |
  | `agent` | inferred |

  - Any tier the payload claims is dropped.
  - The ranking is governed > approved > observed > inferred.
  - A lower-ranked contradiction is recorded and closed at once.
  - Promotion to approved needs a person (`actor_kind == "human"`).
- **Resolve.** First an exact symbol or node match for the subject. Then an exact duplicate merges.
  Then the same subject's current facts, plus the nearest by embedding, go to a model judgement
  (same, contradicts or unrelated), within the per-run ceiling.
- **Scrubbing** (`services/knowledge/scrub.py`):
  - `knowledge_context._redact_secrets` is extended: GitHub fine-grained and app tokens, Google
    keys, Slack webhooks, JWTs, passwords in URLs, assigned secrets, and `sk_live_` / `whsec_`;
  - a per-org PII detector covers email, phone, IP, Luhn-checked cards, national ids, and custom
    patterns;
  - it runs on the source and again on every model reply.
- **Archivist** (`services/knowledge/archivist.py`):
  - one extraction call per finished run, review or resolved incident, then judgements, then
    embeddings;
  - every call goes through `model_routing.governed(..., category="knowledge")`. The gateway gained
    a `category` and an `estimate_usd_micros` override, and governs `create_embeddings`;
  - the org's daily and monthly knowledge caps and the per-run ceiling are checked first. A refusal
    at either level means structure-only.
- **Scheduling.** The worker's existing knowledge tick (every 10 minutes) calls
  `distil_due()`: up to 5 records per tick, each in its own transaction.
- **Tools:**
  - `kg_facts` (read, ranked, in the `knowledge.read` bundle every role holds);
  - `kg_propose_fact` (write, always inferred, scrubbed; not in any baseline).
- **The Archivist role, "Lore"**:
  - `packages/specpack/roles/archivist.yaml` (team: architecture), and in the core_v2 pack;
  - Passport preset `archivist`: reads only, plus `spend:reserve`; no code, GitHub write, deploy or
    comms;
  - avatar in `apps/web/lib/avatar-kit/cast.ts`, with `docs/cast` regenerated;
  - a canonical role key.

## Operator decisions (2026-10-21), implemented

1. **Real-model precision run: approved**, cap $0.50 total, on qwen3-32b and claude-sonnet-5.
   Results are in the section below once run.
2. **`kg_propose_fact`** is the `knowledge.propose` skill (kit.yaml), granted through the role
   specs' `allowed_skills` to pm, engineering_captain, qa, adversarial_reviewer,
   security_reviewer and sre only (`agent_baseline.PROPOSE_FACT_ROLES`). It is not in the
   baseline. It is the only bundle allowed to offer a write (`WRITE_BUNDLES`), the tool must declare
   itself safe to reverse, the model cannot name the project, and what it writes is always
   `inferred`. Test: `test_kg_propose_fact_is_granted_to_exactly_the_six_roles`.
3. **Event-driven ingest.** Database triggers queue a run and its review the moment
   `code_task_runs.status` becomes completed or failed, and an incident the moment it becomes
   resolved (`kg_ingest_queue`), whatever code path finished it. The worker drains the queue
   every 30 s (`POST /v1/knowledge/distil/tick`, `distil_queued`). The 10-minute sweep
   (`distil_due`) stays only as the fallback. Tests:
   `test_a_finished_run_is_queued_by_trigger_and_ingested_in_under_five_minutes` (the distil
   plus the worst-case 30 s wait is well under 300 s) and `test_a_resolved_incident_is_queued_by_trigger`.
4. **Defaults kept:** $2/day, $20/month, $0.20 per run, 365-day retention. Each org edits them at
   `GET` / `PUT /v1/knowledge/settings` (settings:read / settings:update; Phase 3 moves the write
   to knowledge:manage). Out-of-range values and unknown keys are refused. Test:
   `test_the_knowledge_defaults_hold_and_each_org_can_edit_them`.

**A bug found on the way.** `org_settings` selected `w.org_id, s.*`, and `s.org_id` (NULL for an
org with no settings row) shadowed the org id. The org's spend then summed to 0, so the default
caps never refused anything for such an org. Fixed;
`test_the_default_daily_cap_bites_for_an_org_with_no_settings_row` fails before the fix.

Still open: **no console surface yet** for the cost of knowledge, approvals, or forget (Phase 3/4).

## Handover: where to pick up

- **Branches:**
  - `feat/knowledge-graph-p0`: PR #11;
  - `feat/knowledge-graph-p1`: PR #12, stacked on p0;
  - `feat/knowledge-graph-p2`: this phase, stacked on p1.

  Do not merge; the operator merges.
- **Tests:**
  - `apps/api/tests/test_knowledge_*.py` need Postgres with pgvector, which the
    `boss-community-db` image has. Run them with `DATABASE_URL=postgresql+psycopg://USER:PASS@127.0.0.1:PORT/boss_test`;
  - `engine/tests/test_knowledge_code_parse.py` needs no database.
- **Rules carried through every phase:**
  - no paid model runs without approval;
  - every model or embedding call goes through `model_routing.governed`;
  - targeted tests, each failing before its change;
  - `test_the_public_tree_names_no_private_infrastructure` must pass;
  - only 203.0.113.x and example.com in tracked files.
- **Next:** Phase 3 (spec §3.5–§3.7): retrieval and context packs, the HTTP API with
  `knowledge:*` permissions, the per-run branch overlay (operator decision from Phase 1), and the
  console.
