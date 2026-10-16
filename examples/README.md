# Examples

Fifteen ready-to-install workflows built from skills BOSS already ships. Each one uses only the
public API — the same calls the console makes when you click through the workflow builder — so
anything a script does, you can also do by hand.

| Example | Function | Input | Output | Stops for approval before |
|---|---|---|---|---|
| [video-workflow](video-workflow/) | content | A video link and a plain-English edit | An edited video | Publishing to YouTube |
| [meeting-to-tickets](meeting-to-tickets/) | any team | A meeting transcript | Tickets in your tracker | Creating the tickets |
| [weekly-newsletter](weekly-newsletter/) | marketing | Sectors you follow | A cited newsletter by email | Sending the email |
| [sales-signal](sales-signal/) | sales | A company name | An assessment and outreach draft for your salesperson | Emailing the salesperson |
| [trend-to-content](trend-to-content/) | content | A trend | A verdict, and a script if the trend fits | Emailing the script for review |
| [pipeline-digest](pipeline-digest/) | sales | A CRM pipeline export | The weekly digest by email | Sending the email |
| [prd-from-interviews](prd-from-interviews/) | product | Customer interview notes | A PRD, or the evidence gaps | Emailing either |
| [competitive-teardown](competitive-teardown/) | product | A competitor and your product | A cited teardown | Emailing it |
| [support-ticket-triage](support-ticket-triage/) | support | A batch of tickets | Category, priority and a draft reply per ticket, as a digest | Emailing your support lead (never a customer) |
| [oncall-handoff](oncall-handoff/) | ops | A period and your notes | The handoff, from what BOSS ran and what is waiting | Emailing the incoming on-call |
| [release-notes](release-notes/) | engineering | A tag or date | Customer and internal notes, written in parallel | Emailing the reviewer |
| [dependency-upgrade-pr](dependency-upgrade-pr/) | engineering | A package name | A plan, a gated pull request, a CI result | Opening the PR; emailing the owner |
| [security-finding-triage](security-finding-triage/) | security | One finding | A verdict with evidence → a ticket or a note | Creating the ticket; emailing |
| [flaky-test-triage](flaky-test-triage/) | engineering | A failing test and its output | Flaky → a ticket; real → an escalation | Creating the ticket; emailing |
| [incident-postmortem](incident-postmortem/) | engineering / ops | Incident notes | A postmortem and its action items | Emailing it; creating the tickets |

These examples ship as optional setup scripts. None is installed or runs on a fresh install.
Running a setup script creates definitions in *your* project, which you can change, rename or
delete. Actual runs need the model and connected services listed for each example, and external
actions wait for the required approval. The scripts assemble skills, roles, and tools that BOSS
ships; your own workflows can arrange them differently.

Every example has the same layout:

```
examples/<name>/
    README.md    what it does and why, what to set up first, and how to build it BY HAND
    setup.py     the same thing as a script — --dry-run first, safe to re-run
```

## First: your API key

Every command below reads a BOSS API key from `BOSS_API_KEY`. Use one of:

- **a personal key**: in the console, **My account › API keys › New key**. It acts as you, with
  your access. It is shown once, so copy it into `BOSS_API_KEY` straight away;
- **the host tools' key**, on the BOSS machine: `BOSS_HOST_API_KEY` in the install's `.env`
  (the `boss` command issues it; `boss identity host-key` makes a new one).

Do not use the old shared `BOSS_API_KEY` line from `.env`: it stops working 30 days after the
install is claimed. Here is how to get a key into your shell, depending on where BOSS runs.

**BOSS on a server, and you are on your laptop.** If you also have a checkout of this repository
on the laptop, start an SSH tunnel in a separate terminal. The installer binds the API to the
VM's loopback address.

```bash
scripts/install/remote.sh --host root@YOUR_HOST --key ~/.ssh/your_key tunnel
```

In the terminal where you will run the examples, print the three export lines:

```bash
scripts/install/remote.sh --host root@YOUR_HOST --key ~/.ssh/your_key env
```

