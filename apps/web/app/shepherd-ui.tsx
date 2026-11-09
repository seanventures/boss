"use client";

import { useEffect, useState } from "react";

/**
 * Task Shepherd console.
 *
 * The operator's stated interest is the REASONING, not the actions, "I want to see how the
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

import { callAction } from "../lib/call-action";
import { GatedButton } from "../components/gated";
import { useOnScreen } from "../lib/area-shown";
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

/** An overview, or the reason there isn't one.
 *
 *  `getShepherdOverviewAction` RETURNS `{error}` rather than throwing, so the page's
 *  `.catch(() => null)` never fires and the error object arrives here dressed as data. It is
 *  truthy, so the "still loading" guard passes it through, and the first property read off
 *  `spend` threw, taking the whole console down with a client-side exception because one
 *  endpoint had a bad minute.
 *
 *  A payload is only an Overview if it carries the field the panel is about.
 */
function readOverview(value: unknown): { data: Overview | null; error: string | null } {
  if (!value || typeof value !== "object") return { data: null, error: null };
  const record = value as Record<string, unknown>;
  if (record.error) return { data: null, error: String(record.error) };
  if (!record.spend || typeof record.spend !== "object") {
    // Not an error and not usable. Saying so beats rendering zeros: "productive $0.00" is a
    // claim about the world, and this panel exists to say whether spend is being wasted.
    return { data: null, error: "The shepherd overview came back without a spend summary." };
  }
  return { data: value as Overview, error: null };
}

