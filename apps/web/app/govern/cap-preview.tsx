"use client";

/**
 * "Would have stopped 3 days" (spec §4.4.6): the last 30 days of REAL daily money, and the
 * proposed cap as a dashed line — for a daily cap against each day, for a monthly cap against
 * the month's running total. The count is the server's replay, not the drawing's.
 */
import React from "react";

import { money, type CapPreview as CapPreviewData, type DailySpend } from "../../lib/govern";

const W = 300;
const H = 64;

export function DailyBars({ daily, cap, cumulative, testid = "gv-daily" }: {
  daily: DailySpend[]; cap: number | null; cumulative?: boolean; testid?: string;
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
  const peak = Math.max(1, cap ?? 0, ...series, ...(cumulative ? [] : included.map((v, i) => v + series[i])));
  const bw = W / Math.max(1, daily.length);
  const y = (v: number) => H - (v / peak) * (H - 4);
  return (
    <svg className="gv-chart" viewBox={`0 0 ${W} ${H}`} preserveAspectRatio="none" role="img" data-testid={testid}
         aria-label={`${daily.length} days of spend`}>
      {daily.map((d, i) => {
        const v = series[i];
        return (
          <g key={d.day}>
            <title>{`${d.day} · ${money(d.money_usd_micros)}${cap ? ` · cap ${money(cap)}` : ""}`}</title>
            {!cumulative && included[i] > 0 ? (
              <rect className="gv-chart-included" x={i * bw + 1} width={Math.max(1, bw - 2)}
                    y={y(v + included[i])} height={Math.max(0, y(v) - y(v + included[i]))} />
            ) : null}
            <rect className="gv-chart-bar" data-over={cap !== null && v > cap ? true : undefined}
                  x={i * bw + 1} width={Math.max(1, bw - 2)} y={y(v)} height={Math.max(0, H - y(v))} />
          </g>
        );
      })}
      {cap !== null ? <line className="gv-chart-cap" x1={0} x2={W} y1={y(cap)} y2={y(cap)} /> : null}
    </svg>
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
