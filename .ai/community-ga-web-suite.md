# Community GA web suite

Worktree: `/Volumes/DataSSD1/boss-community-ga-integration/swarms/web-suite`
Branch: `ai/grok/ga-web-suite`
Base: `46c10ca957e8d4f01403c0ff6c6c290be44ae156`
Date: 2026-09-29

`apps/web/node_modules` was missing in the worktree. It was symlinked to `/Volumes/DataSSD1/boss-community-ga-integration/boss/apps/web/node_modules` and was not committed.

No product or test code changed. The suite was already green, including the three candidate areas below. Held deploy and autonomy routes were not enabled. The org chart was not restyled.

## Command

Cwd: `/Volumes/DataSSD1/boss-community-ga-integration/swarms/web-suite/apps/web`

```
pnpm test
```

Package script: `vitest run` (`boss-web@0.14.0`). Exit 0. Duration 7.64s.

## Counts

| | Passed | Failed | Skipped | Total |
|---|---:|---:|---:|---:|
| Test files | 212 | 0 | 1 | 213 |
| Tests | 2486 | 0 | 1 | 2487 |

The skipped file is `tests/_render-cards-preview.test.tsx` (1 skipped test). Remaining failures: none.

## Candidate areas

| Area | File | Result |
|---|---|---|
| Setup wizard; Claude setup-token import disabled | `tests/setup-wizard.test.ts` | 54 passed |
| Workforce / agent definitions; install does not start work | `tests/workforce-v1.test.tsx` | 21 passed |
| Delivery workflow schedule editing | `tests/delivery-workflows.test.tsx` (115) and `tests/your-workflows-have-their-own-tab.test.ts` (11) | 126 passed |

Nothing was pushed.
