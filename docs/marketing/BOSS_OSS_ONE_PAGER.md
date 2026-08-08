# BOSS - Governed AI Labor

## The operating system for AI labor

BOSS turns a directive into governed execution. It makes AI work inspectable
through role-scoped execution, explicit decisions, budget controls, durable
receipts, and evidence that survives the run.

## The control loop

1. **Intent** - a directive enters a governed work queue.
2. **Evidence** - roles, tests, and adversarial review make the case.
3. **Decision** - a human approval is recorded before irreversible work.
4. **Receipt** - consequential actions leave an inspectable audit trail.

## What ships in the OSS engine

- Self-hosted engine with a durable PostgreSQL workspace.
- CLI and MCP interface for external agent clients.
- Local model lanes for Ollama, vLLM, and OpenAI-compatible endpoints.
- Read-only community Mission Control for operational visibility.
- Receipts, budget reservations, approvals, and leased jobs for governed work.

## Start locally

```bash
docker build -f engine/Dockerfile -t boss-engine .
```

Connect a self-hosted PostgreSQL database, run `boss-engine bootstrap`, then
drive BOSS from the CLI or any MCP-capable client. The OSS engine is a durable
control plane, not an untracked agent wrapper.

## Design partners

For teams that need managed operations, richer collaboration surfaces, or
enterprise rollout controls, BOSS pairs the engine with a commercial control
plane. Start with the engine. Keep the evidence.
