# First-Time Output Quality: Design & Product Coherence Axis

> **Summary:** BOSS is a *correctness* engine with no design/UX/product axis in its generation or verification loops. On a greenfield web-app request it clones an existing repo (no scaffolding), prompts the agent to make "the smallest correct change that passes tests," verifies with `python3 -m unittest`, and gates purely on correctness — so its first-time output feels naive and unfinished even when it is technically right. This spec adds a **design & product-coherence axis** end-to-end: framework-aware scaffolding, a design/product generation role, a web-appropriate verification target, a real visual/UX gate (implementing the existing `required_not_run` placeholder), whole-repo coherence via retrieval, and hybrid frontier+OSS routing so the expensive design pass is paid for only where it moves the needle.
>
> **User ask:** 7 (first-time output quality — be competitive with polsia.com)
> **Module:** Engine & Model Quality (VAG — Verify-Amplify-Govern)
> **Priority:** **P0-oss-blocker** — first impressions gate OSS adoption. The first `boss build me an X` a developer runs is the whole trial; if it returns a bare, coherent-only-by-accident page, they never come back.

---

## 1. Context & Goal

### 1.1 The competitor bar is *coherence*, not feature depth

The reference point is polsia.com. Their public demo (`fitdefence-ai.polsia.app`) generated a fitness-coaching site that, on inspection, is only ~60% of a "product": it is a single polished **landing page** with no auth, no dashboard, no payments, and placeholder FAQ content. It nonetheless *feels* impressive, and the reason is entirely **first-pass coherence**, not completeness:

- **On-domain, specific copy** — real price points (₹1,499/mo), a real audience ("Tier-2/3 cities", defence aspirants), and a real value proposition ("Your defence career starts with a stronger body"). Not lorem ipsum, not generic "Welcome to our platform."
- **A legible product narrative** — a clear numbered process (assess → plan → train → track) that reads as a coached journey.
- **Consistent visual hierarchy** — one type scale, one spacing rhythm, one accent color, a hero → features → process → CTA rhythm that every modern landing page shares.

None of that requires the app to *work end-to-end*. It requires the first pass to be **designed and written as a coherent whole**. That is precisely the axis BOSS has zero machinery for.

### 1.2 Reframe: BOSS optimizes an axis the user isn't judging on

BOSS's VAG frame — Verify a strong signal, Amplify with model effort, Govern the spend — is the right architecture. The problem is *what it verifies*. Every signal in the loop is a **correctness** signal (do the tests pass, did the reviewer find a concrete defect, is the change in scope). There is no signal for "does this look finished," "is the copy on-domain," "is the visual hierarchy consistent," "does it render." **Amplification cannot improve what it cannot measure** — pouring more model effort into a loop whose only gate is `unittest` green produces a more-correct bare page, never a more-coherent product.

### 1.3 Goal

Give BOSS a **design & product-coherence axis** that runs alongside the existing correctness axis, spanning all three VAG phases:

- **Verify** — add a web-native verification target (build + typecheck + lint + e2e) and a real visual/UX gate, so the loop has a design signal to optimize against.
- **Amplify** — add framework-aware scaffolding and a design/product generation role so a first pass *starts* as a coherent whole (design system + on-domain copy) rather than a minimal diff.
- **Govern** — route the expensive design/scaffold pass to a frontier model and the verified fill-in to cheap OSS, under the existing budget governor, so coherence is affordable per-task.

**Non-goal:** feature depth (auth, payments, dashboards) is explicitly *not* the target of this spec. The bar we are matching is first-pass design + copy coherence on the surface the user sees first — typically a landing/marketing page or the primary screen of an app.

---

## 2. Current State (ground truth — why output is naive today)

Every claim below is grounded in a file:line reference in the current tree.

### 2.1 No scaffolding — BOSS can only *edit an existing repo*

The code-task entrypoint requires a `project_id` and clones its existing repo; there is no path that generates a new project skeleton.

- `apps/api/app/main.py:4220` — `POST /v1/code-tasks` rejects a missing `project_id` (`422 "project_id is required."`) and immediately calls `start_code_task` against the existing repo.
- A tree-wide grep for `tailwind | next | vite | react | scaffold` returns **zero** hits in the engine. BOSS has no notion of a frontend framework, a design system, a component kit, or a package manifest. It has never generated a `package.json`, a file tree, or a `tailwind.config`.

