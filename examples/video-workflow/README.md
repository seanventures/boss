# Video editing and publishing

**Paste a link to a video, say what you want done to it in plain English, and get an edited
video waiting for your approval before it reaches your channel.**

---

## The use case

You record long videos. Most of what you publish is not the recording — it is a cut of it: the
dead air removed, a vertical crop for phones, a caption over the opening, your logo in the
corner. The editing itself is mechanical. The judgement is in deciding *what* to cut.

So this workflow splits those apart. You supply a link and an instruction like:

> Cut 2:20 to 2:25 where I fumble, make a short from 2:10 to 2:40, make it vertical for
> phones, and caption the first three seconds with "The one thing nobody tells you".

BOSS turns that sentence into a precise edit plan, runs it, and shows you the result. Nothing
reaches YouTube until you say so.

### What happens, step by step

| # | Step | Agent | What it does |
|---|------|-------|--------------|
| 1 | **Fetch the video** | `video_editor` | Places the link — Google Drive, an S3 bucket, or a plain HTTPS file — and brings it into the project |
| 2 | **Plan the edit** | `video_editor` | Reads your instruction and writes an edit plan. Renders nothing |
| 3 | **Apply the edit** | `video_editor` | Runs the plan and records what each operation changed |
| 4 | **Publish to YouTube** | `media_publisher` | Uploads as **private**. **Stops and waits for you** |

Two things you supply per run — the link and the instruction. Everything else is passed from one
step to the next.

### Three things worth understanding

**The model writes a plan; it never touches the video.** Step 2 produces a list of operations,
and every one is checked against a fixed vocabulary before anything renders. An instruction
asking for something BOSS cannot do — "speed up the boring middle" — comes back as *refused*,
named, rather than approximated with a different operation. A plan with one bad timecode is
rejected **whole**, because a half-edited file is indistinguishable from a finished one without
watching it.

**It tells you what it guessed.** Every operation carries the instruction it came from, and
anything ambiguous lands in `assumptions`. Ambiguity is guessed at rather than asked about —
you review before publishing anyway, and stopping to ask would cost a round trip on every
video. What it must not do is guess *silently*.

**Publishing cannot be made automatic.** `publish_youtube` is risk tier 3, and BOSS requires a
person at or above that floor whatever a workflow asks for. The step is not assigned to the
editor either: `video_editor` is *forbidden* to publish, and assigning it is refused when you
save. That separation is enforced, not conventional.

### The timecode trap

Each operation applies to the result of the one before it. Cut 1:00–2:00 and then caption at
5:00, and that caption lands at 6:00 of your original recording. BOSS raises a note when a plan
does this rather than silently reordering — guessing at intent on somebody's video is worse
than saying plainly what the timecodes will refer to.

If your timecodes were read off the original (they usually were), put the timed operations
*before* the cuts.

---

## Before you run it

### 1. A model must be connected

Step 2 asks a model to write the plan. Settings → connect one, if `connected_engines` is empty.

### 2. Connect Google — required for the first and last step

Step 1 stores the fetched video in Drive; step 4 publishes to your channel. One OAuth consent
covers both.

> **Channels → Connect** in the console.

Without it, steps 1 and 4 fail. Steps 2 and 3 work regardless.

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
    --api  http://YOUR_HOST:8000 \
    --key  "$BOSS_API_KEY" \
    --project YOUR_PROJECT \
    --actor   ceo
```

| Parameter | What it is | Default |
|---|---|---|
| `--api` | Your BOSS API. From your laptop this is `http://<host>:8000`, **not** `127.0.0.1` | `http://127.0.0.1:8000` |
| `--key` | `BOSS_API_KEY` from the install's `.env` | — (required) |
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

- `media.fetch_link`
- `video.plan_edit`
- `video.apply_plan`
- `publish_youtube`

A skill BOSS ships is *available*, not *installed*. Until it is enabled it lives in the
catalogue and not in your project, and binding a step to it fails with *"not registered in this
project"*. This is the step people miss.

### 2. Define four steps

**Delivery → Build your own → Your steps → Define a step.** For each: a name, choose **Skill**,
pick the skill, and list its inputs.

| Name | Skill | Inputs |
|---|---|---|
| Fetch the video | `media.fetch_link` | `url` |
| Plan the edit | `video.plan_edit` | `media_artifact_id`, `instructions` |
| Apply the edit | `video.apply_plan` | `media_artifact_id`, `plan` |
| Publish to YouTube | `publish_youtube` | `media_artifact_id` |

Publish will show **always needs a person**. That comes from its risk tier; you cannot turn it
off.

### 3. Create the workflow

**Create a new workflow** → name it → drag your four steps onto the canvas in order.

### 4. Wire the inputs

This is the part that makes it a workflow rather than four separate steps. Open each step's
**inputs**:

| Step | Input | Source |
|---|---|---|
| Fetch the video | `url` | **asked when the run starts** → `video_url` |
| Plan the edit | `instructions` | **asked when the run starts** → `instructions` |
| Plan the edit | `media_artifact_id` | **from an earlier step** → Fetch the video → `media_artifact_id` |
| Apply the edit | `media_artifact_id` | **from an earlier step** → Fetch the video → `media_artifact_id` |
| Apply the edit | `plan` | **from an earlier step** → Plan the edit → `plan` |
| Publish to YouTube | `media_artifact_id` | **from an earlier step** → Apply the edit → `media_artifact_id` |

Note that **Apply the edit** takes the video from *Fetch*, not from *Plan* — the planner
produces a plan, not a video.

### 5. Assign the agents

Each step row has an agent dropdown. Set the first three to `video_editor` and **Publish** to
`media_publisher`.

Try assigning `video_editor` to Publish — it is refused when you save, naming what that skill
does accept. That is the separation being enforced rather than described.

### 6. Save

**Save as a new version.** If anything is wrong the error names the step and why.

---

## Running it

**Delivery → Workflows → "Video edit and publish" → Start one.** It asks for `video_url` and
`instructions`.

Watch the run: steps 1–3 proceed on their own, then **Publish** stops and waits. Open it to see
the plan that was written, what the model assumed, anything it refused, and the rendered video —
then approve or reject.

---

## When something goes wrong

| What you see | What it means |
|---|---|
| *"skill 'x' is not registered in this project"* | Step 1 above. Enable it in Skills |
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

**BOSS does not ship this workflow.** It is an example. The four steps and the workflow are
data in *your* project, created through the public API. Change it, rename it, delete it — none
of it is part of the product, and a fresh install has the capability and none of this.
