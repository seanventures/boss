"use client";
/**
 * THE GRAPHICS, CODE-SPLIT (fix pass D10). The overview's first-load JS grew 19 kB against a
 * 12 kB budget, most of it the Floor's map, comets and mini card. Each graphic is its own chunk,
 * still rendered on the server (ssr: true) so the first paint is unchanged and nothing shifts;
 * the browser fetches the chunk alongside hydration instead of in the page's first bundle.
 * While a chunk loads on the client, a placeholder of the SAME size holds the space (CLS 0).
 */
import dynamic from "next/dynamic";
import React from "react";

import type { DayStripProps } from "./day-strip";
import type { FloorProps } from "./floor";
import type { TodayChartProps } from "./today-chart";
import type { WeekBarsProps } from "./week-bars";

const hold = (className: string) => function Holding() {
  return <div className={className} aria-hidden data-loading="true" />;
};

export const Floor = dynamic<FloorProps>(() => import("./floor").then((m) => m.Floor),
  { ssr: true, loading: hold("cc-floor-stage cc-hold") });
export const TodayChart = dynamic<TodayChartProps>(() => import("./today-chart").then((m) => m.TodayChart),
  { ssr: true, loading: hold("cc-today-hold cc-hold") });
export const DayStrip = dynamic<DayStripProps>(() => import("./day-strip").then((m) => m.DayStrip),
  { ssr: true, loading: hold("cc-day-hold cc-hold") });
export const WeekBars = dynamic<WeekBarsProps>(() => import("./week-bars").then((m) => m.WeekBars),
  { ssr: true, loading: hold("cc-week-hold cc-hold") });
