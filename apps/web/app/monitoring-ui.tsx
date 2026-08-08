"use client";

/**
 * Monitoring — is what BOSS deployed actually working?
 *
 * Split out of the CI/CD area because that page was answering two different questions.
 * "Did my change ship?" is asked once, at merge time, by whoever wrote it. "Is it healthy?" is
 * asked repeatedly, often by someone who did not write it and does not care which PR it came
 * from. One page serving both buries whichever you did not come for, and during an incident the
 * thing you did not come for is a list of pull requests.
 *
 * The split is presentational only: both areas read the same `deployment_targets`, the same
 * health samples and the same generated dashboards, so they cannot disagree about what exists.
 */

import { DeploymentHealth, type Health } from "./delivery-ui";
import { MonitoringPanel, type Observability } from "./delivery-manage";

export function MonitoringArea({ health, observability, hostTargetId }: {
  health?: Health | null;
  observability?: Observability | null;
  hostTargetId?: string;
}) {
  return (
    <div style={{ display: "grid", gap: 18 }}>
      {/* Health first: the state of the world outranks the machinery that reports it. */}
      <DeploymentHealth health={health} />
      <MonitoringPanel initial={observability} hostTargetId={hostTargetId} />
    </div>
  );
}
