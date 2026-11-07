"use client";

/**
 * Observability › ALERTS (BOSSAPP-598): every app's open incidents, then its alerts. Only those:
 * the generic Needs-you strip is not drawn on this tab (obs-home.tsx).
 *
 * ONE CONCEPT, TWO SCOPES: AlertsPanel is also a project's Health › Alerts. Observability lists
 * every app of every project you may read plus BOSS's own platform (GET /v1/alerts/all); Health
 * lists its project's slice of the same rows (GET /v1/alerts?scope=project). The lead line says
 * which ("All apps · 6 · and BOSS itself" / "This project · HelpDesk") and links to the other.
 *
 *   [ 3 Needs attention ]  All apps · 6 · and BOSS itself  Only HelpDesk › Health →
 *   OPEN INCIDENTS
 *    ● api (prod): 14.4% failing        Firing: 2 alerts                                       ▸
 *    ○ invoice: p95 slow                Recovered at 22:46; a response is waiting on you        ▸
 *   [ All apps ▾ ] [ Firing ▾ ] [ Any severity ▾ ] [ Any source ▾ ] [ Flapping ▾ ] [ Acked ▾ ]   2 firing
 *   [ ] ● Order API  Error rate 14.4%  07:05  Agent: Opened an incident  Alertmanager  Flapping  Ack  ▸
 *   2 selected  [ Ack selected ]
 *
 * Filters live in the URL (aapp, astate, asev, asrc, aflap, aack, aatt). Every row and card carries
 * its SEV flag and a status chip (Firing · Acknowledged · Pending · Resolved · Silenced). An app's name opens its
 * drawer on the Alerts tab — the one place its rules are changed (one surface per job). Ack stops
 * repeats for 4 hours; it never silences.
 */
import React, { useState } from "react";

import { GatedButton } from "../../components/gated";
import { ACK_PATH, ALERT_SOURCES, SEVERITIES as SEV_CATALOG, useAlerts, type Alert, type AlertProject, type AlertsWhere, type OpenIncident, type SevInfo } from "../../lib/app-alerts";
import type { ObsUrl } from "../../lib/obs-view";
import { navigateTo, pxHref } from "../../lib/project-experience";
import { useLocalTime } from "../../lib/local-time";
import { showToast } from "../client-forms";
import { notifyChanged } from "../changed";
import { DrawerSection, Skeleton } from "../infra/kit";
import { AlertRow, whenWord } from "./alerts-tab";
import { AppDrawer } from "./app-drawer";
import { obsWrite } from "./obs-write";
import { RuleBuilder } from "./rule-builder";
import { SevFlag, StatusChip } from "./severity";
import type { ObsTabProps } from "./obs-home";

const STATES = [{ key: "firing", label: "Firing" }, { key: "resolved", label: "Resolved" }, { key: "all", label: "All states" }] as const;
const TRI = (yes: string, no: string, any: string) => [{ key: "", label: any }, { key: "true", label: yes }, { key: "false", label: no }];
const FLAP = TRI("Flapping", "Not flapping", "Flapping or not");
const ACKED = TRI("Acked", "Not acked", "Acked or not");
const triKey = (v: boolean | null) => (v === true ? "true" : v === false ? "false" : "");

export type AlertsFilterState = Pick<ObsUrl, "aapp" | "astate" | "asev" | "asrc" | "aflap" | "aack" | "aatt">;
export const NO_ALERT_FILTER: AlertsFilterState = { aapp: null, astate: "firing", asev: null, asrc: null, aflap: null, aack: null, aatt: false };

/** "…closes on its own unless it fires again — by 08:07": the incident's word, in the viewer's clock. */
function incidentWord(i: OpenIncident, lt: ReturnType<typeof useLocalTime>): string {
  const base = i.recoveredAt && i.word.startsWith("Recovered") ? `Recovered at ${whenWord(i.recoveredAt, lt)}${i.word.slice("Recovered".length)}` : i.word;
  return i.openReason?.closesAt ? `${base} — by ${whenWord(i.openReason.closesAt, lt)}` : base;
}

function ProjectChip({ p, testId }: { p: AlertProject | null; testId: string }) {
  if (!p) return null;
  return <a className="obs-chip" href={pxHref(p.href)} data-testid={testId} title={`${p.name}: its Health tab`}>{p.name}</a>;
}

