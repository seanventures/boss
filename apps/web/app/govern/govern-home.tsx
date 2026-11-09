"use client";

/**
 * Govern: how hard and how much BOSS may spend, how close it is, and what the limits stopped
 * (activity-govern spec §4.4; the Spend-first flow of BOSSAPP-807). One read opens it (`GET /v1/govern`, ETag-polled every 15s while
 * visible and Live); Spend and Envelopes each read only when opened. Nothing here is fetched by
 * the page render, so arriving by the rail never shows "not loaded", the data is the screen's.
 */
import React, { useEffect, useRef, useState } from "react";

import { readGovern, type GovernView } from "../../lib/govern";
import { useConsoleLive } from "../../lib/console-live";
import { usePolledRead } from "../../lib/log-stream";
import { logHref } from "../../lib/log-url";
import { ControlBoard } from "./control-board";
import { EnvelopesTab } from "./envelopes-tab";
import { LimitsScreen } from "./limits-screen";
import { GovernMeter } from "./meter";
import { SelfFixTab } from "./self-fix-tab";
import { AutonomyPanel } from "../agent-ops/autonomy-panel";
import { WherePeopleDecide } from "./decide-tab";
import { SharingTab } from "./sharing-tab";
import { SPEND_PERIODS, SpendHome, type SpendPeriod } from "./spend-home";
import { SpendTab } from "./spend-tab";

export const GOVERN_POLL_MS = 15_000;
/**
 * BOSSAPP-807: Spend is the landing (no `tab`), Limits the second screen. Everything that was
 * a tab stays, one click away under More: "Where the money went" (the old Spend tab, `spend`),
 * the control board ("Advanced", `board`, and wherever a link names a `focus` row), Envelopes,
 * Sharing and Self-fix. Old links keep working: ?tab=spend|envelopes|sharing|selffix open what
 * they opened, ?focus=… still lands on that row of the board.
 */
type Tab = "overview" | "limits" | "autonomy" | "areas" | "board" | "spend" | "envelopes" | "sharing" | "selffix";
const MAIN: { key: Tab; label: string; tip: string }[] = [
  { key: "overview", label: "Spend", tip: "How much, on which model and project" },
  { key: "limits", label: "Limits", tip: "Caps, what happens at them, per project" },
  // WHERE PEOPLE DECIDE (agentic-factory wireframe 1): the compliance list, the only decisions
  // that wait for a person, and the stop switch. Keeps the `autonomy` key so old links land here.
  { key: "autonomy", label: "Where people decide", tip: "The 7 decisions that wait for a person; agents do the rest" },
];
const MORE: { key: Tab; label: string; tip: string }[] = [
  // The expert A0–A3 grid per task class, always-on agents and today's agent budget (was the
  // Autonomy tab; unchanged).
  { key: "areas", label: "Per-area autonomy", tip: "The A0–A3 grid per task class, always-on agents and today's agent budget" },
  { key: "spend", label: "Where the money went", tip: "By agent, by step, savings and top drivers" },
  { key: "board", label: "Advanced limits", tip: "Pace, thresholds, token guides, review rounds" },
  { key: "envelopes", label: "Envelopes", tip: "Allocations, initiatives and commitments" },
  { key: "sharing", label: "Sharing", tip: "What this install tells the BOSS project, and the switch" },
  { key: "selffix", label: "Self-fix", tip: "BOSS fixing its own bugs: the switch and the caps" },
];
const TABS = new Set<Tab>([...MAIN, ...MORE].map((t) => t.key));

function readTab(): { tab: Tab; focus: string | null } {
  if (typeof window === "undefined") return { tab: "overview", focus: null };
  const q = new URLSearchParams(window.location.search);
  const tab = q.get("tab") as Tab | null;
  const focus = q.get("focus");
  if (tab && TABS.has(tab)) return { tab, focus };
  // A link to one limit (Setup's Budget row, the Log) lands on that row of the board.
  return { tab: focus ? "board" : "overview", focus };
}

