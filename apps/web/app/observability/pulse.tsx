"use client";

/**
 * Observability › Apps (spec §4.9): the Pulse — one strip per app with deploy pins, NOT
 * WATCHED, the app drawer with its latency chart, incidents and `Probe now` (this app only).
 * Stream S4 builds this file (and may split it into app-drawer.tsx, latency-chart.tsx); the two
 * exports and their props are frozen.
 */
import React from "react";

import type { ObsTabProps } from "./obs-home";
import { Empty, Skeleton } from "../infra/kit";

export function AppsTab({ pulse }: ObsTabProps) {
  if (pulse.loading) return <Skeleton testId="obs-apps-loading" />;
  return <Empty testId="obs-apps-empty">Nothing deployed yet</Empty>;
}

export function ObsAskBoss(_props: ObsTabProps) {
  return null;
}
