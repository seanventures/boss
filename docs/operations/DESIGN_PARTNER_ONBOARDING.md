# Design-partner & developer onboarding (private repo)

**Plane:** BOSS-292  
**Audience:** a developer invited onto the **private** BOSS repository (monorepo
and/or engine tree).  
**Outcome:** a partner can clone, run the local control plane, prove smoke + an
optional live gate, paste Community BYOK, bind GitHub in Mission Control, and
file one redacted friction record.

This is a **private design-partner path**. It is not a public open-source flip
(BOSS-293 / BOSS-300), not a Cloud product, and not a promise that hosted
goldeneye services or sandboxes are configured for you.

Companion packs:

| Pack | When to use it |
|---|---|
| This file | Full monorepo: `make dev` / `make smoke` / `make qa-live`, Mission Control, Community vs Cloud |
| `engine/DESIGN_PARTNER_ONBOARDING.md` | Engine-only Docker + CLI/MCP sample change |
| [Community quickstart](../community/QUICKSTART.md) | Community edition loop once the repo is already on disk |
| [Secrets and deployment onboarding](../product/SECRETS_AND_DEPLOYMENT_ONBOARDING.md) | Later: onboarding a second **deploy target** (not week-1) |
| [GA living plan](GA_LIVING_PLAN.md) § claims freeze | What you may and may not claim after a successful week |

Print `make onboard` from the repo root for the same first commands. That
target never echoes secret values.

---

## 0. What you are evaluating

| Edition | What it is | What it is not |
|---|---|---|
| **Community** | Self-hosted control plane you run. BYOK models and infra. Local Postgres, API, worker, Mission Control, CLI, MCP. Cloud is optional. | Not a crippled trial. Not hosted multi-tenant SaaS. Not “sandboxes always work.” |
| **Cloud / Enterprise** | Optional hosted team, marketplace, managed capacity, Account Connect. Reached later via Easy Connect. | Not required for week-1. Missing Cloud must not block local commands, budgets, GitHub, or ProviderOps. |

Runtime truth: `BOSS_EDITION=community` (default) and
`GET /v1/edition` → `edition_id=community`. Leave
`BOSS_ACCOUNT_CONNECT_URL` unset.

Claims freeze (do not regress):

- **Allowed:** self-hosted governed loop (tasks, budgets, decisions, receipts);
  Community BYOK paste; project GitHub connect + bind + test receipts; honest
  empty CI/CD / monitoring when undeployed; `make qa-live` as a control-plane
  gate (not a factory guarantee).
- **Forbidden:** “sandboxes / previews always work”; unattended Software Factory;
  hosted IdP / marketplace / billing as Community; public OSS availability;
  zero deadletters as a product claim.

A visible block, decision, retry, or deadletter is a **successful fail-closed
result**. Do not restate it as a completed sample.

---

## 1. Request private repo access (human gate)

Access is granted by the BOSS owner. There is no self-serve public clone and
no Plane/ops credential that substitutes for a GitHub invite.

Send the owner, in one message:

```text
Subject: BOSS design-partner access request
GitHub username:
Evaluation machine: OS + Docker version
Purpose: week-1 Community eval / engine MCP eval / other (one line)
Confirm: I will only target the owner-supplied sample repository
Confirm: I will not paste secrets into issues or chat
Confirm: I will not point the eval at a production provider account
```

The owner supplies:

- GitHub access to this private repository (and a pinned revision / tag)
- a **canonical sample repository** and default branch (non-production)
- the least-privilege GitHub credential or App install for a **draft** PR
- a support contact and the issue tracker to use
- a budget/model lane if you will run a code task

**Residual:** repo access remains a human step. This document cannot mint
invites, Plane keys, or ops tokens.

Do not invent `PLANE_API_KEY`, `OPS_TOKEN`, or GitHub PATs. Do not commit
`.env`. Do not paste secret material into a feedback issue — report the
SecretRef **name** or a redacted error only.

---

## 2. Local path: `make dev` / `make smoke` / `make qa-live`

Prerequisites: Node 20, pnpm 9, Python 3.11+, Docker with Compose. See the
root `README.md`.

