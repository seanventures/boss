"use client";

/**
 * Agents › Organization: `Teams · Flow · Lines` (org-view spec §4.1), read from ONE endpoint.
 *
 *   Teams — a scorecard per team: who is busy, what finished or failed, the oldest wait, busy
 *           time, who it hands work to, metered spend and models, and what it is missing.
 *   Flow  — where work goes between teams: fixed columns, ≤12 edges, isolate on press, a run's
 *           path, and the from×to Table (the accessible Flow).
 *   Lines — today's OrgChartArea (the only editor of reporting lines), plus `Plan vs work`.
 *
 * THE URL IS THE STATE: `&layout=teams|flow|lines`, `&days=1|7|30`, `&isolate=<team>`, written
 * with replaceState. A stored or linked `layout=zones|swarm` lands on Teams / Flow. `?run=` lands
 * on Flow; `?focus=<team>` on Teams with that card shown.
 *
 * Esc closes, in order: a menu (its own), a drawer, the isolate, the selection, the run focus.
 */
import React, { useCallback, useEffect, useMemo, useState } from "react";
import { ArrowClockwise } from "@phosphor-icons/react";

import type { LiveMap } from "../../lib/agents-live";
import { TEAM_ORDER } from "../../lib/agents-live-board";
import {
  ORG_WINDOWS, ORG_WINDOW_LABEL, usdWords, useOrgBoard, type OrgDays, type OrgDrift,
} from "../../lib/agents-org-board";
import { useConsoleLive } from "../../lib/console-live";
import { OrgAgentPanel } from "./org-agent-panel";
import { orgActions } from "./org-actions";
import { OrgEdgePanel, type EdgeFocus } from "./org-edge-panel";
import { OrgFlow } from "./org-flow";
import { OrgTeams } from "./org-teams";
import { useNarrow } from "./team-zones";
import { localFull } from "../../lib/local-time";
import { useIdentityVersion } from "../../lib/use-agent-identities";

export type OrgLayout = "teams" | "flow" | "lines";
const LEGACY_LAYOUT: Record<string, OrgLayout> = { zones: "teams", swarm: "flow" };

/** `layout`, `days` and `isolate` from a URL's search, legacy names mapped. */
export function readOrgUrl(search: string): { layout: OrgLayout | null; days: OrgDays; isolate: string | null } {
  const q = new URLSearchParams(search);
  const raw = q.get("layout") ?? "";
  const layout = (["teams", "flow", "lines"] as const).find((l) => l === raw) ?? LEGACY_LAYOUT[raw] ?? null;
  const d = Number(q.get("days"));
  const days = (ORG_WINDOWS as readonly number[]).includes(d) ? d as OrgDays : 7;
  return { layout, days, isolate: q.get("isolate") || null };
}

function writeOrgUrl(patch: Record<string, string | null>): void {
  try {
    const url = new URL(window.location.href);
    for (const [k, v] of Object.entries(patch)) {
      if (v === null) url.searchParams.delete(k); else url.searchParams.set(k, v);
    }
    if (url.toString() !== window.location.href) window.history.replaceState(null, "", url.toString());
  } catch { /* non-browser */ }
}

function useWide(query: string): boolean {
  const [on, setOn] = useState(() => typeof window !== "undefined" && !!window.matchMedia?.(query).matches);
  useEffect(() => {
    const mq = typeof window !== "undefined" ? window.matchMedia?.(query) : undefined;
    if (!mq) return undefined;
    const f = () => setOn(mq.matches);
    f();
    mq.addEventListener?.("change", f);
    return () => mq.removeEventListener?.("change", f);
  }, [query]);
  return on;
}

export type OrgViewProps = {
  projectId: string;
  live: LiveMap;
  search?: string;
  /** `?run=` and `?focus=`, read from the URL by the shell. */
  focusRunId?: string | null;
  focusTeam?: string | null;
  onClearFocus?: () => void;
  /** Today's OrgChartArea, given the Plan vs work comparison to draw above it. */
  lines: (drift: OrgDrift | null) => React.ReactNode;
  /** The signed-in actor; every write is made as them. Writes are offered only when given. */
  actor?: string;
  /** `+ New agent`, prefilled: `Make agent` (a missing role) and `Register <name>`. */
  onNewAgent?: (o?: { name?: string; team?: string }) => void;
};

