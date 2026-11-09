"use client";

/**
 * Factory › Apps (factory spec §4.6): an aligned table, one row per app, App, Crew, State ·
 * PR, Deploy, Spend, Builds, Updated. The chevron column is always there (disabled for one
 * build) so every name starts at the same x; names wrap, never cut mid-word. The whole row opens
 * the app's newest WORKING build; the chevron lists its builds, numbered by their place in the
 * app (Build 1 = the first), each with only the stations it really ran.
 *
 * The rows come from `GET /v1/factory/apps` (the server groups runs into apps). Until a server
 * answers that, they are grouped here from the build list with the same rule
 * (app-grouping.ts), and only what that list holds is shown: no faces, no deploys guessed.
 */
import { changeHref, isLocalChangeUrl, linkTarget } from "../../lib/repo-links";
import { CaretDown, CaretRight } from "@phosphor-icons/react";
import React, { useEffect, useMemo, useState } from "react";

import type { CodeTaskRun, PreviewRec } from "@boss/api-client";
import { arr, num, obj, opt, parseBuildRow, str, type BuildRow as BuildRowData } from "../../lib/factory-floor";
import { StationLine } from "./station-line";
import { useIdentityVersion } from "../../lib/use-agent-identities";
import { AgFace } from "../agents/agent-face";
import { nameOf } from "../agents/run-face";
import { ago, localTime, useNow } from "../agents/live-util";
import { groupRunsIntoApps } from "../app-grouping";
import { appIdentity } from "../app-identity";
import { previewWebHref } from "../../lib/browser-links";
import { areaHref, costWord } from "./cockpit-util";
import { formatMonthDay } from "../../lib/datetime";
import { GatedButton } from "../../components/gated";
import { NEW_BUILD_TIP } from "../../lib/compose-links";
import { LINKS_TIP, LinksSheet } from "./links-sheet";
import { SecuritySheet, readSecurityParam, writeSecurityParam } from "./security/security-sheet";
import { SIGN_IN_TIP } from "./sign-in-sheet";
import { AppSetupSheet, type SetupTab } from "../../components/v2/templates/setup-sheet";
import { parseSetupNeeds, setupChip, type SetupNeeds } from "../../lib/app-connectors";
import { LabLinks } from "./lab-links";
import { useOnScreen } from "../../lib/area-shown";

/** Where "＋ Add a feature" goes: New project, in add mode, with this app chosen. */
export function addFeatureHref(appKey: string, previewId?: string | null): string {
  return areaHref({ area: "delivery_workflows", compose: "add", app: appKey, preview: previewId ?? null });
}

/** One build of an app. `row` is the full Floor row when the server sends one (walk D21): then
 *  the attempt is drawn as a compact BuildRow with its stations, else as a line of words. */
export type PrState = "open" | "merged" | "closed";
export type AppBuild = {
  runId: string; attempt: number; word: string; at?: string; row?: BuildRowData;
  /** Its place among the app's builds, 1 = the first (the server's `order`). Separate builds of
   *  one app each have `attempt` 1 in their own lineage, so `attempt` is never the number shown. */
  order?: number;
  prNumber?: number; prUrl?: string; prState?: PrState; costUsd?: number;
};
export type AppRow = {
  key: string; app: string; builders: string[]; word: string;
  preview: { status: string; url?: string } | null;
  deploys: { env: string; ok: boolean; word?: string; targetId?: string }[];
  builds: AppBuild[];
  rank: number;
  /** The app's PR (its newest attempt that opened one) and that PR's state, its metered spend,
   *  whether it ran on a subscription only, its repo, its brief and when it last moved. */
  prNumber?: number; prUrl?: string; prState?: PrState; costUsd?: number; included?: boolean;
  repo?: string; brief?: string; at?: string;
  /** The app's id (finalize_ctx.app_id): what app-level surfaces, App › Security, address. */
  appId?: string;
  /** The app's key (its name's identity key): what templates, secrets, Setup and links address. */
  appKey?: string;
  /** What its connectors still need a person for (the server's `setup`): the row's chip. */
  setup?: SetupNeeds;
};

const BUILT = new Set(["pr_open", "merged", "deployed", "done_no_pr"]);

