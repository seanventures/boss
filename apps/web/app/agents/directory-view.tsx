"use client";

/**
 * Agents › Directory: every agent, to find and set up (agents-home spec §4.8).
 *
 * Grouped by team, in the order every Agents surface uses; the presence zones moved to Live.
 * Moved out of workforce-ui.tsx in Phase 0 so the Live stream (which owns the area
 * shell) and the Directory stream (which owns this grid) never edit one file. Testids are
 * preserved: wf-filter-*, roster-ungoverned, workforce-empty, and RosterZones' own.
 */
import React, { useEffect, useMemo, useState } from "react";

import type { V2Agent } from "../../lib/v2/workforce";
import type { LiveMap } from "../../lib/agents-live";
import { askAgent as talkTo, identityOf, openAgent } from "../../lib/agent-world";
import { openDockAbout } from "../../lib/dock-subject";
import { useIdentityVersion } from "../../lib/use-agent-identities";
import { FocusedAgents } from "../focused-agents-ui";
import { RosterZones } from "./roster-zones";

/** What "N unregistered" means, and what pressing it does. */
export const UNREGISTERED_TIP =
  "Doing work with no registered role, so nothing governs it: no trust score, no limits. Show them to register.";

function volumeOf(a: V2Agent): number {
  return (a.calls ?? 0) + (a.steps ?? 0);
}

/** Talk to this agent, 1:1, in its own voice (agents/talk-sheet.tsx). By role_key: the name
 *  is a display string, and two agents can share one. An agent with no registered role has no
 *  voice to talk in yet, so it keeps the dock, addressed by name. */
function askAgent(agent: Pick<V2Agent, "id" | "name" | "registered">): void {
  if (agent.registered) talkTo(agent.id, agent.name, { from: "roster" });
  else openDockAbout({ subject: null, role: agent.name, question: `@${agent.name}: ` });
}

/** `Needs you` left Directory for Live, which owns what is waiting (agents-home spec §3.3). */
export type DirectoryFilter = "all" | "worked" | "duty" | "retired" | "unregistered";

export type DirectoryViewProps = {
  projectId: string;
  actor: string;
  /** Every non-retired roster agent. */
  everyone: V2Agent[];
  retired: V2Agent[];
  live: LiveMap;
  /** The header's `Find an agent` text. */
  search: string;
  /** `true` for a blank agent; a name to register one that works with no registered role. */
  onCreate: (what: true | string) => void;
};

export function DirectoryView({ projectId, actor, everyone, retired, live, search, onCreate }: DirectoryViewProps) {
  const identities = useIdentityVersion();
  const [filter, setFilter] = useState<DirectoryFilter>("all");
  const agents = useMemo(() => {
    const scoped = filter === "worked" ? everyone.filter((a) => volumeOf(a) > 0)
      : filter === "retired" ? retired
      : filter === "unregistered" ? everyone.filter((a) => !a.registered) : everyone;
    const query = search.trim().toLowerCase();
    const who = (a: typeof scoped[number]) => {
      const id = identityOf(a.id, a.name);
      return `${a.name} ${a.role} ${a.task} ${id.callsign ?? ""} ${id.tagline ?? ""}`.toLowerCase();
    };
    return query ? scoped.filter((a) => who(a).includes(query)) : scoped;
  }, [everyone, retired, filter, search, identities]); // eslint-disable-line react-hooks/exhaustive-deps -- the store version

  // COUNTED, NOT ASSUMED: roles idle all window have recorded no work.
  const workedCount = everyone.filter((a) => volumeOf(a) > 0).length;
  const unregisteredCount = everyone.filter((agent) => !agent.registered).length;
  // Once the last one is registered, the "unregistered" view has nothing left to show.
  useEffect(() => {
    if (filter === "unregistered" && unregisteredCount === 0) setFilter("all");
  }, [filter, unregisteredCount]);

  return (
    <div className="roster ag-dir-view">
      <div className="roster-filterbar">
        <span className="roster-filters">
          {(["all", "duty", "worked", ...(retired.length ? ["retired" as const] : [])] as const).map((f) => (
            <button key={f} type="button" className="cap-btn" data-active={filter === f}
                    onClick={() => setFilter(f)} data-testid={`wf-filter-${f}`}
                    title={f === "duty" ? "Agents that watch and act on their own"
                      : f === "retired" ? "Kept for their work. Hidden from pickers." : undefined}>
              {f === "all" ? `All ${everyone.length}` : f === "worked" ? `Has done work ${workedCount}`
                : f === "retired" ? `Retired ${retired.length}` : "On duty"}
            </button>
          ))}
        </span>
        {/* Governance gap, said once: an agent running work without a registered role has
            no trust score, no autonomy level and no violation tracking. Pressed, it shows
            those agents, each with Register. */}
        {unregisteredCount > 0 ? (
          <button type="button" className="cap-btn roster-ungoverned" data-testid="roster-ungoverned"
                  data-active={filter === "unregistered"} title={UNREGISTERED_TIP}
                  onClick={() => setFilter(filter === "unregistered" ? "all" : "unregistered")}>
            {unregisteredCount} unregistered
          </button>
        ) : null}
      </div>

      {filter === "duty" ? (
        <FocusedAgents projectId={projectId} actor={actor} />
      ) : agents.length === 0 ? (
        everyone.length === 0 && filter === "all" && !search.trim() ? (
          <div className="roster-empty" data-testid="workforce-empty">
            <span className="detail">No agents yet</span>
            <button type="button" className="wf-btn" onClick={() => onCreate(true)}>+ New agent</button>
          </div>
        ) : <span className="detail" data-testid="roster-no-match">No match</span>
      ) : (
        // GROUPED BY TEAM (agents-home spec §4.8): what each agent is doing is Live's job.
        <RosterZones agents={agents} live={live} onOpen={openAgent} groupBy="team"
                     onAsk={(roleKey, name) => askAgent(
                       agents.find((a) => a.id === roleKey) ?? { id: roleKey, name, registered: true })}
                     onRegister={(name) => onCreate(name)} />
      )}
    </div>
  );
}
