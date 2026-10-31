# BOSS thinks like a principal engineer

Status: proposed, P1. Release branch: `rel/rc10.10-kg`. Written 2026-10-31.

## 1. Why

BOSS sells engineering judgement, not typing speed. A customer, commercial or open source, should
get from BOSS what a principal engineer brings to a change: understand first, change the cause,
leave existing behaviour intact, prove it, and only ask people the questions only people can
answer.

Today BOSS fails that bar in the same way a bare coding agent does. The clearest evidence came from
this repository, on 2026-10-31.

**The case that started this spec.** The operator reported that BOSS's own workflow runs showed
Preview *failed* and Production launch readiness *deadlettered*, although every build's PR had been
reviewed and merged. A coding agent (Claude Code) handled it in three rounds:

1. **It suppressed the symptom.** It added a project switch, "Ends at the PR", that skipped
   preview, readiness, More tests, deploy and monitoring. It never asked why each step failed, and
   it broke pass 2. The operator rejected it as a patch, and it was reverted.
2. **It found two real causes but claimed more than it knew.**
   - Only a `boss.deploy.yaml` the build *wrote* counted; the repository's existing one was ignored.
   - Builds started with an API key ran their outer run outside the build's authority, so later
     steps were refused `code_task:retry`.

   It also added a heuristic: "preview failed + the app has a manifest → not applicable". It
   called the result a design fix. It was not.
3. **The post-mortem found the real preview causes.** None of them was "the app is a stack":
   - The monorepo `dev` script was run as the app, and the sandbox has no `ps` for `tree-kill`.
   - The guessed Python entry ran with no `PYTHONPATH` contract.
   - A module that only builds `FastAPI(...)` was ranked as a server, so it exited 0 with an
     empty log.

   The heuristic would have hidden real failures for every app with a manifest. It was removed
   (`a2aa4f04b`).

The agent could have done this correctly. It lacked a protocol that forces the study, context about
existing mechanisms, impact analysis, and a check that its claims had evidence. The operator's ask:
*when the same question is given to BOSS, it should understand, study in detail, fix without
breaking existing flows, and address the design properly.*

**Sources of practice.**
- Kun Chen, "L8 Principal's Agentic Engineering Workflow" (2026-06, 45 min).
- Four read-only studies of this codebase (build pipeline; context and memory; planning and
  decisions; the post-mortem above), each with file:line evidence.
- The operator's decisions in this thread: Jev decides first, a person last, and nothing stalls on
  a forgotten click.

## 2. What principal engineers do, and what BOSS does today

Paths are under `engine/boss_engine/services/`; "G" is `governed_code_task_service.py`.