```
export BOSS_API=http://127.0.0.1:8000
export BOSS_API_KEY=…
export BOSS_PROJECT=boss-dogfood
```

Paste those lines into your shell while the tunnel is open. The key is the server's host tools'
key. `env` prints a secret, so keep its output out of tickets and shared logs.

**On the server itself** (the simplest path after a one-line install):

```bash
cd ~/boss
grep -q '^BOSS_HOST_API_KEY=' .env || boss identity host-key
source ./boss-community.env.sh
export BOSS_API="$BOSS_API_URL" BOSS_PROJECT="$BOSS_PROJECT_ID"
export BOSS_API_KEY="$(sed -n 's/^BOSS_HOST_API_KEY=//p' .env)"
```

Nothing here prints the key. If you installed to another directory, change to that directory
first.

> A key is a secret. Keep it in the environment, not in a `--key` argument or a ticket. If a
> personal key leaks, revoke it in **My account › API keys**. If the host tools' key leaks, run
> `boss identity host-key`: the old one stops at once.

Every script prints these instructions if you run it without a key.

---

## Install all of them at once

```bash
python3 examples/setup_all.py --dry-run
python3 examples/setup_all.py
```

| Option | What it does |
|---|---|
| `--dry-run` | Report what would happen and change nothing. Run this first |
| `--only a,b` | Install a subset — `--only video-workflow,sales-signal` |
| `--schedule TZ` | Also schedule the newsletter for Monday 08:00, e.g. `--schedule Europe/London` |
| `--newsletter-sectors` / `--newsletter-to` | Required with `--schedule`: topics and recipient stored on each recurring run |
| `--no-agents` | Leave steps unassigned, so each runs as the workflow itself |
| `--project` / `--actor` | Which project to install into, and who is doing it |

`setup_all.py` runs the individual scripts rather than reimplementing them, so there is only one
copy of each example to maintain.

**One failing example does not stop the others.** The examples have different prerequisites —
some need a work tracker, most need email, the engineering ones need a bound repository — so
you end up with every example you can run, plus a list of what was skipped and why. Re-running
is safe: examples that are already installed are reused, not duplicated.

**Each example says what will not work yet.** After it installs, every script asks BOSS whether
the workflow can run on this project and prints anything missing — a mail server, a connected
tracker, a Google account — with the step that needs it and how to fix it. `setup_all.py`
collects those at the end. Start in the console shows the same list, and refuses to start until
each item is fixed or you choose to start anyway. The video example also adds the two agents it
names (`video_editor`, `media_publisher`, from the media pack) when your project does not have
them.

To schedule the newsletter while installing, provide its run inputs up front:

```bash
python3 examples/setup_all.py --schedule Europe/London \
  --newsletter-sectors "energy, healthcare" --newsletter-to reader@example.com
```

The scheduled run prepares a newsletter every Monday; email delivery still waits for your
approval. The on-call handoff has its own `--schedule` on `examples/oncall-handoff/setup.py`. In the console, **Workflows → Library → ⋯ → Schedule…** asks for the time zone and for every input the
workflow declares, and required inputs are saved with the schedule. **Edit** on the
schedule line replaces those values, and the API refuses a save that drops a required one. The
API equivalent is `POST /v1/delivery-workflows/schedules` with the existing `schedule_id` and a
complete `input` object. Timing stays as it was unless the time zone changes.

---

## The examples

The 34 YAML files in `packages/specpack/agents/` are **definitions** for common roles; installing
them does not start an agent. The fifteen workflows below show how to give agents specific work,
skills, approvals, and schedules through the public API. Use `--dry-run` to inspect the changes
before installing one.

The first five are the originals. The ten after them are the **use-case library**: one per
function, each built to show one shape of workflow — a branch, two steps in parallel, a step
repeated per item, a gate before a write, an error path — on a job somebody actually has. Every
one of them ends at a person: **nothing is sent, filed, published or deployed until a person
approves it.**

### [video-workflow](video-workflow/) — edit a video and publish it

Paste a link, describe the edit in plain English, and get an edited video that waits for your
approval before it reaches your channel.

