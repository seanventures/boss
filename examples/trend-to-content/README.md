# Trend to content

**Give it a trend. Get an honest verdict on whether it is yours to talk about — and a script, if
it is.**

---

## What it does

Something is happening and everyone is posting about it. The useful question is not *"can we
make a video"* — you always can. It is **"do we have anything to say that isn't what everyone
else is saying"**, and the honest answer is usually no. This workflow does the research, makes
that call explicitly, and writes a script only if the answer is yes.

| # | Step | What it does |
|---|------|--------------|
| 1 | **Research** | Cited dossier on the trend |
| 2 | **Assess** | What it is, **ours or not**, the angle, how long it lasts |
| 3 | **Script** | A 45-second vertical script — written from the *assessment* |
| 4 | **Review** | Emails it to you. **Stops and waits** |

Step 3 reads the **assessment**, not the raw research. A trend judged *"not ours"* therefore
produces one honest line rather than a polished script for a video nobody should make. The
default brief says so directly: *"Do not manufacture relevance. Most trends are not ours, and
saying so saves a video."*

---

## What you need

- **A connected model.**
- **Email configured** — Settings → Email, or the `BOSS_EMAIL_*` variables.

---

## Run it

1. Set `BOSS_API_KEY` as described in [examples/README.md](../README.md).
2. Install the workflow. Start with `--dry-run`; re-running is safe.

```bash
python3 examples/trend-to-content/setup.py --api http://127.0.0.1:8000
```

3. Open **Delivery → Workflows → "Trend to content" → Start one** and fill in:
   - `trend` — *"everyone is posting their AI coding setup"*
   - `to` — who reads the script
   - `assess_brief` / `script_brief` — leave blank to use the defaults

---

## What you'll review

The review step emails you the script — or, for a trend that is not yours, the single line saying
so — and stops for your approval.

---

## How it stays safe

The workflow ends with a **script** in your inbox, and the final step waits for a person. Nothing
is published; turning the script into a video is a separate workflow with its own approval.

---

## Build it by hand

1. **Skills → Enable:** `model.generate_text`, `email.send_real_guarded`
2. **Your steps →** *Assess the trend* and *Write the script* (`model.generate_text`, input
   `prompt`), and *Send it for review* (`email.send_real_guarded`, inputs `to`, `body`).
3. **Wire the inputs:**

   | Step | Input | Source |
   |---|---|---|
   | Research | `seed` | run input → `trend` |
   | Assess | `prompt` | run input → `assess_brief` |
   | Assess | `context` | **from an earlier step** → Research → `dossier` |
   | Script | `prompt` | run input → `script_brief` |
   | Script | `context` | **from an earlier step** → **Assess** → `text` |
   | Review | `to` | run input → `to` |
   | Review | `body` | **from an earlier step** → Script → `text` |

The Script → `context` ← **Assess** wiring is the point of the example. Pointing it at Research
instead gives you a script for every trend, which is exactly what this workflow is meant to avoid.

---

## Troubleshooting

| What you see | What it means |
|---|---|
| The script is one line saying there is no angle | Working as intended. The assessment said this trend is not yours |
| The assessment says "ours or not: not ours" | Also an answer. Most trends are not |
| *"BOSS_EMAIL_SMTP_HOST is not configured"* | Set up email first |

---

## Customize

**Chain it into the video workflow.** The output is a script, deliberately. Paste it into
[video-workflow](../video-workflow/)'s editing instructions and the two connect: a trend goes in
here, an edited video comes out there, and both stop for you.

**Supply the trend yourself.** Automatic trend detection would need a feed from X, TikTok or
Google Trends, and BOSS connects to none of them. Detection is also the part you probably want a
person doing, since you know which trends are yours.

**BOSS ships** the `research` capability, `model.generate_text` and `email.send_real_guarded`.
**The workflow itself is not part of BOSS** — it is data in your project.
