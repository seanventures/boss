# HelpDesk AI run: every manual fix, and who should have made it

Run `wfr_fb4fb5ded8c84a4dab12` (HelpDesk Delivery on paramg/boss-sandbox), 14 to 17 Oct 2026.
It ended deployed and healthy at http://203.0.113.10:8000 (`sdep_7be36aa4f88647bea886`). Getting there took
about 15 fixes, each found by a failure the operator saw, then made by Claude in BOSS's code, then a manual
Retry. This file sorts them by who should have handled each one, so BOSS's agents do it next time.

**Agent could:** a BOSS agent with today's access could have fixed it inside the run.
**Agent + capability:** an agent could have fixed it with a capability it lacks today (named).
**BOSS code:** only a change to BOSS fixes it. The agent should work around it if it safely can, write a
recommendation (this file's format), and park the step until BOSS is updated.

## Build

| # | Symptom (what the operator saw) | Cause | Who | What the agent should have done |
|---|---|---|---|---|
| B1 | "needs Java" (4 times) | Gate planners looked at the shared repo root (`pom.xml`), not `helpdesk-ai/` | BOSS code | Diagnose: the app folder holds `package.json`; the root's `pom.xml` belongs to another app. Recommend "resolve the app folder from the app's record". Fixed in `fix/app-record-decides`. |
| B2 | Retry produced an empty diff; reviewers said "no app code" | A blocked build kept no work, and the retry repaired an empty tree | BOSS code | Notice "the change holds none of the app's files" before review, and run a full build instead of a repair. Recommend "a blocked build keeps its work". Fixed in `fix/retry-keeps-work`. |
| B3 | Blocked by the secret scan (`demo.mjs:58`) | A literal password in code; the scan ran last with no repair turn | **Agent could** (the fix) / BOSS code (the order) | Remove the literal and read `DEMO_PASSWORD` from env, then rescan. Fixed so the implementer now gets one repair turn. |
| B4 | App exited at start: "backend exited (code 1)" | BOSS's multi-service start gave the app only `HOME` (no `SESSION_SECRET` or `DEMO_PASSWORD`) | BOSS code | Compare the agent's own boot test (passed) with BOSS's start (failed): the difference is the environment. Recommend "one environment resolver for every start". |
| B5 | The app's own error never appeared anywhere | Start output was not kept | BOSS code | Recommend "keep the app's stdout/stderr on every start". Fixed (BOSSAPP-559). |
| B6 | The build agent tried `rm -rf pom.xml src` at the repo root | It worked around B1 by deleting another app's files | BOSS code (guardrail) | Never write outside the app folder. Recommendation filed under BOSSAPP-555 (write boundary). |

## Deploy

| # | Symptom | Cause | Who | What the agent should have done |
|---|---|---|---|---|
| D1 | "`release_manager` is not assigned" | A default team seeded by an older run, plus approvers inside parallel branches being ignored | BOSS code | Top up a default team with the workflow's agents. Fixed. |
| D2 | Provision failed: SecretRef `helpdesk ai` (space) | An app key with a space broke the secret reference pattern | BOSS code | Recommend "encode app keys in references". Fixed. |
| D3 | `npm: command not found` on 203.0.113.10 | The manifest ran npm on a machine that only has Docker | **Agent could** | Read the host facts (Docker yes, Node no), switch the manifest to `kind: compose`, redeploy. Done automatically by fix-and-redeploy. |
| D4 | Health 600s, then `EAI_AGAIN res-db` | The app ran as its own compose project, not in the resources' project | BOSS code | Read the container logs, see `res-db` unresolvable, class it as BOSS wiring (not the app), recommend it, and do NOT send a repair build. Fixed, along with the classification. |
| D5 | "Port 8000 already allocated" | An older copy of the same app under its own project held the port | **Agent could** | Find the container holding the port; it belongs to this app, so `compose down` that project and redeploy. Now done by the deploy steps themselves. |
| D6 | The project was named "staging", the DB lives in "deploy" | The shared project was named from the target's environment, not the resources' | BOSS code | Recommend "name the shared project from the resources' environment". Fixed. |
| D7 | Monitor: "the authority this run was started under has ended" | The run's grant ended when the run was marked failed | BOSS code | Recommend "a retryable run keeps its grant". Fixed (24h grace). Needs the person once for this run, because the grant already ended. |

## Process (things the operator did by hand)

| # | Symptom | Who | What should happen |
|---|---|---|---|
| P1 | About 100 manual Retry clicks | **Agent could** | The step's agent retries, fixes or cleans on its own within a cost and time budget (recovery loop, `feat/agents-recover-on-their-own`). |
| P2 | Every BOSS update needed a manual retry of the parked step | **Agent could** | Steps parked for BOSS resume by themselves when BOSS's version changes. |
| P3 | Asking Harbor took 240s and could not see the container logs | Agent + capability | Answer from the step's context first; BOSS's own diagnosis reads the container state without the person's `host:exec` grant (BOSSAPP-570). |
| P4 | Failures thrown at the person with "Retry / Skip" | Agent | Ask the person only for a decision only they can make, with ONE question, and say what happens after. |

## The recommendations format (what agents write from now on)

```
### <date> · <short cause> · open
- Run / step / build: <ids>
- Symptom: <what failed, one line>
- Evidence: <redacted log lines, file:line>
- Cause: <BOSS's own bug or missing capability>
- What the agent did meanwhile: <workaround, or parked>
- Proposed change: <files / functions, and the class fix>
```
