# Agentic software factory: autonomy audit of the console

**The theme:** BOSS is an agentic software factory. Always-on agents build, ship, watch and fix, and people approve only what matters.

**What the operator asked for (2026-11-07):** use the same principles as the import-project flow ("worked fine without any assistance and the agent was able to onboard pretty well"), with "monitoring agents across multiple areas … taking decisions autonomously", and "a compliance area where only it requires user input; the rest should move autonomously". Sean on the import flow: "the import stuff is pretty slick".

So this audit puts **autonomy first and UI second**. For every area it answers four questions:

- (a) what an agent can do end to end without a person, as onboarding does today;
- (b) which agent owns the area and watches it continuously;
- (c) which decisions are compliance boundaries that stay with a person, and why;
- (d) the gap between today and that.

The UI pattern comes second. It is the approved Add-a-project flow (`../project-experience/new-project-v2/`):

1. One multi-line input that accepts anything.
2. The agent reads live.
3. An **Agent actions** card to confirm or edit.
4. Everything else pre-picked as boxes and chips, with popovers.
5. One primary button.
6. You land on a live view where agents work and ask only what they cannot do.

Standing rules: premium and visual, diagrams over text, at most 2 clicks, nothing removed (parity), models and GitHub stay global.

Sources: three read-only sweeps of `apps/web/app/**` (about 120 flows) and the engine services that already carry autonomy (`engine/boss_engine/services/autonomy.py`, `autonomy_engine.py`, `shepherd_service.py`, `ai_sre_overwatch.py`, `finops_reconciler.py`, `focused_agents.py`, `self_fix_policy.py`, `deployment_health.py`, `learning_loop.py`). Click counts come from reading the code, not from running it.

Wireframes: [1-where-people-decide.html](1-where-people-decide.html) · [2-watch-an-app.html](2-watch-an-app.html) · [3-add-a-server.html](3-add-a-server.html).

---

## 1. The compliance boundary: "Where people decide"

There is one short list, kept in Govern. Only these wait for a person. Everything else runs on its own under the captain's authority. The area agents decide, act, and explain each decision as a diagram.