### 2.1 Configure without printing secrets

```bash
cp .env.example .env
# Edit .env in an editor. Do not `cat`, `echo`, or paste keys into issues.
# Minimum for a model-backed loop: OPENAI_API_KEY (or another BYOK provider).
# GitHub bind uses an env SecretRef such as BOSS_GITHUB_TOKEN — not a raw
# token typed into Mission Control.
```

If `localhost:5432` is already taken, set `BOSS_POSTGRES_PORT` and make
`DATABASE_URL` use the same port before starting anything.

Never commit `.env`. `make onboard`, `make smoke`, and `make qa-live` are
written so they do not echo `BOSS_API_KEY` or other secret values.

### 2.2 Install, seed, run

From the repository root:

```bash
make install
make seed
make dev
```

`make dev` starts local Postgres (`infra/docker-compose.yml`), waits for it,
then runs the web + API + worker via `pnpm dev`.

Success:

- Postgres accepts the seed
- API health responds (`curl -sS http://127.0.0.1:8000/health` or the port
  printed by `pnpm dev`)
- Mission Control loads (typically `http://127.0.0.1:3000`)
- A database connection failure is **not** a successful install — stop and
  file the redacted error

### 2.3 Smoke (no live providers required)

```bash
make smoke
```

This applies schema/bootstrap against local Postgres. Exit 0 is the check.
It does not prove GitHub, sandboxes, or Cloud.

### 2.4 Optional live control-plane gate

`make qa-live` talks to a **running** BOSS API. It does **not** print the
API key. It does **not** require a sandbox, PR, or merge.

```bash
# Against your local API (recommended for week-1):
export BOSS_API_URL=http://127.0.0.1:8000
export BOSS_PROJECT_ID=boss-dogfood
# BOSS_API_KEY must already be in the environment. Do not echo it.
make qa-live
```

If you were given access to the owner’s dogfood API instead, point
`BOSS_API_URL` at that host. Default in the Makefile is the internal
dogfood URL; do not treat reachability of that host as part of Community
self-host success.

Exit 0 on required checks is the gate. A fail-closed 401/503 with a visible
reason is evidence — not a silent skip.

`make qa-live` is a **control-plane** proof (health, auth, providers list,
budget quote, command→task, decisions, deadletters). It is not a factory
reliability certificate.

---

## 3. Community BYOK paste

Week-1 needs one model key you control. Two supported ways; both store a
**SecretRef**, never display the value again.

**A. Environment (simplest):** put the key in `.env` as `OPENAI_API_KEY` (or
the provider’s documented env name) and restart `make dev`. Mission Control
must not show the value.

**B. Mission Control paste-and-connect:**

1. Open **System Configuration** (Connections).
2. Use the provider **paste-and-connect** form
   (`data-testid="provider-paste-connect-form"`).
3. Paste the key once. BOSS writes it through to the local backend and keeps
   only the SecretRef metadata.
4. Run **Test**. A redacted receipt or a visible fail-closed error is the
   result.

Missing keys must fail closed (decision / event / deadletter). They must not
degrade to a mock success.

Local / OpenAI-compatible endpoints (Ollama, vLLM) are allowed via
`OPENAI_BASE_URL`. That is Community. A managed multi-tenant model gateway is
Cloud — out of week-1 scope.

---

## 4. GitHub bind in Mission Control

Code tasks that open a draft PR need a project connection **and** a
repository binding to the **owner-supplied sample repo**.

1. Place a fine-grained PAT (or GitHub App private key) in the API host
   environment, typically `BOSS_GITHUB_TOKEN`. **Do not paste the raw token
   into Mission Control.**
2. Open **GitHub** in Mission Control.
3. If no connection exists, use **Connect GitHub** (easy-connect). Point it
   at the env credential name, not the secret value.
4. Bind `owner/repo` for the sample repository. Role `primary` if this is
   the first binding.
5. A test receipt or a visible permission/policy error is success. Do not
   bind a production customer repository.

