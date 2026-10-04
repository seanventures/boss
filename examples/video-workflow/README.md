# Video editing and publishing

**Paste a link to a video, say what you want done in plain English, and get an edited video that
waits for your approval before it reaches your channel.**

---

## What it does

Most of what you publish from a long recording is a cut of it: dead air removed, a vertical crop
for phones, a caption over the opening, your logo in the corner. The editing is mechanical; the
judgement is in deciding *what* to cut. This workflow separates the two. You supply a link and an
instruction such as:

> Keep 0:28 to 1:48, make it portrait for phones, and put the ending slide on the end.

The editing agent reads that sentence, decides which of its skills to use and in what order, does
the work, and shows you the result. Nothing reaches YouTube until you approve it.

| # | Step | Agent | What it does |
|---|------|-------|--------------|
| 1 | **Let an agent work it out** | `video_editor` | **Binds no skill.** Fetches the link in your instruction and edits with the skills it holds |
| 2 | **Publish to YouTube** | `media_publisher` | Uploads as **private**. **Stops and waits for you** |

### An editing step that binds no skill

A fixed "plan the edit, then apply the plan" pipeline needs a new release every time someone asks
for an operation it does not have — an ending slide, a fade, a lower third. Instead, the editing
step carries your **instruction**, and `video_editor`'s **skills** are the vocabulary:

```
media.fetch_link  media.fetch_image  video.trim  video.cut  video.reframe
video.caption     video.overlay_image  video.watermark  video.bookend
```

The agent chooses from those and orders them itself. **To make this workflow do something new,
give the agent another skill**: Agents → `video_editor` → *Give this agent a skill*. No step is
edited and nothing needs a new release.

### The timecode trap

Each operation applies to the result of the one before it. Cut 1:00–2:00 and then caption at
5:00, and that caption lands at 6:00 of your original recording.

If there is any doubt, say which recording your timecodes refer to — "0:28 to 1:48 **of the
original**". The agent orders its own operations and is told about this trap, but your sentence
is the only thing that says what you meant.

---

## What you need

**A connected model.** The editing step is an agent working from your sentence. Connect one in
Settings if `connected_engines` is empty.

**Google only to publish — and for Drive links.** Fetching, editing and reviewing need no Google
account: the fetched source and every rendered edit are kept in BOSS's own artifact storage (the
install's artifacts volume), and you watch the result from the console. Connect Google
(**Skills and MCP → Channels → Connect**) when you want to:

- **publish to YouTube** — the last step. Without a channel, publishing refuses; and
- **edit a video from a Google Drive link** — reading it needs an account that can see it.

A direct HTTPS link to the file (`https://…/clip.mp4`) needs no connection. The start checklist
says the same thing: Google is listed against the publish step, and against the editing step only
when the link you gave is a Drive link. Because publishing stops for you anyway, you can start a
run, review the edit, and connect Google before you approve.

To keep the files in Drive as well, give the step a `store_provider` of `google` (or an
`output_folder_id`); without a Google connection they stay in BOSS and the step says so.

*Several channels?* Connect each one; they get their own keys (`youtube_default`, `youtube_2`, …),
each with its own token. To pin a step to a particular channel, open that step's **inputs** in the
builder, set `connection_key` to **a fixed value**, and type the key.

**ffmpeg.** Already in the BOSS images. On a hand-built environment, `ffmpeg` needs the
`libavfilter-extra` package and a font (`fonts-dejavu-core`); without them captions fail, because
`drawtext` is not in the base filter set.

---

## Run it

1. Set `BOSS_API_KEY` as described in [Examples](../README.md).
2. Preview with `--dry-run`, which reports what it would enable, define and create without
   touching anything. Then run it for real. Re-running is safe: steps and the workflow are matched
   by name and reused.

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
| `--dry-run` | Report what would happen and change nothing | off |

Each option can also come from the environment: `BOSS_API`, `BOSS_API_KEY`, `BOSS_PROJECT`,
`BOSS_ACTOR`.

3. Open **Workflows → Library**, find "Video edit and publish", then **⋯ → Start** and fill in:

| Field | What to put in it |
|---|---|
| `video_url` | optional link to your recording — a Google Drive share link or a direct link to the file (.mp4, .mov), not a YouTube or Vimeo page; you can put it in the instruction instead |
| `instructions` | *"Keep 0:28 to 1:48, make it portrait for phones, and put the ending slide on the end."* |
| `asset_url` | optional link to an ending slide, logo, or badge |
| `title` | optional; overrides the title the editing agent derives from your brief |
| `description` | optional; overrides the description the editing agent derives |

---

## What you'll review

The editing step runs, then **Publish** stops and waits. Open the agent step to see **which skills
it chose, in what order, and why**. That record answers "what did it actually do", and it is what
to read before approving.

**Watch the edit before you approve.** The publish approval shows the file it will upload as a
link: for a video BOSS keeps itself that opens `/v1/artifacts/<id>/media` in the console, behind
the same sign-in as the rest of the project, and plays in the browser.

---

## How it stays safe

**The agent can only use skills it was given.** The tools it is offered are exactly the ones it
holds in `role_skills`, and the runtime re-checks that on every call. An instruction that asks for
something it was not granted comes back as a named "I cannot do that", not an approximation with a
tool meant for something else.