/** The build an app opens on: its newest that BUILT something (the old card's pickBuiltRun) , 
 *  not a failed follow-up that happens to be newer than the working app. */
export function builtBuild(builds: AppBuild[]): AppBuild | undefined {
  return builds.find((b) => (b.row ? BUILT.has(b.row.state) : /^(PR open|Merged|Deployed|Done)/.test(b.word))) ?? builds[0];
}

/** The fallback's words for a run's status: only what the run row says. */
function runWord(r: CodeTaskRun): string {
  const s = String(r.status ?? "");
  if (s === "running" || s === "queued" || s === "repairing") return "Building";
  if (s === "completed") return r.pull_request_url ? "PR open" : "Done, no PR";
  if (s === "failed") return "Failed";
  if (s === "cancelled") return "Stopped";
  if (s === "blocked") return "Blocked";
  return s ? s.charAt(0).toUpperCase() + s.slice(1) : "";
}

function rankOf(word: string): number {
  if (word === "Blocked" || word.startsWith("Needs")) return 0;
  if (word === "Building") return 1;
  return 2;
}

export function appsFromRuns(runs: CodeTaskRun[], previews: PreviewRec[]): AppRow[] {
  return groupRunsIntoApps(runs).map((members) => {
    const newest = members[0];
    const live = previews.find((p) => members.some((m) => m.id === p.run_id)
      && (p.status === "running" || p.status === "starting" || p.status === "verified"));
    const builds = members.map((m, i) => ({
      runId: m.id, attempt: Number(((m.finalize_ctx ?? {}) as Record<string, unknown>).attempt ?? members.length - i) || members.length - i,
      word: runWord(m), at: (m as unknown as { created_at?: string }).created_at,
    }));
    const word = runWord(newest);
    return {
      key: newest.id, app: appIdentity(newest).name, builders: [], word,
      preview: live ? { status: live.status === "starting" ? "starting" : "running", url: previewWebHref(live.id) } : null,
      deploys: [], builds, rank: rankOf(word),
    };
  }).sort((a, b) => a.rank - b.rank);
}

const PR_STATES = new Set(["open", "merged", "closed"]);
const prStateOf = (v: unknown): PrState | undefined => (PR_STATES.has(String(v)) ? String(v) as PrState : undefined);
const numOrU = (v: unknown): number | undefined => (typeof v === "number" && Number.isFinite(v) ? v : undefined);

export function parseApps(w: unknown): AppRow[] {
  return arr(obj(w).apps).map((x) => {
    const o = obj(x);
    const p = o.preview ? obj(o.preview) : null;
    const word = str(o.word);
    const builds: AppBuild[] = arr(o.attempts).map((b) => {
      const y = obj(b);
      const full = Array.isArray(y.stations) && y.stations.length > 0;
      return { runId: str(y.run_id), attempt: num(y.attempt, 1), word: str(y.word), at: opt(y.at),
               order: numOrU(y.order), prNumber: numOrU(y.pr_number), prUrl: opt(y.pr_url),
               prState: prStateOf(y.pr_state), costUsd: numOrU(y.cost_usd),
               ...(full ? { row: parseBuildRow(y) } : {}) };
    });
    // A server without `order` (older API): number the builds by their place, oldest = 1.
    builds.forEach((b, i) => { if (b.order === undefined) b.order = builds.length - i; });
    return {
      key: str(o.run_id ?? o.key ?? o.app), app: str(o.app), builders: arr(o.builders).map(String), word,
      preview: p && opt(p.status) ? { status: str(p.status), url: opt(p.url) } : null,
      deploys: arr(o.deploys).map((d) => {
        const y = obj(d);
        return { env: str(y.env), ok: y.ok === true, word: opt(y.word), targetId: opt(y.target_id) };
      }),
      builds,
      rank: rankOf(word),
      prNumber: numOrU(o.pr_number), prUrl: opt(o.pr_url), prState: prStateOf(o.pr_state),
      costUsd: num(o.cost_usd), included: o.included === true, repo: opt(o.repo), brief: opt(o.brief),
      at: opt(o.at), appId: opt(o.app_id), appKey: opt(o.app_key), setup: parseSetupNeeds(o.setup),
    };
  });
}

