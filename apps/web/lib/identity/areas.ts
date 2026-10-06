/**
 * CONSOLE AREA -> ACCESS (docs/product/IDENTITY.md §6.5, D6; frozen mechanism, Phase 0).
 *
 * Every console area declares what opening it needs: the read permission of its primary resource
 * (D6: no separate page:* keys), or `authenticated` for the viewer's own pages. The shell shows an
 * area in the rail only when `can()` allows it, and renders <AccessDenied> for a direct URL to one
 * it does not (stream S4). Actions inside an area use <Can permission=…> with their own keys.
 *
 * Stream S2 fills this table (the draft is IDENTITY_PERMISSIONS.md §8). Until every area is
 * declared, tests/identity-contract.test.ts compares the undeclared ones with the shrinking list
 * in tests/identity/undeclared_console_areas.txt; when it is empty this becomes a total Record.
 */
import type { AreaId } from "../area-ids";
import type { Permission } from "./permissions.gen";

export type AreaAccess =
  | { readonly permission: Permission }
  | { readonly authenticated: true };

export const AREA_ACCESS: Readonly<Partial<Record<AreaId, AreaAccess>>> = {};
