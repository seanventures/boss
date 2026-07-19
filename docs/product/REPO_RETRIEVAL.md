# Whole-repo coherence via retrieval (APP-5 / #95)

> Plane #95, part of the #90 first-time-output-quality epic. Spec `09 §3.5`.

## Why

The agent's repo context was "the first 40 files under 20 KB, by sorted path" — and frontend
extensions (`.tsx/.css/…`) weren't even whitelisted. For a multi-file frontend (design tokens in one
file, layout in another, a dozen components) the agent never saw the whole design system at once, so
coherence — one type scale, one spacing rhythm, one component vocabulary — was exactly what the fixed
window prevented.

## What changed

`apps/api/app/services/repo_retriever.py` replaces path-order truncation with retrieval:
- **Always pin the coherence source of truth** — `is_design_file` (tailwind config, `globals.css`/
  `index.css`, `components.json`, `components/ui/*`, `lib/utils`) is in context for a UI task
  *regardless of file count*.
- **Rank the rest by relevance** (`score_relevance`: lexical overlap on path — weighted — and content),
  not by path order.
- The 40/20 KB caps become a **large safety ceiling** (`max_files=120`, `max_bytes`, per-file head
  truncation), not the selection mechanism.

`_repo_context` (code_agent) now: whitelists frontend exts, skips vendor/build dirs (`node_modules`,
`dist`, …), pins design files (+ Terraform core via the existing predicate), ranks the rest by the
task prompt, and renders design/pinned files first.

## Tests

**8 unit tests** (`test_repo_retriever.py`): design detection, path-weighted relevance, design-pinning
past the cap, relevance ordering, byte/file ceiling + head-truncation, the Terraform pin predicate,
render labeling, and the wiring import. Governed-microvm regression green.

## Next

Model-free lexical retrieval is a deterministic first cut; a semantic/embedding backend (the broader
RAG gap) is the follow-on. A component *index* (symbol → file) would sharpen "which component to reuse."
