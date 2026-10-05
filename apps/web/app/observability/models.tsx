"use client";

/**
 * Observability › Models (spec §4.10): the stat strip, the token stream, the group table and drawer, FAILED CALLS and ROUTING.
 * Stream S4 builds this file; the export and its props are frozen.
 */
import React from "react";

import type { ObsTabProps } from "./obs-home";
import { Empty } from "../infra/kit";

export function ModelsTab(_props: ObsTabProps) {
  return <Empty testId="obs-models-empty">Not built yet</Empty>;
}
