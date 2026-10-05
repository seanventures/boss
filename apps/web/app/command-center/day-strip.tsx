"use client";

/**
 * LAST 24H (command-center spec §4.10): a state timeline, one bar per run that started in the
 * window or is still live. A bar runs from start to finish (or to the now line while live), with
 * a tick per finished step, an amber span for each wait on you, and an end cap whose SHAPE says
 * how it ended (circle done, square failed, hollow cancelled, ▶ live) so colour is never alone.
 *
 * Drawn in HTML, not SVG: every bar is a real link with a real tooltip and a full aria-label,
 * which an SVG <a> cannot give a mouse user. Positions are percentages of the window, so the
 * strip needs no measuring and the server's first paint is the client's.
 */
import React, { useRef } from "react";

import type { Day, DayRow, MotionMode } from "../../lib/command-center-view";
import { linkTo, navigate, plainClick, runPatch, span } from "../agents/live-util";

export type DayStripProps = {
  day: Day;
  /** The client clock (ms), null before mount (SSR draws to the view's generated_at). */
  nowMs: number | null;
  /** The server's `generated_at`, the right edge before the client clock exists. */
  generatedAt: string;
  /** 24 at ≥ 640px, 12 on a phone. */
  windowHours: 24 | 12;
  /** 10 at ≥ 1440px, 8 below. */
  rowsMax: number;
  motion: MotionMode;
  /** The run hovered here or on its MissionRow (one surface seen two ways). */
  hoverRunId: string | null;
  onHover: (runId: string | null) => void;
  orgId?: string | null;
  /** `Start work ↗` on the empty strip: focus the composer. */
  onStartWork: () => void;
};

const LIVE = new Set(["running", "waiting_you", "blocked"]);
/** A bar this wide (px at the 1240px column, ≈ 11% of the window) carries its name inside. */
const NAME_INSIDE = 0.12;

const OUTCOME: Record<string, string> = {
  completed: "Finished", failed: "Failed", cancelled: "Cancelled", running: "Running",
  waiting_you: "Waiting on you", blocked: "Blocked",
};

/** `<run> · <workflow> · step k of n · <verb or outcome> · 42m`: the bar's tooltip and label. */
export function barTip(row: DayRow, nowMs: number): string {
  const start = Date.parse(row.startedAt);
  const end = row.finishedAt ? Date.parse(row.finishedAt) : nowMs;
  const took = Number.isFinite(start) && Number.isFinite(end) ? span((end - start) / 1000) : "";
  const word = LIVE.has(row.state) ? (row.verb || OUTCOME[row.state]) : (OUTCOME[row.state] ?? row.state);
  return [row.name, row.workflowName && row.workflowName !== row.name ? row.workflowName : "",
          row.stepCount ? `step ${row.stepIndex} of ${row.stepCount}` : "", word, took]
    .filter(Boolean).join(" · ");
}

