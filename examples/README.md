# Examples

Workflows built from skills BOSS already ships, using only the public API — the same calls the
console makes when you click through the builder.

These examples ship as optional setup scripts. None is installed or runs on a
fresh install. Running a setup script creates definitions in *your* project,
which you can change, rename or delete. Actual runs still need the model and
connected services listed for each example, and external actions wait for the
required approval. The scripts assemble skills, roles, and tools that BOSS
ships; yours can use a different arrangement.

Every example has the same shape:

```
examples/<name>/
    README.md    what it does and why, what to set up first, and how to build it BY HAND
    setup.py     the same thing as a script — --dry-run first, safe to re-run
```

## First: your API key

Every command below needs `BOSS_API_KEY`. It is generated when BOSS is installed and written to
the install's `.env` — it is not shown again, so this is how to get it back.

**BOSS on a server, and you are on your laptop** — if you also have a checkout
of this repository on the laptop, start an SSH tunnel in a separate terminal.
The installer binds the API to the VM's loopback address.

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

Paste those lines into your shell while the tunnel is open. `env` prints a
secret, so keep its output out of tickets and shared logs.

**On the server itself (the simplest path after a one-line install):**

```bash
cd ~/boss
source ./boss-community.env.sh
export BOSS_API="$BOSS_API_URL" BOSS_PROJECT="$BOSS_PROJECT_ID"
```

The generated shell file is private and supplies the API key without printing
it. If you installed to another directory, change to that directory first.

**BOSS on this machine from a checkout without the installer shell file:**

```bash
export BOSS_API_KEY="$(sed -n 's/^BOSS_API_KEY=//p' .env)"
```

> The key is a secret and grants full API access. Keep it in the environment,
> not a `--key` argument or a ticket. If it leaks, change `BOSS_API_KEY` in the
> install's `.env` and restart the API.

Every script also prints this if you run it without a key.

---

## Install all of them at once

```bash
python3 examples/setup_all.py --dry-run
python3 examples/setup_all.py
```

| Option | What it does |
|---|---|
| `--dry-run` | Say what would happen and change nothing. Worth doing first |
| `--only a,b` | A subset — `--only video-workflow,sales-signal` |
| `--schedule TZ` | Also schedule the newsletter for Monday 08:00, e.g. `--schedule Europe/London` |
| `--newsletter-sectors` / `--newsletter-to` | Required with `--schedule`: topics and recipient stored on each recurring run |
| `--no-agents` | Leave steps unassigned, so each runs as the workflow itself |
| `--project` / `--actor` | Which project, and who is doing it |

It runs the individual scripts rather than reimplementing them, so there is no second copy to
drift from the first.

**One failing example does not stop the others.** They have different prerequisites — one needs
a work tracker, three need email — so you end up with the ones you can actually run, plus a
list of what was skipped and why. Everything is safe to re-run: already-installed examples are
reused, not duplicated.

To schedule the newsletter while installing the examples, provide its run inputs up front:

```bash
python3 examples/setup_all.py --schedule Europe/London \
  --newsletter-sectors "energy, healthcare" --newsletter-to reader@example.com
```

The scheduled run prepares a newsletter on Monday. Email delivery still waits for your approval.
The schedule inputs cannot be edited after creation; remove the schedule and recreate it if
the topics or recipient change.
The console's generic **Schedule** button currently asks only for a time zone. For workflows
with required inputs, use the example script or the API until that button can collect and store
the run inputs.

---

## Available

The 34 YAML files in `packages/specpack/agents/` are **definitions** for common
roles. They do not start an agent on install. The five workflows below show
how to give agents specific work, skills, approvals, and schedules through the
public API. Use `--dry-run` to inspect the changes before installing one.

### [video-workflow](video-workflow/) — edit a video and publish it

Paste a link, say what you want done in plain English, get an edited video waiting for your
approval before it reaches your channel.

**Steps:** **let an agent fetch and edit** → publish (stops for you)
**Skills the editing agent holds:** `media.fetch_link`, `media.fetch_image`, `video.trim`,
`video.cut`, `video.reframe`, `video.caption`, `video.overlay_image`, `video.watermark`,
`video.bookend`
**Needs:** a connected model, and a Google account for the first and last step

