"use client";

/**
 * A build's cockpit (factory spec §4.5). Phase 0 stub: the interface the Floor's shell mounts.
 * S3 replaces the body; the props stay.
 */
import React from "react";

export type CockpitProps = {
  projectId: string;
  /** The build (code task run id) from `?build=`. */
  runId: string;
  actor: string;
  orgId?: string | null;
  /** `← Floor` / Esc: the shell clears `build=` and restores focus to the opening row. */
  onClose: () => void;
  /** Picking another attempt switches `build=`. */
  onOpenBuild: (runId: string) => void;
};

export function Cockpit({ runId, onClose }: CockpitProps) {
  return (
    <section className="fx-cockpit" data-testid="fx-cockpit" data-run={runId}>
      <button type="button" className="ag-link" onClick={onClose}>← Floor</button>
    </section>
  );
}
