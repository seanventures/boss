# Framework-aware scaffolding (APP-1 / #91)

> Plane #91, part of the #90 first-time-output-quality epic. Spec:
> `docs/planning/oss-readiness/09-first-time-output-quality.md` §3.1.

## Why

BOSS could only *edit an existing repo* one small diff at a time, so a greenfield "build me an X"
started from whatever seed was there — the root cause of naive first-time output. This is the biggest
lever: before any agent role runs, stand up a **coherent, buildable baseline** the design role (APP-2)
then themes to the domain.

## What it produces

`scaffold_service.scaffold_project(name, kind)` returns a deterministic `{files, manifest}` — a
**Vite + React + TS + Tailwind** project with:
- a **design-system token layer** (`src/index.css` + `tailwind.config.ts`): one type scale, one
  spacing rhythm, one accent, radius/shadow tokens, light+dark — the single coherence source of truth;
- **shadcn-style primitives** (`button`, `card`, `cn` util — copy-in MIT) so the design role composes
  a kit rather than hand-rolling divs;
- a starter page that already *uses* the kit in a hero→features rhythm, with the copy flagged
  `TODO(APP-2)` so the design role fills in on-domain, non-placeholder text;
- a `package.json` with the **web verification scripts** APP-3 consumes: `build` (`tsc --noEmit &&
  vite build`), `typecheck`, `lint`, `test:e2e` (Playwright), plus a minimal e2e asserting real content.

**Templated, not model-generated** — consistent and buildable every time, zero frontier tokens, and it
makes the build/lint target knowable in advance.

## Verified

`npm install && npm run build` succeeds on the raw scaffold with **zero agent edits** (verified on
Node 23: `tsc --noEmit` clean under strict + no-unused, `vite build` → real bundle). 10 unit tests
(`test_scaffold_service.py`): determinism, required files, the 4 verification scripts, the token layer,
kit usage, valid-JSON configs, manifest, kind aliases, name sanitization, endpoint.

`POST /v1/scaffold {name, kind: vite|spa}`.

## Next

`nextjs` (App Router) variant; wire the scaffold as a pre-stage in `start_code_task` /
`governed_code_task_service` for `project_kind: web` greenfield requests (relax the `project_id`
requirement at `main.py`); move templates to `packages/specpack/scaffolds/` per the specpack convention.