Consequence: on a greenfield "build me a fitness site" request there is no baseline to make coherent — the agent is editing whatever happened to be in the seed repo, one small diff at a time.

### 2.2 Design-blind generation prompts

The agent is explicitly told to minimize, not to design.

- `apps/api/app/services/code_agent.py:150` — RULES: *"Make the smallest correct change that satisfies the criteria."*
- The role prompts across the pipeline are **correctness / spec / security** only:
  - `governed_code_task_service.py:110` (`_qa_prompt`) — author tests, cover happy path / failure / boundary / validation.
  - `governed_code_task_service.py:126` (`_impl_prompt`) — make QA's tests pass, smallest change.
  - `governed_code_task_service.py:171` (`_review_prompt`) — adversarial *correctness* reviewer.
- There is no role whose job is typography, spacing, responsive layout, a component kit, or on-domain copy. Nothing in any prompt mentions visual hierarchy, brand voice, price points, or audience.

### 2.3 The reviewer is *told* to suppress "looks unfinished"

The adversarial reviewer is instructed that anything not in the correctness scope is explicitly **not a defect**:

- `governed_code_task_service.py:176-182` — *"A concern that is OUT OF SCOPE — a feature that was not asked for, or a spec stricter than what was agreed — is NOT a defect; note it but do not fail the change for it."* Verdict is `REFUTED` only for a *concrete correctness defect*.

So even if a reviewer *notices* the page looks bare, the framework tells it to pass the change. The gate structurally cannot register "this is not coherent/finished."

### 2.4 Verification is Python-unittest, not web-native

- `governed_code_task_service.py:50` — `_DEFAULT_TEST_CMD = "python3 -m unittest discover -v"`; hardcoded again at `:799` and `:804`.
- There is no `npm run build`, no `tsc --noEmit`, no `eslint`, no Playwright/e2e path. A React/Next/Vite app's *actual* health signals (does it compile, does it typecheck, does it lint, does it render) are never run.

### 2.5 The visual/UX gate is a hardcoded placeholder

The one hook that could carry a design signal is a stub that always returns "not run":

- `apps/api/app/services/software_factory.py:486` — `_build_browser_validation_review()` selects browser-relevant files (`apps/web/`, `.tsx/.jsx/.css/.scss`) and, if any exist, returns:
  ```python
  {"status": "required_not_run",
   "required_follow_up": ["Capture browser validation evidence before approving a UI-affecting merge."]}
  ```
  It never renders anything, never screenshots, never judges. The release go/no-go (`_build_release_go_no_go` at `:509`) receives this `required_not_run` and does not block on it.

### 2.6 Preview "verify" passes on any HTTP response — even 4xx

- `apps/api/app/services/preview_service.py:66` — `_http_ok(port)` catches `HTTPError` and returns `True` with the comment *"any HTTP response (even 4xx) means the app is serving."* A 404 or a 500 error page counts as a healthy preview.

So the strongest "it works" signal a preview gives is "a socket answered." A blank white page, a React error overlay, or an unstyled DOM all pass.

### 2.7 Whole-app coherence is broken by the context cap

- `code_agent.py:353` — `_repo_context(cwd, limit=40)` lists files but only inlines the contents of files under **20 KB** (`fp.stat().st_size < 20000`, `:365`) and truncates to the first **40 blobs** (`blobs[:limit]`, `:370`).

For a multi-file frontend (design tokens in one file, layout in another, components in a dozen more), the agent never sees the whole design system at once. Coherence — one type scale, one spacing rhythm, one component vocabulary applied everywhere — is exactly the thing a 40-file/20 KB window prevents.

### 2.8 Summary: the VAG frame is right, the axis is missing

| VAG phase | Correctness axis (exists) | Design/product axis (missing) |
|---|---|---|
| **Verify** | `unittest` green; adversarial correctness review; security scan | build/typecheck/lint/e2e; visual render; copy-on-domain check |
| **Amplify** | repair loop re-issues impl prompt with failing evidence | (nothing — no scaffold, no design role, no design repair) |
| **Govern** | budget governor reserves/settles per stage | (no design-vs-fill routing) |

---

## 3. Target Design & Approach

Six moves, ordered by leverage. APP-1 (scaffolding) is by far the biggest lever; everything else compounds on it.

