"use client";

/**
 * Observability › System (spec §4.11): BOSS MACHINE (the stability work's gauges), WORK, STALLS, FINDINGS (system-doctor cards) and what BOSS did.
 * Stream S4 builds this file; the export and its props are frozen.
 */
import React from "react";

import type { ObsTabProps } from "./obs-home";
import { Empty } from "../infra/kit";

export function SystemTab(_props: ObsTabProps) {
  return <Empty testId="obs-system-empty">Not built yet</Empty>;
}
