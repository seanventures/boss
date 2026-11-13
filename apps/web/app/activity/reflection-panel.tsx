"use client";

/**
 * Activity › Feedback › Reflection (BOSSAPP-888): the reflection agent reads an agent's friction
 * reports and failed skill calls and PROPOSES skill and instruction changes — each one a card in
 * Needs you, with the runs it cites. Paused by default: Turn on runs it on a schedule; Reflect
 * now runs it once for one agent. It never writes a change itself.
 */
import React, { useCallback, useEffect, useState } from "react";

import { GatedButton } from "../../components/gated";
import {
  PROPOSAL_WORDS, readReflection, runReflection, setReflection, type ReflectionState,
} from "../../lib/reflection";

export function ReflectionPanel({ projectId }: { projectId: string }) {
  const [state, setState] = useState<ReflectionState | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [role, setRole] = useState("");
  const [said, setSaid] = useState<string | null>(null);

  const load = useCallback(async () => {
    const r = await readReflection(projectId);
    if (r.ok && "data" in r && r.data) { setState(r.data); setError(null); }
    else if (!r.ok) setError(r.error === "missing" ? null : r.error);
  }, [projectId]);
  useEffect(() => { void load(); }, [load]);

  const toggle = async () => {
    if (!state) return;
    const r = await setReflection(projectId, { enabled: !state.enabled });
    setSaid(r.ok ? (state.enabled ? "Reflection paused." : "Reflection is on.") : r.error ?? "Not saved.");
    if (r.ok) void load();
  };
  const runNow = async () => {
    const r = await runReflection(projectId, role.trim());
    if (!r.ok) { setSaid(r.error ?? "Not run."); return; }
    const run = obj(obj(r.body).run);
    const made = Array.isArray(run.proposals) ? run.proposals.length : 0;
    setSaid(made ? `${made} proposal${made === 1 ? "" : "s"} in Needs you.` : "Nothing to propose from the evidence.");
    void load();
  };

  if (!state && !error) return null;
  return (
    <section className="afb-reflection" aria-label="Reflection" data-testid="reflection-panel">
      <h4>Reflection <span className="afb-meta">{state?.enabled ? `on, every ${state.every_hours} h` : "paused"}</span></h4>
      {error ? <p data-testid="reflection-error">{error}</p> : null}
      {state ? (
        <div className="afb-actions">
          <GatedButton permission="agent:update" projectId={projectId} className="kit-link" data-testid="reflection-toggle"
                       title="Reflect on agents' recent runs on a schedule; it only proposes"
                       onClick={() => void toggle()}>{state.enabled ? "Pause" : "Turn on"}</GatedButton>
          <input aria-label="Agent to reflect on" placeholder="agent, e.g. forge" value={role}
                 onChange={(e) => setRole(e.target.value)} />
          <GatedButton permission="agent:update" projectId={projectId} className="kit-link" data-testid="reflection-run"
                       disabled={!role.trim()} onClick={() => void runNow()}>Reflect now</GatedButton>
        </div>
      ) : null}
      {said ? <p role="status" data-testid="reflection-said">{said}</p> : null}
      {state && state.runs.length ? (
        <ul data-testid="reflection-runs">
          {state.runs.slice(0, 5).map((r) => (
            <li key={r.id} data-testid={`reflection-run-${r.id}`}>
              {r.role_key}: {r.status === "proposed"
                ? r.proposals.map((p) => `${PROPOSAL_WORDS[p.kind]} (${p.target}, ${p.cited_runs.length} runs cited)`).join("; ")
                : r.status === "failed" ? `failed — ${r.error ?? "no detail"}` : "nothing to propose"}
            </li>
          ))}
        </ul>
      ) : null}
    </section>
  );
}

function obj(v: unknown): Record<string, unknown> {
  return v && typeof v === "object" && !Array.isArray(v) ? v as Record<string, unknown> : {};
}
