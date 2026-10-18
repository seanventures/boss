# PRD: BOSS fixes itself

**Status:** Draft for approval · **Ticket:** BOSSAPP-571 · **Date:** 2026-10-17 · **Tier:** Marquee feature

## 1. Summary

When a BOSS agent hits a bug in BOSS itself, BOSS fixes it:
1. It writes the fix as a pull request against its own code.
2. It tests the fix and has its own reviewers check it.
3. After a person approves, it installs the fix on the local one-box.
4. It proves the fix works by resuming the step that was stuck.
5. The person can then send that PR to the BOSS team with one click. The PR is labelled as written by a BOSS agent, so it is kept apart from PRs people write.

Every install becomes a contributor to BOSS, whether or not its user writes code. That is the open-source wedge.

## 2. The problem

Today, a BOSS agent that hits a platform or infrastructure bug can only:
1. diagnose it;
2. write a recommendation (the `boss_recommendations` table and `RECOMMENDATIONS.md`);
3. park the step until someone updates BOSS.

A person then has to read the recommendation, fix BOSS with their own coding agent, install the fix, and press Retry. Only then can they send the fix upstream, if they ever do.

**Evidence from a real run.** The HelpDesk AI demo on 2026-10-17 hit 8 blocking problems, all in BOSS's own code. The run is documented in `docs/boss-improvements/2026-10-16-helpdesk-ai-run.md`.
- BOSS's agents resolved **0 of 8** end to end.
- They correctly diagnosed and recommended **2 of 8**:
  - port 8000 held by an old release's container (`brec_0418d7bf948446d6b456`);
  - the app handed the old database address (`brec_2f3ab64de8d942e8bed6`).
- In both cases a person still had to write the code, install it and wait. The run sat blocked for hours waiting on installs.

## 3. Goals

1. **Turn diagnoses into fixes.** A correctly diagnosed BOSS-side bug becomes a tested, reviewed PR with no person writing code.
2. **Close the loop locally.** After approval, BOSS installs the fix on its own one-box. The parked step resumes and passes. That is the proof.
3. **Upstream without friction.** A verified local fix reaches the BOSS team as a well-formed, labelled, deduplicated, privacy-safe PR in one click.
4. **Stay safe.** Self-modification never weakens security, governance or tests, and can always be rolled back.
5. **Measure autonomy.** Every run reports what share of problems BOSS's agents resolved end to end.

## 4. Non-goals (v1)

- Self-fixing the **user's app** code. That is already `fix_app`, and this feature is for BOSS's own code.
- Pushing changes directly to the upstream BOSS repository. Upstream always receives a PR that the BOSS team reviews.
- Installing self-fixes on production or shared installs without a person. v1 targets the developer one-box.
- Changing the model, engine or provider configuration through a self-fix.

## 5. Users and jobs

| User | Job to be done |
|---|---|
| **Developer on a one-box** (primary) | "When BOSS breaks on my machine, let it fix itself so my run finishes. I only approve." |
| **Org operator** | "Let me trust that a self-fix can't loosen security, and let me undo it." |
| **BOSS maintainer** | "Get fixes from the field as small, tested, deduplicated PRs with evidence. Tell agent PRs from people's PRs at a glance." |
| **Open-source evaluator** | "See a platform that repairs itself and contributes back." |

## 6. The experience

1. A workflow step fails. The recovery agent diagnoses it as **BOSS-side** and records a recommendation, as it does today.
2. Instead of only parking, the step shows **"BOSS is fixing itself"** with live progress: Finding the code → Writing the fix → Testing → Reviewing → Waiting for your approval.
3. BOSS builds the fix as a code task on **BOSS's own source**, in a sandbox:
   - the smallest change that fixes the class of bug;
   - a new regression test that fails before the fix and passes after;
   - BOSS's own affected test suites;
   - its security and code reviewers.
4. The person gets **one approval card**. It shows the diagnosis, the diff summary, the tests (with the new one marked), the reviewers' findings, the files touched, the risk tier, and "Installs on this one-box and resumes the step".
5. On approval, BOSS **rebuilds and redeploys itself** on the one-box and runs health checks. The parked step **resumes by itself** and is re-verified.
   - If the step passes: the recommendation is marked **implemented** and the self-fix is **verified locally**.
   - If health regresses, or the step still fails: BOSS **rolls back** to the previous build automatically and records why.
