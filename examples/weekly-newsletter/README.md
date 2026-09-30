# Weekly newsletter

**Name the topics you care about. BOSS prepares a researched, cited newsletter every Monday and
waits for your approval before emailing it.**

---

## What it does

You want to keep up with four or five things — your industry, your city, a sport, a competitor —
without spending Monday morning reading the news. BOSS researches the sectors you name, writes
them up as a newsletter, and emails it after approval. With `--schedule`, the research starts
every Monday without anyone asking.

| # | Step | What it does |
|---|------|--------------|
| 1 | **Gather** | BOSS's own `research` capability — real web research into a **cited dossier**, gated on evidence |
| 2 | **Write** | Turns the dossier into a newsletter, one section per sector |
| 3 | **Send** | Emails it. **Stops and waits for you** |

Step 1 is a **built-in capability**; steps 2 and 3 are steps of your own. This example
deliberately shows a workflow mixing both.

**The research is cited and evidence-gated.** A sector with little news comes back **sparse
rather than padded**: the section says so in a line instead of inventing three items. That is the
intended behaviour, even though it looks like an empty section.

**The schedule holds the sectors.** A scheduled run needs nobody to type anything on Monday
morning. A one-off run can still override them, which is why they are workflow inputs rather
than part of the steps.

---

## What you need

**A connected model**, for steps 1 and 2.

**SMTP configured**, for step 3:

| Setting | What it is |
|---|---|
| `BOSS_EMAIL_SMTP_HOST` | your mail server |
| `BOSS_EMAIL_FROM_ADDRESS` | what it sends as |
| `BOSS_EMAIL_REAL_SEND_ALLOWED` | must be `true`, deliberately — sending is off until you say so |

Without these, step 3 refuses and **names the missing setting** rather than reporting success
with nothing sent.

---

## Run it

1. Set `BOSS_API_KEY` as described in [examples/README.md](../README.md).
2. Install the workflow, optionally with a Monday schedule. Start with `--dry-run`; re-running is
   safe.

```bash
# Build it
python3 examples/weekly-newsletter/setup.py --api http://127.0.0.1:8000

# Build it AND prepare a draft every Monday at 08:00 in your zone
python3 examples/weekly-newsletter/setup.py --api http://127.0.0.1:8000 \
    --schedule Europe/London --sectors "energy, healthcare" \
    --to reader@example.com
```

3. To run it once by hand, open **Delivery → Workflows → "Weekly newsletter" → Start one** and
   fill in:
   - `sectors` — one line: *"UK politics, Premier League football, Chennai local news, AI industry moves"*
   - `brief` — how to write it (the script sets a sensible house brief)
   - `to` — where it goes
   - `subject` — the email subject

### Scheduling

`--schedule` takes an **IANA zone name** — `Europe/London`, `America/New_York`, `Asia/Kolkata`,
`UTC`. Bare `--schedule` means UTC.

"Monday 08:00" means 08:00 where *you* are. BOSS stores the zone with the schedule and computes
in it, so the newsletter keeps its hour across daylight-saving changes instead of drifting twice
a year.

`--schedule` requires both `--sectors` and `--to` before it changes anything; they become the
inputs for each Monday run. To change the topics or recipient later, use **edit inputs** on the
schedule line in Delivery → **Yours** (see [Build it by hand](#build-it-by-hand), step 5), or
remove the schedule there and run the setup command again. The send step still waits for your
approval.

---

## What you'll review

The send step stops with the finished newsletter: one section per sector, each cited, with sparse
sectors saying so in a line. Approve it and the email goes out.

---

## How it stays safe

**It stops before sending, twice over.** `email.send_real_guarded` is tier 2 (BOSS's approval
floor) *and* requires an approval in its own policy. A newsletter you did not write is exactly
what you want to read before it goes out. Scheduling starts the work but never approves the send,
and real sending stays off until `BOSS_EMAIL_REAL_SEND_ALLOWED` is `true`.

---

## Build it by hand

1. **Enable the skills.** **Skills → Enable:** `model.generate_text`, `email.send_real_guarded`

2. **Define two steps.**

   | Name | Skill | Inputs |
   |---|---|---|
   | Write the newsletter | `model.generate_text` | `prompt` |
   | Email the newsletter | `email.send_real_guarded` | `to`, `body` |

3. **Create the workflow.** Drag **Research** (from the built-in palette) plus your two steps, in
   that order.

4. **Wire the inputs.**

   | Step | Input | Source |
   |---|---|---|
   | Research | `seed` | **asked when the run starts** → `sectors` |
   | Write | `prompt` | **asked when the run starts** → `brief` |
   | Write | `context` | **from an earlier step** → Research → `dossier` |
   | Email | `to` | **asked when the run starts** → `to` |
   | Email | `subject` | **asked when the run starts** → `subject` |
   | Email | `body` | **from an earlier step** → Write → `text` |

   The `context` wiring is the interesting one. A step passes whole **values**, so it cannot build
   *"write a newsletter from: {dossier}"* out of two of them. Instead, the standing instruction is
   `prompt`, the research is `context`, and the step joins them.

5. **Schedule it.** Use the setup command above with `--schedule`, `--sectors` and `--to`, or
   Delivery → **Yours** → **Schedule**. The button asks for the time zone and for `sectors`,
   `brief`, `to`, and `subject`. Required values are stored on the schedule. **edit inputs**
   replaces them without creating a second Monday run. The API is the same
   `POST /v1/delivery-workflows/schedules`: send `schedule_id` to edit, and omit it to create. A
   save without the required inputs is refused.

   Pause, resume and remove are on the schedule line in Delivery → **Yours**. Resuming recomputes
   the next run, so a schedule paused for a month does not fire the moment you bring it back.

---

## Troubleshooting

| What you see | What it means |
|---|---|
| *"BOSS_EMAIL_SMTP_HOST is not configured"* | Step 3's settings are missing. It refuses rather than pretending to send |
| *"BOSS_EMAIL_REAL_SEND_ALLOWED is false"* | Sending is off by default, deliberately |
| A section says there was little to report | The research found little. Sparse rather than padded is the design |
| The schedule fired and failed | The card shows the last error. Five consecutive failures disable the schedule |
| Nothing arrived on Monday | Check the run and its approval request; scheduling starts the work but never approves the send |

---

## Customize

Change `brief` to set the voice and structure, or `sectors` to change what is researched — per
run, or on the schedule with **edit inputs**.

**BOSS ships** the `research` capability, `model.generate_text`, `email.send_real_guarded`, and
the scheduler. **The workflow itself is not part of BOSS** — it is data in your project.
