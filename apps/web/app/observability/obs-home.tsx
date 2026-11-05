"use client";

/**
 * OBSERVABILITY (infra-observability spec §4.9-§4.13): is it alive, how fast, and what is BOSS's
 * thinking costing.
 *
 *   Observability [● Apps 5] [Models] [System ●] [Sources]   4 up · 1 slow · $3.12 · 24h   [24h|7d|30d] [Find] [Ask BOSS]
 *   NEEDS YOU   AttentionCards tagged Observability (app down, the system doctor) — not on Alerts,
 *               which shows only its alerts and incidents
 *   <tab>       Apps (the Pulse) · Models (the llm_calls ledger) · System (BOSS's machine) · Sources
 *
 * One window control drives every number and chart on a tab, and it lives in the URL (`win=`).
 * `view=` names the tab; `tab=` is accepted too (Govern links `?area=observability&tab=models`).
 * The Pulse and System views are polled on every tab: they carry the header counts, the System
 * tab's dot and the attention list. Nothing here refreshes the console.
 */
import React, { useCallback, useRef, useState } from "react";

import { useConsoleLive } from "../../lib/console-live";
import { useBand } from "../agents/live-util";
import { usePolled } from "../../lib/factory-floor";
import {
  OBS_TABS, WINDOWS, parseSystem, readObsUrl, systemUrl, usePulse, usd,
  type ObsTab, type ObsUrl, type Pulse, type System, type Win,
} from "../../lib/obs-view";
import type { Polled } from "../../lib/factory-floor";
import { AreaHead, FindBox, NeedsYou, PartialLine, useUrlState, writeUrl } from "../infra/kit";
import { AppsTab, ObsAskBoss } from "./pulse";
import { AlertsTab } from "./alerts";
import { GrafanaHeaderLink } from "./grafana-link";
import { ModelsTab } from "./models";
import { SystemTab } from "./system";
import { SourcesTab } from "./sources";
import { ScorecardsTab } from "./scorecards";
import { InstallsTab } from "./installs";
import { NotifyTab } from "./notify";
import { ConnectSheet } from "./connect-sheet";
import { GatedButton } from "../../components/gated";
import { polledImprove, type SharingView } from "../../lib/improve";
import { usePolledRead } from "../../lib/log-stream";
import "../infra/infra.css";
import "./obs.css";

export type ObsTabProps = {
  projectId: string;
  actor: string;
  orgId: string | null;
  url: ObsUrl;
  go: (patch: Record<string, string | null>, mode?: "push" | "replace") => void;
  query: string;
  pulse: Polled<Pulse>;
  system: Polled<System>;
};

const SYSTEM_FOREGROUND_MS = 10_000;
const SYSTEM_BACKGROUND_MS = 30_000;

