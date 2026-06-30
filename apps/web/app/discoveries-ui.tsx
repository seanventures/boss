"use client";

import { useActionState, useEffect, useState } from "react";
import { useRouter } from "next/navigation";

import type { DiscoveryRun, DiscoveryFinding, DiscoveryOutcome } from "@boss/api-client";

import { startDiscoveryAction, promoteOutcomeAction, deepenDiscoveryAction } from "./actions";

const PHASE_LABEL: Record<string, string> = {
  queued: "Queued",
  signal_sweep: "Signal sweep (web)",
  synthesize: "Synthesize",
  vet: "Adversarial vetting",
  shape: "Shape outcomes",
  gate: "Evidence gate",
  done: "Done",
};
const PHASE_ORDER = ["signal_sweep", "synthesize", "vet", "shape", "gate", "done"];

function fmtUsd(micros?: number | null): string {
  if (micros == null) return "";
  return `$${(micros / 1_000_000).toFixed(2)}`;
}
function statusTone(s: string): string {
  if (s === "completed") return "ok";
  if (s === "failed") return "bad";
  return "warn";
}

function PromoteOutcome({ runId, idx, orgId, promotedTaskId }: { runId: string; idx: number; orgId?: string; promotedTaskId?: string }) {
  const [state, action, pending] = useActionState(promoteOutcomeAction, null);
  const taskId = promotedTaskId ?? state?.taskId;
  if (taskId) {
    const href = `/?org=${orgId ?? "org_boss"}&area=meetings&task=${taskId}`;
    return <a className="cap-btn" href={href}>✓ PRD meeting convened — open in Meetings →</a>;
  }
  return (
    <form action={action} style={{ display: "inline-flex", gap: 8, alignItems: "center", marginTop: 6 }}>
      <input type="hidden" name="run_id" value={runId} />
      <input type="hidden" name="outcome_index" value={idx} />
      <button className="cap-btn primary" disabled={pending}>{pending ? "Convening…" : "→ Send to PRD meeting"}</button>
      {state?.error ? <span className="detail" style={{ color: "#f85149" }}>⚠ {state.error}</span> : null}
    </form>
  );
}

function DeepenButton({ runId }: { runId: string }) {
  const [state, action, pending] = useActionState(deepenDiscoveryAction, null);
  if (state?.runId) return <span className="detail" style={{ color: "#3fb950" }}>⏬ Deeper pass started ({state.runId.slice(0, 9)}…) — see the list</span>;
  return (
    <form action={action} style={{ display: "inline-flex" }}>
      <input type="hidden" name="run_id" value={runId} />
      <button className="cap-btn" disabled={pending} title="Run a multi-source parallel scout sweep for a richer, more-cited dossier">
        {pending ? "Deepening…" : "⏬ Deepen research"}
      </button>
      {state?.error ? <span className="detail" style={{ color: "#f85149" }}>⚠ {state.error}</span> : null}
    </form>
  );
}

function VerdictBadge({ v }: { v?: string }) {
  if (!v) return null;
  if (v === "pursue") return <span className="cap-badge tone-ok">✓ pursue</span>;
  if (v === "promising") {
    return <span className="cap-badge" style={{ background: "#1f6feb33", color: "#58a6ff", borderColor: "#1f6feb66" }}>◆ promising</span>;
  }
  return <span className="cap-badge tone-warn">needs more research</span>;
}

function useLiveRefresh(active: boolean, intervalMs = 4000) {
  const router = useRouter();
  useEffect(() => {
    if (!active) return;
    const t = setInterval(() => router.refresh(), intervalMs);
    return () => clearInterval(t);
  }, [active, intervalMs, router]);
}

