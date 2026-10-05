"use client";

/**
 * THE PULSE (cc v2): the team's last 14 weeks and its latest moves — both served by the API
 * (`view.pulse`, aggregated in SQL from the acts ledger), never counted here.
 *
 *   Left: a day-by-day heatmap of finished agent steps (Monday-first columns), the totals line
 *   (steps · PRs open · busiest day) and the five most active agents.
 *   Right: the seven latest moves, one per app — "Atlas planned Interview Scorecard".
 *
 * A chart only where there is data: no steps in 14 weeks is one quiet line, not an empty grid.
 */
import React from "react";

import type { Pulse, PulseEvent, Week } from "../../lib/command-center-view";
import { AgentAvatar } from "../agent-avatar";
import { nameOf } from "../agents/run-face";
import { ago, navigate, plainClick, runPatch } from "../agents/live-util";
import { Go, whoLine } from "./hero";

const MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];

/** `2026-09-03` → its UTC date parts (the server's dates are already the viewer's days). */
function parts(date: string): { y: number; m: number; d: number; dow: number } {
  const t = Date.parse(`${date}T00:00:00Z`);
  const x = new Date(Number.isFinite(t) ? t : 0);
  return { y: x.getUTCFullYear(), m: x.getUTCMonth(), d: x.getUTCDate(), dow: (x.getUTCDay() + 6) % 7 };
}

export function dayWord(date: string): string {
  const p = parts(date);
  return `${MONTHS[p.m]} ${p.d}`;
}

/** Level 0-4 for a count against the busiest day. */
export function level(n: number, peak: number): number {
  if (n <= 0 || peak <= 0) return 0;
  return Math.min(4, 1 + Math.floor((3 * n) / peak));
}

export function Heatmap({ pulse }: { pulse: Pulse }) {
  const peak = pulse.busiest?.n ?? Math.max(0, ...pulse.days.map((d) => d.n));
  const first = pulse.days[0] ? parts(pulse.days[0].date).dow : 0;
  const weeks = Math.max(1, Math.ceil((first + pulse.days.length) / 7));
  // Month labels at the column where each month starts.
  const months: { col: number; word: string }[] = [];
  let last = -1;
  pulse.days.forEach((d, i) => {
    const p = parts(d.date);
    const col = Math.floor((first + i) / 7);
    if (p.m !== last) {
      last = p.m;
      // A LABEL NEVER TOUCHES ITS NEIGHBOUR (independent test, 390px): one that would start
      // within two weeks of the previous is dropped, the later month kept.
      const prev = months[months.length - 1];
      if (prev && col - prev.col < 2) months.pop();
      months.push({ col, word: MONTHS[p.m] });
    }
  });
  return (
    <div className="cc-heat-wrap" style={{ ["--weeks" as string]: weeks }}>
      <div className="cc-heat-months" aria-hidden>
        {months.map((m) => <span key={`${m.col}-${m.word}`} style={{ gridColumn: m.col + 1 }}>{m.word}</span>)}
      </div>
      <div className="cc-heat-body">
        <div className="cc-heat-days" aria-hidden><span>M</span><span /><span>W</span><span /><span>F</span><span /><span /></div>
        <div className="cc-heat" role="img" data-testid="cc-heat"
             aria-label={`${pulse.total} agent steps in ${pulse.weeks} weeks${pulse.busiest ? `; busiest ${dayWord(pulse.busiest.date)} with ${pulse.busiest.n}` : ""}`}>
          {first ? <i className="pad" style={{ gridRow: `1 / span ${first}` }} /> : null}
          {pulse.days.map((d) => (
            <i key={d.date} className={`l${level(d.n, peak)}`} title={`${dayWord(d.date)}: ${d.n} agent step${d.n === 1 ? "" : "s"}`} />
          ))}
        </div>
      </div>
    </div>
  );
}

const VERDICT: Record<string, string> = { approved: " — approved", changes_asked: " — asked for changes" };

function Event({ e, now, orgId }: { e: PulseEvent; now: number | null; orgId?: string | null }) {
  const who = nameOf(e.roleKey);
  const role = who.role && who.role.toLowerCase() !== who.name.toLowerCase() ? who.role : "";
  const when = ago(e.at, now);
  const body = (
    <>
      <span className="cc-ev-face"><AgentAvatar agent={e.roleKey} size={30} aiBadge={false} /></span>
      <span className="cc-ev-text">
        <span><b>{who.name}</b> {e.verb} <b data-user-text>{e.subject}</b>{e.verdict ? VERDICT[e.verdict] : ""}</span>
        <span className="cc-ev-sub">{[when && `${when} ago`, role].filter(Boolean).join(" · ")}</span>
      </span>
    </>
  );
  if (e.url) {
    return <a className="cc-ev" href={e.url} target="_blank" rel="noreferrer" data-testid="cc-ev">{body}</a>;
  }
  if ((e.kind === "run" || e.kind === "made") && e.runId && e.runId.startsWith("wfr_")) {
    const runId = e.runId;
    return (
      <a className="cc-ev" href={`/?${new URLSearchParams({ ...(orgId ? { org: orgId } : {}), area: "delivery_workflows", wfr: runId })}`}
         data-testid="cc-ev" onClick={(ev) => { if (plainClick(ev)) { ev.preventDefault(); navigate(runPatch(runId)); } }}>
        {body}
      </a>
    );
  }
  return (
    <Go className="cc-ev" patch={e.runId ? { area: "code_tasks", view: "apps", build: e.runId, role: null }
                                      : { area: "workforce", view: "activity", role: null }} orgId={orgId} testId="cc-ev">
      {body}
    </Go>
  );
}