| # | Practice | BOSS today (evidence) | Gap |
|---|---|---|---|
| 1 | **Study before changing**: existing code, callers, history, existing mechanisms | Design stage only when `risk_tier >= 2 and not web app` (G:5935). Edits to existing code are tier 0–1 because "blast radius is small" (pipeline_profiles.py:282-299). Context is the task, standards, KG rules and lessons, and keyword-ranked file heads (code_agent.py:854). | **Edits to existing code get the least scrutiny**, which is backwards. There is no impact or caller analysis and no "existing mechanism" search. |
| 2 | **Reproduce first, end to end**, for bug fixes | `red_first` runs only if criteria match a regex and only records the result (red_first.py:21; G:6441). The hard rule exists only for self-fix (self_fix_build.py:837). | Not enforced; no end-to-end reproduction. |
| 3 | **Change the cause, not the label** | Nothing guards code that turns a failure into success or not applicable (the case above). | No outcome-relabel guard. |
| 4 | **Know the blast radius** | KG tools `kg_callers`, `kg_blast_radius`, `kg_precedent` exist (agent_baseline.py:101) but are kept out of the build harness (code_task_mcp.py:83-117). | Implementer and reviewer never see callers or dependents. |
| 5 | **Fresh-context adversarial review that sees the whole change and checks what else breaks** | Adversarial prompt (G:2975). Reviewers get whole files capped at 12k chars per file and 48k in total (review_input.py:129-132). The prompt says "Judge ONLY against the stated task" (G:2986). | Changed lines in large files can be invisible. Broken callers are out of scope by instruction. Ambiguous product questions have no escalation path. |
| 6 | **Don't break existing flows** | The regression seat re-runs verify commands. With boss.verify.yaml only tests that *import the changed file* run (verify_manifest.py:31). The only end-to-end check is "app answers HTTP" (preview_service.py:2390). | A change that breaks an untouched caller passes `nothing_broke`. No smoke of existing user flows. |
| 7 | **Rebase on the latest base, resolve conflicts before review** | Base cloned once (sandbox.py:460); conflicts only at merge (software_factory.py:2560, task_shepherd.py:300). | Review and tests judge a stale base. |
| 8 | **Prove it with evidence** | PR shows commands with ✅/❌ and gates (pr_description.py:41). | No screenshots, video or logs tied to the acceptance criteria. |
| 9 | **Update the docs the change affects** | docs_agent writes a feature-doc block on a separate branch after the PR (docs_agent.py:391). | Existing docs the change invalidates are not updated in the build. |
| 10 | **Babysit the PR until merged** | `_wait_for_ci` waits once (G:2721). | No CI-failure fix loop, no watch for new conflicts, no handling of new human comments. |
| 11 | **Tell the reviewer how risky it is** | `risk_tier` is internal only. | No risk rating or "how closely to look" in the PR. |
| 12 | **Write down every correction** | Only machine signals are learned (learning_writeback.py:1046, 1349). Steers live only on one run (builtin.py:3658). Fix notes, PR rejections, human PR comments and repeated reviewer findings are never learned. person_wait passes `evidence=[]`, so the learning is dropped (person_wait.py:324). | **The same mistake costs the same human intervention every build.** Nothing promotes a repeated correction to a standard. |
| 13 | **Small always-loaded memory; conditional knowledge loaded on demand** | Fixed block of about 2.5k tokens. Mandated standards are pasted whole, uncapped and unscoped (platform_standards.py:255). Guide skills have real progressive disclosure (skill_definitions.py:54) but not in governed builds. Role instructions, guides and agent memory never reach governed builds. Repo standards from the design stage are dropped (G:5976). | Customising an agent changes nothing in its builds. Standards grow without scope. |
| 14 | **Engineering opinions**: don't weigh development cost; reproduce end to end; prefer existing mechanisms | Built-in rules push the other way: "smallest correct change" (code_agent.py:1089), "nothing unasked" (G:2314). | No place for opinions. Models' human-time cost estimates bias them toward cheap, low-quality designs. |
| 15 | **Measure skills, tools and context; don't trust popularity** | Experiments vary only harness, review rounds and routing (experiments.py:25-33). MCP returns unbounded `json.dumps` (mcp/server.py:540). | Cannot A/B a skill, standard or knowledge mode. Tool output is not token-budgeted. |
| 16 | **Plan visually and decide by clicking options** | PRD has fixed sections, no Alternatives, one approve/reject (meeting_service.py:1176-1251). Mermaid does not render in the console. The architecture step never reads the repo and is in no catalogue workflow. The plan review asks no alternatives, reuse or blast-radius questions (G:3788). | A wall of text, or auto-approval that skips the open questions. |
| 17 | **People decide only what is ambiguous; nothing stalls** | 24h deadline with default `hold`; on expiry nothing happens. 77% of 165 decisions expired (decisions.py:428). Recovery asks are "press Retry" text with no deadline. The risk floor makes every tier ≥2 deploy, ux and spec gate manual (workflow_capabilities.py:1038). Step recovery uses the routed model, not Jev (agent_decision.py:740). The run supervisor's Jev verdict is recorded, never enforced (runner:4076). | **The biggest source of stalls.** |
| 18 | **Applicability decided before a step, failure cause classified after** | Applicability is about 20 coded `not_applicable` returns. The model pre-check is off by default. A model classifies failures only for build, deploy and waiver. | Every other failure goes straight to a person, and "not applicable" is inferred from failures. |
| 19 | **A first mate**: one conversation that fans independent work out in parallel and drives it to done | Plans run sequentially and need a confirm (intake.py:161). Parallel lanes only inside one multi-component app (change_sets.py). Triage is per event. | No fan-out across apps or repos, no backlog sweep, no "drive to done and report". |
| 20 | **Long objective loops with stop conditions and caps** | Captain auto-improve, self-heal. | No general "objective + stop condition + spend cap" loop (e.g. a persona usability loop). |
| 21 | **Preview that is right about what an app is** | See the case: monorepo `dev` script run as the app; no `ps` in the image (Dockerfile.community:154); no env contract for guessed entries; "builds FastAPI" ranked as "serves". | Real preview bugs across all apps. |

Where BOSS is already ahead of the talk: P1/P2/P3 review with implementer replies; budgets, receipts
and identity on every effect; a knowledge graph with ranked recall; incident response; multi-repo
change sets; one governed path for all surfaces. Those are kept and built on.

## 3. Design

