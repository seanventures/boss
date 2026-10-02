"use client";

import React, { useCallback, useEffect, useMemo, useRef, useState } from "react";

import type { RoleFeedItem } from "../lib/roles";
import type { SkillFeedItem } from "../lib/skills";
import { AgentCreationWizard } from "./agent-creation-wizard";
import { AgentDetail } from "./agent-detail-ui";
import type { OrgChart, OrgNode, V2Agent, Workforce } from "../lib/v2/workforce";
import { apiWrite } from "../lib/api-write";
import { openDockAbout } from "../lib/dock-subject";

/**
 * Workforce and Org Chart, in v1's own styling.
 *
 * The data is the same the v2 screens read — one fetch each, already bounded server-side. What
 * differs is the chrome: v2's classes live under `.v2-shell` and render unstyled here, so these
 * use v1's own compact list and structure styles rather than importing unstyled components.
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

function AgentModelHelp() {
  return (
    <span className="org-model-help">
      <button type="button" aria-label="How roles, agents, and runs fit together">?</button>
      <span role="tooltip">
        A role names a responsibility. An agent definition sets its behavior. Runs record actual
        work. Team structure is a plan; it does not route tasks.
      </span>
    </span>
  );
}

function openArea(area: "org_chart" | "studio"): void {
  const url = new URL(window.location.href);
  url.searchParams.set("area", area);
  url.searchParams.delete("role");
  url.searchParams.delete("studio_role");
  window.history.pushState(null, "", url.toString());
  window.dispatchEvent(new PopStateEvent("popstate"));
}

/** Where to go to put a question to one of these agents, keeping who you are. */
/** Open the console's side dock, addressed to this member of the workforce.
 *
 *  This was an <a href> that rebuilt the query string and sent the operator to the Agents
 *  AREA, so asking one question cost them whatever they were looking at — and the answer came
 *  from BOSS-in-general anyway, because the name was only prose in a prefilled box.
 *
 *  Now it edits the CURRENT url and tells the shell to re-read it: same page, same scroll
 *  position, panel open on the right with the agent's name in its head. `dock_role` is what
 *  makes the reply come from that role's lens and its own past arguments.
 */
function askAgent(agentName: string): void {
  // `dock_role` is what makes the reply come from that role's lens and its own past arguments.
  openDockAbout({ subject: null, role: agentName, question: `@${agentName}: ` });
}

/** Open one agent. Addressable, because a view nobody can link to is one nobody can send. */
function openAgent(roleKey: string): void {
  try {
    const url = new URL(window.location.href);
    url.searchParams.set("role", roleKey);
    window.history.pushState(null, "", url.toString());
    window.dispatchEvent(new PopStateEvent("popstate"));
  } catch { /* non-browser */ }
}

function openRoleWork(roleKey: string): void {
  try {
    const url = new URL(window.location.href);
    url.searchParams.set("area", "workforce");
    url.searchParams.set("role", roleKey);
    url.searchParams.delete("studio_role");
    window.history.pushState(null, "", url.toString());
    window.dispatchEvent(new PopStateEvent("popstate"));
  } catch { /* non-browser */ }
}

function openAgentDefinition(roleKey: string): void {
  try {
    const url = new URL(window.location.href);
    url.searchParams.set("area", "studio");
    url.searchParams.set("studio_role", roleKey);
    url.searchParams.delete("role");
    window.history.pushState(null, "", url.toString());
    window.dispatchEvent(new PopStateEvent("popstate"));
  } catch { /* non-browser */ }
}