const n = (v: number, one: string, many: string) => `${v.toLocaleString("en-US")} ${v === 1 ? one : many}`;
const big = (v: number) => (v >= 1_000_000 ? `${(v / 1_000_000).toFixed(1)}M` : v >= 1000 ? `${Math.round(v / 1000)}k` : String(v));

/** The week's delivery line (the old THIS WEEK strip), whole, as one tooltip. */
export function weekLine(w: Week): string {
  return [n(w.runs, "run", "runs") + ` (${w.passed} passed)`, `${n(w.builds, "build", "builds")} (${w.buildsPassed} passed)`,
          n(w.prsOpened, "PR opened", "PRs opened"), n(w.jobs, "job", "jobs"), n(w.deployments, "deploy", "deploys"),
          `${big(w.tokens)} tokens`, `${w.modelHours.toFixed(1)} model hours`].join(" · ");
}

export function PulseRow({ pulse, week, prsOpen, now, orgId }: {
  pulse: Pulse | null; week?: Week | null; prsOpen: number | null; now: number | null; orgId?: string | null;
}) {
  if (!pulse) return null;
  const top = pulse.top.slice(0, 5);
  const most = top[0]?.n ?? 1;
  return (
    <section className="cc-pulse" aria-label="Your team's pulse" data-testid="cc-pulse">
      <div className="cc-card">
        <header className="cc-card-h">
          <h2>Your team&apos;s last {pulse.weeks} weeks</h2>
          <span className="cc-spacer" />
          <Go className="cc-link" patch={{ area: "workforce", view: "activity", role: null }} orgId={orgId}
              testId="cc-activity-link">Activity log ↗</Go>
        </header>
        {pulse.total === 0 ? (
          <div className="cc-quiet-line" data-testid="cc-pulse-empty"><span>No agent work yet</span></div>
        ) : (
          <>
            <Heatmap pulse={pulse} />
            <div className="cc-heat-foot" data-testid="cc-heat-foot">
              <span><b>{pulse.total.toLocaleString("en-US")}</b>agent steps</span>
              {prsOpen !== null ? <span><b>{prsOpen}</b>PRs open</span> : null}
              {pulse.busiest ? <span>Busiest <b className="cc-busy">{dayWord(pulse.busiest.date)} · {pulse.busiest.n} steps</b></span> : null}

              <span className="cc-heat-legend" aria-hidden>Less <i className="l0" /><i className="l1" /><i className="l2" /><i className="l3" /><i className="l4" /> More</span>
            </div>
            {week ? (
              // THIS WEEK, REACHABLE BY TOUCH (parity b): a line you open, not a tooltip.
              <details className="cc-week" data-testid="cc-week">
                <summary>
                  This week: <b>{week.passed}</b> finished
                  {week.failed ? <> · <b className="cc-failed">{week.failed}</b> failed</> : null}
                </summary>
                <p data-testid="cc-week-line" aria-label={weekLine(week)}>
                  {weekLine(week).split(" · ").map((part) => <span key={part}>{part}</span>)}
                </p>
              </details>
            ) : null}
            {top.length ? (
              <>
                <div className="cc-top-h">Most active agents</div>
                <div className="cc-tops" data-testid="cc-tops">
                  {top.map((t) => {
                    const who = nameOf(t.roleKey);
                    return (
                      <Go key={t.roleKey} className="cc-top-row" patch={{ area: "workforce", role: t.roleKey, view: null }}
                          orgId={orgId} title={`${whoLine(t.roleKey)} · ${t.n} steps`}>
                        <span className="cc-ev-face"><AgentAvatar agent={t.roleKey} size={30} aiBadge={false} /></span>
                        <span className="cc-top-name">{who.name}{who.role.toLowerCase() !== who.name.toLowerCase() ? <small>{who.role}</small> : null}</span>
                        <span className="cc-bar" aria-hidden><i style={{ width: `${Math.max(2, (100 * t.n) / most)}%` }} /></span>
                        <b>{t.n}</b>
                      </Go>
                    );
                  })}
                </div>
              </>
            ) : null}
          </>
        )}
      </div>
      <div className="cc-card">
        <header className="cc-card-h">
          <h2>Latest from your team</h2>
          <span className="cc-spacer" />
          <Go className="cc-link" patch={{ area: "code_tasks", view: "apps", build: null, role: null }} orgId={orgId}
              testId="cc-latest-factory">Factory ↗</Go>
        </header>
        {pulse.latest.length === 0 ? (
          <div className="cc-quiet-line"><span>Nothing yet</span></div>
        ) : (
          <div className="cc-feed" data-testid="cc-feed">
            {pulse.latest.slice(0, 7).map((e) => <Event key={`${e.runId}-${e.at}`} e={e} now={now} orgId={orgId} />)}
          </div>
        )}
      </div>
    </section>
  );
}
