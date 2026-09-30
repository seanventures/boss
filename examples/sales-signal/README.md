# Sales signal

**Give it a company. Get a researched, scored, drafted approach — in your salesperson's inbox,
before anything goes near the prospect.**

---

## The use case

A name lands: a demo request, a pricing-page visit, somebody a colleague mentioned. Someone now
has to spend twenty minutes finding out who they are, whether they are worth pursuing, and what
to open with. Usually they spend five, and the email that goes out could have gone to anybody.

The research is mechanical. The judgement — is this worth our time, what do we actually say —
is not, which is why the assessment and the draft are separate steps and a person reads both.

### What happens

| # | Step | What it does |
|---|------|--------------|
| 1 | **Research** | BOSS's `research` capability — a **cited dossier**, gated on evidence |
| 2 | **Assess** | Fit score, why-now, risks, and **unknowns** |
| 3 | **Draft** | A note containing the assessment and a short opening email grounded in it |
| 4 | **Notify** | Emails your **salesperson**. **Stops and waits** |

**Nothing is sent to the prospect.** Step 4 emails your own salesperson the assessment and the
drafted approach for them to edit and send. A workflow that emails prospects unattended is a
workflow that will eventually email the wrong one.

### It starts by hand, not on a signal

The version everyone sketches begins *"a prospect visits the pricing page"*. That needs an
inbound webhook from whatever watches your site, and BOSS has no opinion about what that is.
So this starts from the **company**, which is what the signal would have carried anyway.

Wiring a real trigger later changes the first step and nothing else.

### What the briefs refuse to do

**It will not infer budget, headcount or intent** that is not in the research. A confident
guess is worse than a gap here, because the salesperson repeats it to the prospect.

**It reports `UNKNOWNS`** as a named section — what the research did not establish is the part
a scoring model usually paints over.

**The draft names the thing it refers to.** No flattery, no invented mutual connections, no
*"I hope this finds you well"*. If the research gives nothing specific to open with, the brief
says to write that rather than a generic email.

---

## Before you run it

- **A model** connected.
- **Email configured** — Settings → Email, or the `BOSS_EMAIL_*` environment variables.
  Step 4 refuses and names the missing setting rather than pretending to send.

---

## Running the setup script

```bash
python3 examples/sales-signal/setup.py --api http://127.0.0.1:8000
```

Standard parameters — see [examples/README.md](../README.md). `--dry-run` first; safe to re-run.

---

## Running it

**Delivery → Workflows → "Sales signal" → Start one.**

- `company` — *"Acme Corp, acme.com"*
- `salesperson` — the address that gets the summary
- `assess_brief` / `draft_brief` — leave blank for the defaults

The last step stops. Open it to read the assessment and the draft before either is used.

---

## Doing it by hand

**Skills → Enable:** `model.generate_text`, `email.send_real_guarded`

**Your steps →** define three: *Assess the opportunity* and *Draft the approach* (both
`model.generate_text`, input `prompt`), and *Tell the salesperson*
(`email.send_real_guarded`, inputs `to` and `body`).

**Create a new workflow →** drag **Research** from the built-in palette, then your three.

**Wire the inputs:**

| Step | Input | Source |
|---|---|---|
| Research | `seed` | run input → `company` |
| Assess | `prompt` | run input → `assess_brief`, or the stored scoring brief |
| Assess | `context` | **from an earlier step** → Research → `dossier` |
| Draft | `prompt` | run input → `draft_brief`, or the stored draft brief |
| Draft | `context` | **from an earlier step** → Assess → `text` |
| Notify | `to` | run input → `salesperson` |
| Notify | `body` | **from an earlier step** → Draft → `text` |

Set the agents to `program_manager`. Save.

---

## When something goes wrong

| What you see | What it means |
|---|---|
| The assessment is mostly `UNKNOWNS` | The research found little. That is the answer, not a failure — it means don't spend the twenty minutes |
| *"BOSS_EMAIL_SMTP_HOST is not configured"* | Set up email first |
| The draft says there is nothing specific to open with | The brief told it to say that rather than write a generic email |

---

## What ships with BOSS, and what does not

**BOSS ships** the `research` capability, `model.generate_text`, `email.send_real_guarded` and
the roles. **BOSS does not ship this workflow** — it is data in your project.