| # | Decision that needs a person | Default | Scope | Why it stays with a person |
|---|---|---|---|---|
| 1 | **Production deploy** (incl. prod rollback, picking a prod host) | Always a person; optional quiet windows | Install; a project may add approvers | Customers see it. It cannot be taken back. |
| 2 | **Merge to the default branch** (incl. installing a BOSS self-fix) | Always a person, after review and tests | Install; a project may name maintainers | It is what ships next. Self-fixes already require approval (`self_fix_policy.py`). |
| 3 | **Spend above a cap** | Above $500 a run or a day (today's Full-auto default) | Install; per project | Money cannot be recalled. Below the cap, FinOps decides. |
| 4 | **Secrets and credentials** (add, reveal, rotate, grant, hand over SSH access) | Always a person, with a passkey | Install | Only a person can hand over a key. Agents use secrets by name and never see them. |
| 5 | **Talking to the outside** (email or post to customers, status pages, new outside channels) | Always a person | Install; per project | It cannot be unsent. Messages to your own team do not count. |
| 6 | **Deleting data** (drop, purge, retire a host, forget facts, delete sent telemetry) | Always a person, with a typed name | Install | Gone is gone. Agents may archive; only a person deletes. |
| 7 | **Who can do what** (roles, invites, API keys, agent powers, autonomy raises) | Always a person, with a passkey | Install | An agent never raises its own powers or anyone else's. |
| 8 | *Changing this list* | Fixed: Owner and passkey | Install | So no agent can move the line it works under. |

Every entry has an audit trail (who decided, how fast, the diagram), a "who decides" seat with a backup, an optional "on my behalf" delegate, and a 30-day replay that shows the effect before a change is applied. A card can make the rule stricter. It can never make it looser than the irreversible backstop in code.

**What exists today:**
- `autonomy.py` already encodes the boundary as code. Full auto never approves anything irreversible (deploy, merge, delete, send, pay, rotate, grant, prod, external), anything whose cost it cannot read, anything above the threshold, or anything above risk tier 1. Every auto-approval writes a record.
- `autonomy_engine.py` raises autonomy from evidence (by proposal only) and lowers it immediately on a violation.
- Approval seats, "Approve on my behalf", and the read-only approval rules exist.

**The gap:**
- None of this is a single page a person can read. The boundary is a regex in code, an A0–A3 grid in Govern, a seat table in Identity, a read-only rules list in Ops, and a duplicate control in Project Settings and v2.
- There is no "what did agents do alone today" view.
- The area agents are not named owners.

Wireframe 1 makes the list the page. It folds in the A0–A3 grid, Full auto, seats and delegates, and lists 21 of 21 controls kept.

---

## 2. Autonomy map: area × owning agent × today × gap × human gate

"Owner" is the always-on agent accountable for the area under Helm, the captain. **Bold** in the last column is a boundary decision from section 1. Everything else in that column is no longer a gate.

| Area | Owning agent (always on) | (a) Autonomous end to end today | (d) Gap to close | (c) Human gate |
|---|---|---|---|---|
| **Onboard a repo or folder** | Onboarding | **Yes.** Maps code, runs setup and tests, explains failures, finds rules, proposes a backlog. This is the reference behaviour. | Staging setup still comes later, and the multi-repo component graph is built by hand (`multirepo/app-components.tsx`). | Accept the backlog (optional); **merge** |
| **New app / new work** | Helm, then the build crew | Mostly. PRD panel, build, review, tests, PR. | PRD approval is a gate by habit, not a boundary. Make it optional per project ("approve the brief" is enough). | **Merge**; **spend above cap** |
| **Stalled work** | Shepherd | **No.** Phase 1 only observes, classifies and proposes ("I would do X"). Nothing executes (`shepherd_service.py`). Focused agents (`queue_helper`) can relaunch factory jobs. | Phase 2: execute ledger moves (retry, re-route, raise the turn budget within the cap, restart a preview) alone, with a receipt. Escalate only on a boundary. | **Spend above cap** |
| **Review** | Reviewer (focused agent) | Proposes reviews and never merges. | Review comments should be resolved by the build crew without a person, as they partly are now. | none |
| **Deploy to staging / previews** | Release (+ Preview keeper) | Partly. The Preview keeper restarts previews. Staging deploys go through a plan, then a ChangeSet approval. Raw release-packet JSON in `infra/deploy-sheet.tsx`. | Staging deploys are not on the list, so they should run alone after green checks. The packet should be generated, not typed. | none |
| **Deploy to production** | Release | Prepares and preflights. | Show the prepared deploy as one decision with its diagram. | **Production deploy** |
| **Incidents and alerts** | SRE on call | **No.** `ai_sre_overwatch.py` is observe-only ("never auto-remediate"). Remediate is a button. Rules and contact points are set up by hand (about 31 clicks for one app). | Wireframe 2: the SRE agent sets up monitoring from the app and its code, then fixes reversible problems (restart, roll back staging, scale, mute ≤ 1 h, fix PR) alone. | **Prod rollback**; **status page / customers**; verifying a new outside channel |
| **Hosts and capacity** | Infra (new standing watch) | **No.** A 4-stage manual stepper. You run a command and press "I ran it". No one watches disk (the 2026-10-10 disk-full incident). | Wireframe 3: the host onboards itself after one credential hand-over. The Infra agent then watches disk, Docker, ports and certificates, and prunes or renews alone. | **Hand over access**; **retire a host**; outside DNS changes |
| **Security / CVEs** | Security (new standing watch) | Partly. Security review runs as a build step (`security_scan/`). I found no standing dependency or CVE watcher. | A daily scan of each repo's dependencies. Open fix PRs alone and rate the risk. | **Merge**; **secrets** if one leaked |
| **Spend** | FinOps | Partly. `finops_reconciler.py` reconciles commitments and opens review decisions. Small initiatives auto-approve. "At limit → cheaper models" exists. | Act alone under the cap (switch tier, pause runaway jobs). Explain each move as a diagram. Propose caps from history instead of raw numbers. | **Spend above cap**; changing caps |
| **Knowledge and learning** | Knowledge | Partly. `learning_loop.py` records outcomes. Lessons wait in a proposed queue (`memory-ui.tsx`). Rules wait for review. | Promote lessons alone when backed by evidence (repeat success, no contradiction). Regulatory rules stay reviewed. Segment pasted regulation text automatically. | Regulatory rules in a compliance pack (domain choice, per pack); **forget a fact** |
| **Workflows** | Helm (program manager) | Partly. "Describe it" drafts a workflow onto the canvas without explaining it. The step inspector is a raw prompt plus field wiring. | Agent actions card + diagram. Agent-drafted steps. Inputs wired by the agent. | none (a workflow change is not on the list) |
| **Agents, skills, MCP** | Helm (program manager) | Partly. "Draft it" covers the persona only. Skills use the Describe dock. MCP comes from a catalogue. | "Describe the job": the agent drafts role, skills, MCP and tool allow-list. The model is routed globally. | **Granting powers** (agent powers ⊂ who can do what); **MCP auth tokens** |
| **GitHub and models (setup)** | Onboarding | Partly. It detects signed-in CLIs and Ollama, and runs 3 access checks. | One input ("paste a repo, or say which model"). Routing defaults stay Smart. | **Credentials** (token, sign-in) |
| **Identity** | none (by design) | No. Everything here is on the list. | UI only: "invite like Sarah", "describe the role, BOSS drafts the matrix", project pickers instead of typed ids. | **Who can do what** (all of it) |
| **Self-fix** | Self-fix (Shepherd lineage) | Builds, tests and prepares its own platform fixes. Protected paths are refused mechanically. | Show installs as merge decisions on the list. | **Merge** (install); **outside** (send upstream) |
| **Notify / talking to people** | Helm | Partly. Digest, contact points with verification. | Fold into the "Tell" chip everywhere. Team messages are not gated. | **Talking to the outside** |
| **Govern itself** | none | n/a | Wireframe 1. | **Changing the list** |

**New watchers proposed:** Security (CVEs) and Infra (host health) as standing agents. Shepherd moves from phase 1 to phase 2. The SRE agent moves from observe-only to fixing reversible problems. All four act only below the boundary, write a receipt per action (the pattern in `focused_agents.py`), and show up in the "last 24 hours" lanes on wireframe 1.

---

## 3. Ranked list of console flows

Ranking = autonomy gain first, then UI gain, then effort. Impact: H/M/L. Effort: S (≤ 1 week), M (2–3 weeks), L (> 3 weeks), estimated from the code read.

| # | Flow (where) | Today: clicks / steps | What hurts | Pattern applied (input → agent actions → boxes → 1 button → live view) | Owner agent | Autonomy | UI | Effort |
|---|---|---|---|---|---|---|---|---|
| 1 | **Where people decide** (Govern › Autonomy, Identity › Seats, Ops › Approval rules, Account › Approve on my behalf, Project › Settings › Autonomy, v2 AutonomyControl) | 7 clicks across 3 areas to see who decides. "Over $200 → Sarah" takes 8. | The boundary is scattered and unnamed. A0–A3 codes. Duplicates. No view of what agents did alone. | Rule in plain words → Rule change card with 30-day replay → 7 cards + popovers → **Apply** (passkey) → lanes + decision diagrams | Helm + all | **H** (the frame for everything else) | H | M |
| 2 | **Watch an app** (Observability › Connect an app, Rule builder, Notify, Silences, Reliability) | ≈ 31 clicks, plus pasting env vars and redeploying the app | You pick the telemetry path. PromQL and label matchers. Contact points, policies and per-rule consent are set by hand. The SRE agent only observes. | "HelpDesk staging, wake me if replies stop sending" → watches with baseline and backtest → 6 boxes → **Start watching** → on call at once | SRE | **H** | H | M–L |
| 3 | **Add a server** (Infra › Add › Host, host stepper, App target, Secrets, Enroll, Hardware/Cloud account) | ≈ 17 clicks, 6 typed fields, plus running a command yourself and pressing "I ran it" | You type the IP, user, port and arch. A manual 4-stage stepper. Targets are a separate sheet. No one watches the host. | `user@host` + a sentence → one credential gate → agent actions → 6 boxes → **Add and set up** → setup diagram, then the Infra agent owns it | Infra | **H** | H | M |
| 4 | **Stalled work** (Shepherd escalations, BOSS Agent home, Factory Retry) | 1 click per escalation, but every one waits for a person | Phase 1 only proposes. "Waiting / Stopped / Not answering" lists for a person to clear. | No new form. Shepherd acts on ledger moves alone, and the lanes show it. Escalations only for boundary items. | Shepherd | **H** | M | M |
| 5 | **Staging deploy** (Infra › + Deploy, DeployWizard, ChangeSet, Stack deploy step) | 3 clicks + approval on another screen + raw JSON packet | A plan, then an approval, for a reversible action. Three duplicate deploy UIs. | Release agent deploys staging alone after green checks. Prod shows as one decision card with a preflight diagram. | Release | **H** | M | M |
| 6 | **Answer a decision** (Approvals, Needs you, decision inbox) | 2 clicks. "Hand to someone" takes a typed role key. Digest channel syntax. | Too many decisions reach people (most are not boundaries). Text-heavy. | Only boundary kinds arrive. Each card shows "what I'd do · why" as a diagram, with one primary and a people picker. | Helm | **H** (by volume) | H | S–M |
| 7 | **Security / CVE watch** (no flow today) | none | Nothing watches dependencies between builds. | Standing agent → fix PR → merge on the list. A Security lane in Govern. | Security | **H** | L | M |
| 8 | **Create or edit a workflow** (Workflows › New › Describe it, step inspector, custom step) | ≈ 4 clicks to draft. 1–2 clicks per field per step. Snake_case start form. Raw prompts. | The draft lands unexplained. You wire inputs by name and pick bindings from a long list. Custom steps get no AI help. | "When a PR merges, deploy staging and smoke test" → agent actions (the steps) + diagram → step chips → **Save and test** | Helm | M | H | M |
| 9 | **Add an agent / skill / MCP** (Agent Studio › New agent, Skills › + New, Add MCP, Import) | 3–5 clicks + Advanced raw prompts + `source:model` id. Skills ask for image, command and risk. Import is raw YAML/JSON. | Expert fields, long checklists, hand-typed tool allow-lists. | "An agent that keeps our docs in sync with the code" → role, skills, MCP, allow-list drafted → **Create and test** (3 probes) → passport | Helm | M | H | M |
| 10 | **Knowledge: add docs and rules** (KG setup stepper, rulebook upload, regulation paste, rules review, lessons) | Setup takes 8+ clicks. Clauses are split by hand. Each rule is approved one by one. | Picking the scan area and budget. Hand segmentation. Lessons wait in a queue. | Drop a PDF, URL or repo → sections and rules as agent actions → **Add and learn**. Evidence-backed lessons promote alone. Regulatory packs stay reviewed. | Knowledge | M | M | M |
| 11 | **Spend limits** (Govern › Limits, Advanced board, Envelopes) | 1 click, but raw $ caps, burst per minute, token caps, tier numbers | No suggested defaults. | FinOps proposes caps from 30 days, with the replay already there → **Apply**. Envelope tiers in words. | FinOps | M | M | S |
| 12 | **Connect GitHub / add a model** (Repo and models) | GitHub ≈ 6–7 clicks over 2 sites. Model ≈ 4. Routing is many dropdowns. | Three ways to connect GitHub. Routing knobs. | One input: a repo URL or "use my Claude subscription" → detected path → **Connect** (checks run live). Stays global. Smart routing default. | Onboarding | M | M | M |
| 13 | **First run** (setup stepper, welcome, Try it) | 4 steps | It works, but it is a stepper before value shows. | Add a project's one input *is* the first run. GitHub and model readiness dots link to the global setup. | Onboarding | M | M | S |
| 14 | **Project deploy values / env vars** (Project › Settings) | Type NAME + value per variable | You must know which variables the app needs. | Onboarding reads the code and lists the needed names. You give values only for secrets (boundary 4). | Onboarding | M | M | S |
| 15 | **CI config** (Infra › Deploys › CI) | Vendor, language, framework, then hand-edited YAML. Nothing is written. | Raw YAML, and a dry run only. | The Release agent proposes CI as a PR (merge is the gate). | Release | M | M | S |
| 16 | **Multi-repo components** (Factory › components editor) | Row editor of names, paths and depends_on | You draw the dependency graph by hand. | Onboarding infers components from all the repos → agent actions (the graph) → confirm. | Onboarding | M | M | M |
| 17 | **Focused (always-on) agents** (Agent Studio directory) | Template, interval in seconds, raw action names | Duplicates the area agents. | Folded into the area agents on wireframe 1. Custom watchers via "Describe the job" (#9). | Helm | M | M | S |
| 18 | **Convene a panel** (Meetings) | Type radio, directive, seats by checkbox, a long task select | You must know the seat lenses. | The directive picks the type and seats → agent actions → **Convene**. | Helm | L–M | M | S |
| 19 | **Invite people / roles / API keys** (Identity) | Invite 1 click. Role editor is a huge matrix. Keys take typed project ids and CIDRs. | Permission semantics, raw ids. | "Invite ana@ like Sarah" → agent actions (the grants) → **Invite** (passkey). "A role that can deploy staging only" → drafted matrix with impact diff. All on the list. | none | L (gated by design) | M | M |
| 20 | **SSO / app sign-in** (Identity › Sign-in, App sign-in) | About 13 fields per client, about 14 policy fields | IdP jargon, redirect URIs typed by hand. | Paste the IdP metadata URL → detected → agent actions → **Connect** + test sign-in. Reuse the Factory one-press sign-in sheet. | none | L | M | M |
| 21 | **Ask BOSS / captain chat** (Command Center hero, captain drawer, BOSS Agent home) | 2–3 clicks. Already strong (work agreement preview). | Several entry points. | Keep. Make every flow above reachable from it ("watch my app" opens #2 prefilled). | Helm | L | M | S |
| 22 | **Command Center starts** (hero next action, templates, + Add to existing app) | 1–2 clicks | Mostly navigation. | The next action points into the new flows. Lanes from wireframe 1 as a strip. | Helm | L | M | S |
| 23 | **Self-fixes** (Self-fixes, send upstream) | ≈ 3 clicks. Exact-bytes preview. | Already strong. | Show the install as a merge card on the list. Upstream send is "talking to the outside". | Self-fix | L | L | S |
| 24 | **Cloud account** (Infra › Cloud › + Account) | ≈ 4 clicks, scopes and secret refs typed | Secret-ref syntax. | Kind pill in Add a server (#3). The credential goes through the gate. | Infra | M | M | S |
| 25 | **Secrets sheet** (Infra) | 3 clicks, kind and backend dropdowns | Picking the kind and backend. | Kind inferred from the name or value shape. Backend default from the install. | none | L | L | S |
| 26 | **Agent processes / improvement programs** (ops) | Typed keys, row tables, raw `qa_eval_…` ids | Raw ids. | Describe → compile already exists. Make it the default. Pick evals from a list. | Helm | L | L | S |
| 27 | **Sandboxes** | Raw shell + break-glass reason | Expert surface by design. | Keep as is (break-glass is correctly manual). | none | L | L | — |

Everything under "UI" that says "unchanged" in the wireframes' parity tables stays reachable: expert grids, PromQL, policy trees, JSON views and YAML import are folded behind "More" or "Wire it myself", never removed.

---

## 4. The three wireframes

| Wireframe | What it shows | Clicks old → new |
|---|---|---|
| [1-where-people-decide.html](1-where-people-decide.html) | Govern › **Where people decide**:<br>• loop diagram (signals → area agents → on the list? → a person)<br>• 24 h stats<br>• rule-in-your-words input with a **Rule change** card and a 30-day replay<br>• the 7 + 1 boundary cards, one open popover (who decides, scope, quiet window, on my behalf, Full auto locked, audit trail)<br>• the 9 monitoring agents with alone-vs-waited bars<br>• 24 h lanes and one decision drawn as a diagram<br>• parity 21/21 | See what needs a person **7 → 1**. "Over $200 → Sarah" **8 → 2**. What agents did alone yesterday **4 → 1** |
| [2-watch-an-app.html](2-watch-an-app.html) | Observability › **Watch an app**:<br>• one input<br>• the SRE agent's **Agent actions** card (how it talks, what matters from your words + the code graph, 5 watches with baselines and 7-day backtests, one open question)<br>• 6 boxes and an alone / waits-for-you strip<br>• **Start watching**<br>• a watch popover with PromQL<br>• landing on the app's health page already on call<br>• the first incident fixed alone on staging vs. one click on prod<br>• parity 18/18 | App watched with 5 alerts and Slack **31 → 3**. Night incident on staging **3+ → 0** |
| [3-add-a-server.html](3-add-a-server.html) | Infra › **Add a server**:<br>• `user@host` + a sentence<br>• the single credential gate (append BOSS's public key, or a key from the vault)<br>• **Agent actions** card<br>• 6 boxes<br>• **Add and set up**<br>• landing on a self-setting-up server diagram<br>• a week later, the Infra agent prunes cache alone and asks only for an outside DNS record<br>• parity 19/19 | Server ready with a staging target **17 → 3**. Typed fields **6 → 1** |

Screenshots (1440×900 full page and fold, 390×844 phone, no sideways scroll) were rendered with Playwright from `file://`.

---

## 5. Cross-cutting findings

- **One boundary, many copies.** Autonomy and budget live in Govern, in Project Settings and in v2. App OIDC lives in Identity and in the Factory sheet. Repos live in Setup and in Project. Deploy has three UIs (`infra/deploy-sheet.tsx`, `deploy-ui.tsx`, `stack-deploy-ui.tsx`). Each should have one home, with links from the others.
- **Raw inputs to retire:** dollar and token caps without suggestions, 0–1 weights, tier numbers, CIDRs, project ids, role keys (`release_manager`), channel syntax (`slack:#…`), redirect URIs, PromQL label matchers, release-packet JSON, CI YAML, pack and skill YAML, `source:model` ids, interval seconds.
- **Agent help exists only in conversation**: Ask BOSS, the captain, Change it, roadmap recommendations, Describe docks. None of it is inside the forms in Infra, Observability, Govern, Identity or Settings. The pattern puts the agent *inside* the form, as the **Agent actions** card.
- **Observe-only agents.** The SRE overwatch, Shepherd phase 1 and the Reviewer all stop at "propose". With the boundary explicit, these can act below the line, with receipts and diagrams. That is the autonomy gain.
- **Reuse, don't rebuild:** the Factory one-press sign-in sheet, the work-agreement preview, the rule backtest, the limits 30-day replay, the role impact diff and the revoke-cascade dry run are already the "show the effect first" half of the pattern.

## 6. Suggested order

1. **Where people decide**, because it makes autonomy safe to expand. The engine half is mostly there (`autonomy.py` backstop). The new parts are named decision kinds with scope and seats, per-agent receipt counts, and the page.
2. **Watch an app**, together with SRE phase 2 (fix reversible problems alone).
3. **Add a server**, together with the Infra host-health watcher.
4. Then Shepherd phase 2 and the Release agent's alone-on-staging deploys (#4, #5), which need no new screens. They light up the lanes on page 1.
5. Then the authoring flows (#8, #9, #10) with the Agent actions card pattern.

Build each one through BOSS agents, per the standing rule. These are wireframes only; no product code was changed.
