"use client";

import React, { useState, useTransition } from "react";
import {
  type SecurityFinding,
  type SecurityScanRun,
  type SecurityScanner,
  evaluateSecurityGate,
} from "../lib/security-scan";

function sevTone(sev: string): string {
  const s = (sev || "").toLowerCase();
  if (s === "critical") return "#f85149";
  if (s === "high") return "#ff7b72";
  if (s === "medium") return "#d29922";
  if (s === "low") return "#3fb950";
  return "#8b949e";
}

function statusTone(status: string): string {
  if (status === "completed" || status === "pass") return "#3fb950";
  if (status === "failed" || status === "no-go") return "#f85149";
  return "#8b949e";
}

function FindingRow({ f }: { f: SecurityFinding }) {
  return (
    <div
      style={{
        display: "grid",
        gridTemplateColumns: "72px 1fr",
        gap: 8,
        fontSize: 12.5,
        padding: "6px 0",
        borderBottom: "1px solid var(--border, #30363d)",
      }}
    >
      <span style={{ color: sevTone(f.severity), fontWeight: 700, textTransform: "uppercase" }}>
        {f.severity}
      </span>
      <div>
        <div style={{ color: "var(--ink)" }}>
          <b>{f.rule_id}</b>{" "}
          <span style={{ color: "var(--muted)" }}>· {f.scanner}</span>
          {f.cve ? <span style={{ color: "var(--muted)" }}> · {f.cve}</span> : null}
        </div>
        <div style={{ color: "var(--ink-soft)" }}>{f.message}</div>
        <div style={{ color: "var(--muted)", fontSize: 11.5 }}>
          {f.path}
          {f.line != null ? `:${f.line}` : ""}
          {f.fix ? ` — fix: ${f.fix}` : ""}
        </div>
      </div>
    </div>
  );
}

export function SecurityScanCard({
  initialRun,
  scanners = [],
}: {
  projectId: string;
  initialRun: SecurityScanRun;
  scanners?: SecurityScanner[];
}) {
  const [run, setRun] = useState<SecurityScanRun>(initialRun);
  const [error, setError] = useState<string | null>(null);
  const [pending, startTransition] = useTransition();

  const onGate = () => {
    if (!run?.id) return;
    setError(null);
    startTransition(async () => {
      const gate = await evaluateSecurityGate(run.id);
      if (!gate) {
        setError("Gate evaluation failed.");
        return;
      }
      setRun({ ...run, gate, status: gate.go ? run.status : "failed" });
    });
  };

  return (
    <article className="stack-card">
      <div
        style={{
          display: "flex",
          justifyContent: "space-between",
          alignItems: "center",
          gap: 10,
          flexWrap: "wrap",
        }}
      >
        <h3 style={{ margin: 0 }}>Security scan (OSS)</h3>
        <div style={{ display: "flex", gap: 8, alignItems: "center" }}>
          {run?.id ? (
            <button
              type="button"
              onClick={onGate}
              disabled={pending}
              className="button secondary"
              style={{
                borderRadius: 8,
                padding: "6px 13px",
                fontSize: 12.5,
                cursor: pending ? "wait" : "pointer",
              }}
            >
              Re-evaluate gate
            </button>
          ) : null}
        </div>
      </div>

      <p className="detail">
        Scanner runs are unavailable until BOSS confines them to a configured project
        workspace and reserves a durable receipt before execution. Prior findings and
        their gates remain available below.
      </p>

      {scanners.length > 0 ? (
        <div style={{ display: "flex", flexWrap: "wrap", gap: 6, marginBottom: 8 }}>
          {scanners.map((s) => (
            <span
              key={s.id}
              className="pill"
              title={s.stars_note}
              style={{
                opacity: s.status === "supported" ? 1 : 0.6,
                fontSize: 11.5,
              }}
            >
              {s.name}
              {s.available ? " ●" : s.status === "planned_byo" ? " (BYO)" : ""}
            </span>
          ))}
        </div>
      ) : null}

      {error ? (
        <p className="detail" style={{ color: "#f85149" }}>
          {error}
        </p>
      ) : null}

      {!run ? (
        <p className="detail">No scan runs yet.</p>
      ) : (
        <>
          <p className="detail" style={{ marginBottom: 8 }}>
            Latest:{" "}
            <b style={{ color: statusTone(run.status) }}>{run.status.toUpperCase()}</b>
            {" · "}
            gate{" "}
            <b style={{ color: statusTone(run.gate?.decision ?? "") }}>
              {(run.gate?.decision ?? "—").toUpperCase()}
            </b>
            {" · "}
            {run.summary?.total ?? 0} findings
            {run.summary?.critical ? ` (${run.summary.critical} critical)` : ""}
            {run.simulated ? " · simulated" : ""}
            {run.receipt_id ? (
              <span style={{ color: "var(--muted)" }}> · receipt {run.receipt_id}</span>
            ) : null}
          </p>
          <div style={{ maxHeight: 280, overflow: "auto" }}>
            {(run.findings ?? []).slice(0, 40).map((f, i) => (
              <FindingRow key={`${f.scanner}-${f.rule_id}-${f.path}-${i}`} f={f} />
            ))}
          </div>
          {(run.findings?.length ?? 0) > 40 ? (
            <p className="detail">Showing first 40 of {run.findings.length} findings.</p>
          ) : null}
        </>
      )}
    </article>
  );
}