// --------------------------------------------------------------------------- //
function StartDiscovery({ projectId }: { projectId: string }) {
  const [state, action, pending] = useActionState(startDiscoveryAction, null);
  return (
    <form action={action} className="panel" style={{ display: "grid", gap: 8, padding: 16 }}>
      <input type="hidden" name="project_id" value={projectId} />
      <div className="eyebrow">Research a signal</div>
      <textarea name="seed" required rows={3} placeholder="The idea / problem / signal to research — e.g. 'AI scribe for veterinary clinics' or a YC co-founder's idea"
        style={{ width: "100%", padding: 8, fontFamily: "inherit" }} />
      <input name="context" placeholder="Operator context (optional) — ICP, market, constraints"
        style={{ width: "100%", padding: 8 }} />
      <div style={{ display: "flex", gap: 8, alignItems: "center" }}>
        <button className="cap-btn primary" disabled={pending}>
          {pending ? "Starting research…" : "🔎 Run discovery"}
        </button>
        {state?.runId ? <span className="detail" style={{ color: "#3fb950" }}>Started {state.runId} — researching…</span> : null}
        {state?.error ? <span className="detail" style={{ color: "#f85149" }}>⚠ {state.error}</span> : null}
      </div>
    </form>
  );
}

// --------------------------------------------------------------------------- //
export function DiscoveriesHome({ projectId, orgId, discoveries, apiReachable }: {
  projectId: string;
  orgId?: string;
  discoveries: DiscoveryRun[];
  apiReachable: boolean;
}) {
  const [selected, setSelected] = useState<string | null>(null);
  const anyLive = discoveries.some((d) => d.status === "running");
  useLiveRefresh(anyLive);

  const run = selected ? discoveries.find((d) => d.id === selected) : null;
  if (run) return <DiscoveryDetail run={run} orgId={orgId} onBack={() => setSelected(null)} />;

  return (
    <div style={{ display: "grid", gap: 16 }}>
      <StartDiscovery projectId={projectId} />
      {!apiReachable ? <div className="detail" style={{ color: "#f85149" }}>API unreachable — start the control plane.</div> : null}
      {discoveries.length === 0 ? (
        <div className="detail">No discovery runs yet. Feed an idea above and BOSS will research the market signal behind it.</div>
      ) : (
        <div style={{ display: "grid", gap: 10 }}>
          {discoveries.map((d, i) => {
            const st = d.state ?? {};
            const verdict = st.gate?.verdict;
            const findings = st.signal_findings?.length ?? 0;
            const outcomes = st.candidate_outcomes?.length ?? 0;
            const title = (d.seed && d.seed.trim()) || d.title || "(untitled)";
            return (
              <button key={d.id} className="ct-card" onClick={() => setSelected(d.id)} style={{ gap: 6 }}>
                <div style={{ display: "flex", gap: 10, alignItems: "flex-start", justifyContent: "space-between" }}>
                  <div style={{ fontWeight: 700, fontSize: 16, lineHeight: 1.35, color: "var(--ink)", flex: 1, minWidth: 0 }}>
                    <span style={{ color: "var(--muted)", fontWeight: 600, marginRight: 8 }}>{i + 1}.</span>
                    {title}
                  </div>
                  <span style={{ display: "flex", gap: 6, flexShrink: 0 }}>
                    <span className={`cap-badge tone-${statusTone(d.status)}`}>{d.status}</span>
                    <VerdictBadge v={verdict} />
                  </span>
                </div>
                <div className="detail">
                  {d.status === "running" ? `running — ${PHASE_LABEL[d.phase] ?? d.phase}` :
                   d.status === "failed" ? `✗ ${(d.error ?? "failed").slice(0, 80)}` :
                   `${findings} cited signals · ${outcomes} outcome${outcomes === 1 ? "" : "s"}`}
                </div>
                <div className="ct-card-foot">
                  {st.gate?.confidence != null ? <span className="detail">confidence {Math.round((st.gate.confidence ?? 0) * 100)}%</span> : null}
                  {d.usage?.cost_usd_micros != null ? <span className="detail">{fmtUsd(d.usage.cost_usd_micros)}</span> : null}
                </div>
              </button>
            );
          })}
        </div>
      )}
    </div>
  );
}

