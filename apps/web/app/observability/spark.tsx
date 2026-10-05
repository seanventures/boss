"use client";

/**
 * A sparkline (spec §4.10 stat strip and table trend): one thin line over the window's 24
 * points, the last point marked. `role=img` with a one-line summary; never animated.
 */
import React from "react";

export function sparkPath(values: number[], w: number, h: number): string {
  if (!values.length) return "";
  const max = Math.max(...values);
  const min = Math.min(0, ...values);
  const span = max - min || 1;
  const step = values.length > 1 ? w / (values.length - 1) : 0;
  return values.map((v, i) => `${i ? "L" : "M"}${(i * step).toFixed(1)},${(h - 1 - ((v - min) / span) * (h - 2)).toFixed(1)}`).join("");
}

export function Spark({ values, label, testId, slot }: { values: number[]; label: string; testId?: string; slot?: string }) {
  const W = 72;
  const H = 20;
  if (!values.length || values.every((v) => v === 0)) {
    return <svg className="obs-spark" data-empty="1" viewBox={`0 0 ${W} ${H}`} role="img" aria-label={`${label}: nothing in this window`} data-testid={testId}>
      <line className="obs-spark-flat" x1={0} x2={W} y1={H - 1} y2={H - 1} />
    </svg>;
  }
  const d = sparkPath(values, W, H);
  const lo = Math.min(...values);
  const hi = Math.max(...values);
  return (
    <svg className="obs-spark" data-slot={slot} viewBox={`0 0 ${W} ${H}`} preserveAspectRatio="none" role="img"
         aria-label={`${label}: ${values.length} points, low ${lo.toLocaleString("en-US")}, high ${hi.toLocaleString("en-US")}`} data-testid={testId}>
      <path className="obs-spark-line" d={d} vectorEffect="non-scaling-stroke" />
    </svg>
  );
}
