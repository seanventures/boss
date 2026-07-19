# Design/product role + design-aware prompts (APP-2 / #92)

> Plane #92, part of the #90 first-time-output-quality epic. Spec `09 §3.2`.

## Why

BOSS's prompts are correctness-only: the implementer is told to make "the smallest correct change,"
and the reviewer is told anything out of the correctness scope is *not* a defect. So even a correct
page comes out bare (placeholder copy, no hierarchy), and the gate structurally cannot register "this
isn't coherent." This adds the missing **design & product-coherence axis** — but only where it applies.

## What it adds (`apps/api/app/services/design_prompts.py`)

- `is_ui_bearing(spec, title, test_files)` — a task touches the user-facing surface (project_kind
  web/spa, a `.tsx/.css/…` target file, or UI vocabulary). Back-end tasks are untouched — **additive**.
- `product_design_prompt(...)` — the new **product_design** role: one type scale / spacing / accent,
  clear hierarchy, responsive-by-default, compose the component kit, and the load-bearing move —
  **on-domain copy** (specific audience, value prop, process narrative), with lorem/placeholder forbidden.
- `design_impl_clause()` — appended to `_impl_prompt` for UI tasks: "smallest change" is scoped to
  *behavior*; the design system and copy are **not strippable**.
- `design_review_clause()` — appended to `_review_prompt` for UI changes: **incoherence is in scope**
  (placeholder copy, inconsistent type/spacing, missing responsive, broken hierarchy → REFUTED).

## Wiring

`_impl_prompt`/`_review_prompt` gain a `ui` flag (byte-identical when off — verified by test); the
governed pipeline computes `ui_task = is_ui_bearing(spec, title)` alongside `iac_task` and threads it
through. **11 tests** (`test_design_prompts.py`), including the byte-identical-when-off guarantee.

## Next

Add `product_design` as a first-class pre-implement stage (frontier-routed per APP-6) that themes the
APP-1 scaffold before the implementer fills it in; a severity threshold for the reviewer (block on
placeholder/broken render, warn on minor spacing — open decision #3 in the spec).