// --------------------------------------------------------------------------- //
function DiscoveryDetail({ run, orgId, onBack }: { run: DiscoveryRun; orgId?: string; onBack: () => void }) {
  const live = run.status === "running";
  useLiveRefresh(live);
  const d = run.state ?? {};
  const gate = d.gate;
  const buildable = gate?.verdict === "pursue" || gate?.verdict === "promising";

  return (
    <div style={{ display: "grid", gap: 16 }}>
      <div style={{ display: "flex", gap: 12, alignItems: "center" }}>
        <button className="cap-btn" onClick={onBack}>← Back</button>
        <span className={`cap-badge tone-${statusTone(run.status)}`}>{run.status}</span>
        <VerdictBadge v={gate?.verdict} />
        {(d as { deep?: boolean }).deep ? <span className="cap-badge">deep sweep</span> : null}
        {run.usage?.cost_usd_micros != null ? <span className="detail">{fmtUsd(run.usage.cost_usd_micros)}</span> : null}
        <span style={{ flex: 1 }} />
        {run.status === "completed" ? <DeepenButton runId={run.id} /> : null}
      </div>

      <div>
        <h2 style={{ margin: "0 0 4px" }}>{run.title}</h2>
        {run.seed ? <div className="detail">{run.seed}</div> : null}
        {run.context ? <div className="detail" style={{ marginTop: 4 }}>Context: {run.context}</div> : null}
      </div>

      {/* progress rail */}
      <div style={{ display: "flex", gap: 6, flexWrap: "wrap" }}>
        {PHASE_ORDER.map((p) => {
          const done = PHASE_ORDER.indexOf(p) < PHASE_ORDER.indexOf(run.phase);
          const cur = p === run.phase;
          return (
            <span key={p} className="cap-badge" style={{ opacity: done || cur ? 1 : 0.4, background: cur ? "#1f6feb33" : undefined }}>
              {done ? "✓ " : cur && live ? "● " : ""}{PHASE_LABEL[p]}
            </span>
          );
        })}
      </div>

      {run.error ? <div className="panel" style={{ padding: 12, color: "#f85149" }}>⚠ {run.error}</div> : null}

      {/* Evidence gate */}
      {gate ? (
        <section className="panel" style={{ padding: 16 }}>
          <div className="eyebrow">
            Evidence gate · {gate.verdict} · confidence {Math.round((gate.confidence ?? 0) * 100)}%
            {gate.thesis_confidence != null ? ` (thesis ${Math.round(gate.thesis_confidence * 100)}% · best outcome ${Math.round((gate.best_outcome_confidence ?? 0) * 100)}%)` : ""}
          </div>
          <div style={{ display: "grid", gap: 4, marginTop: 8 }}>
            {Object.entries(gate.checks ?? {}).map(([k, v]) => (
              <div key={k} className="detail">{v ? "✓" : "✗"} {k.replace(/_/g, " ")}</div>
            ))}
          </div>
        </section>
      ) : null}

      {/* Candidate outcomes */}
      {(d.candidate_outcomes?.length ?? 0) > 0 ? (
        <section style={{ display: "grid", gap: 10 }}>
          <div className="eyebrow">Candidate outcomes</div>
          {d.candidate_outcomes!.map((o: DiscoveryOutcome, i) => (
            <div key={i} className="panel" style={{ padding: 14, display: "grid", gap: 4 }}>
              <div style={{ fontWeight: 700 }}>{o.outcome}{o.confidence_0_1 != null ? <span className="detail" style={{ fontWeight: 400 }}> · {Math.round((o.confidence_0_1) * 100)}% confidence</span> : null}</div>
              {o.wedge ? <div className="detail"><b>Wedge:</b> {o.wedge}</div> : null}
              {o.target_pilot ? <div className="detail"><b>Pilot with:</b> {o.target_pilot}</div> : null}
              {o.first_prototype_slice ? <div className="detail"><b>First prototype:</b> {o.first_prototype_slice}</div> : null}
              {o.validation_plan ? <div className="detail"><b>Validation:</b> {o.validation_plan}</div> : null}
              {o.rationale ? <div className="detail">{o.rationale}</div> : null}
              {buildable ? (
                <PromoteOutcome runId={run.id} idx={i} orgId={orgId}
                  promotedTaskId={(d.promotions ?? []).find((p) => p.outcome_index === i)?.task_id} />
              ) : null}
            </div>
          ))}
        </section>
      ) : null}

      {/* Adversarial critique */}
      {d.critique ? (
        <section className="panel" style={{ padding: 16 }}>
          <div className="eyebrow">Adversarial vetting{d.critique.overall_confidence_0_1 != null ? ` · confidence ${Math.round(d.critique.overall_confidence_0_1 * 100)}%` : ""}</div>
          {d.critique.biggest_risk ? <div className="detail" style={{ marginTop: 6 }}><b>Biggest risk:</b> {d.critique.biggest_risk}</div> : null}
          {(d.critique.claims?.length ?? 0) > 0 ? (
            <div style={{ display: "grid", gap: 4, marginTop: 8 }}>
              {d.critique.claims!.map((c, i) => (
                <div key={i} className="detail">
                  <span style={{ color: c.verdict === "supported" ? "#3fb950" : c.verdict === "unsupported" ? "#f85149" : "#d29922" }}>[{c.verdict}]</span> {c.claim} — {c.reason}
                </div>
              ))}
            </div>
          ) : null}
          {(d.critique.disconfirming_evidence?.length ?? 0) > 0 ? (
            <div style={{ marginTop: 8 }}>
              <div className="detail" style={{ fontWeight: 600 }}>Disconfirming evidence:</div>
              {d.critique.disconfirming_evidence!.map((x, i) => <div key={i} className="detail">• {x}</div>)}
            </div>
          ) : null}
        </section>
      ) : null}

      {/* Market analysis */}
      {d.market_analysis && Object.keys(d.market_analysis).length > 0 ? (
        <section className="panel" style={{ padding: 16 }}>
          <div className="eyebrow">Market analysis</div>
          <pre style={{ whiteSpace: "pre-wrap", fontSize: 12, marginTop: 8 }}>{JSON.stringify(d.market_analysis, null, 2)}</pre>
        </section>
      ) : null}

      {/* Cited signals */}
      {(d.signal_findings?.length ?? 0) > 0 ? (
        <section style={{ display: "grid", gap: 8 }}>
          <div className="eyebrow">Cited market signals ({d.signal_findings!.length})</div>
          {d.signal_findings!.map((f: DiscoveryFinding, i) => (
            <div key={i} className="panel" style={{ padding: 12, display: "grid", gap: 2 }}>
              <div className="detail"><span className="cap-badge">{f.angle}</span> {f.finding}</div>
              {f.evidence ? <div className="detail">{f.evidence}</div> : null}
              {f.citation_url ? <a className="detail" href={f.citation_url} target="_blank" rel="noreferrer" style={{ color: "#58a6ff" }}>{f.citation_url} ↗</a> : null}
            </div>
          ))}
        </section>
      ) : null}

      {/* Research brief */}
      {d.research_brief?.hypothesis ? (
        <section className="panel" style={{ padding: 16 }}>
          <div className="eyebrow">Research brief</div>
          <div className="detail" style={{ marginTop: 6 }}><b>Hypothesis:</b> {d.research_brief.hypothesis}</div>
          {(d.research_brief.questions?.length ?? 0) > 0 ? (
            <ul style={{ margin: "6px 0 0 18px" }}>{d.research_brief.questions!.map((q, i) => <li key={i} className="detail">{q}</li>)}</ul>
          ) : null}
        </section>
      ) : null}
    </div>
  );
}