- **Steps:** **an agent fetches and edits** → publish (stops for you)
- **Skills the editing agent holds:** `media.fetch_link`, `media.fetch_image`,
  `media.read_document`, `video.trim`, `video.cut`, `video.reframe`, `video.caption`,
  `video.overlay_image`, `video.watermark`, `video.bookend`
- **Needs:** a connected model and the `video_editor` and `media_publisher` agents (setup adds
  them from the media pack). A Google account only to **publish** to YouTube, or when the source
  is a Google Drive link — editing a direct link and reviewing the result needs no Google at all

**What it demonstrates:** a step that binds no skill at all. The editing step carries your
sentence, and the agent composes its own skills to satisfy it. "Trim it" and "trim it, make it
portrait and put a slide on the end" run through the same step, and teaching the workflow
something new means giving the agent another skill rather than changing the workflow. It also
shows two agents with different authority, and an approval that cannot be switched off.

### [meeting-to-tickets](meeting-to-tickets/) — a transcript becomes tracked work

Paste a meeting transcript. BOSS writes down what was decided and what somebody has to do, you
review that as plain sentences, and the tickets are created in your tracker.

- **Steps:** read → create (stops for you)
- **Skills:** `meeting.extract_actions`, `tracker.issues.create`
- **Needs:** a connected model, and a work tracker — Plane, Jira, Linear, GitHub Issues or Azure DevOps

**What it demonstrates:** a tier-0 reading step separated from a tier-2 writing step, so you
approve a list you can read rather than a step you have to trust.

### [weekly-newsletter](weekly-newsletter/) — research, written up, in your inbox

Name the sectors you care about. BOSS researches them into a cited dossier, writes a newsletter,
and emails it once you approve the send step. `--schedule` starts the research every Monday.

- **Steps:** gather → write → send (stops for you)
- **Skills:** the `research` capability, `model.generate_text`, `email.send_real_guarded`
- **Needs:** a connected model, and SMTP configured for the last step

**What it demonstrates:** a built-in capability and your own steps in one workflow, a step that
takes an earlier step's output as material, and the scheduler, with the time zone stored on the
schedule.

### [sales-signal](sales-signal/) — a company name becomes a researched approach

Give it a company. BOSS researches it, scores the opportunity, drafts an opening email, and puts
both in your salesperson's inbox. Nothing is sent to the prospect.

- **Steps:** research → assess → draft → notify (stops for you)
- **Needs:** a connected model, and email configured

**What it demonstrates:** an assessment handed to a second model step, which prepares both the
assessment and an editable outreach draft for the salesperson. The workflow ends with a person,
not with an email to the prospect.

### [trend-to-content](trend-to-content/) — a trend becomes a script, or an honest no

Give it a trend. BOSS researches it, says plainly whether it is yours to talk about, and writes a
script only if it is.

- **Steps:** research → assess → script → review (stops for you)
- **Needs:** a connected model, and email configured

**What it demonstrates:** a step that reads an earlier step's *judgement* rather than its raw
material, so a trend judged "not ours" produces one line instead of a polished script for a video
nobody should make. Its output feeds straight into video-workflow.

### [pipeline-digest](pipeline-digest/) — a CRM export becomes the weekly digest

Paste the pipeline export. BOSS writes what moved, what stalled, what closes and what is at
risk, in numbers from the export, and emails it once you approve.

- **Steps:** digest → send (stops for you)
- **Skills:** `model.generate_text`, `email.send_real_guarded`
- **Needs:** a connected model, and email configured

**What it demonstrates:** the simplest shape in the library — two steps, one of them a stop.
Build this one first by hand. Pasted rather than pulled, because BOSS has no CRM connector,
and not scheduled for the same reason.

### [prd-from-interviews](prd-from-interviews/) — interview notes become a PRD, or an honest no

Paste the interview notes. BOSS finds the themes, says whether there is enough evidence, and
drafts a PRD only if there is.

- **Steps:** themes → **branch** → PRD → send (stops for you), or → send the gaps (stops for you)
- **Skills:** `model.generate_text`, `docs.draft`, `email.send_real_guarded`
- **Needs:** a connected model, and email configured

