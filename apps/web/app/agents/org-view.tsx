"use client";

/**
 * Agents › Organization: `Zones | Lines | Swarm` (agents-home spec §4.6).
 *
 *   Zones  — teams as soft regions (TeamZones, full density): one agent's edges at a time, or
 *            one run's path (`?run=`). The default.
 *   Lines  — today's OrgChartArea (Planned roles / Actual handoffs), passed in by the shell and
 *            unchanged: it stays the only editor of reporting lines.
 *   Swarm  — today's TeamSwarm, including `Replay last run`. Hidden under 640px, where a radial
 *            graph is unreadable; Zones is a list there.
 */
import React, { useEffect, useState } from "react";

import type { LiveBoard } from "../../lib/agents-live-board";
import type { LiveMap } from "../../lib/agents-live";
import type { OrgChart } from "../../lib/v2/workforce";
import { TeamSwarm } from "./team-swarm";
import { TeamZones, useNarrow } from "./team-zones";

export type OrgLayout = "zones" | "lines" | "swarm";

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
  /** OPTIONAL (S3): the signed-in actor, for Zones' `Move to…`. */
  actor?: string;
  /** OPTIONAL (S3): refresh the board after a move. */
  onChanged?: () => void;
};

export function OrgView({ projectId, chart, board, live, search, focusRunId, focusTeam, onClearFocus, lines,
                          actor, onChanged }: OrgViewProps): React.ReactElement | null {
  const [layout, setLayout] = useState<OrgLayout>("zones");
  const narrow = useNarrow();
  // A run or team focus is a Zones thing: arriving with one shows Zones.
  useEffect(() => { if (focusRunId || focusTeam) setLayout("zones"); }, [focusRunId, focusTeam]);
  useEffect(() => { if (narrow && layout === "swarm") setLayout("zones"); }, [narrow, layout]);

  const options: { key: OrgLayout; label: string; tip: string }[] = [
    { key: "zones", label: "Zones", tip: "Teams, and who hands work to whom" },
    { key: "lines", label: "Lines", tip: "Planned reporting lines. Doesn't change who runs a step." },
    ...(narrow ? [] : [{ key: "swarm" as const, label: "Swarm", tip: "Who handed work to whom, live" }]),
  ];
  return (
    <div className="ag-org" data-testid="ag-org" data-layout={layout}>
      <div className="ag-org-bar">
        <span className="ag-seg" role="group" aria-label="Layout">
          {options.map((o) => (
            <button key={o.key} type="button" aria-pressed={layout === o.key} data-testid={`ag-org-${o.key}`}
                    title={o.tip} onClick={() => setLayout(o.key)}>{o.label}</button>
          ))}
        </span>
      </div>
      {layout === "lines" ? lines
        : layout === "swarm" ? (chart ? <TeamSwarm projectId={projectId} chart={chart} live={live} />
          : <div className="ag-empty"><span>Team unavailable</span></div>)
        : board ? (
          <TeamZones projectId={projectId} board={board} density="full" search={search} focusRunId={focusRunId}
                     onClearFocus={onClearFocus} focusTeam={focusTeam} live={live} actor={actor} onChanged={onChanged} />
        ) : (
          <div className="ag-zones" data-testid="ag-zones" aria-busy="true">
            <span className="ag-sr">Loading</span>
            <div className="ag-zones-skeleton" aria-hidden />
          </div>
        )}
    </div>
  );
}
