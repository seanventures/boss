"use client";

/**
 * Grafana, linked only once BOSS has checked that it answers (operator decision 2026-10-18).
 *
 *   header   Grafana ↗                       or  [Grafana not answering] [Check]
 *   drawer   Dashboard                      or  No dashboard, why (tooltip) [Check] [Deploy stack]
 *
 * The state comes from the server's RECORDED check (observability_stack.endpoint_state): a link
 * built by concatenation to a host whose port does not answer looked exactly like a working one.
 * `Check` asks Grafana now (POST /v1/observability/grafana/check); `Deploy stack` opens Sources,
 * where the stack is deployed, one surface per job.
 */
import React, { useState } from "react";

import { GatedButton } from "../../components/gated";
import { GRAFANA_CHECK_PATH, type Grafana } from "../../lib/app-alerts";
import { showToast } from "../client-forms";
import { notifyChanged } from "../changed";
import { obsWrite } from "./obs-write";

const SHORT: Record<Grafana["state"], string> = {
  answering: "Grafana answers", not_answering: "Grafana not answering", unchecked: "Grafana not checked",
  not_deployed: "No monitoring stack",
};

function useCheck(projectId: string, actor: string, after: () => void) {
  const [busy, setBusy] = useState(false);
  const check = async () => {
    if (busy) return;
    setBusy(true);
    const r = await obsWrite<{ grafana?: { state?: string; reason?: string } }>(GRAFANA_CHECK_PATH, { project_id: projectId, actor },
                                                                               { scope: "obs:grafana-check", label: "Check" });
    setBusy(false);
    if (r.ok) {
      const st = r.body?.grafana?.state;
      showToast(st === "answering" ? "success" : "error", st === "answering" ? "Grafana answers" : (r.body?.grafana?.reason ?? "Grafana isn't answering"));
      notifyChanged("observability", { grafana: "checked" });
      after();
    } else {
      showToast("error", r.error ?? "Could not check");
    }
  };
  return { busy, check };
}

function tipOf(g: Grafana, reason: string | null): string {
  const why = g.why ? ` (${g.why})` : "";
  return `${reason ?? g.reason ?? SHORT[g.state]}${why}`;
}

/** Observability's header: `Grafana ↗` once it answers, else the state and Check. */
export function GrafanaHeaderLink({ g, projectId, actor, onChecked }: { g: Grafana; projectId: string; actor: string; onChecked: () => void }) {
  const { busy, check } = useCheck(projectId, actor, onChecked);
  if (g.state === "answering" && g.url) {
    return <a className="inf-link" href={g.url} target="_blank" rel="noreferrer noopener" data-testid="obs-grafana" title="Open Grafana">Grafana ↗</a>;
  }
  return (
    <span className="obs-grafana-off" data-testid="obs-grafana" data-state={g.state}>
      <span className="obs-chip" data-tone={g.state === "not_answering" ? "amber" : undefined} title={tipOf(g, null)}
            data-testid="obs-grafana-word">{SHORT[g.state]}</span>
      {g.state !== "not_deployed" ? (
        <GatedButton permission="observability:manage" type="button" className="ag-btn ag-btn-sm" data-testid="obs-grafana-check"
                     disabled={busy} aria-busy={busy || undefined} title="Ask Grafana now" onClick={check}>
          {busy ? "Checking…" : "Check"}
        </GatedButton>
      ) : null}
    </span>
  );
}

/** The app drawer's `Dashboard ↗`, or why there is none, with Check and Deploy stack. */
export function DashboardLink({ g, url, reason, projectId, actor, onChecked, onDeploy }: {
  g: Grafana; url: string | null; reason: string | null; projectId: string; actor: string;
  onChecked: () => void; onDeploy: () => void;
}) {
  const { busy, check } = useCheck(projectId, actor, onChecked);
  if (url && g.state === "answering") {
    return <a className="inf-link" href={url} target="_blank" rel="noreferrer noopener" data-testid="obs-app-dashboard"
              title="This app's Grafana dashboard">Dashboard ↗</a>;
  }
  return (
    <span className="obs-grafana-off" data-testid="obs-app-dashboard" data-state={g.state}>
      <span className="inf-faint" title={tipOf(g, reason)} data-testid="obs-app-dashboard-why">No dashboard</span>
      {g.state !== "not_deployed" ? (
        <GatedButton permission="observability:manage" type="button" className="ag-btn ag-btn-sm" data-testid="obs-app-dashboard-check"
                     disabled={busy} aria-busy={busy || undefined} title="Ask Grafana now" onClick={check}>
          {busy ? "Checking…" : "Check"}
        </GatedButton>
      ) : null}
      <button type="button" className="ag-btn ag-btn-sm" data-testid="obs-app-dashboard-deploy" title="Deploy the monitoring stack in Sources"
              onClick={onDeploy}>Deploy stack</button>
    </span>
  );
}
