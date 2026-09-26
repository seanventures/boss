# Trend to content

**Give it a trend. Get an honest verdict on whether it is yours to talk about — and a script,
if it is.**

---

## The use case

Something is happening and everyone is posting about it. The question is not *"can we make a
video"* — you can always make a video. It is **"do we have anything to say that isn't what
everyone else is saying"**, and the honest answer is usually no.

So this does the research, then makes that call explicitly, and only writes a script if the
answer was yes.

### What happens

| # | Step | What it does |
|---|------|--------------|
| 1 | **Research** | Cited dossier on the trend |
| 2 | **Assess** | What it is, **ours or not**, the angle, how long it lasts |
| 3 | **Script** | A 45-second vertical script — written from the *assessment* |
| 4 | **Review** | Emails it to you. **Stops and waits** |

### The part that makes it useful

Step 3 reads the **assessment**, not the raw research. So a trend judged *"not ours"* produces
one honest line rather than a polished script for a video nobody should make.

The brief says so directly: *"Do not manufacture relevance. Most trends are not ours, and
saying so saves a video."* A tool that always produces a script is a tool that will talk you
into things.

### You supply the trend

The sketch everyone draws starts with **detect trend**, which needs a feed from X, TikTok or
Google Trends. BOSS connects to none of them, and pretending otherwise would make this a demo
of something that does not work.

Detection is also the part you probably want a human doing — you know which trends are yours.

### It chains into the video workflow

It ends at a **script**, deliberately. Paste that into
[video-workflow](../video-workflow/)'s editing instructions and the two connect: trend in here,
edited video out of there, both stopping for you.

---

## Before you run it

- **A model** connected.
- **Email configured** — Settings → Email, or the `BOSS_EMAIL_*` variables.

---

## Running the setup script

```bash
python3 examples/trend-to-content/setup.py --api http://YOUR_HOST:8000 --key "$BOSS_API_KEY"
```

`--dry-run` first; safe to re-run.

---

## Running it

**Delivery → Workflows → "Trend to content" → Start one.**

- `trend` — *"everyone is posting their AI coding setup"*
- `to` — who reads the script
- `assess_brief` / `script_brief` — leave blank for the defaults

---

## Doing it by hand

**Skills → Enable:** `model.generate_text`, `email.send_real_guarded`

**Your steps →** *Assess the trend* and *Write the script* (`model.generate_text`, input
`prompt`), and *Send it for review* (`email.send_real_guarded`, inputs `to`, `body`).

**Wire the inputs:**

| Step | Input | Source |
|---|---|---|
| Research | `seed` | run input → `trend` |
| Assess | `prompt` | run input → `assess_brief` |
| Assess | `context` | **from an earlier step** → Research → `dossier` |
| Script | `prompt` | run input → `script_brief` |
| Script | `context` | **from an earlier step** → **Assess** → `text` |
| Review | `to` | run input → `to` |
| Review | `body` | **from an earlier step** → Script → `text` |

That Script→`context`←**Assess** wiring is the point of the example. Pointing it at Research
instead gives you a script for every trend, which is the thing worth avoiding.

---

## When something goes wrong

| What you see | What it means |
|---|---|
| The script is one line saying there is no angle | Working as intended. The assessment said this is not yours |
| The assessment says "ours or not: not ours" | Also the answer. Most trends are not |
| *"BOSS_EMAIL_SMTP_HOST is not configured"* | Set up email first |

---

## What ships with BOSS, and what does not

**BOSS ships** the `research` capability, `model.generate_text` and `email.send_real_guarded`.
**BOSS does not ship this workflow** — it is data in your project.
