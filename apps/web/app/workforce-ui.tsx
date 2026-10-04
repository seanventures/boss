"use client";

import React, { useCallback, useEffect, useMemo, useRef, useState } from "react";

import type { AgentStudioFeed } from "../lib/agent-studio";
import { AgentDetail } from "./agent-detail-ui";
import { PanelsView, orphanProfiles } from "./agent-profile-ui";
import { AgentQuickCreate } from "./agent-quick-create";
import { FocusedAgents } from "./focused-agents-ui";
import type { OrgChart, OrgNode, V2Agent, Workforce } from "../lib/v2/workforce";
import { apiWrite } from "../lib/api-write";
import { askAgent as talkTo, identityOf, openAgent } from "../lib/agent-world";
import { useLivePresence } from "../lib/agents-live";
import { AgFace } from "./agents/agent-face";
import { RosterSummary, RosterZones, placeAgents } from "./agents/roster-zones";
import { TeamSwarm } from "./agents/team-swarm";
import { openDockAbout } from "../lib/dock-subject";
import { openRoom } from "../lib/agent-converse";

/**
 * The Agents area: Roster, Team and Panels, in v1's own styling.
 *
 * The data is the same the v2 screens read — one fetch each, already bounded server-side.
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

function volumeOf(a: V2Agent): number {
  return (a.calls ?? 0) + (a.steps ?? 0);
}

/** Talk to this agent, 1:1, in its own voice (agents/talk-sheet.tsx). By role_key: the name
 *  is a display string, and two agents can share one. An agent with no registered role has no
 *  voice to talk in yet, so it keeps the dock, addressed by name. */
function askAgent(agent: Pick<V2Agent, "id" | "name" | "registered">): void {
  if (agent.registered) talkTo(agent.id, agent.name, { from: "roster" });
  else openDockAbout({ subject: null, role: agent.name, question: `@${agent.name}: ` });
}

export type AgentsView = "roster" | "team" | "panels";

/** What "N unregistered" means, and what pressing it does. */
export const UNREGISTERED_TIP =
  "Doing work with no registered role, so nothing governs it: no trust score, no limits. Show them to register.";

/** The view the URL names. `view` is shared with other areas (the workflow editor uses
 *  visual|json), so anything this area does not know is the roster. */
export function readAgentsView(search: string): AgentsView {
  const v = new URLSearchParams(search).get("view");
  return v === "team" || v === "panels" ? v : "roster";
}

/**
 * Agents: every agent, keyed by `role_key` — the thing a workflow step's `captain` names, so
 * the roster, the team, a step's agent picker and an agent's "Used in" all count the same.
 *
 * Three views of one set: the Roster (who they are and what they have done), the Team (planned
 * reporting lines; it does not route work) and Panels (agents that decide together; hidden
 * until there is one). One "+ New agent" makes an agent wherever it is pressed.
 */
