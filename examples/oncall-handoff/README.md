# Oncall handoff

**The SRE agent reads what BOSS ran, what failed and what is still waiting, adds your notes,
and writes the handoff — emailed to the incoming on-call once you approve.**

---

## What it does

A handoff is a list of what the next person needs to know, and most of it is already recorded
somewhere nobody reads at the end of a shift. This workflow has the SRE agent read BOSS's own
records for the period — run logs, decisions waiting, deployments and health, what is running
now — and write the handoff from them.

| # | Step | Agent | What it does |
|---|------|-------|--------------|
| 1 | **gather** | `sre` | Reads BOSS through its own read tools; adds the outgoing on-call's notes. Declares `handoff` and `open_items` |
| 2 | **Email the handoff** | `sre` | Sends it to the incoming on-call. **Stops for you** |

**What it reads is what BOSS knows.** The agent's tools are `boss.fundamentals` — read logs,
run activity, decisions, deployments, health. It is not a pager and reads no alerting system.
If your on-call covers services BOSS does not run, the `notes` input is where that goes, and
the brief tells the agent to report only what the tools returned.

With `--schedule`, the handoff is prepared every Monday; the email still waits for a person.

---

## What you need

**A connected model**, and **email configured** for the last step. The read tools need nothing
set up: they read the project BOSS already holds.

---

## Run it

1. Set `BOSS_API_KEY` as described in [examples/README.md](../README.md).
2. Install the workflow. Start with `--dry-run`; re-running is safe.

```bash
python3 examples/oncall-handoff/setup.py \
    --api  http://127.0.0.1:8000 \
    --project YOUR_PROJECT \
    --actor   ceo
```

   To also prepare it every Monday at 09:00:

```bash
python3 examples/oncall-handoff/setup.py --schedule Europe/London --to oncall@example.com
```

3. Open **Workflows → Library**, find "Oncall handoff", then **⋯ → Start**:
   - `period` — optional; defaults to the last 7 days
   - `notes` — optional: what BOSS cannot see
   - `to` — the incoming on-call

---

## What you'll review

The send step stops. Open it to read the handoff:

```
Still open
  · Decision dec_4f2a — "Deploy member-portal to production?" waiting since Fri 16:10
  · Run wfr_9c78 (Standard Product Delivery: invoice export) — failed at deploy, not retried

What broke and what was done
  · wfr_8b11 dead-lettered Tue 03:12: step_timeout on build; retried Wed, succeeded

Deployed this period
  · member-portal → staging, Thu 11:04, healthy

Watch for
  · The deployment host "do-fra-1" reported low disk on Sat

Notes from the outgoing on-call
  · Vendor ticket #5521 with the DNS provider still open
```

---

## How it stays safe

The gather step reads only (**tier 0** tools), scoped to this project. The email is **tier 2**
and its own policy requires an approval. **Nothing is sent until a person approves it.**

---

## Build it by hand

1. **Enable the skills.** `boss.fundamentals`, `email.send_real_guarded`; give both to `sre`
   (every agent already holds `boss.fundamentals`; adding it is harmless).
2. **Define one step.** "Email the handoff" on `email.send_real_guarded` (`to`, `body`,
   `subject`).
3. **Create the workflow.** **Agent task** `gather` (agent `sre`, instructions asked at start
   as `gather_brief`, context `period` and `notes` from input, producing `handoff` and
   `open_items`) → `send` (`body` from `gather → handoff`, approval manual).
4. **Schedule it** (optional). **⋯ → Schedule…** on the workflow: weekly, Monday, 09:00, your
   time zone, with `to` filled in.

---

## Troubleshooting

| What you see | What it means |
|---|---|
| "could not be read" under Watch for | A read tool failed; the handoff says which, rather than omitting the section |
| An empty handoff | Nothing ran, failed or waited in the period. That is the answer |
| *not ready yet: email* | Configure SMTP in Settings |

---

## Customize

Shorten `period` for a daily handoff and schedule it daily. Post to a channel with
`mattermost.post_real` or `slack.post_real_guarded` instead of email. Give `sre` a skill that
reads your alerting system, when one exists, and the brief covers it without changing the step.