**What it demonstrates:** a `condition` on the text a step wrote (`contains`), so the workflow
can say no, and a document written from an extracted judgement rather than raw notes.

### [competitive-teardown](competitive-teardown/) — two research runs become a teardown

Name a competitor and your own product. Both are researched **at the same time**, compared from
the evidence, written up and emailed once you approve.

- **Steps:** **parallel** (research them, research us) → **join** → compare → write → send (stops for you)
- **Skills:** the `research` capability twice, `model.generate_text`, `docs.draft`, `email.send_real_guarded`
- **Needs:** a connected model, web search for the research, and email configured

**What it demonstrates:** `parallel` and `join` around two built-in capability steps, and a step
that reads two earlier outputs (one as `prompt`, one as `context`).

### [support-ticket-triage](support-ticket-triage/) — a batch of tickets, triaged one by one

Paste a batch of tickets. An agent splits them; **one step runs once per ticket** — category,
priority, draft reply — and the digest goes to your support lead once you approve. Nothing goes
to a customer.

- **Steps:** split → **for each** ticket: reply → digest → send (stops for you)
- **Skills:** agent work, `model.generate_text`, `email.send_real_guarded`
- **Needs:** a connected model, and email configured

**What it demonstrates:** `for_each` over a list an agent declared, with the item and its index
wired into the body step, and `results` collected in order for the step after.

### [oncall-handoff](oncall-handoff/) — what BOSS ran and what is waiting, as a handoff

The SRE agent reads BOSS's own records for the period — failed runs, decisions waiting,
deployments and health — adds your notes, and writes the handoff. `--schedule` prepares it every
Monday.

