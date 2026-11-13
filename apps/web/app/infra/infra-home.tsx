"use client";

/**
 * INFRA (infra-observability spec §4.1-§4.8): where your apps run and how they got there.
 *
 *   Infra   [● Fleet 3] [Deploys] [Previews 2] [Cloud]   2 ready, 5 apps, 1 needs you   [Find] [+ Add ▾] [Ask BOSS]
 *   NEEDS YOU   AttentionCards from infra_view.attention(), filtered to Infra (≤ 4, then +n more)
 *   <tab>       Fleet (racks), Deploys (rail), Previews, Cloud (the resource plane)
 *
 * The URL is the state (`view=`, drawers `host=` `target=` `deploy=` `preview=` `account=` …,
 * `q=`). Every tab polls its own ETag'd view through usePolled; nothing here refreshes the
 * console. The Fleet view is polled on every tab because it carries the header counts and the
 * attention list.
 */
import React, { useCallback, useEffect, useState } from "react";

import { useConsoleLive } from "../../lib/console-live";
import { readInfraUrl, INFRA_TABS, useFleet, type InfraTab, type InfraUrl } from "../../lib/infra-view";
import { AreaHead, FindBox, NeedsYou, PartialLine, useUrlState, writeUrl } from "./kit";
import { FleetTab, InfraAddMenu, InfraAskBoss } from "./fleet";
import { DeploysTab } from "./deploys";
import { PreviewsTab } from "./previews";
import { CloudTab } from "./cloud";
import { PluginsTab } from "./plugins";
import { ExtensionsTab } from "./extensions";
import "./infra.css";
import "../observability/obs.css";

export type InfraTabProps = {
  projectId: string;
  actor: string;
  orgId: string | null;
  url: InfraUrl;
  /** Patch the URL and re-read it. Drawers and tabs push; filters replace. */
  go: (patch: Record<string, string | null>, mode?: "push" | "replace") => void;
  query: string;
};

export function InfraHome({ projectId, actor, orgId }: { projectId: string; actor: string; orgId?: string | null }) {
  const live = useConsoleLive();
  const [url, reread] = useUrlState(readInfraUrl);
  const [query, setQuery] = useState(url.q);
  const fleet = useFleet(projectId, actor);
  const go = useCallback((patch: Record<string, string | null>, mode: "push" | "replace" = "push") => {
    writeUrl(patch, mode);
    reread();
  }, [reread]);

  // `?area=delivery#hardware-manager` (the old "Where apps install" anchor) opens This computer.
  useEffect(() => {
    if (typeof window !== "undefined" && window.location.hash === "#hardware-manager") {
      history.replaceState(null, "", window.location.pathname + window.location.search);
      go({ view: "fleet", host: "this" }, "replace");
    }
  }, [go]);

  const setTab = (key: string) => go({ view: key === "fleet" ? null : key, host: null, target: null, deploy: null,
                                       preview: null, account: null, resource: null, changeset: null, pool: null }, "push");
  const c = fleet.data?.counts;
  const needs = c?.needsYou ?? 0;
  const tabs = INFRA_TABS.map((t) => ({
    ...t,
    count: t.key === "fleet" ? (c ? c.ready + c.settingUp + c.unreachable : undefined)
      : t.key === "previews" ? c?.previewsLive : undefined,
    dot: t.key === "fleet" && c && c.unreachable > 0 ? { tone: "red" as const, tip: `${c.unreachable} unreachable` } : null,
  }));
  const props: InfraTabProps = { projectId, actor, orgId: orgId ?? null, url, go, query };
  const panel: Record<InfraTab, React.ReactNode> = {
    fleet: <FleetTab {...props} fleet={fleet} />,
    deploys: <DeploysTab {...props} />,
    previews: <PreviewsTab {...props} />,
    cloud: <CloudTab {...props} />,
    plugins: <PluginsTab {...props} />,
    extensions: <ExtensionsTab {...props} />,
  };

  return (
    <div className="inf" data-testid="infra" data-paused={live ? undefined : ""}>
      <AreaHead area="infra" title="Infra" tabs={tabs} active={url.view} onTab={setTab}
                counts={c ? (
                  <>
                    <span>{c.ready} ready</span><span aria-hidden>, </span>
                    <span>{c.apps} {c.apps === 1 ? "app" : "apps"}</span>
                    {needs ? <><span aria-hidden>, </span><span className="inf-needs-count">{needs === 1 ? "1 needs you" : `${needs} need you`}</span></> : null}
                  </>
                ) : null}>
        <FindBox value={query} onChange={(v) => { setQuery(v); writeUrl({ q: v || null }, "replace"); }} label="Find" testId="infra-find" />
        <InfraAddMenu {...props} fleet={fleet} />
        <InfraAskBoss {...props} fleet={fleet} />
      </AreaHead>
      <PartialLine partial={fleet.data?.partial ?? []} error={fleet.data ? null : fleet.error} testId="infra-partial" />
      <NeedsYou items={(fleet.data?.attention ?? []).filter((a) => !a.area || a.area === "infra")}
                projectId={projectId} actor={actor} orgId={orgId} onRefresh={fleet.refresh} testId="infra-needs" />
      <div role="tabpanel" aria-label={INFRA_TABS.find((t) => t.key === url.view)?.label} data-testid={`infra-panel-${url.view}`}>
        {panel[url.view]}
      </div>
    </div>
  );
}
