---
title: "Inside the BOSS Engine"
description: "A self-hosted workflow engine for governed AI work, driven from the CLI or MCP."
audience: "Developers and platform engineers"
status: "launch-ready draft"
---

# Inside the BOSS Engine

Most agent demos end at a response. Production work begins there.

The BOSS engine is a self-hosted workflow engine for turning AI-assisted work
into durable, governed execution. It owns the work graph behind a task: the
roles involved, the policy and budget checks, the evidence produced, the
decision that needs a human, and the receipt that records the outcome.

It is deliberately usable without the BOSS web console. The engine ships as a
Python package and standalone container, backed by a self-hosted PostgreSQL
workspace. Drive it from the CLI, from its in-process tool layer, or from MCP.

## One engine, three interfaces

At the center is `boss_engine`.

```text
boss_engine/
  context.py   settings, database, and services at one application boundary
  tools/       typed read/write capability registry
  agent/       engine-native tool-calling agent
  mcp/         JSON-RPC MCP server over the same tool registry
  __main__.py  standalone CLI
```

The important design choice is that these are interfaces to the same engine,
not parallel implementations.

### 1. The tool layer

`boss_engine.tools` is the typed capability catalog. It exposes operational
reads such as project status, current work, task state, costs, and decisions;
its write tools begin or steer governed work. The native agent and MCP server
consume that one registry, which keeps an external agent client from acquiring
a second, unofficial way to operate BOSS.

### 2. The engine-native agent

`EngineAgent` reasons over the tool catalog. It can inspect a project, propose
the next action, and invoke permitted tools. The engine—not a prompt—keeps the
workflow boundary: write actions create durable records and remain subject to
the same policy, budget, receipt, and approval rules as every other path.

### 3. MCP

The MCP server exposes the same schema-typed tools through JSON-RPC. That means
an MCP-capable client can become a BOSS interface without receiving ambient
credentials or a private backdoor into the system. Use stdio for a local client
integration or the optional HTTP transport for a read-only community Mission
Control view.

## Start the engine locally

The engine needs a durable workspace before it executes governed work. Build
the image, connect PostgreSQL, then bootstrap once:

```bash
docker build -f engine/Dockerfile -t boss-engine .

docker network create boss
docker run -d --name boss-db --network boss \
  -e POSTGRES_DB=boss -e POSTGRES_USER=boss -e POSTGRES_PASSWORD=boss postgres:16

export DB='postgresql+psycopg://boss:boss@boss-db:5432/boss'
docker run --rm --network boss -e DATABASE_URL=$DB boss-engine bootstrap
docker run --rm --network boss -e DATABASE_URL=$DB boss-engine status
```

Then choose an interface:

```bash
# Inspect the available capability catalog
docker run --rm --network boss -e DATABASE_URL=$DB boss-engine tools

# Use MCP over stdio
docker run -i --rm --network boss -e DATABASE_URL=$DB boss-engine serve-mcp

# Serve read-only Mission Control and HTTP MCP
docker run -d --name boss-mcp --network boss -p 8791:8791 \
  -e DATABASE_URL=$DB boss-engine serve-mcp --http --host 0.0.0.0
```

Without `DATABASE_URL`, BOSS intentionally runs in catalog-only mode. It will
not pretend it can create a task, receipt, approval, or lease without a durable
place to record it.

## Bring your own model

The engine does not require a single hosted-model provider. The chat lane
honors an OpenAI-compatible base URL, and the code-agent lane has local
Ollama and vLLM providers. That makes the engine usable with a local daemon, a
private endpoint, or a provider of your choice while preserving the same
routing and accounting boundary.

For example, an Ollama-backed local setup can configure a compatible endpoint
and model, then use the normal engine entrypoints. The local model choice does
not remove the operational record around the work it performs.

## Why Postgres is part of the story

A task is not just a row called “complete.” BOSS needs durable coordination
semantics: leases prevent duplicate execution, receipts identify external
effects, approvals capture intent, and budget reservations prevent a worker
from spending first and explaining later.

That is why the OSS engine uses a self-hosted PostgreSQL workspace for governed
work. It is not a hosted control-plane dependency. It is the local system of
record that makes the execution accountable.

## The engine/control-plane split

The engine is the self-hosted core: workflow execution, policy-aware service
graph, CLI, tool layer, and MCP interface. The larger BOSS control plane adds
richer operational surfaces and enterprise capabilities on top of the same
core. Keeping that boundary explicit lets a team start with the engine while
retaining a path to deeper collaboration and operations without rewriting the
workflow model.

The next post follows one directive through that model—from intent to an
evidence-backed release.