export function WorkforceArea(
  { workforce, skills = [] }: { workforce: Workforce; skills?: SkillFeedItem[] },
) {
  const [filter, setFilter] = useState<"all" | "working" | "worked">("all");
  const [search, setSearch] = useState("");
  const [showAll, setShowAll] = useState(false);
  // WHICH AGENT IS OPEN, read from the URL so Back closes it and a link opens it.
  const [openRole, setOpenRole] = useState("");
  useEffect(() => {
    const sync = () => {
      try {
        setOpenRole(new URL(window.location.href).searchParams.get("role") ?? "");
      } catch { /* non-browser */ }
    };
    sync();
    window.addEventListener("popstate", sync);
    return () => window.removeEventListener("popstate", sync);
  }, []);
  // MAKE ONE. The page listed 26 agents and offered no way to add a 27th — the only creation
  // wizard in BOSS lives in Agent Studio and writes `agent_definitions`, which this roster does
  // not read. Same wizard, now registering the role too, so what it makes appears here.
  const [creating, setCreating] = useState(false);
  const [roleOptions, setRoleOptions] = useState<RoleFeedItem[]>([]);
  useEffect(() => {
    if (!creating) return;
    let alive = true;
    (async () => {
      try {
        const r = await fetch(
          `/api/agent?view=roles&project_id=${encodeURIComponent(workforce.project_id)}`,
          { cache: "no-store" });
        if (!r.ok) return;
        const body = await r.json();
        if (!alive) return;
        // Only the four fields the wizard's picker reads. The rest of RoleFeedItem is a
        // scoreboard this list has no use for, and inventing zeros for it would put numbers on
        // screen that describe nothing.
        setRoleOptions(((body.items ?? []) as Array<Record<string, unknown>>).map((x) => ({
          id: String(x.id ?? x.role_key ?? ""),
          roleKey: String(x.role_key ?? ""),
          displayName: String(x.display_name ?? x.role_key ?? ""),
          autonomyLevel: Number(x.autonomy_level ?? 0),
        } as RoleFeedItem)));
      } catch { /* the wizard still offers "a new role", which is the point of it here */ }
    })();
    return () => { alive = false; };
  }, [creating, workforce.project_id]);

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
  const agents = useMemo(() => {
    const list = workforce.agents ?? [];
    const scoped = filter === "working"
      ? list.filter((a) => a.status === "WORKING" || a.status === "STUCK")
      : filter === "worked" ? list.filter((a) => volumeOf(a) > 0) : list;
    const query = search.trim().toLowerCase();
    return query ? scoped.filter((a) => `${a.name} ${a.role} ${a.task}`.toLowerCase().includes(query)) : scoped;
  }, [workforce.agents, filter, search]);

  const counts = workforce.counts ?? { WORKING: 0, STUCK: 0, IDLE: 0, total: 0 };
  const unregisteredCount = (workforce.agents ?? []).filter((agent) => !agent.registered).length;
  const shownAgents = filter === "all" && !search.trim() && !showAll
    ? agents.slice(0, 8) : agents;

  if (!workforce.apiReachable) {
    return (
      <p className="detail">
        Could not reach the control plane{workforce.blockedReason ? ` — ${workforce.blockedReason}` : ""}.
        An empty roster would read as a company with no agents.
      </p>
    );
  }


  // The detail view, over the roster. Rendered here rather than as a route because the console
  // mounts every area at once and a route would be a second place agents can be listed.
  if (openRole) {
    return <AgentDetail projectId={workforce.project_id} roleKey={openRole}
                        onClose={closeAgent} />;
  }
  return (
    <div className="roster">
      <div className="roster-filterbar">
        <span className="detail">
          {counts.total} role{counts.total === 1 ? "" : "s"} with recorded work
        </span>
        <span className="roster-filters">
          {(["all", "working", "worked"] as const).map((f) => (
            <button
              key={f}
              type="button"
              className="cap-btn"
              data-active={filter === f}
              onClick={() => setFilter(f)}
              data-testid={`wf-filter-${f}`}
            >
              {f === "all" ? `All ${counts.total}` : f === "working" ? `Working or stuck ${counts.WORKING + counts.STUCK}` : "Has done work"}
            </button>
          ))}
          {/* A ROSTER YOU CANNOT ADD TO. The page listed 26 agents and had no way to make a
              27th — the only wizard in BOSS is in Agent Studio, and it writes
              `agent_definitions`, which this roster does not read. */}
          <button type="button" className="cap-btn roster-new" data-testid="wf-new-agent"
                  onClick={() => setCreating(true)}>Create agent</button>
        </span>
      </div>
      <div className="roster-guide">
        <span>Work is grouped by role.</span>
        <AgentModelHelp />
        <button type="button" onClick={() => openArea("org_chart")}>Team structure</button>
        <button type="button" onClick={() => openArea("studio")}>Agent Studio</button>
      </div>
      <input className="roster-search" type="search" aria-label="Find an agent role"
             placeholder="Find an agent role" value={search}
             onChange={(event) => setSearch(event.target.value)} />

      {creating ? (
        // SAME WIZARD, SO IT NEEDS THE SAME SKILLS. This passed `[]`, so the commission
        // dialog opened from the Agents page always said "No installed skills are available
        // to request" — the identical dialog opened from Agent Studio offered them.
        <AgentCreationWizard projectId={workforce.project_id} roles={roleOptions} skills={skills}
                             onClose={() => setCreating(false)} />
      ) : null}

      {/* Governance gap, said once and at the top: an agent running work without a registered
          role has no trust score, no autonomy level and no violation tracking. */}
      {unregisteredCount > 0 ? (
        <p className="detail roster-ungoverned" data-testid="roster-ungoverned">
          Work is attributed to {unregisteredCount} unregistered role{unregisteredCount === 1 ? "" : "s"}.
          Review authority before another run.
        </p>
      ) : null}

      {agents.length === 0 ? (
        (workforce.agents ?? []).length === 0 && filter === "all" && !search.trim() ? (
          <div className="roster-empty" data-testid="workforce-empty">
            <p className="detail">No customer agent work recorded yet. Browse agent definitions in Agent Studio, then run an agent or workflow to see work here.</p>
            <button type="button" className="cap-btn" onClick={() => openArea("studio")}>Browse agent definitions</button>
          </div>
        ) : <p className="detail">No agent work matches this filter.</p>
      ) : (
        <div className="roster-grid" data-testid="roster-grid">
          {shownAgents.map((a) => (
            <article className="roster-card" key={a.id} data-status={a.status}
                     data-testid={`wf-agent-${a.id}`}>
              <header className="roster-card-head">
                <span className="roster-name">{a.name}</span>
                <span className="roster-state">{a.status === "IDLE" ? "Idle" : a.status === "WORKING" ? "Working" : a.status === "STUCK" ? "Stuck" : "At gate"}</span>
              </header>
              <p className="roster-task">{a.last_touched || (a.task === "No work in this window." ? "No recent work" : a.task.replace(/^last: /, "Last: "))}</p>
              <div className="roster-actions">
                <button type="button" className="roster-open" onClick={() => openAgent(a.id)}
                        data-testid={`wf-open-${a.id}`}>Details</button>
                <button type="button" onClick={() => openAgentDefinition(a.id)}
                        data-testid={`wf-definition-${a.id}`}>Definition</button>
                <button type="button" className="roster-ask" onClick={() => askAgent(a.name)}
                        data-testid={`wf-ask-${a.id}`}>Ask</button>
              </div>
            </article>
          ))}
        </div>
      )}
      {!showAll && filter === "all" && !search.trim() && agents.length > shownAgents.length ? (
        <button type="button" className="roster-show-all" onClick={() => setShowAll(true)}>
          Show all {agents.length} roles
        </button>
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
  type AgentTemplate = {
    id: string; name: string; description: string; role_key: string;
    category: string; installed: boolean; active: boolean;
    mobile?: { execution_status: string; device_targets: string[] } | null;
  };
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
  const [templateOpen, setTemplateOpen] = useState(false);
  const [templates, setTemplates] = useState<AgentTemplate[]>([]);
  const [templateSearch, setTemplateSearch] = useState("");
  const [templateError, setTemplateError] = useState<string | null>(null);
  const [installing, setInstalling] = useState<string | null>(null);
  const [installedTemplate, setInstalledTemplate] = useState<string | null>(null);
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

  useEffect(() => {
    if (!templateOpen) return;
    let alive = true;
    void fetch(`/v1/agent-studio/templates?project_id=${encodeURIComponent(projectId)}`,
      { cache: "no-store" })
      .then(async (response) => {
        const body = await response.json().catch(() => ({}));
        if (!response.ok) throw new Error(body.detail ?? `Templates returned ${response.status}`);
        return body;
      })
      .then((body) => { if (alive) { setTemplates(body.items ?? []); setTemplateError(null); } })
      .catch((error) => { if (alive) setTemplateError(String(error.message ?? error)); });
    return () => { alive = false; };
  }, [templateOpen, projectId]);

  const installTemplate = useCallback(async (templateId: string) => {
    setInstalling(templateId); setTemplateError(null);
    try {
      const response = await apiWrite(
        `/v1/agent-studio/templates/${encodeURIComponent(templateId)}/install`, {
          method: "POST", headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ project_id: projectId, actor }),
        });
      const body = await response.json().catch(() => ({}));
      if (!response.ok) throw new Error(body.detail ?? `Install returned ${response.status}`);
      setTemplates((current) => current.map((item) => item.id === templateId
        ? { ...item, installed: true, active: true } : item));
      setInstalledTemplate(templateId);
      setNote("Added to Agent Studio. The team structure is unchanged; no work has started.");
    } catch (error) {
      setTemplateError(String((error as Error).message ?? error));
    } finally { setInstalling(null); }
  }, [projectId, actor]);

  const visibleTemplates = templates.filter((item) =>
    `${item.name} ${item.description} ${item.category}`.toLowerCase()
      .includes(templateSearch.trim().toLowerCase()));

  const reload = useCallback(async () => {
    try {
      const r = await fetch(`/api/org?project_id=${encodeURIComponent(projectId)}`,
                            { cache: "no-store" });
      if (r.ok) setLive(await r.json());
    } catch { /* keep what is on screen; a failed refresh is not a reason to blank it */ }
  }, [projectId]);

  const place = useCallback(async (agentId: string, reportsTo: string | null) => {
    setBusy(true); setNote(null); setInstalledTemplate(null);
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
    setBusy(true); setNote(null); setInstalledTemplate(null);
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

  if (!chartNow.apiReachable) {
    return <p className="detail" role="alert">
      Team structure is unavailable{chartNow.blockedReason ? ` — ${chartNow.blockedReason}` : ""}.
    </p>;
  }

  return (
    <div className="org" data-declared={view === "planned" && placedCount > 0}
         data-view={view}>
      <div className="org-viewbar">
        <div className="org-views" role="tablist" aria-label="Team structure views">
          <button type="button" role="tab" aria-selected={view === "planned"}
                  onClick={() => setView("planned")}>Planned roles</button>
          <button type="button" role="tab" aria-selected={view === "observed"}
                  onClick={() => setView("observed")}>Actual handoffs</button>
        </div>
        <AgentModelHelp />
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
        {view === "planned" ? <button type="button" className="cap-btn" aria-expanded={templateOpen}
                aria-controls="org-template-picker" data-testid="org-template-toggle"
                onClick={() => setTemplateOpen((value) => !value)}>
          {templateOpen ? "Close templates" : "Add agent from template"}
        </button> : null}
      </div>
      {view === "planned" && templateOpen ? (
        <section className="org-templates" id="org-template-picker"
                 aria-label="Agent templates" data-testid="org-templates">
          <div className="org-template-head">
            <div>
              <strong>Agent templates</strong>
              <p>Templates create agent definitions in Studio. They do not add reporting lines or start work.</p>
            </div>
            <input type="search" aria-label="Search agent templates" placeholder="Find a role"
                   value={templateSearch} onChange={(event) => setTemplateSearch(event.target.value)} />
          </div>
          {templateError ? <p role="alert" className="org-template-error">{templateError}</p> : null}
          <div className="org-template-list">
            {visibleTemplates.map((item) => (
              <article className="org-template" key={item.id}>
                <div>
                  <strong>{item.name}</strong>
                  <span>{item.category} · {humanise(item.role_key)}</span>
                  <p>{item.description}</p>
                  {item.mobile ? (
                    <small>Device work needs a configured Android or macOS/Xcode runner.</small>
                  ) : null}
                </div>
                <button type="button" className="cap-btn" disabled={item.installed || !!installing}
                        data-testid={`org-template-${item.id}`}
                        onClick={() => void installTemplate(item.id)}>
                  {item.installed ? "In Studio" : installing === item.id ? "Adding…" : "Add"}
                </button>
              </article>
            ))}
          </div>
          {!templateError && templates.length === 0 ? <p className="detail">Loading templates…</p> : null}
          {templates.length > 0 && visibleTemplates.length === 0 ? <p className="detail">No matching templates.</p> : null}
        </section>
      ) : null}
      {note ? <p className="org-note" data-tone={installedTemplate ? "ok" : "error"}
                 data-testid="org-note">{note}</p> : null}
      {installedTemplate ? (
        <button type="button" className="org-next-action"
                onClick={() => openAgentDefinition(
                  templates.find((item) => item.id === installedTemplate)?.role_key ?? installedTemplate)}>
          Open definition in Agent Studio
        </button>
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
                    <button type="button" className="org-node-act" onClick={() => openRoleWork(n.id)}
                            data-testid={`org-work-${n.id}`}>Work</button>
                    <button type="button" className="org-node-act" onClick={() => openAgentDefinition(n.id)}
                            data-testid={`org-definition-${n.id}`}>Definition</button>
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
          <button type="button" className="org-node-act" onClick={() => openArea("studio")}>Browse agent definitions</button>
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
                <button type="button" className="org-node-act" onClick={() => openRoleWork(n.id)}
                        data-testid={`org-work-${n.id}`}>Work</button>
                <button type="button" className="org-node-act" onClick={() => openAgentDefinition(n.id)}
                        data-testid={`org-definition-${n.id}`}>Definition</button>
                <select aria-label={`Reports to for ${displayName(n)}`}
                        value={placementTargets[n.id] ?? ""}
                        onChange={(event) => setPlacementTargets((current) =>
                          ({ ...current, [n.id]: event.target.value }))}>
                  <option value="">Top-level role</option>
                  {(chartNow.tree ?? []).map((parent) => (
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
              <p className="detail">No available roles match. Browse Agent Studio to add a definition.</p>
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
                <button type="button" className="org-node-act" onClick={() => openRoleWork(n.id)}
                        data-testid={`org-work-${n.id}`}>Work</button>
                <button type="button" className="org-node-act" onClick={() => openAgentDefinition(n.id)}
                        data-testid={`org-definition-${n.id}`}>Definition</button>
              </div>
            ))}
          </div>
        </details>
      ) : null}
    </div>
  );
}
