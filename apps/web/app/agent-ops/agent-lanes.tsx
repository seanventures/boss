"use client";

/**
 * AGENTS AT WORK › the last 24 hours, per area agent (moved here from the first draft of Govern ›
 * Where people decide; its parity table keeps the row). Each area agent is a card: what it did
 * ALONE (teal) against what WAITED for a person (amber), as one split bar, and its 24-hour lane,
 * hour by hour. A class whose evidence earned a raise says so, linking to More › Per-area autonomy
 * (a person accepts it there; an agent never raises itself). Reads GET /v1/decide/activity.
 */
import React from "react";

import { useConsoleLive } from "../../lib/console-live";
import { autonomyPath, parseAutonomy, polled } from "../../lib/ambient";
import { activityPath, parseActivity, roleWords, type AgentRow } from "../../lib/decide";
import { usePolledRead } from "../../lib/log-stream";
import { AgentAvatar } from "../agent-avatar";
import "../govern/decide.css";

function Lane({ lane }: { lane: Array<[number, number]> }) {
  const max = Math.max(1, ...lane.map(([a, w]) => a + w));
  return (
    <span className="wd-lane" role="img" aria-label="Last 24 hours, hour by hour">
      {lane.map(([alone, waited], i) => {
        const k = waited ? "waited" : alone ? "alone" : undefined;
        return <i key={i} data-k={k} style={{ height: `${Math.max(2, Math.round(((alone + waited) / max) * 18))}px` }}
                  title={`${24 - i} h ago: ${alone} alone, ${waited} waited`} />;
      })}
    </span>
  );
}

function AgentCard({ row, raise }: { row: AgentRow; raise: string[] }) {
  const total = row.alone + row.waited;
  const pct = total ? Math.round((row.alone / total) * 100) : 0;
  const href = (() => {
    if (typeof window === "undefined") return "";
    const q = new URLSearchParams();
    const org = new URLSearchParams(window.location.search).get("org");
    if (org) q.set("org", org);
    q.set("area", "budget"); q.set("tab", "areas");
    return `/?${q.toString()}`;
  })();
  return (
    <div className="wd-agent" data-testid="wd-agent" data-role={row.role}>
      <span className="wd-agent-h">
        <AgentAvatar agent={row.role} size={24} aiBadge={false} tip={roleWords(row.role)} />
        <b>{roleWords(row.role)}</b>
      </span>
      <span className="wd-split" role="img" aria-label={`${row.alone} alone, ${row.waited} waited for a person`}>
        {total ? <><i data-k="alone" style={{ width: `${pct}%` }} /><i data-k="waited" style={{ width: `${100 - pct}%` }} /></> : null}
      </span>
      <span className="wd-agent-n">
        <span><b className="wd-teal">{row.alone}</b> alone</span>
        <span><b className="wd-amber">{row.waited}</b> waited</span>
        {raise.length ? <a className="wd-link" href={href || undefined} data-testid="wd-agent-raise"
                           title={`Evidence earned a raise: ${raise.join(", ")}. A person accepts it.`}>↑ raise suggested</a> : null}
      </span>
      <Lane lane={row.lane.length ? row.lane : Array.from({ length: 24 }, () => [0, 0] as [number, number])} />
    </div>
  );
}

export function AgentLanes({ projectId }: { projectId: string }) {
  const live = useConsoleLive();
  const act = usePolledRead({ key: activityPath(projectId), read: polled(activityPath(projectId), parseActivity), pollMs: 60_000, paused: !live, enabled: !!projectId });
  const auto = usePolledRead({ key: autonomyPath(projectId), read: polled(autonomyPath(projectId), parseAutonomy), pollMs: 120_000, paused: !live, enabled: !!projectId });
  const agents = act.data?.agents ?? [];
  if (!act.data) return null;
  const raise: Record<string, string[]> = {};
  for (const c of auto.data?.classes ?? []) {
    if (c.suggested_level && c.owner_role) (raise[c.owner_role] ??= []).push(`${c.title} → ${c.suggested_level}`);
  }
  const alone = agents.reduce((n, a) => n + a.alone, 0);
  const waited = agents.reduce((n, a) => n + a.waited, 0);
  return (
    <section className="wd" data-testid="wd-lanes" aria-label="What each agent did in the last 24 hours">
      <div className="wd-cap">Last 24 hours · each agent
        <span className="wd-legend wd-cap-sub"><span><i style={{ background: "var(--teal)" }} />{alone} alone</span>
          <span><i style={{ background: "var(--amber)" }} />{waited} waited for a person</span></span></div>
      <div className="wd-agents">
        {agents.map((a) => <AgentCard key={a.role} row={a} raise={raise[a.role] ?? []} />)}
      </div>
    </section>
  );
}
