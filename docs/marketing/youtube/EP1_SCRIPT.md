# YouTube Ep1 — Install & run BOSS open-source locally (Docker)

| Field | Value |
|---|---|
| **Plane** | BOSS-288 |
| **Duration target** | 12–15 minutes |
| **Audience** | Developers evaluating the open-source BOSS Engine |
| **Status** | Script package delivered (recording is production work outside the repo) |
| **Companion** | [`docs/marketing/video/01-install-and-run-boss-engine.md`](../video/01-install-and-run-boss-engine.md) |
| **Date** | 2026-08-12 |

## Learning outcomes

By the end of the episode the viewer can:

1. Build the standalone `boss-engine` image from this monorepo.
2. Start a disposable local PostgreSQL and bootstrap durable BOSS state.
3. Inspect tools / project status via CLI.
4. Start the HTTP MCP + read-only community Mission Control surface.
5. (Optional) attach a BYO model key without pasting secrets on camera.

## Truth constraints (do not break on camera)

- Call it a **self-hosted engine + PostgreSQL**, not a zero-database demo.
- Do **not** show API keys, production DB URLs, or customer repos.
- The browser UI at `serve-mcp --http` is **read-only community MC**, not the commercial multi-user console.
- Do not claim a task/PR/deploy succeeded unless it is visible in the recording.
- Public OSS packaging remains gated by **BOSS-300** (licensing decision). Say “engine package as shipped in this repo,” not “already published to a public org.”

## Pre-flight (unrecorded)

```bash
# From monorepo root
docker version && docker info >/dev/null
docker ps
# Clean names from prior demos
docker rm -f boss-ep1-db boss-ep1-mcp 2>/dev/null || true
docker network rm boss-ep1 2>/dev/null || true
```

Checklist:

- [ ] Docker has room for image + Postgres
- [ ] No containers named `boss-ep1-db` / `boss-ep1-mcp`
- [ ] Browser ready for `http://127.0.0.1:8791/`
- [ ] Optional model key available in a **separate unrecorded** shell

## Recording outline

### 0:00–0:45 — Premise

> “BOSS Engine is the self-hosted workflow core for governed AI work. A model can generate text, but the engine records the task, policy checks, decisions, budget controls, and evidence around the work. Today we run that engine locally against a local PostgreSQL workspace.”

Show: repo root, `engine/README.md`, `engine/Dockerfile`.

### 0:45–2:30 — Build the image

```bash
docker build -f engine/Dockerfile -t boss-engine:ep1 .
```

Say: “This image is the portable engine package — CLI, MCP server, and bootstrap — without the commercial control-plane console.”

### 2:30–5:00 — Postgres + network

```bash
docker network create boss-ep1
docker run -d --name boss-ep1-db --network boss-ep1 \
  -e POSTGRES_USER=boss -e POSTGRES_PASSWORD=boss -e POSTGRES_DB=boss \
  postgres:16-alpine
# wait until ready
docker exec boss-ep1-db pg_isready -U boss
```

### 5:00–7:30 — Bootstrap durable state

```bash
export BOSS_DATABASE_URL='postgresql+psycopg://boss:boss@boss-ep1-db:5432/boss'
docker run --rm --network boss-ep1 \
  -e BOSS_DATABASE_URL="$BOSS_DATABASE_URL" \
  boss-engine:ep1 bootstrap
```

Say: “Governed work needs durable state for tasks, leases, receipts, decisions, and budgets. Bootstrap creates that schema.”

### 7:30–10:00 — CLI inspect

```bash
docker run --rm --network boss-ep1 \
  -e BOSS_DATABASE_URL="$BOSS_DATABASE_URL" \
  boss-engine:ep1 tools

docker run --rm --network boss-ep1 \
  -e BOSS_DATABASE_URL="$BOSS_DATABASE_URL" \
  boss-engine:ep1 status
```

Alternate (if image entrypoint differs, use module form documented in `engine/README.md`):

```bash
docker run --rm --network boss-ep1 -e BOSS_DATABASE_URL="$BOSS_DATABASE_URL" \
  boss-engine:ep1 python -m boss_engine --help
```

### 10:00–13:00 — MCP HTTP + read-only MC

```bash
docker run -d --name boss-ep1-mcp --network boss-ep1 \
  -p 8791:8791 \
  -e BOSS_DATABASE_URL="$BOSS_DATABASE_URL" \
  boss-engine:ep1 serve-mcp --http --host 0.0.0.0 --port 8791
```

Open browser: `http://127.0.0.1:8791/`  
Say: “This is the read-only community surface — status and evidence, not full multi-tenant Mission Control.”

### 13:00–15:00 — Optional model config (no secrets on screen)

Show env var **names only**:

```text
BOSS_MODEL_PROVIDER=openai   # or anthropic / xai / google
# BOSS_OPENAI_API_KEY is set in an unrecorded shell
```

Close with: “Next episode: a governed sample code task end-to-end.”

## B-roll / cutaways

- `engine/pyproject.toml` version line
- `docs/community/QUICKSTART.md`
- Receipts / decisions concept diagram (if available in `docs/product/diagrams/`)

## Outro CTA

- Link to monorepo / engine package docs
- Ep2: sample project end-to-end
- Ep3: MCP from Claude / Cursor / Copilot

## Recording production notes (outside repo)

| Item | Owner | Notes |
|---|---|---|
| Screen capture + voiceover | Production | Keep terminal font ≥16pt |
| Thumbnail | Design | “Docker · 15 min · BOSS Engine” |
| Captions | Production | Auto + cleanup for CLI commands |
| Publish gate | Product | Prefer after BOSS-300 if claiming “public OSS” |

## Evidence for Plane Done

- This script package path: `docs/marketing/youtube/EP1_SCRIPT.md`
- Companion long-form kit: `docs/marketing/video/01-install-and-run-boss-engine.md`
- **Recording is production work outside the repository** and is not required to close the script deliverable.