### 3.1 Framework-aware scaffolding (the single biggest lever)

Before the agent edits anything, BOSS must be able to **stand up a coherent baseline project**: a framework choice, a real file tree, a package manifest, and a design-system foundation.

- **New `scaffold` step** in the code-task flow, triggered when the task targets a greenfield web app (no existing frontend detected, or `project_kind: web` on the request). It emits, deterministically (templated, not model-freehanded):
  - a framework skeleton — default **Next.js (App Router) + TypeScript**, alternative **Vite + React + TS** for pure SPAs;
  - **Tailwind + shadcn/ui** wired in (`tailwind.config`, `globals.css` with a token layer, `components.json`, a starter set of shadcn primitives);
  - a **design-system baseline**: a single type scale, a spacing scale, a color/accent token set, radius/shadow tokens — one source of truth the design role then themes to the domain;
  - a `package.json` with the web verification scripts (`build`, `typecheck`, `lint`, `test:e2e`) already defined (feeds §3.3);
  - a minimal but real file tree (`app/layout.tsx`, `app/page.tsx`, `components/ui/*`, `lib/`).
- **Where it lives:** a new `services/scaffold_service.py` plus templates under `packages/specpack/scaffolds/` (mirrors the existing `packages/specpack/skills/*.yaml` convention). Wire into `start_code_task` (`code_task_service.py`) and the governed entry (`governed_code_task_service.py`) as an optional pre-stage that runs *before* the first agent role.
- **Why templated, not model-generated:** a scaffold must be *consistent and buildable every time*. The model's job is to theme and populate it (§3.2), not to reinvent the boilerplate. This also makes the baseline free (no frontier tokens) and makes the build/lint target (§3.3) knowable in advance.

### 3.2 Design/product generation role + design-aware prompts

Add a first-class **design/product role** to the pipeline, and make the implement prompt design-aware when the task is UI-bearing.

- **New role `product_design`** (alongside qa/impl/review in `governed_code_task_service.py`). Its prompt covers:
  - **Visual system:** apply the scaffold's type scale / spacing / accent consistently; establish visual hierarchy (hero → sections → CTA); responsive by default (mobile-first breakpoints).
  - **Component kit usage:** compose from the shadcn primitives already scaffolded rather than hand-rolling divs — enforces coherence for free.
  - **On-domain copy:** the load-bearing move. The prompt must demand *specific, audience-aware* copy — concrete price points, named audience segments, a real value proposition and process narrative — and explicitly **forbid** placeholder/lorem/generic copy. This is what makes polsia's output *feel* real; it is a prompt-and-role change, not a hard technical problem.
- **Amend the implement prompt** (`code_agent.py:150`, `governed_code_task_service.py:126`) so that for UI-bearing tasks the "smallest correct change" rule is scoped to *behavior*, not *presentation* — the agent must still satisfy the design system and copy requirements, not strip them to a minimal diff.
- **Amend the reviewer** (`governed_code_task_service.py:176`) so that for UI-bearing changes, **incoherence is in scope**: missing responsive behavior, inconsistent type/spacing, placeholder copy, and broken visual hierarchy become reportable defects rather than suppressed nice-to-haves. This is the counterpart to §3.4 — the human-language reviewer *and* the automated visual gate both get a design axis.

### 3.3 Web-appropriate verification target

Replace the Python-unittest default with a **project-type-aware** verification target so the loop optimizes the right signals.

- Introduce a **verification profile** resolved from the project (extends the pattern already in `pipeline_profiles.py`). For a web/TS project the default command set becomes:
  - `npm run build` (or `pnpm build`) — must compile;
  - `tsc --noEmit` — must typecheck;
  - `eslint` — must lint clean (or under a threshold);
  - `playwright test` — a minimal e2e that at least loads the primary route and asserts real content is present.
- Thread the resolved command set through the places that currently hardcode `python3 -m unittest discover -v`: `governed_code_task_service.py:50`, `:799`, `:804`. The repair loop (`_repair_prompt`, `governed_code_task_service.py:141`) then re-issues with *build/typecheck/e2e* failure evidence, giving Amplify a web-native signal to repair against.
- Keep Python unittest as the default for Python projects — this is additive, profile-selected, not a rip-and-replace.

### 3.4 A real visual/UX gate (implement the `required_not_run` placeholder)

