# BOSS Community 1.0.0-rc.10.12 candidate

RC10.12 builds on rc10.11. It brings the **project experience** (one page per project, New work, onboarding agent, Needs you), a rebuilt **Add a project** with **two doors** and **local-first projects** you can build on this machine and **publish to GitHub later**, **Where people decide** (the 7 decisions only a person makes), **Add a server**, **Watch this app**, **Delete project**, a larger **type scale**, and many fixes to the **always-on agents**. It also carries Sean's console polish and hardening from main, with three production-build fixes for it.

This is a test candidate.

## What changed

### Projects
- **One project page:** Overview, Work, Roadmap, Repo & knowledge, Deployments, Health and Settings, with a journey diagram, Needs you and the Captain (BOSSAPP-913/914).
- **New work** as a small dialog: Helm reads your request into a Task, picks the workflow, priority and estimate, and can chat before you start.
- **Add a project has two doors:** Start a new app, or Bring existing code (BOSSAPP-916). A new app gets a Project brief; existing code gets a Repo summary. Every button is covered by a browser test.
- **Same repo, same project:** choosing a repo that is already a project opens it, offers "Add work there instead", and makes a second project only after one confirmation; the API refuses a duplicate otherwise.
- **Delete project** with a full cleanup in one transaction (every row, its knowledge graph, previews and staging apps, local repos and work files); the GitHub repo and the audit log are kept. Type the name and confirm with your passkey.
- **A repo with several apps:** Roadmap and Repo & knowledge show this project's app first; other apps sit under "Elsewhere in this repo" (BOSSAPP-931).
- **PRD approval shows the PRD,** links the meeting where the panel debated it, and commits it to the repo as `docs/prd/<name>.md` (BOSSAPP-929).

### Local-first (BOSSAPP-915)
- **Build on this machine:** BOSS keeps a git repo per project; builds commit locally, review and merge happen in BOSS, and previews and staging deploy from the local repo.
- **Publish to GitHub later:** history, branches and open changes (as PRs); safe to re-run.
- **GitHub outages:** a build keeps its work locally, shows "Waiting for GitHub" instead of failing, and pushes when GitHub is back.
- **Import code** from a folder on this machine or a host, keeping its history.
- **BOSS can create the GitHub repo** for a new app.
- Backup and restore include the local repos.

### Governance and agents
- **Where people decide** replaces Govern › Autonomy: production deploy, merge to the default branch, spend above the cap, secrets, outside messages, deleting data, and who can do what. Rules can only get stricter; quiet windows and backup deciders really hold approvals; change a rule in plain words.
- **Decisions only a person makes never silently expire;** a merge question whose PR is already merged or closed withdraws itself.
- **Merges are idempotent:** merged state shows everywhere at once, a retry updates the open PR instead of opening a second one, a fix after a merge is a named follow-up, and a failed merge says why.
- **Always-on agents:** cards name the work and their buttons do what they say; squads read with their role's tools; work waiting on a person is parked; nothing waits forever; spent budgets ask a person; nudges run on their own.
- **BOSS reads its own upgrades and restarts** (`install_restarts`), so the infra squad can tell an upgrade from a crash.

### Infrastructure and monitoring
- **Add a server:** one input, one hand-over of access, and the Infra agent sets it up and watches it. A server gets a role, never an app.
- **Watch this app** on Project › Health, wired to the app's alert rules, contact points and the incident flow.
- **Deployments tell the truth:** an address is live only after a successful deploy; a running deploy shows its steps and logs live, with Fix and Roll back (BOSSAPP-930/932).

### Console
- **Type scale raised** to match Workflows (body 14px, small text 13px); a test fails any hard-coded font size.
- **Popovers open fully visible** on every screen size.
- **Confident headings:** Project brief, Repo summary, Task, Agent actions.

### Installer and release gate
- **Upgrades on installs older than 30 days:** the post-start check and rollback use the host key, not the retired shared key.
- **The app-start proof uses its own port;** a manifest's `PORT` no longer overrides it.
- **The gate checks local-first** without reading the install's `.env`.

### From main (Sean)
- Console polish without a reskin (BOSSAPP-899), plus hardening fixes: inbox cards for capacity failures and rules 403s, OIDC body caps, conversation streams, permission declarations, receipt double-spend.
- **Fixed here:** two CSS-module `:root` blocks and three colour tokens cut mid-expression stopped the console image from building.

## Checks
- Release gates: plumbing 20/20, engines 15/15, one real agent build (see below).
- Type check clean; key API suites pass; the web suite has no new failures against main (known failures are tracked in BOSSAPP-944).
- Installed and smoke-walked on a laptop with real projects.

## Known gaps
- Paperclip P0/P1 gaps (BOSSAPP-812) and Kiro Crew gaps (BOSSAPP-934) remain open.
- No production `next build` in CI yet.