export function WorkforceArea(
  { workforce, chart, studio, actor }: {
    workforce: Workforce;
    chart?: OrgChart;
    /** The Studio catalog: Profiles, Panels, and orphan definitions offered as templates. */
    studio?: AgentStudioFeed;
    /** The signed-in actor every agent write is made as — never a literal. */
    actor: string;
  },
) {
  const [view, setViewState] = useState<AgentsView>("roster");
  const [filter, setFilter] = useState<"all" | "needs_you" | "worked" | "duty" | "retired" | "unregistered">("all");
  const [search, setSearch] = useState("");
  const [teamLayout, setTeamLayout] = useState<"list" | "swarm">("list");
  // WHO IS DOING WHAT, from open work: the same store the top-bar pill reads.
  const live = useLivePresence(workforce.project_id);
  // WHICH AGENT IS OPEN, read from the URL so Back closes it and a link opens it.
  const [openRole, setOpenRole] = useState("");
  useEffect(() => {
    const sync = () => {
      try {
        const url = new URL(window.location.href);
        setOpenRole(url.searchParams.get("role") ?? "");
        setViewState(readAgentsView(url.search));
      } catch { /* non-browser */ }
    };
    sync();
    window.addEventListener("popstate", sync);
    return () => window.removeEventListener("popstate", sync);
  }, []);
  const setView = (next: AgentsView) => {
    setViewState(next);
    try {
      const url = new URL(window.location.href);
      if (next === "roster") url.searchParams.delete("view"); else url.searchParams.set("view", next);
      window.history.pushState(null, "", url.toString());
    } catch { /* non-browser */ }
  };
  // `true` for a blank agent; a name to register an agent that works with no registered role.
  const [creating, setCreating] = useState<boolean | string>(false);

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
  const retired = useMemo(() => all.filter((a) => identityOf(a.id, a.name).lifecycle === "retired"), [all]);
  const everyone = useMemo(() => all.filter((a) => !retired.includes(a)), [all, retired]);
  const needsYou = useMemo(() => new Set(placeAgents(everyone, live)
    .filter((p) => p.zone === "needs_you").map((p) => p.agent.id)), [everyone, live]);
  const agents = useMemo(() => {
    const scoped = filter === "needs_you" ? everyone.filter((a) => needsYou.has(a.id))
      : filter === "worked" ? everyone.filter((a) => volumeOf(a) > 0)
      : filter === "retired" ? retired
      : filter === "unregistered" ? everyone.filter((a) => !a.registered) : everyone;
    const query = search.trim().toLowerCase();
    const who = (a: typeof scoped[number]) => {
      const id = identityOf(a.id, a.name);
      return `${a.name} ${a.role} ${a.task} ${id.callsign ?? ""} ${id.tagline ?? ""}`.toLowerCase();
    };
    return query ? scoped.filter((a) => who(a).includes(query)) : scoped;
  }, [everyone, retired, needsYou, filter, search]);

  // COUNTED, NOT ASSUMED: roles idle all window have recorded no work.
  const workedCount = everyone.filter((a) => volumeOf(a) > 0).length;
  const unregisteredCount = everyone.filter((agent) => !agent.registered).length;
  // Once the last one is registered, the "unregistered" view has nothing left to show.
  useEffect(() => {
    if (filter === "unregistered" && unregisteredCount === 0) setFilter("all");
  }, [filter, unregisteredCount]);
  const panels = studio?.panels ?? [];
  const orphans = useMemo(() => studio ? orphanProfiles(studio, everyone.map((a) => a.id)) : [],
                          [studio, everyone]);

  if (!workforce.apiReachable) {
    return (
      <p className="detail" title={workforce.blockedReason}>
        Could not reach the control plane
      </p>
    );
  }

  return (
    <div className="wf-agents" data-testid="agents-area">
      <header className="wf-lib-head">
        <div className="wf-tabs" role="tablist" aria-label="Agents">
          <button type="button" role="tab" className="wf-tab" aria-selected={view === "roster"}
                  data-testid="agents-view-roster" onClick={() => setView("roster")}>
            Roster <em data-testid="wf-roster-count">({everyone.length})</em>
          </button>
          <button type="button" role="tab" className="wf-tab" aria-selected={view === "team"}
                  data-testid="agents-view-team" title="Planned reporting lines. Doesn't change who runs a step."
                  onClick={() => setView("team")}>Team</button>
          {panels.length ? (
            <button type="button" role="tab" className="wf-tab" aria-selected={view === "panels"}
                    data-testid="agents-view-panels" onClick={() => setView("panels")}>
              Panels <em>({panels.length})</em>
            </button>
          ) : null}
        </div>
        {view === "roster" ? <RosterSummary agents={everyone} live={live} /> : null}
        <span className="wf-lib-spacer" />
        {view === "team" ? (
          <span className="wf-seg" role="group" aria-label="Layout">
            <button type="button" aria-pressed={teamLayout === "list"} data-active={teamLayout === "list"}
                    data-testid="agents-team-list" onClick={() => setTeamLayout("list")}>List</button>
            <button type="button" aria-pressed={teamLayout === "swarm"} data-active={teamLayout === "swarm"}
                    data-testid="agents-team-swarm" title="Who handed work to whom, live"
                    onClick={() => setTeamLayout("swarm")}>Swarm</button>
          </span>
        ) : null}
        {view === "roster" ? (
          <label className="wf-lib-search">
            <input type="search" aria-label="Find an agent" placeholder="Find an agent"
                   value={search} onChange={(event) => setSearch(event.target.value)} />
          </label>
        ) : null}
        <button type="button" className="wf-btn" data-testid="wf-open-room"
                title="Ask 2-5 agents at once" onClick={() => openRoom()}>Room</button>
        <button type="button" className="wf-btn wf-btn-primary" data-testid="wf-new-agent"
                onClick={() => setCreating(true)}>+ New agent</button>
      </header>

      {creating ? (
        <AgentQuickCreate projectId={workforce.project_id} actor={actor} orphans={orphans}
                          initialName={typeof creating === "string" ? creating : ""}
                          onClose={() => setCreating(false)}
                          onCreated={(roleKey) => { setCreating(false); openAgent(roleKey); }} />
      ) : null}

      {view === "team" ? (
        !chart ? <p className="detail">Team unavailable</p>
          : teamLayout === "swarm" ? <TeamSwarm projectId={workforce.project_id} chart={chart} live={live} />
          : <OrgChartArea projectId={workforce.project_id} actor={actor} chart={chart} />
      ) : view === "panels" && studio ? (
        <PanelsView feed={studio} actor={actor} />
      ) : (
        <div className="roster">
          <div className="roster-filterbar">
            <span className="roster-filters">
              {(["all", "duty", "needs_you", "worked", ...(retired.length ? ["retired" as const] : [])] as const).map((f) => (
                <button key={f} type="button" className="cap-btn" data-active={filter === f}
                        onClick={() => setFilter(f)} data-testid={`wf-filter-${f}`}
                        title={f === "duty" ? "Agents that watch and act on their own"
                          : f === "retired" ? "Kept for their work. Hidden from pickers." : undefined}>
                  {f === "all" ? `All ${everyone.length}` : f === "needs_you" ? `Needs you ${needsYou.size}`
                    : f === "worked" ? `Has done work ${workedCount}`
                    : f === "retired" ? `Retired ${retired.length}` : "On duty"}
                </button>
              ))}
            </span>
            {/* Governance gap, said once: an agent running work without a registered role has
                no trust score, no autonomy level and no violation tracking. Pressed, it shows
                those agents, each with Register. */}
            {unregisteredCount > 0 ? (
              <button type="button" className="cap-btn roster-ungoverned" data-testid="roster-ungoverned"
                      data-active={filter === "unregistered"} title={UNREGISTERED_TIP}
                      onClick={() => setFilter(filter === "unregistered" ? "all" : "unregistered")}>
                {unregisteredCount} unregistered
              </button>
            ) : null}
          </div>

          {filter === "duty" ? (
            <FocusedAgents projectId={workforce.project_id} actor={actor} />
          ) : agents.length === 0 ? (
            everyone.length === 0 && filter === "all" && !search.trim() ? (
              <div className="roster-empty" data-testid="workforce-empty">
                <span className="detail">No agents yet</span>
                <button type="button" className="wf-btn" onClick={() => setCreating(true)}>+ New agent</button>
              </div>
            ) : <p className="detail">No match</p>
          ) : (
            // GROUPED BY WHAT THEY ARE DOING: Needs you, Working, In a panel, Idle (spec §3.2).
            <RosterZones agents={agents} live={live} onOpen={openAgent}
                         onAsk={(roleKey, name) => askAgent(
                           agents.find((a) => a.id === roleKey) ?? { id: roleKey, name, registered: true })}
                         onRegister={(name) => setCreating(name)} />
          )}
        </div>
      )}

      {/* The detail view, over every view. Rendered here rather than as a route because the
          console mounts every area at once and a route would be a second place agents live. */}
      {openRole ? (
        <AgentDetail projectId={workforce.project_id} roleKey={openRole} actor={actor}
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
export function OrgChartArea({ chart, projectId, actor }: {
  chart: OrgChart; projectId: string; actor: string;
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
      else await reload();
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
          <button type="button" className="cap-btn" disabled={busy} data-testid="org-reset-all"
                  onClick={() => {
                    if (window.confirm("Clear all planned reporting lines for this project?")) void release("*");
                  }}>
            Clear planned structure
          </button>
        ) : null}
      </div>
      {note ? <p className="org-note" data-tone="error"
                 data-testid="org-note">{note}</p> : null}

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
                      {parentId ? <button type="button" className="org-node-act" disabled={busy}
                              title="Make this a top-level role"
                              data-testid={`org-top-${n.id}`}
                              onClick={() => void place(n.id, null)}>Top level</button> : null}
                      {Object.prototype.hasOwnProperty.call(declared, n.id) ? (
                        <button type="button" className="org-node-act" disabled={busy}
                                title="Remove this role from the planned structure"
                                data-testid={`org-reset-${n.id}`}
                                onClick={() => void release(n.id)}>Remove</button>
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
                <button type="button" className="org-place-btn" disabled={busy}
                        onClick={() => void place(n.id, placementTargets[n.id] || null)}>
                  Place
                </button>
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