function IncidentRow({ i, onOpen, severities, showProject }: { i: OpenIncident; onOpen: (() => void) | null; severities: SevInfo[]; showProject: boolean }) {
  const lt = useLocalTime();
  // An open incident with nothing firing has recovered: never drawn as failing.
  const tone = i.happening && i.firing > 0 ? "red" : "grey";
  return (
    <li className="obs-line" data-testid={`obs-incident-${i.id}`} data-happening={i.happening ? "1" : undefined}
        data-why={i.openReason?.key}>
      <span className="obs-adot" data-tone={tone} aria-hidden />
      <SevFlag sev={i.sev} catalog={severities} />
      <StatusChip status={i.chip} testId="obs-incident-status"
                  extraTip={i.ackedBy ? `Acknowledged by ${i.ackedBy}` : null} />
      <span className="obs-strong obs-alert-what" data-user-text title={i.title}>{i.title}</span>
      {showProject ? (i.platform ? <span className="obs-chip" data-testid="obs-incident-project">BOSS</span>
        : <ProjectChip p={i.project} testId="obs-incident-project" />) : null}
      <span className="inf-muted" data-testid="obs-incident-word" data-copy="data"
            title={[i.recoveredAt ? `Recovered at ${lt.full(i.recoveredAt)}` : "", i.openReason?.closesAt ? `Closes on its own at ${lt.full(i.openReason.closesAt)}` : ""].filter(Boolean).join(" · ") || undefined}>
        {incidentWord(i, lt)}
      </span>
      {i.urgency === "low" ? <span className="obs-chip" title="Low urgency: never pages">Low urgency</span> : null}
      <span className="ag-spacer" />
      {i.since ? <span className="inf-faint inf-num" title={lt.full(i.since)}>{whenWord(i.since, lt)}</span> : null}
      {onOpen ? (
        <button type="button" className="ag-icon-btn" aria-label="Open the incident" title="Its timeline, triage and response"
                data-testid="obs-incident-open" onClick={onOpen}>▸</button>
      ) : null}
    </li>
  );
}

/**
 * ONE ALERTS LIST, TWO SCOPES (operator, 2026-11-07: "what is Alerts under Observability vs Alerts
 * under Health — are they same?"). The same component and the same rows: a project's Health shows
 * its slice (`where: project`), Observability every app of every project you may read plus BOSS's
 * own platform (`where: all`). The first line says which, and links to the other.
 */
