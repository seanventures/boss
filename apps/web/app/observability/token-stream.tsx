"use client";

/**
 * The token stream (spec §4.10): the window's 24 buckets as a stacked area, one band per group —
 * the top 5 by the chosen metric plus `Other`, as the server sends them. Latency is not a sum,
 * so it draws one p95 line per group instead of stacking.
 *
 * Colour follows the band's slot in the server's order (`--viz-1..5`, Other `--viz-other`), the
 * same key the table's swatches use, so a band is never colour alone: the legend names every
 * band, the table repeats the key, and `View as table` lists every value. Hovering (or arrow
 * keys) moves a crosshair and fills the readout line above the chart with every band's value at
 * that bucket. Clicking a band (or its legend entry) selects that group; the others dim.
 *
 * Drawn in plot units and stretched to the box (lines keep their width); the axis labels are
 * HTML around it so they never scale with the plot.
 */
import React, { useMemo, useState } from "react";

import { compact, ms, usd, type Metric } from "../../lib/obs-view";
import { TableToggle } from "../infra/kit";

export const OTHER = "__other__";
export const isOther = (b: { key: string; label: string }) => b.key === OTHER || b.label === "Other";
export const slotOf = (key: string, i: number, label?: string) => (key === OTHER || label === "Other" || i >= 5 ? "other" : String(i + 1));

export function fmtMetric(metric: Metric, v: number): string {
  if (metric === "spend") return v > 0 ? usd(v) : "$0.00";
  if (metric === "latency") return v > 0 ? ms(v) : "—";
  return compact(Math.round(v));
}

const W = 1000;
const H = 200;

export type Band = { key: string; label: string; values: number[] };

export function stackBands(bands: Band[], n: number): { lo: number[]; hi: number[] }[] {
  const base = new Array(n).fill(0);
  return bands.map((b) => {
    const lo = [...base];
    const hi = base.map((v, i) => v + (b.values[i] ?? 0));
    hi.forEach((v, i) => { base[i] = v; });
    return { lo, hi };
  });
}

