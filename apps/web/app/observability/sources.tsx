"use client";

/**
 * Observability › Sources (spec §4.13): STACK, SCRAPED APPS, SOURCES, GAPS and Add tools.
 * Stream S4 builds this file; the export and its props are frozen.
 */
import React from "react";

import type { ObsTabProps } from "./obs-home";
import { Empty } from "../infra/kit";

export function SourcesTab(_props: ObsTabProps) {
  return <Empty testId="obs-sources-empty">Not built yet</Empty>;
}
