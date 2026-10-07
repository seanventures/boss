# Examples

Five ready-to-install workflows built from skills BOSS already ships. Each one uses only the
public API — the same calls the console makes when you click through the workflow builder — so
anything a script does, you can also do by hand.

| Example | Input | Output | Stops for approval before |
|---|---|---|---|
| [video-workflow](video-workflow/) | A video link and a plain-English edit | An edited video | Publishing to YouTube |
| [meeting-to-tickets](meeting-to-tickets/) | A meeting transcript | Tickets in your tracker | Creating the tickets |
| [weekly-newsletter](weekly-newsletter/) | Sectors you follow | A cited newsletter by email | Sending the email |
| [sales-signal](sales-signal/) | A company name | An assessment and outreach draft for your salesperson | Emailing the salesperson |
| [trend-to-content](trend-to-content/) | A trend | A verdict, and a script if the trend fits | Emailing the script for review |

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
one needs a work tracker, three need email — so you end up with every example you can run, plus
a list of what was skipped and why. Re-running is safe: examples that are already installed are
reused, not duplicated.

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
approval. In the console, **Workflows → Library → ⋯ → Schedule…** asks for the time zone and for every input the
workflow declares, and required inputs are saved with the schedule. **Edit** on the
schedule line replaces those values, and the API refuses a save that drops a required one. The
API equivalent is `POST /v1/delivery-workflows/schedules` with the existing `schedule_id` and a
complete `input` object. Timing stays as it was unless the time zone changes.

---

## The examples

The 34 YAML files in `packages/specpack/agents/` are **definitions** for common roles; installing
them does not start an agent. The five workflows below show how to give agents specific work,
skills, approvals, and schedules through the public API. Use `--dry-run` to inspect the changes
before installing one.

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

Every example here starts **on demand**, and `weekly-newsletter` can also start **on a
schedule**. None starts because something happened elsewhere — a form was submitted, a meeting
ended, a deploy finished.

BOSS supports that pattern: an event subscription can start a workflow, and the event's data
becomes the run's input. What BOSS cannot do is *notice* the event itself; whatever watches your
site, calendar or CI has to call BOSS. That is why the examples start by hand. Wiring a real
trigger later changes the first step and nothing else.

### Repeating a step per item

A workflow's step list is fixed when you publish it. A step cannot repeat once per item in a
list, so "make six clips from this recording" is six runs rather than one run making six.

That is usually the better shape anyway: six runs means six approvals, so you can accept four and
reject two. Where it matters, a skill can take a list instead — `video.apply_plan` takes a whole
edit plan and `tracker.issues.create` takes every action from a meeting, both for this reason.
