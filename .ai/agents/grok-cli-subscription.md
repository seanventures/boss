# Grok CLI subscription handoff

**Plane:** BOSS-433 (In Progress). **Branch:** `ai/codex/grok-cli-subscription`, from
`origin/main` `9a72bd9a`. **Owner:** Codex TUI/API agent.

The official Grok Build CLI supports device authentication and headless `--single` coding
turns. BOSS now declares a Grok CLI harness, checks for a private worker-owned local
session, strips API-key fallback variables, proves a read-only turn on connect, records
`xai_grok_build` as an operator-scoped subscription, and reports the code-engine setting.
It does not add a Grok subscription chat adapter or make the session portable.

Coordination: the installer agent owns Dockerfile/Compose CLI packaging and the persistent
`/home/boss/.grok` volume; this branch owns only harness, setup, tests, and Grok docs.
The API image is the current coding CLI host; the worker can also run engine code paths.

Qualification remaining: rebuild API and worker images with the official CLI, authenticate
inside the API container as `boss`, run the default setup proof, then complete one governed
code task and inspect diff, tests, budget/telemetry, receipt, and event evidence. This agent's
host is signed out, so a live subscription turn was not run in this branch. Keep the harness
flag `verified=false` and BOSS-433 open until that evidence exists.
