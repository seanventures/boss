/**
 * BOSS-13 area loader: Assurance Center + security scanning.
 *
 * Covers ?area=assurance panels.
 */

import {
  fetchAssuranceFeed,
  type AssuranceFeed,
} from "../assurance";
import {
  fetchLatestSecurityScan,
  fetchSecurityScanners,
  type SecurityScanRun,
  type SecurityScanner,
} from "../security-scan";
import { isAreaActive, type AreaLoadOptions } from "./types";

export const ASSURANCE_AREA_IDS = ["assurance"] as const;

export type AssuranceAreaData = {
  assuranceFeed: AssuranceFeed;
  securityScanners: SecurityScanner[];
  latestSecurityScan: SecurityScanRun;
  loaded: boolean;
};

function emptyAssurance(projectId: string): AssuranceFeed {
  return {
    projectId,
    apiReachable: false,
    overview: {
      project_id: projectId,
      metrics: [],
      cases: [],
      surfaces: [],
      verification_runs: [],
      settled_conclusions: [],
    },
    modelUsage: {
      project_id: projectId,
      window_days: 30,
      totals: {},
      items: [],
    },
  };
}

export function shouldLoadAssuranceArea(
  requestedArea: string | undefined,
): boolean {
  return isAreaActive(requestedArea, ASSURANCE_AREA_IDS);
}

export async function loadAssuranceAreaData(
  options: AreaLoadOptions,
): Promise<AssuranceAreaData> {
  const { projectId, active = true } = options;
  if (!active) {
    return {
      assuranceFeed: emptyAssurance(projectId),
      securityScanners: [],
      latestSecurityScan: null,
      loaded: false,
    };
  }

  const [assuranceFeed, securityScanners, latestSecurityScan] =
    await Promise.all([
      fetchAssuranceFeed(projectId),
      fetchSecurityScanners(),
      fetchLatestSecurityScan(projectId),
    ]);

  return {
    assuranceFeed,
    securityScanners,
    latestSecurityScan,
    loaded: true,
  };
}
