"use client";

/**
 * TODAY (command-center spec §4.9): a Stripe-style cumulative line of today, drawn only up to
 * now, over yesterday's line dotted. `Work` is agent work finished (agent_acts); `Spend` is the
 * budget's own money (MONEY_SQL over cost_receipts), so a plan-covered call is $0 here exactly as
 * it is in Budget, and its API value is drawn apart as `Included`.
 *
 * NEVER PAST NOW, NEVER A FLAT FAKE. Both days zero draws no line, only the axis and the words.
 * The line is drawn statically on load; only a poll that adds work in the current hour extends
 * it (a 300ms clip, transform only).
 */
import React, { useEffect, useLayoutEffect, useRef, useState } from "react";

import { cumulative, money, niceMax, nowPosition } from "../../lib/command-center-graphics";
import type { BudgetState, CommandCenterView, MotionMode } from "../../lib/command-center-view";

/** useLayoutEffect in the browser, nothing on the server (which has no layout to read). */
const useIsoLayoutEffect = typeof window !== "undefined" ? useLayoutEffect : useEffect;

export type TodayTab = "work" | "spend";

export type TodayChartProps = {
  view: CommandCenterView;
  tab: TodayTab;
  onTab: (tab: TodayTab) => void;
  motion: MotionMode;
  /** The client clock (ms), null before mount (SSR draws to `view.today.now`). */
  nowMs: number | null;
  /** The viewer's zone (the view's `tz`), for the cap-line rule. */
  tz: string;
  compact?: boolean;
};

const PAD = { l: 44, r: 92, t: 18, b: 24 };
const PERIOD_WORD: Record<string, string> = { month: "this month", week: "this week", day: "today" };
const METER_TONE: Record<BudgetState, string> = { unknown: "muted", green: "green", yellow: "amber", orange: "amber", red: "red" };

function useWidth(ref: React.RefObject<HTMLDivElement | null>, initial: number): number {
  const [w, setW] = useState(initial);
  useIsoLayoutEffect(() => {
    const el = ref.current;
    if (!el) return undefined;
    const read = () => { const x = Math.round(el.clientWidth); if (x > 0) setW(x); };
    read();
    if (typeof ResizeObserver === "undefined") return undefined;
    const ro = new ResizeObserver(read);
    ro.observe(el);
    return () => ro.disconnect();
  }, [ref]);
  return w;
}

const pad2 = (n: number) => String(n).padStart(2, "0");

/** The chart's numbers, for both tabs: what the lines, labels and caption say. */
export function todaySeries(view: CommandCenterView, tab: TodayTab, nowMs: number) {
  const { index, frac } = nowPosition(view.today.hours, nowMs);
  const field = tab === "work" ? "work" : "moneyMicros";
  const main = cumulative(view.today.hours, index, frac, field);
  const inc = cumulative(view.today.hours, index, frac, "includedMicros");
  const showIncluded = tab === "spend" && main.todayTotal === 0 && inc.todayTotal > 0;
  return { index, frac, main, inc, showIncluded, nowX: index - 24 + frac };
}

