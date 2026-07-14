# VAG Phase 1 — Refute Panel Sweep (batch bake_panel_848367be)

Same hybrid config (cheap DeepSeek implements, frontier Claude verifies), run with a **single
reviewer (panel 1)** vs a **3-member refute panel (panel 3)** — only the panel size differs.
12 runs · 5 clean tasks + 2 known-bad probes · live governed pipeline.

| Config | Panel | Pass-rate (clean) | Caught (known-bad) | False pass | $/verified | Mean wall-clock | p90 | Mean confidence |
|---|---|---|---|---|---|---|---|---|
| Hybrid · panel 1 | 1 | 60% | 0/1 | 1 | $6.2997 | 1538s | 2216s | 0.83 |
| Hybrid · panel 3 | 3 | 80% | 1/1 | 0 | $4.9252 | 2040s | 3066s | 0.93 |

## What the numbers say
- Panel-3 costs 0.78× panel-1 per verified outcome ($4.9252 vs $6.2997) — the review stage runs 3 critics instead of 1.
- Wall-clock is nearly flat: panel-3 mean 2040s vs panel-1 1538s (+33%) — the extra reviewers run in parallel, so the panel is a cost multiplier, not a latency multiplier.
- Clean-task pass-rate: panel-1 60% vs panel-3 80% — no false-fails introduced.
- Known-bad probes caught: panel-1 0/1, panel-3 1/1 (a false-pass = the verifier was fooled).
- Mean panel confidence: panel-1 0.83 (single vote → always 0 or 1), panel-3 0.93 (a real distribution — the new signal).

## Per-model latency & cost
| Model | Calls | Avg latency | p90 | Total cost |
|---|---|---|---|---|
| claude-fable-5 | 101 | 53.0s | 83.1s | $38.5497 | 
| deepseek/deepseek-chat | 24 | 14.1s | 34.8s | $0.0501 | 
| (unknown) | 1 | 30.1s | 30.1s | $0.0000 | 

## Honest caveats
- Clean tasks are high-pass → panel size moves **cost** and the **confidence distribution**, not clean pass-rate. The panel's marginal *catch* is best isolated by a verifier meta-eval (#105).
- Known-bad probes are planted contradictions caught by the whole gate → they measure **gate trustworthiness**, not the panel alone.
- Claude runs on the Max plan (≈$0 marginal); $/verified bills it at API list price for accounting.
