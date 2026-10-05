"use client";
/** TODAY (command-center spec §4.9): today vs yesterday, Work | Spend. Owned by stream S2. STUB. */
import React from "react";

import type { CommandCenterView, MotionMode } from "../../lib/command-center-view";

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

export function TodayChart({ tab }: TodayChartProps) {
  return <figure className="cc-today-chart" data-tab={tab} />;
}
