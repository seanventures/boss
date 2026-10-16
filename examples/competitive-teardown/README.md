# Competitive teardown

**Name a competitor and say what you sell. BOSS researches both at once, compares them from
the evidence, and emails the teardown once you approve.**

---

## What it does

A teardown is a comparison, and a comparison is only as good as the weaker of its two sides.
This workflow researches the competitor and your own product to the same standard, in
parallel, then compares and writes.

| # | Step | Agent | What it does |
|---|------|-------|--------------|
| 1a | **them** | *research capability* | Web research on the competitor → a cited dossier |
| 1b | **us** | *research capability* | Web research on your product → a cited dossier |
| 2 | **Compare the two** | `program_manager` | Positioning, pricing, features each way, customers, recent moves, unknowns |
| 3 | **Write the teardown** | `program_manager` | The document a product team would read |
| 4 | **Email the teardown** | `program_manager` | Sends it to the reviewer. **Stops for you** |

1a and 1b are BOSS's own `research` capability — real web research, gated on evidence — running
**at the same time** under a `parallel` step with a `join`. The comparison reads one dossier as
its main text and the other as its material.

The comparison brief's rule: **every claim names its source, and a gap is "not established".** A
competitor with little public material produces a short teardown that says so, not a confident
one.

---

## What you need

**A connected model**, and the research capability's web access (see
[Skills & MCP](../../docs/community/MCP.md) for the web search connection). **Email configured**
for the last step.

---

## Run it

1. Set `BOSS_API_KEY` as described in [examples/README.md](../README.md).
2. Install the workflow. Start with `--dry-run`; re-running is safe.

```bash
python3 examples/competitive-teardown/setup.py \
    --api  http://127.0.0.1:8000 \
    --project YOUR_PROJECT \
    --actor   ceo
```

3. Open **Workflows → Library**, find "Competitive teardown", then **⋯ → Start** and fill in:
   - `competitor` — name and website
   - `ours` — your product: name, website, one line on what it does
   - `to` — who reviews the teardown
   - `compare_brief` — optional; blank uses the stored brief

---

## What you'll review

The last step stops. Open it to read the teardown before it goes anywhere:

```
## Summary
Acme Analytics sells to the same retailers at roughly half our list price (source: acme-
analytics.com/pricing, 2026-10-01). They lack scheduled exports and any audit log...

## Pricing
...
## Unknowns
Their enterprise tier price is not published — not established.
```

Two research runs cost more than one: expect this example to be the most expensive in the
library per run. The run's cost is on its page.

---

## How it stays safe

Research is **tier 1** and reads the web; the comparison and the writing are **tier 0**. The
email is **tier 2** and its own policy requires an approval. **Nothing is sent until a person
approves it.**

---

## Build it by hand

1. **Enable the skills.** `model.generate_text`, `docs.draft`, `email.send_real_guarded`; give
   them to `program_manager`.
2. **Define three steps.** "Compare the two" on `model.generate_text` (`prompt`, `context`,
   `system`); "Write the teardown" on `docs.draft` (`brief`, `context`, `audience`); "Email the
   teardown" on `email.send_real_guarded` (`to`, `body`, `subject`).
3. **Create the workflow.** A **Do at the same time** step `fork` with branches `them` and `us`, joined at
   `join`. Each branch is the built-in **Research** step with `seed` from the run's input
   (`competitor`, `ours`). A **Wait for branches** step waiting for both, then `compare` (`system` the brief,
   `prompt` from `them → dossier`, `context` from `us → dossier`), `write` (`context` from
   `compare → text`), `send` (`body` from `write → markdown`, approval manual).

---

## Troubleshooting

| What you see | What it means |
|---|---|
| A dossier is thin | Little public material, or web search is not connected. Thin is reported, not padded |
| The join never completes | One research run failed; open it. The join waits for both |
| *not ready yet: email* | Configure SMTP in Settings |

---

## Customize

Add a third branch researching the category as a whole. Change `compare_brief` per run to
focus on one dimension — pricing only, say. Swap the email for `slack.post_real_guarded` to post
the summary to a channel; it stops for a person too.
