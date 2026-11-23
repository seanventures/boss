"use client";

/**
 * AGENT CAPACITY (Setup › Advanced): total job slots (1-4, what the worker leases at once)
 * apart from the heavy-build ceiling (admission's "builds at once", never above what the host
 * measures). Install-wide: projects share the workers and the host. Short labels, the why in
 * tooltips (the board's copy budget).
 */
import React, { useEffect, useState } from "react";
import { GatedButton } from "../../components/gated";
import { setupWrite } from "../../lib/setup-client";
import { SectionLabel } from "../section-label";
import { toast, useBoard } from "./parts";
import { Tip } from "./pill";

const TIP = "Install-wide. Heavy builds wait while CPU, memory or disk is short.";
const SLOTS_TIP = "Jobs at once. Lowering it lets running jobs finish; none is stopped.";
const BUILDS_TIP = "Heavy builds at once. Never above what this machine measures it can run.";

type Capacity = {
  worker_slots: number;
  builds_at_once: { value: number; auto: boolean; pinned: number | null; warning: string | null };
  next_build: { admitted: boolean; shortfall: string | null };
};

/** Why the limits could not be read, and what to do: never a bare "unavailable". */
export function capacityReadError(status: number): string {
  if (status === 401 || status === 403) return "Needs system:read. Ask an admin for it, then reload Setup.";
  return `Read failed${status ? ` (HTTP ${status})` : ""}. Reload Setup, or check Observability › System.`;
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
  const bao = state?.builds_at_once;
  const waiting = state && !state.next_build?.admitted && state.next_build?.shortfall ? state.next_build.shortfall : "";
  return <section className="su-row" aria-label="Agent capacity" data-testid="su-row-agent-capacity">
    <div className="su-row-label"><SectionLabel as="span" tip={TIP}>Agent capacity</SectionLabel></div>
    <div className="su-row-body su-stack">
      {error ? <Tip tip={error} className="su-faint" testid="su-capacity-error"><span role="alert">Couldn&apos;t read capacity</span></Tip> : null}
      <div className="su-line">
        <Tip tip={SLOTS_TIP}><label>Job slots <select value={workers} onChange={event => setWorkers(Number(event.target.value))} disabled={busy || !state}>
          {[1, 2, 3, 4].map(value => <option key={value} value={value}>{value}</option>)}
        </select></label></Tip>
        <Tip tip={BUILDS_TIP}><label>Heavy builds <select value={builds} onChange={event => setBuilds(event.target.value)} disabled={busy || !state}>
          <option value="auto">Auto</option>
          {[1, 2, 3, 4, 5, 6, 7, 8].map(value => <option key={value} value={value}>{value}</option>)}
        </select></label></Tip>
        <GatedButton permission="project:update" className="su-btn su-btn-sm" disabled={busy || !state} onClick={() => void save()}>Save</GatedButton>
      </div>
      {bao ? <Tip tip={[bao.warning, waiting ? `Waiting: ${waiting}` : ""].filter(Boolean).join(" ") || "Heavy builds this machine runs at once now."}
                  className="su-faint" testid="su-capacity-now">{`Now: ${bao.value} heavy build${bao.value === 1 ? "" : "s"}${waiting ? ", waiting" : ""}`}</Tip> : null}
    </div>
  </section>;
}