export function OrgView({ projectId, live, search = "", focusRunId, focusTeam, onClearFocus, lines, actor,
                          onNewAgent }: OrgViewProps): React.ReactElement {
  const identityV = useIdentityVersion(); // names and faces re-resolve when the install's identities load
  const initial = useMemo(() => (typeof window !== "undefined" ? readOrgUrl(window.location.search)
    : { layout: null, days: 7 as OrgDays, isolate: null }), []);
  const [layout, setLayoutState] = useState<OrgLayout>(initial.layout ?? (focusRunId ? "flow" : "teams"));
  const [days, setDaysState] = useState<OrgDays>(initial.days);
  const [isolate, setIsolateState] = useState<string | null>(initial.isolate);
  const [selected, setSelected] = useState<string | null>(null);
  const [panel, setPanel] = useState(false);
  const [edge, setEdge] = useState<EdgeFocus | null>(null);
  const [flowMode, setFlowMode] = useState<"map" | "table">("map");
  const narrow = useNarrow();
  const wide = useWide("(min-width: 1024px)");
  const consoleLive = useConsoleLive();
  const org = useOrgBoard(projectId, days, true);
  const board = org.board;
  const actions = useMemo(() => (actor ? orgActions(projectId, actor, org.refresh) : null),
                          // eslint-disable-next-line react-hooks/exhaustive-deps -- its toasts name agents
                          [projectId, actor, org.refresh, identityV]);

  // Back/forward and a link arriving: the URL wins.
  useEffect(() => {
    const sync = () => {
      const now = readOrgUrl(window.location.search);
      if (now.layout) setLayoutState(now.layout);
      setDaysState(now.days);
      setIsolateState(now.isolate);
    };
    window.addEventListener("popstate", sync);
    // A legacy name is rewritten, not just read: the URL says what is on screen.
    const raw = new URLSearchParams(window.location.search).get("layout");
    if (raw && LEGACY_LAYOUT[raw]) writeOrgUrl({ layout: LEGACY_LAYOUT[raw] });
    return () => window.removeEventListener("popstate", sync);
  }, []);
  // A run focus is a Flow thing; a team focus is a Teams thing.
  useEffect(() => { if (focusRunId) { setLayoutState("flow"); writeOrgUrl({ layout: "flow" }); } }, [focusRunId]);
  const teamFocus = focusTeam && board?.teams.some((t) => t.key === focusTeam) ? focusTeam
    : focusTeam && TEAM_ORDER.includes(focusTeam) ? focusTeam : null;
  useEffect(() => { if (teamFocus && !focusRunId) { setLayoutState("teams"); writeOrgUrl({ layout: "teams" }); } }, [teamFocus, focusRunId]);

  const setLayout = (l: OrgLayout) => { setLayoutState(l); writeOrgUrl({ layout: l === "teams" ? null : l }); };
  const setDays = (d: OrgDays) => { setDaysState(d); writeOrgUrl({ days: d === 7 ? null : String(d) }); };
  const setIsolate = useCallback((t: string | null) => { setIsolateState(t); writeOrgUrl({ isolate: t }); }, []);
  const select = useCallback((r: string | null) => { setSelected(r); setPanel(!!r); if (r) setEdge(null); }, []);

  // Esc: drawer → isolate → selection → run focus (a menu closes itself first).
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key !== "Escape") return;
      if (edge) setEdge(null);
      else if (panel) setPanel(false);
      else if (isolate && layout === "flow") setIsolate(null);
      else if (selected) setSelected(null);
      else if (focusRunId) onClearFocus?.();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [edge, panel, isolate, selected, focusRunId, onClearFocus, layout, setIsolate]);

  const options: { key: OrgLayout; label: string; tip: string }[] = [
    { key: "teams", label: "Teams", tip: "Each team's work, waits, spend and gaps" },
    { key: "flow", label: "Flow", tip: "Where work goes between teams" },
    { key: "lines", label: "Lines", tip: "Planned reporting lines. Doesn't change who runs a step." },
  ];
  const radioKeys = (e: React.KeyboardEvent, keys: string[], at: number, go: (i: number) => void) => {
    if (e.key !== "ArrowRight" && e.key !== "ArrowLeft") return;
    e.preventDefault();
    const next = (at + (e.key === "ArrowRight" ? 1 : keys.length - 1)) % keys.length;
    go(next);
    (e.currentTarget.querySelectorAll<HTMLElement>("[role='radio']")[next])?.focus();
  };

  const t = board?.totals;
  const totals = t ? (t.spendOk ? [
    t.meteredUsd > 0 ? { text: `${usdWords(t.meteredUsd)} metered`, tip: "Model spend billed per call in this window" } : null,
    t.planCalls > 0 ? { text: `${t.planCalls} on plan`, tip: "Calls covered by a subscription. Not counted as money." } : null,
    t.untiedUsd > 0 ? { text: `${usdWords(t.untiedUsd)} not tied to an agent`, tip: "Calls with no agent recorded" } : null,
  ].filter(Boolean) as { text: string; tip: string }[] : [{ text: "Spend unknown", tip: t.spendReason ?? "" }]) : [];
  const totalsTip = totals.map((x) => x.text).join(" · ");
  const stale = !!org.error && !!org.confirmedAt && Date.now() - org.confirmedAt > 60_000;

  const onWorksWith = (team: string, from: string, to: string) => {
    setLayout("flow");
    setIsolate(team);
    setEdge({ from, to, level: "team" });
  };

  let body: React.ReactNode;
  if (layout === "lines") {
    body = lines(board?.drift ?? null);
  } else if (!board && org.loading) {
    body = (
      <div className="ag-zones ag-org-skeleton" data-testid="ag-zones" aria-busy="true" data-layout={layout}>
        <span className="ag-sr">Loading</span>
        {Array.from({ length: layout === "flow" ? 5 : 6 }, (_, i) => (
          <div key={i} className={layout === "flow" ? "ag-org-skel-pill" : "ag-org-skel-card"} aria-hidden />
        ))}
      </div>
    );
  } else if (!board) {
    body = (
      <div className="ag-empty" role="alert" data-testid="ag-org-error">
        <span title={org.error ?? undefined}>Can&apos;t reach BOSS</span>
        <button type="button" className="ag-btn" onClick={() => org.refresh()}>
          <ArrowClockwise size={12} aria-hidden /> Retry
        </button>
      </div>
    );
  } else if (!board.teams.some((x) => x.members.length)) {
    body = (
      <div className="ag-empty" data-testid="ag-org-none">
        <span>No agents yet</span>
        {onNewAgent ? <button type="button" className="ag-btn ag-btn-primary" onClick={() => onNewAgent()}>+ New agent</button> : null}
      </div>
    );
  } else if (layout === "flow") {
    body = (
      <OrgFlow projectId={projectId} board={board} live={live} search={search} days={days} narrow={narrow}
               isolate={isolate} onIsolate={setIsolate} selected={selected} onSelect={select}
               onOpenEdge={(f) => { setEdge(f); setPanel(false); }}
               focusRunId={focusRunId} onClearFocus={onClearFocus} onWindow={setDays}
               mode={flowMode} onMode={setFlowMode} onShowWaits={() => setLayout("teams")} />
    );
  } else {
    body = (
      <OrgTeams projectId={projectId} board={board} live={live} search={search} days={days} narrow={narrow}
                focusTeam={teamFocus} selected={selected} onSelect={select} onWorksWith={onWorksWith}
                onMakeAgent={(roleKey, team) => onNewAgent?.({ name: humaniseKey(roleKey), team })}
                onRegister={(name) => onNewAgent?.({ name })} actions={actions} />
    );
  }

  return (
    <div className="ag-org" data-testid="ag-org" data-layout={layout}>
      <div className="ag-org-bar">
        <span className="ag-seg" role="radiogroup" aria-label="Organization view"
              onKeyDown={(e) => radioKeys(e, options.map((o) => o.key), options.findIndex((o) => o.key === layout),
                                         (i) => setLayout(options[i].key))}>
          {options.map((o) => (
            <button key={o.key} type="button" role="radio" aria-checked={layout === o.key} aria-pressed={layout === o.key}
                    tabIndex={layout === o.key ? 0 : -1} data-testid={`ag-org-${o.key}`} title={o.tip}
                    onClick={() => setLayout(o.key)}>{o.label}</button>
          ))}
        </span>
        {layout !== "lines" ? (
          <span className="ag-seg" role="radiogroup" aria-label="Window" title={wide ? undefined : totalsTip || undefined}
                onKeyDown={(e) => radioKeys(e, ORG_WINDOWS.map(String), ORG_WINDOWS.indexOf(days),
                                           (i) => setDays(ORG_WINDOWS[i]))}>
            {ORG_WINDOWS.map((d) => (
              <button key={d} type="button" role="radio" aria-checked={days === d} aria-pressed={days === d}
                      tabIndex={days === d ? 0 : -1} data-testid={`ag-org-days-${d}`} onClick={() => setDays(d)}>
                {ORG_WINDOW_LABEL[d]}
              </button>
            ))}
          </span>
        ) : null}
        {!consoleLive ? (
          <span className="ag-chip ag-org-status" data-testid="ag-org-paused"
                title="Live updates are paused. Press Live in the top bar.">Paused</span>
        ) : stale && org.confirmedAt ? (
          <span className="ag-chip ag-org-status" data-testid="ag-org-stale"
                title={localFull(org.confirmedAt)}>
            Updated {Math.max(1, Math.round((Date.now() - org.confirmedAt) / 60_000))}m ago
          </span>
        ) : null}
        <span className="ag-spacer" />
        {layout !== "lines" && wide && totals.length ? (
          <span className="ag-org-totals" data-testid="ag-org-totals">
            {totals.map((x) => <span key={x.text} title={x.tip}>{x.text}</span>)}
          </span>
        ) : null}
      </div>
      {body}
      {board && panel && selected && layout !== "lines" ? (
        <OrgAgentPanel board={board} live={live} roleKey={selected} days={days} actions={actions}
                       onClose={() => setPanel(false)} />
      ) : null}
      {board && edge && layout !== "lines" ? (
        <OrgEdgePanel projectId={projectId} board={board} focus={edge} days={days} onClose={() => setEdge(null)} />
      ) : null}
    </div>
  );
}

function humaniseKey(key: string): string {
  const words = key.replace(/[_-]+/g, " ").trim();
  return words ? words[0].toUpperCase() + words.slice(1) : key;
}
