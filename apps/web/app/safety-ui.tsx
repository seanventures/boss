"use client";

import React, { useState } from "react";
import { useRouter, useSearchParams } from "next/navigation";
import { formatDateTime, formatTime } from "../lib/datetime";
import { type PolicyEval, type RunTrace } from "../lib/safety";
import { scanAnomaliesAction } from "./actions";
import { SubmitButton } from "./submit-button";
import { ActionForm } from "./client-forms";

function decisionTone(d: string): string {
  if (d === "allow") return "#3fb950";
  if (d === "deny") return "#f85149";
  if (d === "defer") return "#d29922";
  return "#8b949e";
}

export function PolicyEvalList({ items }: { items: PolicyEval[] }) {
  return (
    <article className="stack-card">
      <h3 style={{ margin: 0 }}>Policy-eval audit trail</h3>
      <p className="detail">
        Every allow / deny / defer at an enforcement boundary — the durable record behind “policy
        violations tracked and blocked.”
      </p>
      {items.length === 0 ? (
        <p className="detail">No policy evaluations recorded yet.</p>
      ) : (
        <div className="feed-list">
          {items.map((e) => (
            <div className="feed-row" key={e.id}>
              <div>
                <h4 style={{ margin: 0 }}>
                  <span style={{ color: decisionTone(e.decision), textTransform: "uppercase", fontSize: 12 }}>
                    {e.decision}
                  </span>{" "}
                  <span style={{ color: "var(--ink)" }}>{e.boundary}</span>
                </h4>
                <p className="detail">{e.action ?? "—"}{e.reason ? ` · ${e.reason}` : ""}</p>
              </div>
              <span className="pill">{e.createdAt ? formatDateTime(e.createdAt) : ""}</span>
            </div>
          ))}
        </div>
      )}
    </article>
  );
}

export function AnomalyScanCard({ projectId, actor }: { projectId: string; actor: string }) {
  return (
    <article className="stack-card">
      <h3 style={{ margin: 0 }}>Cost-anomaly scan</h3>
      <p className="detail">
        Scan for retry storms and spend spikes. Detected anomalies open an incident Decision and can
        trip auto-lite-mode (lower budget intensity).
      </p>
      <ActionForm action={scanAnomaliesAction}>
        <input type="hidden" name="project_id" value={projectId} />
        <input type="hidden" name="actor" value={actor} />
        <SubmitButton className="button secondary" type="submit">Scan for anomalies</SubmitButton>
      </ActionForm>
    </article>
  );
}

export function RunTraceViewer({ trace, initialRunId }: { trace: RunTrace; initialRunId?: string }) {
  const router = useRouter();
  const params = useSearchParams();
  const [runId, setRunId] = useState(initialRunId ?? "");

  const lookup = () => {
    const next = new URLSearchParams(params.toString());
    next.set("area", "ops");
    if (runId.trim()) next.set("trace_run", runId.trim());
    else next.delete("trace_run");
    router.push(`/?${next.toString()}`);
  };

  return (
    <article className="stack-card">
      <h3 style={{ margin: 0 }}>Run trace — “why did we do that”</h3>
      <p className="detail">
        Reconstruct a code-task run’s full timeline: the run, its fail-closed quality gate, and every
        event that references it.
      </p>
      <div style={{ display: "flex", gap: 8, margin: "8px 0" }}>
        <input
          value={runId}
          onChange={(e) => setRunId(e.target.value)}
          onKeyDown={(e) => { if (e.key === "Enter") lookup(); }}
          placeholder="run id (ctr_…)"
          style={{ flex: 1, minWidth: 0 }}
          aria-label="Run id to trace"
        />
        <button type="button" className="button secondary" onClick={lookup}>Trace</button>
      </div>
      {initialRunId && !trace ? (
        <p className="detail" style={{ color: "#f85149" }}>No run found for “{initialRunId}”.</p>
      ) : null}
      {trace ? (
        <>
          <p className="detail" style={{ marginBottom: 6 }}>
            <b style={{ color: "var(--ink)" }}>{trace.run.title || trace.run.id}</b> — {trace.run.status} · {trace.run.phase} · {trace.eventCount} events
            {trace.qualityGate ? (
              <>
                {" "}· gate{" "}
                <b style={{ color: trace.qualityGate.status === "pass" ? "#3fb950" : "#f85149" }}>
                  {trace.qualityGate.status}
                </b>
              </>
            ) : null}
          </p>
          <div style={{ display: "grid", gap: 3 }}>
            {trace.timeline.slice(0, 40).map((ev, i) => (
              <div key={i} style={{ fontSize: 12.5, color: "var(--ink-soft)" }}>
                <span style={{ color: "var(--muted)" }}>{formatTime(ev.createdAt)}</span>{" "}
                <b style={{ color: "var(--ink)" }}>{ev.eventType}</b>{" "}
                <span style={{ color: "var(--muted)" }}>· {ev.actor}</span>
              </div>
            ))}
          </div>
        </>
      ) : null}
    </article>
  );
}
