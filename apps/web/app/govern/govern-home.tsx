"use client";

/**
 * Govern: how hard and how much BOSS may spend, how close it is, and what the limits stopped
 * (activity-govern spec §4.4). One read opens it (`GET /v1/govern`, ETag-polled every 15s while
 * visible and Live); Spend and Envelopes each read only when opened. Nothing here is fetched by
 * the page render, so arriving by the rail never shows "not loaded" — the data is the screen's.
 */
import React, { useEffect, useState } from "react";

import { readGovern, type GovernView } from "../../lib/govern";
import { useConsoleLive } from "../../lib/console-live";
import { usePolledRead } from "../../lib/log-stream";
import { logHref } from "../../lib/log-url";
import { ControlBoard } from "./control-board";
import { EnvelopesTab } from "./envelopes-tab";
import { GovernMeter } from "./meter";
import { SpendTab } from "./spend-tab";

export const GOVERN_POLL_MS = 15_000;
type Tab = "limits" | "spend" | "envelopes";
const TABS: { key: Tab; label: string; tip: string }[] = [
  { key: "limits", label: "Limits", tip: "Every limit, and what enforces it" },
  { key: "spend", label: "Spend", tip: "Where the money went" },
  { key: "envelopes", label: "Envelopes", tip: "Allocations, initiatives and commitments" },
];

function readTab(): { tab: Tab; focus: string | null } {
  if (typeof window === "undefined") return { tab: "limits", focus: null };
  const q = new URLSearchParams(window.location.search);
  const tab = q.get("tab");
  return { tab: tab === "spend" || tab === "envelopes" ? tab : "limits", focus: q.get("focus") };
}

export function GovernHome({ projectId, actor }: { projectId: string; actor: string }) {
  const live = useConsoleLive();
  // Read after mount: the server renders the default tab, and the first paint must match it.
  const [{ tab, focus }, setPlace] = useState<{ tab: Tab; focus: string | null }>({ tab: "limits", focus: null });
  useEffect(() => {
    setPlace(readTab());
    const on = () => setPlace(readTab());
    window.addEventListener("popstate", on);
    return () => window.removeEventListener("popstate", on);
  }, []);
  const go = (next: Tab) => {
    setPlace({ tab: next, focus: null });
    try {
      const q = new URLSearchParams(window.location.search);
      q.set("area", "budget");
      if (next === "limits") q.delete("tab"); else q.set("tab", next);
      q.delete("focus");
      window.history.replaceState(null, "", `${window.location.pathname}?${q.toString()}`);
    } catch { /* in-memory state still moves */ }
  };

  const view = usePolledRead<GovernView>({
    key: projectId, pollMs: GOVERN_POLL_MS, paused: !live,
    read: async (etag, signal) => {
      const got = await readGovern<GovernView>(projectId, "limits", {}, { etag, signal });
      return got.ok ? (got.notModified ? { ok: true as const, notModified: true, etag: got.etag }
        : { ok: true as const, data: got.data, etag: got.etag }) : { ok: false as const, error: got.error };
    },
  });
  const data = view.data;
  // Links that keep who you are (org=) are built from the URL, so only after mount: a server
  // render has no URL, and an href that differs between the two is a hydration mismatch.
  const [links, setLinks] = useState<{ changes: string; stopped: string }>({ changes: "", stopped: "" });
  useEffect(() => {
    setLinks({ changes: logHref({ view: "govern-changes", range: "30d" }), stopped: logHref({ view: "refusals", range: "7d" }) });
  }, []);
  const frozen = data?.meter.state === "red";

  return (
    <div className="gv" data-testid="gv">
      <header className="gv-head">
        <h2 className="gv-title">Govern</h2>
        <span className="gv-tabs" role="tablist" aria-label="Govern">
          {TABS.map((t) => (
            <button key={t.key} type="button" role="tab" aria-selected={tab === t.key} className="gv-tab"
                    data-testid={`gv-tab-${t.key}`} title={t.tip} onClick={() => go(t.key)}>{t.label}</button>
          ))}
        </span>
        <span className="gv-spacer" />
        <a className="ag-link" href={links.changes || undefined} data-testid="gv-changes"
           title="Every limit someone changed, in the Log">Changes ↗</a>
        <a className="ag-link" href={links.stopped || undefined} data-testid="gv-stopped"
           title="Work a limit stopped, in the Log">Stopped ↗</a>
      </header>
      <GovernMeter meter={data?.meter ?? null} error={view.error} onRetry={view.reload} />
      {frozen ? (
        <div className="gv-frozen" role="status" data-testid="gv-frozen">
          <span>New work is waiting for budget</span>
          <button type="button" className="ag-btn" onClick={() => setPlace({ tab: "limits", focus: "monthly_cap" })}>Raise monthly cap</button>
          <a className="ag-link" href={links.stopped || undefined}>Stopped ↗</a>
        </div>
      ) : null}
      {tab === "limits" ? (
        <ControlBoard view={data} projectId={projectId} actor={actor} unreachable={!data && !!view.error}
                      focus={focus} onSaved={view.reload} />
      ) : tab === "spend" ? (
        <SpendTab projectId={projectId} paused={!live} />
      ) : (
        <EnvelopesTab projectId={projectId} actor={actor} paused={!live} />
      )}
    </div>
  );
}
