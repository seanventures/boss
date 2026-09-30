# Video editing and publishing

**Paste a link to a video, say what you want done to it in plain English, and get an edited
video waiting for your approval before it reaches your channel.**

---

## The use case

You record long videos. Most of what you publish is not the recording — it is a cut of it: the
dead air removed, a vertical crop for phones, a caption over the opening, your logo in the
corner. The editing itself is mechanical. The judgement is in deciding *what* to cut.

So this workflow splits those apart. You supply a link and an instruction like:

> Keep 0:28 to 1:48, make it portrait for phones, and put the ending slide on the end.

The editing agent reads that sentence, decides which of its skills to use and in what order,
does the work, and shows you the result. Nothing reaches YouTube until you say so.

### What happens, step by step

| # | Step | Agent | What it does |
|---|------|-------|--------------|
| 1 | **Let an agent work it out** | `video_editor` | **Binds no skill.** Fetches the link in your instruction and edits with the skills it holds |
| 2 | **Publish to YouTube** | `media_publisher` | Uploads as **private**. **Stops and waits for you** |

### The one thing worth understanding about the editing step

**It binds nothing, and that is the point.**

The middle used to be two steps — "plan the edit" and "apply the plan" — with a fixed list of
operations between them. That shape does not survive contact with real use. The first person to
ask for an ending slide needed the operation list to grow a word for it; the next asks for a
fade, the one after that for a lower third. A framework whose answer to every new request is a
release is not a framework.

So the editing step carries your **instruction**, and `video_editor`'s **skills** are the vocabulary:

```
media.fetch_link  media.fetch_image  video.trim  video.cut  video.reframe
video.caption     video.overlay_image  video.watermark  video.bookend
```

The agent picks from those and orders them itself. Which means:

**To make this workflow do something new, give the agent another skill.** Agents → `video_editor`
→ *Give this agent a skill*. The workflow does not change. No step is edited. Nothing is
released.

**It cannot use a skill nobody gave it.** The tools it is offered are exactly the ones it holds
in `role_skills`, and the runtime re-checks that on every call — so an instruction asking for
something it was not granted comes back as "I cannot do that", named, rather than approximated
with a tool meant for something else.

**It has no shell, no database and no file system.** An ordinary BOSS agent run gets all three,
because that is the job in the software factory. This kind gets none of them: the skills you gave
it are the whole of its reach, and that claim has to be true to be worth making.

**Publishing still cannot be made automatic.** `publish_youtube` is risk tier 3, and BOSS
requires a person at or above that floor whatever a workflow asks for. `video_editor` is
*forbidden* to publish, and assigning it to that step is refused when you save. That separation
is enforced, not conventional.

### The timecode trap

Each operation applies to the result of the one before it. Cut 1:00–2:00 and then caption at
5:00, and that caption lands at 6:00 of your original recording.

Say which recording your timecodes are from if there is any doubt — "0:28 to 1:48 **of the
original**". The agent orders its own operations and is told about this trap, but your sentence
is the only thing that says what you meant.

---

## Before you run it

### 1. A model must be connected

The editing step is an agent working from your sentence, so it needs a model. Settings → connect one, if
`connected_engines` is empty.

### 2. Connect YouTube for publishing

The editing agent can fetch a link you provide. Publishing needs a connected YouTube channel.

> **Channels → Connect** in the console.

Without a channel, publishing refuses. Private Google Drive source links also need an account
that can read them; a public HTTPS source can be fetched without Drive sign-in.

**Several channels?** Connect each one; they get their own keys (`youtube_default`,
`youtube_2`, …), each with its own token. To pin a step to a particular channel, open that
step's **inputs** in the builder, set `connection_key` to **a fixed value**, and type the key.

### 3. ffmpeg

Already in the BOSS images. On a hand-built environment, `ffmpeg` needs the
`libavfilter-extra` package and a font (`fonts-dejavu-core`) — without them captions fail,
because `drawtext` is not in the base filter set.

---

## Running the setup script

```bash
python3 examples/video-workflow/setup.py \
    --api  http://127.0.0.1:8000 \
    --project YOUR_PROJECT \
    --actor   ceo
```

| Parameter | What it is | Default |
|---|---|---|
| `--api` | Your BOSS API. On a remote VM, open the SSH tunnel described in [Examples](../README.md) first | `http://127.0.0.1:8000` |
| `BOSS_API_KEY` | API key in your environment; keep it out of command arguments | required |
| `--project` | Which project to build it in | `boss-dogfood` |
| `--actor` | Who is doing this. Must be a principal your API key may act as — the API names the right one when it refuses | `ceo` |
| `--no-agents` | Leave steps unassigned, so each runs as the workflow itself | off |
| `--dry-run` | Say what would happen and change nothing | off |

Each also reads from the environment: `BOSS_API`, `BOSS_API_KEY`, `BOSS_PROJECT`, `BOSS_ACTOR`.

**Start with `--dry-run`.** It reports what it would enable, define and create without touching
anything.

**Re-running is safe.** Steps and the workflow are matched by name and reused.

---

## Doing it by hand instead

The script only calls the public API. Every step below is the same thing in the console, and
doing it once is the fastest way to understand what you have.

### 1. Enable the skills — **do this first**

**Skills → find each → Enable:**

