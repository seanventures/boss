"use client";
/** THE FLOOR (command-center spec §4.7): the hero map. Owned by stream S2. PHASE 0 STUB. */
import React from "react";

import type { CommandCenterView, MotionMode } from "../../lib/command-center-view";
import type { Band } from "../agents/live-util";

export type FloorProps = {
  view: CommandCenterView;
  projectId: string;
  motion: MotionMode;
  /** Why motion is off when it is (`Motion paused while the host struggles`), for You's tooltip. */
  motionWhy?: string | null;
  band: Band;
  orgId?: string | null;
  /** Press You: scroll to Needs you. */
  onYou: () => void;
  /** Press a thread: focus that attention card. */
  onThread: (attentionId: string) => void;
};

export function Floor({ view }: FloorProps) {
  return <div className="cc-floor-stage" data-testid="cc-floor-stage" data-agents={view.agents.length} />;
}
