# BOSS — Fully-Local Run Guide & Use-Case Walkthrough

Recipe **A**: run the entire BOSS governance OS on your laptop with **zero cloud
dependencies**. The only infrastructure is a local Postgres container. Verified working on
this machine (macOS, 2026-06-05).

---

## What is running right now

| Service | URL / Port | Process | Status |
|---------|-----------|---------|--------|
| **Web console** (Next.js) | http://localhost:**3001** | `pnpm --filter boss-web dev` | ✅ HTTP 200 |
| **Control plane API** (FastAPI) | http://localhost:**8000** | `uvicorn apps.api.app.main:app` | ✅ `/health` ok |
| **Worker** (Python) | — (polls API) | `python -m apps.worker.boss_worker.main` | ✅ polling |
| **Postgres 16** | localhost:**5432** | docker container `infra-db-1` | ✅ healthy |

> Web is on **3001** (not the default 3000) because port 3000 was already in use on this
> machine. Open **http://localhost:3001** in your browser to see the executive console.

`/health` reports `database_connected:true, github_configured:false, openai_configured:false`
— exactly the fully-local posture: the platform is fully operational; the two cloud
integrations are simply absent (and the system fails *closed* when they're needed).

---

## Fixes that were required to boot (important)

A clean checkout did **not** boot as-is. Three fixes were applied to get a fresh install
running. These are real reproducibility issues worth upstreaming.

1. **`pydantic` is unpinned in `requirements.txt`.** It pins `pydantic-settings==2.10.1`
   but not `pydantic`, so a fresh 2026 install pulls `pydantic==2.13.4`. Pinned to
   `pydantic==2.11.7` in the venv. *(Recommend: add `pydantic==2.11.7` to
   `apps/api/requirements.txt` and `apps/worker/requirements.txt`.)*

2. **Shared `.env` rejected by the Settings models.** Both `apps/api/app/config.py` and
   `apps/worker/boss_worker/config.py` load the **same** root `.env`, but each model only
   declares its own keys and neither set an `extra` policy — so the API rejected the
   worker/Makefile keys (`BOSS_POSTGRES_PORT`, `BOSS_CONTROL_PLANE_URL`,
   `BOSS_LEASE_TTL_SECONDS`) as forbidden extras. Added `extra="ignore"` to both
   `SettingsConfigDict(...)`. *(One-line fix in each file; clearly the intended behavior.)*

3. **Schema ordering bug in `db/schema.sql`.** Line ~190 declared
   `account_connect_auth_sessions.receipt_id TEXT REFERENCES receipts(id)`, but the
   `receipts` table isn't created until line ~734 → `relation "receipts" does not exist`.
   Made the column a plain `TEXT` and re-attached the foreign key with an idempotent
   `DO $$ … $$` guard immediately after `receipts` is created. *(Preserves referential
   integrity and keeps the schema re-runnable.)*

> These edits are local to your working copy. If you `git diff` you'll see exactly:
> `apps/api/app/config.py`, `apps/worker/boss_worker/config.py`, `db/schema.sql`.

---

## From-scratch setup (what was done)

```bash
cd ~/startup/sean/boss

# 0. Prereqs: Docker running, Node 20+ , pnpm 9, Python 3.x
corepack enable && corepack prepare pnpm@9.15.0 --activate   # if pnpm missing

# 1. Local env — all secrets left BLANK = fully local, fail-closed
cp .env.example .env

# 2. Install toolchain (pnpm workspace + python venv)
make install
.venv/bin/pip install 'pydantic==2.11.7'      # fix #1 above

# 3. (apply fixes #2 and #3 — already done in this working copy)

# 4. Bring up Postgres + apply schema + seed the dogfood org
make seed
make smoke        # optional: schema/bootstrap smoke check → "Smoke check passed."
```

Seed result: **1 project** (`boss-dogfood`), **17 roles**, **9 member/actors**, **`core_v1`
pack** installed.

---

## Start / stop the stack

**Start (3 background processes):**
```bash
cd ~/startup/sean/boss
DB='postgresql+psycopg://boss:boss@localhost:5432/boss'

docker compose -f infra/docker-compose.yml up -d db            # Postgres

DATABASE_URL=$DB nohup .venv/bin/python -m uvicorn apps.api.app.main:app \
  --host 0.0.0.0 --port 8000 > .local-logs/api.log 2>&1 &      # API

DATABASE_URL=$DB nohup .venv/bin/python -m apps.worker.boss_worker.main \
  > .local-logs/worker.log 2>&1 &                              # Worker

PORT=3001 nohup pnpm --filter boss-web dev > .local-logs/web.log 2>&1 &   # Web
```

> Alternatively `make dev` runs all three in the foreground via `concurrently` — but it
> uses web port 3000. Use the background commands above (port 3001) when 3000 is taken.

**Stop:**
```bash
pkill -f 'uvicorn apps.api.app.main'
pkill -f 'boss_worker.main'
pkill -f 'next dev'
docker compose -f infra/docker-compose.yml down       # add -v to wipe the DB volume
```

**Logs:** `tail -f .local-logs/{api,worker,web}.log`

---

## Use-case walkthrough: a CEO outcome → governed work

This is the core loop (command → task → job → lease → plan → decision → approval →
budget reservation → next job). Everything below ran locally with **no API keys**.

### 1 — CEO submits an outcome command
```bash
curl -s -X POST http://localhost:8000/v1/commands/propose \
  -H 'Content-Type: application/json' \
  -d '{"project_id":"boss-dogfood","actor":"ceo",
       "command":"Add a /healthz readiness endpoint to the web app and document it",
       "constraints":{"max_cost_usd":5,"deadline_hours":24}}'
# → { "status":"accepted", "task_id":"tsk_…", "job_id":"job_…", "artifact_id":"art_…" }
```
Creates a **Task**, a `command_brief` **Artifact**, and a `plan.command` **Job**.

### 2 — The worker leases and executes the job (≈ within 15s poll)
The worker claims the job (`FOR UPDATE SKIP LOCKED`), heartbeats (`LEASED → RUNNING`),
generates a structured **`plan_draft`** artifact *locally* (no LLM), completes the job, and
opens a **blocking Decision**.
```bash
curl -s "http://localhost:8000/v1/jobs/<job_id>?project_id=boss-dogfood"   # status: succeeded
```

### 3 — Review the Decision Inbox + the generated plan
```bash
curl -s "http://localhost:8000/v1/decisions/inbox?project_id=boss-dogfood"
curl -s "http://localhost:8000/v1/artifacts/<artifact_id>?project_id=boss-dogfood"
```
The decision is **risk-tier 2** with `default_action: hold` → requires **CEO** approval
(per the `default_safety` policy). The plan artifact contains `objectives`, `steps`,
`risks`, `done_when`.

### 4 — CEO approves → BOSS auto-reserves budget and queues the next job
```bash
curl -s -X POST http://localhost:8000/v1/decisions/<decision_id>/approve \
  -H 'Content-Type: application/json' \
  -d '{"project_id":"boss-dogfood","actor":"ceo","note":"Proceed"}'
```
Approval is not a dead end — it triggers the **follow-on**: `quote → reserve` a budget
envelope and queue a `software_factory.propose_pr` job. Visible in the event chain:
```
decision.approved → budget.reservation_created → job.created
                  → budget.reservation_attached → decision.follow_on_job_queued
```

### 5 — Ask Metatron "what changed?" (live status, not chat)
```bash
curl -s -X POST http://localhost:8000/v1/metatron/query \
  -H 'Content-Type: application/json' \
  -d '{"project_id":"boss-dogfood","question":"what changed and what needs my attention?"}'
# → answer + freshness_at + typed references (no LLM involved)
```

### 6 — The full audit / event chain (durable, ordered)
```bash
curl -s "http://localhost:8000/v1/events?project_id=boss-dogfood&limit=12"
```
```
task.created → job.leased → job.heartbeat → job.succeeded → plan.created
→ artifact.created → decision.created → decision.approved
→ budget.reservation_created → job.created → reservation_attached → follow_on_job_queued
```
Also: `GET /v1/audit/export?project_id=boss-dogfood` reconstructs the whole
command→task→job→decision→artifact→receipt chain.

---

## The local ↔ cloud boundary (the key takeaway)

When the follow-on `software_factory.propose_pr` job ran, it needed **OpenAI** (codegen)
and **GitHub** (PR). With no credentials it did **not** crash or mock — it **failed
closed into a visible Decision**:

```
Decision: "OpenAI integration is not configured"   (status: pending, risk_tier 2)
Recommendation: provide_openai_api_key
```

And the budget reservation it had taken was **released** (budget returned to
`green / $10.00 cap / $0.00 reserved`) rather than left locked. This is the whole design
thesis in one screen:

- **Everything up to producing real code/PRs runs 100% locally** — commands, planning,
  jobs, leasing, receipts, decisions, approvals, budget reservations, audit, the console,
  Metatron.
- **The two cloud APIs (OpenAI + GitHub) are only needed at the "produce a real artifact"
  step**, and their absence surfaces as a clean, actionable decision — never a silent
  failure.

### To extend to a real software-factory run (recipe B)
Add to `.env` and restart the API + worker:
```bash
OPENAI_API_KEY=sk-…
BOSS_GITHUB_TOKEN=ghp_…          # access to a seanheiney/boss-sandbox repo
# then: make real-smoke-pr      (reservation → worker → OpenAI plan → real GitHub PR)
```
Everything else (DigitalOcean provisioning, Slack/email real sends, merge/deploy) stays
flag-gated off until you explicitly enable it.

---

## Quick reference

| Action | Command |
|--------|---------|
| Web console | open http://localhost:3001 |
| API health | `curl localhost:8000/health` |
| Auth mode | `curl localhost:8000/v1/auth/session` |
| List actors | `curl "localhost:8000/v1/members?project_id=boss-dogfood"` |
| Budget | `curl "localhost:8000/v1/budget/status?project_id=boss-dogfood&scope_type=project&scope_id=boss-dogfood"` |
| Dead-letters | `curl "localhost:8000/v1/deadletters?project_id=boss-dogfood"` |
| DB shell | `docker exec -it infra-db-1 psql -U boss -d boss` |

**Valid actors** (seeded) and key permissions: `ceo` / `executive_console` (full,
incl. `command_submit`), `cto`, `cao`, `coo`, `release_manager`, `vp_agents`, `metatron`,
`system`. Write APIs reject unknown actors or actors lacking the required permission.