export function TokenStream({ buckets, bands, metric, selected, onSelect, testId = "obs-stream" }: {
  buckets: string[]; bands: Band[]; metric: Metric; selected: string | null; onSelect: (key: string) => void; testId?: string;
}) {
  const [hover, setHover] = useState<number | null>(null);
  const [table, setTable] = useState(false);
  const n = buckets.length;
  const stacked = metric !== "latency";
  const geo = useMemo(() => {
    const stack = stackBands(bands, n);
    const top = stacked
      ? Math.max(1, ...stack.map((s) => Math.max(0, ...s.hi)))
      : Math.max(1, ...bands.flatMap((b) => b.values));
    const x = (i: number) => (n > 1 ? (i / (n - 1)) * W : W / 2);
    const y = (v: number) => H - (v / top) * (H - 4);
    return { stack, top, x, y };
  }, [bands, n, stacked]);

  const area = (k: number) => {
    const { lo, hi } = geo.stack[k];
    const up = hi.map((v, i) => `${i ? "L" : "M"}${geo.x(i).toFixed(1)},${geo.y(v).toFixed(1)}`).join("");
    const down = lo.map((v, i) => i).reverse().map((i) => `L${geo.x(i).toFixed(1)},${geo.y(lo[i]).toFixed(1)}`).join("");
    return `${up}${down}Z`;
  };
  const line = (b: Band) => {
    let d = "";
    let pen = false;
    b.values.forEach((v, i) => {
      if (!(v > 0)) { pen = false; return; }
      d += `${pen ? "L" : "M"}${geo.x(i).toFixed(1)},${geo.y(v).toFixed(1)}`;
      pen = true;
    });
    return d;
  };
  const when = (i: number) => {
    const t = Date.parse(buckets[i] ?? "");
    if (!Number.isFinite(t)) return "";
    const spanMs = n > 1 ? Date.parse(buckets[n - 1]) - Date.parse(buckets[0]) : 0;
    return spanMs > 36 * 3600_000
      ? new Date(t).toLocaleDateString([], { month: "short", day: "numeric", hour: "2-digit", hour12: false })
      : new Date(t).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", hour12: false });
  };
  const xTicks = n ? [0, Math.floor((n - 1) / 3), Math.floor((2 * (n - 1)) / 3), n - 1] : [];
  const onMove = (e: React.MouseEvent<SVGSVGElement>) => {
    const box = e.currentTarget.getBoundingClientRect();
    if (!box.width || !n) return;
    const i = Math.round(((e.clientX - box.left) / box.width) * (n - 1));
    setHover(Math.max(0, Math.min(n - 1, i)));
  };
  const onKey = (e: React.KeyboardEvent) => {
    if (!n) return;
    if (e.key === "ArrowRight" || e.key === "ArrowLeft") {
      e.preventDefault();
      const d = e.key === "ArrowRight" ? 1 : -1;
      setHover((v) => Math.max(0, Math.min(n - 1, (v ?? (d > 0 ? -1 : n)) + d)));
    } else if (e.key === "Escape") setHover(null);
  };
  const total = bands.reduce((a, b) => a + b.values.reduce((x, y) => x + y, 0), 0);
  const summary = `${bands.length} ${bands.length === 1 ? "band" : "bands"} over ${n} buckets: ${bands.map((b) => isOther(b) ? "Other" : b.label).join(", ")}`;

  if (!bands.length) return <div className="obs-stream-empty inf-empty" data-testid={`${testId}-empty`}>No model calls yet</div>;

  return (
    <div className="obs-stream" data-testid={testId} data-metric={metric}>
      <div className="obs-stream-bar">
        <span className="obs-legend" role="list" aria-label="Bands">
          {bands.map((b, k) => (
            <button key={b.key} type="button" role="listitem" className="obs-legend-item" data-slot={slotOf(b.key, k, b.label)}
                    aria-pressed={selected === b.key} data-dim={selected && selected !== b.key ? "1" : undefined}
                    data-testid="obs-legend-item" disabled={isOther(b)} title={isOther(b) ? "Everything outside the top 5" : `Select ${b.label}`}
                    onClick={() => onSelect(b.key)}>
              <span className="obs-swatch" data-slot={slotOf(b.key, k, b.label)} aria-hidden />
              <span className="inf-trunc" data-copy="data" data-user-text>{isOther(b) ? "Other" : b.label}</span>
            </button>
          ))}
        </span>
        <span className="ag-spacer" />
        <TableToggle on={table} onToggle={() => setTable((v) => !v)} testId={`${testId}-table-toggle`} />
      </div>
      {table ? (
        <div className="inf-table-wrap">
          <table className="inf-table" data-testid={`${testId}-table`}>
            <thead><tr><th scope="col">Bucket</th>{bands.map((b) => <th key={b.key} scope="col">{isOther(b) ? "Other" : b.label}</th>)}</tr></thead>
            <tbody>
              {buckets.map((t, i) => (
                <tr key={t}><td>{when(i)}</td>{bands.map((b) => <td key={b.key}>{fmtMetric(metric, b.values[i] ?? 0)}</td>)}</tr>
              ))}
            </tbody>
          </table>
        </div>
      ) : (
        <>
          <div className="obs-readout" aria-live="polite" data-testid={`${testId}-readout`}>
            {hover === null ? (
              <span className="inf-faint">{stacked ? `${fmtMetric(metric, total)} in this window` : "p95 per group"}</span>
            ) : (
              <>
                <span className="inf-num obs-strong">{when(hover)}</span>
                {bands.map((b, k) => (
                  <span key={b.key} className="obs-readout-item">
                    <span className="obs-swatch" data-slot={slotOf(b.key, k, b.label)} aria-hidden />
                    <span className="inf-trunc" data-copy="data" data-user-text>{isOther(b) ? "Other" : b.label}</span>
                    <span className="inf-num">{fmtMetric(metric, b.values[hover] ?? 0)}</span>
                  </span>
                ))}
              </>
            )}
          </div>
          <div className="obs-stream-plot">
            <span className="obs-yaxis" aria-hidden>
              <span>{fmtMetric(metric, geo.top)}</span>
              <span>{fmtMetric(metric, geo.top / 2)}</span>
              <span>0</span>
            </span>
            <svg className="obs-stream-svg" viewBox={`0 0 ${W} ${H}`} preserveAspectRatio="none" role="img" aria-label={summary}
                 tabIndex={0} onMouseMove={onMove} onMouseLeave={() => setHover(null)} onKeyDown={onKey} onBlur={() => setHover(null)}>
              {[0.5, 1].map((k) => <line key={k} className="obs-chart-grid" x1={0} x2={W} y1={geo.y(geo.top * k)} y2={geo.y(geo.top * k)} vectorEffect="non-scaling-stroke" />)}
              {bands.map((b, k) => (
                stacked ? (
                  <path key={b.key} className="obs-band" data-slot={slotOf(b.key, k, b.label)} data-testid="obs-band" data-key={b.key}
                        data-dim={selected && selected !== b.key ? "1" : undefined} d={area(k)} vectorEffect="non-scaling-stroke"
                        onClick={() => { if (!isOther(b)) onSelect(b.key); }}>
                    <title>{isOther(b) ? "Other" : b.label}</title>
                  </path>
                ) : (
                  <path key={b.key} className="obs-band-line" data-slot={slotOf(b.key, k, b.label)} data-testid="obs-band" data-key={b.key}
                        data-dim={selected && selected !== b.key ? "1" : undefined} d={line(b)} vectorEffect="non-scaling-stroke"
                        onClick={() => { if (!isOther(b)) onSelect(b.key); }}>
                    <title>{isOther(b) ? "Other" : b.label}</title>
                  </path>
                )
              ))}
              <line className="obs-chart-base" x1={0} x2={W} y1={H} y2={H} vectorEffect="non-scaling-stroke" />
              {hover !== null ? <line className="obs-chart-cross" x1={geo.x(hover)} x2={geo.x(hover)} y1={0} y2={H} vectorEffect="non-scaling-stroke" /> : null}
            </svg>
          </div>
          <span className="obs-xaxis" aria-hidden>
            {xTicks.map((i, k) => <span key={k}>{k === xTicks.length - 1 ? "now" : when(i)}</span>)}
          </span>
        </>
      )}
    </div>
  );
}
