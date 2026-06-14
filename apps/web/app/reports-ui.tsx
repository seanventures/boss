"use client";

import { useState, type ReactNode, type SVGProps } from "react";
import type { ReportFeed } from "../lib/reports";

const mk =
  (d: ReactNode) =>
  (props: SVGProps<SVGSVGElement>) =>
    (
      <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth={1.7} strokeLinecap="round" strokeLinejoin="round" aria-hidden {...props}>
        {d}
      </svg>
    );

const I = {
  task: mk(<><path d="M9 5h6a2 2 0 0 1 2 2v12a2 2 0 0 1-2 2H9a2 2 0 0 1-2-2V7a2 2 0 0 1 2-2Z" /><path d="M9 11h6M9 15h4" /></>),
  check: mk(<><circle cx="12" cy="12" r="9" /><path d="M8.5 12.5l2.5 2.5 4.5-5" /></>),
  x: mk(<><circle cx="12" cy="12" r="9" /><path d="M9 9l6 6M15 9l-6 6" /></>),
  gear: mk(<><circle cx="12" cy="12" r="3" /><path d="M12 4v2M12 18v2M4 12h2M18 12h2M6 6l1.5 1.5M16.5 16.5L18 18M18 6l-1.5 1.5M7.5 16.5L6 18" /></>),
  gavel: mk(<><path d="M14 4l6 6-3 3-6-6 3-3Z" /><path d="M11 7l-6 6 3 3 6-6" /><path d="M4 20h8" /></>),
  wallet: mk(<><rect x="3" y="6" width="18" height="13" rx="2" /><path d="M3 10h18" /><circle cx="16.5" cy="14" r="1.2" /></>),
  shield: mk(<><path d="M12 3l7 3v6c0 4-3 7-7 9-4-2-7-5-7-9V6l7-3Z" /></>),
  clock: mk(<><circle cx="12" cy="12" r="9" /><path d="M12 7v5l3 2" /></>),
};

type Win = "today" | "week" | "month";
const WIN_LABEL: Record<Win, string> = { today: "Today", week: "Last 7 days", month: "Last 30 days" };

function money(micros: number): string {
  return new Intl.NumberFormat("en-US", { style: "currency", currency: "USD", maximumFractionDigits: micros < 1_000_000 ? 4 : 2 }).format(micros / 1_000_000);
}
function dur(seconds: number): string {
  if (!seconds) return "—";
  if (seconds < 60) return `${seconds.toFixed(1)}s`;
  if (seconds < 3600) return `${Math.round(seconds / 60)}m`;
  return `${(seconds / 3600).toFixed(1)}h`;
}
function pct(n: number, d: number): number {
  return d === 0 ? 0 : Math.round((n / d) * 100);
}