export function AlertsPanel({ where, projectId, actor, f, setF, query = "", onConnect, openApp, openIncident, openAlertIncident, otherHref }: {
  where: AlertsWhere; projectId: string; actor: string; f: AlertsFilterState; setF: (patch: Record<string, string | null>) => void;
  query?: string; onConnect?: () => void;
  openApp: (a: Pick<Alert, "targetId" | "project" | "platform">) => void; openIncident: (i: OpenIncident, app: string | null) => (() => void) | null;
  openAlertIncident: (a: Alert) => void; otherHref: string | null;
}) {
  const all = useAlerts(where, { app: f.aapp, state: f.astate, severity: f.asev, source: f.asrc,
                                 flapping: f.aflap, acked: f.aack, attention: f.aatt });
  const [picked, setPicked] = useState<Set<string>>(new Set());
  const [busy, setBusy] = useState(false);
  const [building, setBuilding] = useState(false);
  const d = all.data;
  const across = where.kind === "all";
  const severities = d?.severities ?? SEV_CATALOG;
  const q = query.trim().toLowerCase();
  const rows = (d?.alerts ?? []).filter((a) => !q || `${a.app} ${a.env} ${a.label} ${a.summary} ${a.source ?? ""} ${a.project?.name ?? ""}`.toLowerCase().includes(q));
  const ackable = new Set(rows.filter((a) => a.state !== "resolved" && !a.acked).map((a) => a.id));
  const chosen = [...picked].filter((id) => ackable.has(id));
  const projectOf = (id: string) => rows.find((a) => a.id === id)?.project?.id ?? projectId;

  const ack = async (ids: string[]) => {
    if (!ids.length || busy) return;
    setBusy(true);
    // Each project acks its own alerts (Observability lists several projects' at once).
    const byProject = new Map<string, string[]>();
    for (const id of ids) byProject.set(projectOf(id), [...(byProject.get(projectOf(id)) ?? []), id]);
    let n = 0;
    let error: string | null = null;
    for (const [pid, list] of byProject) {
      const r = await obsWrite<{ acked?: string[]; until?: string }>(ACK_PATH, { project_id: pid, actor, alert_ids: list },
                                                                     { scope: `obs:ack:${list.join(",")}`, label: "Ack" });
      if (r.ok) n += r.body?.acked?.length ?? list.length;
      else error = r.error ?? "Not acked";
    }
    setBusy(false);
    if (!error) {
      showToast("success", `Acked ${n} alert${n === 1 ? "" : "s"}: repeats stop for 4 hours, nothing is silenced`);
      setPicked(new Set());
      notifyChanged("observability", { ack: ids.join(",") });
      all.refresh();
    } else showToast("error", error);
  };
  const toggle = (id: string, on: boolean) => setPicked((s) => { const n = new Set(s); if (on) n.add(id); else n.delete(id); return n; });
  const incidents = d?.incidents ?? [];
  const appOf = (subject: string | null) => (subject && d?.apps.some((a) => a.targetId === subject) ? subject : null);
  // A rule is written to the project you are in: the builder offers only its apps.
  const ownApps = (d?.apps ?? []).filter((a) => !a.service && (!a.project || a.project.id === projectId));
  const scope = d?.scope ?? null;
  const here = across ? (d?.apps.find((a) => a.project?.id === projectId)?.project?.name ?? rows.find((a) => a.project?.id === projectId)?.project?.name ?? null) : null;

  const attention = d?.counts.needsAttention ?? 0;
  return (
    <div className="obs-zone obs-allalerts" data-testid="obs-alerts" data-scope={where.kind}>
      <div className="obs-alerts-lead">
        <button type="button" className="obs-attention" aria-pressed={f.aatt} data-n={d ? String(attention) : "0"}
                data-testid="obs-alerts-attention"
                title={f.aatt ? "Showing only what needs attention; press for every alert" : "Firing and nobody has acknowledged it. Press to show only those."}
                onClick={() => setF({ aatt: f.aatt ? null : "1", astate: null })}>
          <span className="obs-attention-n" data-testid="obs-alerts-attention-n">{d ? attention : "…"}</span>
          <span>Needs attention</span>
        </button>
        {/* WHAT THIS LIST COVERS, and the way to the other scope of the same list. */}
        <span className="obs-scope" data-testid="obs-alerts-scope"
              title={across ? "Every app you may read, and BOSS itself; each project's Health shows its slice"
                : "This project's apps only; Observability shows the same list for every app"}>
          {across ? <><b>All apps{scope?.kind === "all" ? ` · ${scope.apps}` : ""}</b><span className="inf-faint"> · and BOSS itself</span></>
            : <><b>This project · {scope?.kind === "project" ? scope.projectName : "…"}</b></>}
          {otherHref ? (
            <a className="inf-link" href={pxHref(otherHref)} data-testid="obs-alerts-other">
              {across ? (here ? `Only ${here} › Health` : "A project's Health") : "Every app in Observability"} →</a>
          ) : null}
        </span>
        <span className="ag-spacer" />
        {ownApps.length ? (
          <GatedButton permission="observability:manage" type="button" className="inf-link" data-testid="obs-alerts-new-rule"
                       title="Pick an app and a metric it reports; BOSS writes the PromQL" onClick={() => setBuilding(true)}>+ New alert rule</GatedButton>
        ) : null}
      </div>
      {incidents.length ? (
        <DrawerSection label="OPEN INCIDENTS" testId="obs-incidents">
          <ul className="obs-list">
            {incidents.map((i) => <IncidentRow key={`${i.project?.id ?? ""}${i.id}`} i={i} severities={severities} showProject={across}
                                               onOpen={openIncident(i, appOf(i.subjectId))} />)}
          </ul>
        </DrawerSection>
      ) : null}
      <div className="obs-controls" data-testid="obs-alerts-filters">
        <span className="obs-dropchip" data-on={f.aapp ? "1" : undefined}>
          <select aria-label="App" value={f.aapp ?? ""} data-testid="obs-alerts-app"
                  onChange={(e) => setF({ aapp: e.target.value || null })}>
            <option value="">{across ? "All apps" : "All its apps"}</option>
            {(d?.apps ?? []).map((a) => <option key={`${a.project?.id ?? ""}${a.targetId}`} value={a.targetId}>{a.app} · {a.env}{across && a.project ? ` · ${a.project.name}` : ""}</option>)}
          </select>
        </span>
        <span className="obs-dropchip" data-on={f.astate !== "firing" ? "1" : undefined}>
          <select aria-label="State" value={f.astate} data-testid="obs-alerts-state"
                  onChange={(e) => setF({ astate: e.target.value === "firing" ? null : e.target.value })}>
            {STATES.map((s) => <option key={s.key} value={s.key}>{s.label}</option>)}
          </select>
        </span>
        <span className="obs-dropchip" data-on={f.asev ? "1" : undefined}>
          <select aria-label="Severity" value={f.asev ?? ""} data-testid="obs-alerts-severity"
                  onChange={(e) => setF({ asev: e.target.value || null })}>
            <option value="">Any severity</option>
            {severities.map((s) => <option key={s.key} value={s.key} title={`${s.word}: ${s.meaning}`}>{s.label} · {s.word}</option>)}
          </select>
        </span>
        <span className="obs-dropchip" data-on={f.asrc ? "1" : undefined} title="Where the alert came from">
          <select aria-label="Source" value={f.asrc ?? ""} data-testid="obs-alerts-source"
                  onChange={(e) => setF({ asrc: e.target.value || null })}>
            <option value="">Any source</option>
            {ALERT_SOURCES.map((s) => <option key={s.key} value={s.key} title={s.tip}>{s.label}</option>)}
          </select>
        </span>
        <span className="obs-dropchip" data-on={f.aflap !== null ? "1" : undefined} title="Firing and clearing over and over">
          <select aria-label="Flapping" value={triKey(f.aflap)} data-testid="obs-alerts-flapping"
                  onChange={(e) => setF({ aflap: e.target.value || null })}>
            {FLAP.map((s) => <option key={s.key} value={s.key}>{s.label}</option>)}
          </select>
        </span>
        <span className="obs-dropchip" data-on={f.aack !== null ? "1" : undefined} title="Acked: repeats stopped, not silenced">
          <select aria-label="Acked" value={triKey(f.aack)} data-testid="obs-alerts-acked"
                  onChange={(e) => setF({ aack: e.target.value || null })}>
            {ACKED.map((s) => <option key={s.key} value={s.key}>{s.label}</option>)}
          </select>
        </span>
        <span className="ag-spacer" />
        {d ? <span className="inf-faint inf-num" data-testid="obs-alerts-count"
                   title={across ? "Firing now, across every app and BOSS itself" : "Firing now, across this project's apps"}>{d.counts.firing} firing</span> : null}
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
                {f.aatt ? "Nothing needs attention" : f.astate === "firing" ? "Nothing firing" : "No alerts in the last 7 days"}
              </span>
              {onConnect ? (
                <GatedButton permission="observability:manage" type="button" className="inf-link" data-testid="obs-alerts-connect"
                             title="Bring an app's metrics or alerts into BOSS" onClick={onConnect}>Connect an app</GatedButton>
              ) : null}
            </span>
          ) : (
            <ul className="obs-list obs-alerts">
              {rows.map((a) => (
                <AlertRow key={`${a.project?.id ?? ""}${a.id}`} a={a} targetId={a.targetId} projectId={a.project?.id ?? projectId} actor={actor} showApp severities={severities}
                          plainNames={!across}
                          project={across ? (a.platform ? { id: "", name: "BOSS", href: "" } : a.project) : null}
                          onDone={all.refresh}
                          selected={picked.has(a.id)} onSelect={ackable.has(a.id) ? (on) => toggle(a.id, on) : undefined}
                          onAck={() => void ack([a.id])}
                          onApp={() => openApp(a)}
                          onIncident={() => openAlertIncident(a)} />
              ))}
            </ul>
          )}
      </DrawerSection>
      {building && d ? (
        <RuleBuilder targetId={null} apps={ownApps} projectId={projectId} actor={actor} severities={severities}
                     onClose={() => setBuilding(false)}
                     onSaved={(app) => { setBuilding(false); openApp({ targetId: app, project: null, platform: false }); }} />
      ) : null}
    </div>
  );
}