export function ObsHome({ projectId, actor, orgId }: { projectId: string; actor: string; orgId?: string | null }) {
  const live = useConsoleLive();
  const [url, reread] = useUrlState(readObsUrl);
  const [query, setQuery] = useState(url.q);
  const [connecting, setConnecting] = useState(false);
  const go = useCallback((patch: Record<string, string | null>, mode: "push" | "replace" = "push") => {
    // `tab=` is read once and rewritten as `view=`. Opening an app without naming an incident
    // drops the previous one, so a later app open (including the Pulse, which only passes `app`)
    // cannot keep another alert's incident.
    const next: Record<string, string | null> = { tab: null, ...patch };
    if ("app" in patch && !("inc" in patch)) next.inc = null;
    writeUrl(next, mode);
    reread();
  }, [reread]);
  // Narrow layouts ask the server for fewer, wider cells (§5.3); the web never re-buckets.
  const band = useBand();
  const cells = band === "phone" ? 24 : band === "narrow" ? 48 : undefined;
  const pulse = usePulse(projectId, url.win, cells);
  const onSystem = useRef(url.view === "system");
  onSystem.current = url.view === "system";
  const system = usePolled(projectId ? systemUrl(projectId) : null, parseSystem,
                           () => (onSystem.current ? SYSTEM_FOREGROUND_MS : SYSTEM_BACKGROUND_MS));

  const setTab = (key: string) => go({ view: key === "apps" ? null : key, app: null, model: null, atab: null }, "push");
  const setWin = (w: Win) => go({ win: w === "24h" ? null : w }, "replace");
  // Installs is the collector's dashboard: its tab exists only in Collector mode (read once).
  const sharing = usePolledRead<SharingView>({
    key: projectId, pollMs: null, paused: true, enabled: Boolean(projectId),
    read: polledImprove<SharingView>("sharing", { project_id: projectId }),
  });
  const collector = Boolean(sharing.data?.switches?.collector?.on) || url.view === "installs";
  const c = pulse.data?.counts;
  const sys = system.data;
  const tabs = OBS_TABS.filter((t) => t.key !== "installs" || collector).map((t) => ({
    ...t,
    count: t.key === "apps" && pulse.data ? pulse.data.rows.length || undefined
      : t.key === "alerts" && pulse.data ? pulse.data.alertsFiring || undefined : undefined,
    dot: t.key === "system" && sys && sys.state !== "ok"
      ? { tone: (sys.state === "critical" ? "red" : "amber") as "red" | "amber", tip: `BOSS's machine: ${sys.reason ?? sys.state}` }
      : t.key === "apps" && c && c.down > 0 ? { tone: "red" as const, tip: `${c.down} down` }
      : t.key === "alerts" && pulse.data?.alertsFiring ? { tone: "red" as const, tip: `${pulse.data.alertsFiring} firing` } : null,
  }));
  const props: ObsTabProps = { projectId, actor, orgId: orgId ?? null, url, go, query, pulse, system };
  const panel: Record<ObsTab, React.ReactNode> = {
    apps: <AppsTab {...props} />,
    alerts: <AlertsTab {...props} onConnect={() => setConnecting(true)} />,
    notify: <NotifyTab {...props} />,
    models: <ModelsTab {...props} />,
    system: <SystemTab {...props} />,
    sources: <SourcesTab {...props} />,
    scorecards: <ScorecardsTab {...props} />,
    installs: <InstallsTab {...props} />,
  };
  const spend = pulse.data?.spend24hMicros ?? 0;
  const attention = [
    // Only cards the server tagged Observability: an untagged build or workflow card is not ours.
    ...(pulse.data?.attention ?? []).filter((a) => a.area === "observability"),
  // On System, the doctor's findings are its FINDINGS section: one card, not two on one screen.
  ].filter((a) => !(url.view === "system" && a.kind === "system_health"));

  return (
    <div className="inf obs" data-testid="obs" data-paused={live ? undefined : ""}>
      <AreaHead area="obs" title="Observability" tabs={tabs} active={url.view} onTab={setTab}
                counts={c ? (
                  <>
                    <span>{c.up} up</span>
                    {c.slow ? <><span aria-hidden> · </span><span>{c.slow} slow</span></> : null}
                    {c.down ? <><span aria-hidden> · </span><span className="inf-needs-count">{c.down} down</span></> : null}
                    {spend > 0 ? (
                      <><span aria-hidden> · </span>
                        <span title={pulse.data?.spend24hEstimated ? "Some prices were estimated, not billed" : "Metered model spend, last 24 hours"}>
                          {usd(spend)}{pulse.data?.spend24hEstimated ? " est." : ""} · 24h
                        </span></>
                    ) : null}
                  </>
                ) : null}>
        {url.view !== "sources" && url.view !== "scorecards" && url.view !== "installs" && url.view !== "alerts" && url.view !== "notify" ? (
          <span className="inf-seg" role="radiogroup" aria-label="Window" data-testid="obs-window">
            {WINDOWS.map((w) => (
              <button key={w} type="button" role="radio" aria-checked={url.win === w} className="inf-seg-btn"
                      data-testid={`obs-win-${w}`} onClick={() => setWin(w)}>{w}</button>
            ))}
          </span>
        ) : null}
        {pulse.data ? <GrafanaHeaderLink g={pulse.data.grafana} projectId={projectId} actor={actor} onChecked={pulse.refresh} /> : null}
        <GatedButton permission="observability:manage" type="button" className="ag-btn ag-btn-sm" data-testid="obs-connect"
                     title="Bring an app's metrics or alerts into BOSS" onClick={() => setConnecting(true)}>Connect an app</GatedButton>
        <FindBox value={query} onChange={(v) => { setQuery(v); writeUrl({ q: v || null }, "replace"); }} label="Find" testId="obs-find" />
        <ObsAskBoss {...props} />
      </AreaHead>
      <PartialLine partial={pulse.data?.partial ?? []} error={pulse.data ? null : pulse.error} testId="obs-partial" />
      {url.view !== "alerts" ? (
        <NeedsYou items={attention} projectId={projectId} actor={actor} orgId={orgId}
                  onRefresh={() => { pulse.refresh(); system.refresh(); }} testId="obs-needs" />
      ) : null}
      <div role="tabpanel" aria-label={OBS_TABS.find((t) => t.key === url.view)?.label} data-testid={`obs-panel-${url.view}`}>
        {panel[url.view]}
      </div>
      {connecting ? <ConnectSheet projectId={projectId} actor={actor} onClose={() => { setConnecting(false); pulse.refresh(); }} /> : null}
    </div>
  );
}