export function ReportsView({ report }: { report: ReportFeed }) {
  const [win, setWin] = useState<Win>("week");

  if (!report.apiReachable || !report.data) {
    return <p className="detail">{report.blockedReason ?? "Reports are unavailable."}</p>;
  }
  const d = report.data;
  const w = d.windows[win];

  const summary: { label: string; value: string; tone: string; Icon: typeof I.task }[] = [
    { label: "Tasks created", value: String(w.tasks), tone: "accent", Icon: I.task },
    { label: "Jobs run", value: String(w.jobs), tone: "blue", Icon: I.gear },
    { label: "Succeeded", value: String(w.jobs_succeeded), tone: "green", Icon: I.check },
    { label: "Failed", value: String(w.jobs_failed), tone: w.jobs_failed > 0 ? "red" : "muted", Icon: I.x },
    { label: "Decisions", value: String(w.decisions), tone: "amber", Icon: I.gavel },
    { label: "Approved", value: String(w.decisions_approved), tone: "green", Icon: I.check },
    { label: "Spend", value: money(w.spend_usd_micros), tone: w.spend_usd_micros > 0 ? "amber" : "muted", Icon: I.wallet },
  ];

  const maxDay = Math.max(1, ...d.daily.map((x) => x.succeeded + x.failed + x.tasks));

  return (
    <div className="rep">
      {/* window toggle */}
      <div className="rep-toggle">
        {(Object.keys(WIN_LABEL) as Win[]).map((k) => (
          <button key={k} type="button" className={`rep-tab${k === win ? " active" : ""}`} onClick={() => setWin(k)}>
            {WIN_LABEL[k]}
          </button>
        ))}
      </div>

      {/* summary */}
      <div className="wf-stats">
        {summary.map((s) => (
          <div key={s.label} className={`wf-stat tone-${s.tone}`}>
            <span className="wf-stat-ic"><s.Icon /></span>
            <span className="wf-stat-val">{s.value}</span>
            <span className="wf-stat-label">{s.label}</span>
          </div>
        ))}
      </div>

      {/* activity chart */}
      <div className="bx-card wf-pad">
        <div className="wf-section-title">Activity — last 14 days</div>
        <div className="rep-chart">
          {d.daily.map((day) => {
            const total = day.succeeded + day.failed;
            return (
              <div key={day.date} className="rep-col" title={`${day.date} · ${day.tasks} tasks · ${day.succeeded} ok · ${day.failed} failed`}>
                <div className="rep-bar-wrap">
                  {day.failed > 0 ? <span className="rep-seg seg-red" style={{ height: `${(day.failed / maxDay) * 100}%` }} /> : null}
                  {day.succeeded > 0 ? <span className="rep-seg seg-green" style={{ height: `${(day.succeeded / maxDay) * 100}%` }} /> : null}
                  {total === 0 && day.tasks > 0 ? <span className="rep-seg seg-accent" style={{ height: `${(day.tasks / maxDay) * 100}%` }} /> : null}
                </div>
                <span className="rep-col-label">{day.date.slice(5)}</span>
              </div>
            );
          })}
        </div>
        <div className="rep-legend">
          <span><span className="rep-key seg-green" /> Succeeded</span>
          <span><span className="rep-key seg-red" /> Failed</span>
          <span><span className="rep-key seg-accent" /> Tasks (no jobs yet)</span>
        </div>
      </div>

      <div className="rep-split">
        {/* execution by kind */}
        <div className="bx-card wf-pad">
          <div className="wf-section-title">Execution by job kind <span className="wf-count">all time</span></div>
          {d.by_kind.length === 0 ? (
            <p className="detail">No jobs executed yet.</p>
          ) : (
            <div className="rep-table">
              {d.by_kind.map((k) => {
                const rate = pct(k.succeeded, k.total);
                return (
                  <div key={k.kind} className="rep-row">
                    <code className="mono">{k.kind}</code>
                    <span className="rep-rate">
                      <span className="wf-bar"><span className="wf-bar-fill" style={{ width: `${rate}%`, background: rate >= 90 ? "var(--green)" : rate >= 60 ? "var(--amber)" : "var(--red)" }} /></span>
                      <b>{rate}%</b>
                    </span>
                    <span className="rep-cell">{k.total} run{k.total === 1 ? "" : "s"}</span>
                    <span className="rep-cell tone-red">{k.failed > 0 ? `${k.failed} failed` : ""}</span>
                    <span className="rep-cell"><I.clock /> {dur(k.avg_seconds)}</span>
                  </div>
                );
              })}
            </div>
          )}
        </div>

        {/* cost by model */}
        <div className="bx-card wf-pad">
          <div className="wf-section-title">Cost by model <span className="wf-count">all time</span></div>
          {d.by_model.length === 0 ? (
            <p className="detail">No billable model spend recorded yet. This populates once a real model provider (e.g. OpenAI) executes a settled, receipted call.</p>
          ) : (
            <div className="rep-table">
              {d.by_model.map((m) => (
                <div key={m.model} className="rep-row">
                  <code className="mono">{m.model}</code>
                  <span className="rep-cell">{m.count} call{m.count === 1 ? "" : "s"}</span>
                  <span className="rep-cell tone-amber" style={{ marginLeft: "auto", fontWeight: 700 }}>{money(m.spend_usd_micros)}</span>
                </div>
              ))}
            </div>
          )}
          <div className="wf-section-title" style={{ marginTop: 14 }}>Decision latency</div>
          <p className="detail">
            Avg time to approve: <b style={{ color: "var(--ink)" }}>{dur(d.decision_latency.avg_seconds)}</b> across {d.decision_latency.count} approved decision{d.decision_latency.count === 1 ? "" : "s"}.
          </p>
        </div>
      </div>

      {/* agent scorecard */}
      <div className="bx-card wf-pad">
        <div className="wf-section-title"><span className="wf-ic tone-accent"><I.shield /></span> Agent scorecard <span className="wf-count">{d.agents.length} agents</span></div>
        <p className="detail" style={{ marginTop: -4 }}>Ranked by violations, then lowest trust — so problem agents surface first. Populates as governance reviews are recorded.</p>
        <div className="rep-agents">
          <div className="rep-agent-head">
            <span>Agent</span><span>Trust</span><span>Autonomy</span><span>Runs</span><span>Violations</span><span>Last review</span>
          </div>
          {d.agents.slice(0, 12).map((a) => {
            const viol = a.policy_violations + a.scope_violations;
            const trustTone = a.trust_score >= 70 ? "green" : a.trust_score >= 40 ? "amber" : "red";
            return (
              <div key={a.role_key} className="rep-agent">
                <span className="rep-agent-name">{a.display_name}<span className="mono"> {a.role_key}</span></span>
                <span><span className={`wf-pill tone-${trustTone}`}>{a.trust_score}</span></span>
                <span>L{a.autonomy_level}</span>
                <span>{a.successful_runs}</span>
                <span className={viol > 0 ? "tone-red" : "tone-muted"}>{viol > 0 ? `${viol}` : "0"}</span>
                <span className="tone-muted">{a.last_reviewed_at ? new Date(a.last_reviewed_at).toLocaleDateString() : "never"}</span>
              </div>
            );
          })}
        </div>
      </div>
    </div>
  );
}
