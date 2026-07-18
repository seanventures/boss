# BOSS ↔ Plane Integration Spec

**Status:** **Phase 1 implemented** (branch `feat/plane-webhook-intake`) — inbound signed webhook →
auto code‑task, behind the `PLANE_INTEGRATION_ENABLED` flag (default off). Awaiting Sean's P0 setup
(webhook + secret + reachability) to go live. Phases 2–4 not started. **Owner:** paramg. **Date:** 2026‑07‑03 (rev 2026‑07‑17).

> **Implementation note (Phase 1).** The inbound endpoint is **`POST /v1/webhooks/plane`** (mirrors the
> existing `/v1/webhooks/github` ingress and its auth‑exemption), **not** `/v1/integrations/plane/webhook`
> as originally sketched below. It verifies **`X‑Plane‑Signature`** (hex HMAC‑SHA256 of the raw body,
> `sha256=` prefix tolerated), filters (watched project + `boss:` label + `action==created`), maps the
> issue → `start_code_task`, and dedupes on `plane_issue_id` stored in `finalize_ctx`. Service:
> `apps/api/app/services/plane_webhooks.py`; tests: `apps/api/tests/test_plane_webhooks.py` (18).

**Goal:** let a human file a ticket in **Plane** (self‑hosted Jira/Linear‑style tracker) and have **BOSS automatically pick it up and run the job** — starting with coding/bug/design tickets, filtered so BOSS ignores everything else. Plane becomes the **human intake + tracking layer**; BOSS's jobs pipeline is the **execution engine** underneath.

---

## 0. Decisions (up front)

- **Plane edition:** self‑hosted **open‑source Community Edition (free)**. It includes everything we need — projects, labels, **API tokens, and webhooks**. No paid tier required to build or run this. (Paid Cloud/Enterprise only matters later for SSO/support.)
- **License:** Plane is AGPL, but we integrate **via its API + webhooks as separate services** → this does **not** impose AGPL on BOSS. (Only forking + hosting a modified Plane would.)
- **Mechanism:** Plane **webhook** (issue events) → BOSS receiver → job dispatch. Later, BOSS → Plane **REST API** for status sync.
- **Trigger scope:** BOSS listens for **specific task types via a label convention** (`boss:coding`, `boss:bug`, `boss:design`), not all tickets. You control what BOSS runs by labeling.

---

## 1. Architecture

```
   Plane (Sean-hosted)                         BOSS (goldeneye)
   ┌───────────────────┐   webhook (signed)    ┌────────────────────────────────┐
   │ create issue      │──────────────────────▶│ POST /v1/integrations/plane/    │
   │ label boss:coding │   issue.created        │      webhook                    │
   │                   │                        │  verify sig → filter (project + │
   │                   │                        │  label) → map → start job       │
   │                   │◀───────────────────────│  (Phase 2) PATCH issue state +  │
   │  status updates   │   REST API (token)     │  comment PR link                │
   └───────────────────┘                        └───────────────┬────────────────┘
                                                                 ▼
                                                   BOSS jobs pipeline (code task →
                                                   micro-VM → gate → PR → Decision)
```

- **Inbound (v1):** Plane webhook → boss‑api endpoint. Auth = **webhook signature** (not the API key), so the endpoint is auth‑exempt but signature‑verified.
- **Outbound (v2):** boss‑api → Plane REST API with a token, to move the ticket through states and comment.
- **Runs on:** both on the goldeneye tailnet. **Reachability both ways is a prerequisite** (same class of issue we solved for the sandbox‑service — Plane must reach boss‑api's webhook URL, and boss‑api must reach Plane's API).

---

## 2. The flow (v1, one‑way)

```
1. Human creates a Plane issue, labels it `boss:coding` (or boss:bug / boss:design).
2. Plane fires issue.created webhook → BOSS /v1/integrations/plane/webhook.
3. BOSS verifies the signature, then filters:
     - is the project in the watch list?      (else ignore)
     - does it carry a boss:<type> label?      (else ignore)
     - is action == "created"?                  (ignore updates → no loops)
4. Map the issue → a BOSS job:
     - boss:coding / boss:bug → start_code_task(title, instructions=description,
       acceptance_criteria, repo=project→repo map)
     - boss:design → (Phase 3) PRD/meeting flow; v1 may treat as a code task or skip.
5. Store the linkage (plane_issue_id ↔ run_id) for idempotency + later status sync.
6. BOSS runs the job through the existing gated pipeline.
```

**Idempotency:** dedupe on `plane_issue_id` — a re‑delivered webhook or an edit never spawns a second job.

---

## 3. Field mapping (Plane issue → BOSS code task)

| Plane | BOSS `start_code_task` | Notes |
|---|---|---|
| issue title | `title` | |
| issue description | `instructions` | the ask |
| sub‑issues / checklist *(v3)* | `acceptance_criteria[]` | v1: parse from description or leave empty |
| project | `repo` / `owner` | via a **project→repo map** (config) |
| label `boss:<type>` | task type / routing | selects code‑task vs design flow |
| priority | `risk_tier` hint *(v3)* | optional |
| issue id | `finalize_ctx.plane_issue_id` | idempotency + status sync |

---

## 4. Security

