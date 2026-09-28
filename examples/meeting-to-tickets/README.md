# Meeting to tickets

**Paste a transcript. Read what BOSS heard. Approve it, and the tickets appear in your tracker.**

---

## The use case

Every team has the same leak. A meeting produces six decisions and four things somebody has to
do, and within a day two of them exist only in one person's memory. The notes, if anyone took
them, are a wall of text nobody converts into work.

Converting them is mechanical. Deciding *what was actually agreed* is not — which is the same
split the video workflow makes, and the reason this is two steps rather than one.

You paste the transcript. BOSS writes down what was decided and what somebody has to do, with
the owner **as named** and the timing **as said**. You read that as a list of sentences. If it
is right, the tickets are created in Plane — or Jira, Linear, GitHub Issues or Azure DevOps,
whichever you have connected.

### What happens

| # | Step | Agent | What it does |
|---|------|-------|--------------|
| 1 | **Read the meeting** | `program_manager` | Transcript → decisions + actions. Creates nothing |
| 2 | **Create the tickets** | `program_manager` | Creates them in your tracker. **Stops and waits for you** |

### Why the split matters

Step 1 is **risk tier 0** — it reads text and returns a list. Step 2 is **tier 2**, which is
BOSS's approval floor, so it stops whatever the workflow asks for.

That gap is the product. You approve *a list of sentences you can read* — "Draft three pricing
options [Ravi, before Wednesday]" — not "run a step and find out what landed in the tracker
other people work from".

### Three rules it holds, because each is a plausible-looking failure

**It will not invent an owner.** If nobody was named, the ticket is unassigned. Every tracker
has an unassigned state; none has a "the model guessed" state, and the person it lands on will
not know why it is theirs.

**It will not invent a due date.** What was said about timing, in their words, or nothing.

**A meeting that decided nothing produces nothing.** Empty lists are a real outcome. A model
conjuring two action items to look useful is the worst failure available here, and the one
you would not catch.

It also reports what it heard and **could not place** — that list is where a missed action
item will be.

---

## Before you run it

### 1. A model must be connected

Step 1 asks a model to read the transcript. Settings → connect one.

### 2. A work tracker must be connected

Step 2 needs somewhere to create tickets. **Plane** is BOSS's default; Jira, Linear, GitHub
Issues and Azure DevOps work the same way.

For Plane you will need:

| Field | Where it comes from |
|---|---|
| API / web base URL | your Plane instance, e.g. `https://plane.example.com` |
| Workspace slug | the workspace part of your Plane URL |
| Project ID | the project's UUID |
| API key | Plane → Settings → API tokens (the `X-API-Key` value) |

Without one, step 2 refuses with a message saying so rather than pretending to file anything.

---

## Running the setup script

```bash
python3 examples/meeting-to-tickets/setup.py \
    --api  http://127.0.0.1:8000 \
    --project YOUR_PROJECT \
    --actor   ceo
```

Same parameters as every example — see [examples/README.md](../README.md). Start with
`--dry-run`. Re-running is safe.

---

## Doing it by hand instead

### 1. Enable the skills — **first**

**Skills → Enable:** `meeting.extract_actions`, `tracker.issues.create`

A skill BOSS ships is *available*, not *installed*. Until it is enabled, binding a step to it
fails with *"not registered in this project"*.

### 2. Define two steps

**Delivery → Build your own → Your steps → Define a step.**

| Name | Skill | Inputs |
|---|---|---|
| Read the meeting | `meeting.extract_actions` | `transcript` |
| Create the tickets | `tracker.issues.create` | `actions` |

The second will show **always needs a person**.

### 3. Create the workflow, wire it, assign the agents

**Create a new workflow** → drag both steps on → open each step's **inputs**:

| Step | Input | Source |
|---|---|---|
| Read the meeting | `transcript` | **asked when the run starts** → `transcript` |
| Read the meeting | `context` | **asked when the run starts** → `context` *(optional)* |
| Create the tickets | `actions` | **from an earlier step** → Read the meeting → `actions` |

Set both agents to `program_manager`. Save.

---

## Running it

**Delivery → Workflows → "Meeting to tickets" → Start one.**

- `transcript` — paste the notes or transcript
- `context` — optional: who the team are, what the project is called, anything the transcript
  assumes

The second step stops. Open it and you will see:

```
Decided:
  · Ship the pricing page on Friday — the campaign starts Monday

Tickets to create (2):
  · Draft three pricing options  [Ravi, before Wednesday]
  · Update the campaign copy  [unassigned]

Could not place:
  · a discussion about the logo that reached no conclusion
```

Approve, and those become tickets. Each carries what was actually said, so the person who
reads it was not required to be in the room.

---

## When something goes wrong

| What you see | What it means |
|---|---|
| *"skill 'x' is not registered in this project"* | Enable it in Skills first |
| *"this project has no active work tracker configured"* | Connect Plane (or another) |
| *"no transcript was given"* | The run started with an empty `transcript` |
| *Partial* — some tickets created, some failed | Reported per ticket, not hidden. The ones that succeeded are real; the `failed` list says why the others did not |

---

## What ships with BOSS, and what does not

**BOSS ships** `meeting.extract_actions` and `tracker.issues.create`, the tracker adapters, and
the `program_manager` role. **BOSS does not ship this workflow** — it is data in your project,
created through the public API, yours to change or delete.
