"use client";

import React, { useActionState, useEffect, useState, type ReactNode } from "react";
import { useRouter } from "next/navigation";

import type { DiscoveryRun, DiscoveryFinding, DiscoveryOutcome, ModelAttention } from "@boss/api-client";

import { startDiscoveryAction, promoteOutcomeAction, deepenDiscoveryAction } from "./actions";
import { SubmitButton } from "./submit-button";

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
      <SubmitButton className="cap-btn primary" disabled={pending}>{pending ? "Convening…" : "→ Send to PRD meeting"}</SubmitButton>
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
      <SubmitButton className="cap-btn" disabled={pending} title="Run a multi-source parallel scout sweep for a richer, more-cited dossier">
        {pending ? "Deepening…" : "⏬ Deepen research"}
      </SubmitButton>
      {state?.error ? <span className="detail" style={{ color: "#f85149" }}>⚠ {state.error}</span> : null}
    </form>
  );
}

function Collapsible({ title, count, defaultOpen = false, children }: { title: string; count?: number | string; defaultOpen?: boolean; children: ReactNode }) {
  return (
    <details className="panel dx-collapse" open={defaultOpen}>
      <summary>{title}{count != null && count !== "" ? <span className="detail" style={{ fontWeight: 400 }}> · {count}</span> : null}</summary>
      <div>{children}</div>
    </details>
  );
}

