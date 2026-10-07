/**
 * WHAT OPENING EACH CONSOLE AREA NEEDS — the console's reading of it. The authority is S2's
 * AREA_ACCESS (areas.ts), a TOTAL Record: every AreaId declares its access or nothing type-checks.
 * (The draft table that stood in for undeclared areas during integration is gone with the last
 * undeclared area.)
 */
import type { AreaId } from "../area-ids";
import { AREA_ACCESS, type AreaAccess } from "./areas";

export function accessForArea(id: AreaId): AreaAccess {
  return AREA_ACCESS[id];
}
