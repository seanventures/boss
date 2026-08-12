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
 *
 * Empty honesty (BOSS-344): when nothing is registered and the stack is not deployed, this
 * area must not paint "● not_deployed / stack not running" chrome that reads like a live
 * fleet. Setup copy + a path back to CI/CD onboarding only.
 */

import React from "react";

import {
  DeploymentHealth, isHealthUnconfigured, type Health,
} from "./delivery-ui";
import {
  isObservabilityUnconfigured, MonitoringPanel, type Observability,
} from "./delivery-manage";

export function MonitoringArea({ health, observability, hostTargetId }: {
  health?: Health | null;
  observability?: Observability | null;
  hostTargetId?: string;
}) {
  const healthEmpty = isHealthUnconfigured(health);
  const stackEmpty = isObservabilityUnconfigured(observability);
  // Both empty: one combined setup card beats two near-identical "nothing here" blocks.
  if (healthEmpty && stackEmpty) {
    return (
      <div style={{ display: "grid", gap: 18 }} data-empty-state="monitoring-unconfigured">
        <section className="ct-block">
          <div className="ct-block-title">Monitoring is not set up yet</div>
          <p className="empty-state" style={{ padding: "18px 0" }}>
            No deployment targets are registered and the observability stack is not
            deployed. There is no health fleet, no Prometheus, and no Grafana for this
            project.
          </p>
          <p className="detail" style={{ marginTop: 0 }}>
            Start in the CI/CD area: onboard an app (host + credential + environment),
            deploy it, then return here to deploy the monitoring stack onto that host.
          </p>
        </section>
      </div>
    );
  }

  return (
    <div style={{ display: "grid", gap: 18 }} data-empty-state="monitoring-active">
      {/* Health first: the state of the world outranks the machinery that reports it. */}
      <DeploymentHealth health={health} />
      <MonitoringPanel initial={observability} hostTargetId={hostTargetId} />
    </div>
  );
}
