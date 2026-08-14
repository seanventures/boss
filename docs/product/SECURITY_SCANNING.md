# Security scanning (OSS)

BOSS orchestrates **open-source** code security scanners as adapter shells. It never
calls the Snyk commercial API; Snyk appears only as a catalog note for a future
bring-your-own integration.

## Supported scanners

| Id | Tool | Kind | Notes |
|---|---|---|---|
| `semgrep` | [Semgrep](https://github.com/semgrep/semgrep) | SAST | Rule packs + custom rules |
| `trivy` | [Trivy](https://github.com/aquasecurity/trivy) | Vuln / container / FS | `trivy fs` |
| `osv-scanner` | [OSV-Scanner](https://github.com/google/osv-scanner) | Dependencies | OSV database |
| `gitleaks` | [Gitleaks](https://github.com/gitleaks/gitleaks) | Secrets | Hardcoded credential detection |
| `checkov` | [Checkov](https://github.com/bridgecrewio/checkov) | IaC | Also used by the governed `iac_verify` lane |

**Snyk** (`status: planned_byo`) is listed in the catalog with website / stars note only.
Requests that include `snyk` **fail closed**.

## Design principles

1. **Subprocess when available** — if the scanner binary is on `PATH`, BOSS runs it with a
   timeout and captures truncated stdout/stderr.
2. **Always support `simulated=true`** — deterministic fixture findings so E2E and CI work
   without installing tools.
3. **Normalized findings** — every adapter maps to:
   `{ severity, rule_id, path, line, message, scanner, cve?, fix? }`.
4. **Receipts** — each run records a Receipt with unique
   `dedupe_key = security_scan:{project_id}:{run_id}` and effect kind
   `security_scan.run`.
5. **Fail closed** — unknown scanner ids are rejected with HTTP 400 before any work.
6. **Gate** — by default, any **critical** finding marks the run `failed` and gate
   `no-go`. Operators can re-evaluate with a custom severity set via the gate endpoint.
7. **In-memory run store** — last runs and summaries are held in process memory for the
   API surface; durable audit is the Receipt + event trail.

## API

Prefix: `/v1/security-scan`

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/scanners` | Catalog with `stars_note`, `website`, binary availability |
| `POST` | `/run` | `{ project_id, actor, path?, scanners[], simulated?, fail_on_critical? }` |
| `GET` | `/runs?project_id=` | Recent runs + `last_summary` |
| `GET` | `/runs/{run_id}` | Full run with findings |
| `POST` | `/runs/{run_id}/gate` | Evaluate go/no-go (`fail_on` severities) |

### Example (simulated)

```bash
curl -sS -X POST "$BOSS_API_URL/v1/security-scan/run" \
  -H "Authorization: Bearer $BOSS_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "project_id": "boss-dogfood",
    "actor": "operator",
    "scanners": ["semgrep", "trivy", "gitleaks"],
    "simulated": true
  }'
```

### Example (real tools on PATH)

```bash
curl -sS -X POST "$BOSS_API_URL/v1/security-scan/run" \
  -H "Authorization: Bearer $BOSS_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "project_id": "boss-dogfood",
    "actor": "operator",
    "path": "/workspace/repo",
    "scanners": ["semgrep", "trivy", "osv-scanner", "gitleaks", "checkov"],
    "simulated": false,
    "timeout_s": 600
  }'
```

## Gate policy

Default: `fail_on = ["critical"]`.

- Critical findings → run `status=failed`, gate `decision=no-go`.
- Re-evaluate without re-scanning:

```bash
curl -sS -X POST "$BOSS_API_URL/v1/security-scan/runs/$RUN_ID/gate" \
  -H "Authorization: Bearer $BOSS_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"fail_on": ["critical", "high"]}'
```

## Relationship to IaC verify

The governed coding pipeline’s IaC lane (`engine/boss_engine/services/iac_verify.py`)
already runs `checkov` or `tfsec` on Terraform changes as a fail-closed security step.
This module exposes Checkov as an **optional** project-wide scanner kind so operators can
scan an entire tree (or run simulated fixtures) outside that lane.

## Implementation map

| Layer | Path |
|---|---|
| Engine service | `engine/boss_engine/services/security_scan.py` |
| API router | `apps/api/app/routers/security_scan.py` |
| Mount | `apps/api/app/routers/platform.py` → `mount_platform_routers` |
| Tests | `apps/api/tests/test_security_scan_api.py` |
| Web client | `apps/web/lib/security-scan.ts` |
| Web UI | `apps/web/app/security-scan-ui.tsx` |

## Hard rules

- Do **not** call Snyk or any commercial scanning SaaS from BOSS core.
- Do **not** silently skip an unknown scanner — reject the request.
- Every scan run must carry a Receipt `dedupe_key` when the DB is available.
- Subprocess env scrubs common cloud credential prefixes (`AWS_`, `ARM_`, `GOOGLE_`, …)
  and never forwards `SNYK_*`.
- Capture is truncated; timeouts become structured scanner errors, not process crashes.
