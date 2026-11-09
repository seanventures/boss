"use client";

import React, { useCallback, useEffect, useRef, useState } from "react";

import type { InFlightItem } from "../lib/metrics";
import type { AgentRosterRow } from "../lib/roster";
import { AreaLink } from "./area-link";
import { useOnScreen } from "../lib/area-shown";

type Snapshot = {
  roster: AgentRosterRow[];
  inFlight: InFlightItem[];
  observedAt: string;
};

function AgentRow({ agent, orgId }: { agent: AgentRosterRow; orgId?: string }) {
  return <details className="cc-agent-row">
    <summary>
      <span className="cc-agent-name">{agent.name}</span>
      <span className={`cc-agent-state ${agent.state}`}>{agent.state}</span>
    </summary>
    <div className="cc-agent-detail">
      <p>{agent.currentTask}</p>
      {agent.currentTaskId ? <p className="cc-agent-meta">
        {agent.currentTaskStatus ?? "unknown"}
        {agent.currentTaskOwner ? `, owned by ${agent.currentTaskOwner}` : ""}
        {agent.currentTaskUpdatedAt ? `, updated ${agent.currentTaskUpdatedAt.slice(0, 16).replace("T", " ")} UTC` : ""}
      </p> : null}
      {agent.currentTaskId ? <a href={`/?${orgId ? `org=${encodeURIComponent(orgId)}&` : ""}area=work&task=${encodeURIComponent(agent.currentTaskId)}`}>
        Open task 
      </a> : null}
    </div>
  </details>;
}

export function CoordinationPanel({ projectId, orgId, initial }: {
  projectId: string;
  orgId?: string;
  initial: Snapshot;
}) {
  const [snapshot, setSnapshot] = useState(initial);
  const [error, setError] = useState<string | null>(null);
  const pending = useRef(false);

  const refresh = useCallback(async () => {
    if (pending.current || document.hidden) return;
    pending.current = true;
    try {
      const response = await fetch(
        `/api/coordination?project_id=${encodeURIComponent(projectId)}`,
        { cache: "no-store" },
      );
      if (!response.ok) throw new Error(`API returned ${response.status}`);
      setSnapshot(await response.json() as Snapshot);
      setError(null);
    } catch (cause) {
      setError(String((cause as Error)?.message ?? cause));
    } finally {
      pending.current = false;
    }
  }, [projectId]);

  const onScreen = useOnScreen();   // off screen, nothing is asked (lib/area-shown.ts)
  useEffect(() => {
    if (!onScreen) return undefined;
    const timer = window.setInterval(() => { void refresh(); }, 5_000);
    const onVisible = () => { if (!document.hidden) void refresh(); };
    document.addEventListener("visibilitychange", onVisible);
    return () => {
      window.clearInterval(timer);
      document.removeEventListener("visibilitychange", onVisible);
    };
  }, [refresh, onScreen]);

  const activeCount = snapshot.roster.filter((agent) => agent.state === "running").length;
  const activeAgents = snapshot.roster.filter((agent) => agent.state !== "idle");
  const idleAgents = snapshot.roster.filter((agent) => agent.state === "idle");
  return <div className="cc-grid" data-testid="coordination-panel">
    <article className="cc-coord" aria-label="Agent assignments">
      <div className="cc-panel-head">
        <strong>Agent assignments</strong>
        <span className="cc-panel-meta">{activeCount} running</span>
      </div>
      <div className="cc-coord-list">
        {activeAgents.map((agent) => <AgentRow agent={agent} orgId={orgId} key={agent.id} />)}
        {activeAgents.length === 0 && snapshot.roster.length > 0 ? <p className="cc-agent-idle-note">No agents working right now.</p> : null}
        {idleAgents.length > 0 ? <details className="cc-idle-list">
          <summary>Available agents ({idleAgents.length})</summary>
          {idleAgents.map((agent) => <AgentRow agent={agent} orgId={orgId} key={agent.id} />)}
        </details> : null}
        {snapshot.roster.length === 0 ? <p className="empty-state">No agents assigned.</p> : null}
      </div>
    </article>
    <article className="cc-feed" aria-label="Recent work">
      <div className="cc-panel-head">
        <strong>Recent work</strong>
        <span className="cc-feed-meta">last 3 days</span>
      </div>
      <div className="cc-feed-list">
        {snapshot.inFlight.map((item) => item.taskId ? <a className="cc-flight-row"
          href={`/?${orgId ? `org=${encodeURIComponent(orgId)}&` : ""}area=work&task=${encodeURIComponent(item.taskId)}`}
          key={item.taskId} title={`Open task: ${item.title}`}>
          <span className="cc-flight-main">
            <span className="cc-flight-title">{item.title || item.taskId}</span>
            <span className="cc-flight-sub">{item.stage}, {item.status}
              {item.owner ? `, ${item.owner}` : ""}
              {item.updatedAt ? `, updated ${item.updatedAt.slice(0, 16).replace("T", " ")} UTC` :
                item.createdAt ? `, started ${item.createdAt.slice(0, 16).replace("T", " ")} UTC` : ""}</span>
          </span>
                  </a> : <AreaLink className="cc-flight-row" area={item.area} orgId={orgId}
          key={item.title} title={item.title}>{item.title}</AreaLink>)}
        {snapshot.inFlight.length === 0 ? <p className="empty-state">No recent work.</p> : null}
      </div>
    </article>
    <div className="cc-coord-freshness" role="status">
      {error ? `Updates unavailable (${error}); showing last check` : "Checked"} {snapshot.observedAt.slice(11, 16)} UTC
      <button type="button" onClick={() => void refresh()}>Refresh</button>
    </div>
  </div>;
}
