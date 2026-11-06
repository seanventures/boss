# BOSS Community 1.0.0-rc.10.11 candidate

RC10.11 builds on rc10.10. It brings the **new console flow** (Welcome, Repo and models, New project, Govern › Spend), **several GitHub accounts per organisation**, **deploys that wait for their merge**, **shared hosts across an organisation**, the **knowledge-graph case studies** (banking regulation, AML, data quality), Sean's **low-trust containment and egress fixes**, and a **preview of autonomous agents** that ships **off by default**.

This is a test candidate.

## What changed

### Start, set up and sign in
- **A 3-step Welcome, then one Setup page** for GitHub accounts, repositories and models (BOSSAPP-808). Advanced keeps every option; Budget lives in Govern.
- **Several GitHub accounts in one organisation.** Each account has its own token, and a build uses the one that covers its repository.
- **One "Connect GitHub":** your own GitHub App in two clicks, with access that does not expire (815).
- **New project has three doors,** including **Import a repo**, and a project page (BOSSAPP-810). A feature for an imported repository changes that repository's code.
- **Password first, passkey on request.** A passkey is never the only way in (812). A fresh install can be claimed on its own site from a private network (813), and claiming never asks for the shared console password.
- **Claude sign-in works the first time.** The installer writes the secret store's master key, and the first press after "Confirm it's you" no longer says "Reload and retry" (897).
- **Models: one menu per job,** with a Subscription or API key choice for Claude Code and Codex. A model only runs on an engine of its own family.

### Govern
- **Govern opens on Spend,** with one "spent" figure on every screen, spend by project and per-project monthly limits enforced at reservation (BOSSAPP-807). **Pace** is the first card on Limits.

### Builds, merges and deploys
- **A deploy waits for its pull request's merge.** The step says so, links the PR, and needs you until GitHub shows it merged. A merged build deploys its **merge commit**, not the head pinned before the merge.
- **BOSS can merge an approved pull request** when the install turns it on (`BOSS_ENABLE_SANDBOX_MERGE`). It merges the PR's own repository. In a repository without CI it goes ahead on BOSS's own checks, and a merge that could not run fails its job.
- **Shared hosts.** A verified host is usable by every project in its organisation, and its page opens from each of them. A second app with the same name on a shared host gets its own place.
- **Compose secrets.** A deploy generates the session keys its app's compose file reads; previews get the session secret too. **Setup › Demo logins** shows the password an imported project's deployed app runs with.
- **Faster repairs.** An approved PRD is the build's design (a short mapping pass, not a second design session). A repair's review and consistency check judge only what the repair changed.
- **A browser suite runs where its config is.** A step whose work finished while the run was stopped ends when the work did.
- **Run report: Open report** shows the whole report in a dialog, every section open.
- **Every build is reviewed, whatever its tests say,** and review findings are P1/P2/P3. One warm sandbox per build lineage; retries resume at the failed stage.

### Knowledge graph and case studies
- **Rulebooks into the Knowledge Graph:** upload with a confirmed split, DQ rules from clauses, embeddings, citations and neighbours.
- **The banking regulation, AML alert investigation and data-quality (DQ) pipelines** run end to end on real projects, with role agents, person gates and a per-step graph trace. A bank's own outside agent can run a pipeline step under BOSS's contract.
- **Domain and Context layers on one canvas,** with stored decision edges.

### Safety (Sean's public-line fixes)
- **Low-trust containment,** the sandbox network fence fails closed, and egress reports the truth.
- **The build agent's tools are checked,** the installer recovers its own source, and the GitHub App update callback works.
- **Release approval cards,** alerting display fixes and console route fixes.

