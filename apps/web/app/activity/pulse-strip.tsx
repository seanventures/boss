"use client";

/**
 * The pulse (spec §4.2.4): the server's histogram of lines per bucket, stacked by level, above
 * the list. Every bar is a real count with its own tooltip (`18:00–18:15 · 412 lines · 3
 * errors`); heights are on a square-root scale so one error storm does not flatten the rest; a
 * bucket with errors carries a 2px red cap so it shows at any height.
 *
 * Drag across bars to zoom the range to them; double-click to go back. Each bar is a button:
 * Enter zooms to that bucket. Bars move only when a count really changes, never on load, and
 * not at all under reduced motion.
 */
import React, { useRef, useState } from "react";

import type { ActivityHistogram } from "../../lib/activity-log";
import { spanLabel } from "../../lib/log-time";

const LEVEL_ORDER = ["verbose", "info", "warn", "error"] as const;

export function pulseTip(b: ActivityHistogram["buckets"][number], seconds: number): string {
  const lines = b.error + b.warn + b.info + b.verbose;
  const parts = [spanLabel(b.t, seconds), `${lines.toLocaleString("en-US")} line${lines === 1 ? "" : "s"}`];
  if (b.error) parts.push(`${b.error} error${b.error === 1 ? "" : "s"}`);
  return parts.join(" · ");
}

export function PulseStrip({ histogram, loading, onZoom, onReset }: {
  histogram: ActivityHistogram | null;
  loading: boolean;
  onZoom: (fromIso: string, toIso: string) => void;
  onReset: () => void;
}) {
  const [drag, setDragState] = useState<{ a: number; b: number } | null>(null);
  // The drag lives in a ref too: pointer events can arrive faster than React re-renders, and a
  // handler reading the rendered value would lose the start of the drag.
  const dragRef = useRef<{ a: number; b: number } | null>(null);
  const setDrag = (next: { a: number; b: number } | null) => { dragRef.current = next; setDragState(next); };
  const box = useRef<HTMLDivElement>(null);
  const buckets = histogram?.buckets ?? [];
  const seconds = histogram?.bucketSeconds ?? 900;
  const peak = Math.max(1, ...buckets.map((b) => b.error + b.warn + b.info + b.verbose));
  const scale = (n: number) => (n <= 0 ? 0 : Math.sqrt(n) / Math.sqrt(peak));
  const total = buckets.reduce((s, b) => s + b.error + b.warn + b.info + b.verbose, 0);

  const indexAt = (clientX: number): number => {
    const rect = box.current?.getBoundingClientRect();
    if (!rect || !buckets.length) return 0;
    const x = Math.min(Math.max(clientX - rect.left, 0), rect.width - 1);
    return Math.min(buckets.length - 1, Math.floor((x / rect.width) * buckets.length));
  };
  const zoom = (a: number, b: number) => {
    const lo = Math.min(a, b);
    const hi = Math.max(a, b);
    const from = buckets[lo]?.t;
    const toStart = buckets[hi]?.t;
    if (!from || !toStart) return;
    onZoom(from, new Date(Date.parse(toStart) + seconds * 1000).toISOString());
  };

  if (loading && !histogram) {
    return <div className="lx-pulse" aria-busy="true" data-testid="lx-pulse"><span className="lx-pulse-base" /></div>;
  }
  return (
    <div className="lx-pulse" data-testid="lx-pulse" role="group" aria-label="Lines over time"
         onDoubleClick={() => onReset()}>
      <div ref={box} className="lx-pulse-bars" data-dragging={drag ? true : undefined}
           onPointerDown={(e) => {
             if (e.button !== 0 || !buckets.length) return;
             const i = indexAt(e.clientX);
             setDrag({ a: i, b: i });
             (e.currentTarget as HTMLElement).setPointerCapture?.(e.pointerId);
           }}
           onPointerMove={(e) => { const d = dragRef.current; if (d) setDrag({ a: d.a, b: indexAt(e.clientX) }); }}
           onPointerUp={(e) => {
             const d = dragRef.current;
             if (d) {
               const end = indexAt(e.clientX);
               if (d.a !== end) zoom(d.a, end);
             }
             setDrag(null);
           }}>
        {buckets.map((b, i) => {
          const lines = b.error + b.warn + b.info + b.verbose;
          const h = scale(lines);
          const tip = pulseTip(b, seconds);
          const inDrag = drag && i >= Math.min(drag.a, drag.b) && i <= Math.max(drag.a, drag.b);
          return (
            <button key={b.t} type="button" className="lx-bar" data-testid="lx-bar" title={tip} aria-label={tip}
                    data-errors={b.error ? true : undefined} data-selected={inDrag || undefined}
                    onKeyDown={(e) => { if (e.key === "Enter") { e.preventDefault(); zoom(i, i); } }}
                    onClick={(e) => { if (e.detail === 0) zoom(i, i); }}>
              <span className="lx-bar-stack" style={{ height: `${Math.round(h * 100)}%` }}>
                {LEVEL_ORDER.map((level) => (b[level] ? (
                  <span key={level} className="lx-bar-seg" data-level={level}
                        style={{ flexGrow: b[level] }} />
                ) : null))}
              </span>
            </button>
          );
        })}
      </div>
      {histogram && total === 0 ? <span className="lx-pulse-empty" data-testid="lx-pulse-empty">No lines in this range</span> : null}
    </div>
  );
}
