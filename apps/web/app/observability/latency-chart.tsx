"use client";

/**
 * The app drawer's latency chart (spec §4.9 ③, §5.4): hand-built inline SVG over the window's
 * bucketed samples (≤ 288 points from the detail view).
 *
 *   p95 as the line, avg as a faint line (both labelled inline, no legend box), a red tick on the
 *   baseline for every bucket with a failed probe, each deploy as a vertical rule, each incident
 *   as a shaded band, y in ms, x in local time. A bucket with no probes breaks the lines, a gap
 *   is drawn as a gap. Hover or arrow keys move a crosshair whose tooltip says the bucket's truth;
 *   `View as table` lists every bucket (§7). Charts redraw; they never animate.
 */
import React, { useMemo, useState } from "react";

import type { Incident, Pin, SeriesPoint } from "../../lib/obs-view";
import { ms } from "../../lib/obs-view";
import { TableToggle } from "../infra/kit";
import { useLocalTime } from "../../lib/local-time";

const W = 560;
const H = 168;
const L = 44;   // y labels
const R = 30;   // inline line labels
const T = 10;
const B = 22;   // x labels
const PW = W - L - R;
const PH = H - T - B;


/** A round top for the y axis: 212 → 250, 2400 → 2500, 18400 → 20000. */
export function niceMax(v: number): number {
  if (!(v > 0)) return 100;
  const p = 10 ** Math.floor(Math.log10(v));
  for (const m of [1, 1.5, 2, 2.5, 3, 4, 5, 6, 8, 10]) if (m * p >= v) return m * p;
  return 10 * p;
}

/** The SVG path of a series, broken wherever a bucket has no value. */
export function linePath(xs: number[], ys: (number | null)[]): string {
  let d = "";
  let pen = false;
  ys.forEach((y, i) => {
    if (y === null) { pen = false; return; }
    d += `${pen ? "L" : "M"}${xs[i].toFixed(1)},${y.toFixed(1)}`;
    pen = true;
  });
  return d;
}

