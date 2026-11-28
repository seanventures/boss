# App Map Console Confirm / Edit Card — Wireframe & Interaction Spec (BOSSAPP-556)

## Goal

Give operators one clear, scannable card on **Factory › Apps › \<app\> › Components** that shows
where BOSS believes the application lives, how BOSS builds/tests/runs it, where that map came
from, whether the base commit passes baseline verification, and whether repository manifests have
drifted since confirmation.

## Layout Wireframe

```text
┌──────────────────────────────────────────────────────────────────────────────┐
│  App Map  [Status Pill: Confirmed | Needs confirmation | Stale | Ambiguous]  │
│  Provenance: Workspace graph (pnpm-workspaces) · Fingerprint: a1b2c3d4       │
│                                            [Re-scan] [Edit] [Confirm Map]    │
├──────────────────────────────────────────────────────────────────────────────┤
│  (Optional Drift Banner when status == stale)                                │
│  ⚠ Manifests changed (package.json). Review changes and re-confirm App Map.  │
├──────────────────────────────────────────────────────────────────────────────┤
│  (Optional Ambiguous Picker when scan finds multiple apps & none confirmed)  │
│  Multiple apps found — choose the folder for this app:                       │
│   (•) apps/web       Node / Next.js    [pnpm install · pnpm test]            │
│   ( ) apps/api       Node / Express    [pnpm install · pnpm test]            │
│                                                  [Confirm selected app]      │
├──────────────────────────────────────────────────────────────────────────────┤
│  Root path:       apps/web             Workspace root:  .                    │
│  Stack:           node / next (pnpm)   Shared paths:    packages/ui          │
│  Install cmd:     pnpm install --frozen-lockfile                             │
│  Build cmd:       pnpm run build                                             │
│  Test cmd:        pnpm test                                                  │
│  Run cmd:         pnpm run start       Health:          /health              │
│  Manifests:       apps/web/package.json, pnpm-workspace.yaml, turbo.json     │
├──────────────────────────────────────────────────────────────────────────────┤
│  Baseline on base commit  [Passed · abc1234 · 1.2s]   [Run baseline verify]  │
└──────────────────────────────────────────────────────────────────────────────┘
```

## States

1. **Unmapped (`status: "unmapped"`)**: Shows refusal notice explaining BOSS will never guess the
   repository root, with **Scan repository** and **Configure manually** actions.
2. **Proposed (`status: "proposed"`)**: Shows scanned proposal, provenance badge, detected commands,
   and primary **Confirm App Map** button (`PUT /v1/apps/{app_key}/app-map`).
3. **Ambiguous (`status: "ambiguous"`)**: Lists every detected candidate app with its `root_path`
   and stack; selecting a radio row populates the card and lets the operator confirm.
4. **Confirmed (`status: "confirmed"`)**: Green status pill, confirmation metadata, **Re-scan**,
   **Edit**, and **Run baseline verify** (`POST /v1/apps/{app_key}/app-map/baseline`).
5. **Stale (`status: "stale"`)**: Amber drift banner naming changed manifests + **Re-scan** and
   **Re-confirm App Map**.
6. **Edit mode**: Inline form for `root_path`, `workspace_root`, `install`/`build`/`test` commands,
   `run` command, and comma-separated `shared_paths`, saved via `useGuardedCall` +
   `GatedButton permission="repository:manage"`.

## Verification

- Deterministic React + Vitest UI test suite in `apps/web/tests/app-map-card.test.tsx` covering
  unmapped, proposed -> confirm, ambiguous candidate selection, stale drift alert, inline edit,
  and baseline pass/fail rendering.
- TypeScript check: `npm run lint` (`next typegen && tsc --noEmit`).