Turn `_build_browser_validation_review` (`software_factory.py:486`) from a stub into an actual gate, and make preview verify stricter.

- **Render → capture → judge.** For UI-bearing changes, start the preview (reuse `preview_service.py`), navigate the primary route(s) with a headless browser (Playwright), and **screenshot** them.
- **Judge with a rubric.** Two implementable options, not mutually exclusive:
  1. **Vision-model judge** — send the screenshot(s) to a vision-capable model with a coherence rubric (visual hierarchy present? consistent type/spacing? on-domain non-placeholder copy? renders without error overlay? responsive at mobile width?) and get a pass/fail + findings.
  2. **Deterministic render checks** — assert the DOM has real content (not a blank body / React error boundary), no console errors, primary CTA present, mobile viewport doesn't overflow.
- **Return a real status** — replace the hardcoded `required_not_run` with `passed | failed | not_applicable` carrying screenshot `evidence_refs` and findings. Feed the *actual* status into `_build_release_go_no_go` (`software_factory.py:509`) so a failed visual gate **blocks** the release go/no-go for UI-affecting merges.
- **Tighten preview verify.** Change `_http_ok` (`preview_service.py:66`) so a 4xx/5xx no longer counts as healthy for the smoke path — a preview is "serving" only on a 2xx that returns non-empty rendered content. (Keep a permissive mode for intentionally-authed routes, but the default primary-route smoke must see real 2xx content.)

### 3.5 Whole-repo coherence via retrieval

Replace the fixed 40-file / 20 KB window (`code_agent.py:353,365,370`) with retrieval so the agent can hold the whole design system in view.

- Build a **repo context retriever**: index the project (design tokens, `tailwind.config`, `globals.css`, component library, layout files, prior pages) and, for a given task, retrieve the *relevant* slice — always including the design-system source of truth and the component kit surface, regardless of file count.
- Prioritization for UI tasks: design tokens + component index are **always** in context (they define coherence); page/feature files are retrieved by relevance.
- Raise the hard caps as a floor and make the *content* selection retrieval-driven rather than "first 40 files under 20 KB by sorted path." (Ties to the RAG/retrieval gap noted in the broader OSS-readiness plan.)

### 3.6 Hybrid frontier + OSS routing for the design axis

Spend frontier tokens where design coherence is decided; spend cheap OSS tokens on verified fill-in.

- **Frontier pass:** scaffold theming (§3.1) + the `product_design` role (§3.2) + the visual-gate vision judge (§3.4) run on a **frontier** model — this is where taste, hierarchy, and on-domain copy are decided, and where model quality is most visible.
- **OSS fill-in:** once the design system and page structure exist and the verification target is green-able, the *implement/repair* iterations run on a **cheap OSS** model, because the bar is now "make the frozen design + tests pass," which is a correctness task the existing VAG loop already handles well.
- **Wire to the VAG economic controller / budget governor** (already threaded through `software_factory.py:41,45` and reserved/settled per stage in `governed_code_task_service.py`). The design pass is a distinct, budgeted line item; the controller can cap how many frontier design passes a task gets before falling back. This is the Govern side of adding the design axis — coherence becomes an affordable, metered cost rather than "run the whole loop on a frontier model."

---

## 4. Implementation Tickets

> Labels `APP-1..APP-6`. Sizes are relative engineering effort (S ≈ 1–2 days, M ≈ 3–5 days, L ≈ 1–2 weeks).

### APP-1 — Framework-aware scaffolding service *(the biggest lever)*
**Scope:** Add a templated scaffold pre-stage that stands up a Next.js/Vite + TS + Tailwind + shadcn project with a design-system baseline and web verification scripts, before any agent role runs.
**Key files:** new `apps/api/app/services/scaffold_service.py`; new `packages/specpack/scaffolds/{nextjs,vite}/…` templates; wire into `code_task_service.py` (`start_code_task`) and `governed_code_task_service.py`; relax the greenfield rejection at `main.py:4220` to allow a project-less/`project_kind:web` scaffold path.
**Acceptance criteria:**
- [ ] A greenfield web request produces a buildable project: `package.json`, file tree, `tailwind.config`, `globals.css` token layer, `components.json`, starter shadcn primitives.
- [ ] `package.json` defines `build`, `typecheck`, `lint`, `test:e2e` scripts (consumed by APP-3).
- [ ] The scaffold is deterministic/templated (same input → same skeleton), no frontier tokens spent.
- [ ] Design-system baseline present: one type scale, one spacing scale, accent/color + radius/shadow tokens as a single source of truth.
- [ ] `npm install && npm run build` succeeds on the raw scaffold with zero agent edits.
**Size:** L

