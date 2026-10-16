# PRD from customer interviews

**Paste the interview notes. BOSS finds the themes, says whether there is enough evidence,
and drafts a PRD only if there is — emailed to a reviewer once you approve.**

---

## What it does

Interview notes become a requirements document in two steps that are usually one: pulling out
what people actually said, and deciding whether it adds up to something worth specifying. This
workflow keeps them apart, and lets the second one say no.

| # | Step | Agent | What it does |
|---|------|-------|--------------|
| 1 | **Find the themes** | `program_manager` | Notes → themes, in the customers' words, with counts. First line: `EVIDENCE: sufficient` or `EVIDENCE: insufficient` |
| 2 | **route** | — | A `condition`: does the themes text contain `EVIDENCE: insufficient`? Yes → step 3. No → step 4 |
| 3 | **Email the reviewer** (gaps) | `program_manager` | Sends the themes and what is missing. **Stops for you** |
| 4 | **Draft the PRD** | `program_manager` | Themes → PRD; every requirement traceable to a theme |
| 5 | **Email the reviewer** (PRD) | `program_manager` | Sends the PRD. **Stops for you** |

**The honest no is the point.** Three interviews that say different things do not justify a
PRD, and one written anyway would read as though they did. The branch reads the first line of
the themes step's own output, so you can see at the top of the step why the run went the way
it did.

The PRD is written **from the themes, not the raw notes**, so a requirement can be traced to
something a customer said — and a requirement with no theme behind it does not belong.

---

## What you need

**A connected model**, and **email configured** for the last step.

---

## Run it

1. Set `BOSS_API_KEY` as described in [examples/README.md](../README.md).
2. Install the workflow. Start with `--dry-run`; re-running is safe.

```bash
python3 examples/prd-from-interviews/setup.py \
    --api  http://127.0.0.1:8000 \
    --project YOUR_PROJECT \
    --actor   ceo
```

3. Open **Workflows → Library**, find "PRD from customer interviews", then **⋯ → Start**:
   - `notes` — the interview notes, pasted; mark where one interview ends and the next begins
   - `area` — what the PRD is about
   - `to` — who reviews it
   - `themes_brief`, `prd_brief` — optional; blank uses the stored briefs

---

## What you'll review

With enough evidence, the PRD:

```
# Invoice export

## Problem
Four of five interviews described re-keying invoices into their accountant's system by hand
("I copy each line into a spreadsheet on Friday" — interview 2) ...

## What we will build
1. Export invoices for a date range as CSV (themes 1, 3)
...
## What we will not build
- A direct integration with any accounting package (one mention; not established)
```

Without enough evidence, the themes and the gaps instead:

```
EVIDENCE: insufficient
Two interviews mention exports; one wants PDF, one wants CSV, and neither said how often.
Missing: frequency, destination system, who in the customer's team does it.
Next interviews should ask: ...
```

---

## How it stays safe

Steps 1 and 4 read and write text: **tier 0**. Both email steps are **tier 2** and their policy
requires an approval. **Nothing is sent until a person approves it.** The path not taken is
recorded on the run as skipped.

---

## Build it by hand

1. **Enable the skills.** `model.generate_text`, `docs.draft`, `email.send_real_guarded`; give
   them to `program_manager`.
2. **Define three steps.** "Find the themes" on `model.generate_text` (`prompt`, `context`,
   `system`); "Draft the PRD" on `docs.draft` (`brief`, `context`, `audience`); "Email the
   reviewer" on `email.send_real_guarded` (`to`, `body`, `subject`).
3. **Create the workflow.** `themes`: `system` from input `themes_brief` (default in
   `setup.py`), `prompt` from input `area`, `context` from input `notes`. A **Branch** `route`:
   `steps.themes.output.text` **contains** `EVIDENCE: insufficient` → `tell_gap` (body from
   `themes → text`), otherwise `prd` (`brief` from input `prd_brief`, `context` from
   `themes → text`) → `send` (body from `prd → markdown`). Both emails manual.

---

## Troubleshooting

| What you see | What it means |
|---|---|
| The run went to `tell_gap` and you disagree | Read the gaps it names; add interviews, or edit `themes_brief` to lower the bar deliberately |
| The PRD names a requirement you cannot find in the notes | Reject the email and start again; the brief forbids it, and the themes step's text shows what it had |
| *not ready yet: email* | Configure SMTP in Settings |

---

## Customize

Chain it: approve the PRD, then paste it into **meeting-to-tickets** as the transcript to turn
its requirements into tracked work. Or add a `decision` gate after `prd` with the question
"Is this the PRD we want to send?" for a review before the email step.