Eleven workstreams, each a generic capability in the shared path with tests, never a per-app patch.

### A. Study before change: the protocol BOSS enforces

Every build that changes existing code, and every "fix this" request, runs a **Study** stage before
implementation. It produces a structured **Study record** that later gates read.

1. **Symptoms.** Every failing run, step and exact error string (for a fix), or the acceptance
   criteria (for a feature).
2. **Reproduction.** For each symptom, re-run on the real branch head in the same sandbox, end to
   end where the product is used end to end. Output: a log matching the error, or "could not
   reproduce" with what was tried.
3. **Causes.** For each symptom, the file:line that produced it and the trigger chain. None may be
   left unexplained.
4. **Existing mechanisms.** Search the codebase and the KG (`kg_precedent`, `kg_why`, search) for
   code that already handles this class of problem. Output: what exists, and whether it is reused
   or extended.
5. **Classification** of each cause: product bug / environment gap / app's own fault / truly not
   applicable. "Not applicable" needs proof the step cannot apply.
6. **Blast radius.** Callers and dependents (`kg_callers`, `kg_blast_radius`), workflows and apps
   that reach the changed code, and the existing flows at risk. Output: a list of what to verify.
7. **Alternatives.** At least two designs when the change is more than mechanical, with why one is
   chosen. Development cost is not a reason; quality, reuse and maintainability are.

Changes to the pipeline:
- **Invert the risk rule:** edits to existing code are at least tier 2 for the study
  (pipeline_profiles.py:282), sized by blast radius, not file count.
- Give the implementer and reviewer **read-only KG tools** in the build harness (code_task_mcp.py).
  They are read-only, so the credential concern does not apply.
- **Enforce red-first:** a bug fix must have a failing reproduction (end to end where applicable)
  before the fix, as self_fix already requires (self_fix_build.py:837).
- **Claims-to-evidence:** the PR description's "causes" and "fixed" statements must each link to a
  Study record entry. Unlinked claims are marked "unverified".

### B. Learned context: corrections become memory

- **Every human correction is written as a project learning, with evidence:**
  - steers;
  - retry fix-notes;
  - PR rejections;
  - human PR review comments;
  - a reviewer finding that recurs across builds.

  Fix person_wait's `evidence=[]` drop.
- **Promotion.** When the same learning recurs N times or a person confirms it, BOSS proposes it as
  a **standard** (one click or a Jev decision). Standards carry scope: paths, languages, step kinds.
- **Governed builds load the agent's own setup:** role instructions, guide skills (progressively
  disclosed, as in skill_definitions.py), and agent memory. They also carry forward the repo
  standards the design stage derived (G:5976).
- **Scoped, budgeted standards.** Only standards whose scope matches the touched paths and step,
  within a token budget, ranked like learnings. Report what was left out.
- **Seed principal-engineer opinions as default standards**, editable per org:
  - Don't weigh development cost when choosing a design.
  - Reproduce bugs end to end before fixing.
  - Look for an existing mechanism before adding one.
  - Never turn a failure into success without proving the failure carries no information.
  - A change to existing code must keep existing callers working.

  Rephrase "smallest correct change" as "smallest change *that fixes the cause*".

### C. Review that sees the change and judges what it breaks

- Reviewers get the **diff hunks with context** first, then whole files within budget. A change is
  never cut. If it does not fit, the review is split across seats by file group and that is said.
- The reviewer's scope includes **callers and existing flows** from the Study record's blast radius,
  not only "the stated task".
- **Ambiguous product questions** found in review become person questions through the decisions
  path (F). Everything else is fixed automatically in review rounds, as today.
- **Outcome-relabel guard:** any diff that makes a failure count as success or not applicable (for
  example in workflow_capability_runner or workflow_outcomes) requires a blast-radius run and a
  justification in the Study record.

### D. Regression protection

- **Test selection by dependency, not direct import.** Select tests that reach the changed code
  transitively (KG call graph plus import graph), and always the project's P0 suite (test tiers).
  Fall back to the full suite when the graph is unknown (verify_manifest.py:31).
- **Existing-flow smoke:** after a change, run the app's critical user flows (declared in
  boss.verify.yaml or learned from earlier evidence) end to end, not only "it answers HTTP".
- **Replay gate for fixes:** a fix for observed failing runs replays those runs (recheck or
  retry_workflow_step) with the fix installed, before merge. The PR shows the new outcomes.

### E. A clean PR pipeline

- **Rebase onto the latest base and resolve conflicts before review and before the gates**, and
  again before merge if the base moved.