6. On the **Self-fixes** page, each fix shows its full lineage: problem → recommendation → PR → tests → installed → step resumed → upstream status.
7. **Send to BOSS team** (explicit, opt-in, previewed) opens the PR upstream. The person sees exactly what will be sent before it leaves.
8. Upgrades keep the local fixes. A local fix is **dropped automatically** once the official release contains it: its regression test passes upstream, or a matching commit is found.

## 7. Requirements

### P0: must ship

| # | Requirement |
|---|---|
| R1 | A recovery decision classed `wait_for_boss` (BOSS-side) with enough confidence starts a **self-fix**: a code task on BOSS's own source, linked to its recommendation, the run, the step and the signature. |
| R2 | The self-fix must add a **regression test** that fails on the base commit and passes with the fix. It must pass BOSS's affected test suites and the P0 tests, including the private-infrastructure test. |
| R3 | **Test integrity:** a self-fix is refused if it deletes, skips or `xfail`s tests, loosens assertions, or edits test fixtures to pass. |
| R4 | **Protected paths:** identity, authorization, approvals and seats, secrets and vault, policy and safety YAML, the installer's trust checks, and the self-fix machinery itself are **never self-fixable** in v1. A fix that needs them becomes a recommendation for a person, with the reason. |
| R5 | **A person approves every install** by default. The approval is a governed decision of the right tier, approved by the right seat. Policy may later allow auto-install of low-risk fixes on a dev one-box. That is off by default and never on for shared or production installs. |
| R6 | **Install, resume, verify:** an approved self-fix is installed on the one-box (rebuild + redeploy), health-checked, and the parked step resumes. Success is the step passing, not just green tests. |
| R7 | **Automatic rollback:** if health regresses or the resumed step fails for the same cause, BOSS returns to the previous build and records the outcome on the self-fix. |
| R8 | **Local fixes survive upgrades:** they are kept as a local fix branch on top of the release and replayed after an upgrade. They are dropped once upstream contains them. A conflict parks the fix for the person, with the reason. |
| R9 | **Upstreaming is opt-in and previewed:** "Send to BOSS team" shows the exact PR before sending. Its content is the diff, the tests, a redacted evidence bundle, the signature, the BOSS version, the engine, and the local verification result. It never carries secrets, hostnames, IPs or app data. It uses the existing allow-list and redaction gate. |
| R10 | **Labels** on every BOSS-authored PR, both local and upstream: `source:boss-agent` (people's PRs get `source:user`), `boss:self-fix`, `area:<deploy\|build\|preview\|…>`, `verified:local`, `engine:<name>`, `signature:<hash>`. The commit trailer names the agent identity (Passport) and the approving person. |
| R11 | **Dedupe:** before opening upstream, BOSS searches existing issues and PRs by signature. It links to a match and updates it with the extra evidence instead of opening a duplicate. |
| R12 | **Kill switch and budget:** one org-level switch turns self-fix off. Each self-fix has a cost cap and a time cap. When either runs out, the fix falls back to the recommendation. |
| R13 | **Model-agnostic:** behaviour is identical on Claude, Codex, OpenRouter and Ollama; only quality varies. A scripted conformance engine proves the flow. |
| R14 | **Audit:** every step is an event with an actor: diagnosis, fix build, approval, install, rollback, upstream send. |
| R15 | **Autonomy metric:** each run reports problems resolved by BOSS agents end to end, versus problems needing a person or an outside coding agent. The metric is shown on the run and as a trend. |

### P1: should ship

| # | Requirement |
|---|---|
| R16 | **Injection resistance:** app logs and evidence are treated as data, never instructions. A self-fix diff that touches code unrelated to the diagnosed area is refused. |
| R17 | **Maintainer side:** upstream PRs follow a PR template. Collector mode / Improve BOSS can count incoming self-fixes per signature, so the BOSS team can prioritise. |
| R18 | **Fork handling:** BOSS uses the user's fork if one exists, offers to create one, or keeps the fix local (offline installs) with a patch export. |
| R19 | **One self-fix per signature at a time.** A second step that hits the same signature waits for that fix instead of starting another. |

## 8. Safety and governance

This is software changing itself. The bar is: **nothing a self-fix does can widen what BOSS or its agents may do.**
- **Protected paths (R4) and test integrity (R3)** are checked mechanically before any reviewer runs.
- **Separation of duties:** the agent that diagnosed the bug isn't the only reviewer. A security reviewer always runs. A person always approves the install (R5).
- **Reversible:** every install records the previous build. Rollback is automatic on regression (R7) and available by hand.
- **Bounded:** budget and time caps, one fix per signature, and a kill switch (R12, R19).
- **Private by default:** nothing leaves the machine until the person sends it, and they see exactly what is sent (R9).

## 9. Console surfaces

- **The parked step:** "BOSS is fixing itself", with stages and a link to the self-fix.
- **The approval card:** diagnosis, change summary, tests with the new regression test marked, reviewers' findings, files, risk, and "Installs and resumes".
- **The Self-fixes page:** the list and the lineage, as in §6, with status chips (Building, Needs you, Installed, Verified, Rolled back, Sent upstream, In release) and the autonomy trend.
- **Send to BOSS team:** a preview sheet showing the exact PR and labels.

Short labels with tooltips, one surface per job.

## 10. Success metrics

- **Autonomy rate:** BOSS-side problems resolved end to end by BOSS (diagnosis → fix → install → step passes) out of all BOSS-side problems. Baseline on 2026-10-17: **0%**. Target for v1: **at least 50%** of BOSS-side problems on the replay set.
- **Time to unblock:** from step parked to step passing, for a self-fixed problem. Baseline: hours, waiting on a person. Target: under 30 minutes, including the person's approval.
- **Fix quality:** at least 90% of self-fixes keep passing after upgrade and are later merged upstream or superseded by an equivalent fix. Zero self-fixes that touch protected paths or weaken tests.
- **Upstream flow:** the share of verified self-fixes sent upstream, and the duplicate rate (target under 10%).

## 11. Acceptance: the proof

The feature is done when BOSS fixes both real recommendations from this run by itself:
1. **Replay A, port held.** Start from the parent commit of `c56bd88a3`. Deploy HelpDesk AI where an old release holds port 8000. The recovery agent diagnoses the bug, BOSS builds and tests its own fix, a person approves, BOSS installs it, and the deploy resumes and passes health. There is no human-written code.
2. **Replay B, res-db unreachable.** Same as Replay A, from the parent commit of `767786609`.
3. Both fixes appear on the Self-fixes page with full lineage. "Send to BOSS team" previews a correct, labelled and redacted PR.
4. The same flow passes on the scripted conformance engine and at least one non-Claude engine.
5. An independent tester walks every surface in a browser and finds no P0 or P1 issues.

## 12. Phases

1. **Self-fix builds:** R1–R4, R12–R14. A recovery decision starts a fix build on BOSS's own source, with the test-integrity and protected-path gates.
2. **Install and prove:** R5–R8. Approval card, install on the one-box, resume and verify, rollback, upgrade survival.
3. **Upstream:** R9–R11, R17, R18. Preview, labels, dedupe, PR template, forks.
4. **Surfaces and metrics:** R15, plus the parked-step view, the Self-fixes page and the autonomy trend.
5. **Proof:** the replays in §11, the independent browser test and the demo video.

## 13. Risks and open decisions

| Risk / decision | Recommendation |
|---|---|
| A self-fix breaks BOSS itself | Health check, then automatic rollback (R7). A person approves the install (R5). |
| Hidden instructions in logs steer the fix | Evidence is data only. The diff must stay inside the diagnosed area (R16). Security reviewer. |
| Upgrades fight local fixes | Local fix branch, replayed after upgrade, auto-dropped when upstream has it (R8). |
| Should low-risk fixes auto-install on a dev one-box? | Off by default. Available later as an explicit dev-only policy. |
| Where does BOSS's own source come from on an install with no GitHub fork? | Build against the local install checkout. Upstream through a fork when the user opts in; otherwise export a patch (R18). |
| Cost of self-fix builds | Per-fix cap (R12). Show the cost on the approval card. |

## 14. Demo video (about 3 minutes)

1. A real run hits a BOSS bug. The step says "BOSS is fixing itself".
2. BOSS finds the code, writes the fix plus a regression test, and its reviewers pass it.
3. One approval. BOSS reinstalls itself, and the stuck step resumes and passes.
4. The Self-fixes lineage, and "Send to BOSS team" with its preview and labels.
5. Close on the autonomy number moving from 0% to the run's real result.
