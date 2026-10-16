# Support ticket triage

**Paste a batch of tickets. Each one is categorised, prioritised and given a draft reply — one
step run per ticket — and the digest reaches your support lead once you approve. Nothing goes
to a customer.**

---

## What it does

A batch of tickets is a list, and a workflow's step list is fixed when it is published. This
example is the one to copy whenever the number of things to handle is not known in advance:
a `for_each` step runs one step once per item, a few at a time, and finishes with every result
in order.

| # | Step | Agent | What it does |
|---|------|-------|--------------|
| 1 | **split** | `program_manager` | The pasted batch → a list of tickets, text unchanged. Declares `tickets` |
| 2 | **each** | — | A `for_each` over `tickets`: runs step 3 once per ticket, two at a time, at most fifty |
| 3 | **reply** | `program_manager` | One ticket → `category`, `priority`, `draft` reply |
| 4 | **Write the digest** | `program_manager` | Every result → one digest in priority order |
| 5 | **Email the support lead** | `program_manager` | Sends the digest. **Stops for you** |

Each ticket is its own step run with its own attempts; open the `each` step on the run to see
them. A batch longer than fifty is refused with the count, never cut short.

The reply brief's rules: **no promised fix dates, refunds or features**, and **no invented
product behaviour** — where the agent is not sure, the draft says the team will confirm. The
`product` input is where you tell it what is true.

**Nothing is sent to a customer.** The drafts go to your support lead, who edits and sends the
ones worth sending from the help desk. BOSS has no help-desk connector to read tickets from or
reply through; that is why the batch is pasted and the replies are drafts.

---

## What you need

**A connected model**, and **email configured** for the last step.

---

## Run it

1. Set `BOSS_API_KEY` as described in [examples/README.md](../README.md).
2. Install the workflow. Start with `--dry-run`; re-running is safe.

```bash
python3 examples/support-ticket-triage/setup.py \
    --api  http://127.0.0.1:8000 \
    --project YOUR_PROJECT \
    --actor   ceo
```

3. Open **Workflows → Library**, find "Support ticket triage", then **⋯ → Start**:
   - `tickets` — the batch, pasted; separate tickets with a blank line or a reference
   - `product` — optional: what we support and how it behaves
   - `to` — the support lead's address. Never a customer's
   - `reply_brief` — optional; blank uses the stored brief

---

## What you'll review

The last step stops. Open it to read the digest:

```
Urgent 1 · High 2 · Normal 4 · Low 1

#1842  Northwind  bug  URGENT
  Exports fail since this morning with "502"
  Draft: Thanks for the report — we can see the failures on our side and are
  working on it now. We'll update this ticket within the hour. No action needed
  from you.

#1839  Contoso  billing  normal
  Charged twice in October
  ...
```

---

## How it stays safe

Steps 1 to 4 read and write text: **tier 0**. The email is **tier 2** and its own policy
requires an approval. **Nothing is sent until a person approves it**, and the only recipient is
the address you gave for the support lead.

---

## Build it by hand

1. **Enable the skills.** `model.generate_text`, `email.send_real_guarded`; give them to
   `program_manager`.
2. **Define two steps.** "Write the digest" on `model.generate_text` (`prompt`, `context`);
   "Email the support lead" on `email.send_real_guarded` (`to`, `body`, `subject`).
3. **Create the workflow.** **Agent task** `split` (agent `program_manager`, instructions the
   split brief, context `tickets` from input, producing `tickets`). A **For each** step `each`
   with items `steps.split.output.tickets`, body `reply`, max items 50, concurrency 2, then
   `digest`. **Agent task** `reply` (instructions asked at start as `reply_brief`, context
   `ticket` = **the item**, `position` = **the item's index**, `product` from input, producing
   `category`, `priority`, `draft`). `digest`: `prompt` the digest brief, `context` from
   `each → results`. `send`: `body` from `digest → text`, approval manual.

---

## Troubleshooting

| What you see | What it means |
|---|---|
| *the list had 73 items; max_items is 50* | Split the batch, or raise `max_items` on the step |
| One ticket's step run failed | The others finished; `results` has that position empty. Run again from `each` |
| A draft promises something | The brief forbids it; edit the draft in your help desk, and tighten `reply_brief` |

---

## Customize

Raise `concurrency` to triage faster, within your model's rate limits. Add `file` after the
digest with `tracker.issues.create` to turn the urgent ones into tickets — it stops for a person
too. When a help-desk connector exists, the first step changes and nothing else does.
