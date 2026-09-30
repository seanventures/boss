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


/**
 * Ask BOSS here, about what is on this screen.
 *
 * The same handoff the Deployment Manager and the workflow steps use — `?agent=general` opens
 * the side dock, `?dock_ask=` prefills it — rather than a fourth chat surface with its own
 * habits. Monitoring is where the question "is this actually working, and what should we watch
 * instead?" gets asked, and it was the one operating screen with no way to ask it.
 *
 * The starters are built from the fleet ON SCREEN, not from a fixed list: naming the app that
 * is actually down beats "ask about an app", and it means the first question already carries
 * the facts BOSS would otherwise spend three tool calls rediscovering.
 */
function askInDock(question: string): void {
  try {
    const url = new URL(window.location.href);
    url.searchParams.set("agent", "general");
    url.searchParams.set("dock_ask", question);
    // ADDRESS IT, DO NOT INHERIT IT. `dock_role` is a URL parameter and SURVIVES until
    // something overwrites it, so a door that sets only the question opens whichever role was
    // last addressed. This one asks BOSS in general (`agent=general`), so it clears the role
    // rather than leaving the previous agent's name on the banner above an unrelated answer.
    url.searchParams.delete("dock_role");
    window.history.replaceState(null, "", url.toString());
    window.dispatchEvent(new PopStateEvent("popstate"));
  } catch { /* non-browser */ }
}

/** What is worth asking about, given what this screen is currently showing. */
export function monitoringStarters(health?: Health | null): string[] {
  const targets = health?.targets ?? [];
  const sick = targets.filter((t) => t.status === "down" || t.status === "degraded");
  const unknown = targets.filter((t) => t.status === "unknown" || t.status === "not_deployed");
  const out: string[] = [];

  if (sick.length === 1) {
    const t = sick[0];
    out.push(
      `${t.app_name} (${t.environment}) is ${t.status}${t.url ? ` at ${t.url}` : ""}` +
      `${t.last_error ? ` — last error: ${t.last_error}` : ""}. Why, and what is the fix?`);
  } else if (sick.length > 1) {
    out.push(`${sick.map((t) => t.app_name).join(", ")} are not healthy. ` +
             "Check each one against the host and tell me what is actually wrong.");
  } else if (targets.length) {
    out.push("Everything here reads healthy. Check the probes are really running and that " +
             "each app answers on the port its target claims — not just that the row is green.");
  }

  if (unknown.length) {
    out.push(`${unknown.map((t) => t.app_name).join(", ")} ` +
             `${unknown.length === 1 ? "has" : "have"} no health signal. ` +
             "Is that because nothing is deployed, or because nothing is probing it?");
  }

  // The reason an operator comes to this screen with an idea rather than an incident.
  out.push("I want to change what we monitor. What is being checked for each app today, " +
           "what would you add, and what can you set up yourself?");
  return out;
}

function AskBossAboutMonitoring({ health }: { health?: Health | null }) {
  const starters = monitoringStarters(health);
  return (
    <section className="ct-block" data-testid="monitoring-ask">
      <div className="ct-block-title">Ask BOSS about this</div>
      {/* THE LIMIT, SAID UP FRONT — the same sentence the Deployment Manager leads with, for
          the same reason: an operator who believes this can silently change monitoring will
          phrase a request as an instruction and read the reply as confirmation. */}
      <p className="detail" data-testid="monitoring-ask-scope">
        Reads the health probes, the deployment targets and the dashboards, and can look at the
        hosts themselves. Changes to what is monitored are proposed as buttons — nothing is
        reconfigured until you click.
      </p>
      <div className="dm-starters">
        {starters.map((q) => (
          <button type="button" className="cap-btn" key={q} onClick={() => askInDock(q)}
                  data-testid="monitoring-starter">
            {q.length > 96 ? `${q.slice(0, 96)}…` : q}
          </button>
        ))}
      </div>
      <div className="dm-starters">
        <button type="button" className="cap-btn" data-testid="monitoring-open-dock"
                onClick={() => askInDock("")}>
          Open BOSS here
        </button>
      </div>
    </section>
  );
}

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
        {/* Especially here: "nothing is set up" is exactly when an operator wants to ask what
            setting it up would involve, and this was the one screen with no way to ask. */}
        <AskBossAboutMonitoring health={health} />
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
      <AskBossAboutMonitoring health={health} />
    </div>
  );
}
