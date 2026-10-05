"use client";

/**
 * Infra › Cloud (spec §4.8): Accounts, Resources, Capacity, Spend, Changes — one table and a drawer each.
 * Stream S3 builds this file; the export and its props are frozen.
 */
import React from "react";

import type { InfraTabProps } from "./infra-home";
import { Empty } from "./kit";

export function CloudTab(_props: InfraTabProps) {
  return <Empty testId="infra-cloud-empty">Not built yet</Empty>;
}
