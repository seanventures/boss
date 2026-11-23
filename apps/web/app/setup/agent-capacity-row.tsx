"use client";

import React, { useEffect, useState } from "react";
import { GatedButton } from "../../components/gated";
import { setupWrite } from "../../lib/setup-client";
import { toast, useBoard } from "./parts";

type Capacity = {
  worker_slots: number;
  builds_at_once: { value: number; auto: boolean; pinned: number | null; warning: string | null };
  next_build: { admitted: boolean; shortfall: string | null };
};

/** Why the limits could not be read, and what to do: never a bare "unavailable". */
export function capacityReadError(status: number): string {
  if (status === 401 || status === 403) return "Agent capacity needs the system:read permission. Ask an admin to grant it, then reload Setup.";
  return `Could not read agent capacity${status ? ` (HTTP ${status})` : ""}. Reload Setup; if it stays, check that the API is up in Observability › System.`;
}

export function AgentCapacityRow() {
  const { projectId } = useBoard();
  const [state, setState] = useState<Capacity | null>(null);
  const [workers, setWorkers] = useState(3);
  const [builds, setBuilds] = useState("1");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  useEffect(() => {
    let active = true;
    async function read() {
      try {
        const response = await fetch(`/api/setup?project_id=${encodeURIComponent(projectId)}&agent_capacity=1`);
        if (!response.ok) throw new Error(capacityReadError(response.status));
        const body = await response.json();
        if (active && body.admission) {
          setState(body.admission);
          setWorkers(body.admission.worker_slots ?? 3);
          setBuilds(body.admission.builds_at_once.auto ? "auto" : String(body.admission.builds_at_once.pinned));
        }
      } catch (cause) {
        if (active) setError(cause instanceof Error && cause.message ? cause.message : capacityReadError(0));
      }
    }
    void read();
    return () => { active = false; };
  }, [projectId]);
  async function save() {
    setBusy(true);
    try {
      const result = await setupWrite<Capacity>(projectId, "agent_capacity", {
        value: builds === "auto" ? "auto" : Number(builds), worker_slots: workers,
      }, { label: "Save agent capacity" });
      setState(previous => previous ? { ...previous, ...result } : previous);
      toast("Agent capacity saved; active jobs finish without interruption");
    } catch (cause) {
      toast(cause instanceof Error ? cause.message : "Could not save capacity", "error");
    } finally { setBusy(false); }
  }
  return <section className="su-row" aria-label="Agent capacity">
    <div className="su-row-label">Agent capacity</div>
    <div className="su-row-body su-stack">
      <p>Install-wide limits. Heavy builds automatically wait when CPU, memory or storage is under pressure. Active work is not killed.</p>
      {error && <p role="alert">{error}</p>}
      <label>Total job slots <select value={workers} onChange={event => setWorkers(Number(event.target.value))} disabled={busy || !state}>
        {[1, 2, 3, 4].map(value => <option key={value} value={value}>{value}</option>)}
      </select></label>
      <label>Heavy builds at once <select value={builds} onChange={event => setBuilds(event.target.value)} disabled={busy || !state}>
        <option value="auto">Auto (host capacity)</option>
        {[1, 2, 3, 4].map(value => <option key={value} value={value}>{value}</option>)}
      </select></label>
      <p>Recommended for a 4-core / 8 GiB laptop: 3 job slots, 1 heavy build. Parallel streams within a build remain in Govern.</p>
      {state && <p>Effective heavy-build limit: {state.builds_at_once.value}. {state.builds_at_once.warning}
        {!state.next_build?.admitted && state.next_build?.shortfall ? ` Waiting: ${state.next_build.shortfall}` : ""}</p>}
      <GatedButton permission="project:update" className="su-btn su-btn-sm" disabled={busy || !state} onClick={() => void save()}>Save agent capacity</GatedButton>
    </div>
  </section>;
}