export function DayStrip({ day, nowMs, generatedAt, windowHours, rowsMax, motion, hoverRunId, onHover, orgId, onStartWork }: DayStripProps) {
  const now = nowMs ?? Date.parse(generatedAt);
  const since = now - windowHours * 3_600_000;
  const pct = (ms: number) => ((Math.min(Math.max(ms, since), now) - since) / (now - since)) * 100;
  const rows = day.rows.filter((r) => {
    const end = r.finishedAt ? Date.parse(r.finishedAt) : now;
    return LIVE.has(r.state) || end >= since;
  });
  const shown = rows.slice(0, rowsMax);
  const more = rows.length - shown.length + day.more;

  // What changed since the last poll: new ticks fade in, a cap that just ended pulses once.
  const seen = useRef<Map<string, { ticks: number; state: string }> | null>(null);
  const prev = seen.current;
  seen.current = new Map(rows.map((r) => [r.runId, { ticks: r.ticks.length, state: r.state }]));
  const animate = motion === "full" && prev !== null;

  if (rows.length === 0) {
    return (
      <div className="cc-day" data-testid="cc-day-strip" data-empty="true">
        <div className="cc-day-empty">
          <span>No runs in 24h</span>
          <button type="button" className="ag-link" onClick={onStartWork} data-testid="cc-day-start">Start work ↗</button>
        </div>
      </div>
    );
  }

  return (
    <div className="cc-day" data-testid="cc-day-strip" data-window={windowHours} data-motion={motion}>
      <div className="cc-day-rows" onMouseLeave={() => onHover(null)}>
        {shown.map((r) => {
          const start = Date.parse(r.startedAt);
          const end = r.finishedAt ? Date.parse(r.finishedAt) : now;
          const live = LIVE.has(r.state) && !r.finishedAt;
          const left = pct(start);
          const right = pct(end);
          const width = Math.max(0.6, right - left);
          const clipped = Number.isFinite(start) && start < since;
          const tip = barTip(r, now);
          const before = prev?.get(r.runId);
          const capNew = animate && before !== undefined && LIVE.has(before.state) && !LIVE.has(r.state);
          const newTicks = animate && before !== undefined ? Math.max(0, r.ticks.length - before.ticks) : 0;
          const local = (ms: number) => ((ms - Math.max(start, since)) / Math.max(1, end - Math.max(start, since))) * 100;
          const cap = live ? "live" : r.state === "completed" ? "done" : r.state === "cancelled" ? "cancelled" : "failed";
          return (
            <div key={r.runId} className="cc-day-row">
              <a className="cc-day-bar" href={linkTo(runPatch(r.runId), orgId)} data-testid={`cc-day-bar-${r.runId}`}
                 data-run-id={r.runId} data-state={r.state} data-cap={cap} data-hover={hoverRunId === r.runId || undefined}
                 title={tip} aria-label={tip}
                 style={{ left: `${left}%`, width: `${width}%` }}
                 onMouseEnter={() => onHover(r.runId)} onFocus={() => onHover(r.runId)} onBlur={() => onHover(null)}
                 onClick={(e) => { if (plainClick(e)) { e.preventDefault(); navigate(runPatch(r.runId)); } }}>
                {clipped ? <span className="cc-day-clip" aria-hidden>‹</span> : null}
                {r.gates.map((g, i) => {
                  const gs = Date.parse(g.from), ge = g.to ? Date.parse(g.to) : end;
                  if (!Number.isFinite(gs) || ge < since) return null;
                  const l = Math.max(0, local(gs)), w = Math.max(0.8, Math.min(100, local(ge)) - l);
                  const waited = span((ge - gs) / 1000);
                  return <span key={i} className="cc-day-gate" data-testid="cc-day-gate" title={`Waited for you ${waited}`}
                               style={{ left: `${l}%`, width: `${w}%` }} />;
                })}
                {r.ticks.map((t, i) => {
                  const ms = Date.parse(t);
                  if (!Number.isFinite(ms) || ms < since || ms > end) return null;
                  return <span key={t + i} className="cc-day-tick" data-new={i >= r.ticks.length - newTicks || undefined}
                               style={{ left: `${local(ms)}%` }} aria-hidden />;
                })}
                {width >= NAME_INSIDE * 100 ? <span className="cc-day-name" data-user-text>{r.name}</span> : null}
                <span className="cc-day-cap" data-cap={cap} data-new={capNew || undefined} aria-hidden>
                  {cap === "live" ? "▶" : null}
                </span>
              </a>
            </div>
          );
        })}
        <span className="cc-day-now" aria-hidden><span>now</span></span>
      </div>
      {more > 0 ? (
        <a className="cc-day-more ag-link" href={linkTo({ area: "delivery_workflows", view: null, role: null }, orgId)}
           data-testid="cc-day-more"
           onClick={(e) => { if (plainClick(e)) { e.preventDefault(); navigate({ area: "delivery_workflows", view: null, role: null }); } }}>
          {`+${more} more`}
        </a>
      ) : null}
    </div>
  );
}