export function TodayChart({ view, tab, onTab, motion, nowMs, tz, compact = false }: TodayChartProps) {
  const box = useRef<HTMLDivElement>(null);
  const width = useWidth(box, 960);
  const height = compact ? 140 : 180;
  const now = nowMs ?? Date.parse(view.today.now || view.generatedAt);
  const s = todaySeries(view, tab, now);
  const fmt = (v: number) => (tab === "work" ? String(Math.round(v)) : money(v));
  const budget = view.budget;
  const capLine = tab === "spend" && budget.day.capUsdMicros > 0 && tz === budget.budgetTimezone;
  const meter = tab === "spend" && !capLine && budget.capUsdMicros > 0 && budget.period !== "day";
  const ymax = niceMax(Math.max(s.main.todayTotal, s.main.yesterdayTotal,
                                s.showIncluded ? s.inc.todayTotal : 0, capLine ? budget.day.capUsdMicros : 0));
  const empty = s.main.today.length === 0 && !s.showIncluded;
  const iw = Math.max(40, width - PAD.l - PAD.r);
  const ih = height - PAD.t - PAD.b;
  const x = (h: number) => PAD.l + (h / 24) * iw;
  const y = (v: number) => PAD.t + ih - (v / ymax) * ih;
  const path = (pts: [number, number][]) => pts.map(([h, v], i) => `${i ? "L" : "M"} ${x(h).toFixed(1)} ${y(v).toFixed(1)}`).join(" ");
  const ticks = compact ? [0, 6, 12, 18, 24] : [0, 3, 6, 9, 12, 15, 18, 21, 24];

  // Extend on a poll: the last segment unclips from where it was (transform only, 300ms).
  const prev = useRef<{ x: number; total: number; tab: TodayTab } | null>(null);
  const [from, setFrom] = useState<number | null>(null);
  // The transition exists only while a real increase plays: a resize or a clock tick moves the
  // dot without motion (motion carries state, §5.2).
  const [extending, setExtending] = useState(false);
  const nowPx = x(s.nowX);
  useIsoLayoutEffect(() => {
    const p = prev.current;
    prev.current = { x: nowPx, total: s.main.todayTotal, tab };
    if (!p || p.tab !== tab || motion !== "full" || s.main.todayTotal <= p.total || nowPx <= PAD.l) return;
    setFrom(Math.max(0, Math.min(1, (p.x - PAD.l) / (nowPx - PAD.l))));
    setExtending(true);
    const r = requestAnimationFrame(() => requestAnimationFrame(() => setFrom(null)));
    const done = window.setTimeout(() => setExtending(false), 400);
    return () => { cancelAnimationFrame(r); window.clearTimeout(done); };
  }, [s.main.todayTotal, tab]); // eslint-disable-line react-hooks/exhaustive-deps -- a real increase only

  // Crosshair: hover or ←/→, hour by hour.
  const [cross, setCross] = useState<number | null>(null);
  const at = (h: number, pts: [number, number][]) => pts.find(([hh]) => hh === h)?.[1];
  const crossTip = cross === null ? "" : (() => {
    const t = cross + 1 <= s.nowX ? at(cross + 1, s.main.today) : cross <= s.nowX ? s.main.todayTotal : undefined;
    const yv = at(cross + 1, s.main.yesterday) ?? 0;
    return `${pad2(cross)}:00 · ${t === undefined ? "" : `today ${fmt(t)} · `}yesterday ${fmt(yv)}`;
  })();

  const nowLabel = `${pad2(Math.floor(s.nowX))}:${pad2(Math.round((s.nowX % 1) * 60) % 60)}`;
  const caption = tab === "work"
    ? `Work today: ${fmt(s.main.todayTotal)} by ${nowLabel}. Yesterday: ${fmt(s.main.yesterdayByNow)} by this time, ${fmt(s.main.yesterdayTotal)} all day.`
    : `Spend today: ${fmt(s.main.todayTotal)} by ${nowLabel}. Yesterday: ${fmt(s.main.yesterdayByNow)} by this time, ${fmt(s.main.yesterdayTotal)} all day.`;
  const lastPt = s.main.today[s.main.today.length - 1];
  const clipId = `cc-today-clip-${tab}`;

  return (
    <div className="cc-today-chart" data-tab={tab} data-motion={motion} data-extending={extending ? "true" : undefined}>
      <div className="cc-today-tabs" role="group" aria-label="Chart">
        <button type="button" className="cc-seg" aria-pressed={tab === "work"} data-testid="cc-today-tab-work"
                title="Agent work finished, by hour" onClick={() => onTab("work")}>Work</button>
        <button type="button" className="cc-seg" aria-pressed={tab === "spend"} data-testid="cc-today-tab-spend"
                title="Money spent, by hour. Plan-covered calls are $0." onClick={() => onTab("spend")}>Spend</button>
      </div>
      <figure className="cc-today-fig" ref={box} style={{ height }}>
        <figcaption className="ag-sr wf-sr">{caption}</figcaption>
        <svg width={width} height={height} viewBox={`0 0 ${width} ${height}`} className="cc-today-svg"
             aria-label="Hour by hour. Arrow keys move the crosshair."
             tabIndex={0} focusable="true"
             onMouseMove={(e) => {
               const r = (e.currentTarget as SVGSVGElement).getBoundingClientRect();
               const h = Math.floor(((e.clientX - r.left - PAD.l) / iw) * 24);
               setCross(h >= 0 && h < 24 ? h : null);
             }}
             onMouseLeave={() => setCross(null)}
             onKeyDown={(e) => {
               if (e.key === "ArrowRight") { e.preventDefault(); setCross((c) => Math.min(23, (c ?? Math.floor(s.nowX)) + (c === null ? 0 : 1))); }
               if (e.key === "ArrowLeft") { e.preventDefault(); setCross((c) => Math.max(0, (c ?? Math.floor(s.nowX)) - (c === null ? 0 : 1))); }
               if (e.key === "Escape") setCross(null);
             }}
             onBlur={() => setCross(null)}>
          <defs>
            <clipPath id={clipId}>
              <rect x={PAD.l - 4} y={0} width={Math.max(0, nowPx - PAD.l + 8)} height={height}
                    className="cc-today-clip"
                    style={from !== null ? { transform: `scaleX(${from})`, transformOrigin: `${PAD.l}px 0`, transition: "none" }
                      : { transformOrigin: `${PAD.l}px 0` }} />
            </clipPath>
            <linearGradient id={`cc-today-fill-${tab}`} x1="0" y1="0" x2="0" y2="1">
              <stop offset="0%" stopColor="var(--accent)" stopOpacity="0.28" />
              <stop offset="100%" stopColor="var(--accent)" stopOpacity="0" />
            </linearGradient>
          </defs>
          {/* Axis: a baseline and two y ticks, no grid. */}
          <line className="cc-axis" x1={PAD.l} x2={PAD.l + iw} y1={y(0)} y2={y(0)} />
          <text className="cc-tick" x={PAD.l - 8} y={y(0)} textAnchor="end" dominantBaseline="middle">{fmt(0)}</text>
          {!empty ? <text className="cc-tick" x={PAD.l - 8} y={y(ymax)} textAnchor="end" dominantBaseline="middle">{fmt(ymax)}</text> : null}
          {ticks.map((h) => (
            <text key={h} className="cc-tick" x={x(h)} y={height - 6} textAnchor="middle">{pad2(h)}</text>
          ))}
          {capLine ? (
            <g className="cc-cap" data-testid="cc-today-cap">
              <line x1={PAD.l} x2={PAD.l + iw} y1={y(budget.day.capUsdMicros)} y2={y(budget.day.capUsdMicros)} />
              <text x={PAD.l + iw + 6} y={y(budget.day.capUsdMicros)} dominantBaseline="middle">{`Cap ${money(budget.day.capUsdMicros).replace(/\.00$/, "")}`}</text>
            </g>
          ) : null}
          {!empty && s.main.yesterday.length ? (
            <g className="cc-yesterday" data-testid="cc-today-yesterday" data-tip="Yesterday, same hours">
              <title>Yesterday, same hours</title>
              <path d={path(s.main.yesterday)} />
            </g>
          ) : null}
          {s.showIncluded ? (
            <g className="cc-included" data-testid="cc-today-included" data-tip="API-equivalent value covered by your plan">
              <title>API-equivalent value covered by your plan</title>
              <path d={path(s.inc.today)} />
              <text x={x(s.nowX) + 8} y={y(s.inc.todayTotal) - 8}>{`Included ${money(s.inc.todayTotal)}`}</text>
            </g>
          ) : null}
          {!empty && s.main.today.length ? (
            <g className="cc-today-line" data-testid="cc-today-line" clipPath={`url(#${clipId})`}>
              <path className="cc-today-area" d={`${path(s.main.today)} L ${x(lastPt[0]).toFixed(1)} ${y(0)} L ${x(0)} ${y(0)} Z`}
                    fill={`url(#cc-today-fill-${tab})`} />
              <path className="cc-today-stroke" d={path(s.main.today)} data-end-x={lastPt[0]} />
            </g>
          ) : null}
          {/* now: a hairline, the dot, and today's number beside it. */}
          <line className="cc-now" x1={nowPx} x2={nowPx} y1={PAD.t - 6} y2={y(0)} />
          {!empty && s.main.today.length ? (
            <g className="cc-today-dot" style={{ transform: `translate(${nowPx}px, ${y(s.main.todayTotal)}px)` }}>
              <circle r={3} />
              <text x={8} y={-8} className="cc-today-now">{fmt(s.main.todayTotal)}</text>
            </g>
          ) : null}
          {!empty ? (
            <>
              <text className="cc-yday-label" x={nowPx + 8} y={Math.min(y(0) - 4, y(s.main.yesterdayByNow) + 14)}>{`${fmt(s.main.yesterdayByNow)} by now`}</text>
              <text className="cc-yday-label" x={PAD.l + iw + 6} y={y(s.main.yesterdayTotal)} dominantBaseline="middle">{`${fmt(s.main.yesterdayTotal)} all day`}</text>
            </>
          ) : null}
          {cross !== null ? <line className="cc-cross" x1={x(cross + 0.5)} x2={x(cross + 0.5)} y1={PAD.t} y2={y(0)} /> : null}
        </svg>
        {empty ? (
          <span className="cc-today-empty" data-testid="cc-today-empty">{tab === "work" ? "No work yet today" : "No spend yet"}</span>
        ) : null}
        {cross !== null ? (
          <span className="cc-today-tip" role="tooltip" style={{ left: Math.min(width - 160, x(cross + 0.5) + 8) }}>{crossTip}</span>
        ) : null}
        <table className="ag-sr wf-sr">
          <caption>{tab === "work" ? "Work by hour" : "Spend by hour"}</caption>
          <thead><tr><th>Hour</th><th>Today</th><th>Yesterday</th></tr></thead>
          <tbody>
            {Array.from({ length: 24 }, (_, h) => {
              const f = tab === "work" ? "work" : "moneyMicros";
              return (
                <tr key={h}>
                  <td>{`${pad2(h)}:00`}</td>
                  <td>{h <= Math.floor(s.nowX) ? fmt(Number(view.today.hours[24 + h]?.[f] ?? 0)) : ""}</td>
                  <td>{fmt(Number(view.today.hours[h]?.[f] ?? 0))}</td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </figure>
      {meter ? (
        <div className="cc-meter" data-tone={METER_TONE[budget.state]} data-testid="cc-today-meter"
             title={budget.resetsAtLabel ? `Resets ${budget.resetsAtLabel}` : undefined}>
          <span className="cc-meter-bar" aria-hidden>
            <i style={{ transform: `scaleX(${Math.min(1, budget.capUsdMicros ? budget.actualUsdMicros / budget.capUsdMicros : 0)})` }} />
          </span>
          <span>{`${money(budget.actualUsdMicros).replace(/\.00$/, "")} of ${money(budget.capUsdMicros).replace(/\.00$/, "")} · ${PERIOD_WORD[budget.period] ?? `this ${budget.period}`}`}</span>
        </div>
      ) : null}
    </div>
  );
}
