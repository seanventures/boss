"use client";

/**
 * "Would have stopped 3 days" (spec §4.4.6): the last 30 days of REAL daily money, and the
 * proposed cap as a dashed line, for a daily cap against each day, for a monthly cap against
 * the month's running total. The count is the server's replay, not the drawing's.
 *
 * The bars are scaled to the DATA (D15): a cap far above every day is a reference, not the
 * scale, drawn at the top edge and named beside the chart, so $3 days are not flat lines
 * under a $252 cap.
 */
import React from "react";

import { money, type CapPreview as CapPreviewData, type DailySpend } from "../../lib/govern";
import { plural } from "../../lib/factory-floor";

const W = 300;
const H = 64;

export function DailyBars({ daily, cap, cumulative, testid = "gv-daily", highlight, capLabel, capOnScaleAlways }: {
  daily: DailySpend[]; cap: number | null; cumulative?: boolean; testid?: string;
  /** A day (YYYY-MM-DD) drawn as the current one (Govern › Spend: today). */
  highlight?: string;
  /** Names the dashed cap line beside the chart (`daily cap $100`). */
  capLabel?: string;
  /** Draw the cap line at its real height even far above the bars (Govern › Spend). */
  capOnScaleAlways?: boolean;
}) {
  const series = cumulative
    ? daily.reduce<number[]>((acc, d, i) => {
      const month = d.day.slice(0, 7);
      const prev = i > 0 && daily[i - 1].day.slice(0, 7) === month ? acc[i - 1] : 0;
      acc.push(prev + d.money_usd_micros);
      return acc;
    }, [])
    : daily.map((d) => d.money_usd_micros);
  const included = daily.map((d) => d.included_usd_micros);
  const dataPeak = Math.max(1, ...series, ...(cumulative ? [] : included.map((v, i) => v + series[i])));
  // A cap near the data joins the scale; one far above it stays a reference at the top edge.
  const capOnScale = cap !== null && (Boolean(capOnScaleAlways) || cap <= dataPeak * 1.5);
  const peak = capOnScale ? Math.max(dataPeak, cap ?? 0) : dataPeak * 1.1;
  const bw = W / Math.max(1, daily.length);
  const y = (v: number) => H - (Math.min(v, peak) / peak) * (H - 4);
  const chart = (
    <svg className="gv-chart" viewBox={`0 0 ${W} ${H}`} preserveAspectRatio="none" role="img" data-testid={testid}
         aria-label={`${plural(daily.length, "day")} of spend`}>
      {daily.map((d, i) => {
        const v = series[i];
        return (
          <g key={d.day}>
            <title>{`${d.day}, ${money(d.money_usd_micros)}${cap ? `, cap ${money(cap)}` : ""}`}</title>
            {!cumulative && included[i] > 0 ? (
              <rect className="gv-chart-included" x={i * bw + 1} width={Math.max(1, bw - 2)}
                    y={y(v + included[i])} height={Math.max(0, y(v) - y(v + included[i]))} />
            ) : null}
            <rect className="gv-chart-bar" data-over={cap !== null && v > cap ? true : undefined}
                  data-today={highlight && d.day === highlight ? true : undefined}
                  x={i * bw + 1} width={Math.max(1, bw - 2)}
                  y={v > 0 ? Math.min(y(v), H - 1) : y(v)} height={v > 0 ? Math.max(1, H - y(v)) : Math.max(0, H - y(v))} />
          </g>
        );
      })}
      {cap !== null && capOnScale ? <line className="gv-chart-cap" x1={0} x2={W} y1={y(cap)} y2={y(cap)} /> : null}
    </svg>
  );
  if (cap === null) return chart;
  if (capOnScale) {
    if (!capLabel) return chart;
    return (
      <span className="gv-chart-wrap">
        <span className="gv-chart-note gv-chart-caplabel" data-testid={`${testid}-cap-label`}>{capLabel}</span>
        {chart}
      </span>
    );
  }
  return (
    <span className="gv-chart-wrap">
      {chart}
      <span className="gv-chart-note" data-testid={`${testid}-cap-above`} title="Bars are scaled to the spend, not the cap">
        {capLabel ? `${capLabel} ↑` : `Cap ${money(cap)} ↑`}
      </span>
    </span>
  );
}

export function CapPreview({ preview }: { preview: CapPreviewData }) {
  if (preview.not_enough_history) {
    return <span className="gv-preview-line" data-testid="gv-would-stop" title="Replays the last 30 days of real spend">Not enough history</span>;
  }
  return (
    <span className="gv-preview" data-testid="gv-cap-preview">
      <DailyBars daily={preview.daily} cap={preview.cap_usd_micros} cumulative={preview.period === "month"} testid="gv-preview-chart" />
      <span className="gv-preview-line" data-testid="gv-would-stop" title="Replays the last 30 days of real spend">
        {preview.days_stopped ? `Would have stopped ${preview.days_stopped} day${preview.days_stopped === 1 ? "" : "s"}` : "Would have stopped none"}
      </span>
    </span>
  );
}
