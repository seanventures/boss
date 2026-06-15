# BOSS — Sean Demo Runbook

**Goal:** show a one-line directive become a **merged, tested Java PR** — scoped by a multi-agent panel, implemented by a coding agent, and gated by two human approvals — with full budget + audit governance, and **code that never persists on BOSS**.

> Companion docs: `BOSS_Coding_Agent_Architecture.md` (the why/how), `BOSS_Coding_Agent_Implementation_Plan.md` (what's built).

---

## 0. The one-line pitch (say this first)

> "BOSS isn't a coding agent — it's the **governed orchestrator** around one. Watch: I give a one-line directive; a panel of AI roles debates it into a design; I approve; a coding agent implements it in an isolated sandbox, runs the real tests, and opens a pull request; I approve the merge. Two human gates, a budget envelope, a full audit trail — and our servers never hold your source code."

---

## 1. Pre-flight (do this ~5 minutes before)

**The console:** `http://localhost:3001/?org=org_boss`  (Org **BOSS** → project `boss-dogfood`)

Check the three services are up:
```bash
curl -s -o /dev/null -w "API   %{http_code}\n" http://localhost:8000/health
curl -s -o /dev/null -w "WEB   %{http_code}\n" http://localhost:3001/
pgrep -fl 'apps.worker.boss_worker.main' >/dev/null && echo "WORKER up" || echo "WORKER DOWN"
```
If any are down, from `~/startup/sean/boss`:
```bash
nohup .venv/bin/python -m uvicorn apps.api.app.main:app --host 0.0.0.0 --port 8000 > .local-logs/api.log 2>&1 &
nohup .venv/bin/python -m apps.worker.boss_worker.main > .local-logs/worker.log 2>&1 &
# web (if needed): cd apps/web && pnpm dev
```

**Checklist:**
- [ ] API / Web / Worker all up
- [ ] **Decisions** inbox is clean (no stale cards) — see §5 to reset
- [ ] **Code Tasks** tab shows (refresh once if blank)
- [ ] `paramg/boss-sandbox` `main` has the Maven skeleton **and** `docs/prd/…rate-limiter….md` (the approved design)

---

## 2. The story arc 

```
Directive ──▶ Part 1: AI role panel debates ──▶ DESIGN DOC ──▶ [gate #1: approve]
   └─ design is published into the repo (docs/prd/…)
                                            │
Part 2: coding agent reads the design ──▶ writes Java + runs `mvn test` ──▶ PR ──▶ [gate #2: approve merge]
```

Two human gates. Budget reserved/settled. Every effect audited. Sandbox is single-tenant, ephemeral, code never persisted; the agent acts via a revocable **GitHub App** (short-lived, repo-scoped tokens).

---

## Part 1 — Design (multi-agent meeting → governed design doc)

**This is already pre-staged** (the design doc is published + merged to `main`). For the demo you have two ways to present it:

### Option A — Narrate the completed meeting *(recommended — fast)*
1. Go to **Meetings** → open the **"Add a thread-safe token-bucket rate limiter…"** meeting.
2. Walk through it:
   - The **participants** ribbon (Engineer/Architect, QA, Security, Product Manager).
   - **Round 1 → Round 2 → Synthesis** — point out the *debate*: where roles disagreed and how it resolved (e.g. the "Decisions & Recorded Dissent" table — the clock-seam decision).
   - Header shows **`⏱ took 2m 41s`**, the **model**, and **cost** — the governance metering.
3. Note the outcome: **the design was approved (gate #1) and published into the repo** at `docs/prd/…rate-limiter….md`. Open it on GitHub if you want to show it landed.

### Option B — Convene a fresh meeting live *(only if you want to show the debate happening; ~3 min)*
1. **Meetings → ＋ New meeting.**
   - **Directive:** `Add a thread-safe token-bucket rate limiter to our Java service — configurable rate/capacity, a pluggable clock for deterministic tests, thread-safe.`
   - **URL:** leave blank.
   - **Roles:** Engineer/Architect, QA, Security, Product Manager.
   - **Convene.** Watch it run (durable — survives a restart). When done, a Decision appears.
2. **Decisions →** approve **"Approve publishing the PRD"** *(human gate #1)* → BOSS publishes the design as a PR under `docs/prd/`.
3. Merge that design PR (it's a draft — **mark ready → squash merge** on GitHub) so it lands on `main`.

> **Why publish to the repo?** So the implementation task can just **read it from the repo** — the repo is the source of truth, no copy-paste between steps.

---

## Part 2 — Implement (coding agent → tested PR) 

1. **Code Tasks → ＋ New code task.** Fill in:
   - **Title:** `Implement the token-bucket rate limiter (per approved design)`
   - **Instructions:**
     ```
     A design spec for this feature is in this repo at
     docs/prd/prd-add-a-thread-safe-token-bucket-rate-limiter-to.md
     (also anything under docs/prd/). Read it and implement the token-bucket
     rate limiter in Java in the org.bosssandbox package with JUnit 5 tests,
     following the design's API, concurrency model, and test plan. Keep the
     existing SmokeTest passing.
     ```
   - **Test commands:** `mvn -q -B test`
   - **Owner:** `paramg` · **Repo:** `boss-sandbox` · **Base branch:** `main`
   - **Start coding task.**
2. Watch the **phase ribbon**: `cloning → coding → testing → opening PR` (~1.5–2 min; cold Maven the first time, warmer after). Narrate: *the agent clones into an isolated sandbox, reads the design, edits files, and BOSS re-runs the tests as the authoritative gate.*
3. Open the finished card → show the **agent summary**, **diff summary** (files, +/−), **tests passed**, and the **PR link** + the **merge-gate Decision** (this is the review packet).
4. **Decisions →** review the PR (tests green) and approve *(human gate #2)*.
5. *(Optional finale)* Fully merge the code PR on GitHub (it's a draft — **mark ready → squash merge**) to show the feature land on `main`.

---

## 3. Talking points

- **Governance is the product.** Two explicit human gates (design, merge); budget **reserve→settle** per task; every tool call audited. "Autonomy is bounded by approvals and budget."
- **Multi-agent, like a real company.** A role panel turned a vague directive into acceptance criteria — fewer wasted runs, better diffs.
- **Real engineering, not a toy.** The agent wrote real Java, ran real `mvn test`; the change of record is a real `git diff` + PR — not model chatter.
- **Own the agent, rent the model, isolate the sandbox.** Customers buy BOSS + model access — never Devin/Cursor/Codex, and (in the SaaS model) no infrastructure of their own.
- **Data protection.** Code runs only in a **single-tenant, ephemeral microVM** (Firecracker/gVisor in the target arch); **never persisted** — BOSS keeps diff *summary*, tests, PR link, cost. (See architecture §5.)
- **Developer impersonation, revocable.** The agent acts via a customer-installed **GitHub App** — short-lived, repo-scoped tokens, one-click uninstall to revoke, commits attributed to the bot. (Architecture §6.) Our live proof: App `4015223` on `paramg/boss-sandbox`.

---

## 4. Gotchas & troubleshooting

| Symptom | Cause / fix |
|---|---|
| Code task **blocks** with "no eligible model entitlement" | Budget near cap → freeze. Daily cap is $100; if needed raise `bp_boss_default.daily_cap_usd_micros`. |
| Code task can't run `mvn` | Worker must have the widened allowlist loaded — restart the worker (it reads `.env` at boot). JDK 17 + Maven are on the host. |
| PR won't merge ("still a draft") | `publish_document`/code PRs open as **draft** — mark ready, then merge (GitHub UI, or the mark-ready+merge step). |
| Meeting shows raw `**` / tables look broken | Already fixed (markdown renderer). Hard-refresh the page. |
| A tab shows an empty screen | Refresh once; the area sections hydrate on first load. |
| Code task takes long | First Maven run downloads JUnit (cold `~/.m2`); subsequent runs are warm. |

---

## 5. Reset between rehearsal and the live demo

After you rehearse Part 2, you'll have an extra PR + decisions. To start the live demo clean:

- **Close the rehearsal PR** on `github.com/paramg/boss-sandbox` (and optionally delete its branch).
- **Clear stale decisions** so only the design gate (or nothing) shows. Quick way from `~/startup/sean/boss`:
  ```bash
  .venv/bin/python - <<'PY'
  from apps.api.app.config import Settings
  from apps.api.app.db import begin, get_engine
  from sqlalchemy import text
  eng=get_engine(Settings())
  with begin(eng) as c:
      n=c.execute(text("UPDATE decisions SET status='rejected', resolution_note='demo reset' "
                       "WHERE project_id='boss-dogfood' AND status='pending' "
                       "AND question NOT ILIKE '%publishing the PRD%'")).rowcount
  print('cleared', n, 'pending decisions')
  PY
  ```
- The design doc on `main` (`docs/prd/…`) can **stay** — Part 2 reads it. If you re-run a fresh meeting (Option B), it'll publish a new one alongside.

---

## Appendix — environment facts

- **Console:** `http://localhost:3001/?org=org_boss` · **API:** `http://localhost:8000`
- **Org/Project:** BOSS / `boss-dogfood`
- **Repo:** `paramg/boss-sandbox` — `main` has `pom.xml`, `src/test/java/org/bosssandbox/SmokeTest.java`, `.gitignore`, `docs/prd/…rate-limiter….md` (+ `.pdf`)
- **GitHub App:** ID `4015223`, installed on `boss-sandbox`; private key at `~/.config/boss/github-app.pem` (chmod 600)
- **Budget:** $100/day cap on `boss-dogfood`
- **Toolchain on host:** JDK 17 + Maven 3.9.9
- **Design roles available:** Engineer/Architect, QA, Security, Product Manager (+ others)
