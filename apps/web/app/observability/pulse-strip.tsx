"use client";

/**
 * THE PULSE STRIP (infra-observability spec §3.2, §4.9, §5.4): one component, every density —
 * 96/84/90 on Observability › Apps, 48 and 24 at narrow widths, 32 as the heartbeat on an Infra
 * tile and in the target drawer. Frozen in Phase 0; both web streams draw it.
 *
 * Each cell is ok of n probes in its bucket:
 *   all ok → green · some failed → amber · none ok → red, hatched · no probes → an outlined gap.
 * A gap is never drawn green (`n === 0`). Hovering or focusing a cell's tooltip says the bucket's
 * truth: `14:15–14:30 · 7 of 8 ok · p95 412 ms`, or `No probes`. Deploy pins sit above the strip
 * at the moment each deploy finished, coloured by its result; a pin links to the deploy drawer.
 * The whole strip is one `role=img` with a summary label; `View as table` lists every bucket.
 */
import React, { useId } from "react";

import type { Tone } from "../../lib/infra-view";
import { LOCAL, useLocalTime, type LocalTime } from "../../lib/local-time";

export type StripCellIn = { ok: number; n: number; p95?: number | null };
export type StripPin = { at: string; sha7: string | null; word: string; tone: Tone; href?: string | null };
export type CellKind = "ok" | "some" | "none" | "gap";

export function cellKind(c: StripCellIn): CellKind {
  if (c.n <= 0) return "gap";
  if (c.ok >= c.n) return "ok";
  if (c.ok <= 0) return "none";
  return "some";
}


/** `<from>–<to> · k of n ok · p95 <ms>` or `<from>–<to> · No probes`. */
export function cellTip(c: StripCellIn, startMs: number | null, cellS: number, t: LocalTime = LOCAL): string {
  const { hhmm, day } = t;
  let when = "";
  if (startMs !== null && cellS > 0) {
    const end = startMs + cellS * 1000;
    when = cellS >= 6 * 3600 ? `${day(startMs)} ${hhmm(startMs)}–${hhmm(end)}` : `${hhmm(startMs)}–${hhmm(end)}`;
  }
  if (c.n <= 0) return when ? `${when} · No probes` : "No probes";
  const p95 = c.p95 !== undefined && c.p95 !== null ? ` · p95 ${Math.round(c.p95)} ms` : "";
  return `${when ? `${when} · ` : ""}${c.ok} of ${c.n} ok${p95}`;
}

/** The strip's one-line summary for screen readers: `invoice prod, 24 hours: up 99.9%, 2 deploys`. */
export function stripSummary(name: string, span: string, cells: StripCellIn[], pins: number): string {
  const n = cells.reduce((a, c) => a + c.n, 0);
  const ok = cells.reduce((a, c) => a + c.ok, 0);
  const share = n ? `up ${((ok / n) * 100).toFixed(1)}%` : "no probes";
  const bad = cells.filter((c) => c.n > 0 && c.ok < c.n).length;
  return `${name}, ${span}: ${share}${bad ? `, ${bad} rough ${bad === 1 ? "period" : "periods"}` : ""}${pins ? `, ${pins} ${pins === 1 ? "deploy" : "deploys"}` : ""}`;
}