export function GovernHome({ projectId, actor }: { projectId: string; actor: string }) {
  const live = useConsoleLive();
  // Read after mount: the server renders the default tab, and the first paint must match it.
  const [{ tab, focus }, setPlace] = useState<{ tab: Tab; focus: string | null }>({ tab: "overview", focus: null });
  const [period, setPeriod] = useState<SpendPeriod>("month");
  // Spend reads its own window only once the URL has been read: a link to another tab must not
  // first fetch the landing's spend (one read opens Govern).
  const [ready, setReady] = useState(false);
  useEffect(() => {
    setPlace(readTab());
    setReady(true);
    const on = () => setPlace(readTab());
    window.addEventListener("popstate", on);
    return () => window.removeEventListener("popstate", on);
  }, []);
  const moreRef = useRef<HTMLDetailsElement | null>(null);
  const go = (next: Tab, nextFocus: string | null = null) => {
    setPlace({ tab: next, focus: nextFocus });
    if (moreRef.current) moreRef.current.open = false;
    try {
      const q = new URLSearchParams(window.location.search);
      q.set("area", "budget");
      if (next === "overview") q.delete("tab"); else q.set("tab", next);
      if (nextFocus) q.set("focus", nextFocus); else q.delete("focus");
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
  const inMore = MORE.some((t) => t.key === tab);

  return (
    <div className="gv" data-testid="gv">
      <header className="gv-head">
        <h2 className="gv-title">Govern</h2>
        <span className="gv-tabs" role="tablist" aria-label="Govern">
          {MAIN.map((t) => (
            <button key={t.key} type="button" role="tab" aria-selected={tab === t.key} className="gv-tab"
                    data-testid={`gv-tab-${t.key}`} title={t.tip} onClick={() => go(t.key)}>{t.label}</button>
          ))}
        </span>
        <span className="gv-spacer" />
        {tab === "overview" ? (
          <span className="ag-seg" role="group" aria-label="Period" data-testid="gv-period">
            {SPEND_PERIODS.map((p) => (
              <button key={p.key} type="button" aria-pressed={period === p.key} data-testid={`gv-period-${p.key}`}
                      onClick={() => setPeriod(p.key)}>{p.label}</button>
            ))}
          </span>
        ) : null}
        <details className="gv-moremenu" ref={moreRef} data-testid="gv-more">
          <summary className="ag-btn" data-current={inMore || undefined}
                   title="Per-area autonomy, where the money went, advanced limits, envelopes, sharing, self-fix">More</summary>
          <span className="gv-menu" role="tablist" aria-label="More">
            {MORE.map((t) => (
              <button key={t.key} type="button" role="tab" aria-selected={tab === t.key}
                      data-testid={`gv-tab-${t.key}`} title={t.tip} onClick={() => go(t.key)}>{t.label}</button>
            ))}
            <a className="ag-link" href={links.changes || undefined} data-testid="gv-changes"
               title="Every limit someone changed, in the Log">Changes</a>
            <a className="ag-link" href={links.stopped || undefined} data-testid="gv-stopped"
               title="Work a limit stopped, in the Log">Stopped</a>
          </span>
        </details>
      </header>
      {tab !== "overview" && tab !== "limits" && tab !== "autonomy" && tab !== "areas" ? (
        <GovernMeter meter={data?.meter ?? null} error={view.error} onRetry={view.reload} />
      ) : null}
      {frozen ? (
        <div className="gv-frozen" role="status" data-testid="gv-frozen">
          <span>New work is waiting for budget</span>
          <button type="button" className="ag-btn" onClick={() => go("limits", "monthly_cap")}>Raise monthly cap</button>
          <a className="ag-link" href={links.stopped || undefined}>Stopped</a>
        </div>
      ) : null}
      {tab === "overview" && !ready ? (
        <div className="gv-skel" aria-busy="true" />
      ) : tab === "overview" ? (
        <SpendHome view={data} viewError={view.error} onRetry={view.reload} projectId={projectId} paused={!live}
                   period={period} onDetail={() => go("spend")} />
      ) : tab === "limits" ? (
        <LimitsScreen view={data} projectId={projectId} actor={actor} unreachable={!data && !!view.error}
                      focus={focus} onSaved={view.reload} onAdvanced={() => go("board")} onBack={() => go("overview")} />
      ) : tab === "autonomy" ? (
        <WherePeopleDecide projectId={projectId} />
      ) : tab === "areas" ? (
        <AutonomyPanel projectId={projectId} />
      ) : tab === "board" ? (
        <ControlBoard view={data} projectId={projectId} actor={actor} unreachable={!data && !!view.error}
                      focus={focus} onSaved={view.reload} />
      ) : tab === "spend" ? (
        <SpendTab projectId={projectId} paused={!live} />
      ) : tab === "sharing" ? (
        <SharingTab projectId={projectId} actor={actor} paused={!live} />
      ) : tab === "selffix" ? (
        <SelfFixTab projectId={projectId} actor={actor} paused={!live} />
      ) : (
        <EnvelopesTab projectId={projectId} actor={actor} paused={!live} />
      )}
    </div>
  );
}
