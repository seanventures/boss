"use client";

/**
 * Observability › ALERTS (BOSSAPP-598): every app's open incidents, then its alerts. Only those:
 * the generic Needs-you strip is not drawn on this tab (obs-home.tsx).
 *
 *   OPEN INCIDENTS
 *    ● api (prod): 14.4% failing        Firing: 2 alerts                                       ▸
 *    ○ invoice: p95 slow                Recovered at 22:46; a response is waiting on you        ▸
 *   [ All apps ▾ ] [ Firing ▾ ] [ Any severity ▾ ] [ Any source ▾ ] [ Flapping ▾ ] [ Acked ▾ ]   2 firing
 *   [ ] ● Order API  Error rate 14.4%  07:05  Agent: Opened an incident  Alertmanager  Flapping  Ack  ▸
 *   2 selected  [ Ack selected ]
 *
 * Filters live in the URL (aapp, astate, asev, asrc, aflap, aack). An app's name opens its
 * drawer on the Alerts tab — the one place its rules are changed (one surface per job). Ack stops
 * repeats for 4 hours; it never silences.
 */
import React, { useState } from "react";

import { GatedButton } from "../../components/gated";
import { ACK_PATH, ALERT_SOURCES, useAllAlerts, type OpenIncident } from "../../lib/app-alerts";
import { useLocalTime } from "../../lib/local-time";
import { showToast } from "../client-forms";
import { notifyChanged } from "../changed";
import { DrawerSection, Skeleton } from "../infra/kit";
import { AlertRow, whenWord } from "./alerts-tab";
import { AppDrawer } from "./app-drawer";
import { obsWrite } from "./obs-write";
import type { ObsTabProps } from "./obs-home";

const STATES = [{ key: "firing", label: "Firing" }, { key: "resolved", label: "Resolved" }, { key: "all", label: "All states" }] as const;
const SEVERITIES = [{ key: "", label: "Any severity" }, { key: "critical", label: "High" }, { key: "warning", label: "Low" }] as const;
const TRI = (yes: string, no: string, any: string) => [{ key: "", label: any }, { key: "true", label: yes }, { key: "false", label: no }];
const FLAP = TRI("Flapping", "Not flapping", "Flapping or not");
const ACKED = TRI("Acked", "Not acked", "Acked or not");
const triKey = (v: boolean | null) => (v === true ? "true" : v === false ? "false" : "");

function IncidentRow({ i, onOpen }: { i: OpenIncident; onOpen: (() => void) | null }) {
  const lt = useLocalTime();
  // An open incident with nothing firing has recovered: never drawn as failing.
  const tone = i.happening && i.firing > 0 ? "red" : "grey";
  return (
    <li className="obs-line" data-testid={`obs-incident-${i.id}`} data-happening={i.happening ? "1" : undefined}>
      <span className="obs-adot" data-tone={tone} aria-hidden />
      <span className="obs-strong obs-alert-what" data-user-text title={i.title}>{i.title}</span>
      <span className="inf-muted" data-testid="obs-incident-word" data-copy="data">{i.word}</span>
      {i.urgency === "low" ? <span className="obs-chip" title="Low urgency: never pages">Low urgency</span> : null}
      {i.acked ? <span className="obs-chip" data-tone="green" title="Someone acked it: repeats stopped">Acked</span> : null}
      <span className="ag-spacer" />
      {i.since ? <span className="inf-faint inf-num" title={lt.full(i.since)}>{whenWord(i.since, lt)}</span> : null}
      {onOpen ? (
        <button type="button" className="ag-icon-btn" aria-label="Open the incident" title="Its timeline, triage and response"
                data-testid="obs-incident-open" onClick={onOpen}>▸</button>
      ) : null}
    </li>
  );
}