/** `/v1/factory/apps` on open and every 15s while the tab is visible (the old list refreshed
 *  itself while anything ran), or null when no server answers it (then the fallback). */
function useServerApps(projectId: string): AppRow[] | null {
  const [apps, setApps] = useState<AppRow[] | null>(null);
  const onScreen = useOnScreen();   // off screen, nothing is asked (lib/area-shown.ts)
  useEffect(() => {
    let stop = false;
    const load = async () => {
      if (typeof document !== "undefined" && document.visibilityState === "hidden") return;
      try {
        const r = await fetch(`/v1/factory/apps?project_id=${encodeURIComponent(projectId)}`, { cache: "no-store" });
        if (!r.ok || stop) return;
        setApps(parseApps(await r.json()));
      } catch { /* the fallback stands */ }
    };
    void load();
    const id = onScreen ? window.setInterval(() => void load(), 15_000) : undefined;
    return () => { stop = true; if (id) window.clearInterval(id); };
  }, [projectId, onScreen]);
  return apps;
}

// ── The table ───────────────────────────────────────────────────────────────────────────────

export type AppSort = "updated" | "spend" | "name";
const SORT_LABEL: Record<AppSort, string> = { updated: "Updated", spend: "Spend", name: "App" };

/** Updated (newest first, the default), Spend (most first), Name (A, Z). Stable inside. */
export function sortApps(apps: AppRow[], by: AppSort): AppRow[] {
  const t = (a: AppRow) => (a.at ? Date.parse(a.at) : 0) || 0;
  return apps.map((a, i) => ({ a, i })).sort((x, y) => {
    const d = by === "name" ? x.a.app.localeCompare(y.a.app)
      : by === "spend" ? (y.a.costUsd ?? 0) - (x.a.costUsd ?? 0)
      : t(y.a) - t(x.a);
    return d || x.i - y.i;
  }).map(({ a }) => a);
}

const CREW_MAX = 5;

/** The agents who built it, side by side, never overlapped into a smudge, each named. */
function Crew({ roleKeys }: { roleKeys: string[] }) {
  useIdentityVersion(); // names and faces re-resolve when the install's identities load
  const shown = roleKeys.slice(0, CREW_MAX);
  const rest = roleKeys.slice(CREW_MAX);
  if (!shown.length) return <span className="fx-muted" title="No agent has signed work on it yet">{"—"}</span>;
  return (
    <span className="fx-crew" role="list" aria-label={roleKeys.map((k) => nameOf(k).name).join(", ")}>
      {shown.map((k) => (
        <span key={k} className="fx-crew-face" role="listitem" title={nameOf(k).name}>
          <AgFace roleKey={k} size={20} decorative />
        </span>
      ))}
      {rest.length ? <span className="fx-crew-more" title={rest.map((k) => nameOf(k).name).join(", ")}>+{rest.length}</span> : null}
    </span>
  );
}

function PrCell({ number, url, state, repo, testId }: { number?: number; url?: string; state?: PrState; repo?: string; testId?: string }) {
  if (!number) return <span className="fx-muted">{"—"}</span>;
  const tip = `PR #${number} is ${state ?? "open"}${repo ? `, ${repo}` : ""}`;
  return (
    <span className="fx-pr" data-state={state ?? "open"}>
      <a className="ag-link" href={changeHref(url) ?? undefined} {...linkTarget(url)} title={isLocalChangeUrl(url) ? `Change #${number} in BOSS` : tip} data-testid={testId}
         onClick={(e) => e.stopPropagation()}>#{number}</a>
      <span className="fx-pr-state">{state ?? "open"}</span>
    </span>
  );
}

function spendOf(a: { costUsd?: number; included?: boolean }): { text: string; tip?: string } {
  const word = costWord(a.costUsd ?? 0);
  if (word) return { text: word, tip: "Metered model spend over every build" };
  if (a.included) return { text: "Included", tip: "Ran on a subscription. No metered spend." };
  return { text: "—" };
}

/** A build's date, pinned to one locale (the console renders on the server too). */
function dayOf(iso?: string): string {
  if (!iso) return "";
  return formatMonthDay(iso);
}

/** The stations a build actually reached: never a ring for a station it never ran. */
export function stationsThatRan(row: BuildRowData): BuildRowData["stations"] {
  return row.stations.filter((s) => s.status !== "pending" && s.status !== "skipped");
}

