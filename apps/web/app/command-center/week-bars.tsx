"use client";

/**
 * THIS WEEK (command-center spec §4.12): seven days of finished and failed runs, and one line of
 * the week's real totals. Never animated.
 *
 * ONE MEANING PER WORD. `runs` are workflow runs (as everywhere on this page), counted over every
 * run in the window; code tasks are `builds`; `model hours` is the time models spent answering
 * (it was labelled agent-hours, which claimed more than the data). Money is the budget's measure.
 */
import React from "react";

import { money } from "../../lib/command-center-graphics";
import type { Week } from "../../lib/command-center-view";

export type WeekBarsProps = { week: Week; tz: string };

const BAR_H = 110;
const DOW = ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"];
const MON = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];

/** `Tue 30 Sep` from a calendar date (already in the viewer's zone: no zone maths here). */
export function dayWords(date: string): string {
  const [y, m, d] = date.split("-").map(Number);
  if (!y || !m || !d) return date;
  const dow = new Date(Date.UTC(y, m - 1, d)).getUTCDay();
  return `${DOW[dow]} ${d} ${MON[m - 1]}`;
}

/** `2.1M` / `850K` / `120`. */
export function tokenWords(n: number): string {
  if (n >= 100_000) return `${(n / 1_000_000).toFixed(1)}M`;
  if (n >= 1_000) return `${Math.round(n / 1_000)}K`;
  return String(n);
}

/** The line's tooltip: the rest of the old delivery strip, with honest labels. */
const count = (n: number, one: string, many: string) => `${n} ${n === 1 ? one : many}`;

export function weekTip(w: Week): string {
  return `${count(w.builds, "build", "builds")} (${w.buildsPassed} passed) · ${count(w.prsOpened, "PR", "PRs")} · `
    + `${count(w.jobs, "job", "jobs")} · ${tokenWords(w.tokens)} tokens · ${count(w.modelHours, "model hour", "model hours")}`;
}

export function WeekBars({ week }: WeekBarsProps) {
  const max = Math.max(1, ...week.days.map((d) => d.completed + d.failed));
  const plural = (n: number, one: string, many: string) => `${n} ${n === 1 ? one : many}`;
  return (
    <div className="cc-week-body" data-testid="cc-week-bars">
      <div className="cc-week-chart" aria-hidden>
        <div className="cc-week-bars">
          {week.days.map((d) => {
            const g = (d.completed / max) * BAR_H;
            const r = (d.failed / max) * BAR_H;
            const n = d.completed + d.failed;
            const tip = `${dayWords(d.date)} · ${d.completed} finished · ${d.failed} failed`;
            return (
              <span key={d.date} className="cc-week-bar" title={tip} data-testid="cc-week-bar">
                <span className="cc-week-n" data-zero={n === 0 || undefined}>{n}</span>
                <span className="cc-week-stack" style={{ height: BAR_H }}>
                  {r > 0 ? <i className="cc-week-failed" style={{ height: `${r}px` }} /> : null}
                  {g > 0 ? <i className="cc-week-done" style={{ height: `${g}px` }} /> : null}
                </span>
                <span className="cc-week-dow">{dayWords(d.date).slice(0, 3)}</span>
              </span>
            );
          })}
        </div>
        <span className="cc-week-legend">
          <span><i className="cc-week-key" data-k="done" />passed</span>
          <span><i className="cc-week-key" data-k="failed" />failed</span>
        </span>
      </div>
      {week.runs === 0 ? (
        <span className="cc-week-line" data-testid="cc-week-line">No runs this week</span>
      ) : (
        <span className="cc-week-line" data-testid="cc-week-line" title={weekTip(week)} tabIndex={0}>
          <span>{plural(week.runs, "run", "runs")}</span>
          <span className="cc-sep" aria-hidden>·</span>
          <span>{`${week.passed} passed`}</span>
          <span className="cc-sep" aria-hidden>·</span>
          <span>{plural(week.deployments, "deploy", "deploys")}</span>
          <span className="cc-sep" aria-hidden>·</span>
          <span>{money(week.moneyMicros)}</span>
        </span>
      )}
      {/* The tooltip's parts, each with its own word explained, for a screen reader. */}
      <ul className="ag-sr wf-sr" data-testid="cc-week-more">
        <li title="Code tasks: an agent edits code and opens a PR">{`${count(week.builds, "build", "builds")} (${week.buildsPassed} passed)`}</li>
        <li>{count(week.prsOpened, "PR", "PRs")}</li>
        <li>{count(week.jobs, "job", "jobs")}</li>
        <li>{`${tokenWords(week.tokens)} tokens`}</li>
        <li title="Time models spent answering">{count(week.modelHours, "model hour", "model hours")}</li>
      </ul>
      <table className="ag-sr wf-sr">
        <caption>Runs by day</caption>
        <thead><tr><th>Day</th><th>Finished</th><th>Failed</th></tr></thead>
        <tbody>
          {week.days.map((d) => (
            <tr key={d.date}><td>{dayWords(d.date)}</td><td>{d.completed}</td><td>{d.failed}</td></tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