- **Signature‑verified webhook** — Plane signs each delivery with the webhook secret (`X‑Plane‑Signature` HMAC); BOSS rejects anything that doesn't match. The endpoint is auth‑exempt (it's a machine callback) but never trusts an unsigned payload.
- **Allowlist, not open trigger** — only issues in a **watched project** carrying a **`boss:` label** ever start a job. A random support ticket can't make BOSS run code.
- **Secrets** live in the ops secret store (never in the repo): `PLANE_BASE_URL`, `PLANE_API_TOKEN`, `PLANE_WEBHOOK_SECRET`, plus the project→repo map + watched labels.
- **Loop prevention (v2):** BOSS's own status updates back into Plane must **not** re‑trigger a job — filter on `action == "created"` + ignore issues already linked to a run.

---

## 5. What we need from Sean

1. **Plane instance URL** + confirm **network reachability both ways** on the goldeneye tailnet (Plane → boss‑api webhook, boss‑api → Plane API). *(This is the one likely gotcha — same as the sandbox‑service firewall/DNS wiring.)*
2. A **Plane API token** (workspace/project scoped) and the **webhook secret**.
3. **Webhook configured** in Plane → URL `https://boss-api.local.sean.network/v1/integrations/plane/webhook`, on **issue** events.
4. The **project(s) to watch** and confirmation of the **label convention** (`boss:coding` / `boss:bug` / `boss:design`, or his preference).
5. **Repo mapping** — which GitHub repo each Plane project corresponds to (so a coding ticket knows where to open the PR).
6. *(Nice to have)* one **sample webhook payload** from his instance so we lock the exact field names on the first try.

---

## 6. Phased implementation

### Phase 0 — Setup & reachability *(mostly Sean)*
- Sean: Plane project + labels + API token + webhook + secret.
- Confirm Plane ↔ boss‑api reachability on the tailnet.
- **Exit:** a test webhook from Plane reaches a stub BOSS endpoint (logged), and boss‑api can `GET` the Plane API.

### Phase 1 — One‑way trigger (MVP) *(BOSS/me)*
- `POST /v1/integrations/plane/webhook`: verify signature → filter (project + `boss:` label + `created`) → map → `start_code_task` → persist `plane_issue_id`. Idempotent. Config in the ops store.
- **Exit:** create a `boss:coding` ticket in Plane → a BOSS code‑task job auto‑starts and runs through the gate to a PR.

### Phase 2 — Status sync back *(BOSS/me)*
- On job phase changes, BOSS PATCHes the Plane issue **state** (e.g. In Progress → In Review → Done) and **comments the PR link + gate Decision**. Loop‑guarded.
- **Exit:** the Plane ticket visibly tracks the BOSS job end‑to‑end without anyone touching BOSS's console.

### Phase 3 — Richer task types & mapping *(BOSS/me)*
- `boss:design` → PRD/meeting flow; `boss:bug` → code task seeded with the repro; acceptance criteria from sub‑issues/checklist; priority → risk tier; assignee = human vs agent.
- **Exit:** design/bug tickets route to the right BOSS flow, not just generic code tasks.

### Phase 4 — Closed loop & polish *(later)*
- Reflect BOSS gate Decisions as Plane approvals; on PR merge, auto‑close the ticket; failures post the retained‑VM inspection link (ties into the inspection feature we shipped).
- **Exit:** Plane is a faithful, two‑way mirror of the BOSS jobs pipeline.

---

## 7. Config / secrets (BOSS side)

```
PLANE_INTEGRATION_ENABLED  master flag — default false (fail-closed until Sean's P0 is done)
PLANE_BASE_URL             e.g. https://plane.local.sean.network
PLANE_API_TOKEN            outbound calls (Phase 2+). Accepts PLANE_API_KEY too (already in the store)
PLANE_WEBHOOK_SECRET       HMAC secret to verify inbound webhooks  ← THE ONE STILL MISSING
PLANE_PROJECT_MAP          {"<plane_project_id>": {"owner":"...","repo":"...","project_id":"..."}}
PLANE_WATCH_LABELS         boss:coding,boss:bug,boss:design (comma/newline separated)
```
All in the ops secret store; rendered into boss‑api's env (self‑serve deploy). As of 2026‑07‑17 the
store already has `PLANE_API_KEY` + `PLANE_PROJECT_URL`; **`PLANE_WEBHOOK_SECRET` is not yet set**, and
`PLANE_INTEGRATION_ENABLED`/`PLANE_PROJECT_MAP` need configuring before the flag is flipped on.

---

## 8. Risks / open questions

- **Reachability** (Plane ↔ boss‑api on the tailnet) — the likeliest friction; may need a network‑wiring nudge from Sean, exactly like the sandbox‑service `10.100.9.1` fix.
- **Exact webhook payload shape** — Plane's field names/signature header; we'll parse defensively and **log the first real delivery** to lock the mapping (same de‑risking approach we used for the sandbox contract).
- **Loop prevention** — must be airtight before Phase 2 (BOSS updates must not re‑trigger jobs).
- **Design‑ticket semantics** — what a `boss:design` ticket should *do* is a product decision to settle before Phase 3.
- **One‑ticket‑one‑job vs decomposition** — v1 is 1:1; later a big ticket might fan out into several BOSS jobs (out of scope for now).

---

## 9. Effort summary

- **Phase 1 (MVP, one‑way):** ~1–2 focused days on BOSS's side; Plane setup is minutes for Sean.
- **Phase 2 (status sync):** ~1–2 days.
- **Phases 3–4:** incremental, product‑driven.

Start with Phase 1 — smallest thing that proves the loop (ticket → auto job → PR) — then refine.
