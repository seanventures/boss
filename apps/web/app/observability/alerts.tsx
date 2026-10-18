"use client";

/**
 * Observability › ALERTS (BOSSAPP-598): every app's firing and recent alerts.
 *
 *   [ All apps ▾ ] [ Firing ▾ ] [ Any severity ▾ ]                        2 firing
 *    ● Order API     Error rate 14.4%    07:05   Agent: Opened an incident   Responding   ▸
 *    ○ HelpDesk AI   p95 latency 900 ms  Oct 17  Agent: Noise                              ▸
 *
 * Filters live in the URL (aapp, astate, asev). An app's name opens its drawer on the Alerts
 * tab — the one place its rules are changed (one surface per job).
 */
import React from "react";

import { useAllAlerts } from "../../lib/app-alerts";
import { DrawerSection, Skeleton } from "../infra/kit";
import { AlertRow } from "./alerts-tab";
import { AppDrawer } from "./app-drawer";
import type { ObsTabProps } from "./obs-home";

const STATES = [{ key: "firing", label: "Firing" }, { key: "resolved", label: "Resolved" }, { key: "all", label: "All states" }] as const;
const SEVERITIES = [{ key: "", label: "Any severity" }, { key: "critical", label: "High" }, { key: "warning", label: "Low" }] as const;

export function AlertsTab(props: ObsTabProps) {
  const { projectId, actor, url, go } = props;
  const all = useAllAlerts(projectId, { app: url.aapp, state: url.astate, severity: url.asev });
  const d = all.data;
  const q = props.query.trim().toLowerCase();
  const rows = (d?.alerts ?? []).filter((a) => !q || `${a.app} ${a.env} ${a.label} ${a.summary}`.toLowerCase().includes(q));
  return (
    <div className="obs-zone obs-allalerts" data-testid="obs-alerts">
      <div className="obs-controls" data-testid="obs-alerts-filters">
        <span className="obs-dropchip" data-on={url.aapp ? "1" : undefined}>
          <select aria-label="App" value={url.aapp ?? ""} data-testid="obs-alerts-app"
                  onChange={(e) => go({ aapp: e.target.value || null }, "replace")}>
            <option value="">All apps</option>
            {(d?.apps ?? []).map((a) => <option key={a.targetId} value={a.targetId}>{a.app} · {a.env}</option>)}
          </select>
        </span>
        <span className="obs-dropchip" data-on={url.astate !== "firing" ? "1" : undefined}>
          <select aria-label="State" value={url.astate} data-testid="obs-alerts-state"
                  onChange={(e) => go({ astate: e.target.value === "firing" ? null : e.target.value }, "replace")}>
            {STATES.map((s) => <option key={s.key} value={s.key}>{s.label}</option>)}
          </select>
        </span>
        <span className="obs-dropchip" data-on={url.asev ? "1" : undefined}>
          <select aria-label="Severity" value={url.asev ?? ""} data-testid="obs-alerts-severity"
                  onChange={(e) => go({ asev: e.target.value || null }, "replace")}>
            {SEVERITIES.map((s) => <option key={s.key} value={s.key}>{s.label}</option>)}
          </select>
        </span>
        <span className="ag-spacer" />
        {d ? <span className="inf-faint inf-num" data-testid="obs-alerts-count"
                   title="Firing now, across every app">{d.counts.firing} firing</span> : null}
      </div>
      <DrawerSection label="ALERTS" testId="obs-alerts-list">
        {!d ? (all.error ? <span className="inf-error" title={all.error}>Can&apos;t load alerts</span> : <Skeleton testId="obs-alerts-loading" />)
          : rows.length === 0 ? (
            <span className="inf-empty" data-testid="obs-alerts-empty">
              {url.astate === "firing" ? "Nothing firing" : "No alerts in the last 7 days"}
            </span>
          ) : (
            <ul className="obs-list obs-alerts">
              {rows.map((a) => (
                <AlertRow key={a.id} a={a} targetId={a.targetId} projectId={projectId} actor={actor} showApp
                          onDone={all.refresh}
                          onApp={() => go({ app: a.targetId, atab: "alerts" }, "push")}
                          onIncident={() => go({ app: a.targetId, atab: null }, "push")} />
              ))}
            </ul>
          )}
      </DrawerSection>
      {url.app ? <AppDrawer {...props} targetId={url.app} row={props.pulse.data?.rows.find((r) => r.targetId === url.app) ?? null} /> : null}
    </div>
  );
}
