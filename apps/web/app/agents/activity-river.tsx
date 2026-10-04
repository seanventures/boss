"use client";

/**
 * Agents › Activity: a readable river of agent work, tool calls folded under each item
 * (agents-home spec §4.9). PHASE 0 STUB — stream S3 owns it.
 */
import React from "react";

export type ActivityRiverProps = {
  projectId: string;
  /** The header's `Find an agent` text, resolved to a role key when it names one. */
  roleKey?: string | null;
  /** `?run=` focus: chip `<run> ×`. */
  runId?: string | null;
  onClearRun?: () => void;
  /** The console's Live/Paused toggle: no polling while paused. */
  paused?: boolean;
};

export function ActivityRiver(_props: ActivityRiverProps): React.ReactElement | null {
  return null;
}
