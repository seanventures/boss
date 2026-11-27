# Failure Pre-check Proposals (BOSSAPP-541)

## Problem

Coded pre-checks in `engine/boss_engine/services/workflow_precheck.py` were historically added by
hand after production incidents. On 2026-10-14 three run-killers slipped past pre-check:

1. **`socat` missing** from the runtime image (`Sandbox dependencies not available: socat not installed`).
2. **Disconnected model source reported connected** (stale or rejected credentials / chat fallback reported ready while the routed build source failed).
3. **Budget caps that cannot fit metered billing** (daily/monthly/burst cap too small for the run's metered token spend).

Rather than guessing which checks to write next, every failed run classifies its failure cause and
emits a deterministic pre-check proposal linked to an issue record.

## Storage & Deduplication

- **Table**: `precheck_proposals` (`db/schema.sql`), tenant/project-scoped via
  `project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE`.
- **Deduplication**: `UNIQUE (project_id, dedupe_key)` where
  `dedupe_key = "precheck:<cause_class>:<proposed_check_id>"`. Repeated failures of the same cause
  increment `occurrences`, append the new `run_id` to `run_ids` (bounded), and update `updated_at`
  without creating duplicate proposals or duplicate issues.
- **Issue linkage**: Every recorded proposal also records (or increments) a matching row in
  `boss_recommendations` (`recommendation_id = brec_...`), which surfaces in `RECOMMENDATIONS.md`
  and `GET /v1/boss-recommendations`.
- **Audit trail**: Creation emits `precheck.proposal_recorded`; human review emits
  `precheck.proposal_reviewed`. All evidence is scrubbed through `redaction.redact_secrets` and
  `identity.redaction.redact_text(public=True)` before storage so credentials never enter the table.

## Classification Pipeline

`engine/boss_engine/services/precheck_proposals.py` runs BOSS's existing deterministic classifiers
in priority order:

1. `harness_failures.classify` / `harness_failures.parse_sentence` (missing runtime binaries such
   as `socat`/`bwrap`, sandbox init failures, refused auth, credit refusal, CLI crash).
2. `model_failures.explain` (rejected login / disconnected source, budget cap or hold refusal,
   unconfigured model, provider requirement, storage failure).
3. `code_task_service.classify_pipeline_exception` + explicit cause rules (repository access,
   build services, unsupported stack, missing app environment variables, generic fallback).

Each classification produces:
- `cause_class` (canonical dot-delimited class, e.g. `harness.missing_runtime`,
  `model.source_disconnected`, `budget.cap_vs_metered_billing`)
- `proposed_check_id` & `existing_check_id` (mapping onto `workflow_precheck.CHECKS`)
- `checkpoint` (`pre_run` or `pre_deploy`) and `target_capability`
- `probe_spec` (structured check logic, expected verdict, fix action, target files)

## Approval Workflow (Never Self-Modifying)

- Proposals are created strictly in `status = 'proposed'`.
- **BOSS never self-modifies** `workflow_precheck.py`, runtime checks, or policies automatically.
- Approval requires an explicit human actor holding `improvement:manage` (Project Admin / Owner)
  via `POST /v1/precheck-proposals/{proposal_id}/review` with `status = "approved" | "rejected"`.
- Approving a proposal marks it ready for a governed code change (PR or reviewed self-fix) and
  updates the audit trail; rejecting it records the reviewer and rationale so the same cause is
  tracked without silently dropping failures.
