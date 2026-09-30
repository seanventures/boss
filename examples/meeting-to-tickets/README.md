# Meeting to tickets

**Paste a transcript, read what BOSS heard, approve it, and the tickets appear in your tracker.**

---

## What it does

A meeting produces decisions and action items, and within a day some of them exist only in one
person's memory. Turning notes into tickets is mechanical; deciding *what was actually agreed* is
not. This workflow separates the two.

You paste the transcript. BOSS writes down what was decided and what somebody has to do, with
the owner **as named** and the timing **as said**. You review that as a list of sentences. If it
is right, you approve and the tickets are created in Plane, Jira, Linear, GitHub Issues or Azure
DevOps — whichever you have connected.

| # | Step | Agent | What it does |
|---|------|-------|--------------|
| 1 | **Read the meeting** | `program_manager` | Transcript → decisions + actions. Creates nothing |
| 2 | **Create the tickets** | `program_manager` | Creates them in your tracker. **Stops and waits for you** |

The extraction brief holds three rules, each guarding against a failure that would otherwise
look plausible:

- **No invented owners.** If nobody was named, the ticket is unassigned. Every tracker has an
  unassigned state; none has a "the model guessed" state, and the person a guessed ticket lands on
  will not know why it is theirs.
- **No invented due dates.** Timing is recorded in the speakers' words, or not at all.
- **A meeting that decided nothing produces nothing.** An empty list is a valid outcome. Two
  conjured action items would be the hardest failure to catch.

It also reports what it heard but **could not place** — that list is where a missed action item
will turn up.

---

## What you need

**A connected model.** Step 1 asks a model to read the transcript. Connect one in Settings.

**A connected work tracker.** Step 2 needs somewhere to create tickets. **Plane** is BOSS's
default; Jira, Linear, GitHub Issues and Azure DevOps work the same way. For Plane you need:

| Field | Where it comes from |
|---|---|
| API / web base URL | your Plane instance, e.g. `https://plane.example.com` |
| Workspace slug | the workspace part of your Plane URL |
| Project ID | the project's UUID |
| API key | Plane → Settings → API tokens (the `X-API-Key` value) |

Without a tracker, step 2 refuses with a message that says so.

---

## Run it

1. Set `BOSS_API_KEY` as described in [examples/README.md](../README.md).
2. Install the workflow. Start with `--dry-run`; re-running is safe.

```bash
python3 examples/meeting-to-tickets/setup.py \
    --api  http://127.0.0.1:8000 \
    --project YOUR_PROJECT \
    --actor   ceo
```

   The parameters are the same for every example — see [examples/README.md](../README.md).
3. Open **Delivery → Workflows → "Meeting to tickets" → Start one** and fill in:
   - `transcript` — paste the notes or transcript
   - `context` — optional: who the team are, what the project is called, anything the transcript
     assumes

---

## What you'll review

The second step stops for approval. Open it to see:

```
Decided:
  · Ship the pricing page on Friday — the campaign starts Monday

Tickets to create (2):
  · Draft three pricing options  [Ravi, before Wednesday]
  · Update the campaign copy  [unassigned]

Could not place:
  · a discussion about the logo that reached no conclusion
```

Approve, and those become tickets. Each one carries what was actually said, so the person who
picks it up does not need to have been in the room.

---

## How it stays safe

Step 1 is **risk tier 0**: it reads text and returns a list. Step 2 is **tier 2**, which is BOSS's
approval floor, so it stops for a person regardless of what the workflow asks for.

That split is the point of the example. You approve *a list of sentences you can read* — "Draft
three pricing options [Ravi, before Wednesday]" — rather than running a step and finding out
afterwards what landed in a tracker other people work from.

---

## Build it by hand

The script only calls the public API; every step below does the same thing in the console.

1. **Enable the skills first.** **Skills → Enable:** `meeting.extract_actions`,
   `tracker.issues.create`.

   A skill BOSS ships is *available*, not *installed*. Until it is enabled, binding a step to it
   fails with *"not registered in this project"*.

2. **Define two steps.** **Delivery → Build your own → Your steps → Define a step.**

   | Name | Skill | Inputs |
   |---|---|---|
   | Read the meeting | `meeting.extract_actions` | `transcript` |
   | Create the tickets | `tracker.issues.create` | `actions` |

   The second step will show **always needs a person**.

3. **Create the workflow, wire it, and assign the agents.** **Create a new workflow** → drag both
   steps on → open each step's **inputs**:

   | Step | Input | Source |
   |---|---|---|
   | Read the meeting | `transcript` | **asked when the run starts** → `transcript` |
   | Read the meeting | `context` | **asked when the run starts** → `context` *(optional)* |
   | Create the tickets | `actions` | **from an earlier step** → Read the meeting → `actions` |

   Set both agents to `program_manager`, then save.

---

## Troubleshooting

| What you see | What it means |
|---|---|
| *"skill 'x' is not registered in this project"* | Enable it in Skills first |
| *"this project has no active work tracker configured"* | Connect Plane (or another tracker) |
| *"no transcript was given"* | The run started with an empty `transcript` |
| *Partial* — some tickets created, some failed | Results are reported per ticket. The ones that succeeded are real; the `failed` list says why the others did not |

---

## Customize

**BOSS ships** `meeting.extract_actions` and `tracker.issues.create`, the tracker adapters, and
the `program_manager` role. **The workflow itself is not part of BOSS** — it is data in your
project, created through the public API, and yours to change or delete. Use `context` to tell the
reading step about your team and project; step 2 files into whichever tracker is active.
