"use client";
/** THIS WEEK (command-center spec §4.12): 7 bars and one line of totals. Owned by S2. STUB. */
import React from "react";

import type { Week } from "../../lib/command-center-view";

export type WeekBarsProps = { week: Week; tz: string };

export function WeekBars({ week }: WeekBarsProps) {
  return <div className="cc-week-bars" data-runs={week.runs} />;
}