Factory APIs and UI exist. “Unattended reliable PRs / sandboxes always work”
is a forbidden claim. A governed block is an acceptable week-1 outcome.

---

## 5. How to file Plane / ops requests

Plane (`https://plane.local.sean.network`, workspace `goldeneye`, project
BOSS) is the **internal** system of record. Design partners are not given
Plane API keys as part of this pack.

| Who | How to file | Do not |
|---|---|---|
| Design partner | Open a GitHub issue on **this private repo** with the template below. The owner/agents may promote it to Plane. | Invent `PLANE_API_KEY`. Call undocumented ops APIs. Duplicate an issue the owner already filed. |
| Partner invited to Plane in the browser | Use the Plane UI the owner invited you to. Comment on the existing issue when one exists. | Commit the key. Print it. Create a second issue for the same friction. |
| Internal ops `/requests` | Auto-filed as Plane issues. | Re-create the same request by hand. |

Feedback template (one issue per distinct friction):

```text
Title: [BOSS design partner] <short outcome or blocker>
Environment: OS, Docker version, git revision, surface (MC / CLI / MCP / make)
Step: exact section and command (no secret values)
Expected: what this guide said would happen
Observed: result, redacted logs, screenshot, or HTTP status
Impact: blocked / serious friction / minor friction
Evidence: task/run id, decision id, receipt id, draft PR URL if any
Suggested improvement: optional
```

If the friction is a running code task, also attach the task/run id so it
stays in the work lineage. Never attach `.env`, PATs, or customer source.

---

## 6. Week-1 success criteria

A design-partner week-1 is **done** when every box is either checked or has a
filed, redacted blocker.

- [ ] Private repo access received (human). Clone of the pinned revision succeeds.
- [ ] `.env` created from `.env.example`; no secret committed or pasted into chat.
- [ ] `make install` and `make seed` exit 0.
- [ ] `make dev` brings up API + worker + Mission Control against local Postgres.
- [ ] `make smoke` exits 0.
- [ ] `GET /v1/edition` (or Mission Control edition chip) reports Community
      without requiring Cloud.
- [ ] One BYOK model key is present as a SecretRef or env ref; Test is a
      receipt **or** a visible fail-closed error.
- [ ] GitHub project connection + sample-repo binding exists, **or** the
      bind failed closed with a filed issue (permissions / missing env ref).
- [ ] Optional: `make qa-live` required checks pass against the API you are
      actually running. Not required if the API never stayed up — file that
      instead.
- [ ] You have **not** claimed sandboxes/previews always work. An empty or
      409 preview/sandbox surface is recorded as honest-empty, not as a pass.
- [ ] One governed sample (command or code task) reached a reviewed draft PR
      **or** a visible block/decision/deadletter. No silent drop.
- [ ] At least one feedback issue filed with redacted evidence.
- [ ] Local Docker resources and any temporary credentials revoked per the
      owner’s instructions.

The BOSS owner records partner results on BOSS-292. The private design-partner
phase does not exit until at least two external partners self-serve this path
to a completed sample (or a reproducible, triaged block) and P0 friction is
clear — see the engine pack exit checklist. Authoring this file is not that
exit.

---

## 7. Pointers

| Surface | Pointer |
|---|---|
| CLI | `make onboard` |
| Mission Control | Command Center card **Design-partner setup** |
| Engine-only eval | `engine/DESIGN_PARTNER_ONBOARDING.md` |
| Community vs Cloud | [Community spec](../product/BOSS_COMMUNITY_EDITION_SPEC.md), [Cloud / Enterprise spec](../product/BOSS_CLOUD_ENTERPRISE_SPEC.md) |
| Claims freeze | [GA living plan](GA_LIVING_PLAN.md) |
| Local run extras | Root `README.md` § Setup / Commands |

### Explicit non-goals

- Making the repository public (BOSS-293), choosing a license (BOSS-300), or
  advertising general availability.
- Inventing legal terms, SLAs, or a self-serve access portal.
- Treating goldeneye / tailnet hostnames as something a partner must reach.
- Onboarding a second production deploy target (that is the secrets/deployment
  design, not week-1).