**Its reach is limited to those skills.** An ordinary BOSS agent run in the software factory gets
a shell, a database and a file system. This kind of agent gets none of them.

**Publishing always needs a person.** `publish_youtube` is risk tier 3, and BOSS requires a person
at or above that floor whatever a workflow asks for. `video_editor` is *forbidden* to publish, and
assigning it to the publish step is refused when you save — the separation is enforced, not a
convention. Videos upload as **private**.

---

## Build it by hand

The script only calls the public API. Every step below does the same thing in the console, and
doing it once is the fastest way to understand what you have.

### 1. Enable the skills

**Skills → find each → Enable:**

- `media.fetch_link` — a link becomes a video
- `media.fetch_image` — a link becomes a picture: an ending slide, a title card, a logo
- `media.read_document` — a link becomes text: a brief, a title, a description
- `video.trim`, `video.cut`, `video.reframe`, `video.caption`, `video.overlay_image`,
  `video.watermark`, `video.bookend` — what the agent will compose from
- `publish_youtube`

A skill BOSS ships is *available*, not *installed*: until it is enabled, it lives in the catalogue
and not in your project. This is the step people most often miss.

### 2. Make sure the two agents exist

The default pack does not include `video_editor` or `media_publisher`. Install the **media pack**
(`POST /v1/packs/install` with `pack_id: media_pack`), which adds exactly those two. `setup.py`
does this for you when either is missing — granting a skill to an agent the project does not have
is refused, which is where this example used to stop.

### 3. Give the agents their skills

Enabling a skill puts it in your project; it does **not** give it to anybody. The runtime checks
`role_skills` before letting an agent call anything. A role spec's "allowed skills" list reaches
only the agent's *prompt*, so an agent can be told what it may use while holding nothing, and
every step naming it is refused at the first call.

- **Agents → `video_editor` → Give this agent a skill**, once per editing skill above.
- **Agents → `media_publisher` → Give this agent a skill** → `publish_youtube`.

The agent page shows what each agent holds. If that list is empty, the workflow cannot work, and
the page says so.

### 4. Define the publishing step

**Workflows → Library → Steps → + New step.** Give it a name, choose **Skill**, pick
the skill, and list its inputs.

| Name | Skill | Inputs |
|---|---|---|
| Publish to YouTube | `publish_youtube` | `media_artifact_id`, `title`, `description` |

Publish will show **always needs a person**. That comes from its risk tier, and you cannot turn it
off. The editing step needs no definition; see the next section.

### 5. Create the workflow with the agent step first

**Workflows → Library → + New workflow** → name it → then, from the palette:

1. **Let an agent work it out** — the first entry in the palette. It binds nothing.
2. **Publish to YouTube** (your step)

On the agent step's row, set the instruction source to **ask me when the run starts**. The other
option, *always do this*, pins a sentence onto the step for a workflow that does the same job
every time.

### 6. Wire the inputs

Open each step's **inputs**:

| Step | Input | Source |
|---|---|---|
| Let an agent work it out | `instructions` | **asked when the run starts** → `instructions` |
| Let an agent work it out | `video_url` | optional run input → `video_url` |
| Let an agent work it out | `asset_url` | optional run input → `asset_url` |
| Publish to YouTube | `media_artifact_id` | **from an earlier step** → Let an agent work it out → `media_artifact_id` |
| Publish to YouTube | `title` | the title you supplied, or **from an earlier step** → Let an agent work it out → `title` |
| Publish to YouTube | `description` | the description you supplied, or **from an earlier step** → Let an agent work it out → `description` |

Everything the agent step receives other than `instructions` is passed to the agent as **facts it
must use exactly**, such as an artifact id it must not invent. The agent step's output carries
whatever its skills returned, with the last one winning, which is why **Publish** can read
`media_artifact_id` from it.

### 7. Assign the agents

Each step row has an agent dropdown. Set the editing step to `video_editor` and **Publish** to
`media_publisher`.

On the agent step, leaving the dropdown blank means **whichever agent fits**: BOSS picks based on
what each agent is for and what it holds, and records why in the run.

Try assigning `video_editor` to Publish: it is refused when you save, and the error names what that
skill does accept.

### 8. Save

**Save as a new version.** If anything is wrong, the error names the step and the reason.

---

## Troubleshooting

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
| The start checklist lists *Google (Drive link)* | Your link is a Drive link. Connect Google, or use a direct HTTPS link to the file |

---

## Customize

- **Teach it a new edit** by giving `video_editor` another skill. The workflow does not change.
- **Pin a channel** by setting the publish step's `connection_key` to a fixed value.
- **Fix the instruction** with *always do this* on the agent step, for a workflow that does the
  same job every time.

**BOSS ships the capability**: the video skills, `media.fetch_link`, `publish_youtube`, the
`video_editor` and `media_publisher` roles (in the media pack, which is not installed by
default), and ffmpeg in the images. All of it is opt-in; nothing
is forced on a project. **The workflow itself is an example**, not part of the product. Its steps
and definition are data in *your* project, created through the public API, and a fresh install
has the capability without any of this. Change it, rename it, or delete it.
