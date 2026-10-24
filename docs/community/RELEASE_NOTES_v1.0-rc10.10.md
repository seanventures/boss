# BOSS Community 1.0.0-rc.10.10 candidate

RC10.10 builds on rc10.9. It brings the **knowledge graph** (BOSSAPP-622), a new default database image, **one agent pipeline** for every place BOSS's agent chooses what to do (BOSSAPP-759), **project creation**, the **Email (SMTP) connector**, and the second round of alerting fixes.

This is a test candidate.

## What changed

### Knowledge graph
- **A knowledge plane in BOSS's own database.** It holds a code graph (Python, plus TypeScript, JavaScript, Go, Java and C# through tree-sitter) that resolves across files, and facts learned from builds. Pull requests, decisions and releases link to the symbols they touch.
- **Retrieval fuses text, graph and vector results.** An agent turn starts with a budgeted, quoted knowledge pack. A decision that cites facts shows "Based on".
- **Each project has an AGE graph** with read-only Cypher. Agents use the `kg_*` tools: `kg_symbol`, `kg_callers`, `kg_blast_radius`, `kg_why`, `kg_search`, `kg_precedent` and `kg_propose_fact`.
- **Every shipped agent can read knowledge.** Builder and reviewer roles can also propose facts.
- **The knowledge setting is `on`, `keyword` or `off`.** `keyword` gives agents plain full-text matches only: no graph hops, no vectors, no learned facts and no precedent. A person's own console search stays full.
- **A console explorer and settings,** with a promotion queue.

### New default database image
- **Postgres 16 on Alpine, with pgvector and Apache AGE compiled in** (`boss-community-db:pg16.12-vector0.8.1-age1.6.0`, built from `db-image/Dockerfile`).
- **The upgrade is an image swap.** The existing data volume opens unchanged, and the schema step creates the extensions. `boss upgrade` first takes the complete protected backup, then swaps the image.

### One agent pipeline (BOSSAPP-759)
- **Metatron and the console talk to the same BOSS agent** through one `agent_turn`. A Metatron sentence no longer goes through its own keyword reader and model decision. The Metatron terminal and Ask BOSS share the general chat thread.
- **The captain autopilot is retired.** Its powers are now the BOSS agent's actions: watch, goals and set goal the agent may run. Drive, overwatch and theater are offered to a person, who presses them. The worker's always-on overwatch now only observes.
- **Every entry point that chooses a BOSS action goes through the pipeline:**
  - alert triage and flap tuning;
  - the incident verdict;
  - GitHub issue triage and inbox replies;
  - focused agents;
  - the workflow agent step's agent choice;
  - agent-to-agent talk;
  - step recovery.

  Receipts, budgets and outcomes are unchanged. An enforcing test keeps new entry points on it.
- **Every shipped agent has the same base.**

### Projects
- **Create a project:** `POST /v1/projects`, the agent's `create_project` action (also from the Metatron terminal), and **New project** in the top bar's project menu. A new project is seeded the same way as the default one: budget policy, members, agents, skills, MCP servers, workflows and trackers.
- **The build composer is now "New build".** "New project" appears only in the project menu.

### Email
- **Email (SMTP) is a connector.** Enter one connection URL (`smtp://` or `smtps://`) or the separate fields. **Send test** shows the server's own reply. The key goes to the secret store and is shown only as "set".
- **One mail transport for every sender.** Invites are branded, with a delivery status, the real reason for a failure, **Resend** and **Copy link**.
- **Mailpit, the local test inbox, is opt-in:** `--with-mailpit` or `BOSS_EMAIL_DEV_INBOX=1`. It is off by default.

### Alerting, round 2
- **A change never silently drops an app's monitoring.** A pull request puts back alert and dashboard entries that the change removed without naming them. A deploy whose manifest lost rules keeps those rules running, and raises one alert until they are back. The Rules list marks a rule the latest manifest dropped.
- **Tune a rule where it lives,** from the Rules list.
- **A settle window after a verified fix.** A lagging alert inside the firing rule's own window joins the fixed occurrence as evidence and does not reset the incident.
- **Grafana links** work out the browser address themselves.
- **The time label** on logs uses one clock.
- **rc10.9's BOSSAPP-602/603/604 items are fixed:**
  - the release-commits check credits a run's own later steps;
  - runbook file names are stable;
  - the pack never ships over a broken test suite;
  - investigate() reads only the current occurrence;
  - a model out of credit says so and names the provider.

### Setup and safety
- **The boss-1 API-key fixes.** Add a model drops stale pills and says why Connect is disabled. "Paused · budget" names the cap that is actually binding. A stale legacy exposure hold no longer blocks paid work forever.
- **A refused write says its real reason.** This covers setup, observability and connection-proof refusals, and the invite page's password hint.
- **The installer ships the full commit SHA,** and an ambiguous one is resolved or refused at backup time.
- **The navigation no longer flashes the org picker** mid-session.
- **The web console type-checks** against this tree's own API client.

## Validation
- **Targeted tests** on the combined branch, for every change above. The operator chose targeted tests only. They covered:
  - knowledge, on the new database image;
  - email;
  - alerting round 2;
  - project creation;
  - the one agent pipeline and Metatron;
  - the installer and the image swap;
  - the identity route matrix and MCP parity;
  - web `tsc` and the touched web tests;
  - the P0 public-tree check.
- **The plumbing e2e gate** (`scripts/e2e-gate.sh plumbing`, no model calls) passed 19 of 19 checks.
- **The real-agent release gate was not run** for this candidate. It needs the operator's spend approval.
- **No full suites were run.**

## Known issues
- **BOSSAPP-760:** the GitHub App callback can answer "unauthenticated".
- **Metatron's client times out on long turns,** and a turn can be refused with "budget admission busy" (both found 2026-10-23).
- **The agent's tools can't target another project.** A project the agent creates is set up, but follow-up actions still act on the current project.
- **Knowledge graph ship gate is open** (`case_study_primary_not_run`). These have not been run:
  - the case-study primary criteria;
  - the 1M-node AGE benchmark;
  - model precision;
  - retrieval at the 10M-node scale target.
- **BOSSAPP-759 follow-ups:** the captain's overwatch playbook stays deterministic inside its tools by design. Any further entry point that chooses a BOSS action must join the pipeline; the enforcing test lists them.

## Install and upgrade

Select `v1.0-rc10.10` explicitly with the installer's `--ref` option, from a checkout of that same tag. See the [Quickstart](QUICKSTART.md).

Before upgrading an existing install, take a complete protected backup (`boss upgrade` does this). The database image swap happens after that backup. Changing the checkout back does not undo database migrations.