export function LatencyChart({ series, pins, incidents, fromIso, toIso, testId = "obs-latency-chart", label }: {
  series: SeriesPoint[]; pins: Pin[]; incidents: Incident[]; fromIso: string | null; toIso: string | null; testId?: string; label: string;
}) {
  const lt = useLocalTime();
  const hhmm = lt.hhmm;
  const dayhm = (t: number, long: boolean) => (long ? lt.day(t) : lt.hhmm(t));
  const [table, setTable] = useState(false);
  const [hover, setHover] = useState<number | null>(null);
  const geo = useMemo(() => {
    const times = series.map((p) => Date.parse(p.at));
    const from = fromIso ? Date.parse(fromIso) : times[0];
    const lastStep = times.length > 1 ? times[times.length - 1] - times[times.length - 2] : 0;
    const to = toIso ? Date.parse(toIso) : (times[times.length - 1] ?? 0) + lastStep;
    const spanMs = Math.max(1, to - from);
    const x = (t: number) => L + Math.min(PW, Math.max(0, ((t - from) / spanMs) * PW));
    const step = series.length ? PW / series.length : PW;
    const xs = series.map((_, i) => L + step * (i + 0.5));
    const top = niceMax(Math.max(0, ...series.map((p) => Math.max(p.p95 ?? 0, p.avg ?? 0))));
    const y = (v: number) => T + PH - (v / top) * PH;
    const long = spanMs > 36 * 3600_000;
    return { from, to, spanMs, x, xs, step, top, y, long };
  }, [series, fromIso, toIso]);

  const p95 = series.map((p) => (p.p95 === null ? null : geo.y(p.p95)));
  const avg = series.map((p) => (p.avg === null ? null : geo.y(p.avg)));
  const lastP95 = [...p95].reverse().find((v) => v !== null) ?? null;
  const lastAvg = [...avg].reverse().find((v) => v !== null) ?? null;
  const yTicks = [0, 0.5, 1].map((k) => geo.top * k);
  const xTicks = [0, 0.25, 0.5, 0.75, 1].map((k) => geo.from + geo.spanMs * k);
  const failed = series.filter((p) => p.n > 0 && p.ok < p.n).length;
  const summary = `${label} latency, ${series.length} buckets: ${failed} with failed probes, ${incidents.length} incidents, ${pins.length} deploys`;
  const h = hover === null ? null : series[hover];

  const onMove = (e: React.MouseEvent<SVGSVGElement>) => {
    const box = e.currentTarget.getBoundingClientRect();
    if (!box.width || !series.length) return;
    const vx = ((e.clientX - box.left) / box.width) * W;
    const i = Math.floor((vx - L) / geo.step);
    setHover(i >= 0 && i < series.length ? i : null);
  };
  const onKey = (e: React.KeyboardEvent) => {
    if (!series.length) return;
    if (e.key === "ArrowRight" || e.key === "ArrowLeft") {
      e.preventDefault();
      const d = e.key === "ArrowRight" ? 1 : -1;
      setHover((v) => Math.min(series.length - 1, Math.max(0, (v ?? (d > 0 ? -1 : series.length)) + d)));
    } else if (e.key === "Escape") setHover(null);
  };
  const tip = h ? `${dayhm(Date.parse(h.at), geo.long)}, ${h.n ? `${h.ok} of ${h.n} ok` : "No probes"}${h.p95 !== null ? `, p95 ${ms(h.p95)}` : ""}${h.avg !== null ? `, avg ${ms(h.avg)}` : ""}` : "";
  const tipW = Math.max(120, tip.length * 5.6);
  const tipX = hover === null ? 0 : Math.min(W - tipW - 2, Math.max(L, geo.xs[hover] - tipW / 2));

  return (
    <div className="obs-chart" data-testid={testId}>
      <div className="obs-chart-bar">
        <span className="ag-spacer" />
        <TableToggle on={table} onToggle={() => setTable((v) => !v)} testId={`${testId}-table-toggle`} />
      </div>
      {table ? (
        <table className="inf-table" data-testid={`${testId}-table`}>
          <thead><tr><th scope="col">From</th><th scope="col">Probes</th><th scope="col">OK</th><th scope="col">p95</th><th scope="col">Avg</th></tr></thead>
          <tbody>
            {series.map((p) => (
              <tr key={p.at} data-k={p.n === 0 ? "gap" : p.ok < p.n ? (p.ok === 0 ? "none" : "some") : "ok"}>
                <td>{dayhm(Date.parse(p.at), geo.long)}</td><td>{p.n}</td><td>{p.n ? p.ok : "—"}</td>
                <td>{ms(p.p95)}</td><td>{ms(p.avg)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      ) : (
        <svg className="obs-chart-svg" viewBox={`0 0 ${W} ${H}`} role="img" aria-label={summary} tabIndex={0}
             onMouseMove={onMove} onMouseLeave={() => setHover(null)} onKeyDown={onKey} onBlur={() => setHover(null)}>
          {/* incidents: shaded bands */}
          {incidents.map((inc, i) => {
            const s = Date.parse(inc.start);
            const e = inc.end ? Date.parse(inc.end) : geo.to;
            if (!Number.isFinite(s)) return null;
            const x0 = geo.x(s);
            const x1 = Math.max(x0 + 2, geo.x(e));
            return <rect key={`inc-${i}`} className="obs-chart-incident" data-ongoing={inc.end ? undefined : "1"} x={x0} y={T} width={x1 - x0} height={PH}><title>{inc.end ? `Incident, ${hhmm(s)}, ${hhmm(e)}` : `Ongoing since ${hhmm(s)}`}</title></rect>;
          })}
          {/* grid + y axis */}
          {yTicks.map((v) => (
            <g key={`y-${v}`}>
              <line className="obs-chart-grid" x1={L} x2={L + PW} y1={geo.y(v)} y2={geo.y(v)} />
              <text className="obs-chart-axis" x={L - 6} y={geo.y(v) + 3} textAnchor="end">{v === 0 ? "0" : ms(v)}</text>
            </g>
          ))}
          {xTicks.map((t, i) => (
            <text key={`x-${i}`} className="obs-chart-axis" x={geo.x(t)} y={H - 6}
                  textAnchor={i === 0 ? "start" : i === 4 ? "end" : "middle"}>{i === 4 ? "now" : dayhm(t, geo.long)}</text>
          ))}
          {/* deploys: vertical rules */}
          {pins.map((p, i) => {
            const t = Date.parse(p.at);
            if (!Number.isFinite(t)) return null;
            return <line key={`pin-${i}`} className="obs-chart-pin" data-tone={p.tone} x1={geo.x(t)} x2={geo.x(t)} y1={T} y2={T + PH}><title>{`Deployed ${p.sha7 ?? "—"}, ${hhmm(t)}, ${p.word}`}</title></line>;
          })}
          {/* the lines */}
          <path className="obs-chart-avg" d={linePath(geo.xs, avg)} />
          <path className="obs-chart-p95" d={linePath(geo.xs, p95)} />
          {lastP95 !== null ? <text className="obs-chart-lbl" x={L + PW + 4} y={lastP95 + 3}>p95</text> : null}
          {lastAvg !== null ? <text className="obs-chart-lbl obs-chart-lbl-faint" x={L + PW + 4} y={lastAvg + (lastP95 !== null && Math.abs(lastAvg - lastP95) < 10 ? 12 : 3)}>avg</text> : null}
          {/* failed buckets: red ticks on the baseline */}
          {series.map((p, i) => (p.n > 0 && p.ok < p.n ? (
            <line key={`f-${i}`} className="obs-chart-fail" data-testid="obs-chart-fail" x1={geo.xs[i]} x2={geo.xs[i]} y1={T + PH} y2={T + PH - 7} />
          ) : null))}
          <line className="obs-chart-base" x1={L} x2={L + PW} y1={T + PH} y2={T + PH} />
          {/* hover */}
          {h && hover !== null ? (
            <g className="obs-chart-hover" role="tooltip" data-testid="obs-chart-hover">
              <line className="obs-chart-cross" x1={geo.xs[hover]} x2={geo.xs[hover]} y1={T} y2={T + PH} />
              {p95[hover] !== null ? <circle className="obs-chart-dot" cx={geo.xs[hover]} cy={p95[hover]!} r={4} /> : null}
              <rect className="obs-chart-tip" x={tipX} y={T} width={tipW} height={18} rx={4} />
              <text className="obs-chart-tiptext" x={tipX + 6} y={T + 12}>{tip}</text>
            </g>
          ) : null}
        </svg>
      )}
      <span className="ag-sr" aria-live="polite" data-copy="data" data-user-text>{tip}</span>
    </div>
  );
}