- **Evidence:** each acceptance criterion links to proof (a screenshot or video from the preview
  for UI, a log or test output for backend), recorded by the test step and shown in the PR.
- **Docs pass in the build:** find docs the change invalidates (references to changed names,
  commands, settings, screens) and update them in the same PR.
- **Risk rating** in the PR from blast radius, tier, touched surfaces and test evidence, with "how
  closely to review" guidance. Low-risk PRs say why.
- **Babysit until merged:** watch CI and the base. A CI failure starts a fix round. A new conflict
  is rebased and resolved. A new human comment is answered or fixed. All are bounded by the run's
  budget.

### F. Autonomous decisions: Jev first, a person last, nothing stalls

One decision ladder for gates, step recovery, applicability and review escalations:

1. **Coded facts** when the evidence is unambiguous (for example, the manifest says
   `requires: {sudo: true}` and the preview sandbox cannot do sudo).
2. **Jev** answers a typed question with fixed options (`make_decider`).
3. **The global decision model** when Jev's confidence is below the threshold (existing
   `SecondOpinionDecider`).
4. **The step's recovery agent** gathers more evidence (logs, repo, host) and re-asks.
5. **A person,** asked once, with BOSS's recommendation preselected and the evidence, posted to the
   project's channel.

The typed questions:
- **"Does this step apply to this app?"** (before a step): applies / does not apply / unknown.
  Answered from the **app profile**: how the app runs (one process, compose, VM install), entry
  points, services, deploy target, and what worked before. The profile is built from the
  repository and earlier runs and refreshed by every build. Steps declare **requirements**
  (preview: a runnable app the preview sandbox can start; deploy: a target or manifest; readiness:
  an operated app).
- **"Why did this step fail?"** (after a failure, for every step kind, not only build and deploy):
  app bug / BOSS environment gap / does not apply / transient.
- **"What next?"**: retry / repair build / skip this step / skip the steps with the same unmet
  requirement / ask a person.

Rules:
- Applicability is decided **before** a step from the profile, never inferred from a failure.
- A "does not apply" learned during a failure updates the profile with its evidence, and is shown.
- **Deadlines with safe defaults.**
  - Every person question has a deadline.
  - *Reversible and safe* actions (skip a preview, mark not applicable, retry, start a repair
    build) apply BOSS's recommendation when the deadline passes, and say so.
  - *Irreversible or risky* actions (deploy, merge, spend past a cap) are never defaulted. They
    remind (channel, console) and escalate to the next approver.
  - Replace the `hold`-then-expire behaviour (core.py:489, decisions.py:55).
- **The risk floor becomes evidence-based.** A tier ≥2 gate asks a person only when the ladder is
  unsure, or the action is irreversible and the org's policy names a person.
- **Use what the supervisor decides.** Its Jev verdict on workflow steps acts, within the allowed
  actions, instead of being record-only (runner:4076).
- Step recovery uses `make_decider` for its structured decision after its read phase.
- **Every decision records its decider** (coded / Jev / model / agent / person / deadline default).
  The **override rate** per app and step is measured, to tune thresholds.

### G. Planning people can review in a minute

- The PRD gains **Alternatives** and **Open questions as separate decisions**: each is a clickable
  option card answered on its own, so auto-approval cannot skip them.
- A **visual plan page**: an HTML prototype or option comparison in the app's own design system,
  with inline comments that go back to the agent and option buttons for decisions.
- The console renders **Mermaid**.
- The architecture step **reads the repository** (existing mechanisms, blast radius) and is part of
  the catalogue workflows that change existing systems.
- The plan review asks about alternatives, reuse, blast radius and maintainability, not only edge
  cases (G:3788).

### H. A first mate

Ask BOSS can split a request into independent tasks across apps and repos, start them in parallel
(each through the full pipeline), sweep a backlog (open issues, Plane) for actionable items, and
drive the set to done with one progress report. It is a thin client of the one agent path
(BOSSAPP-759), not a second engine.

### I. Measured, not assumed

- Experiments can vary **skills, standards, instructions and knowledge mode** (experiments.py),
  reporting tokens, time, interventions and review findings per arm.
- **Tool output budgets:** compact, token-efficient output for BOSS MCP and tool results, with
  per-tool caps and a "more" handle. The MCP server stops returning unbounded JSON
  (mcp/server.py:540).
- A token audit of every build agent's always-loaded prompt, published per release.

### J. Objective loops

A workflow kind: objective + stop condition + caps (tokens, iterations, money, time), running
unattended. Examples: "use the app as a 7-year-old, find the first confusing thing, fix it,
repeat"; "cut p95 page load below X". Each iteration goes through the clean-PR pipeline. In the
morning there is a list of PRs and evidence.

