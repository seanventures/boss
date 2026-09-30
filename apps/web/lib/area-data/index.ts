/**
 * BOSS-13: per-area console data loaders.
 *
 * Import from here in page.tsx instead of inlining 55 parallel fetches.
 */

export {
  loadAssuranceAreaData,
  shouldLoadAssuranceArea,
  ASSURANCE_AREA_IDS,
  type AssuranceAreaData,
} from "./assurance";
export {
  loadBudgetAreaData,
  shouldLoadBudgetArea,
  BUDGET_AREA_IDS,
  type BudgetAreaData,
} from "./budget";
export {
  loadInfraAreaData,
  shouldLoadInfraArea,
  INFRA_AREA_IDS,
  type InfraAreaData,
} from "./infra";
export { isAreaActive, type AreaLoadOptions } from "./types";