export function AlertsTab(props: ObsTabProps & { onConnect?: () => void }) {
  const { projectId, actor, url, go } = props;
  const all = useAllAlerts(projectId, { app: url.aapp, state: url.astate, severity: url.asev, source: url.asrc,
                                        flapping: url.aflap, acked: url.aack });
  const [picked, setPicked] = useState<Set<string>>(new Set());
  const [busy, setBusy] = useState(false);
  const d = all.data;
  const q = props.query.trim().toLowerCase();
  const rows = (d?.alerts ?? []).filter((a) => !q || `${a.app} ${a.env} ${a.label} ${a.summary} ${a.source ?? ""}`.toLowerCase().includes(q));
  const ackable = new Set(rows.filter((a) => a.state !== "resolved" && !a.acked).map((a) => a.id));
  const chosen = [...picked].filter((id) => ackable.has(id));

  const ack = async (ids: string[]) => {
    if (!ids.length || busy) return;
    setBusy(true);
    const r = await obsWrite<{ acked?: string[]; until?: string }>(ACK_PATH, { project_id: projectId, actor, alert_ids: ids },
                                                                   { scope: `obs:ack:${ids.join(",")}`, label: "Ack" });
    setBusy(false);
    if (r.ok) {
      const n = r.body?.acked?.length ?? ids.length;
      showToast("success", `Acked ${n} alert${n === 1 ? "" : "s"}: repeats stop for 4 hours, nothing is silenced`);
      setPicked(new Set());
      notifyChanged("observability", { ack: ids.join(",") });
      all.refresh();
    } else showToast("error", r.error ?? "Not acked");
  };
  const toggle = (id: string, on: boolean) => setPicked((s) => { const n = new Set(s); if (on) n.add(id); else n.delete(id); return n; });
  const incidents = d?.incidents ?? [];
  const appOf = (subject: string | null) => (subject && d?.apps.some((a) => a.targetId === subject) ? subject : null);

  return (
    <div className="obs-zone obs-allalerts" data-testid="obs-alerts">
      {incidents.length ? (
        <DrawerSection label="OPEN INCIDENTS" testId="obs-incidents">
          <ul className="obs-list">
            {incidents.map((i) => {
              const app = appOf(i.subjectId);
              return <IncidentRow key={i.id} i={i} onOpen={app ? () => go({ app, atab: null }, "push") : null} />;
            })}
          </ul>
        </DrawerSection>
      ) : null}
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
        <span className="obs-dropchip" data-on={url.asrc ? "1" : undefined} title="Where the alert came from">
          <select aria-label="Source" value={url.asrc ?? ""} data-testid="obs-alerts-source"
                  onChange={(e) => go({ asrc: e.target.value || null }, "replace")}>
            <option value="">Any source</option>
            {ALERT_SOURCES.map((s) => <option key={s.key} value={s.key} title={s.tip}>{s.label}</option>)}
          </select>
        </span>
        <span className="obs-dropchip" data-on={url.aflap !== null ? "1" : undefined} title="Firing and clearing over and over">
          <select aria-label="Flapping" value={triKey(url.aflap)} data-testid="obs-alerts-flapping"
                  onChange={(e) => go({ aflap: e.target.value || null }, "replace")}>
            {FLAP.map((s) => <option key={s.key} value={s.key}>{s.label}</option>)}
          </select>
        </span>
        <span className="obs-dropchip" data-on={url.aack !== null ? "1" : undefined} title="Acked: repeats stopped, not silenced">
          <select aria-label="Acked" value={triKey(url.aack)} data-testid="obs-alerts-acked"
                  onChange={(e) => go({ aack: e.target.value || null }, "replace")}>
            {ACKED.map((s) => <option key={s.key} value={s.key}>{s.label}</option>)}
          </select>
        </span>
        <span className="ag-spacer" />
        {d ? <span className="inf-faint inf-num" data-testid="obs-alerts-count"
                   title="Firing now, across every app">{d.counts.firing} firing</span> : null}
      </div>
      {chosen.length ? (
        <div className="obs-line" data-testid="obs-alerts-bulk">
          <span className="inf-muted">{chosen.length} selected</span>
          <GatedButton permission="observability:manage" type="button" className="ag-btn ag-btn-sm" data-testid="obs-alerts-ack-selected"
                       disabled={busy} aria-busy={busy || undefined} title="Stops repeat notifications for 4 hours. It does not silence."
                       onClick={() => void ack(chosen)}>{busy ? "Acking…" : "Ack selected"}</GatedButton>
          <button type="button" className="inf-link" onClick={() => setPicked(new Set())}>Clear</button>
        </div>
      ) : null}
      <DrawerSection label="ALERTS" testId="obs-alerts-list">
        {!d ? (all.error ? <span className="inf-error" title={all.error}>Can&apos;t load alerts</span> : <Skeleton testId="obs-alerts-loading" />)
          : rows.length === 0 ? (
            <span className="obs-line" data-testid="obs-alerts-empty-line">
              <span className="inf-empty" data-testid="obs-alerts-empty">
                {url.astate === "firing" ? "Nothing firing" : "No alerts in the last 7 days"}
              </span>
              {props.onConnect ? (
                <GatedButton permission="observability:manage" type="button" className="inf-link" data-testid="obs-alerts-connect"
                             title="Bring an app's metrics or alerts into BOSS" onClick={props.onConnect}>Connect an app</GatedButton>
              ) : null}
            </span>
          ) : (
            <ul className="obs-list obs-alerts">
              {rows.map((a) => (
                <AlertRow key={a.id} a={a} targetId={a.targetId} projectId={projectId} actor={actor} showApp
                          onDone={all.refresh}
                          selected={picked.has(a.id)} onSelect={ackable.has(a.id) ? (on) => toggle(a.id, on) : undefined}
                          onAck={() => void ack([a.id])}
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
