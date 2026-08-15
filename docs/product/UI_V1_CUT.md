# Mission Control UI — Community v1 cut

**Not public GA.** Areas are not deleted. ⌘K and `?area=` still reach every
`AreaId`. This is a **primary-nav** cut so Community home is honest and small.

Source: `docs/product/V1_FEATURE_VERIFICATION.md` (UI simplification v1) and
the 2026-08-13 UI panel walk (`docs/operations/qa-2026-08-13-ui-panel.md`).

## v1 home (Community primary nav)

| Nav id | Label on Community | Why it stays |
|---|---|---|
| `overview` | Command Center | Landing / next actions |
| `activity` | Activity log | Searchable SoR |
| `work` | Jobs | Task board + live jobs |
| `decisions` | Approvals | Hold-default inbox |
| `reliability` | Reliability | Incidents / on-call / KG |
| `code_tasks` | Factory | Governed code tasks |
| `reports` | QA | Evidence + official scorecards |
| `budget` | Budget | Spend + reservations |
| `enterprise` | SSO | Community-included identity |

Paid editions keep the full sidebar.

## Hidden from Community primary nav (not deleted)

Everything else: Metatron, Studio, GitHub, System Configuration (infra),
Ops, Roster, pipeline, comms, sandboxes, discovery, meetings, delivery,
monitoring, assurance, design-system lab.

Deep links remain. Infra fabric / Cloud / Enterprise panels stay in the
tree so `?area=infra` and `?area=enterprise` still render.

## Gated / honest-empty panels (do not show raw 500)

| Surface | v1 treatment |
|---|---|
| Cloud Easy Connect (device/browser CTAs) | `PaidSurfaceGate` `account_connect` |
| Account Connect / marketplace / clusters | already gated (keep) |
| Elastic fabric | `status=not_configured` + empty copy; `host.connected` never invented |
| Goldeneye PaaS | same |
| Provider usage | empty ledger, not “API returned 500” |

Missing schema on the API is repaired with `ensure_tables` and a 200 empty
payload. The web client also maps 500/503 on those three routes to
`not_configured` so a drifted dogfood DB cannot paint a false red.

## Implementation

- Nav: `apps/web/lib/edition.ts` (`V1_COMMUNITY_PRIMARY_NAV`)
- Fabric honesty: `apps/web/lib/infra-fabric.ts`, `apps/web/app/infra-fabric-ui.tsx`
- Cloud CTA gate: `apps/web/app/page.tsx` (`data-testid="cloud-easy-connect"`)

Do not stamp Community GA from this cut.