- `media.fetch_link` — a link becomes a video
- `media.fetch_image` — a link becomes a picture: an ending slide, a title card, a logo
- `video.trim`, `video.cut`, `video.reframe`, `video.caption`, `video.overlay_image`,
  `video.watermark`, `video.bookend` — what the agent will compose from
- `publish_youtube`

A skill BOSS ships is *available*, not *installed*. Until it is enabled it lives in the
catalogue and not in your project. This is the step people miss.

### 2. Give the agents their skills

**This is the step that actually matters, and it is the one that was missing for a long time.**

Enabling a skill puts it in your project. It does **not** give it to anybody. The runtime checks
`role_skills` before letting an agent call anything, and a role spec's "allowed skills" list
reaches the agent's *prompt* and nowhere else — so an agent can be told what it may use and hold
nothing, and every step naming it is refused at the first call.

**Agents → `video_editor` → Give this agent a skill**, once per editing skill above.

**Agents → `media_publisher` → Give this agent a skill** → `publish_youtube`.

The agent page shows what it holds. If that list is empty, the workflow cannot work, and the page
says so rather than looking fine.

### 3. Define the publishing step that binds a skill

**Delivery → Build your own → Your steps → Define a step.** For each: a name, choose **Skill**,
pick the skill, and list its inputs.

| Name | Skill | Inputs |
|---|---|---|
| Publish to YouTube | `publish_youtube` | `media_artifact_id`, `title`, `description` |

Publish will show **always needs a person**. That comes from its risk tier; you cannot turn it
off.

There is no step to define for the editing. That is the next section.

### 4. Create the workflow and put the agent step first

**Create a new workflow** → name it → then, from the palette:

1. **Let an agent work it out** — the first entry in the palette. It binds nothing.
2. **Publish to YouTube** (your step)

On the agent step's row, set the instruction source to **ask me when the run starts**. The other
option, *always do this*, pins a sentence onto the step for a workflow that does the same job
every time.

### 5. Wire the inputs

This is the part that makes it a workflow rather than two separate steps. Open each step's
**inputs**:

| Step | Input | Source |
|---|---|---|
| Let an agent work it out | `instructions` | **asked when the run starts** → `instructions` |
| Let an agent work it out | `video_url` | optional run input → `video_url` |
| Let an agent work it out | `asset_url` | optional run input → `asset_url` |
| Publish to YouTube | `media_artifact_id` | **from an earlier step** → Let an agent work it out → `media_artifact_id` |
| Publish to YouTube | `title` | the title you supplied, or **from an earlier step** → Let an agent work it out → `title` |
| Publish to YouTube | `description` | the description you supplied, or **from an earlier step** → Let an agent work it out → `description` |

Everything the agent step is given other than `instructions` is passed to the agent as **facts it
must use exactly** — an artifact id it must not invent. The agent step's own output carries
whatever its skills returned, last one winning, which is why **Publish** can read
`media_artifact_id` from it.

### 6. Assign the agents

Each step row has an agent dropdown. Set the editing step to `video_editor` and **Publish** to
`media_publisher`.

On the agent step, leaving it blank is a real choice with a different meaning: **whichever agent
fits** — BOSS picks, from what each agent is for and what it holds, and records why in the run.

Try assigning `video_editor` to Publish — it is refused when you save, naming what that skill
does accept. That is the separation being enforced rather than described.

### 7. Save

**Save as a new version.** If anything is wrong the error names the step and why.

---

## Running it

**Delivery → Workflows → "Video edit and publish" → Start one.**

| Field | What to put in it |
|---|---|
| `video_url` | optional link to your recording; you can put it in the instruction instead |
| `instructions` | *"Keep 0:28 to 1:48, make it portrait for phones, and put the ending slide on the end."* |
| `asset_url` | optional link to an ending slide, logo, or badge |
| `title` | optional; overrides the title the editing agent derives from your brief |
| `description` | optional; overrides the description the editing agent derives |

Watch the run. The editing step proceeds, then **Publish** stops and waits. Open the
agent step to see **which skills it chose, in what order, and why** — that record is the answer
to "what did it actually do", and it is the thing to read before approving.

---

## When something goes wrong

| What you see | What it means |
|---|---|
| *"skill 'x' is not registered in this project"* | Step 1 above. Enable it in Skills |
| *"video_editor holds no skills, so it cannot do anything"* | Step 2 above. Enabling is not granting — give the agent its skills on the Agents page |
| The agent says it cannot do part of what you asked | It was not given a skill for that. Give it one; the workflow does not change |
| *"gives '…' to video_editor, which is not allowed to run publish_youtube"* | The separation working. Assign `media_publisher` |
| *"that link returns a web page, not a video"* | A share link needing sign-in. Use one the connected account can read |
| *"this ffmpeg has no drawtext filter"* | Captions need `libavfilter-extra` and a font. The BOSS images have both |
| *"an edit plan must be a non-empty list"* | The instruction asked for no changes. Say what you want done |
| Publish fails with a connection error | No channel connected. Channels → Connect |

---

## What ships with BOSS, and what does not

**BOSS ships the capability**: the video skills, `media.fetch_link`, `publish_youtube`, the
`video_editor` and `media_publisher` roles, and ffmpeg in the images. All opt-in — nothing is
forced on a project.

**BOSS does not ship this workflow.** It is an example. The steps and the workflow are
data in *your* project, created through the public API. Change it, rename it, delete it — none
of it is part of the product, and a fresh install has the capability and none of this.