### Autonomous agents — preview, off by default
- **What it is:** BOSS notices problems on its own (alerts, red CI, advisories, cost spikes, a new repository's dossier). A **Chief of Staff** agent triages each one into a single work item, and **squads** work it together on a shared board. The squads include an **AI SRE incident squad**. Every decision is on a ledger, every action goes through the autonomy gate, and **continuous learning** feeds a **Recommended roadmap**. In the console, **Agents at work** shows each item as a flow chart.
- **It ships off.** The operator has not reviewed it yet, and the browser walk has not run. With nothing set, the worker does no signal sweep, no triage, no squad turn, no automatic dossier and no learning tick.
- **You can still look at it.** The **Agents at work** area shows. On a demo project (its name says demo, sample, sandbox or test), **Load demo** loads four stories through the real machinery. **Triage now** runs once, and asks you before anything is handed to an agent.
- **To turn it on,** set `BOSS_AMBIENT_COS=propose` in the install's `.env` and restart the API and worker. With `propose`, every handover asks a person first. `route` hands A2+ classes straight to their squad. `BOSS_AMBIENT_SIGNALS=1` and `BOSS_AMBIENT_LEARNING=1` run the sweep or the learning tick on their own. `BOSS_AMBIENT_DISABLED=1`, **Stop all** and a project's **Stop** win over everything. See [AUTONOMOUS_AGENTS_SPEC §0](../product/AUTONOMOUS_AGENTS_SPEC.md).

## Validation
- **P0 plus the tests of the changed areas,** one file at a time against `boss_test`:
  - 485 Python test files (417 engine, 56 API, 12 worker): 7,376 tests passed.
  - Eight failures were new on the combined branch and are fixed in it: a real server address in three comments, the meeting-title and run-reach guards, the one-gate scan for the merge wait, the browser-suite pass, the checks-failing wiring test and the identity route matrix.
  - Every other failure also fails on `origin/main`, or passes on a clean test database (the shared database's leftovers).
- **Narrowed API coverage.** API files that only import a widely shared module were cut after 21 of them ran, to keep the gate within hours. API P0, every API test the merges added or changed, and every API test touching ambient, deploy, merge, host, kit or release planning code all ran.
- **The console:** `tsc --noEmit` is clean. 218 vitest files ran: 3,144 tests, and one new failure, fixed (the Run group now lists Agents at work). The other 14 failures also fail on `origin/main`.
- **The plumbing e2e gate** (`scripts/e2e-gate.sh plumbing`) passed 16 of 19 checks. The same 3 fail on `origin/main` in this environment: the app-start proof, and the connector and optional-settings checks that depend on it. The app listens but does not answer HTTP within 90 s.
- **The engines e2e gate** (`scripts/e2e-gate.sh engines`) passed 15 of 15 checks.
- **The real-agent build gate:** two HelpDesk AI Demo runs on the operator's laptop on 2026-11-06, `wfr_07e8e723b44f445c9656` and `wfr_2adddafc003f4533adb3`. The first went brief → build → PR → merge by BOSS → deploy → a healthy app.
- **Pre-existing failures** (also on `origin/main`):
  - the public-tree scan of three test fixtures;
  - `test_multirepo_change_sets`, `test_policy_approval_is_narrow` and `test_toolchains_java`;
  - `test_every_template_runs_end_to_end` and `test_every_template_says_what_it_needs`;
  - `test_knowledge_code_parse` and `test_knowledge_code_graph_db`;
  - `test_release_planning_api`, `test_check_reports_unset_ability` and `test_a_catalogue_default_never_pins_its_agent`;
  - the MCP parity audit and `test_community_boss_cli`'s compose-services check;
  - the ones `tests/known-failures.txt` lists.
- **No full suites were run.** The autonomous agents have had no browser walk.

## Known issues
- **The Approve button on the deploy step's panel can fail step-up.** Approve the deploy from the **Approvals** page instead.
- **The planning panel can cut scope** from a brief without saying so. Read the PRD before approving.
- **A PRD has no "Request changes".** You can only approve or reject it.
- **Add repo lists only one GitHub account** when several are connected.
- **The GitHub App's return link can say "expired"** after a successful install. The connection itself is made; reload Setup.
- **Autonomous agents are a preview.** They are off by default and have not had a browser walk.

## Install and upgrade

Select `v1.0-rc10.11` explicitly with the installer's `--ref` option, from a checkout of that same tag. See the [Quickstart](QUICKSTART.md).

Before upgrading an existing install, take a complete protected backup (`boss upgrade` does this). This release adds database tables for the autonomous agents; changing the checkout back does not undo database migrations.
