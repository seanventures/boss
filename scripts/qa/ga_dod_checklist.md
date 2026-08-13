# Community GA Definition of Done — command checklist

Maps the **8 Community DoD criteria** (see `docs/operations/GA_LIVING_PLAN.md`) to
runnable commands. Use this before claiming Community GA or private preview readiness.

**Branch context:** `ai/grok/community-ga`  
**Umbrella Plane:** BOSS-337  
**Living plan:** always update `docs/operations/GA_LIVING_PLAN.md` after live proof.

---

## Quick gates (automated)

| Gate | Command | Needs |
|------|---------|-------|
| Workflow E2E (CEO → task → budget → jobs) | `make qa-workflow-e2e` | `BOSS_API_KEY` (+ optional `BOSS_API_BASE`, `PROJECT_ID`) |
| Workflow structure only | `make qa-workflow-e2e DRY_RUN=1` | none |
| Factory E2E (WIP + code-tasks list) | `make qa-factory-e2e` | `BOSS_API_KEY` for live; dry-run if missing |
| Factory structure only | `make qa-factory-e2e DRY_RUN=1` | none |
| Full live dogfood (broader) | `make qa-live` | `BOSS_API_KEY` |
| OpenAPI factory slice parity | `make openapi-live-qa-parity` | local Python env |

Default live API base: `https://boss-api.local.sean.network`  
Default project: `boss-dogfood`

Env aliases accepted by the new gates:

- `BOSS_API_BASE` **or** `BOSS_API_URL`
- `PROJECT_ID` **or** `BOSS_PROJECT_ID`
- `BOSS_API_KEY` (never commit)

---

## DoD 1–8 → commands / evidence

### 1. BYOK model + infra keys

**Criterion:** Attach third-party model keys and infra API keys in System Configuration.

| Check | Command / action |
|-------|------------------|
| Provider definitions mounted | `curl -sS -H "X-Boss-Api-Key: $BOSS_API_KEY" "$BOSS_API_BASE/v1/provider-definitions" \| head` |
| Provider accounts list | included in `make qa-live` (`providers.definitions`, `providers.list`) |
| Console | System Configuration / ProviderOps UI on community edition |

**Pass signal:** definitions + accounts 200; keys stored as SecretRefs (not plaintext logs).

---

### 2. CEO command → task + budget path

**Criterion:** Issue a CEO command → durable task + plan + budget path.

| Check | Command |
|-------|---------|
| **Primary automated gate** | `make qa-workflow-e2e` |
| Broader dogfood | `make qa-live` (command.propose + budget.quote) |
| Structure only | `make qa-workflow-e2e DRY_RUN=1` |

Flow covered by `scripts/qa/workflow_e2e_gate.py`:

1. `GET /health`
2. `POST /v1/commands/propose`
3. `GET /v1/tasks`
4. `GET /v1/budget/status` **or** `POST /v1/budget/quote`
5. `GET /v1/jobs`

**Pass signal:** gate overall=PASS; all five critical steps 2xx.

---

### 3. Code → evidence → PR reliable

**Criterion:** Drive code work in sandbox → tests/evidence → PR with receipts (low DLQ).

| Check | Command |
|-------|---------|
| Factory surfaces up | `make qa-factory-e2e` (live list + WIP) |
| Optional create (budgeted!) | see below |
| Deadletters trend | `make qa-live` → `deadletters.list`; triage in Plane BOSS-342 |
| Full factory PR path | manual / weekly sealed run (GA plan B1) — **not** auto-merged by gates |

**Optional code-task create (spends budget — explicit allow only):**

```bash
export BOSS_API_KEY=…
export BOSS_API_BASE=https://boss-api.local.sean.network
export PROJECT_ID=boss-dogfood
export BOSS_FACTORY_OWNER=<github-owner>
export BOSS_FACTORY_REPO=<github-repo>
export BOSS_FACTORY_ALLOW_CREATE=1
python3 scripts/qa/factory_e2e_gate.py --create-code-task
# or: make qa-factory-e2e CREATE_CODE_TASK=1
```

**Pass signal:** create returns 2xx + `run_id`; worker reaches evidence/PR without DLQ class regression.  
**Not claimed green** until substrate P0s closed (rootfs / shared-tag deploy).

---

### 4. Previews reachable

