"use client";

/**
 * Infra › Previews (spec §4.7): live, failed and ended previews; Open, Log, Restart, Stop, Build.
 * Stream S3 builds this file; the export and its props are frozen.
 */
import React from "react";

import type { InfraTabProps } from "./infra-home";
import { Empty } from "./kit";

export function PreviewsTab(_props: InfraTabProps) {
  return <Empty testId="infra-previews-empty">Not built yet</Empty>;
}
