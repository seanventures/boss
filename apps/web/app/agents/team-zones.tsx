"use client";

/**
 * Teams as soft zones: Live › Agent force (compact) and Organization › Zones (full), one
 * component at two densities (agents-home spec §4.6). PHASE 0 STUB — stream S3 owns it; the
 * props are the frozen interface Live (S2) mounts.
 */
import React from "react";

import type { LiveBoard } from "../../lib/agents-live-board";

export type TeamZonesProps = {
  projectId: string;
  board: LiveBoard;
  density: "compact" | "full";
  /** The header's `Find an agent` text: non-matching faces dim to 30%. */
  search?: string;
  /** Organization mission focus (`?run=`): that run's agents ring, others dim. */
  focusRunId?: string | null;
  onClearFocus?: () => void;
  /** A team focused from Live's `Open map ↗` (`?focus=<team>`). */
  focusTeam?: string | null;
  /** Compact only: `Open map ↗`, optionally with a team to focus. */
  onOpenMap?: (teamKey?: string) => void;
};

export function TeamZones(_props: TeamZonesProps): React.ReactElement | null {
  return null;
}
