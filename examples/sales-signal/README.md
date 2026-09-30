# Sales signal

**Give it a company name. Get a researched, scored, drafted approach in your salesperson's inbox —
before anything goes near the prospect.**

---

## What it does

A name lands: a demo request, a pricing-page visit, somebody a colleague mentioned. Someone now
has to find out who they are, whether they are worth pursuing, and what to open with. The
research is mechanical; the judgement — is this worth our time, and what do we actually say — is
not. So the assessment and the draft are separate steps, and a person reads both.

| # | Step | What it does |
|---|------|--------------|
| 1 | **Research** | BOSS's `research` capability — a **cited dossier**, gated on evidence |
| 2 | **Assess** | Fit score, why-now, risks, and **unknowns** |
| 3 | **Draft** | A note containing the assessment and a short opening email grounded in it |
| 4 | **Notify** | Emails your **salesperson**. **Stops and waits** |

The default briefs set three rules:

- **No inferred budget, headcount or intent** beyond what the research found. A confident guess
  is worse than a gap, because the salesperson repeats it to the prospect.
- **`UNKNOWNS` is a named section.** What the research did not establish is the part a scoring
  model usually paints over.
- **The draft names the specific thing it refers to.** No flattery, no invented mutual
  connections, no *"I hope this finds you well"*. If the research gives nothing specific to open
  with, the draft says so instead of producing a generic email.

---

## What you need

- **A connected model.**
- **Email configured** — Settings → Email, or the `BOSS_EMAIL_*` environment variables. Without
  it, step 4 refuses and names the missing setting.

---

## Run it

1. Set `BOSS_API_KEY` as described in [examples/README.md](../README.md).
2. Install the workflow. Start with `--dry-run`; re-running is safe. Standard parameters are
   listed in [examples/README.md](../README.md).

```bash
python3 examples/sales-signal/setup.py --api http://127.0.0.1:8000
```

3. Open **Delivery → Workflows → "Sales signal" → Start one** and fill in:
   - `company` — *"Acme Corp, acme.com"*
   - `salesperson` — the address that receives the summary
   - `assess_brief` / `draft_brief` — leave blank to use the defaults

---

## What you'll review

The last step stops for approval. Open it to read the assessment and the draft before either is
used.

---

## How it stays safe

**Nothing is sent to the prospect.** Step 4 emails your own salesperson the assessment and the
drafted approach, for them to edit and send. A workflow that emails prospects unattended will
eventually email the wrong one, so this one ends with a person.

---

## Build it by hand

1. **Skills → Enable:** `model.generate_text`, `email.send_real_guarded`
2. **Your steps →** define three: *Assess the opportunity* and *Draft the approach* (both
   `model.generate_text`, input `prompt`), and *Tell the salesperson*
   (`email.send_real_guarded`, inputs `to` and `body`).
3. **Create a new workflow →** drag **Research** from the built-in palette, then your three steps.
4. **Wire the inputs:**

   | Step | Input | Source |
   |---|---|---|
   | Research | `seed` | run input → `company` |
   | Assess | `prompt` | run input → `assess_brief`, or the stored scoring brief |
   | Assess | `context` | **from an earlier step** → Research → `dossier` |
   | Draft | `prompt` | run input → `draft_brief`, or the stored draft brief |
   | Draft | `context` | **from an earlier step** → Assess → `text` |
   | Notify | `to` | run input → `salesperson` |
   | Notify | `body` | **from an earlier step** → Draft → `text` |

5. Set the agents to `program_manager`, then save.

---

## Troubleshooting

| What you see | What it means |
|---|---|
| The assessment is mostly `UNKNOWNS` | The research found little. That is an answer, not a failure: it is not worth the time |
| *"BOSS_EMAIL_SMTP_HOST is not configured"* | Set up email first |
| The draft says there is nothing specific to open with | The brief asks for that instead of a generic email |

---

## Customize

**Change the briefs.** `assess_brief` and `draft_brief` override the stored defaults for a single
run.

**Start it from a real signal.** Most versions of this idea begin with *"a prospect visits the
pricing page"*. That needs an inbound webhook from whatever watches your site, and BOSS does not
prescribe one, so this example starts from the **company** — which is what the signal would have
carried anyway. Wiring a real trigger later changes the first step and nothing else.

**BOSS ships** the `research` capability, `model.generate_text`, `email.send_real_guarded` and
the roles. **The workflow itself is not part of BOSS** — it is data in your project.