### APP-2 — Design/product role + design-aware prompts
**Scope:** Add a `product_design` role and make implement/review prompts design-aware for UI-bearing tasks; forbid placeholder copy, require on-domain specificity.
**Key files:** `governed_code_task_service.py` (new `_product_design_prompt`; amend `_impl_prompt:126` and `_review_prompt:171`); `code_agent.py:150` (implement RULES).
**Acceptance criteria:**
- [ ] New `product_design` role prompt covers visual hierarchy, type/spacing consistency, responsive-by-default, shadcn component-kit usage, and on-domain copy.
- [ ] Copy requirement explicitly forbids lorem/placeholder/generic copy and demands audience-specific specifics (price points, segments, value prop, process narrative).
- [ ] Implement prompt's "smallest correct change" is scoped to behavior; presentation/design-system requirements are not strippable.
- [ ] Reviewer treats incoherence (placeholder copy, inconsistent type/spacing, missing responsive, broken hierarchy) as an in-scope defect for UI changes — no longer suppressed by the `:176` out-of-scope rule.
**Size:** M

### APP-3 — Web-appropriate verification target
**Scope:** Make the verification command set project-type-aware; use build+typecheck+lint+e2e for web projects instead of the hardcoded `unittest` default.
**Key files:** `pipeline_profiles.py` (verification profile); `governed_code_task_service.py:50,799,804` (replace hardcoded `_DEFAULT_TEST_CMD`); `_repair_prompt:141` (carry web failure evidence).
**Acceptance criteria:**
- [ ] Verification profile resolves from project type; web/TS → `build`, `tsc --noEmit`, `eslint`, `playwright test`.
- [ ] Python projects still default to `python3 -m unittest discover -v` (additive, not a replacement).
- [ ] Repair loop re-issues with build/typecheck/e2e failure evidence for web tasks.
- [ ] A minimal e2e loads the primary route and asserts real content (not just a 200).
**Size:** M

### APP-4 — Real visual/UX gate + stricter preview verify
**Scope:** Implement `_build_browser_validation_review` as render→screenshot→rubric (vision judge and/or deterministic render checks); feed its real status into the release go/no-go; make preview verify reject empty/4xx as "healthy."
**Key files:** `software_factory.py:486` (`_build_browser_validation_review`), `:509` (`_build_release_go_no_go`); `preview_service.py:66` (`_http_ok`).
**Acceptance criteria:**
- [ ] For UI-bearing changes the gate starts the preview, navigates primary route(s) headless, and captures screenshot evidence.
- [ ] Returns `passed | failed | not_applicable` with screenshot `evidence_refs` and findings — never a hardcoded `required_not_run`.
- [ ] Rubric checks: renders without error overlay, real (non-placeholder) content present, consistent visual hierarchy, responsive at mobile width.
- [ ] A `failed` visual gate blocks `_build_release_go_no_go` for UI-affecting merges.
- [ ] `_http_ok` primary-route smoke passes only on 2xx with non-empty rendered content; 4xx/5xx no longer counts as healthy by default.
**Size:** L

### APP-5 — Whole-repo coherence via retrieval
**Scope:** Replace the 40-file/20 KB context window with a retriever that always surfaces the design system + component kit and pulls relevant files by relevance.
**Key files:** `code_agent.py:353` (`_repo_context`, and the `:365` size gate / `:370` `[:limit]` truncation); new retriever module.
**Acceptance criteria:**
- [ ] Design tokens, `tailwind.config`, `globals.css`, and the component index are **always** in context for UI tasks regardless of file count.
- [ ] Non-design files are selected by relevance to the task, not first-40-by-path.
- [ ] The 40-file/20 KB hard caps are removed as the selection mechanism (may remain as a large safety ceiling).
**Size:** M