// Render the market-analysis object as readable rows (handles nested objects + arrays) — not raw JSON.
function AnalysisRows({ obj }: { obj: Record<string, unknown> }) {
  const row = (label: string, val: unknown): ReactNode => {
    if (val == null || val === "") return null;
    const L = label.replace(/_0_5$/, "").replace(/_/g, " ");
    if (Array.isArray(val)) return <div key={label} className="detail"><b>{L}:</b> {val.map((x) => String(x)).join("; ")}</div>;
    if (typeof val === "object") {
      return (
        <div key={label} style={{ marginTop: 2 }}>
          <div className="detail" style={{ fontWeight: 600 }}>{L}</div>
          <div style={{ marginLeft: 12, display: "grid", gap: 2 }}>{Object.entries(val as Record<string, unknown>).map(([k, v]) => row(k, v))}</div>
        </div>
      );
    }
    return <div key={label} className="detail"><b>{L}:</b> {String(val)}</div>;
  };
  return <div style={{ display: "grid", gap: 4 }}>{Object.entries(obj).map(([k, v]) => row(k, v))}</div>;
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
function StartDiscovery({ projectId, researchAvailable }: {
  projectId: string;
  researchAvailable: boolean;
}) {
  const [state, action, pending] = useActionState(startDiscoveryAction, null);
  if (!researchAvailable) {
    return (
      <section className="panel" style={{ padding: 16 }} data-testid="discovery-research-paused">
        <div className="eyebrow">Discovery research paused in Community</div>
        <p className="detail" style={{ margin: "8px 0 0" }}>
          New and deeper research will return when budget and recovery safeguards are ready.
          Saved dossiers below remain available.
        </p>
      </section>
    );
  }
  return (
    <form action={action} className="panel" style={{ display: "grid", gap: 8, padding: 16 }}>
      <input type="hidden" name="project_id" value={projectId} />
      <div className="eyebrow">Research a signal</div>
      <textarea name="seed" required rows={3} placeholder="The idea / problem / signal to research — e.g. 'AI scribe for veterinary clinics' or a YC co-founder's idea"
        style={{ width: "100%", padding: 8, fontFamily: "inherit" }} />
      <input name="context" placeholder="Operator context (optional) — ICP, market, constraints"
        style={{ width: "100%", padding: 8 }} />
      <div style={{ display: "flex", gap: 8, alignItems: "center" }}>
        <SubmitButton className="cap-btn primary" disabled={pending}>
          {pending ? "Starting research…" : "🔎 Run discovery"}
        </SubmitButton>
        {state?.runId ? <span className="detail" style={{ color: "#3fb950" }}>Started {state.runId} — researching…</span> : null}
        {state?.error ? <span className="detail" style={{ color: "#f85149" }}>⚠ {state.error}</span> : null}
      </div>
    </form>
  );
}

// --------------------------------------------------------------------------- //
export function DiscoveriesHome({
  projectId, orgId, discoveries, apiReachable, initialDiscoveryId,
  researchAvailable = true,
}: {
  projectId: string;
  orgId?: string;
  discoveries: DiscoveryRun[];
  apiReachable: boolean;
  researchAvailable?: boolean;
  /** A dossier named in the URL — `?area=discovery&run=drun_…`, which is what the workflow's
   *  research step links to. Without this the id in the link was ignored and every "open the
   *  research" arrived at the list of every discovery the project had ever run. */
  initialDiscoveryId?: string | null;
}) {
  const [selected, setSelected] = useState<string | null>(initialDiscoveryId ?? null);
  // THE PROP IS READ ON EVERY NAVIGATION, NOT ONLY THE FIRST MOUNT.
  //
  // The console renders all fifty areas at once and shows one, so this component has ALREADY
  // mounted — with no dossier in the URL — long before the operator clicks "open the research"
  // on a workflow step. `useState(initialDiscoveryId)` captures its argument once, so the id
  // arriving in the URL changed the prop and nothing else: every link landed on the list of
  // every discovery the project had run, which is exactly what it looks like when a link is
  // broken. The plumbing was right and the state ignored it.
  useEffect(() => {
    setSelected(initialDiscoveryId ?? null);
  }, [initialDiscoveryId]);
  const anyLive = discoveries.some((d) => d.status === "running");
  useLiveRefresh(anyLive);

  const run = selected ? discoveries.find((d) => d.id === selected) : null;
  if (run) return <DiscoveryDetail run={run} orgId={orgId}
    researchAvailable={researchAvailable} onBack={() => setSelected(null)} />;

  // ASKED FOR ONE, GOT THE SHELF. A link naming a dossier that is not in this feed — older
  // than the window, or another project's — used to fall through to the list with no word of
  // explanation, which looks exactly like a link that does nothing.
  const missing = selected && !run ? selected : null;

  return (
    <div style={{ display: "grid", gap: 16 }}>
      <StartDiscovery projectId={projectId} researchAvailable={researchAvailable} />
      {missing ? (
        <div className="detail" data-testid="discovery-not-in-feed">
          Research run <code>{missing}</code> is not in this project&rsquo;s recent runs — showing
          all of them instead.
        </div>
      ) : null}
      {!apiReachable ? <div className="detail" style={{ color: "#f85149" }}>API unreachable — start the control plane.</div> : null}
      {discoveries.length === 0 ? (
        <div className="detail">{researchAvailable
          ? "No discovery runs yet. Feed an idea above and BOSS will research the market signal behind it."
          : "No saved discovery dossiers yet."}</div>
      ) : (
        <div style={{ display: "grid", gap: 10 }}>
          {discoveries.map((d, i) => {
            const st = d.state ?? {};
            const verdict = st.gate?.verdict;
            const findings = st.signal_findings?.length ?? 0;
            const outcomes = st.candidate_outcomes?.length ?? 0;
            const title = (d.seed && d.seed.trim()) || d.title || "(untitled)";
            // THE LIST IS WHAT THE OPERATOR SEES.
            //
            // The colour work went into the expanded run detail, which is one click away — so
            // the page itself looked untouched, and the operator said so. Same vocabulary as
            // the Meeting room applied HERE: a coloured stage chip, the flow as a compact
            // ribbon, and the numbers as pills rather than grey text.
            return (
              <button key={d.id} className="dx-row" onClick={() => setSelected(d.id)}>
                <span className="dx-row-head">
                  <span className={`dx-stage stage-${String(d.phase || "queued")}`}>
                    {PHASE_LABEL[d.phase] ?? d.phase}
                  </span>
                  <span className="dx-row-title">{title}</span>
                  <span className="dx-row-badges">
                    <span className={`cap-badge tone-${statusTone(d.status)}`}>{d.status}</span>
                    <VerdictBadge v={verdict} />
                  </span>
                </span>

                <span className="dx-mini-flow" aria-hidden>
                  {PHASE_ORDER.map((ph) => {
                    const at = PHASE_ORDER.indexOf(d.phase);
                    const done = PHASE_ORDER.indexOf(ph) < at || d.status === "completed";
                    const cur = ph === d.phase && d.status === "running";
                    const stopped = ph === d.phase && d.status === "failed";
                    return (
                      <span
                        key={ph}
                        className={`dx-tick${done ? " done" : ""}${cur ? " current" : ""}${stopped ? " stopped" : ""}`}
                        title={PHASE_LABEL[ph]}
                      />
                    );
                  })}
                </span>

                {d.attention ? (
                  <span className={d.attention.state === "waiting" ? "dx-row-wait" : "dx-row-fail"}
                        data-testid="discovery-row-attention">
                    {d.attention.state === "waiting" ? "⏳" : "⚠"} {d.attention.headline}
                  </span>
                ) : d.status === "failed" ? (
                  <span className="dx-row-fail">✗ {(d.error ?? "failed").slice(0, 120)}</span>
                ) : (
                  <span className="dx-row-stats">
                    <span className="dx-pill"><b>{findings}</b> cited signals</span>
                    <span className="dx-pill"><b>{outcomes}</b> outcome{outcomes === 1 ? "" : "s"}</span>
                    {st.gate?.confidence != null ? (
                      <span className="dx-pill"><b>{Math.round((st.gate.confidence ?? 0) * 100)}%</b> confidence</span>
                    ) : null}
                    {d.usage?.cost_usd_micros != null ? (
                      <span className="dx-pill muted">{fmtUsd(d.usage.cost_usd_micros)}</span>
                    ) : null}
                  </span>
                )}
              </button>
            );
          })}
        </div>
      )}
    </div>
  );
}

// --------------------------------------------------------------------------- //
function DiscoveryDetail({ run, orgId, researchAvailable, onBack }: {
  run: DiscoveryRun;
  orgId?: string;
  researchAvailable: boolean;
  onBack: () => void;
}) {
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
        {run.status === "completed" && researchAvailable ? <DeepenButton runId={run.id} /> : null}
      </div>

      <div>
        <h2 style={{ margin: "0 0 4px" }}>{run.title}</h2>
        {run.seed ? <div className="detail">{run.seed}</div> : null}
        {run.context ? <div className="detail" style={{ marginTop: 4 }}>Context: {run.context}</div> : null}
        {!researchAvailable ? <div className="detail" style={{ marginTop: 8 }}>
          This saved dossier remains available. Deeper research is paused in Community until
          budget and recovery safeguards are ready.
        </div> : null}
      </div>

      {/* THE FLOW, ALWAYS. This appeared only while running, so a finished or failed run showed
          no shape at all — which is most of why this page read as a wall of grey next to the
          Meeting room. Same treatment as the meeting flow ribbon: where it got to, and where
          it stopped. */}
      <div className="dx-flow" data-testid="discovery-flow">
        {PHASE_ORDER.map((p, n) => {
          const at = PHASE_ORDER.indexOf(run.phase);
          const done = PHASE_ORDER.indexOf(p) < at || run.status === "completed";
          const cur = p === run.phase && live;
          const stopped = p === run.phase && run.status === "failed";
          return (
            <React.Fragment key={p}>
              {n > 0 ? <span className="dx-flow-arrow" aria-hidden>→</span> : null}
              <span className={`dx-flow-step${done ? " done" : ""}${cur ? " current" : ""}${stopped ? " stopped" : ""}`}>
                <span className="dx-flow-dot" aria-hidden />
                {PHASE_LABEL[p]}
              </span>
            </React.Fragment>
          );
        })}
      </div>

      {run.attention ? <AttentionBanner attention={run.attention} /> : run.error
        ? <div className="panel" style={{ padding: 12, color: "#f85149" }}>⚠ {run.error}</div> : null}

      {/* Evidence gate — compact, always visible */}
      {gate ? (
        <section className="panel" style={{ padding: "12px 16px" }}>
          <div style={{ display: "flex", flexWrap: "wrap", alignItems: "center", gap: 10 }}>
            <span style={{ fontWeight: 600 }}>Evidence gate</span>
            <VerdictBadge v={gate.verdict} />
            <span className="detail">confidence {Math.round((gate.confidence ?? 0) * 100)}%{gate.thesis_confidence != null ? ` · thesis ${Math.round(gate.thesis_confidence * 100)}% · best outcome ${Math.round((gate.best_outcome_confidence ?? 0) * 100)}%` : ""}</span>
          </div>
          <div style={{ display: "flex", flexWrap: "wrap", gap: 12, marginTop: 8 }}>
            {Object.entries(gate.checks ?? {}).map(([k, v]) => (
              <span key={k} className="detail" style={{ color: v ? "#3fb950" : "#f85149" }}>{v ? "✓" : "✗"} {k.replace(/_/g, " ")}</span>
            ))}
          </div>
        </section>
      ) : null}

      {/* Candidate outcomes */}
      {(d.candidate_outcomes?.length ?? 0) > 0 ? (
        <section style={{ display: "grid", gap: 10 }}>
          <div className="eyebrow">Candidate outcomes ({d.candidate_outcomes!.length})</div>
          {d.candidate_outcomes!.map((o: DiscoveryOutcome, i) => (
            <div key={i} className="panel" style={{ padding: 14, display: "grid", gap: 8 }}>
              <div style={{ fontWeight: 700, fontSize: 15, lineHeight: 1.35, color: "var(--ink)" }}>
                {o.outcome}{o.confidence_0_1 != null ? <span className="detail" style={{ fontWeight: 400 }}> · {Math.round(o.confidence_0_1 * 100)}% confidence</span> : null}
              </div>
              {(o.wedge || o.target_pilot || o.first_prototype_slice || o.validation_plan || o.rationale) ? (
                <details className="dx-collapse dx-inline">
                  <summary>details</summary>
                  <div style={{ display: "grid", gap: 4, marginTop: 6 }}>
                    {o.wedge ? <div className="detail"><b>Wedge:</b> {o.wedge}</div> : null}
                    {o.target_pilot ? <div className="detail"><b>Pilot with:</b> {o.target_pilot}</div> : null}
                    {o.first_prototype_slice ? <div className="detail"><b>First prototype:</b> {o.first_prototype_slice}</div> : null}
                    {o.validation_plan ? <div className="detail"><b>Validation:</b> {o.validation_plan}</div> : null}
                    {o.rationale ? <div className="detail">{o.rationale}</div> : null}
                  </div>
                </details>
              ) : null}
              {buildable ? (
                <PromoteOutcome runId={run.id} idx={i} orgId={orgId}
                  promotedTaskId={(d.promotions ?? []).find((p) => p.outcome_index === i)?.task_id} />
              ) : null}
            </div>
          ))}
        </section>
      ) : null}

      {/* Deep evidence — collapsed by default for skimmability */}
      {d.critique ? (
        <Collapsible title="Adversarial vetting" count={d.critique.overall_confidence_0_1 != null ? `confidence ${Math.round(d.critique.overall_confidence_0_1 * 100)}%` : ""}>
          {d.critique.biggest_risk ? <div className="detail"><b>Biggest risk:</b> {d.critique.biggest_risk}</div> : null}
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
        </Collapsible>
      ) : null}

      {d.market_analysis && Object.keys(d.market_analysis).length > 0 ? (
        <Collapsible title="Market analysis"><AnalysisRows obj={d.market_analysis} /></Collapsible>
      ) : null}

      {(d.signal_findings?.length ?? 0) > 0 ? (
        <Collapsible title="Cited market signals" count={d.signal_findings!.length}>
          <div style={{ display: "grid", gap: 10 }}>
            {d.signal_findings!.map((f: DiscoveryFinding, i) => (
              <div key={i} style={{ display: "grid", gap: 2 }}>
                <div className="dx-finding">
                  <span className={`dx-angle angle-${String(f.angle || "other").toLowerCase()}`}>
                    {f.angle}
                  </span>
                  <span className="dx-finding-text">{f.finding}</span>
                </div>
                {f.evidence ? <div className="detail">{f.evidence}</div> : null}
                {f.citation_url ? <a className="detail" href={f.citation_url} target="_blank" rel="noreferrer" style={{ color: "#58a6ff" }}>{f.citation_url} ↗</a> : null}
              </div>
            ))}
          </div>
        </Collapsible>
      ) : null}

      {d.research_brief?.hypothesis ? (
        <Collapsible title="Research brief">
          <div className="detail"><b>Hypothesis:</b> {d.research_brief.hypothesis}</div>
          {(d.research_brief.questions?.length ?? 0) > 0 ? (
            <ul style={{ margin: "6px 0 0 18px" }}>{d.research_brief.questions!.map((q, i) => <li key={i} className="detail">{q}</li>)}</ul>
          ) : null}
        </Collapsible>
      ) : null}
    </div>
  );
}


/**
 * WHY IT IS NOT MOVING, AND WHAT TO DO. A run that said "queued" while its worker was refused,
 * its budget was capped or its Claude login was rejected gave the operator nothing to act on.
 * The API now explains each of those (model_failures.explain); this puts it where they look.
 */
function AttentionBanner({ attention }: { attention: ModelAttention }) {
  const waiting = attention.state === "waiting";
  return (
    <div className={`panel dx-attention ${waiting ? "is-waiting" : "is-stopped"}`} role="status"
         data-testid="discovery-attention" data-kind={attention.kind}>
      <div className="dx-attention-head">{waiting ? "⏳" : "⚠"} {attention.headline}</div>
      <div className="dx-attention-action">
        {attention.action}
        {attention.href ? <> <a href={attention.href}>Open →</a></> : null}
      </div>
      {attention.detail ? <div className="detail dx-attention-detail">{attention.detail}</div> : null}
    </div>
  );
}
