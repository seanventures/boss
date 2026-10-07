"use client";

import { GatedButton } from "../components/gated";
import { DotsThree, MagnifyingGlass } from "@phosphor-icons/react";
import React, { useCallback, useEffect, useMemo, useRef, useState } from "react";

import type { AgentStudioFeed } from "../lib/agent-studio";
import { AgentDetail } from "./agent-detail-ui";
import { PanelsView, orphanProfiles } from "./agent-profile-ui";
import { AgentQuickCreate } from "./agent-quick-create";
import type { OrgChart, OrgNode, V2Agent, Workforce } from "../lib/v2/workforce";
import { apiWrite } from "../lib/api-write";
import { identityOf, isLive, openAgent } from "../lib/agent-world";
import { useAttention, useLivePresence } from "../lib/agents-live";
import { useLiveBoard } from "../lib/agents-live-board";
import { useConsoleLive } from "../lib/console-live";
import { useIdentityVersion } from "../lib/use-agent-identities";
import { AgFace } from "./agents/agent-face";
import { DirectoryView } from "./agents/directory-view";
import { LiveBoardView, liveDot } from "./agents/live-board";
import { OrgView } from "./agents/org-view";
import { PlanVsWork } from "./agents/org-drift";
import type { OrgDrift } from "../lib/agents-org-board";
import { notifyChanged } from "./changed";
import { ActivityRiver } from "./agents/activity-river";
import { openRoom } from "../lib/agent-converse";

/**
 * The Agents area: Live, Organization, Directory, Activity (and Panels when there is one) —
 * agents-home spec §3.
 *
 * Live is the default: what needs you, what is moving, what got done, who is idle. Directory is
 * the card grid, to find and set up an agent. Organization is the teams, reporting lines and
 * Swarm. Activity is what agents did, newest first. `?role=` opens the Passport over any tab.
 */

/** Acronyms stay acronyms: "Qa" and "Cto" read as typos. */
const ACRONYMS: Record<string, string> = {
  qa: "QA", cto: "CTO", coo: "COO", cao: "CAO", ux: "UX", vp: "VP", pm: "PM", sre: "SRE",
};
function humanise(key: string): string {
  return key.split("_")
    .map((w) => ACRONYMS[w.toLowerCase()] ?? (w.charAt(0).toUpperCase() + w.slice(1)))
    .join(" ");
}

export type AgentsView = "live" | "org" | "directory" | "activity" | "panels";

export { UNREGISTERED_TIP } from "./agents/directory-view";

/** Old links keep working: the Roster is the Directory now, the Team is Organization. */
const LEGACY_VIEWS: Record<string, AgentsView> = { roster: "directory", team: "org" };
const VIEWS = new Set<AgentsView>(["live", "org", "directory", "activity", "panels"]);

/** The view the URL names. `view` is shared with other areas (the workflow editor uses
 *  visual|json), so anything this area does not know is Live. */
export function readAgentsView(search: string): AgentsView {
  const v = new URLSearchParams(search).get("view") ?? "";
  if (LEGACY_VIEWS[v]) return LEGACY_VIEWS[v];
  return VIEWS.has(v as AgentsView) ? v as AgentsView : "live";
}

const TAB_TIP: Record<AgentsView, string | undefined> = {
  live: "What needs you and what's moving",
  org: "Teams, and who hands work to whom",
  directory: "Every agent, to find and set up",
  activity: "What agents did, newest first",
  panels: undefined,
};

/** Agents the header's search names: by name, callsign, key or role. */
export function matchAgents(agents: V2Agent[], query: string): V2Agent[] {
  const q = query.trim().toLowerCase();
  if (!q) return [];
  return agents.filter((a) => {
    const id = identityOf(a.id, a.name);
    return `${a.name} ${a.id} ${a.role} ${id.callsign ?? ""}`.toLowerCase().includes(q);
  });
}

function areaIsAgents(): boolean {
  try {
    const area = new URLSearchParams(window.location.search).get("area");
    return area === "workforce";
  } catch {
    return false;
  }
}

/**
 * Agents: every agent, keyed by `role_key` — the thing a workflow step's `captain` names, so
 * the roster, the team, a step's agent picker and an agent's "Used in" all count the same.
 * One "+ New agent" makes an agent wherever it is pressed.
 */
