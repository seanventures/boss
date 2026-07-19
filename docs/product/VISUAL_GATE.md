# Real visual/UX gate (APP-4 / #94)

> Plane #94, part of the #90 first-time-output-quality epic. Spec `09 §3.4`.

## Why

`_build_browser_validation_review` was a stub that always returned `required_not_run` — it never
rendered, screenshotted, or judged, and the release go/no-go didn't block on it. And the preview
"verify" passed on any 2xx even if the body was blank or a React error overlay. So the strongest "it
works" signal was "a socket answered."

## Decision (spec §6 #2)

**Deterministic checks now**; a vision-model rubric judge is the frontier-metered follow-on (APP-6).
Render capture is however it can be got — the **served HTML from the existing preview works today**; a
headless-browser (Playwright) capture with screenshots + a real mobile viewport is the injectable
follow-on (where that infra runs is the deferred sandbox decision). The rubric is pure and judges
whatever evidence it's given.

## What it does (`visual_gate.py`)

`evaluate_visual(evidence)` → `passed | failed | not_applicable` + findings, judging render evidence
(`{html, console_errors?, viewport?, route?, screenshot_ref?}`) against a rubric:
- renders **without an error overlay** (framework error boundary / 500 page / console errors),
- has **real, non-placeholder content** (not a blank body / bare root div; no lorem / "Welcome to our
  platform" / "Your title here"),
- has **visual hierarchy** (`<h1>` + sectioning),
- **no mobile overflow** (when a viewport measurement is available).

Hard failures block; a missing hierarchy is a finding but soft when `require_hierarchy=False` (spec
open-decision #3).

## Wiring

- `_build_browser_validation_review(files, render_evidence=?)` — with evidence it returns a **real**
  `passed|failed` with findings + evidence_refs; without, the honest `required_not_run` (additive).
- `_build_release_go_no_go` — a **`failed`** visual gate is now a **blocker** (`visual_gate_failed`);
  `required_not_run` stays a warning.
- Preview `_http_ok` — the primary-route smoke rejects a blank body or an error overlay on a 2xx (an
  SPA shell still passes; the full render rubric is the headless gate).
- `POST /v1/visual-gate {evidence}`.

**13 tests** (`test_visual_gate.py`): every rubric primitive, evaluate pass/fail/n-a incl. mobile
overflow + soft hierarchy, the real-gate-with-evidence wiring, and the release-blocking.

## Next

Wire a headless-browser capture (served HTML → real rendered DOM + screenshots + mobile viewport +
console errors) — the "where does Playwright run" sandbox decision; then the vision-model judge under
APP-6's frontier routing.