Shows: **a step that binds no skill at all.** The middle step carries your sentence and the
agent composes its own skills to satisfy it — so "trim it" and "trim it, make it portrait and put
a slide on the end" are the same step, and making it do something new means giving the agent
another skill rather than changing the workflow. Also: two agents with different authority, and
an approval that cannot be switched off.

### [meeting-to-tickets](meeting-to-tickets/) — a transcript becomes tracked work

Paste a meeting transcript. BOSS writes down what was decided and what somebody has to do, you
read that as sentences, and the tickets are created in your tracker.

**Steps:** read → create (stops for you)
**Skills:** `meeting.extract_actions`, `tracker.issues.create`
**Needs:** a connected model, and a work tracker — Plane, Jira, Linear, GitHub Issues or Azure DevOps

Shows: a tier-0 reading step separated from a tier-2 writing one, so the approval is over a
list you can read rather than a step you have to trust.

### [weekly-newsletter](weekly-newsletter/) — research, written up, in your inbox

Name the sectors you care about. BOSS researches them into a cited dossier, writes a
newsletter, and emails it after you have approved the send step. `--schedule` starts the
research every Monday.

**Steps:** gather → write → send (stops for you)
**Skills:** the `research` capability, `model.generate_text`, `email.send_real_guarded`
**Needs:** a connected model, and SMTP configured for the last step

Shows: a built-in capability and your own steps in one workflow; a step taking an earlier
step's output as material; and the scheduler, with the time zone stored where it belongs.

### [sales-signal](sales-signal/) — a company name becomes a researched approach

Give it a company. BOSS researches them, scores the opportunity, drafts an opening email, and
puts both in your salesperson's inbox. Nothing goes near the prospect.

**Steps:** research → assess → draft → notify (stops for you)
**Needs:** a connected model, and email configured

Shows: an assessment handed to a second model step that prepares both the assessment and an
editable outreach draft for the salesperson. It ends at a human rather than a prospect email.

### [trend-to-content](trend-to-content/) — a trend becomes a script, or an honest no

Give it a trend. BOSS researches it, says plainly whether it is yours to talk about, and writes
a script only if it is.

**Steps:** research → assess → script → review (stops for you)
**Needs:** a connected model, and email configured

Shows: a step reading an earlier step's JUDGEMENT rather than its raw material, so a trend
judged "not ours" produces one line instead of a polished script for a video nobody should
make. Chains into video-workflow.

---

## What these examples cannot do yet

Worth knowing before you plan around them.

### Publishing to social platforms

BOSS can publish to **YouTube**. It has **no** Facebook, Instagram, LinkedIn, X or TikTok
skills, and adding one is mostly not a coding problem:

| Platform | What it takes |
|---|---|
| **Instagram / Facebook** | Meta Graph API, a Business account, and an app through App Review. Posting is a two-step publish — create a container, then publish it — and video has its own limits |
| **LinkedIn** | Community Management API, and partner access to post as an organisation |
| **X / Twitter** | Paid, tiered API access; media upload is a separate endpoint |
| **TikTok** | Content Posting API, app review, and an audited scope for direct posting |

The BOSS side of any of them is small: a provider entry, an OAuth flow, one `*.post` skill and
a role permitted to call it. The platform side — an app, a review, a business account — is what
decides how long it takes. The editing lane in
[video-workflow](video-workflow/) already feeds all of them; only the last step differs.

### Starting on something that happened

Every example here starts **on demand**, and `weekly-newsletter` can also start **on a
schedule**. None starts because something happened elsewhere — a form was submitted, a meeting
ended, a deploy finished.

BOSS can do that: an event subscription can start a workflow, and the event's data becomes the
run's input. What it cannot do is *notice* the thing happening — that needs whatever watches
your site, your calendar or your CI to call BOSS. The examples are written to start by hand for
that reason, and wiring a real trigger later changes the first step and nothing else.

### Repeating a step per item

A workflow's step list is fixed when you publish it. A step cannot be repeated once per item in
a list, so "make six clips from this recording" is six runs rather than one run making six.

That is usually the better shape anyway — six runs means six approvals, so you can accept four
and reject two. Where it genuinely hurts, a skill can take a list instead: `video.apply_plan`
takes a whole edit plan and `tracker.issues.create` takes all the actions from a meeting, both
for this reason.