export function WorkforceArea(
  { workforce, chart, studio, actor, initialView = "live" }: {
    workforce: Workforce;
    chart?: OrgChart;
    /** The Studio catalog: Profiles, Panels, and orphan definitions offered as templates. */
    studio?: AgentStudioFeed;
    /** The signed-in actor every agent write is made as — never a literal. */
    actor: string;
    /** The view before the URL is read (a server render has no URL): Live unless told. */
    initialView?: AgentsView;
  },
) {
  const projectId = workforce.project_id;
  const [view, setViewState] = useState<AgentsView>(initialView);
  // Retired and the search read identities: a load (lifecycle, callsign) re-runs them.
  const identities = useIdentityVersion();
  const [search, setSearch] = useState("");
  const [searchOpen, setSearchOpen] = useState(false);
  const [menuOpen, setMenuOpen] = useState(false);
  const searchBox = useRef<HTMLInputElement>(null);
  // WHO IS DOING WHAT, and WHAT NEEDS YOU, from open work: the same store the top-bar pill reads.
  const live = useLivePresence(projectId);
  const attention = useAttention(projectId);
  const consoleLive = useConsoleLive();
  // ONE READ OF THE BOARD for Live, polled only while it is open. Organization reads its own
  // board (`/v1/agents/org-board`, 30s) and a focused run's chain; it never polls this one.
  const board = useLiveBoard(projectId, workforce.apiReachable && view === "live");
  // WHICH AGENT IS OPEN, read from the URL so Back closes it and a link opens it.
  const [openRole, setOpenRole] = useState("");
  const [focusId, setFocusId] = useState<string | null>(null);
  const [focusRun, setFocusRun] = useState<string | null>(null);
  // Activity reached from a link (Organization's edges): one agent and a window, from the URL.
  const [activityAgent, setActivityAgent] = useState<string | null>(null);
  const [activityDays, setActivityDays] = useState<1 | 7>(1);
  const [orgId, setOrgId] = useState<string | null>(null);
  useEffect(() => {
    const sync = () => {
      try {
        const url = new URL(window.location.href);
        setOpenRole(url.searchParams.get("role") ?? "");
        const raw = url.searchParams.get("view") ?? "";
        setViewState(readAgentsView(url.search));
        // AN OLD LINK IS REWRITTEN, not just read: the URL says what is on screen.
        if (LEGACY_VIEWS[raw] && url.searchParams.get("area") === "workforce") {
          url.searchParams.set("view", LEGACY_VIEWS[raw]);
          window.history.replaceState(null, "", url.toString());
        }
        setFocusId(url.searchParams.get("focus"));
        setFocusRun(url.searchParams.get("run"));
        setActivityAgent(url.searchParams.get("agent"));
        setActivityDays(Number(url.searchParams.get("days")) >= 7 ? 7 : 1);
        setOrgId(url.searchParams.get("org"));
      } catch { /* non-browser */ }
    };
    sync();
    window.addEventListener("popstate", sync);
    return () => window.removeEventListener("popstate", sync);
  }, []);
  const setView = (next: AgentsView, patch: Record<string, string | null> = {}) => {
    setViewState(next);
    try {
      const url = new URL(window.location.href);
      if (next === "live") url.searchParams.delete("view"); else url.searchParams.set("view", next);
      // A link's one-agent Activity filter belongs to that link, not to the next visit.
      if (!("agent" in patch)) { url.searchParams.delete("agent"); setActivityAgent(null); }
      for (const [k, v] of Object.entries(patch)) {
        if (v === null) url.searchParams.delete(k); else url.searchParams.set(k, v);
      }
      window.history.pushState(null, "", url.toString());
      if ("run" in patch) setFocusRun(patch.run ?? null);
      if ("focus" in patch) setFocusId(patch.focus ?? null);
    } catch { /* non-browser */ }
  };
  // `true` for a blank agent; a name to register an agent that works with no registered role;
  // a name and a team for Organization's `Make agent` (a role a workflow names, nobody fills).
  const [creating, setCreating] = useState<boolean | string | { name: string; team?: string }>(false);

  const closeAgent = useCallback(() => {
    setOpenRole("");
    try {
      const url = new URL(window.location.href);
      if (!url.searchParams.has("role")) return;
      url.searchParams.delete("role");
      // replace, not back(): back() would leave the site for anyone who arrived on the link.
      window.history.replaceState(null, "", url.toString());
    } catch { /* non-browser */ }
  }, []);
  // DELETED HERE, GONE HERE. The roster is server data refreshed on an interval; without this a
  // just-deleted agent stays on screen and opening it says it does not exist.
  const [deleted, setDeleted] = useState<Set<string>>(new Set());
  // THE ROSTER, NOT THE WORK FEED: every agent a step's picker offers, plus unregistered ones
  // doing work. `agents` alone is who worked, which said "All 5" beside a picker of 34.
  // RETIRED AGENTS KEEP THEIR WORK and leave the roster: they are under Retired only.
  const all = useMemo(
    () => (workforce.roster ?? workforce.agents ?? []).filter((a) => !deleted.has(a.id)),
    [workforce.roster, workforce.agents, deleted]);
  const retired = useMemo(() => all.filter((a) => identityOf(a.id, a.name).lifecycle === "retired"), [all, identities]); // eslint-disable-line react-hooks/exhaustive-deps -- the store version
  const everyone = useMemo(() => all.filter((a) => !retired.includes(a)), [all, retired]);
  const panels = studio?.panels ?? [];
  const orphans = useMemo(() => studio ? orphanProfiles(studio, everyone.map((a) => a.id)) : [],
                          [studio, everyone]);
  const matches = useMemo(() => matchAgents(everyone, search), [everyone, search, identities]); // eslint-disable-line react-hooks/exhaustive-deps -- the store version
  // Activity filters by one agent: the one the search names exactly, or the only match.
  const searchedRole = useMemo(() => {
    const q = search.trim().toLowerCase();
    if (!q) return null;
    const exact = matches.find((a) => a.id === q || a.name.toLowerCase() === q
      || (identityOf(a.id, a.name).callsign ?? "").toLowerCase() === q);
    return exact?.id ?? (matches.length === 1 ? matches[0].id : null);
  }, [matches, search, identities]); // eslint-disable-line react-hooks/exhaustive-deps -- the store version

  // THE SAME COUNTS AS THE PILL: working agents from presence, needs-you from attention items.
  const working = useMemo(() => [...live.values()].filter((p) => isLive(p) && p.state !== "needs_you").length, [live]);
  const needs = attention ? attention.count : [...live.values()].filter((p) => p.state === "needs_you").length;
  const dot = liveDot(needs, working);

  // `/` finds an agent, from anywhere in the area.
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key !== "/" || e.metaKey || e.ctrlKey || e.altKey || !areaIsAgents()) return;
      const t = e.target as HTMLElement | null;
      if (t && (t.closest("input, textarea, select, [contenteditable='true']"))) return;
      e.preventDefault();
      setSearchOpen(true);
      window.setTimeout(() => searchBox.current?.focus(), 0);
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, []);

  const tabs: { key: AgentsView; label: React.ReactNode; testid: string }[] = [
    { key: "live", testid: "agents-view-live", label: (
      <>
        {dot ? (
          <span className="ag-home-dot" data-tone={dot.tone} title={dot.tip} data-testid="agents-live-dot">
            <span className="ag-sr">{dot.tip}</span>
          </span>
        ) : null}
        Live
      </>
    ) },
    { key: "org", testid: "agents-view-org", label: "Organization" },
    { key: "directory", testid: "agents-view-directory",
      label: <>Directory <em data-testid="wf-roster-count">({everyone.length})</em></> },
    { key: "activity", testid: "agents-view-activity", label: "Activity" },
    ...(panels.length ? [{ key: "panels" as const, testid: "agents-view-panels",
                           label: <>Panels <em>({panels.length})</em></> }] : []),
  ];

  function onTabsKey(e: React.KeyboardEvent<HTMLDivElement>) {
    if (e.key !== "ArrowRight" && e.key !== "ArrowLeft" && e.key !== "Home" && e.key !== "End") return;
    const at = tabs.findIndex((t) => t.key === view);
    const next = e.key === "Home" ? 0 : e.key === "End" ? tabs.length - 1
      : (at + (e.key === "ArrowRight" ? 1 : -1) + tabs.length) % tabs.length;
    e.preventDefault();
    setView(tabs[next].key);
    window.setTimeout(() => e.currentTarget?.querySelectorAll<HTMLElement>("[role='tab']")[next]?.focus(), 0);
  }

  function goLiveSection(id: string, focusFirstCard = false) {
    if (view !== "live") setView("live");
    window.setTimeout(() => {
      const el = document.getElementById(id);
      el?.scrollIntoView?.({ block: "start", behavior: "smooth" });
      if (focusFirstCard) el?.querySelector<HTMLElement>("[data-att-card]")?.focus();
    }, 0);
  }

  if (!workforce.apiReachable) {
    return (
      <p className="detail" title={workforce.blockedReason}>
        Could not reach the control plane
      </p>
    );
  }

  const openMap = (team?: string) => setView("org", { focus: team ?? null, run: null });
  const showDropdown = (view === "live" || view === "org") && search.trim() && matches.length > 0;

  return (
    <div className="wf-agents ag-home" data-testid="agents-area" data-view={view}>
      <header className="wf-lib-head ag-home-head">
        <div className="wf-tabs ag-home-tabs" role="tablist" aria-label="Agent Studio" onKeyDown={onTabsKey}>
          {tabs.map((t) => (
            <button key={t.key} type="button" role="tab" className="wf-tab" aria-selected={view === t.key}
                    tabIndex={view === t.key ? 0 : -1} data-testid={t.testid} title={TAB_TIP[t.key]}
                    onClick={() => setView(t.key)}>
              {t.label}
            </button>
          ))}
        </div>
        <span className="ag-home-sum" data-testid="ag-home-summary">
          <button type="button" className="ag-home-count" title="Every agent, in Directory"
                  onClick={() => setView("directory")}>{everyone.length} agents</button>
          {working ? (
            <>
              <span aria-hidden> · </span>
              <button type="button" className="ag-home-count" data-tone="accent" title="See the work in Live"
                      onClick={() => goLiveSection("ag-live-work")}>{working} working</button>
            </>
          ) : null}
          {needs ? (
            <>
              <span aria-hidden> · </span>
              <button type="button" className="ag-home-count" data-tone="amber" data-testid="ag-home-needs"
                      title="Go to the first thing that needs you"
                      onClick={() => goLiveSection("ag-live-needs", true)}>
                {needs === 1 ? "1 needs you" : `${needs} need you`}
              </button>
            </>
          ) : null}
        </span>
        <span className="wf-lib-spacer" />
        <div className="ag-home-search" data-open={searchOpen || search ? "true" : undefined}>
          <button type="button" className="ag-icon-btn ag-home-search-icon" aria-label="Find an agent"
                  title="Find an agent (/)" onClick={() => { setSearchOpen(true); window.setTimeout(() => searchBox.current?.focus(), 0); }}>
            <MagnifyingGlass size={14} weight="bold" aria-hidden />
          </button>
          <label className="wf-lib-search">
            <input ref={searchBox} type="search" aria-label="Find an agent" placeholder="Find an agent"
                   data-testid="ag-home-search" value={search} title="Press / to search"
                   onChange={(event) => setSearch(event.target.value)}
                   onBlur={() => { if (!search) setSearchOpen(false); }}
                   onKeyDown={(e) => {
                     if (e.key === "Enter" && showDropdown) { e.preventDefault(); openAgent(matches[0].id); }
                     if (e.key === "Escape") { setSearch(""); setSearchOpen(false); }
                   }} />
          </label>
          {showDropdown ? (
            <ul className="ag-home-matches" role="listbox" aria-label="Matching agents" data-testid="ag-home-matches">
              {matches.slice(0, 8).map((a) => {
                const id = identityOf(a.id, a.name);
                return (
                  <li key={a.id} role="option" aria-selected={false}>
                    <button type="button" onMouseDown={(e) => e.preventDefault()} onClick={() => openAgent(a.id)}
                            data-testid={`ag-home-match-${a.id}`}>
                      <AgFace roleKey={a.id} size={20} decorative />
                      <span>{id.callsign || id.name}</span>
                      {id.callsign ? <span className="ag-muted">{id.name}</span> : null}
                    </button>
                  </li>
                );
              })}
            </ul>
          ) : null}
        </div>
        <button type="button" className="wf-btn ag-home-wide" data-testid="wf-open-room"
                title="Ask 2-5 agents at once" onClick={() => openRoom()}>Room</button>
        <button type="button" className="wf-btn wf-btn-primary ag-home-wide" data-testid="wf-new-agent"
                onClick={() => setCreating(true)}>+ New agent</button>
        <div className="ag-home-menu">
          <button type="button" className="ag-icon-btn" aria-label="More" aria-expanded={menuOpen}
                  onClick={() => setMenuOpen((v) => !v)}><DotsThree size={16} weight="bold" aria-hidden /></button>
          {menuOpen ? (
            <div className="ag-home-menu-pop" role="menu">
              <button type="button" role="menuitem" onClick={() => { setMenuOpen(false); openRoom(); }}>Room</button>
              <button type="button" role="menuitem" onClick={() => { setMenuOpen(false); setCreating(true); }}>+ New agent</button>
            </div>
          ) : null}
        </div>
      </header>

      {creating ? (
        <AgentQuickCreate projectId={workforce.project_id} actor={actor} orphans={orphans}
                          initialName={typeof creating === "string" ? creating
                            : typeof creating === "object" ? creating.name : ""}
                          initialTeam={typeof creating === "object" ? creating.team : undefined}
                          onClose={() => setCreating(false)}
                          onCreated={(roleKey) => { setCreating(false); openAgent(roleKey); }} />
      ) : null}

      {view === "live" ? (
        <LiveBoardView projectId={projectId} actor={actor} live={board} paused={!consoleLive}
                       focusId={focusId} search={search} onOpenMap={openMap}
                       onActivity={() => setView("activity")} onDirectory={() => setView("directory")}
                       onNewAgent={() => setCreating(true)} orgId={orgId} />
      ) : view === "org" ? (
        <OrgView projectId={projectId} live={live} search={search} actor={actor}
                 focusRunId={focusRun} focusTeam={focusId}
                 onClearFocus={() => setView("org", { run: null, focus: null })}
                 onNewAgent={(o) => setCreating(o?.name ? { name: o.name, team: o.team } : true)}
                 lines={(drift) => (chart ? <OrgChartArea projectId={projectId} actor={actor} chart={chart} drift={drift} />
                   : <p className="detail">Team unavailable</p>)} />
      ) : view === "activity" ? (
        <ActivityRiver projectId={projectId} roleKey={searchedRole ?? activityAgent} runId={focusRun}
                       key={`${activityAgent ?? ""}:${activityDays}`} initialDays={activityDays}
                       runLabel={board.board?.missions.find((m) => m.runId === focusRun)?.subject || undefined}
                       onClearRun={() => setView("activity", { run: null })} paused={!consoleLive} />
      ) : view === "panels" && studio ? (
        <PanelsView feed={studio} actor={actor} />
      ) : (
        <DirectoryView projectId={projectId} actor={actor} everyone={everyone}
                       retired={retired} live={live} search={search} onCreate={setCreating} />
      )}

      {/* The detail view, over every view. Rendered here rather than as a route because the
          console mounts every area at once and a route would be a second place agents live. */}
      {openRole ? (
        <AgentDetail projectId={projectId} roleKey={openRole} actor={actor}
                     studio={studio} onClose={closeAgent}
                     onDeleted={(key) => setDeleted((d) => new Set(d).add(key))} />
      ) : null}
    </div>
  );
}

