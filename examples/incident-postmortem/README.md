# Incident postmortem

**Paste the incident notes. The timeline and the action items are written at the same time,
the postmortem is drafted from both, and it reaches a reviewer and the tracker — each after
you approve.**

---

## What it does

The day after an incident, two things need writing from the same notes: what happened, in
order, and what somebody now has to do. They are independent readings, so this workflow does
them in parallel, then writes the postmortem from both.

| # | Step | Agent | What it does |
|---|------|-------|--------------|
| 1a | **Write the timeline** | `program_manager` | Notes (and chat log) → timestamped timeline and impact, in the notes' own words |
| 1b | **Read the action items** | `program_manager` | Notes → what somebody has to do, owner as named, timing as said. Creates nothing |
| 2 | **Write the postmortem** | `program_manager` | Timeline + action items → the blameless postmortem |
| 3 | **Email the postmortem** | `program_manager` | Sends it to the reviewer. **Stops for you** |
| 4 | **File the action items** | `program_manager` | Creates them in the tracker. **Stops for you, separately** |

1a and 1b run **at the same time** under a `parallel` step; a `join` waits for both. Step 2
reads the timeline as its main text and the action list as its material — a step wires whole
values, so a document from two outputs takes two input fields.

Two rules in the briefs: **times and names as written** (a time not stated is "time not
recorded", never estimated), and **no causes the material does not support** — where the cause
is not established, the postmortem says so.

---

## What you need

**A connected model.** Steps 1a, 1b and 2 use one.

**Email configured** for step 3, and **a work tracker** connected in **Skills & MCP → File
tickets** for step 4. With only one of the two, the workflow installs and `Start` says which
step cannot run yet.

---

## Run it

1. Set `BOSS_API_KEY` as described in [examples/README.md](../README.md).
2. Install the workflow. Start with `--dry-run`; re-running is safe.

```bash
python3 examples/incident-postmortem/setup.py \
    --api  http://127.0.0.1:8000 \
    --project YOUR_PROJECT \
    --actor   ceo
```

3. Open **Workflows → Library**, find "Incident postmortem", then **⋯ → Start** and fill in:
   - `notes` — the incident notes, pasted
   - `chat_log` — optional: the incident channel, pasted
   - `to` — who reviews the postmortem
   - `subject` — optional

---

## What you'll review

**First stop**, the postmortem before it is emailed:

```
# Postmortem: API latency, 12 Oct

## Summary
...
## Timeline
- 09:14  Alerts fire on p95 latency (Priya)
- 09:21  Rolled back deploy 3f2a1c (Dan)
- time not recorded  Root cause identified as connection-pool exhaustion
...
## Action items
- Add a pool-size alert [Dan, "this week"]
- Load-test the pool change before re-deploy [unassigned]
```

**Second stop**, the action items as tickets:

```
Tickets to create (2):
  · Add a pool-size alert  [Dan, this week]
  · Load-test the pool change before re-deploy  [unassigned]
```

Reject either on its own. Rejecting the tickets does not unsend the email; rejecting the email
stops the run before the tickets.

---

## How it stays safe

Steps 1 and 2 read and write text: **tier 0**. Step 3 is **tier 2** and its own policy requires
an approval; step 4 is **tier 2**, the approval floor. **Nothing is sent or filed until a person
approves it.** Each effect is approved on its own, because they land in different systems.

---

## Build it by hand

1. **Enable the skills.** `model.generate_text`, `meeting.extract_actions`,
   `email.send_real_guarded`, `tracker.issues.create`. Give all four to `program_manager`.
2. **Define five steps** (**Workflows → Library → Steps → + New step**):

   | Name | Skill | Inputs |
   |---|---|---|
   | Write the timeline | `model.generate_text` | `prompt`, `context` (optional), `system` (optional) |
   | Read the action items | `meeting.extract_actions` | `transcript`, `context` (optional) |
   | Write the postmortem | `model.generate_text` | `prompt`, `context` (optional), `system` (optional) |
   | Email the postmortem | `email.send_real_guarded` | `to`, `body`, `subject` (optional) |
   | File the action items | `tracker.issues.create` | `actions` |

3. **Create the workflow.** A **Do at the same time** step `fork` with branches `timeline` and `actions`,
   joined at `join`. `timeline`: `system` the timeline brief, `prompt` from input `notes`,
   `context` from input `chat_log`. `actions`: `transcript` from `notes`, `context` from
   `chat_log`. A **Wait for branches** step waiting for both, then `write`: `system` the postmortem brief,
   `prompt` from `timeline → text`, `context` from `actions → actions`. Then `send` (`body` from
   `write → text`, manual) and `file` (`actions` from `actions → actions`, manual).

---

## Troubleshooting

| What you see | What it means |
|---|---|
| *not ready yet: email* / *no connected work tracker* | Configure SMTP, or connect a tracker in Skills & MCP |
| "time not recorded" throughout | The notes have no timestamps. That is reported, not invented |
| No action items | The notes decided nothing. The file step creates nothing and says so |

---

## Customize

Post the postmortem to a channel with `slack.post_real_guarded` instead of email. Add a third
parallel branch that drafts customer-facing status text. Give the action-item step a `context`
describing how your tickets are usually written.
