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
  DeploymentHealth, dashboardKey, isHealthUnconfigured,
  type DashboardIndex, type Health,
} from "./delivery-ui";
import {
  isObservabilityUnconfigured, MonitoringPanel, type Observability,
} from "./delivery-manage";

export function MonitoringArea({ health, observability, hostTargetId }: {
  health?: Health | null;
  observability?: Observability | null;
  hostTargetId?: string;
}) {
  // `?watch=app|environment`, handed over by the workflow's "Set up monitoring" step. Read
  // here rather than passed down from the page: the area is client-side and the param can
  // change without a server render.
  const [watch, setWatch] = React.useState("");
  React.useEffect(() => {
    const read = () => {
      const raw = new URLSearchParams(window.location.search).get("watch") ?? "";
      const [app, env] = raw.split("|");
      setWatch(raw ? dashboardKey(app ?? "", env ?? "") : "");
    };
    read();
    window.addEventListener("popstate", read);
    return () => window.removeEventListener("popstate", read);
  }, []);

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

  // ONE PANEL. These were two blocks listing the same three apps: one saying each was up, the
  // other saying where its dashboard was. "Is it healthy" and "show me its graphs" are the same
  // question asked twice, and answering them in two places meant scrolling between them to
  // follow one app.
  const dashboards: DashboardIndex = new Map(
    (observability?.apps ?? []).map((a) => [
      dashboardKey(a.app, a.environment),
      { dashboard_url: a.dashboard_url, scrape_url: `${a.scrape_url}/metrics` },
    ]),
  );

  // An app the stack scrapes but health does not know about would vanish in the merge. Health
  // is keyed on deployment TARGETS and the stack on what it was told to scrape, and the two can
  // legitimately differ — so anything unmatched is named rather than dropped.
  const healthKeys = new Set(
    (health?.targets ?? []).map((t) => dashboardKey(t.app_name, t.environment)));
  const orphans = (observability?.apps ?? []).filter(
    (a) => !healthKeys.has(dashboardKey(a.app, a.environment)));

  return (
    <div style={{ display: "grid", gap: 18 }} data-empty-state="monitoring-active">
      <DeploymentHealth
        health={health}
        dashboards={dashboards}
        watch={watch}
        footer={
          <>
            {orphans.length > 0 ? (
              <div className="detail" style={{ fontSize: 11, marginBottom: 8 }}
                   data-testid="monitoring-orphans">
                Scraped but not health-checked:{" "}
                {orphans.map((a) => `${a.app} · ${a.environment}`).join(", ")} — these are on a
                dashboard without a health target watching them.
              </div>
            ) : null}
            <MonitoringPanel initial={observability} hostTargetId={hostTargetId} compact />
          </>
        }
      />
      {/* Health empty but the stack is not: the merged panel renders nothing, so the stack
          still needs somewhere to live. */}
      {healthEmpty && !stackEmpty ? (
        <MonitoringPanel initial={observability} hostTargetId={hostTargetId} />
      ) : null}
    </div>
  );
}
