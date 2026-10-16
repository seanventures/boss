# Pipeline digest

**Paste the pipeline export, read the digest, approve the email.**

---

## What it does

A sales lead wants the same five things every Monday: what moved, what stalled, what closes
this month, what is at risk, and the totals. Reading a 200-row export to find them is the work
nobody does well at 8 a.m. This workflow does the reading; you do the approving.

| # | Step | Agent | What it does |
|---|------|-------|--------------|
| 1 | **Write the digest** | `program_manager` | Export → digest, in numbers from the export. Sends nothing |
| 2 | **Email the digest** | `program_manager` | Sends it. **Stops and waits for you** |

The brief holds one rule worth knowing: **numbers come from the export**. A column that is not
there is reported as "not in the export", not estimated. A weighted pipeline figure appears only
if the export has the columns to compute it.

This is the simplest example in the library on purpose — two steps, one of them a stop. If you
are building your first workflow by hand, build this one.

---

## What you need

**A connected model.** Step 1 asks a model to write. Connect one in Settings.

**Email configured.** Step 2 sends through the project's SMTP settings. Without it, the step
refuses with a message that says so, and `Start` lists it as not ready.

**No CRM connector.** BOSS does not ship one, so the export is pasted rather than fetched. That is
also why this example is not scheduled: a weekly run working from last week's paste would be
a digest of stale data. When a CRM connector exists, the first step changes and nothing else
does.

---

## Run it

1. Set `BOSS_API_KEY` as described in [examples/README.md](../README.md).
2. Install the workflow. Start with `--dry-run`; re-running is safe.

```bash
python3 examples/pipeline-digest/setup.py \
    --api  http://127.0.0.1:8000 \
    --project YOUR_PROJECT \
    --actor   ceo
```

3. Open **Workflows → Library**, find "Pipeline digest", then **⋯ → Start** and fill in:
   - `pipeline` — the CRM export, pasted as CSV or a table
   - `focus` — optional: what matters this week
   - `to` — who receives the digest
   - `subject` — optional

---

## What you'll review

The send step stops. Open it to see the digest as it will be sent:

```
Pipeline: 42 open deals, $1.24M (weighted: not in the export)

Moved this week (3)
  · Northwind — Proposal → Negotiation (R. Patel)
  ...

Stalled 14+ days (5)
  ...

Closing this month (4)
  ...

At risk
  · Contoso — "waiting on legal since 3 Oct" (from the notes column)
```

Approve, and it is sent. Reject, and nothing leaves.

---

## How it stays safe

Step 1 is **risk tier 0**: it reads text and returns text. Step 2 is **tier 2** and its own
policy requires an approval, so it stops for a person whatever the workflow asks for.
**Nothing is emailed until a person approves it.**

---

## Build it by hand

The script only calls the public API; every step below does the same thing in the console.

1. **Enable the skills.** **Skills → Enable:** `model.generate_text`, `email.send_real_guarded`.

2. **Define two steps.** **Workflows → Library → Steps → + New step.**

   | Name | Skill | Inputs |
   |---|---|---|
   | Write the digest | `model.generate_text` | `prompt`, `context` (optional), `system` (optional) |
   | Email the digest | `email.send_real_guarded` | `to`, `body`, `subject` (optional) |

   The second step will show **always needs a person**.

3. **Create the workflow and wire it.** **Workflows → Library → + New workflow** → drag both
   steps on → open each step's **inputs**:

   | Step | Input | Source |
   |---|---|---|
   | Write the digest | `system` | **asked when the run starts** → `digest_brief`, default: the brief in `setup.py` |
   | Write the digest | `prompt` | **asked when the run starts** → `pipeline` |
   | Write the digest | `context` | **asked when the run starts** → `focus` *(optional)* |
   | Email the digest | `to` | **asked when the run starts** → `to` |
   | Email the digest | `subject` | **asked when the run starts** → `subject`, default "Weekly pipeline digest" |
   | Email the digest | `body` | **from an earlier step** → Write the digest → `text` |

   Set both agents to `program_manager`, then save.

---

## Troubleshooting

| What you see | What it means |
|---|---|
| *"skill 'x' is not registered in this project"* | Enable it in Skills first |
| *not ready yet: email* at install or Start | Configure SMTP in Settings |
| A digest full of "not in the export" | The export lacks those columns. Add them, or accept the gaps |

---

## Customize

Change `digest_brief` per run to reorder the sections or add one. Give a different agent the
two skills and set it on the steps. Swap the email step for `slack.post_real_guarded` to post the
digest to a channel instead — it is tier 2 as well, so it still stops for you.