/** Observability › Alerts: the panel over every app and BOSS itself; filters live in the URL. */
export function AlertsTab(props: ObsTabProps & { onConnect?: () => void }) {
  const { projectId, actor, url, go } = props;
  const elsewhere = (pid: string, patch: Record<string, string>) =>
    navigateTo(pxHref(`?${new URLSearchParams({ area: "observability", project_id: pid, ...patch }).toString()}`));
  return (
    <>
      <AlertsPanel where={{ kind: "all" }} projectId={projectId} actor={actor} f={url} setF={(patch) => go(patch, "replace")}
                   query={props.query} onConnect={props.onConnect}
                   otherHref={projectId ? `?area=project&project_id=${encodeURIComponent(projectId)}&tab=health` : null}
                   openApp={(a) => {
                     if (a.platform) go({ view: "system", app: null }, "push");
                     else if (a.project && a.project.id !== projectId) elsewhere(a.project.id, { app: a.targetId, atab: "alerts" });
                     else go({ app: a.targetId, atab: "alerts" }, "push");
                   }}
                   openIncident={(i, app) => {
                     if (!i.subjectId) return null;
                     if (i.project && i.project.id !== projectId) return () => elsewhere(i.project!.id, { app: i.subjectId!, inc: i.id });
                     return app ? () => go({ app, atab: null }, "push") : null;
                   }}
                   openAlertIncident={(a) => {
                     if (a.project && a.project.id !== projectId) elsewhere(a.project.id, { app: a.targetId, ...(a.incident ? { inc: a.incident.id } : {}) });
                     else go({ app: a.targetId, atab: null, inc: a.incident?.id ?? null }, "push");
                   }} />
      {url.app ? <AppDrawer {...props} targetId={url.app} row={props.pulse.data?.rows.find((r) => r.targetId === url.app) ?? null} /> : null}
    </>
  );
}
