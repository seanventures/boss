# YouTube Ep2 — Execute a sample project end-to-end with BOSS

| Field | Value |
|---|---|
| **Plane** | BOSS-289 |
| **Duration target** | 12–15 minutes |
| **Audience** | Engineering leaders and developers evaluating governed code delivery |
| **Status** | Script package delivered (recording is production work outside the repo) |
| **Companion** | [`docs/marketing/video/02-governed-sample-project.md`](../video/02-governed-sample-project.md) |
| **Canonical sample** | Private real-only `seanheiney/boss-sandbox` (not a customer repo) |
| **Date** | 2026-08-12 |

## Learning outcomes

1. A BOSS code task has explicit instructions, acceptance criteria, tests, and a target repository **before** an agent starts.
2. Work moves through **visible phases** (plan → implement → test → review packet), not unobservable chat.
3. Passing tests are **evidence for a review decision**, not authority to merge.
4. Repository mutation stays behind scoped connection + human gate + **receipt / audit** records.

## Truth constraints

- Prefer the **local / read-only / fail-closed** path unless live GitHub write is pre-verified.
- Never demo ambient GitHub access or paste PATs on camera.
- If `main` protection / Actions are incomplete on the sample, **say so** and stop before the PR segment (or use a dry-run / review-packet-only cut).
- Do not claim unattended autonomous company operation.

## Pre-flight (unrecorded)

```bash
# Ep1 stack still available (Postgres + engine image)
docker ps --filter name=boss-ep1
export BOSS_DATABASE_URL='postgresql+psycopg://boss:boss@boss-ep1-db:5432/boss'

# Read-only inspect sample (requires GH auth in unrecorded shell only)
gh repo view seanheiney/boss-sandbox --json name,isPrivate,defaultBranchRef
gh api repos/seanheiney/boss-sandbox/branches/main/protection 2>/dev/null || echo "protection not set — PR segment gated"
```

Checklist:

- [ ] Ep1 image `boss-engine:ep1` and DB healthy
- [ ] Sample repo is the sandbox, not production customer code
- [ ] Decision: record **with** live PR or **without** (packet-only) based on protection
- [ ] No secrets in shell history that will be recorded

## Recording outline

### 0:00–1:00 — Premise

> “Last time we started the engine. Today we create a governed code task against a tiny sample repo and walk the evidence trail: scope, implementation, tests, review packet, and receipts.”

### 1:00–3:00 — Show the sample contract

On screen (sanitized):

- Small Node project: `npm run lint`, `npm test`
- Intentional trivial change (e.g. add a pure function + unit test)
- Acceptance criteria written **before** the agent runs

### 3:00–6:00 — Create the code task (CLI or MCP)

Demo commands (adjust to the image’s current CLI surface; see `engine/README.md`):

```bash
# Example shape — use the shipped CLI/MCP tool names from the live image
docker run --rm --network boss-ep1 \
  -e BOSS_DATABASE_URL="$BOSS_DATABASE_URL" \
  boss-engine:ep1 create-code-task \
  --title "Add greet() helper with unit test" \
  --repo "seanheiney/boss-sandbox" \
  --acceptance "lint green; npm test green; greet returns Hello, <name>"
```

If create is MCP-only in the cut you ship, open Ep3-style MCP tools and call `create_code_task` instead — stay consistent with the tool list from `boss-engine:ep1 tools`.

### 6:00–10:00 — Watch phases + evidence

Show (read-only):

```bash
docker run --rm --network boss-ep1 \
  -e BOSS_DATABASE_URL="$BOSS_DATABASE_URL" \
  boss-engine:ep1 status

# Or HTTP MC from Ep1
open http://127.0.0.1:8791/
```

Narrate:

- Task / job / lease visibility
- Sandbox boundary (local subprocess vs microVM — do not claim Firecracker unless live)
- Test results as **receipt-backed evidence**

### 10:00–13:00 — Review packet + decision gate

> “Passing tests are not merge authority. A human (or policy) still reviews the packet. BOSS records the decision.”

If live PR is **gated off**:

> “We stop at the review packet. Opening a PR requires a scoped connection and repository protection we deliberately keep fail-closed for this recording.”

If live PR is **pre-verified** (unrecorded setup complete):

- Show PR URL + checks
- Show receipt / decision inbox entry
- **Do not merge** unless that is an explicit approved segment

### 13:00–15:00 — Close the loop

Summary diagram verbally:

```text
Outcome → Code task → Sandboxed work → Tests → Review packet → Decision → Receipts
```

CTA → Ep3: same loop from Claude/Cursor via MCP.

## Segment gate matrix

| Segment | Required proof | If missing |
|---|---|---|
| Create task | Local engine + DB | Abort record |
| Run implement | Worker/sandbox available | Use mock/narrated cut + honest label |
| Live PR | Connection + protection + no secret leakage | Packet-only cut |
| Merge | Explicit approval | Skip |

## Evidence for Plane Done

- This script package: `docs/marketing/youtube/EP2_SCRIPT.md`
- Companion kit: `docs/marketing/video/02-governed-sample-project.md`
- **Recording is production work outside the repository.**
