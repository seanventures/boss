# Security scanning (OSS)

BOSS has adapter code for Semgrep, Trivy, OSV-Scanner, Gitleaks, and optional
Checkov. Snyk is catalog-only; BOSS does not call its commercial API.

## Community GA execution gate

`POST /v1/security-scan/run` currently returns HTTP 409 after authenticated
`providerops_manage` authorization in the requested project. It never invokes
a scanner. Unauthenticated requests receive 401 and actors without project
permission receive 403. The gate applies to real and simulated requests and
to all editions until the execution boundary is safe.

The previous API accepted a caller-controlled host path, or used the API
process's working directory when no path was supplied. It also created its
Receipt only after the scanner process finished and tolerated receipt failure.
A scanner run must first resolve a configured project workspace, confine and
validate the scan path, reserve a durable receipt, and use a policy-guarded
adapter before API execution can be restored. These requirements are a GA
capability blocker. The internal simulated adapter remains available for tests;
it does not make the API run action available.

The console shows prior findings and gate results without offering a run
control. The scanner catalog and read endpoints remain available:

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/v1/security-scan/scanners` | Scanner catalog and `execution_available: false` |
| `GET` | `/v1/security-scan/latest?project_id=` | Most recent run or honest `not_configured` |
| `GET` | `/v1/security-scan/runs?project_id=` | Recent runs and last summary |
| `GET` | `/v1/security-scan/runs/{run_id}` | Prior findings |
| `POST` | `/v1/security-scan/runs/{run_id}/gate` | Re-evaluate a prior run's go/no-go status |

The governed coding pipeline's IaC verify lane is separate from this project
wide scanner API and continues to run Checkov or tfsec on Terraform changes
under its own controls.

## Implementation map

| Layer | Path |
|---|---|
| Engine adapter prototype | `engine/boss_engine/services/security_scan.py` |
| API gate and read routes | `apps/api/app/routers/security_scan.py` |
| Console | `apps/web/app/security-scan-ui.tsx` |
| API tests | `apps/api/tests/test_security_scan_api.py` |