**Criterion:** Open previews from Mission Control (non-loopback URLs).

| Check | Command / action |
|-------|------------------|
| Preview host inventory | ops / goldeneye runbook (not in workflow gate) |
| API | `GET /v1/previews?project_id=$PROJECT_ID` (manual) |
| Browser proof | Mission Control → View preview URL must not be `127.0.0.1` |

**Pass signal:** one non-loopback View proof recorded in GA living plan + Plane BOSS-341.

---

### 5. Honest unpaid / unconfigured UI

**Criterion:** No permanent 404 banners; Enterprise surfaces honest on community.

| Check | Command / action |
|-------|------------------|
| Console walk | LIVE_UI functional audit checklist |
| Edition runtime | `pytest tests/test_edition_runtime.py` (local) |
| Empty CI/CD CTA | visual: Deploy / targets empty state |

**Pass signal:** no enterprise upsell as if enabled; empty states have CTA, not hard 404.

---

### 6. GitHub Mission Control connected

**Criterion:** Real connection + binding **or** clear empty-state.

| Check | Command / action |
|-------|------------------|
| Live connections | console Mission Control GitHub page |
| Factory dual-plane honesty | GA plan A6 / BOSS-343 residual |

**Pass signal:** ≥1 connection + binding **or** setup empty-state (no silent fail).

---

### 7. Deploy path or honest empty CI/CD

**Criterion:** Minimal deploy **or** honest not-configured.

| Check | Command / action |
|-------|------------------|
| Targets list | `GET /v1/deployment-targets?project_id=$PROJECT_ID` |
| Console | Deploy empty CTA without 500s |

**Pass signal:** either one sandbox target succeeds, or empty state is honest.

---

### 8. Automated live gate

**Criterion:** One automated live gate fails the release if the vertical slice regresses.

| Check | Command |
|-------|---------|
| Historical dogfood | `make qa-live` |
| **Workflow vertical slice** | `make qa-workflow-e2e` |
| **Factory surface** | `make qa-factory-e2e` |
| Structure CI-safe | `make qa-workflow-e2e DRY_RUN=1 && make qa-factory-e2e DRY_RUN=1` |
| Unit tests for dry-run | `python3 -m pytest scripts/tests/test_workflow_e2e_gate.py scripts/tests/test_factory_e2e_gate.py -q` |

**Pass signal:**

- Dry-run gates exit 0 in CI without secrets.
- Live `qa-workflow-e2e` + `qa-live` exit 0 against dogfood after every shared-tag deploy.
- Reports under `artifacts/qa/` (or `/tmp/boss-qa/artifacts/`).

---

## Suggested pre-release sequence

```bash
# 1) Always-safe structure (no secrets)
make qa-workflow-e2e DRY_RUN=1
make qa-factory-e2e DRY_RUN=1
python3 -m pytest scripts/tests/test_workflow_e2e_gate.py scripts/tests/test_factory_e2e_gate.py -q

# 2) Live vertical slice (requires key)
export BOSS_API_KEY=…   # never commit
export BOSS_API_BASE=https://boss-api.local.sean.network
export PROJECT_ID=boss-dogfood
make qa-workflow-e2e
make qa-factory-e2e
make qa-live

# 3) Record evidence in GA living plan + Plane BOSS-337 / BOSS-351
```

---

## Report artifacts

| Gate | Default report path pattern |
|------|-----------------------------|
| workflow | `artifacts/qa/workflow-e2e-gate-<utc>.json` |
| factory | `artifacts/qa/factory-e2e-gate-<utc>.json` |
| qa-live | `artifacts/qa/live-dogfood-gate-<utc>.json` |

Override with `BOSS_WORKFLOW_GATE_OUTPUT`, `BOSS_FACTORY_GATE_OUTPUT`, `BOSS_LIVE_GATE_OUTPUT`.

---

## Related docs

- `docs/operations/GA_LIVING_PLAN.md` — living DoD status
- `docs/operations/E2E_COMPLETION_PLAN.md` — workstreams W0–W5
- `docs/operations/ga-readiness-2026-08-12/GA_READINESS_SCORECARD.md`
- `scripts/qa/live_dogfood_gate.py` — broader live dogfood
- `scripts/qa/workflow_e2e_gate.py` — workflow vertical slice
- `scripts/qa/factory_e2e_gate.py` — factory surface + optional create
