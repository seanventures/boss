"use client";
/** LAST 24H (command-center spec §4.10): one bar per run. Owned by stream S2. PHASE 0 STUB. */
import React from "react";

import type { Day, MotionMode } from "../../lib/command-center-view";

export type DayStripProps = {
  day: Day;
  /** The client clock (ms), null before mount (SSR draws to the view's generated_at). */
  nowMs: number | null;
  /** The server's `generated_at`, the right edge before the client clock exists. */
  generatedAt: string;
  /** 24 at ≥ 640px, 12 on a phone. */
  windowHours: 24 | 12;
  /** 10 at ≥ 1440px, 8 below. */
  rowsMax: number;
  motion: MotionMode;
  /** The run hovered here or on its MissionRow (one surface seen two ways). */
  hoverRunId: string | null;
  onHover: (runId: string | null) => void;
  orgId?: string | null;
  /** `Start work ↗` on the empty strip: focus the composer. */
  onStartWork: () => void;
};

export function DayStrip({ day }: DayStripProps) {
  return <div className="cc-day-strip" data-testid="cc-day-strip" data-rows={day.rows.length} />;
}
