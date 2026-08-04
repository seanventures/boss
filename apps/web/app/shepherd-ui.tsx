"use client";

import { useEffect, useState } from "react";

/**
 * Task Shepherd console.
 *
 * The operator's stated interest is the REASONING, not the actions — "I want to see how the
 * task shepherd is actually thinking". So this leads with thought: every card is one argument
 * (observe → hypothesis → check → decide), and the check and the cost prediction are given
 * the same weight as the conclusion.
 *
 * Two things are deliberately impossible to miss, because both were invisible during the
 * incident this exists to prevent:
 *   - whether a hypothesis was CORROBORATED, since acting on an unchecked assumption is what
 *     restarted a healthy 90-minute run three times;
 *   - predicted vs ACTUAL cost, since a shepherd that quietly under-estimates is
 *     pattern-matching with a confident tone, not reasoning.
 */

import {
  getShepherdOverviewAction, shepherdAnswerAction, shepherdSweepAction,
} from "./actions";

type Reasoning = {
  id: string; task_ref: string; step_no: number; observe: string;
  hypothesis: string | null; confidence: number | null;
  check_result: string | null; check_confirmed: boolean | null;
  failure_class: number | null; remedy_chosen: string | null; rationale: string | null;
  predicted_cost_usd_micros: number | null; predicted_outcome: string | null;
  actual_cost_usd_micros: number | null; actual_outcome: string | null;
  stopped_because: string | null; created_at: string;
};
type Ledger = {
  task_ref: string; failure_class: number; remedy_id: string; status: string;
  cost_usd_micros: number | null; outcome: string | null;
};
type Escalation = {
  id: string; task_ref: string; question: string;
  options: { label: string; action: string | null; est_cost_usd: number; estimate_source: string }[];
  evidence: Record<string, unknown>; created_at: string;
};
type Calib = {
  remedy: string; failure_class: number; n: number;
  predicted_usd: number; actual_usd: number; ratio: number | null; corroborated_pct: number;
};
type Overview = {
  class_names: Record<string, string>;
  reasoning: Reasoning[]; ledger: Ledger[]; escalations: Escalation[];
  spend: { productive?: number; unproductive?: number };
  calibration: Calib[];
};

const usd = (micros: number | null | undefined) =>
  micros == null ? "—" : `$${(micros / 1e6).toFixed(2)}`;

