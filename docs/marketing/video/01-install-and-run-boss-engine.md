---
title: "Video 01 — Install and Run the BOSS Engine Locally"
audience: "Developers evaluating the open-source BOSS Engine"
duration_target: "12–15 minutes"
status: "recording-ready draft"
---

# Video 01 — Install and Run the BOSS Engine Locally

This is the exact recording script and checklist for the first BOSS Engine
training video. It shows a self-hosted engine backed by local PostgreSQL. It
does not claim that a hosted BOSS account, cloud deployment, or model-provider
credential is required for the baseline demonstration.

## What the viewer should learn

By the end, a viewer can:

1. Build the standalone `boss-engine` image from the repository root.
2. Start a local PostgreSQL workspace.
3. Bootstrap durable BOSS state.
4. Inspect the tool layer and project status.
5. Start read-only Mission Control and the HTTP MCP endpoint.
6. Optionally connect a model provider and use the interactive agent without
   exposing a credential in the recording.

## Truth constraints for the presenter

- Call it a **self-hosted engine plus PostgreSQL**, not a zero-database demo.
  Governed work needs durable state for tasks, leases, receipts, decisions, and
  budgets.
- Do not show an API key, database URL containing production credentials, or a
  real customer repository.
- The browser UI served by `serve-mcp --http` is the read-only community
  Mission Control view. It is not the commercial multi-user console.
- Do not claim a task, PR, deployment, or model chat succeeded unless it is
  visible in the recording. If a model key is unavailable, use the optional
  section as a configuration walkthrough and say so.

## Pre-flight checklist

Run these before recording, from a clean terminal at the monorepo root:

```bash
docker version
docker info >/dev/null
docker ps
```

- [ ] Docker daemon is running and has room for the image plus PostgreSQL.
- [ ] No earlier demo container uses the names `boss-ep1-db` or
  `boss-ep1-mcp`.
- [ ] The terminal uses a temporary local database only.
- [ ] A browser is available for `http://127.0.0.1:8791/`.
- [ ] Optional: a separate shell has a model credential or local-model endpoint
  ready; never paste it into the recorded terminal.

## The recording script

### 0:00–0:45 — Set the premise

Say:

> “BOSS Engine is the self-hosted workflow core for governed AI work. A model
> can generate text, but the engine records the task, policy checks, decisions,
> budget controls, and evidence around the work. Today we will run that engine
> locally against a local PostgreSQL workspace.”

Show the repository root and `engine/README.md`. Do not spend time on the
commercial console.

### 0:45–2:15 — Build the standalone image

Run:

```bash
docker build -f engine/Dockerfile -t boss-engine:ep1 .
```

Say:

> “This build vendors the engine, the database schema, the SpecPack policies
> and workflows, and the traceability data it needs. The standalone image does
> not depend on the BOSS HTTP API or web console.”

Wait for a successful build. If the build fails, stop the recording and repair
the environment—do not cut around the failure.

### 2:15–3:30 — Start a local durable workspace

Run:

```bash
docker network create boss-ep1
docker run -d --name boss-ep1-db --network boss-ep1 \
  -e POSTGRES_DB=boss \
  -e POSTGRES_USER=boss \
  -e POSTGRES_PASSWORD=boss \
  postgres:16

export DB='postgresql+psycopg://boss:boss@boss-ep1-db:5432/boss'
```

Say:

> “PostgreSQL is not an optional implementation detail here. It is where the
> engine keeps the durable work graph and audit record that make a governed run
> recoverable.”

Wait until `docker ps` shows PostgreSQL running. If startup is slow, narrate
that the next command initializes the schema rather than guessing that the
database is ready.

### 3:30–5:00 — Bootstrap, inspect tools, and prove the workspace exists

Run:

```bash
docker run --rm --network boss-ep1 -e DATABASE_URL="$DB" boss-engine:ep1 bootstrap
docker run --rm --network boss-ep1 -e DATABASE_URL="$DB" boss-engine:ep1 tools
docker run --rm --network boss-ep1 -e DATABASE_URL="$DB" \
  boss-engine:ep1 --project boss-dogfood status
```

Narrate the resulting facts:

- `bootstrap` creates the schema and seeds the default organization, project,
  role pack, policy defaults, and budget policy.
- `tools` is the schema-typed capability catalog—not a second hidden API.
- `status` reads the resulting project from the durable workspace.

### 5:00–7:00 — Show MCP and read-only Mission Control

Run:

```bash
docker run -d --name boss-ep1-mcp --network boss-ep1 -p 8791:8791 \
  -e DATABASE_URL="$DB" \
  boss-engine:ep1 serve-mcp --http --host 0.0.0.0

curl -fsS http://127.0.0.1:8791/health
```

Open `http://127.0.0.1:8791/` in the browser. Point out that the view is
read-only and is rendered through the same engine tool layer. Then show the
MCP tool catalog with a single JSON-RPC request:

```bash
curl -fsS http://127.0.0.1:8791/mcp \
  -H 'content-type: application/json' \
  -d '{"jsonrpc":"2.0","id":1,"method":"tools/list"}'
```

Say:

> “Any MCP client can use this interface, but it receives the governed BOSS
> capabilities—not ambient repository or cloud authority.”

### 7:00–9:30 — Optional: attach a model without revealing it

Use this only when a safe, disposable provider credential or local model
endpoint is available. Put the secret in the shell environment before screen
recording, or use a local Ollama/vLLM endpoint. Never type the key on screen.

For an OpenAI-compatible local endpoint, the configuration shape is:

```bash
export BOSS_LLM_ENGINE=openai
export OPENAI_BASE_URL=http://localhost:11434/v1
export OPENAI_MODEL=qwen2.5-coder:14b
# Set OPENAI_API_KEY outside the visible recording if the endpoint requires one.
```

Then demonstrate a read-only question:

```bash
docker run -it --rm --network boss-ep1 \
  -e DATABASE_URL="$DB" \
  -e BOSS_LLM_ENGINE -e OPENAI_BASE_URL -e OPENAI_MODEL -e OPENAI_API_KEY \
  boss-engine:ep1 ask "How many tasks are running right now?"
```

If the model lane is unavailable, say: “The engine and MCP layer are working;
this optional model connection needs the viewer’s own provider or local-model
configuration.” Do not substitute a fabricated response.

For an interactive session, run the same container command with
`boss-engine:ep1 chat`; add `--allow-writes` only when deliberately showing a
governed write path with a disposable local workspace.

### 9:30–11:00 — Explain the boundary

Show `tools` once more and say:

> “The value is not a chatbot wrapper. The engine gives work a durable owner,
> a policy boundary, a cost and evidence record, and an MCP surface. Write
> actions remain governed: they create durable records and are still subject to
> approvals, leases, budget reservations, and receipts.”

Close by pointing viewers to `engine/README.md` and the governed coding
pipeline documentation for the next layer of the system.

## Post-recording verification checklist

- [ ] The image built in the recorded environment.
- [ ] `bootstrap`, `tools`, and `status` were visibly successful.
- [ ] `/health` and `tools/list` responded from the HTTP MCP container.
- [ ] The browser showed only the read-only community Mission Control surface.
- [ ] Any optional model response is visibly tool-grounded and contains no
  credential or customer data.
- [ ] Captions identify the engine image tag and local-only database.
- [ ] The final cut includes no unverified claim about PRs, deployments,
  provider writes, or production readiness.

## Cleanup

After recording, remove the local demo resources:

```bash
docker rm -f boss-ep1-mcp boss-ep1-db
docker network rm boss-ep1
```

Keep the image only if it is useful for a follow-up recording:

```bash
docker image rm boss-engine:ep1
```
