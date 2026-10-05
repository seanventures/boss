"use client";

/**
 * Infra › Fleet (spec §4.3-§4.5): racks, resident tiles, the stage line, the host and target
 * drawers, the `+ Add ▾` sheets and the header's Ask BOSS. Stream S3 builds this file (and may
 * split it into rack.tsx, resident-tile.tsx, stage-line.tsx, host-drawer.tsx, target-drawer.tsx,
 * target-sheet.tsx); the three exports and their props are frozen.
 */
import React from "react";

import type { Polled } from "../../lib/factory-floor";
import type { Fleet } from "../../lib/infra-view";
import type { InfraTabProps } from "./infra-home";
import { Empty, Skeleton } from "./kit";

export function FleetTab({ fleet }: InfraTabProps & { fleet: Polled<Fleet> }) {
  if (fleet.loading) return <Skeleton testId="infra-fleet-loading" />;
  return <Empty testId="infra-fleet-empty">No machines yet</Empty>;
}

export function InfraAddMenu(_props: InfraTabProps & { fleet: Polled<Fleet> }) {
  return null;
}

export function InfraAskBoss(_props: InfraTabProps & { fleet: Polled<Fleet> }) {
  return null;
}