export function PulseStrip({ cells, fromIso, cellS, pins = [], label, span, density = "full", testId, onPin }: {
  cells: StripCellIn[];
  /** Start of the first cell; cells are contiguous and `cellS` seconds wide. */
  fromIso: string | null;
  cellS: number;
  pins?: StripPin[];
  /** Whose strip: `invoice prod`. */
  label: string;
  /** `24 hours`, `7 days`, `30 days`. */
  span: string;
  density?: "full" | "mini";
  testId?: string;
  onPin?: (pin: StripPin) => void;
}) {
  const hatch = `obs-hatch-${useId().replace(/[^a-zA-Z0-9_-]/g, "")}`;
  const t = useLocalTime();
  const { hhmm } = t;
  const start = fromIso ? Date.parse(fromIso) : NaN;
  const startMs = Number.isFinite(start) ? start : null;
  const n = Math.max(1, cells.length);
  const total = cells.length * cellS * 1000;
  // Drawn in SVG user units: each cell is 10 wide with a 2-unit gutter; the strip stretches to
  // its box (preserveAspectRatio none) and strokes keep their width (non-scaling-stroke).
  const W = n * 10;
  return (
    <div className="obs-strip" data-density={density} data-testid={testId}>
      {pins.length && startMs !== null && total > 0 ? (
        <svg className="obs-pins" viewBox={`0 0 ${W} 8`} preserveAspectRatio="none" aria-hidden={onPin ? undefined : true}>
          {pins.map((p, i) => {
            const at = Date.parse(p.at);
            if (!Number.isFinite(at)) return null;
            const x = Math.min(W, Math.max(0, ((at - startMs) / total) * W));
            const tip = `Deployed ${p.sha7 ?? "—"} · ${hhmm(at)} · ${p.word}`;
            const mark = (
              <polygon className="obs-pin" data-tone={p.tone} points={`${x - 3},0 ${x + 3},0 ${x},7`}>
                <title>{tip}</title>
              </polygon>
            );
            return onPin || p.href ? (
              <a key={`${p.at}-${i}`} href={p.href ?? undefined} aria-label={tip}
                 onClick={(e) => { if (onPin) { e.preventDefault(); onPin(p); } }}>{mark}</a>
            ) : <g key={`${p.at}-${i}`}>{mark}</g>;
          })}
        </svg>
      ) : null}
      <svg className="obs-cells" viewBox={`0 0 ${W} 12`} preserveAspectRatio="none" role="img"
           aria-label={stripSummary(label, span, cells, pins.length)}>
        <defs>
          <pattern id={hatch} width="4" height="4" patternUnits="userSpaceOnUse" patternTransform="rotate(45)">
            <rect className="obs-hatch-bg" width="4" height="4" />
            <line className="obs-hatch-line" x1="0" y1="0" x2="0" y2="4" />
          </pattern>
        </defs>
        {cells.map((c, i) => {
          const k = cellKind(c);
          return (
            <rect key={i} className="obs-cell" data-k={k} x={i * 10 + 1} y={k === "gap" ? 1 : 0} width={8} height={k === "gap" ? 10 : 12}
                  rx={1.5} fill={k === "none" ? `url(#${hatch})` : undefined} vectorEffect="non-scaling-stroke">
              <title>{cellTip(c, startMs === null ? null : startMs + i * cellS * 1000, cellS, t)}</title>
            </rect>
          );
        })}
      </svg>
    </div>
  );
}

/** The same buckets as a table (`View as table`, §7). */
export function StripTable({ cells, fromIso, cellS, testId }: { cells: StripCellIn[]; fromIso: string | null; cellS: number; testId?: string }) {
  const { hhmm, day } = useLocalTime();
  const start = fromIso ? Date.parse(fromIso) : NaN;
  return (
    <table className="inf-table obs-strip-table" data-testid={testId}>
      <thead><tr><th scope="col">From</th><th scope="col">Probes</th><th scope="col">OK</th><th scope="col">p95</th></tr></thead>
      <tbody>
        {cells.map((c, i) => {
          const at = Number.isFinite(start) ? start + i * cellS * 1000 : null;
          return (
            <tr key={i} data-k={cellKind(c)}>
              <td>{at === null ? "—" : `${cellS >= 6 * 3600 ? `${day(at)} ` : ""}${hhmm(at)}`}</td>
              <td>{c.n}</td>
              <td>{c.n ? c.ok : "—"}</td>
              <td>{c.p95 !== undefined && c.p95 !== null ? `${Math.round(c.p95)} ms` : "—"}</td>
            </tr>
          );
        })}
      </tbody>
    </table>
  );
}