/** Builds still on the line say which station they are at; a shipped or stopped one does not. */
const ON_THE_LINE = new Set(["building", "queued", "quiet", "budget_wait", "github_wait", "blocked", "needs_plan", "needs_waiver", "needs_person", "budget_refused"]);

function stateCell(b: { word: string; row?: BuildRowData }): { word: string; where: string } {
  const st = b.row && ON_THE_LINE.has(b.row.state) ? b.row.stations.find((s) => s.key === b.row?.station) : undefined;
  return { word: b.word, where: st && !b.word.split(" · ").includes(st.label) ? st.label : "" };
}

/** The row's setup chip ("Set up sign-in, 2 providers"), or null when nothing needs you. */
const chipOf = (a: AppRow) => setupChip(a.setup);

export type AppsTabProps = {
  projectId: string;
  runs: CodeTaskRun[];
  previews: PreviewRec[];
  query?: string;
  onOpen: (runId: string, from: HTMLElement | null) => void;
  onNew: () => void;
  /** App › Components (multi-repo): its parts and the repositories they live in. */
  onComponents?: (appKey: string) => void;
};

export function AppsTab({ projectId, runs, previews, query = "", onOpen, onNew, onComponents }: AppsTabProps) {
  useIdentityVersion();
  const now = useNow(30_000);
  const served = useServerApps(projectId);
  const fallback = useMemo(() => appsFromRuns(runs, previews), [runs, previews]);
  const [sort, setSort] = useState<AppSort>("updated");
  const q = query.trim().toLowerCase();
  const apps = sortApps((served ?? fallback).filter((a) => !q || a.app.toLowerCase().includes(q)
    || (a.brief ?? "").toLowerCase().includes(q)), sort);
  const [open, setOpen] = useState<string | null>(null);
  /** The app whose row menu is open. */
  const [menu, setMenu] = useState<string | null>(null);
  /** App ▸ Links (F5): the app whose links sheet is open. */
  const [linksOf, setLinksOf] = useState<AppRow | null>(null);
  /** App ▸ Setup: the app whose Connectors and Settings are open, and on which tab. Its chip,
   *  ⋯ › Setup and ⋯ › Sign-in all open it: one surface for an app's setup. */
  const [setupOf, setSetupOf] = useState<{ app: AppRow; tab: SetupTab } | null>(null);
  /** App › Security: the app id it is open on (`?security=<app id>`, so it can be linked). */
  const [security, setSecurity] = useState<string | null>(() => readSecurityParam());
  const openSecurity = (appId: string | null) => { setSecurity(appId); writeSecurityParam(appId); };
  // A row menu closes on a press anywhere else, or Esc.
  useEffect(() => {
    if (!menu) return undefined;
    const away = (e: Event) => { if (!(e.target as HTMLElement | null)?.closest?.(".fx-app-acts")) setMenu(null); };
    const esc = (e: KeyboardEvent) => { if (e.key === "Escape") setMenu(null); };
    document.addEventListener("mousedown", away);
    document.addEventListener("keydown", esc);
    return () => { document.removeEventListener("mousedown", away); document.removeEventListener("keydown", esc); };
  }, [menu]);

  if (!apps.length) {
    return (
      <div className="fx-apps" data-testid="fx-apps">
        <LabLinks projectId={projectId} />
        <div className="ag-empty fx-empty" data-testid="fx-no-apps">
          <span>No apps yet</span>
          <GatedButton permission="workflow_run:start" type="button" className="ag-btn ag-btn-primary" onClick={onNew}
                       title={NEW_BUILD_TIP} data-testid="fx-apps-new">+ New build</GatedButton>
        </div>
      </div>
    );
  }
  const sortButton = (key: AppSort, cls: string) => (
    <span className={`fx-apps-th ${cls}`} role="columnheader" aria-sort={sort === key ? (key === "name" ? "ascending" : "descending") : "none"}>
      <button type="button" className="fx-apps-sort" data-active={sort === key || undefined} data-testid={`fx-apps-sort-${key}`}
              title={key === "updated" ? "Newest first" : key === "spend" ? "Most spent first" : "A to Z"}
              onClick={() => setSort(key)}>{SORT_LABEL[key]}</button>
    </span>
  );
  return (
    <>
    <LabLinks projectId={projectId} />
    <div className="fx-apps" data-testid="fx-apps" role="table" aria-label="Apps">
      <div className="fx-apps-head fx-app-grid" role="row">
        <span className="fx-apps-th fx-c-chev" role="columnheader" aria-label="Builds of the app" />
        {sortButton("name", "fx-c-app")}
        <span className="fx-apps-th fx-c-crew" role="columnheader">Crew</span>
        <span className="fx-apps-th fx-c-state" role="columnheader">State</span>
        <span className="fx-apps-th fx-c-pr" role="columnheader">PR</span>
        <span className="fx-apps-th fx-c-deploy" role="columnheader">Deploy</span>
        {sortButton("spend", "fx-c-spend")}
        <span className="fx-apps-th fx-c-builds" role="columnheader">Builds</span>
        {sortButton("updated", "fx-c-updated")}
      </div>
      <ul className="fx-apps-list" role="rowgroup">
        {apps.map((a) => {
          const expanded = open === a.key;
          const built = builtBuild(a.builds);
          const opens = built?.runId ?? a.key;
          const lastFailed = a.builds[0] && built && a.builds[0] !== built
            && (a.builds[0].row?.state === "failed" || a.builds[0].word.startsWith("Failed"));
          // Shipped AND building (the old card's "shipped, building"): a newer build is on the line.
          const alsoBuilding = a.builds[0] && built && a.builds[0] !== built
            && ["building", "queued", "quiet", "budget_wait"].includes(a.builds[0].row?.state ?? "");
          const shown = built ?? a.builds[0];
          const state = stateCell({ word: built && built !== a.builds[0] ? built.word : a.word, row: shown?.row });
          const spend = spendOf(a);
          const many = a.builds.length > 1;
          const deploy = a.deploys[0];
          const openRow = (e: React.MouseEvent<HTMLElement> | React.KeyboardEvent<HTMLElement>) => {
            const t = e.target as HTMLElement;
            if (t.closest("a, .fx-app-chev, .fx-app-acts")) return;
            onOpen(opens, (e.currentTarget as HTMLElement).closest("li"));
          };
          return (
            <li key={a.key} className="fx-app" data-testid={`fx-app-${a.key}`} role="presentation">
              <div className="fx-app-row fx-app-grid" role="row" tabIndex={0} data-testid={`fx-app-open-${a.key}`}
                   aria-label={`${a.app}: open its ${many ? "newest working " : ""}build`}
                   onClick={openRow} onKeyDown={(e) => { if (e.key === "Enter") { e.preventDefault(); openRow(e); } }}>
                <span className="fx-c-chev" role="cell">
                  <button type="button" className="fx-app-chev ag-icon-btn" aria-expanded={many ? expanded : undefined}
                          disabled={!many} aria-label={many ? `${expanded ? "Hide" : "Show"} the builds of ${a.app}` : "One build"}
                          title={many ? undefined : "One build"} data-testid={`fx-app-chev-${a.key}`}
                          onClick={(e) => { e.stopPropagation(); if (many) setOpen(expanded ? null : a.key); }}>
                    {expanded ? <CaretDown size={12} weight="bold" aria-hidden /> : <CaretRight size={12} weight="bold" aria-hidden />}
                  </button>
                </span>
                <span className="fx-c-app fx-app-cell" role="cell">
                  {/* THE NAME ALWAYS SHOWS: the chip sits under it, never in its place. */}
                  <span className="fx-app-title">
                    <span className="fx-app-name" title={a.brief || a.app} data-user-text>{a.app}</span>
                    {/* ONLY WHEN A CONNECTOR NEEDS THE OPERATOR: one press to the app's Connectors. */}
                    {chipOf(a) ? (
                      <button type="button" className="fx-setup-chip" data-testid={`fx-app-setup-chip-${a.key}`}
                              title={chipOf(a)!.tip} aria-label={`${chipOf(a)!.text} for ${a.app}`}
                              onClick={(e) => { e.stopPropagation(); setSetupOf({ app: a, tab: "connectors" }); }}>
                        {chipOf(a)!.text}
                      </button>
                    ) : null}
                  </span>
                  {/* ADD A FEATURE, ON THE ROW (the everyday job): on hover, and in the row's menu. */}
                  <span className="fx-app-acts">
                    <a className="fx-app-add" href={addFeatureHref(opens)} data-testid={`fx-app-add-${a.key}`}
                       title="Add a feature to this app: built on its branch, its tests kept"
                       onClick={(e) => e.stopPropagation()}>＋ Add a feature</a>
                    <span className="fx-pop-anchor">
                      <button type="button" className="fx-app-more ag-icon-btn" aria-label={`More for ${a.app}`}
                              aria-haspopup="menu" aria-expanded={menu === a.key} data-testid={`fx-app-more-${a.key}`}
                              onClick={(e) => { e.stopPropagation(); setMenu(menu === a.key ? null : a.key); }}>⋯</button>
                      {menu === a.key ? (
                        <span role="menu" className="fx-ck-menu fx-app-menu" data-testid={`fx-app-menu-${a.key}`}>
                          <a role="menuitem" href={addFeatureHref(opens)} data-testid={`fx-app-menu-add-${a.key}`}
                             onClick={(e) => e.stopPropagation()}>Add a feature</a>
                          <button type="button" role="menuitem" onClick={(e) => {
                            e.stopPropagation(); setMenu(null);
                            onOpen(opens, (e.currentTarget as HTMLElement).closest("li"));
                          }}>Open its build</button>
                          {onComponents ? (
                            <button type="button" role="menuitem" data-testid={`fx-app-menu-components-${a.key}`}
                                    title="What it is built from: its components and their repositories"
                                    onClick={(e) => { e.stopPropagation(); setMenu(null); onComponents(a.appKey ?? opens); }}>Components</button>
                          ) : null}
                          <button type="button" role="menuitem" title="Its connectors, secrets and setup guide"
                                  data-testid={`fx-app-menu-setup-${a.key}`}
                                  onClick={(e) => { e.stopPropagation(); setMenu(null); setSetupOf({ app: a, tab: "connectors" }); }}>Setup</button>
                          <button type="button" role="menuitem" title={LINKS_TIP} data-testid={`fx-app-menu-links-${a.key}`}
                                  onClick={(e) => { e.stopPropagation(); setMenu(null); setLinksOf(a); }}>Links</button>
                          <button type="button" role="menuitem" title={SIGN_IN_TIP} data-testid={`fx-app-menu-signin-${a.key}`}
                                  onClick={(e) => { e.stopPropagation(); setMenu(null); setSetupOf({ app: a, tab: "connectors" }); }}>Sign-in</button>
                          {/* ALERTS LIVE IN OBSERVABILITY: this links to the app's Alerts tab there. */}
                          {a.deploys.find((d) => d.targetId) ? (
                            <a role="menuitem" data-testid={`fx-app-menu-alerts-${a.key}`} title="Its alert rules and every alert that fired"
                               href={areaHref({ area: "observability", app: a.deploys.find((d) => d.targetId)!.targetId!, atab: "alerts" })}
                               onClick={(e) => e.stopPropagation()}>Alerts</a>
                          ) : null}
                          {a.appId ? (
                            <button type="button" role="menuitem" data-testid={`fx-app-menu-security-${a.key}`}
                                    title="Scans, findings and security reviews of this app" onClick={(e) => {
                              e.stopPropagation(); setMenu(null); openSecurity(a.appId ?? null);
                            }}>Security</button>
                          ) : null}
                        </span>
                      ) : null}
                    </span>
                  </span>
                </span>
                <span className="fx-c-crew" role="cell"><Crew roleKeys={a.builders} /></span>
                <span className="fx-c-state fx-row-now" role="cell">
                  <span className="fx-word">{state.word}</span>
                  {state.where ? <span className="fx-state-where" title="The station it is at">{`, ${state.where}`}</span> : null}
                  {lastFailed ? <span className="fx-muted" title="The newest build of this app failed; the one that works opens">{", last failed"}</span> : null}
                  {alsoBuilding ? <span className="fx-muted" title="A newer build of this app is on the line">{", building"}</span> : null}
                </span>
                <span className="fx-c-pr fx-app-pr" role="cell">
                  <PrCell number={a.prNumber} url={a.prUrl} state={a.prState} repo={a.repo} testId={`fx-app-pr-${a.key}`} />
                </span>
                <span className="fx-c-deploy fx-app-deploys" role="cell"
                      title={a.deploys.map((d) => d.word ?? d.env).join(", ") || "Not deployed"}>
                  {deploy ? <span data-ok={deploy.ok || undefined}>{deploy.word ?? `${deploy.env} ${deploy.ok ? "✓" : "—"}`}</span> : <span className="fx-muted">{"—"}</span>}
                  {a.deploys.length > 1 ? <span className="fx-muted"> +{a.deploys.length - 1}</span> : null}
                </span>
                <span className="fx-c-spend fx-app-spend" role="cell" title={spend.tip}>{spend.text}</span>
                <span className="fx-c-builds fx-app-count" role="cell" title="Builds of this app">{a.builds.length}</span>
                <span className="fx-c-updated" role="cell" title={a.at ? localTime(a.at) : undefined}>{a.at ? ago(a.at, now) : "—"}</span>
              </div>
              {expanded ? (
                <ul className="fx-app-builds" aria-label={`Builds of ${a.app}`} role="rowgroup">
                  {a.builds.map((b, i) => {
                    const st = stateCell(b);
                    const cost = costWord(b.costUsd ?? b.row?.costUsd ?? 0);
                    const ran = b.row ? stationsThatRan(b.row) : [];
                    return (
                      <li key={b.runId} className="fx-app-build fx-app-grid" role="row" tabIndex={0}
                          data-testid={`fx-attempt-row-${b.runId}`} data-latest={i === 0 || undefined}
                          onClick={(e) => { if (!(e.target as HTMLElement).closest("a")) onOpen(b.runId, e.currentTarget); }}
                          onKeyDown={(e) => { if (e.key === "Enter") { e.preventDefault(); onOpen(b.runId, e.currentTarget); } }}>
                        <span className="fx-c-chev" role="cell" />
                        <span className="fx-c-app fx-build-n" role="cell">
                          <span data-testid={`fx-build-n-${b.runId}`}>Build {b.order ?? a.builds.length - i}</span>
                          {b.at ? <span className="fx-muted" title={localTime(b.at)}>, {dayOf(b.at)}</span> : null}
                          {i === 0 ? <span className="fx-latest" title="The newest build of this app">latest</span> : null}
                        </span>
                        <span className="fx-c-crew" role="cell">
                          {ran.length ? <StationLine stations={ran} density="row" still label="Stations it ran" /> : null}
                        </span>
                        <span className="fx-c-state" role="cell"><span className="fx-word">{st.word}</span></span>
                        <span className="fx-c-pr" role="cell">
                          <PrCell number={b.prNumber} url={b.prUrl} state={b.prState} repo={a.repo} />
                        </span>
                        <span className="fx-c-deploy" role="cell" />
                        <span className="fx-c-spend" role="cell" title={cost ? "Metered model spend" : undefined}>{cost || "—"}</span>
                        <span className="fx-c-builds" role="cell" />
                        <span className="fx-c-updated" role="cell" title={b.at ? localTime(b.at) : undefined}>{b.at ? ago(b.at, now) : ""}</span>
                      </li>
                    );
                  })}
                </ul>
              ) : null}
            </li>
          );
        })}
      </ul>
      {linksOf ? (
        <LinksSheet projectId={projectId} appKey={linksOf.key} app={linksOf.app}
                    others={apps.filter((o) => o.key !== linksOf.key).map((o) => ({ key: o.key, app: o.app }))}
                    onClose={() => setLinksOf(null)} />
      ) : null}
      {setupOf ? (
        <AppSetupSheet projectId={projectId} appKey={setupOf.app.appKey ?? null}
                       runId={builtBuild(setupOf.app.builds)?.runId ?? setupOf.app.key}
                       appName={setupOf.app.app} tab={setupOf.tab} deployLabel={setupOf.app.deploys[0]?.env}
                       onClose={() => setSetupOf(null)} />
      ) : null}
      {security ? (
        <SecuritySheet projectId={projectId} appKey={security}
                       appName={apps.find((a) => a.appId === security)?.app}
                       onClose={() => openSecurity(null)} />
      ) : null}
    </div>
    </>
  );
}