export function ShepherdConsole({ initial }: { initial?: Overview | null }) {
  // Server-rendered on first paint. The first version relied entirely on a browser fetch and
  // showed "Loading shepherd…" forever if anything about that call failed, including a CORS
  // block that is invisible outside the browser. Data arrives with the page; the client only
  // refreshes it.
  const first = readOverview(initial);
  const [data, setData] = useState<Overview | null>(first.data);
  const [err, setErr] = useState<string | null>(first.error);
  const [busy, setBusy] = useState(false);

  // Through a SERVER ACTION, not a browser fetch: the api sends no CORS headers, so a
  // cross-origin call from the console is blocked before it leaves. Every other panel here
  // does the same.
  const load = async () => {
    // Same reader as the server payload, so a refresh cannot admit a shape the first render
    // rejected. The old `d as Overview` cast was the other half of this bug.
    const next = readOverview(await callAction(getShepherdOverviewAction, undefined, "autopilot:read")());
    if (next.error) setErr(next.error);
    else if (next.data) { setData(next.data); setErr(null); }
  };
  // Off screen, nothing is asked (lib/area-shown.ts); back on screen, it reads at once.
  const onScreen = useOnScreen();
  useEffect(() => {
    if (!onScreen) return;
    load();
    const t = setInterval(load, 20000);
    return () => clearInterval(t);
  }, [onScreen]); // eslint-disable-line react-hooks/exhaustive-deps

  const sweep = async () => {
    setBusy(true);
    try { await callAction(shepherdSweepAction, undefined, "autopilot:operate")(); await load(); } finally { setBusy(false); }
  };

  const answer = async (id: string, choice: string) => {
    await callAction(shepherdAnswerAction, undefined, "autopilot:operate")(id, choice);
    await load();
  };

  if (err) return <div className="detail" style={{ color: "var(--boss-c-f85149)" }}>Shepherd: {err}</div>;
  if (!data) return <div className="detail">Loading shepherd…</div>;

  const prod = data.spend?.productive ?? 0;
  const waste = data.spend?.unproductive ?? 0;
  const total = prod + waste;
  const wastePct = total ? Math.round((waste / total) * 100) : 0;

  return (
    <div style={{ display: "grid", gap: 18 }}>
      {/* ---- spend, first and unflinching. If waste is not falling, it is not working. ---- */}
      <section className="ct-block">
        <div className="ct-block-title">Spend, last 7 days</div>
        <div style={{ display: "flex", gap: 22, alignItems: "baseline", flexWrap: "wrap" }}>
          <span style={{ color: "var(--boss-c-3fb950)", fontSize: 20 }}>${prod.toFixed(2)} productive</span>
          <span style={{ color: wastePct > 30 ? "var(--boss-c-f85149)" : "var(--boss-c-d29922)", fontSize: 20 }}>
            ${waste.toFixed(2)} produced nothing ({wastePct}%)
          </span>
          <span className="detail">baseline before the shepherd: 58%</span>
        </div>
        <div style={{ height: 8, background: "var(--boss-c-0f1c00d7e3)", borderRadius: 4, marginTop: 10 }}>
          <div style={{ width: `${100 - wastePct}%`, height: "100%", background: "var(--boss-c-3fb950)", borderRadius: 4 }} />
        </div>
      </section>

      {/* ---- escalations: one decision-shaped question, never a log dump ---- */}
      {data.escalations.length > 0 && (
        <section className="ct-block">
          <div className="ct-block-title">Needs you, {data.escalations.length}</div>
          <div style={{ display: "grid", gap: 12 }}>
            {data.escalations.map((e) => (
              <div key={e.id} style={{ border: "1px solid var(--boss-c-43864a3bef)", borderRadius: 8, padding: 12 }}>
                <div style={{ fontWeight: 600 }}>{e.question}</div>
                <div className="detail" style={{ margin: "6px 0" }}>
                  {String((e.evidence as Record<string, string>)?.observed ?? "")}
                </div>
                <div className="detail" style={{ marginBottom: 8 }}>
                  {String((e.evidence as Record<string, string>)?.check ?? "")}
                </div>
                <div style={{ display: "flex", gap: 8, flexWrap: "wrap" }}>
                  {(e.options ?? []).map((o, i) => (
                    <GatedButton key={i} permission="autopilot:operate" className="cap-btn" onClick={() => answer(e.id, o.label)}>
                      {o.label}
                      {o.est_cost_usd > 0 && (
                        <span className="detail" style={{ marginLeft: 6 }}>
                          ~${o.est_cost_usd.toFixed(2)}
                          {/* Never quote a constant as though it were a measurement. */}
                          {o.estimate_source === "prior" ? " (est.)" : ""}
                        </span>
                      )}
                    </GatedButton>
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
          <GatedButton permission="autopilot:operate" className="cap-btn" onClick={sweep} disabled={busy}
                  data-testid="shepherd-sweep">
            {busy ? "Reasoning…" : "↻ Re-examine failures"}
          </GatedButton>
        </div>
        <div style={{ display: "grid", gap: 12 }}>
          {data.reasoning.length === 0 && (
            <div className="detail">No reasoning recorded yet, press “Re-examine failures”.</div>
          )}
          {data.reasoning.map((r) => {
            const confirmed = r.check_confirmed === true;
            const pred = r.predicted_cost_usd_micros ?? 0;
            const actual = r.actual_cost_usd_micros;
            const over = actual != null && pred > 0 && actual > pred * 1.3;
            return (
              <div key={r.id} style={{
                border: `1px solid ${confirmed ? "var(--boss-c-95838be7ae)" : "var(--boss-c-4a91cf21f6)"}`,
                borderRadius: 8, padding: 12,
              }}>
                <div style={{ display: "flex", justifyContent: "space-between", flexWrap: "wrap", gap: 8 }}>
                  <code style={{ fontSize: 12 }}>{r.task_ref.slice(0, 22)}, step {r.step_no}</code>
                  <span className="cap-badge tone-blue">
                    {data.class_names[String(r.failure_class)] ?? `class ${r.failure_class}`}
                  </span>
                </div>
                <Row label="OBSERVE" value={r.observe} />
                <Row label="HYPOTHESIS" value={`${r.hypothesis ?? "—"}, confidence ${
                  r.confidence != null ? Math.round(Number(r.confidence) * 100) : "—"}%`} />
                <Row
                  label="CHECK"
                  value={`${confirmed ? "✓ " : "✗ "}${r.check_result ?? "—"}`}
                  color={confirmed ? "#3fb950" : "#d29922"}
                />
                <Row label="DECIDE" value={
                  r.remedy_chosen
                    ? `${r.remedy_chosen}, ${r.predicted_outcome ?? ""}, predicted ${usd(pred)}`
                    : `stop: ${r.stopped_because ?? "—"}`
                } />
                {r.rationale && <Row label="WHY" value={r.rationale} />}
                {actual != null && (
                  <Row
                    label="OUTCOME"
                    value={`${r.actual_outcome ?? "—"}, actual ${usd(actual)}${
                      over ? "   over prediction" : ""}`}
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
              <tr key={i} style={{ borderTop: "1px solid var(--boss-c-bdbe0baeec)" }}>
                <td><code style={{ fontSize: 11 }}>{l.task_ref.slice(0, 18)}</code></td>
                <td>{data.class_names[String(l.failure_class)] ?? l.failure_class}</td>
                <td>{l.remedy_id}</td>
                <td style={{ color: l.status === "used" ? "var(--boss-c-8b949e)" : "var(--boss-c-3fb950)" }}>{l.status}</td>
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
        <div className="ct-block-title">Calibration, predicted vs actual</div>
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
              <tr key={i} style={{ borderTop: "1px solid var(--boss-c-bdbe0baeec)" }}>
                <td>{c.remedy}</td><td>{c.n}</td>
                <td>${c.predicted_usd.toFixed(2)}</td>
                <td>${c.actual_usd.toFixed(2)}</td>
                <td style={{ color: c.ratio && c.ratio > 1.3 ? "var(--boss-c-d29922)" : "var(--boss-c-3fb950)" }}>
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