- **Steps:** gather → send (stops for you)
- **Skills:** `boss.fundamentals` (BOSS's read tools), `email.send_real_guarded`
- **Needs:** a connected model, and email configured

**What it demonstrates:** an agent working from BOSS's own data through read-only tools, and a
schedule on a workflow whose last step still waits for a person.

### [release-notes](release-notes/) — merged pull requests become release notes

Name a tag. An agent reads what was merged since, customer and internal notes are written **in
parallel**, combined, and emailed once you approve.

- **Steps:** collect (agent, repository tools) → **parallel** (customer notes, internal notes) → **join** → combine → send (stops for you)
- **Skills:** `repo.read`, `docs.draft`, `model.generate_text`, `email.send_real_guarded`
- **Needs:** a connected model, a bound repository, and email configured

**What it demonstrates:** an agent step with the engine's repository read tools declaring values
for later steps, and two custom steps running in parallel from the same material.

### [dependency-upgrade-pr](dependency-upgrade-pr/) — an upgrade is assessed, gated, built and checked

Name a package. An agent assesses the upgrade; a **person opens the gate**; the governed build
opens the pull request; the run waits for CI; the owner is emailed. Nothing is merged.

- **Steps:** assess → **branch** (skip → tell; upgrade → **gate** → build → wait for CI → tell), with **on_error** → tell
- **Skills:** `repo.read`, `ci.status`, the `build` and `wait_for_ci` capabilities, `email.send_real_guarded`
- **Needs:** a connected model, a bound repository with pull-request access, and email configured

**What it demonstrates:** a `decision` gate in front of a step that writes outside BOSS, an
error path that tells somebody instead of stopping silently, and a condition on an agent's
declared verdict.

### [security-finding-triage](security-finding-triage/) — a finding is confirmed or dismissed

Paste one finding. The security reviewer reads the code it points at; a true positive becomes
a ticket, anything else a note to the owner.

- **Steps:** judge (agent) → **branch** → file the ticket (stops for you) or tell the owner (stops for you)
- **Skills:** `repo.read`, `tracker.issues.create`, `email.send_real_guarded`
- **Needs:** a connected model, a bound repository, a work tracker, and email configured

**What it demonstrates:** two different effects on two different systems behind one branch, each
approved on its own.

### [flaky-test-triage](flaky-test-triage/) — a failing test is classified and routed

Paste a failing test. An agent reads the test, the code, the history and CI, and says flaky or
real. Flaky becomes a ticket; real is escalated.

- **Steps:** diagnose (agent) → **branch** → file the ticket (stops for you) or escalate (stops for you)
- **Skills:** `repo.read`, `ci.status`, `tracker.issues.create`, `email.send_real_guarded`
- **Needs:** a connected model, a bound repository, a work tracker, and email configured

**What it demonstrates:** the same branch shape as the security example, with `ci.status` as
evidence, and a README that shows how to start it from CI with a webhook trigger.

### [incident-postmortem](incident-postmortem/) — notes become a postmortem and action items

Paste the incident notes. The timeline and the action items are read **in parallel**, the
postmortem is written from both, emailed for review, and the action items are filed as tickets.

- **Steps:** **parallel** (timeline, action items) → **join** → write → send (stops for you) → file (stops for you)
- **Skills:** `model.generate_text`, `meeting.extract_actions`, `email.send_real_guarded`, `tracker.issues.create`
- **Needs:** a connected model, email configured, and a work tracker

**What it demonstrates:** `parallel` and `join` over custom steps, and two effects approved
separately — rejecting the tickets does not unsend the email.

---

## Not in the library, and why

Templates that were planned and not built, because the tool they need does not ship with an
executor yet. Each is a small BOSS-side change plus whatever the other system requires.

| Template | Blocked on |
|---|---|
| Deploy over SSH after approval | an executor for the `ssh_host.*` skills (declared, not runnable). BOSS's own `deploy` capability needs a BOSS-managed stack target |
| Access review | a directory or identity-provider connector (Okta, Google Workspace, Entra) |
| Churn-risk digest, scheduled pipeline digest | a CRM or billing connector |
| Social posts | see below |

---

## Current limits

Worth knowing before you plan around these examples.

### Publishing to social platforms

BOSS can publish to **YouTube**. It has **no** Facebook, Instagram, LinkedIn, X or TikTok skills,
and the work to add one is mostly on the platform side:

| Platform | What it takes |
|---|---|
| **Instagram / Facebook** | Meta Graph API, a Business account, and an app through App Review. Posting is a two-step publish — create a container, then publish it — and video has its own limits |
| **LinkedIn** | Community Management API, and partner access to post as an organisation |
| **X / Twitter** | Paid, tiered API access; media upload is a separate endpoint |
| **TikTok** | Content Posting API, app review, and an audited scope for direct posting |

The BOSS side of any of them is small: a provider entry, an OAuth flow, one `*.post` skill, and a
role permitted to call it. The platform side — an app, a review, a business account — determines
how long it takes. The editing lane in [video-workflow](video-workflow/) already produces output
for all of them; only the last step differs.

### Starting from an external event

Every example here starts **on demand**, and `weekly-newsletter` and `oncall-handoff` can also
start **on a schedule**. None starts because something happened elsewhere — a form was
submitted, a meeting ended, a CI job failed.

BOSS supports that pattern: a [webhook trigger](../docs/community/WORKFLOW_WEBHOOKS.md) or an
event subscription can start a workflow, and the delivery's data becomes the run's input. What
BOSS cannot do is *notice* the event itself; whatever watches your site, calendar or CI has to
call BOSS. A trigger's URL and secret are created per install, in the designer, which is why
the examples start by hand. Wiring a real trigger later changes who presses Start and nothing
else — `flaky-test-triage` and `security-finding-triage` describe it.

### Repeating a step per item

A workflow's step list is fixed when you publish it, and a [`for_each`
step](../docs/community/WORKFLOW_FOR_EACH.md) runs one step once per item of a list —
`support-ticket-triage` is the example. Two things to know before reaching for it:

- **Each item is its own step run, but the approval is the step's.** A for-each that ends in a
  write would be one approval for every item at once. The library keeps the effect *after* the
  loop — a digest of fifty drafts, approved once — or uses one run per item where each needs
  its own decision, as `security-finding-triage` does.
- **A skill that takes a list is often simpler.** `video.apply_plan` takes a whole edit plan and
  `tracker.issues.create` takes every action from a meeting, so a loop is not needed to create
  six tickets.
