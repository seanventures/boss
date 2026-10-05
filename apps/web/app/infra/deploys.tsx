"use client";

/**
 * Infra › Deploys (spec §4.6): the rail, the deploy drawer, + Deploy, Rules, CI config, Secrets.
 * Stream S3 builds this file; the export and its props are frozen.
 */
import React from "react";

import type { InfraTabProps } from "./infra-home";
import { Empty } from "./kit";

export function DeploysTab(_props: InfraTabProps) {
  return <Empty testId="infra-deploys-empty">Not built yet</Empty>;
}
