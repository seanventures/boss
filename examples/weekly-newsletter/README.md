# Weekly newsletter

**Name the things you care about. BOSS prepares a researched, cited newsletter every Monday
and waits for your approval before emailing it.**

---

## The use case

You want to keep up with four or five things — your industry, your city, a sport, whatever a
competitor is doing — and you do not want to spend Monday morning doing it. Reading the news is
not the work; *knowing what happened* is.

So this does the reading. BOSS researches the sectors you named, writes them up as a
newsletter, and emails it after approval. With `--schedule` it starts the research every Monday
without anyone asking.

### What happens

| # | Step | What it does |
|---|------|--------------|
| 1 | **Gather** | BOSS's own `research` capability — real web research into a **cited dossier**, gated on evidence |
| 2 | **Write** | Turns the dossier into a newsletter, one section per sector |
| 3 | **Send** | Emails it. **Stops and waits for you** |

Step 1 is a **built-in capability**; steps 2 and 3 are steps of your own. A workflow can mix
both, and this example shows it deliberately.

### What is worth understanding

**The research is cited and evidence-gated.** A sector with thin news comes back **sparse
rather than padded** — the section will say so in a line instead of inventing three items. That
is correct behaviour and it will look like an empty section, so it is worth knowing before you
call it a bug.

**It stops before sending, twice over.** `email.send_real_guarded` is tier 2 (BOSS's approval
floor) *and* requires an approval in its own policy. A newsletter you did not write is the one
thing you want to read before it goes out.

**The schedule holds the sectors.** A scheduled run needs nobody to type anything on Monday
morning. A one-off run can still override them — which is why they are workflow inputs rather
than baked into the steps.

---

## Before you run it

### 1. A model must be connected

Steps 1 and 2 both need one.

### 2. SMTP must be configured, for step 3

| Setting | What it is |
|---|---|
| `BOSS_EMAIL_SMTP_HOST` | your mail server |
| `BOSS_EMAIL_FROM_ADDRESS` | what it sends as |
| `BOSS_EMAIL_REAL_SEND_ALLOWED` | must be `true`, deliberately — sending is off until you say so |

Without these, step 3 refuses and **names the setting that is missing**. It does not report
success with nothing sent.

---

## Running the setup script

```bash
# Build it
python3 examples/weekly-newsletter/setup.py --api http://127.0.0.1:8000

# Build it AND prepare a draft every Monday at 08:00 in your zone
python3 examples/weekly-newsletter/setup.py --api http://127.0.0.1:8000 \
    --schedule Europe/London --sectors "energy, healthcare" \
    --to reader@example.com
```

`--schedule` takes an **IANA zone name** — `Europe/London`, `America/New_York`,
`Asia/Kolkata`, `UTC`. Bare `--schedule` means UTC.

**The zone matters.** "Monday 08:00" means 08:00 where *you* are. BOSS stores the zone with the
schedule and computes in it, so the newsletter holds its hour across daylight-saving changes
instead of drifting by an hour twice a year.

`--schedule` requires both `--sectors` and `--to` before it changes anything. They become the
inputs for each Monday run. The console can pause or remove a schedule, but cannot edit those
inputs yet. To change the topics or recipient, remove the schedule from Delivery → **Yours**
and run this command again. The send step still waits for your approval.

Start with `--dry-run`. Re-running is safe.

---

## Doing it by hand instead

### 1. Enable the skills

**Skills → Enable:** `model.generate_text`, `email.send_real_guarded`

### 2. Define two steps

| Name | Skill | Inputs |
|---|---|---|
| Write the newsletter | `model.generate_text` | `prompt` |
| Email the newsletter | `email.send_real_guarded` | `to`, `body` |

### 3. Create the workflow

Drag **Research** (from the built-in palette) plus your two steps, in that order.

### 4. Wire the inputs

| Step | Input | Source |
|---|---|---|
| Research | `seed` | **asked when the run starts** → `sectors` |
| Write | `prompt` | **asked when the run starts** → `brief` |
| Write | `context` | **from an earlier step** → Research → `dossier` |
| Email | `to` | **asked when the run starts** → `to` |
| Email | `subject` | **asked when the run starts** → `subject` |
| Email | `body` | **from an earlier step** → Write → `text` |

That `context` wiring is the interesting one: a step passes whole **values** and cannot build
*"write a newsletter from: {dossier}"* out of two of them. So the standing instruction is
`prompt`, the research is `context`, and the step joins them.

### 5. Schedule it

Use the setup command above with `--schedule`, `--sectors` and `--to`. The console's generic
**Schedule** button currently asks only for a time zone and cannot store the required newsletter
inputs, so it is not a complete scheduling path for this workflow. The same schedule can be
created through `POST /v1/delivery-workflows/schedules` with those values in `input`.

Pause, resume and remove are on the schedule line in Delivery → **Yours**. Resuming recomputes
the next run, so a schedule paused for a month does not fire the moment you bring it back.

---

## Running it

**Delivery → Workflows → "Weekly newsletter" → Start one.**

- `sectors` — one line: *"UK politics, Premier League football, Chennai local news, AI industry moves"*
- `brief` — how to write it (the script sets a sensible house brief)
- `to` — where it goes
- `subject` — the email subject

---

## When something goes wrong

| What you see | What it means |
|---|---|
| *"BOSS_EMAIL_SMTP_HOST is not configured"* | Step 3's settings. It refuses rather than pretending |
| *"BOSS_EMAIL_REAL_SEND_ALLOWED is false"* | Sending is off by default. Deliberate |
| A section says there was little to report | The research found little. Sparse rather than padded is the design |
| The schedule fired and failed | The card shows the last error. Five consecutive failures disables it |
| Nothing arrived on Monday | Check the run and its approval request; scheduling starts the work but never approves the send |

---

## What ships with BOSS, and what does not

**BOSS ships** the `research` capability, `model.generate_text`, `email.send_real_guarded`, and
the scheduler. **BOSS does not ship this workflow** — it is data in your project.