### APP-6 — Hybrid frontier+OSS routing for the design axis
**Scope:** Route scaffold-theming + `product_design` role + visual-gate vision judge to a frontier model; route implement/repair fill-in to cheap OSS; meter the design pass as a distinct budgeted line item in the VAG governor.
**Key files:** `governed_code_task_service.py` (per-role model routing — the pipeline already runs builder vs adversary on different models); `software_factory.py:41,45` (budget governor threading); `pipeline_profiles.py` (model routing profile).
**Acceptance criteria:**
- [ ] Design/scaffold/visual-judge stages run on a frontier model; implement/repair iterations run on a cheap OSS model.
- [ ] The design pass is a distinct budgeted line item; the budget governor can cap frontier design passes per task and fall back when the cap is hit.
- [ ] Routing is profile-driven and overridable per project/task.
**Size:** M

---

## 5. Dependencies, Sequencing & Priority

```
APP-1 (scaffold) ──► APP-2 (design role/prompts) ──► APP-4 (visual gate)
     │                     │                              ▲
     └──► APP-3 (web verify) ─────────────────────────────┘
APP-5 (retrieval)  ── independent, compounds APP-2
APP-6 (routing)    ── after APP-1 + APP-2 exist (needs a design pass to route)
```

- **APP-1 is the critical path and the highest leverage.** Nothing else matters if BOSS can't produce a coherent baseline; a design role with no scaffold to theme is a role editing a random seed repo. Do it first.
- **APP-3 can start in parallel with APP-1** (it depends only on knowing the project type and having web scripts, which APP-1 defines) — pair them.
- **APP-2 depends on APP-1** (the design role themes the scaffold's tokens and composes its component kit).
- **APP-4 depends on APP-1 + APP-3** (needs a buildable, preview-able app to render and a preview that reports honestly). This is what closes the loop — it gives Verify a real design signal.
- **APP-5 is independent** and can land anytime; it compounds APP-2's coherence.
- **APP-6 is last** — it optimizes cost once a working frontier design pass (APP-1/APP-2) and a real gate (APP-4) exist to route around.

**Recommended order:** APP-1 + APP-3 (parallel) → APP-2 → APP-4 → APP-5 → APP-6.

**Priority rationale:** P0-oss-blocker. The first build a developer runs is the entire trial. Correctness-only output that renders bare loses the user before they ever see BOSS's real strength (the VAG correctness loop). Even APP-1 + APP-2 alone — a scaffolded, design-themed, on-domain first pass — would move BOSS from "naive" to "polsia-competitive on coherence," which is the actual bar.

---

## 6. Open Decisions

1. **Framework default.** Next.js App Router vs Vite+React as the primary scaffold. Next covers landing→app growth (SSR, routing) but is heavier; Vite is leaner for pure SPAs. Proposal: default Next, offer Vite for `project_kind: spa`. *Owner: engine.*
2. **Visual judge: vision-model vs deterministic-only (APP-4).** A vision-model rubric judge is more aligned with "does it *look* finished" but adds a frontier vision call per UI gate and is non-deterministic. Deterministic render checks are cheap and repeatable but can't judge taste. Proposal: deterministic checks always + vision judge on the frontier design pass only (metered by APP-6). *Owner: engine + cost.*
3. **How aggressively should the reviewer fail on incoherence (APP-2)?** Failing on every design nit risks stalling the loop; too lenient reproduces today's suppression. Need a severity threshold (block on placeholder copy / broken render; warn on minor spacing). *Owner: quality.*
4. **Scaffold provenance / licensing.** shadcn/ui is copy-in (MIT) — confirm the templated primitives and their licenses are OSS-clean for the engine's own open-source split. *Owner: OSS packaging.*
5. **Retrieval backend for APP-5.** Reuse an existing index/embedding path or add a dedicated code retriever? Ties into the broader RAG gap in the OSS-readiness plan — decide whether APP-5 seeds that or waits on it. *Owner: engine.*
6. **Copy specificity without hallucinated facts.** On-domain copy (₹1,499/mo, named segments) is what makes output *feel* real, but BOSS shouldn't invent binding claims (real prices, guarantees). Decide the boundary: plausible illustrative copy clearly marked as placeholder-for-real-values vs. requiring the user to supply domain facts up front. *Owner: product.*
7. **Preview verify strictness vs authed routes (APP-4).** Tightening `_http_ok` to require 2xx+content could false-fail apps whose primary route is intentionally behind auth. Need a per-task hint for "primary public route" vs "authed." *Owner: engine.*