export function ShepherdConsole({ initial }: { initial?: Overview | null }) {
  // Server-rendered on first paint. The first version relied entirely on a browser fetch and
  // showed "Loading shepherd…" forever if anything about that call failed — including a CORS
  // block that is invisible outside the browser. Data arrives with the page; the client only
  // refreshes it.
  const [data, setData] = useState<Overview | null>(initial ?? null);
  const [err, setErr] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  // Through a SERVER ACTION, not a browser fetch: the api sends no CORS headers, so a
  // cross-origin call from the console is blocked before it leaves. Every other panel here
  // does the same.
  const load = async () => {
    const d = await getShepherdOverviewAction();
    if (d?.error) setErr(String(d.error));
    else { setData(d as Overview); setErr(null); }
  };
  useEffect(() => {
    load();
    const t = setInterval(load, 20000);
    return () => clearInterval(t);
  }, []);

  const sweep = async () => {
    setBusy(true);
    try { await shepherdSweepAction(); await load(); } finally { setBusy(false); }
  };

  const answer = async (id: string, choice: string) => {
    await shepherdAnswerAction(id, choice);
    await load();
  };

  if (err) return <div className="detail" style={{ color: "#f85149" }}>Shepherd: {err}</div>;
  if (!data) return <div className="detail">Loading shepherd…</div>;

  const prod = data.spend.productive ?? 0;
  const waste = data.spend.unproductive ?? 0;
  const total = prod + waste;
  const wastePct = total ? Math.round((waste / total) * 100) : 0;

  return (
    <div style={{ display: "grid", gap: 18 }}>
      {/* ---- spend, first and unflinching. If waste is not falling, it is not working. ---- */}
      <section className="ct-block">
        <div className="ct-block-title">Spend, last 7 days</div>
        <div style={{ display: "flex", gap: 22, alignItems: "baseline", flexWrap: "wrap" }}>
          <span style={{ color: "#3fb950", fontSize: 20 }}>${prod.toFixed(2)} productive</span>
          <span style={{ color: wastePct > 30 ? "#f85149" : "#d29922", fontSize: 20 }}>
            ${waste.toFixed(2)} produced nothing ({wastePct}%)
          </span>
          <span className="detail">baseline before the shepherd: 58%</span>
        </div>
        <div style={{ height: 8, background: "rgba(255,255,255,.08)", borderRadius: 4, marginTop: 10 }}>
          <div style={{ width: `${100 - wastePct}%`, height: "100%", background: "#3fb950", borderRadius: 4 }} />
        </div>
      </section>

      {/* ---- escalations: one decision-shaped question, never a log dump ---- */}
      {data.escalations.length > 0 && (
        <section className="ct-block">
          <div className="ct-block-title">Needs you — {data.escalations.length}</div>
          <div style={{ display: "grid", gap: 12 }}>
            {data.escalations.map((e) => (
              <div key={e.id} style={{ border: "1px solid rgba(210,153,34,.4)", borderRadius: 8, padding: 12 }}>
                <div style={{ fontWeight: 600 }}>{e.question}</div>
                <div className="detail" style={{ margin: "6px 0" }}>
                  {String((e.evidence as Record<string, string>)?.observed ?? "")}
                </div>
                <div className="detail" style={{ marginBottom: 8 }}>
                  {String((e.evidence as Record<string, string>)?.check ?? "")}
                </div>
                <div style={{ display: "flex", gap: 8, flexWrap: "wrap" }}>
                  {(e.options ?? []).map((o, i) => (
                    <button key={i} className="cap-btn" onClick={() => answer(e.id, o.label)}>
                      {o.label}
                      {o.est_cost_usd > 0 && (
                        <span className="detail" style={{ marginLeft: 6 }}>
                          ~${o.est_cost_usd.toFixed(2)}
                          {/* Never quote a constant as though it were a measurement. */}
                          {o.estimate_source === "prior" ? " (est.)" : ""}
                        </span>
                      )}
                    </button>
                  ))}
                </div>
              </div>
            ))}
          </div>
        </section>
      )}

      {/* ---- the reasoning timeline: the primary artifact ---- */}
      <section className="ct-block">
        <div className="ct-block-title" style={{ display: "flex", justifyContent: "space-between" }}>
          <span>How it is thinking</span>
          <button className="cap-btn" onClick={sweep} disabled={busy}>
            {busy ? "Reasoning…" : "↻ Re-examine failures"}
          </button>
        </div>
        <div style={{ display: "grid", gap: 12 }}>
          {data.reasoning.length === 0 && (
            <div className="detail">No reasoning recorded yet — press “Re-examine failures”.</div>
          )}
          {data.reasoning.map((r) => {
            const confirmed = r.check_confirmed === true;
            const pred = r.predicted_cost_usd_micros ?? 0;
            const actual = r.actual_cost_usd_micros;
            const over = actual != null && pred > 0 && actual > pred * 1.3;
            return (
              <div key={r.id} style={{
                border: `1px solid ${confirmed ? "rgba(63,185,80,.35)" : "rgba(210,153,34,.45)"}`,
                borderRadius: 8, padding: 12,
              }}>
                <div style={{ display: "flex", justifyContent: "space-between", flexWrap: "wrap", gap: 8 }}>
                  <code style={{ fontSize: 12 }}>{r.task_ref.slice(0, 22)} · step {r.step_no}</code>
                  <span className="cap-badge tone-blue">
                    {data.class_names[String(r.failure_class)] ?? `class ${r.failure_class}`}
                  </span>
                </div>
                <Row label="OBSERVE" value={r.observe} />
                <Row label="HYPOTHESIS" value={`${r.hypothesis ?? "—"}  ·  confidence ${
                  r.confidence != null ? Math.round(Number(r.confidence) * 100) : "—"}%`} />
                <Row
                  label="CHECK"
                  value={`${confirmed ? "✓ " : "✗ "}${r.check_result ?? "—"}`}
                  color={confirmed ? "#3fb950" : "#d29922"}
                />
                <Row label="DECIDE" value={
                  r.remedy_chosen
                    ? `${r.remedy_chosen} — ${r.predicted_outcome ?? ""} · predicted ${usd(pred)}`
                    : `stop: ${r.stopped_because ?? "—"}`
                } />
                {r.rationale && <Row label="WHY" value={r.rationale} />}
                {actual != null && (
                  <Row
                    label="OUTCOME"
                    value={`${r.actual_outcome ?? "—"} · actual ${usd(actual)}${
                      over ? "  ⚠ over prediction" : ""}`}
                    color={over ? "#d29922" : undefined}
                  />
                )}
              </div>
            );
          })}
        </div>
      </section>

      {/* ---- ledger: why it kept going, or why it stopped ---- */}
      <section className="ct-block">
        <div className="ct-block-title">Remedy ledger</div>
        <table style={{ width: "100%", fontSize: 13, borderCollapse: "collapse" }}>
          <thead><tr style={{ textAlign: "left", opacity: .7 }}>
            <th>task</th><th>class</th><th>remedy</th><th>status</th><th>cost</th><th>outcome</th>
          </tr></thead>
          <tbody>
            {data.ledger.map((l, i) => (
              <tr key={i} style={{ borderTop: "1px solid rgba(255,255,255,.07)" }}>
                <td><code style={{ fontSize: 11 }}>{l.task_ref.slice(0, 18)}</code></td>
                <td>{data.class_names[String(l.failure_class)] ?? l.failure_class}</td>
                <td>{l.remedy_id}</td>
                <td style={{ color: l.status === "used" ? "#8b949e" : "#3fb950" }}>{l.status}</td>
                <td>{usd(l.cost_usd_micros)}</td>
                <td className="detail">{l.outcome ?? "—"}</td>
              </tr>
            ))}
            {data.ledger.length === 0 && (
              <tr><td colSpan={6} className="detail">nothing attempted yet</td></tr>
            )}
          </tbody>
        </table>
      </section>

      {/* ---- calibration: is it actually reasoning, or guessing confidently? ---- */}
      <section className="ct-block">
        <div className="ct-block-title">Calibration — predicted vs actual</div>
        <div className="detail" style={{ marginBottom: 8 }}>
          A shepherd that consistently under-estimates is pattern-matching, not reasoning.
          Ratios far above 1.0 mean it is starting work it cannot finish.
        </div>
        <table style={{ width: "100%", fontSize: 13, borderCollapse: "collapse" }}>
          <thead><tr style={{ textAlign: "left", opacity: .7 }}>
            <th>remedy</th><th>n</th><th>predicted</th><th>actual</th><th>ratio</th><th>corroborated</th>
          </tr></thead>
          <tbody>
            {data.calibration.map((c, i) => (
              <tr key={i} style={{ borderTop: "1px solid rgba(255,255,255,.07)" }}>
                <td>{c.remedy}</td><td>{c.n}</td>
                <td>${c.predicted_usd.toFixed(2)}</td>
                <td>${c.actual_usd.toFixed(2)}</td>
                <td style={{ color: c.ratio && c.ratio > 1.3 ? "#d29922" : "#3fb950" }}>
                  {c.ratio ?? "—"}
                </td>
                <td>{c.corroborated_pct}%</td>
              </tr>
            ))}
            {data.calibration.length === 0 && (
              <tr><td colSpan={6} className="detail">no remedies proposed yet</td></tr>
            )}
          </tbody>
        </table>
      </section>
    </div>
  );
}

function Row({ label, value, color }: { label: string; value: string; color?: string }) {
  return (
    <div style={{ display: "flex", gap: 10, marginTop: 6, fontSize: 13 }}>
      <span style={{ minWidth: 92, opacity: .55, fontFamily: "monospace", fontSize: 11 }}>{label}</span>
      <span style={{ color }}>{value}</span>
    </div>
  );
}