/**
 * THE ORG CHART, EDITABLE.
 *
 * It was derived entirely from observed handoffs — who passed work to whom in the last week.
 * Honest, useful, and completely unchangeable: nobody could say "the QA lead reports to the
 * captain" or add a seat that has not been filled yet. A measurement nobody can act on is a
 * report, and this one had no effect anywhere else in the product.
 *
 * Planned reporting lines and observed handoffs stay in separate views. A reporting line is a
 * decision by the operator; an observed handoff is evidence of recent work, not authority.
 */
export function OrgChartArea({ chart, projectId, actor, drift = null }: {
  chart: OrgChart; projectId: string; actor: string;
  /** Organization › Lines › Plan vs work (org-view spec §4.5): the plan against 30 days of work. */
  drift?: OrgDrift | null;
}) {
  const [live, setLive] = useState<OrgChart>(chart);
  const [view, setView] = useState<"planned" | "observed">("planned");
  const [roleSearch, setRoleSearch] = useState("");
  const [rolePickerOpen, setRolePickerOpen] = useState(false);
  const [availableRoles, setAvailableRoles] = useState<Array<{ id: string; name: string; role: string }>>([]);
  const [rolesLoading, setRolesLoading] = useState(false);
  const [rolesError, setRolesError] = useState<string | null>(null);
  const [placementTargets, setPlacementTargets] = useState<Record<string, string>>({});
  const [busy, setBusy] = useState(false);
  const [note, setNote] = useState<string | null>(null);
  const unassignedRef = useRef<HTMLDetailsElement>(null);
  const [dragging, setDragging] = useState<string | null>(null);
  const [over, setOver] = useState<string | null>(null);
  useEffect(() => { setLive(chart); }, [chart]);

  useEffect(() => {
    setAvailableRoles([]);
    setRolesError(null);
    setRolePickerOpen(false);
    if (unassignedRef.current) unassignedRef.current.open = false;
  }, [projectId]);

  useEffect(() => {
    if (!rolePickerOpen) return;
    let alive = true;
    setRolesLoading(true);
    void fetch(`/api/agent?view=roles&project_id=${encodeURIComponent(projectId)}`,
      { cache: "no-store" })
      .then(async (response) => {
        if (!response.ok) throw new Error(`Roles returned ${response.status}`);
        return response.json();
      })
      .then((body) => {
        if (!alive) return;
        setAvailableRoles(((body.items ?? []) as Array<Record<string, unknown>>).map((item) => ({
          id: String(item.role_key ?? ""),
          name: String(item.display_name ?? item.role_key ?? ""),
          role: String(item.role_key ?? ""),
        })).filter((item) => item.id));
        setRolesError(null);
      })
      .catch((error) => { if (alive) setRolesError(String((error as Error).message ?? error)); })
      .finally(() => { if (alive) setRolesLoading(false); });
    return () => { alive = false; };
  }, [rolePickerOpen, projectId]);


  const reload = useCallback(async () => {
    try {
      const r = await fetch(`/api/org?project_id=${encodeURIComponent(projectId)}`,
                            { cache: "no-store" });
      if (r.ok) setLive(await r.json());
    } catch { /* keep what is on screen; a failed refresh is not a reason to blank it */ }
  }, [projectId]);

  const place = useCallback(async (agentId: string, reportsTo: string | null) => {
    setBusy(true); setNote(null);
    try {
      const r = await apiWrite(`/api/org`, {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ agent_id: agentId, project_id: projectId, actor,
                               reports_to: reportsTo }),
      });
      const body = await r.json().catch(() => ({}));
      // The API's own sentence: "X already sits above Y, so this would make a loop" is
      // actionable; "could not save" sends somebody looking in the wrong place.
      if (!r.ok) setNote(body.error ?? body.detail ?? "Could not move it.");
      else { await reload(); notifyChanged("org-lines"); }
    } finally { setBusy(false); }
  }, [projectId, actor, reload]);

  const release = useCallback(async (agentId: string) => {
    setBusy(true); setNote(null);
    try {
      const url = `/api/org?project_id=${encodeURIComponent(projectId)}`
        + `&agent_id=${encodeURIComponent(agentId)}`
        + `&actor=${encodeURIComponent(actor)}`;
      const r = await apiWrite(url, { method: "DELETE" });
      if (!r.ok) setNote("Could not reset it.");
      else await reload();
    } finally { setBusy(false); }
  }, [projectId, actor, reload]);

  const chartNow = live;
  const declared = chartNow.declared ?? {};
  const placedCount = Object.keys(declared).length;
  const treeNow = view === "observed"
    ? (chartNow.observed_tree ?? (placedCount ? [] : chartNow.tree) ?? [])
    : (placedCount ? chartNow.tree ?? [] : []);
  const parentFor = (node: OrgNode) => view === "planned"
    ? declared[node.id]?.reports_to ?? null : node.parent ?? null;
  // Keep each role beside its parent. Grouping all roles by depth makes unrelated
  // branches look like one reporting line when the chart gets wider than a row.
  const treeIds = new Set(treeNow.map((node) => node.id));
  const children = new Map<string, OrgNode[]>();
  for (const node of treeNow) {
    const assignedParent = parentFor(node);
    const parent = assignedParent && treeIds.has(assignedParent) ? assignedParent : "";
    children.set(parent, [...(children.get(parent) ?? []), node]);
  }
  const ordered: OrgNode[] = [];
  const visited = new Set<string>();
  const visit = (node: OrgNode) => {
    if (visited.has(node.id)) return;
    visited.add(node.id);
    ordered.push(node);
    for (const child of children.get(node.id) ?? []) visit(child);
  };
  for (const root of children.get("") ?? []) visit(root);
  for (const node of treeNow) visit(node);
  const isolated = (chartNow.nodes ?? []).filter((n) => !n.connected);
  // A definition is available to place, but is not part of the team's structure yet.
  // Include observed unregistered roles too, so real work can still be assigned explicitly.
  const placeable = [...new Map([...availableRoles, ...(chartNow.nodes ?? [])]
    .filter((n) => !Object.prototype.hasOwnProperty.call(declared, n.id))
    .map((n) => [n.id, n])).values()];
  const matchingPlaceable = placeable.filter((n) =>
    `${n.name} ${n.role} ${n.id}`.toLowerCase().includes(roleSearch.trim().toLowerCase()));
  const nameCounts = new Map<string, number>();
  for (const n of chartNow.nodes ?? []) nameCounts.set(n.name, (nameCounts.get(n.name) ?? 0) + 1);
  const displayName = (n: { id: string; name: string; role: string }) => (nameCounts.get(n.name) ?? 0) > 1
    ? `${n.name} (${humanise(n.id) === n.name ? n.id : humanise(n.id)})` : n.name;
  const namesById = new Map((chartNow.nodes ?? []).map((node) => [node.id, displayName(node)]));
  // A role can only report to a role that is in the plan.
  const plannedParents = [...new Map([...(chartNow.nodes ?? []), ...availableRoles]
    .filter((node) => Object.prototype.hasOwnProperty.call(declared, node.id))
    .map((node) => [node.id, node])).values()];

  if (!chartNow.apiReachable) {
    return <p className="detail" role="alert">
      The team is unavailable{chartNow.blockedReason ? ` — ${chartNow.blockedReason}` : ""}.
    </p>;
  }

  return (
    <div className="org" data-declared={view === "planned" && placedCount > 0}
         data-view={view}>
      <div className="org-viewbar">
        <div className="org-views" role="tablist" aria-label="Team views">
          <button type="button" role="tab" aria-selected={view === "planned"}
                  onClick={() => setView("planned")}>Planned roles</button>
          <button type="button" role="tab" aria-selected={view === "observed"}
                  onClick={() => setView("observed")}>Actual handoffs</button>
        </div>
      </div>
      <p className="detail" data-testid="org-provenance">
        {view === "planned"
          ? "Arrange responsibilities for this project. This plan does not change task routing."
          : `Read-only work handoffs from the last ${chartNow.window_hours} hours.${chartNow.root_inferred
            ? " A display root was selected because the handoffs form a cycle." : ""}`}
      </p>
      <div className="org-toolbar">
        {view === "planned" && placedCount > 0 ? (
          <span className="detail">{placedCount} role{placedCount === 1 ? "" : "s"} in structure</span>
        ) : null}
        {view === "observed" && treeNow.length > 0 ? (
          <span className="detail">{treeNow.length} role{treeNow.length === 1 ? "" : "s"} in recent handoffs</span>
        ) : null}
        {view === "planned" && placedCount ? (
          <GatedButton permission="agent:update" type="button" className="cap-btn" disabled={busy} data-testid="org-reset-all"
                  onClick={() => {
                    if (window.confirm("Clear all planned reporting lines for this project?")) void release("*");
                  }}>
            Clear planned structure
          </GatedButton>
        ) : null}
      </div>
      {note ? <p className="org-note" data-tone="error"
                 data-testid="org-note">{note}</p> : null}

      {drift && (placedCount > 0 || drift.lines > 0) && view === "planned" ? (
        <PlanVsWork drift={drift} busy={busy} declared={declared}
                    onPlan={(child, parent) => void place(child, parent)} />
      ) : null}

      {treeNow.length === 0 ? (
        <div className="org-empty" data-testid="org-empty">
          <p className="detail">{view === "planned"
            ? "No planned roles yet."
            : "No recent handoffs."}</p>
          {view === "planned" ? (
            <button type="button" className="org-next-action" onClick={() => {
              if (unassignedRef.current) {
                unassignedRef.current.open = true;
                unassignedRef.current.scrollIntoView({ block: "nearest" });
              }
            }}>Choose a role to place</button>
          ) : null}
        </div>
      ) : (
        <div className="org-tree" data-testid="org-tree">
          <div className="org-list-head" aria-hidden="true">
            <span>Role</span><span>{view === "planned" ? "Reports to" : "Shown under"}</span><span>Open</span>
          </div>
          {ordered.map((n) => {
            const parentId = parentFor(n);
            return (
              <div className="org-node" key={n.id} data-root={!parentId}
                     data-testid={`org-node-${n.id}`}
                     data-placed={Object.prototype.hasOwnProperty.call(declared, n.id)}
                     data-over={over === n.id || undefined}
                     style={{ "--org-depth": Math.min(n.depth ?? 0, 6) } as React.CSSProperties}
                     draggable={view === "planned" && !busy}
                     onDragStart={() => setDragging(n.id)}
                     onDragEnd={() => { setDragging(null); setOver(null); }}
                     onDragOver={(e) => { e.preventDefault(); setOver(n.id); }}
                     onDragLeave={() => setOver((c) => (c === n.id ? null : c))}
                     onDrop={(e) => {
                       e.preventDefault();
                       setOver(null);
                       if (view === "planned" && dragging && dragging !== n.id) void place(dragging, n.id);
                       setDragging(null);
                     }}>
                  <div className="org-node-identity">
                    <AgFace roleKey={n.id} size={24} decorative />
                    <span className="org-node-name">{displayName(n)}</span>
                    {/* Only show the role when it adds information beyond the name. */}
                    {humanise(n.role || n.id) !== n.name ? (
                      <span className="org-node-role">{humanise(n.role || n.id)}</span>
                    ) : null}
                    {view === "observed" && n.task ? <span className="org-node-task">{n.task}</span> : null}
                  </div>
                  <span className="org-node-parent">
                    {parentId ? namesById.get(parentId) ?? humanise(parentId)
                      : view === "planned" ? "Top level"
                        : chartNow.root_inferred ? "Display root" : "Handoff start"}
                  </span>
                  <span className="org-node-tools">
                    <button type="button" className="org-node-act" onClick={() => openAgent(n.id)}
                            data-testid={`org-work-${n.id}`}>Work</button>
                    {view === "planned" ? <>
                      {parentId ? <GatedButton permission="agent:update" type="button" className="org-node-act" disabled={busy}
                              title="Make this a top-level role"
                              data-testid={`org-top-${n.id}`}
                              onClick={() => void place(n.id, null)}>Top level</GatedButton> : null}
                      {Object.prototype.hasOwnProperty.call(declared, n.id) ? (
                        <GatedButton permission="agent:update" type="button" className="org-node-act" disabled={busy}
                                title="Remove this role from the planned structure"
                                data-testid={`org-reset-${n.id}`}
                                onClick={() => void release(n.id)}>Remove</GatedButton>
                      ) : null}
                    </> : null}
                  </span>
              </div>
            );
          })}
        </div>
      )}

      {view === "planned" ? (
        <details className="org-unassigned" data-testid="org-unassigned" ref={unassignedRef}
                 onToggle={(event) => setRolePickerOpen(event.currentTarget.open)}>
          <summary>Add role to team</summary>
          <p className="detail">Choose a definition to place in the team. Adding it does not start work.</p>
          {rolesError ? <p className="detail" role="alert">Could not load roles: {rolesError}</p> : null}
          {rolesLoading ? <p className="detail">Loading roles…</p> : null}
          <label className="org-unassigned-search">
            <span>Find a role</span>
            <input type="search" value={roleSearch}
                   onChange={(event) => setRoleSearch(event.target.value)} />
          </label>
          <div className="org-unassigned-list" data-testid="org-bench">
            {matchingPlaceable.map((n) => (
              <div className="org-unassigned-row" key={n.id}
                   data-testid={`org-bench-${n.id}`}>
                <span>{displayName(n)}</span>
                {/* Testids say which list the button is in: the same id was reused in the tree,
                    this bench and the no-handoffs list, so a test could not tell them apart. */}
                <button type="button" className="org-node-act" onClick={() => openAgent(n.id)}
                        data-testid={`org-bench-work-${n.id}`}>Work</button>
                {/* ONLY ROLES IN THE PLAN. This listed chartNow.tree, which falls back to the
                    observed tree when nothing is planned, so a role could be told to report to
                    somebody the plan did not contain — and then rendered at the top level with
                    a parent named beside it. */}
                <select aria-label={`Reports to for ${displayName(n)}`}
                        value={placementTargets[n.id] ?? ""}
                        onChange={(event) => setPlacementTargets((current) =>
                          ({ ...current, [n.id]: event.target.value }))}
                        data-testid={`org-bench-parent-${n.id}`}>
                  <option value="">Top-level role</option>
                  {plannedParents.map((parent) => (
                    <option key={parent.id} value={parent.id}>{displayName(parent)}</option>
                  ))}
                </select>
                <GatedButton permission="agent:update" type="button" className="org-place-btn" disabled={busy}
                        onClick={() => void place(n.id, placementTargets[n.id] || null)}>
                  Place
                </GatedButton>
              </div>
            ))}
            {!rolesLoading && !rolesError && matchingPlaceable.length === 0 ? (
              <p className="detail">No match. Make one with + New agent.</p>
            ) : null}
          </div>
        </details>
      ) : null}

      {view === "observed" && isolated.length > 0 ? (
        <details className="org-unassigned" data-testid="roster-alone">
          <summary>No recent handoffs <span>{isolated.length}</span></summary>
          <div className="org-unassigned-list">
            {isolated.map((n) => (
              <div className="org-unassigned-row" key={n.id} data-testid={`org-alone-${n.id}`}>
                <span>{displayName(n)}</span>
                <button type="button" className="org-node-act" onClick={() => openAgent(n.id)}
                        data-testid={`org-alone-work-${n.id}`}>Work</button>
              </div>
            ))}
          </div>
        </details>
      ) : null}
    </div>
  );
}
