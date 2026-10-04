"use client";

/**
 * Agents › Organization: `Zones | Lines | Swarm` (agents-home spec §4.6). PHASE 0 STUB —
 * stream S3 owns it. `lines` is today's OrgChartArea, rendered by the area shell and passed in
 * so the editor stays where it is; Swarm is today's TeamSwarm.
 */
import React from "react";

import type { LiveBoard } from "../../lib/agents-live-board";
import type { LiveMap } from "../../lib/agents-live";
import type { OrgChart } from "../../lib/v2/workforce";

export type OrgViewProps = {
  projectId: string;
  chart?: OrgChart;
  board: LiveBoard | null;
  live: LiveMap;
  search?: string;
  /** `?run=` mission focus and `?focus=<team>`, read from the URL by the shell. */
  focusRunId?: string | null;
  focusTeam?: string | null;
  onClearFocus?: () => void;
  /** Today's OrgChartArea (Planned roles / Actual handoffs), unchanged. */
  lines: React.ReactNode;
};

export function OrgView(_props: OrgViewProps): React.ReactElement | null {
  return null;
}