### K. Preview that is right about the app (the bugs found in the case)

- Never run a monorepo root `dev` script as "the app". Resolve the app first; the root script is
  for workspaces.
- **Environment contract** for guessed entries (for example `PYTHONPATH` from the repo's own
  scripts or pyproject). Better, prefer a declared `boss.preview.command`.
- "Builds an app object" is not "serves": require a run path (`__main__`, uvicorn, a start script)
  before ranking an entry as a server.
- Add `procps` to the preview sandbox image.
- Applicability from the manifest before launch (F), not after a failure.

### L. Done contracts and a completion check (the obvious is never left undone)

**Why.** The spec's own tickets were first filed as eight unlinked tickets among about 150 in
Plane. Nothing made them a set, so no agent could tell they belonged together. The model could
easily have linked them. Nothing asked what "done" means for a ticket set, and nothing checked
before reporting. This is the same failure as the deadletter fix: capable models miss the obvious
when the harness never states it.

- **Done contracts per output kind**, as checkable rules, versioned like standards:
  - **Ticket set:** one parent; every child linked; an order; spec and branch links at the top of
    each description.
  - **PR:** evidence per criterion; risk rating; blast radius; Study record link.
  - **Fix:** every symptom has a reproduced cause; no failure relabelled without proof; replay
    outcomes.
  - **Decision:** recommendation; evidence; deadline; owner.
  - **Doc or spec:** linked from where it will be needed (ticket, README, index).
  - **Deploy:** health and existing-flow smoke on the target.
- **Tools enforce what they can.** Creating two or more related tickets requires a parent, or
  creates one. A PR cannot open without its evidence section. Making the wrong shape hard beats
  catching it later.
- **A completion check before anything is reported done.** A separate pass with a fresh context
  receives the request, the outputs and the contracts, and answers: *could the next person or
  agent pick this up and finish it without asking anything?* Each failed check goes back to the
  producing agent. Only real ambiguity goes to a person (§3F).
- **Corrections become contract checks.** A structural correction from a person ("these should
  be linked", "this needs evidence") is proposed as a new check in the matching contract, extending
  §3B. From then on it is enforced, not remembered.

## 4. Phases

**Phase 1 (P1): stop shipping shallow fixes and stop stalling.**
- A Study stage and record, KG read tools in builds, red-first enforced, risk rule inverted.
- B: corrections become learnings, governed builds load agent setup, seeded opinions.
- C: diff-first review with caller scope and the relabel guard.
- D: test selection by dependency, replay gate.
- F: the decision ladder with deadlines and safe defaults, applicability before the step,
  supervisor verdicts act.
- K: the preview bugs.
- L: done contracts, tool enforcement and the completion check.

**Phase 2: the clean PR and planning.**
- E: rebase-first, evidence, docs pass, risk rating, babysitting.
- G: alternatives, option cards, visual plan page, Mermaid, architecture reads the repo.
- D: existing-flow smoke.

**Phase 3: scale.**
- H: first mate.
- I: measured skills, standards and tool budgets.
- J: objective loops.

Each workstream ships with tests through the real runtime (scripted models where needed) and is
proven on a real run before release.

## 5. How we know it works

- **The replay benchmark.** Give BOSS the original request ("these runs show deadlettered and
  failed although the builds merged; fix it"). Pass means all of:
  - a Study record naming the authority cause, the manifest cause and the three preview causes,
    each reproduced;
  - no skip switch and no relabel of failures;
  - fixes with regression tests per cause;
  - other apps' previews unchanged;
  - the original runs replayed green.

  Re-run every release.
- **Scorecard per PR:**
  - human interventions;
  - decisions expired (target near 0; 77% today);
  - share of decisions made by Jev, the model, and a person;
  - person override rate;
  - regressions escaped (caught after merge);
  - tokens and wall time;
  - review findings by priority.
- **Context usefulness:** learnings and standards cited, and their effect in experiments.

## 6. Risks and limits

- **More stages cost time and tokens.** Mitigation: the Study is sized by blast radius, cheap for
  trivial edits, and cached in the KG. Measure against a plain coding agent (harness efficiency is
  the product).
- **Deadline defaults could act wrongly.** Mitigation: they apply only to reversible actions,
  always state what was done, and are measured by override rate.
- **Jev availability.** Without `TYPESAFE_API_KEY` on a host, the ladder starts at the global
  decision model. Behaviour is the same; cost and speed differ.
