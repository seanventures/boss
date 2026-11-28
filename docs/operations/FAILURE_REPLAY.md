# Customer Failure Replay Workflow (`BOSSAPP-1434`)

Every real customer or dogfood failure that becomes a bugfix must leave behind a deterministic,
scrubbed replay test under `engine/tests/fixtures/customer_replays/<id>/record.yaml` so the exact
failure class cannot regress silently.

## Canonical Format: `boss.customer_failure_record/v1`

Each replay fixture is a YAML document stored at:

```text
engine/tests/fixtures/customer_replays/<id>/record.yaml
```

Required fields:

```yaml
schema: boss.customer_failure_record/v1
id: northgate_breaker_quoted_db          # ^[a-z0-9][a-z0-9_-]{2,64}$
recorded_at: "2026-11-16"                # YYYY-MM-DD
source: incident_note                    # share_bundle | incident_note | retrospective | dogfood_run
title: Short human-readable title
scrubbed: true                           # Must be true; validated against redaction/privacy scanners
symptom: What the operator observed
root_cause: Verified underlying cause in BOSS
raw_evidence: |
  Scrubbed log/error/brief snippet (no secrets, no private infra names, no raw host IPs).
replay:
  kind: model_failure_explain            # model_failure_explain | app_root_java_build | stack_deploy_explain
  target: boss_engine.services.model_failures.explain
  inputs:
    error: "..."
  expected:
    equals:
      is_storage_failure: false
    not_equals:
      kind: storage
    not_contains:
      action: "server log"
```

### Supported `replay.kind` Dispatchers

| `replay.kind` | Target | Inputs | Output Fields Checked |
|---|---|---|---|
| `model_failure_explain` | `model_failures.is_storage_failure` + `model_failures.explain` | `error` | `is_storage_failure`, `kind`, `headline`, `action`, `href`, `retry` |
| `app_root_java_build` | `app_root.java_build` | `files` (relative path -> content), `changed`, `lineage` | `app_dir_name`, `build_tool` |
| `stack_deploy_explain` | `stack_deploy.explain_failure` | `step`, `returncode`, `output`, `host`, `url`, `timeout` | `cause`, `headline`, `action` |

### `expected` Assertion Operators

- `equals`: mapping of `output_key: expected_value` (exact equality).
- `not_equals`: mapping of `output_key: forbidden_value`.
- `contains`: mapping of `output_key: substring` (case-sensitive substring check on `str(output[key])`).
- `not_contains`: mapping of `output_key: substring`.

## Conversion & Scrubbing Workflow

1. **Extract**: Start from a `share_bundle`, `run_retrospective`, `boss_recommendations` row, or incident note.
2. **Scrub**: Remove every credential (`redaction.redact_secrets`), every private infrastructure name (`private_infra.findings`), and every real host or IP address (`self_fix_privacy.findings`). Use placeholders such as `[SECRET]`, `[host]`, `localhost`, or `127.0.0.1` for local loopback examples.
3. **Author**: Write `engine/tests/fixtures/customer_replays/<id>/record.yaml` using `schema: boss.customer_failure_record/v1`.
4. **Validate**: Run `customer_failure_replay.validate_record(record)` to verify schema completeness and zero redaction/privacy findings.
5. **Verify**: Run `pytest -v engine/tests/test_customer_failure_replays.py engine/tests/test_the_public_tree_names_no_private_infrastructure.py`.
